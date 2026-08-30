use crate::audio::capture::AudioBackend;
use crate::audio::AudioDevice;
use crate::db;
use crate::recovery::{RecoveryMeta, RecoveryStatus};
use crate::report::SavedReportFiles;
use crate::state::RecordingStatus;
use crate::upload::UploadSessionTracksResult;
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

    // Register the session in the local SQLite index immediately so the
    // WAV files and session list stay consistent, even if the app exits
    // before stop_capture runs.
    state
        .db
        .create_session(
            &session_id,
            None,
            mic_wav_path.to_str(),
            loopback_wav_path.to_str(),
            16000,
            Some("recording"),
        )
        .map_err(|e| format!("failed to register local session: {e}"))?;

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

    let ended_at = chrono::Utc::now().to_rfc3339();
    let duration_secs = stop_info
        .started_at
        .map(|started| (chrono::Utc::now() - started).num_milliseconds() as f64 / 1000.0);

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

            if let Err(e) = state.db.update_session(
                &stop_info.session_id,
                None,
                Some("completed"),
                Some(&ended_at),
                duration_secs,
                Some("finalized"),
                None,
            ) {
                eprintln!("[stop_capture] db update failed: {e}");
            }

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

            if let Err(db_err) = state.db.update_session(
                &stop_info.session_id,
                None,
                Some("failed"),
                Some(&ended_at),
                duration_secs,
                Some("failed"),
                Some(&e.to_string()),
            ) {
                eprintln!("[stop_capture] db update failed: {db_err}");
            }

            Err(e.to_string())
        }
    }
}

/// List sessions that can be recovered after an abnormal exit.
#[tauri::command]
pub fn list_recoverable_sessions(state: State<'_, AppState>) -> Vec<RecoveryMeta> {
    crate::recovery::list_recoverable(&state.sessions_dir)
}

/// Delete a local session: cascade DB records, then remove local files.
/// Path validation prevents traversal. Errors are recoverable (reported, not fatal).
#[tauri::command]
pub fn delete_local_session(
    session_id: String,
    state: State<'_, AppState>,
) -> Result<db::LocalSession, String> {
    let session_dir = crate::paths::validate_session_path(&session_id, &state.sessions_dir)
        .map_err(|e| e.to_string())?;

    // Cascade-delete DB records first (derived data for this session only)
    let session = state
        .db
        .cascade_delete_session(&session_id, &state.sessions_dir)
        .map_err(|e| e.to_string())?;

    // Remove local audio/report files
    if session_dir.exists() {
        std::fs::remove_dir_all(&session_dir).map_err(|e| format!("file cleanup failed: {}", e))?;
    }

    Ok(session)
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

    // Sync recovery status to DB if session exists there
    let _ = state
        .db
        .update_session(&session_id, None, None, None, None, Some("finalized"), None);

    Ok(())
}

// ── Local session repository commands ───────────────────────────────────────

/// Create a local session record in the SQLite repository.
#[tauri::command]
pub fn create_local_session(
    title: Option<String>,
    mic_path: Option<String>,
    loopback_path: Option<String>,
    sample_rate: Option<i64>,
    recovery_status: Option<String>,
    state: State<'_, AppState>,
) -> Result<db::LocalSession, String> {
    let id = uuid::Uuid::new_v4().to_string();
    state
        .db
        .create_session(
            &id,
            title.as_deref(),
            mic_path.as_deref(),
            loopback_path.as_deref(),
            sample_rate.unwrap_or(16000),
            recovery_status.as_deref(),
        )
        .map_err(|e| e.to_string())
}

/// Update a local session's metadata.
#[tauri::command]
pub fn update_local_session(
    session_id: String,
    title: Option<String>,
    status: Option<String>,
    ended_at: Option<String>,
    duration_secs: Option<f64>,
    recovery_status: Option<String>,
    error_code: Option<String>,
    state: State<'_, AppState>,
) -> Result<db::LocalSession, String> {
    state
        .db
        .update_session(
            &session_id,
            title.as_deref(),
            status.as_deref(),
            ended_at.as_deref(),
            duration_secs,
            recovery_status.as_deref(),
            error_code.as_deref(),
        )
        .map_err(|e| e.to_string())
}

/// List local sessions, optionally filtered by status.
#[tauri::command]
pub fn list_local_sessions(
    status: Option<String>,
    state: State<'_, AppState>,
) -> Result<Vec<db::LocalSession>, String> {
    state
        .db
        .list_sessions(status.as_deref())
        .map_err(|e| e.to_string())
}

/// Get a single local session by ID.
#[tauri::command]
pub fn get_local_session(
    session_id: String,
    state: State<'_, AppState>,
) -> Result<db::LocalSession, String> {
    state.db.get_session(&session_id).map_err(|e| e.to_string())
}

/// Save analysis results (analysis entries + memory candidates) for a session.
#[tauri::command]
pub fn save_analysis_results(
    session_id: String,
    results: Vec<db::AnalysisResult>,
    memory_candidates: Vec<db::MemoryCandidate>,
    state: State<'_, AppState>,
) -> Result<db::AnalysisResultsBundle, String> {
    state
        .db
        .save_analysis_results(&session_id, &results, &memory_candidates)
        .map_err(|e| e.to_string())
}

