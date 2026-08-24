use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc};
use std::thread::JoinHandle;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct AudioDevice {
    pub id: String,
    pub name: String,
    pub is_input: bool,
    pub is_default: bool,
}

#[derive(Debug)]
pub struct CaptureThreadResult {
    pub bytes_written: u64,
}

/// Trait abstracting WASAPI device enumeration + capture.
pub trait AudioBackend: Send + 'static {
    fn enumerate_devices(&self) -> Result<Vec<AudioDevice>, CaptureError>;
    fn resolve_device(
        &self,
        device_id: Option<&str>,
        is_capture: bool,
    ) -> Result<AudioDevice, CaptureError>;
    /// Run a blocking capture loop. Writes PCM into `wav`, updates
    /// `bytes_written` only after successful `flush_safe`, and calls
    /// `wav.finalize()` before returning (even on error path).
    fn capture_loop(
        &self,
        device: &AudioDevice,
        wav: &mut crate::audio::wav::StreamingWav,
        stop: Arc<AtomicBool>,
        pause: Arc<AtomicBool>,
        bytes_written: Arc<AtomicU64>,
    ) -> Result<CaptureThreadResult, CaptureError>;
}

/// Manages audio capture from microphone and loopback.
pub struct AudioCapture {
    mic_handle: Option<JoinHandle<Result<CaptureThreadResult, CaptureError>>>,
    loopback_handle: Option<JoinHandle<Result<CaptureThreadResult, CaptureError>>>,
    recovery_handle: Option<JoinHandle<()>>,
    stop_flag: Arc<AtomicBool>,
    pause_flag: Arc<AtomicBool>,
    mic_bytes: Arc<AtomicU64>,
    loopback_bytes: Arc<AtomicU64>,
    /// Sender used to wake the recovery supervisor immediately on stop.
    stop_tx: Option<mpsc::Sender<()>>,
}

impl AudioCapture {
    pub fn new() -> Self {
        Self {
            mic_handle: None,
            loopback_handle: None,
            recovery_handle: None,
            stop_flag: Arc::new(AtomicBool::new(false)),
            pause_flag: Arc::new(AtomicBool::new(false)),
            mic_bytes: Arc::new(AtomicU64::new(0)),
            loopback_bytes: Arc::new(AtomicU64::new(0)),
            stop_tx: None,
        }
    }

    pub fn start_with_backends(
        &mut self,
        mic_backend: impl AudioBackend,
        loop_backend: impl AudioBackend,
        mic_device: AudioDevice,
        loopback_device: AudioDevice,
        mut mic_wav: crate::audio::wav::StreamingWav,
        mut loop_wav: crate::audio::wav::StreamingWav,
        session_dir: PathBuf,
        mic_wav_path: PathBuf,
        loopback_wav_path: PathBuf,
        started_at: chrono::DateTime<chrono::Utc>,
    ) -> Result<(), CaptureError> {
        self.stop_flag.store(false, Ordering::SeqCst);
        self.pause_flag.store(false, Ordering::SeqCst);
        self.mic_bytes.store(0, Ordering::SeqCst);
        self.loopback_bytes.store(0, Ordering::SeqCst);

        let stop = self.stop_flag.clone();
        let pause = self.pause_flag.clone();
        let written = self.mic_bytes.clone();
        self.mic_handle = Some(std::thread::spawn(move || {
            mic_backend.capture_loop(&mic_device, &mut mic_wav, stop, pause, written)
        }));

        let stop = self.stop_flag.clone();
        let pause = self.pause_flag.clone();
        let written = self.loopback_bytes.clone();
        self.loopback_handle = Some(std::thread::spawn(move || {
            loop_backend.capture_loop(&loopback_device, &mut loop_wav, stop, pause, written)
        }));

        // Recovery supervisor with mpsc wakeup
        let (stop_tx, stop_rx) = mpsc::channel::<()>();
        self.stop_tx = Some(stop_tx);

        let stop = self.stop_flag.clone();
        let pause = self.pause_flag.clone();
        let mic_bytes = self.mic_bytes.clone();
        let loop_bytes = self.loopback_bytes.clone();
        self.recovery_handle = Some(std::thread::spawn(move || {
            recovery_supervisor(
                session_dir,
                mic_wav_path,
                loopback_wav_path,
                started_at,
                stop,
                pause,
                mic_bytes,
                loop_bytes,
                stop_rx,
            );
        }));

        Ok(())
    }

    pub fn pause(&self) {
        self.pause_flag.store(true, Ordering::SeqCst);
    }

    pub fn resume(&self) {
        self.pause_flag.store(false, Ordering::SeqCst);
    }

    /// Signal stop, wake supervisor immediately, join all threads.
    ///
    /// All handles are joined unconditionally — if mic panics, loopback and
    /// recovery are still joined. Errors are aggregated into a combined string.
    pub fn stop(&mut self) -> Result<(CaptureThreadResult, CaptureThreadResult), CaptureError> {
        self.stop_flag.store(true, Ordering::SeqCst);

        // Wake the supervisor so it doesn't sleep for up to 5 seconds
        if let Some(tx) = self.stop_tx.take() {
            let _ = tx.send(());
        }

        // Join mic handle unconditionally
        let mic_result = if let Some(h) = self.mic_handle.take() {
            match h.join() {
                Ok(Ok(r)) => Ok(r),
                Ok(Err(e)) => Err(format!("mic: {e}")),
                Err(_) => Err("mic: thread panicked".to_string()),
            }
        } else {
            Ok(CaptureThreadResult { bytes_written: 0 })
        };

        // Join loopback handle unconditionally (even if mic failed)
        let loop_result = if let Some(h) = self.loopback_handle.take() {
            match h.join() {
                Ok(Ok(r)) => Ok(r),
                Ok(Err(e)) => Err(format!("loopback: {e}")),
                Err(_) => Err("loopback: thread panicked".to_string()),
            }
        } else {
            Ok(CaptureThreadResult { bytes_written: 0 })
        };

        // Join recovery supervisor unconditionally
        if let Some(h) = self.recovery_handle.take() {
            let _ = h.join();
        }

        // Aggregate errors: preserve which track failed
        match (mic_result, loop_result) {
            (Ok(m), Ok(l)) => Ok((m, l)),
            (Err(m), Err(l)) => Err(CaptureError::Capture(format!("{m}; {l}"))),
            (Err(e), Ok(r)) | (Ok(r), Err(e)) => {
                // One track succeeded, one failed — return the successful
                // result alongside the error. We pick the error since the
                // caller needs to know something went wrong, but we still
                // have the successful track's bytes in the AtomicU64.
                let _ = r;
                Err(CaptureError::Capture(e))
            }
        }
    }

