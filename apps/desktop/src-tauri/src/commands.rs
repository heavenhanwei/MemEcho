use crate::audio::capture::AudioBackend;
use crate::audio::AudioDevice;
use crate::recovery::{RecoveryMeta, RecoveryStatus};
use crate::state::RecordingStatus;
use crate::AppState;
use std::path::PathBuf;
use tauri::State;

#[derive(Debug, serde::Serialize)]
pub struct CaptureInfo {
    pub session_id: String,
    pub mic_path: PathBuf,
    pub loopback_path: PathBuf,
}

#[derive(Debug, serde::Serialize)]
pub struct StopResult {
    pub session_id: String,
    pub mic_path: PathBuf,
    pub loopback_path: PathBuf,
}

/// List available audio devices using WASAPI IMMDeviceEnumerator.
#[tauri::command]
pub fn list_audio_devices() -> Result<Vec<AudioDevice>, String> {
    #[cfg(windows)]
    {
        let backend = crate::audio::capture::wasapi::WasapiBackend::new();
        backend.enumerate_devices().map_err(|e| e.to_string())
    }
    #[cfg(not(windows))]
    {
        Err("Audio device enumeration not supported on this platform".into())
    }
}

/// Start audio capture.
#[tauri::command]
pub fn start_capture(
    mic_device_id: Option<String>,
    render_device_id: Option<String>,
    state: State<'_, AppState>,
) -> Result<CaptureInfo, String> {
    let mut capture = state.capture.lock();

    if capture.status != RecordingStatus::Idle {
        return Err("Already recording".into());
    }

    let session_id = uuid::Uuid::new_v4().to_string();
    let session_dir = state.sessions_dir.join(&session_id);
    std::fs::create_dir_all(&session_dir).map_err(|e| e.to_string())?;

    crate::paths::validate_session_path(&session_id, &state.sessions_dir)
        .map_err(|e| e.to_string())?;

    let mic_wav_path = crate::paths::mic_wav_path(&session_dir);
    let loopback_wav_path = crate::paths::loopback_wav_path(&session_dir);

    capture
        .start_recording(
            session_id.clone(),
            mic_wav_path.clone(),
            loopback_wav_path.clone(),
        )
        .map_err(|e| e.to_string())?;

    let started_at = chrono::Utc::now();

    let mic_wav =
        crate::audio::wav::create_streaming_wav(&mic_wav_path, 16000).map_err(|e| e.to_string())?;
    let loop_wav = crate::audio::wav::create_streaming_wav(&loopback_wav_path, 16000)
        .map_err(|e| e.to_string())?;

    #[cfg(windows)]
    {
        let backend = crate::audio::capture::wasapi::WasapiBackend::new();
        let mic_device = backend
            .resolve_device(mic_device_id.as_deref(), true)
            .map_err(|e| e.to_string())?;
        let loop_device = backend
            .resolve_device(render_device_id.as_deref(), false)
            .map_err(|e| e.to_string())?;
        let backend2 = crate::audio::capture::wasapi::WasapiBackend::new();

        let mut audio = state.audio.lock();
        audio
            .start_with_backends(
                backend,
                backend2,
                mic_device,
                loop_device,
                mic_wav,
                loop_wav,
                session_dir.clone(),
                mic_wav_path.clone(),
                loopback_wav_path.clone(),
                started_at,
            )
            .map_err(|e| e.to_string())?;
    }

    let meta = RecoveryMeta {
        session_id: session_id.clone(),
        mic_path: mic_wav_path.clone(),
        loopback_path: loopback_wav_path.clone(),
        sample_rate: 16000,
        started_at,
        mic_offset: 0,
        loopback_offset: 0,
        status: RecoveryStatus::Recording,
        error_code: None,
    };
    meta.save(&session_dir).map_err(|e| e.to_string())?;

    Ok(CaptureInfo {
        session_id,
        mic_path: mic_wav_path,
        loopback_path: loopback_wav_path,
    })
}

/// Pause the current capture.
#[tauri::command]
pub fn pause_capture(state: State<'_, AppState>) -> Result<(), String> {
    let mut capture = state.capture.lock();
    let audio = state.audio.lock();
    capture.pause().map_err(|e| e.to_string())?;
    audio.pause();
    Ok(())
}

/// Resume the current capture after pause.
#[tauri::command]
pub fn resume_capture(state: State<'_, AppState>) -> Result<(), String> {
    let mut capture = state.capture.lock();
    let audio = state.audio.lock();
    capture.resume().map_err(|e| e.to_string())?;
    audio.resume();
    Ok(())
}