/// Get all analysis results and memory candidates for a session.
#[tauri::command]
pub fn get_analysis_results(
    session_id: String,
    state: State<'_, AppState>,
) -> Result<db::AnalysisResultsBundle, String> {
    state
        .db
        .get_analysis_results(&session_id)
        .map_err(|e| e.to_string())
}

/// List source relations that involve a local session.
#[tauri::command]
pub fn list_source_relations(
    session_id: String,
    state: State<'_, AppState>,
) -> Result<Vec<db::SourceRelation>, String> {
    state
        .db
        .list_source_relations(&session_id)
        .map_err(|e| e.to_string())
}

/// Read a bounded PCM WAV evidence clip without exposing its local path.
#[tauri::command]
pub fn read_evidence_clip(
    session_id: String,
    track: String,
    start_ms: u64,
    end_ms: u64,
    state: State<'_, AppState>,
) -> Result<crate::evidence::EvidenceClip, String> {
    crate::evidence::read_evidence_clip_impl(
        &session_id,
        &track,
        start_ms,
        end_ms,
        &state.sessions_dir,
    )
    .map_err(|error| error.to_string())
}
/// Copy an imported MP3/WAV/M4A/MP4 into a new private local session.
#[tauri::command]
pub fn import_media_file(
    source_path: String,
    title: Option<String>,
    state: State<'_, AppState>,
) -> Result<crate::importer::ImportedSession, String> {
    crate::importer::import_media_file_impl(
        &source_path,
        title.as_deref(),
        &state.sessions_dir,
        &state.db,
    )
    .map_err(|error| error.to_string())
}

/// Save UTF-8 text as the source of a new private local session.
#[tauri::command]
pub fn import_text_content(
    text: String,
    title: Option<String>,
    source_name: Option<String>,
    state: State<'_, AppState>,
) -> Result<crate::importer::ImportedSession, String> {
    crate::importer::import_text_content_impl(
        &text,
        title.as_deref(),
        source_name.as_deref(),
        &state.sessions_dir,
        &state.db,
    )
    .map_err(|error| error.to_string())
}
/// Upload session audio tracks to the gateway.
///
/// Validates IDs and URL, reads gateway token from Windows Credential Manager,
/// streams SHA-256 checksums, uploads chunks with retry, and verifies completion.
#[tauri::command]
pub async fn upload_session_tracks(
    local_session_id: String,
    gateway_session_id: String,
    gateway_base_url: String,
    state: State<'_, AppState>,
) -> Result<UploadSessionTracksResult, String> {
    crate::upload::upload_session_tracks_impl(
        local_session_id,
        gateway_session_id,
        gateway_base_url,
        &state.sessions_dir,
    )
    .await
    .map_err(|e| {
        // Redact token-related errors
        let msg = e.to_string();
        if msg.contains("gateway_token") || msg.contains("bearer") {
            "authentication error (details redacted)".to_string()
        } else {
            msg
        }
    })
}

/// Save report files (JSON, Markdown, HTML) for a session.
///
/// Validates the session, enforces size limits, atomically writes files,
/// and updates the SQLite analysis record.
#[tauri::command]
pub fn save_report_files(
    local_session_id: String,
    analysis_json: String,
    markdown: String,
    html: String,
    state: State<'_, AppState>,
) -> Result<SavedReportFiles, String> {
    crate::report::save_report_files_impl(
        &local_session_id,
        &analysis_json,
        &markdown,
        &html,
        &state.sessions_dir,
        &state.db,
    )
    .map_err(|e| e.to_string())
}

// ── Gateway connectivity ────────────────────────────────────────────────────

/// Check if the analysis gateway is reachable and healthy.
/// Optionally override the gateway URL; otherwise reads from saved config.
#[tauri::command]
pub async fn check_gateway(
    gateway_url: Option<String>,
    state: State<'_, AppState>,
) -> Result<crate::gateway_check::GatewayStatus, String> {
    let url =
        gateway_url.unwrap_or_else(|| crate::gateway_check::load_gateway_url(&state.sessions_dir));
    Ok(crate::gateway_check::check_gateway_health(&url).await)
}

/// Persist a gateway URL for future sessions.
#[tauri::command]
pub fn set_gateway_url(gateway_url: String, state: State<'_, AppState>) -> Result<(), String> {
    crate::gateway_check::save_gateway_url(&state.sessions_dir, &gateway_url)
}

/// Return a user-saved gateway URL, or null on first run so the frontend can
/// retain the HTTPS URL embedded in the desktop bundle.
#[tauri::command]
pub fn get_gateway_url(state: State<'_, AppState>) -> Option<String> {
    crate::gateway_check::load_saved_gateway_url(&state.sessions_dir)
}

/// Current gateway runtime connection info prepared by the supervisor
/// (managed sidecar or external dev gateway), or null when no runtime is
/// active. The one-time token travels only over local IPC — never in a URL.
#[tauri::command]
pub async fn gateway_connection(
    state: State<'_, AppState>,
) -> Result<Option<crate::gateway_supervisor::GatewayConnectionInfo>, String> {
    let mut supervisor = state.gateway.lock().await;
    Ok(supervisor.connection())
}