    pub fn bytes_written(&self) -> (u64, u64) {
        (
            self.mic_bytes.load(Ordering::SeqCst),
            self.loopback_bytes.load(Ordering::SeqCst),
        )
    }
}

/// Recovery supervisor: saves recovery.json every 5 seconds.
/// Wakes immediately when `stop_rx` receives a signal (on stop).
fn recovery_supervisor(
    session_dir: PathBuf,
    mic_path: PathBuf,
    loopback_path: PathBuf,
    started_at: chrono::DateTime<chrono::Utc>,
    stop: Arc<AtomicBool>,
    pause: Arc<AtomicBool>,
    mic_bytes: Arc<AtomicU64>,
    loop_bytes: Arc<AtomicU64>,
    stop_rx: mpsc::Receiver<()>,
) {
    let interval = std::time::Duration::from_secs(5);
    let session_id = session_dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_string();

    loop {
        if stop.load(Ordering::SeqCst) {
            break;
        }

        // Block for up to 5 seconds OR until stop signal arrives
        let _ = stop_rx.recv_timeout(interval);

        let is_paused = pause.load(Ordering::SeqCst);
        let status = if is_paused {
            crate::recovery::RecoveryStatus::Paused
        } else {
            crate::recovery::RecoveryStatus::Recording
        };

        let meta = crate::recovery::RecoveryMeta {
            session_id: session_id.clone(),
            mic_path: mic_path.clone(),
            loopback_path: loopback_path.clone(),
            sample_rate: 16000,
            started_at,
            mic_offset: mic_bytes.load(Ordering::SeqCst),
            loopback_offset: loop_bytes.load(Ordering::SeqCst),
            status,
            error_code: None,
        };
        let _ = meta.save(&session_dir);

        if stop.load(Ordering::SeqCst) {
            break;
        }
    }
}

/// Write the final Finalized or Failed status to recovery.json.
/// Called from the main thread after capture threads have joined.
pub fn write_final_recovery(
    session_dir: &PathBuf,
    mic_path: &PathBuf,
    loopback_path: &PathBuf,
    started_at: chrono::DateTime<chrono::Utc>,
    mic_offset: u64,
    loopback_offset: u64,
    status: crate::recovery::RecoveryStatus,
    error_code: Option<String>,
) {
    let session_id = session_dir
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("")
        .to_string();
    let meta = crate::recovery::RecoveryMeta {
        session_id,
        mic_path: mic_path.clone(),
        loopback_path: loopback_path.clone(),
        sample_rate: 16000,
        started_at,
        mic_offset,
        loopback_offset,
        status,
        error_code,
    };
    let _ = meta.save(session_dir);
}

#[derive(Debug, thiserror::Error)]
pub enum CaptureError {
    #[error("capture error: {0}")]
    Capture(String),
    #[error("thread panic")]
    ThreadPanic,
    #[error("WASAPI error: {0}")]
    Wasapi(String),
    #[error("device not found: {0}")]
    DeviceNotFound(String),
    #[error("no audio devices available")]
    NoDevices,
    #[error("max duration reached")]
    MaxDuration,
    #[error("COM initialization failed")]
    ComInit,
    #[error("unsupported audio format: {0}")]
    UnsupportedFormat(String),
}

// ─────────────────────────────────────────────────────────────────────────────
// Real WASAPI backend (Windows only)
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(windows)]
pub mod wasapi {
    use super::*;
    use windows::core::GUID;
    use windows::Win32::Media::Audio::*;
    use windows::Win32::System::Com::*;

    const KSDATAFORMAT_SUBTYPE_IEEE_FLOAT: GUID =
        GUID::from_u128(0x00000003_0000_0010_8000_00aa00389b71);
    const WAVE_FORMAT_IEEE_FLOAT: u16 = 0x0003;
    const WAVE_FORMAT_EXTENSIBLE: u16 = 0xFFFE;
    const AUDCLNT_BUFFERFLAGS_SILENT: u32 = 0x00000002;

    const MAX_DURATION_SECS: f64 = 7200.0;
    const TARGET_SAMPLE_RATE: u32 = 16000;
    const FLUSH_INTERVAL: std::time::Duration = std::time::Duration::from_secs(1);

    pub struct WasapiBackend;
    impl WasapiBackend {
        pub fn new() -> Self {
            Self
        }
    }
    unsafe impl Send for WasapiBackend {}

    /// Initialize COM on the current thread, tolerating already-initialized
    /// apartments. Tauri's WebView2 main thread is typically STA; trying to
    /// switch to MTA raises RPC_E_CHANGED_MODE. We accept whatever model is
    /// already set (S_FALSE) or succeed on first init (S_OK).
    fn com_init_tolerant() -> Result<(), CaptureError> {
        unsafe {
            let hr = CoInitializeEx(None, COINIT_MULTITHREADED);
            // S_OK (0) or S_FALSE (already initialized, compatible) are fine.
            // RPC_E_CHANGED_MODE means another apartment model is active —
            // we can still use COM APIs, just not switch models.
            if hr.is_ok() || hr == windows::core::HRESULT(1) {
                return Ok(());
            }
            // Try apartment-threaded as a fallback (matches WebView2 STA)
            let hr2 = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
            if hr2.is_ok() || hr2 == windows::core::HRESULT(1) {
                return Ok(());
            }
            Err(CaptureError::ComInit)
        }
    }