/// Stop the current capture.
///
/// Capture threads finalize WAV headers before returning.
/// Recovery supervisor writes Finalized/Failed status.
///
/// `audio.stop()` joins all handles unconditionally. If it returns an error,
/// we still have the durable byte offsets from the AtomicU64 counters
/// (updated by each capture thread's final flush_safe) and can write them
/// into Failed recovery metadata so crash recovery can truncate to a
/// known-good position.
#[tauri::command]
pub fn stop_capture(state: State<'_, AppState>) -> Result<StopResult, String> {
    let mut capture = state.capture.lock();
    let mut audio = state.audio.lock();

    let stop_info = capture.stop().map_err(|e| e.to_string())?;

    let audio_result = audio.stop();

    match audio_result {
        Ok((mic_result, loop_result)) => {
            // Both tracks succeeded — write Finalized with confirmed bytes
            crate::audio::write_final_recovery(
                &state.sessions_dir.join(&stop_info.session_id),
                &stop_info.mic_path,
                &stop_info.loopback_path,
                stop_info.started_at.unwrap_or_else(chrono::Utc::now),
                mic_result.bytes_written,
                loop_result.bytes_written,
                RecoveryStatus::Finalized,
                None,
            );

            Ok(StopResult {
                session_id: stop_info.session_id,
                mic_path: stop_info.mic_path,
                loopback_path: stop_info.loopback_path,
            })
        }
        Err(e) => {
            // At least one track failed. audio.stop() joins all handles
            // unconditionally, so we know the threads are done. The
            // AtomicU64 counters were updated by each thread's final
            // flush_safe before returning, so pre_mic_bytes/pre_loop_bytes
            // reflect the last durable offset. However, they may have been
            // updated further by the finalize path. Re-read them.
            let (mic_bytes, loop_bytes) = audio.bytes_written();

            crate::audio::write_final_recovery(
                &state.sessions_dir.join(&stop_info.session_id),
                &stop_info.mic_path,
                &stop_info.loopback_path,
                stop_info.started_at.unwrap_or_else(chrono::Utc::now),
                mic_bytes,
                loop_bytes,
                RecoveryStatus::Failed,
                Some(e.to_string()),
            );

            Err(e.to_string())
        }
    }
}

/// List sessions that can be recovered after an abnormal exit.
#[tauri::command]
pub fn list_recoverable_sessions(state: State<'_, AppState>) -> Vec<RecoveryMeta> {
    crate::recovery::list_recoverable(&state.sessions_dir)
}

/// Delete a local session directory and all its contents.
#[tauri::command]
pub fn delete_local_session(session_id: String, state: State<'_, AppState>) -> Result<(), String> {
    let session_dir = crate::paths::validate_session_path(&session_id, &state.sessions_dir)
        .map_err(|e| e.to_string())?;
    if !session_dir.exists() {
        return Err("Session not found".into());
    }
    std::fs::remove_dir_all(&session_dir).map_err(|e| e.to_string())?;
    Ok(())
}

/// Store a credential in Windows Credential Manager.
#[tauri::command]
pub fn credential_set(name: String, secret: String) -> Result<(), String> {
    crate::credential::credential_set(&name, &secret).map_err(|e| e.to_string())
}

/// Retrieve a credential from Windows Credential Manager.
#[tauri::command]
pub fn credential_get(name: String) -> Result<String, String> {
    crate::credential::credential_get(&name).map_err(|e| e.to_string())
}

/// Delete a credential from Windows Credential Manager.
#[tauri::command]
pub fn credential_delete(name: String) -> Result<(), String> {
    crate::credential::credential_delete(&name).map_err(|e| e.to_string())
}

/// Attempt to recover an interrupted session by truncating WAV files to
/// the safe offset recorded in recovery.json and fixing headers.
#[tauri::command]
pub fn recover_session(session_id: String, state: State<'_, AppState>) -> Result<(), String> {
    let session_dir = crate::paths::validate_session_path(&session_id, &state.sessions_dir)
        .map_err(|e| e.to_string())?;
    let meta = RecoveryMeta::load(&session_dir).map_err(|e| e.to_string())?;
    let mic_wav = crate::paths::mic_wav_path(&session_dir);
    let loop_wav = crate::paths::loopback_wav_path(&session_dir);

    if mic_wav.exists() && meta.mic_offset > 0 {
        crate::audio::wav::truncate_and_fixup_wav(&mic_wav, meta.mic_offset)
            .map_err(|e| e.to_string())?;
    }
    if loop_wav.exists() && meta.loopback_offset > 0 {
        crate::audio::wav::truncate_and_fixup_wav(&loop_wav, meta.loopback_offset)
            .map_err(|e| e.to_string())?;
    }

    let final_meta = RecoveryMeta {
        status: RecoveryStatus::Finalized,
        ..meta
    };
    final_meta.save(&session_dir).map_err(|e| e.to_string())?;
    Ok(())
}