/// Explicitly (re)start the bundled gateway sidecar on a random loopback
/// port. Fails with a stable error while no sidecar binary is bundled.
#[tauri::command]
pub async fn start_gateway_sidecar(
    state: State<'_, AppState>,
) -> Result<crate::gateway_supervisor::GatewayConnectionInfo, String> {
    let program = crate::gateway_supervisor::resolve_sidecar_binary()
        .ok_or_else(|| "gateway sidecar binary is not bundled with this build yet".to_string())?;
    let config = crate::gateway_supervisor::SupervisorConfig::for_sidecar(program)
        .with_data_dir(state.sessions_dir.join("gateway"));
    state
        .gateway
        .lock()
        .await
        .start_sidecar(&config)
        .await
        .map_err(|error| error.to_string())
}

// --- LLM config ---

/// Load user LLM configuration (endpoints, model names, workspace ID).
/// API keys are stored separately in Windows Credential Manager.
#[tauri::command]
pub fn get_llm_config(state: State<'_, AppState>) -> crate::llm_config::LlmConfig {
    crate::llm_config::load_llm_config(&state.sessions_dir)
}

/// Save user LLM configuration (non-secret fields only).
/// API keys are saved via credential_set.
#[tauri::command]
pub fn set_llm_config_file(
    text_endpoint: String,
    text_model: String,
    audio_endpoint: String,
    workspace_id: String,
    state: State<'_, AppState>,
) -> Result<(), String> {
    crate::llm_config::save_llm_config(
        &state.sessions_dir,
        &crate::llm_config::LlmConfig {
            text_endpoint,
            text_model,
            audio_endpoint,
            workspace_id,
        },
    )
}

// ── Live PCM streaming (native audio → frontend for real-time captioning) ──

/// Start a native live PCM stream that captures system audio, microphone, or both.
/// The stream buffers 16kHz mono PCM16 LE bytes that the frontend polls via `poll_live_pcm`.
///
/// `source`: "system" (loopback), "mic" (microphone), "mixed" (both averaged).
#[tauri::command]
pub fn start_live_stream(
    source: String,
    mic_device_id: Option<String>,
    render_device_id: Option<String>,
    state: State<'_, AppState>,
) -> Result<(), String> {
    use crate::audio::live_pcm;
    use std::sync::atomic::AtomicBool;
    use std::sync::Arc;

    let mut guard = state.live.stream.lock();
    if guard.is_some() {
        return Err("live stream already active".into());
    }

    let stream = live_pcm::start_live_stream(
        &source,
        mic_device_id.as_deref(),
        render_device_id.as_deref(),
        Arc::new(AtomicBool::new(false)),
    )?;
    *guard = Some(stream);
    Ok(())
}

/// Pause the live PCM stream: capture keeps running but no audio is emitted
/// until `resume_live_stream`, so subtitles stop while local WAV recording
/// remains under the separate `pause_capture` control.
#[tauri::command]
pub fn pause_live_stream(state: State<'_, AppState>) -> Result<(), String> {
    let guard = state.live.stream.lock();
    match guard.as_ref() {
        Some(stream) => {
            stream.set_paused(true);
            Ok(())
        }
        None => Err("no active live stream".into()),
    }
}

/// Resume emission of a paused live PCM stream.
#[tauri::command]
pub fn resume_live_stream(state: State<'_, AppState>) -> Result<(), String> {
    let guard = state.live.stream.lock();
    match guard.as_ref() {
        Some(stream) => {
            stream.set_paused(false);
            Ok(())
        }
        None => Err("no active live stream".into()),
    }
}

/// Poll buffered PCM bytes from the live stream. Returns a base64-encoded string
/// of 16kHz mono PCM16 LE data. Returns empty string if no data is available.
#[tauri::command]
pub fn poll_live_pcm(state: State<'_, AppState>) -> Result<String, String> {
    let guard = state.live.stream.lock();
    match guard.as_ref() {
        Some(stream) => {
            if let Some(error) = stream.take_error() {
                return Err(format!("live audio capture failed: {error}"));
            }
            let bytes = stream.poll();
            if bytes.is_empty() {
                Ok(String::new())
            } else {
                Ok(base64_encode(&bytes))
            }
        }
        None => Err("no active live stream".into()),
    }
}

/// Stop the live PCM stream and return any remaining buffered data.
#[tauri::command]
pub fn stop_live_stream(state: State<'_, AppState>) -> Result<(), String> {
    let mut guard = state.live.stream.lock();
    if let Some(mut stream) = guard.take() {
        stream.stop();
    }
    Ok(())
}

/// Encode bytes as base64 (no external dependency — inline implementation).
fn base64_encode(data: &[u8]) -> String {
    const CHARS: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(CHARS[((triple >> 18) & 0x3F) as usize] as char);
        out.push(CHARS[((triple >> 12) & 0x3F) as usize] as char);
        if chunk.len() > 1 {
            out.push(CHARS[((triple >> 6) & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(CHARS[(triple & 0x3F) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}