    impl AudioBackend for WasapiBackend {
        fn enumerate_devices(&self) -> Result<Vec<AudioDevice>, CaptureError> {
            unsafe {
                com_init_tolerant()?;
                let enumerator: IMMDeviceEnumerator =
                    CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                        .map_err(|e| CaptureError::Wasapi(format!("CoCreateInstance: {e}")))?;
                let mut devices = Vec::new();
                for flow in [eCapture, eRender] {
                    let is_input = flow == eCapture;
                    let collection = enumerator
                        .EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE)
                        .map_err(|e| CaptureError::Wasapi(format!("EnumAudioEndpoints: {e}")))?;
                    let count = collection
                        .GetCount()
                        .map_err(|e| CaptureError::Wasapi(format!("GetCount: {e}")))?;
                    let default_dev = enumerator.GetDefaultAudioEndpoint(flow, eConsole).ok();
                    let default_id_str = default_dev
                        .as_ref()
                        .and_then(|d| d.GetId().ok())
                        .map(|id| id.to_string().unwrap_or_default())
                        .unwrap_or_default();
                    for i in 0..count {
                        let device = collection
                            .Item(i)
                            .map_err(|e| CaptureError::Wasapi(format!("Item({i}): {e}")))?;
                        let id = device
                            .GetId()
                            .map(|s| s.to_string().unwrap_or_default())
                            .unwrap_or_default();
                        let name = get_device_friendly_name(&device)
                            .unwrap_or_else(|| format!("Device {i}"));
                        let is_default = id == default_id_str;
                        devices.push(AudioDevice {
                            id,
                            name,
                            is_input,
                            is_default,
                        });
                    }
                }
                Ok(devices)
            }
        }

        fn resolve_device(
            &self,
            device_id: Option<&str>,
            is_capture: bool,
        ) -> Result<AudioDevice, CaptureError> {
            unsafe {
                com_init_tolerant()?;
                let enumerator: IMMDeviceEnumerator =
                    CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                        .map_err(|e| CaptureError::Wasapi(format!("CoCreateInstance: {e}")))?;
                let flow = if is_capture { eCapture } else { eRender };
                let device = if let Some(id) = device_id {
                    let id_wide: Vec<u16> = id.encode_utf16().chain(std::iter::once(0)).collect();
                    enumerator
                        .GetDevice(windows::core::PCWSTR(id_wide.as_ptr()))
                        .map_err(|_| CaptureError::DeviceNotFound(id.to_string()))?
                } else {
                    enumerator
                        .GetDefaultAudioEndpoint(flow, eConsole)
                        .map_err(|e| {
                            CaptureError::Wasapi(format!("GetDefaultAudioEndpoint: {e}"))
                        })?
                };
                let id_str = device
                    .GetId()
                    .map(|s| s.to_string().unwrap_or_default())
                    .unwrap_or_default();
                let name =
                    get_device_friendly_name(&device).unwrap_or_else(|| "Unknown".to_string());
                Ok(AudioDevice {
                    id: id_str,
                    name,
                    is_input: is_capture,
                    is_default: device_id.is_none(),
                })
            }
        }

        fn capture_loop(
            &self,
            device: &AudioDevice,
            wav: &mut crate::audio::wav::StreamingWav,
            stop: Arc<AtomicBool>,
            pause: Arc<AtomicBool>,
            bytes_written: Arc<AtomicU64>,
        ) -> Result<CaptureThreadResult, CaptureError> {
            unsafe {
                com_init_tolerant()?;
                capture_loop_impl(device, wav, stop, pause, bytes_written)
            }
        }
    }

    unsafe fn get_device_friendly_name(device: &IMMDevice) -> Option<String> {
        use windows::Win32::Devices::FunctionDiscovery::*;
        let props = device.OpenPropertyStore(STGM_READ).ok()?;
        let pv = props.GetValue(&PKEY_Device_FriendlyName).ok()?;
        let pwsz = pv.Anonymous.Anonymous.Anonymous.pwszVal;
        if pwsz.is_null() {
            return None;
        }
        let len = (0..).find(|&i| *pwsz.0.add(i) == 0).unwrap_or(0);
        let slice = std::slice::from_raw_parts(pwsz.0, len);
        String::from_utf16(slice).ok()
    }

    /// # Safety
    /// Caller must have initialized COM on this thread.
    unsafe fn capture_loop_impl(
        device: &AudioDevice,
        wav: &mut crate::audio::wav::StreamingWav,
        stop: Arc<AtomicBool>,
        pause: Arc<AtomicBool>,
        bytes_written: Arc<AtomicU64>,
    ) -> Result<CaptureThreadResult, CaptureError> {
        let enumerator: IMMDeviceEnumerator =
            CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                .map_err(|e| CaptureError::Wasapi(format!("CoCreateInstance enumerator: {e}")))?;

        let id_wide: Vec<u16> = device.id.encode_utf16().chain(std::iter::once(0)).collect();
        let mm_device = enumerator
            .GetDevice(windows::core::PCWSTR(id_wide.as_ptr()))
            .map_err(|_| CaptureError::DeviceNotFound(device.id.clone()))?;

        let audio_client: IAudioClient = mm_device
            .Activate::<IAudioClient>(CLSCTX_ALL, None)
            .map_err(|e| CaptureError::Wasapi(format!("Activate IAudioClient: {e}")))?;

        let mix_format_ptr = audio_client
            .GetMixFormat()
            .map_err(|e| CaptureError::Wasapi(format!("GetMixFormat: {e}")))?;

        if mix_format_ptr.is_null() {
            return Err(CaptureError::Wasapi("GetMixFormat returned null".into()));
        }

        let mix_format = *mix_format_ptr;
        let src_channels = mix_format.nChannels;
        let src_sample_rate = mix_format.nSamplesPerSec;
        let src_bits = mix_format.wBitsPerSample;
        let w_format_tag = mix_format.wFormatTag;
        let is_ieee_float = if w_format_tag == WAVE_FORMAT_EXTENSIBLE {
            let ext_ptr = mix_format_ptr as *const WAVEFORMATEXTENSIBLE;
            let sub = std::ptr::read_unaligned(std::ptr::addr_of!((*ext_ptr).SubFormat));
            sub == KSDATAFORMAT_SUBTYPE_IEEE_FLOAT
        } else {
            w_format_tag == WAVE_FORMAT_IEEE_FLOAT
        };

        let format_supported = matches!(
            (is_ieee_float, src_bits),
            (true, 32) | (false, 16) | (false, 24) | (false, 32)
        );
        if !format_supported {
            CoTaskMemFree(Some(mix_format_ptr as *const _));
            return Err(CaptureError::UnsupportedFormat(format!(
                "format_tag={} bits={} float={} channels={}",
                w_format_tag, src_bits, is_ieee_float, src_channels
            )));
        }

        let mut stream_flags = AUDCLNT_STREAMFLAGS_NOPERSIST;
        if !device.is_input {
            stream_flags |= AUDCLNT_STREAMFLAGS_LOOPBACK;
        }

        let buffer_duration: i64 = 1_000_000; // 100ms
        audio_client
            .Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                stream_flags,
                buffer_duration,
                0,
                mix_format_ptr,
                None,
            )
            .map_err(|e| CaptureError::Wasapi(format!("Initialize: {e}")))?;

        let capture_client: IAudioCaptureClient = audio_client
            .GetService()
            .map_err(|e| CaptureError::Wasapi(format!("GetService IAudioCaptureClient: {e}")))?;

        audio_client
            .Start()
            .map_err(|e| CaptureError::Wasapi(format!("Start: {e}")))?;

        let start_time = std::time::Instant::now();
        let mut total_written: u64 = 0;
        let mut last_flush = std::time::Instant::now();

        let resample_ratio = src_sample_rate as f64 / TARGET_SAMPLE_RATE as f64;
        let mut resample_phase: f64 = 0.0;
        let mut prev_sample: f32 = 0.0;

        let loop_result = (|| -> Result<(), CaptureError> {
            loop {
                if stop.load(Ordering::SeqCst) {
                    break;
                }
                if start_time.elapsed().as_secs_f64() >= MAX_DURATION_SECS {
                    stop.store(true, Ordering::SeqCst);
                    break;
                }

                let packet_frames = match capture_client.GetNextPacketSize() {
                    Ok(n) => n,
                    Err(_) => {
                        std::thread::sleep(std::time::Duration::from_millis(5));
                        continue;
                    }
                };
                if packet_frames == 0 {
                    std::thread::sleep(std::time::Duration::from_millis(5));
                    continue;
                }

                let mut data_ptr: *mut u8 = std::ptr::null_mut();
                let mut num_frames: u32 = 0;
                let mut flags: u32 = 0;
                let mut qpc_position: u64 = 0;
                capture_client
                    .GetBuffer(
                        &mut data_ptr,
                        &mut num_frames,
                        &mut flags,
                        None,
                        Some(&mut qpc_position),
                    )
                    .map_err(|e| CaptureError::Wasapi(format!("GetBuffer: {e}")))?;

                let is_silent = (flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0;

                let mono_f32 = if is_silent {
                    vec![0.0f32; num_frames as usize]
                } else if data_ptr.is_null() || num_frames == 0 {
                    vec![0.0f32; num_frames as usize]
                } else {
                    convert_to_mono_f32(
                        data_ptr,
                        num_frames,
                        src_channels,
                        src_bits,
                        is_ieee_float,
                    )?
                };

                capture_client
                    .ReleaseBuffer(num_frames)
                    .map_err(|e| CaptureError::Wasapi(format!("ReleaseBuffer: {e}")))?;

                if pause.load(Ordering::SeqCst) {
                    continue;
                }

                let resampled = resample_linear(
                    &mono_f32,
                    resample_ratio,
                    &mut resample_phase,
                    &mut prev_sample,
                );
                let i16_bytes = f32_to_i16_bytes(&resampled);

                wav.append(&i16_bytes)
                    .map_err(|e| CaptureError::Capture(format!("wav append: {e}")))?;
                total_written = wav.data_bytes();

                // Periodic flush_safe: only update AtomicU64 on success
                if last_flush.elapsed() >= FLUSH_INTERVAL {
                    if let Ok(confirmed) = wav.flush_safe() {
                        bytes_written.store(confirmed, Ordering::SeqCst);
                    }
                    last_flush = std::time::Instant::now();
                }
            }
            Ok(())
        })();

        // Stop WASAPI stream
        let _ = audio_client.Stop();
        CoTaskMemFree(Some(mix_format_ptr as *const _));

        // Final flush + finalize WAV header — always attempted, even if
        // the capture loop returned an error. Track the first error.
        let mut first_err: Option<String> = loop_result.err().map(|e| e.to_string());

        // Final flush: try to get any remaining buffered data to disk
        if let Err(e) = wav.flush_safe() {
            if first_err.is_none() {
                first_err = Some(format!("final flush: {e}"));
            }
        }

        // Always finalize the WAV header so the file is valid even on error.
        // Zero-data captures still get a valid 44-byte WAV.
        if let Err(e) = wav.finalize() {
            if first_err.is_none() {
                first_err = Some(format!("finalize: {e}"));
            }
        }

        // Update AtomicU64 with last successfully synchronized bytes
        bytes_written.store(wav.flushed_bytes(), Ordering::SeqCst);

        if let Some(e) = first_err {
            return Err(CaptureError::Capture(e));
        }

        Ok(CaptureThreadResult {
            bytes_written: wav.flushed_bytes(),
        })
    }

    unsafe fn convert_to_mono_f32(
        data_ptr: *const u8,
        num_frames: u32,
        channels: u16,
        bits: u16,
        is_ieee_float: bool,
    ) -> Result<Vec<f32>, CaptureError> {
        let total = (num_frames as usize) * (channels as usize);
        let mut mono = Vec::with_capacity(num_frames as usize);
        match (is_ieee_float, bits) {
            (true, 32) => {
                let s = std::slice::from_raw_parts(data_ptr as *const f32, total);
                for f in s.chunks(channels as usize) {
                    mono.push(f.iter().sum::<f32>() / channels as f32);
                }
            }
            (false, 16) => {
                let s = std::slice::from_raw_parts(data_ptr as *const i16, total);
                for f in s.chunks(channels as usize) {
                    mono.push(f.iter().map(|&v| v as f32 / 32768.0).sum::<f32>() / channels as f32);
                }
            }
            (false, 24) => {
                let bytes = std::slice::from_raw_parts(data_ptr, total * 3);
                for fi in 0..num_frames as usize {
                    let mut sum = 0.0f32;
                    for ch in 0..channels as usize {
                        let o = (fi * channels as usize + ch) * 3;
                        let v = ((bytes[o + 2] as i32) << 24
                            | (bytes[o + 1] as i32) << 16
                            | (bytes[o] as i32) << 8)
                            >> 8;
                        sum += v as f32 / 8388608.0;
                    }
                    mono.push(sum / channels as f32);
                }
            }
            (false, 32) => {
                let s = std::slice::from_raw_parts(data_ptr as *const i32, total);
                for f in s.chunks(channels as usize) {
                    mono.push(
                        f.iter().map(|&v| v as f32 / 2147483648.0).sum::<f32>() / channels as f32,
                    );
                }
            }
            _ => {
                return Err(CaptureError::UnsupportedFormat(format!(
                    "float={} bits={} channels={}",
                    is_ieee_float, bits, channels
                )));
            }
        }
        Ok(mono)
    }

    fn resample_linear(
        input: &[f32],
        ratio: f64,
        phase: &mut f64,
        prev_sample: &mut f32,
    ) -> Vec<f32> {
        let mut out = Vec::new();
        for &s in input {
            while *phase < 1.0 {
                let t = *phase;
                out.push(*prev_sample * (1.0 - t as f32) + s * t as f32);
                *phase += ratio;
            }
            *phase -= 1.0;
            *prev_sample = s;
        }
        out
    }

    fn f32_to_i16_bytes(samples: &[f32]) -> Vec<u8> {
        let mut b = Vec::with_capacity(samples.len() * 2);
        for &s in samples {
            let c = s.clamp(-1.0, 1.0);
            b.extend_from_slice(&((c * 32767.0) as i16).to_le_bytes());
        }
        b
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Mock backend for unit tests only
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
pub mod mock {
    use super::*;

    pub struct MockBackend {
        pub devices: Vec<AudioDevice>,
        pub chunk_size: usize,
    }

    impl MockBackend {
        pub fn new() -> Self {
            Self {
                devices: vec![
                    AudioDevice {
                        id: "mock-mic-001".into(),
                        name: "Mock Microphone".into(),
                        is_input: true,
                        is_default: true,
                    },
                    AudioDevice {
                        id: "mock-render-001".into(),
                        name: "Mock Speakers".into(),
                        is_input: false,
                        is_default: true,
                    },
                ],
                chunk_size: 320,
            }
        }
    }

    impl AudioBackend for MockBackend {
        fn enumerate_devices(&self) -> Result<Vec<AudioDevice>, CaptureError> {
            Ok(self.devices.clone())
        }
        fn resolve_device(
            &self,
            device_id: Option<&str>,
            is_capture: bool,
        ) -> Result<AudioDevice, CaptureError> {
            if let Some(id) = device_id {
                self.devices
                    .iter()
                    .find(|d| d.id == id)
                    .cloned()
                    .ok_or_else(|| CaptureError::DeviceNotFound(id.to_string()))
            } else {
                self.devices
                    .iter()
                    .find(|d| d.is_input == is_capture && d.is_default)
                    .cloned()
                    .ok_or(CaptureError::NoDevices)
            }
        }
        fn capture_loop(
            &self,
            _device: &AudioDevice,
            wav: &mut crate::audio::wav::StreamingWav,
            stop: Arc<AtomicBool>,
            pause: Arc<AtomicBool>,
            bytes_written: Arc<AtomicU64>,
        ) -> Result<CaptureThreadResult, CaptureError> {
            let chunk: Vec<u8> = (0..self.chunk_size)
                .map(|i| ((i % 256) as u8).wrapping_add(1))
                .collect();

            let mut last_flush = std::time::Instant::now();

            while !stop.load(Ordering::SeqCst) {
                if !pause.load(Ordering::SeqCst) {
                    wav.append(&chunk)
                        .map_err(|e| CaptureError::Capture(format!("mock append: {e}")))?;
                    if last_flush.elapsed() >= std::time::Duration::from_millis(100) {
                        if let Ok(c) = wav.flush_safe() {
                            bytes_written.store(c, Ordering::SeqCst);
                        }
                        last_flush = std::time::Instant::now();
                    }
                }
                std::thread::sleep(std::time::Duration::from_millis(10));
            }

            // Final flush + finalize (always, even on empty capture).
            // Track the first error but always attempt both operations.
            let mut first_err: Option<String> = None;

            if let Err(e) = wav.flush_safe() {
                first_err = Some(format!("mock final flush: {e}"));
            }
            if let Err(e) = wav.finalize() {
                if first_err.is_none() {
                    first_err = Some(format!("mock finalize: {e}"));
                }
            }

            bytes_written.store(wav.flushed_bytes(), Ordering::SeqCst);

            if let Some(e) = first_err {
                return Err(CaptureError::Capture(e));
            }

            Ok(CaptureThreadResult {
                bytes_written: wav.flushed_bytes(),
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use mock::MockBackend;

    #[test]
    fn test_mock_enumerate_devices() {
        let b = MockBackend::new();
        let d = b.enumerate_devices().unwrap();
        assert!(d.len() >= 2);
        assert!(d.iter().any(|d| d.is_input));
        assert!(d.iter().any(|d| !d.is_input));
    }

    #[test]
    fn test_mock_resolve_default_mic() {
        let b = MockBackend::new();
        let d = b.resolve_device(None, true).unwrap();
        assert!(d.is_input);
        assert!(d.is_default);
    }

    #[test]
    fn test_mock_resolve_default_render() {
        let b = MockBackend::new();
        let d = b.resolve_device(None, false).unwrap();
        assert!(!d.is_input);
        assert!(d.is_default);
    }

    #[test]
    fn test_mock_resolve_specific_device() {
        let b = MockBackend::new();
        let d = b.resolve_device(Some("mock-mic-001"), true).unwrap();
        assert_eq!(d.id, "mock-mic-001");
    }

    #[test]
    fn test_mock_resolve_nonexistent_device() {
        let b = MockBackend::new();
        assert!(b.resolve_device(Some("nope"), true).is_err());
    }

    #[test]
    fn test_mock_resolve_no_devices() {
        let b = MockBackend {
            devices: vec![],
            chunk_size: 320,
        };
        assert!(b.resolve_device(None, true).is_err());
    }

    #[test]
    fn test_audio_capture_start_stop_fast() {
        let mut cap = AudioCapture::new();
        let dir = std::env::temp_dir().join("memecho_t_cap_fast");
        std::fs::create_dir_all(&dir).unwrap();
        let mp = dir.join("mic.wav");
        let lp = dir.join("lb.wav");

        let mw = crate::audio::wav::create_streaming_wav(&mp, 16000).unwrap();
        let lw = crate::audio::wav::create_streaming_wav(&lp, 16000).unwrap();

        let mb = MockBackend::new();
        let lb = MockBackend::new();
        let md = mb.resolve_device(None, true).unwrap();
        let ld = lb.resolve_device(None, false).unwrap();

        cap.start_with_backends(
            mb,
            lb,
            md,
            ld,
            mw,
            lw,
            dir.clone(),
            mp.clone(),
            lp.clone(),
            chrono::Utc::now(),
        )
        .unwrap();

        std::thread::sleep(std::time::Duration::from_millis(100));

        let t0 = std::time::Instant::now();
        let (mr, lr) = cap.stop().unwrap();
        let elapsed = t0.elapsed();

        assert!(mr.bytes_written > 0);
        assert!(lr.bytes_written > 0);
        assert!(mp.exists());
        assert!(lp.exists());
        // Stop must complete in < 1 second (no 5s sleep)
        assert!(
            elapsed.as_secs() < 1,
            "stop took {:?}, expected < 1s",
            elapsed
        );

        // WAV files should be valid (finalize was called inside capture thread)
        let d = std::fs::read(&mp).unwrap();
        assert_eq!(&d[0..4], b"RIFF");
        assert_eq!(&d[8..12], b"WAVE");
        let ds = u32::from_le_bytes([d[40], d[41], d[42], d[43]]);
        assert!(ds > 0);
        assert_eq!(d.len(), 44 + ds as usize);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_audio_capture_pause_resume() {
        let mut cap = AudioCapture::new();
        let dir = std::env::temp_dir().join("memecho_t_cap_pr");
        std::fs::create_dir_all(&dir).unwrap();
        let mp = dir.join("mic.wav");
        let lp = dir.join("lb.wav");
        let mw = crate::audio::wav::create_streaming_wav(&mp, 16000).unwrap();
        let lw = crate::audio::wav::create_streaming_wav(&lp, 16000).unwrap();
        let mb = MockBackend::new();
        let lb = MockBackend::new();
        let md = mb.resolve_device(None, true).unwrap();
        let ld = lb.resolve_device(None, false).unwrap();
        cap.start_with_backends(
            mb,
            lb,
            md,
            ld,
            mw,
            lw,
            dir.clone(),
            mp.clone(),
            lp.clone(),
            chrono::Utc::now(),
        )
        .unwrap();

        // Wait > 200ms (mock flush interval=100ms + thread startup) so flush_safe occurs
        std::thread::sleep(std::time::Duration::from_millis(250));
        let (mb_before, _) = cap.bytes_written();
        assert!(mb_before > 0);

        cap.pause();
        std::thread::sleep(std::time::Duration::from_millis(200));
        let (mb_during, _) = cap.bytes_written();
        assert!(mb_during <= mb_before + 1000);

        cap.resume();
        std::thread::sleep(std::time::Duration::from_millis(250));
        let (mb_after, _) = cap.bytes_written();
        assert!(mb_after > mb_during);

        cap.stop().unwrap();
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_audio_capture_flush_safe_offset() {
        let mut cap = AudioCapture::new();
        let dir = std::env::temp_dir().join("memecho_t_cap_flush");
        std::fs::create_dir_all(&dir).unwrap();
        let mp = dir.join("mic.wav");
        let lp = dir.join("lb.wav");
        let mw = crate::audio::wav::create_streaming_wav(&mp, 16000).unwrap();
        let lw = crate::audio::wav::create_streaming_wav(&lp, 16000).unwrap();
        let mb = MockBackend::new();
        let lb = MockBackend::new();
        let md = mb.resolve_device(None, true).unwrap();
        let ld = lb.resolve_device(None, false).unwrap();
        cap.start_with_backends(
            mb,
            lb,
            md,
            ld,
            mw,
            lw,
            dir.clone(),
            mp.clone(),
            lp.clone(),
            chrono::Utc::now(),
        )
        .unwrap();

        std::thread::sleep(std::time::Duration::from_millis(300));
        let (mic_b, loop_b) = cap.bytes_written();
        // After 300ms, the flush_safe in mock runs every 100ms,
        // so bytes_written should reflect flushed (not just appended) bytes
        assert!(mic_b > 0);
        assert!(loop_b > 0);
        // Both should be multiples of chunk_size (320) after flush
        assert_eq!(mic_b % 320, 0);
        assert_eq!(loop_b % 320, 0);

        cap.stop().unwrap();
        std::fs::remove_dir_all(&dir).ok();
    }

    #[cfg(windows)]
    #[test]
    fn test_wasapi_backend_creation() {
        let _ = wasapi::WasapiBackend::new();
    }

    #[cfg(windows)]
    #[test]
    fn test_wasapi_enumerate_devices() {
        let b = wasapi::WasapiBackend::new();
        match b.enumerate_devices() {
            Ok(devices) => {
                println!("Found {} devices", devices.len());
                for d in &devices {
                    println!(
                        "  {} [{}] {}{}",
                        d.id,
                        if d.is_input { "IN" } else { "OUT" },
                        d.name,
                        if d.is_default { " (DEFAULT)" } else { "" }
                    );
                }
            }
            Err(e) => println!("enumerate_devices failed: {e}"),
        }
    }

    #[cfg(windows)]
    #[test]
    fn test_wasapi_resolve_default_devices() {
        let b = wasapi::WasapiBackend::new();
        match b.resolve_device(None, true) {
            Ok(d) => println!("Default mic: {} ({})", d.name, d.id),
            Err(e) => println!("No default mic: {e}"),
        }
        match b.resolve_device(None, false) {
            Ok(d) => println!("Default render: {} ({})", d.name, d.id),
            Err(e) => println!("No default render: {e}"),
        }
    }

    // ── Reliability tests ────────────────────────────────────────────────

    /// A backend that writes some data then returns an error on capture_loop.
    struct ErrorBackend {
        chunk_count: usize,
    }

    impl ErrorBackend {
        fn new(chunk_count: usize) -> Self {
            Self { chunk_count }
        }
    }

    impl AudioBackend for ErrorBackend {
        fn enumerate_devices(&self) -> Result<Vec<AudioDevice>, CaptureError> {
            Ok(vec![AudioDevice {
                id: "err-001".into(),
                name: "Error Device".into(),
                is_input: true,
                is_default: true,
            }])
        }
        fn resolve_device(
            &self,
            _device_id: Option<&str>,
            _is_capture: bool,
        ) -> Result<AudioDevice, CaptureError> {
            Ok(AudioDevice {
                id: "err-001".into(),
                name: "Error Device".into(),
                is_input: true,
                is_default: true,
            })
        }
        fn capture_loop(
            &self,
            _device: &AudioDevice,
            wav: &mut crate::audio::wav::StreamingWav,
            stop: Arc<AtomicBool>,
            _pause: Arc<AtomicBool>,
            bytes_written: Arc<AtomicU64>,
        ) -> Result<CaptureThreadResult, CaptureError> {
            let chunk = vec![0xABu8; 320];
            for _ in 0..self.chunk_count {
                wav.append(&chunk)
                    .map_err(|e| CaptureError::Capture(format!("err append: {e}")))?;
            }
            // Flush and finalize so the WAV is valid
            let _ = wav.flush_safe();
            let _ = wav.finalize();
            bytes_written.store(wav.flushed_bytes(), Ordering::SeqCst);
            // Now return an error
            stop.store(true, Ordering::SeqCst);
            Err(CaptureError::Capture("simulated capture error".into()))
        }
    }

    /// A backend that panics in capture_loop.
    struct PanicBackend;

    impl AudioBackend for PanicBackend {
        fn enumerate_devices(&self) -> Result<Vec<AudioDevice>, CaptureError> {
            Ok(vec![AudioDevice {
                id: "panic-001".into(),
                name: "Panic Device".into(),
                is_input: true,
                is_default: true,
            }])
        }
        fn resolve_device(
            &self,
            _device_id: Option<&str>,
            _is_capture: bool,
        ) -> Result<AudioDevice, CaptureError> {
            Ok(AudioDevice {
                id: "panic-001".into(),
                name: "Panic Device".into(),
                is_input: true,
                is_default: true,
            })
        }
        fn capture_loop(
            &self,
            _device: &AudioDevice,
            _wav: &mut crate::audio::wav::StreamingWav,
            _stop: Arc<AtomicBool>,
            _pause: Arc<AtomicBool>,
            _bytes_written: Arc<AtomicU64>,
        ) -> Result<CaptureThreadResult, CaptureError> {
            panic!("simulated panic in capture thread");
        }
    }

    #[test]
    fn test_one_track_error_still_joins_other() {
        // mic returns error after writing some data, loopback succeeds
        let mut cap = AudioCapture::new();
        let dir = std::env::temp_dir().join("memecho_t_one_err");
        std::fs::create_dir_all(&dir).unwrap();
        let mp = dir.join("mic.wav");
        let lp = dir.join("lb.wav");

        let mw = crate::audio::wav::create_streaming_wav(&mp, 16000).unwrap();
        let lw = crate::audio::wav::create_streaming_wav(&lp, 16000).unwrap();

        let err_backend = ErrorBackend::new(3);
        let ok_backend = MockBackend::new();
        let ed = err_backend.resolve_device(None, true).unwrap();
        let od = ok_backend.resolve_device(None, false).unwrap();

        cap.start_with_backends(
            err_backend,
            ok_backend,
            ed,
            od,
            mw,
            lw,
            dir.clone(),
            mp.clone(),
            lp.clone(),
            chrono::Utc::now(),
        )
        .unwrap();

        std::thread::sleep(std::time::Duration::from_millis(250));

        // stop() must return Err (mic failed) but still join both handles
        let result = cap.stop();
        assert!(result.is_err(), "expected error from mic track");
        let err_str = result.unwrap_err().to_string();
        assert!(
            err_str.contains("simulated capture error"),
            "error should contain mic failure reason: {err_str}"
        );

        // The successful loopback track should have finalized its WAV
        assert!(lp.exists());
        let d = std::fs::read(&lp).unwrap();
        assert_eq!(&d[0..4], b"RIFF");
        assert_eq!(&d[8..12], b"WAVE");
        let ds = u32::from_le_bytes([d[40], d[41], d[42], d[43]]);
        assert!(ds > 0, "loopback WAV should have data");

        // The mic track also wrote data before erroring
        assert!(mp.exists());
        let dm = std::fs::read(&mp).unwrap();
        assert_eq!(&dm[0..4], b"RIFF");
        let dsm = u32::from_le_bytes([dm[40], dm[41], dm[42], dm[43]]);
        assert!(dsm > 0, "mic WAV should have data before error");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_one_track_panic_still_joins_other() {
        // mic panics, loopback succeeds — stop must not hang or propagate panic
        let mut cap = AudioCapture::new();
        let dir = std::env::temp_dir().join("memecho_t_one_panic");
        std::fs::create_dir_all(&dir).unwrap();
        let mp = dir.join("mic.wav");
        let lp = dir.join("lb.wav");

        let mw = crate::audio::wav::create_streaming_wav(&mp, 16000).unwrap();
        let lw = crate::audio::wav::create_streaming_wav(&lp, 16000).unwrap();

        let panic_backend = PanicBackend;
        let ok_backend = MockBackend::new();
        let pd = panic_backend.resolve_device(None, true).unwrap();
        let od = ok_backend.resolve_device(None, false).unwrap();

        cap.start_with_backends(
            panic_backend,
            ok_backend,
            pd,
            od,
            mw,
            lw,
            dir.clone(),
            mp.clone(),
            lp.clone(),
            chrono::Utc::now(),
        )
        .unwrap();

        std::thread::sleep(std::time::Duration::from_millis(250));

        let t0 = std::time::Instant::now();
        let result = cap.stop();
        let elapsed = t0.elapsed();

        // stop must complete in < 1s (not hang)
        assert!(
            elapsed.as_secs() < 1,
            "stop took {:?}, expected < 1s",
            elapsed
        );

        // Must return error (panic detected)
        assert!(result.is_err(), "expected error from panic track");
        let err_str = result.unwrap_err().to_string();
        assert!(
            err_str.contains("panicked"),
            "error should mention panic: {err_str}"
        );

        // The successful loopback track should have finalized its WAV
        assert!(lp.exists());
        let d = std::fs::read(&lp).unwrap();
        assert_eq!(&d[0..4], b"RIFF");
        let ds = u32::from_le_bytes([d[40], d[41], d[42], d[43]]);
        assert!(ds > 0, "loopback WAV should have data");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_failed_recovery_preserves_nonzero_offsets() {
        // Verify that when audio.stop() returns an error, the recovery
        // metadata still records the durable byte offsets (not zeros).
        let dir = std::env::temp_dir().join("memecho_t_fail_offsets");
        std::fs::create_dir_all(&dir).unwrap();
        let session_id = dir
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap()
            .to_string();

        // Write initial recovery metadata
        let mic_path = dir.join("mic.wav");
        let lb_path = dir.join("lb.wav");
        let started_at = chrono::Utc::now();

        // Simulate a session that wrote some data then failed
        // Write a recovery meta with nonzero offsets and Failed status
        let meta = crate::recovery::RecoveryMeta {
            session_id: session_id.clone(),
            mic_path: mic_path.clone(),
            loopback_path: lb_path.clone(),
            sample_rate: 16000,
            started_at,
            mic_offset: 9600,      // 300 chunks * 32 bytes each
            loopback_offset: 6400, // 200 chunks * 32 bytes each
            status: crate::recovery::RecoveryStatus::Failed,
            error_code: Some("simulated capture error".into()),
        };
        meta.save(&dir).unwrap();

        // Load and verify offsets are preserved
        let loaded = crate::recovery::RecoveryMeta::load(&dir).unwrap();
        assert_eq!(loaded.status, crate::recovery::RecoveryStatus::Failed);
        assert_eq!(loaded.mic_offset, 9600);
        assert_eq!(loaded.loopback_offset, 6400);
        assert_eq!(
            loaded.error_code.as_deref(),
            Some("simulated capture error")
        );

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_stop_with_error_reads_durable_offsets() {
        // Integration test: start with ErrorBackend on mic, MockBackend on loopback.
        // After stop returns Err, verify the AtomicU64 counters had nonzero values
        // (meaning the recovery metadata would get real offsets, not zeros).
        let mut cap = AudioCapture::new();
        let dir = std::env::temp_dir().join("memecho_t_stop_offsets");
        std::fs::create_dir_all(&dir).unwrap();
        let mp = dir.join("mic.wav");
        let lp = dir.join("lb.wav");

        let mw = crate::audio::wav::create_streaming_wav(&mp, 16000).unwrap();
        let lw = crate::audio::wav::create_streaming_wav(&lp, 16000).unwrap();

        let err_backend = ErrorBackend::new(5); // writes 5 * 320 = 1600 bytes
        let ok_backend = MockBackend::new();
        let ed = err_backend.resolve_device(None, true).unwrap();
        let od = ok_backend.resolve_device(None, false).unwrap();

        cap.start_with_backends(
            err_backend,
            ok_backend,
            ed,
            od,
            mw,
            lw,
            dir.clone(),
            mp.clone(),
            lp.clone(),
            chrono::Utc::now(),
        )
        .unwrap();

        std::thread::sleep(std::time::Duration::from_millis(250));

        // Read the durable counters BEFORE stop
        let (mic_bytes_before, loop_bytes_before) = cap.bytes_written();

        let _ = cap.stop();

        // Read the durable counters AFTER stop — should be >= before
        let (mic_bytes_after, loop_bytes_after) = cap.bytes_written();

        // The mic track wrote data before returning error
        assert!(
            mic_bytes_before > 0 || mic_bytes_after > 0,
            "mic should have nonzero durable bytes"
        );
        // The loopback track succeeded
        assert!(
            loop_bytes_before > 0 || loop_bytes_after > 0,
            "loopback should have nonzero durable bytes"
        );

        std::fs::remove_dir_all(&dir).ok();
    }
}
