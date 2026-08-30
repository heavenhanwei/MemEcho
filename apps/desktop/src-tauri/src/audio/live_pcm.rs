use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;

use parking_lot::Mutex;

/// Target sample rate for the live PCM stream (matches Gateway expectation).
const TARGET_SAMPLE_RATE: u32 = 16_000;

/// 16-bit signed little-endian PCM encoded from f32 samples.
fn f32_to_i16_bytes(samples: &[f32]) -> Vec<u8> {
    let mut b = Vec::with_capacity(samples.len() * 2);
    for &s in samples {
        let c = s.clamp(-1.0, 1.0);
        b.extend_from_slice(&((c * 32767.0) as i16).to_le_bytes());
    }
    b
}

/// Average two PCM16 LE byte streams sample-by-sample into a new stream.
/// Only the overlapping prefix is mixed; a silent or missing track passes
/// the other track through unchanged.
fn mix_pcm16(a: &[u8], b: &[u8]) -> Vec<u8> {
    if a.is_empty() {
        return b.to_vec();
    }
    if b.is_empty() {
        return a.to_vec();
    }
    let len = a.len().min(b.len());
    let mut out = Vec::with_capacity(len);
    for i in (0..len).step_by(2) {
        let x = i16::from_le_bytes([a[i], a[i + 1]]) as f32;
        let y = i16::from_le_bytes([b[i], b[i + 1]]) as f32;
        let avg = ((x + y) / 2.0).clamp(-32768.0, 32767.0) as i16;
        out.extend_from_slice(&avg.to_le_bytes());
    }
    out
}

/// Linear resampler from native sample rate to TARGET_SAMPLE_RATE.
struct Resampler {
    ratio: f64,
    phase: f64,
    prev_sample: f32,
}

impl Resampler {
    fn new(src_rate: u32) -> Self {
        Self {
            ratio: src_rate as f64 / TARGET_SAMPLE_RATE as f64,
            phase: 1.0,
            prev_sample: 0.0,
        }
    }

    fn process(&mut self, input: &[f32]) -> Vec<f32> {
        let mut out = Vec::new();
        for &s in input {
            while self.phase < 1.0 {
                let t = self.phase;
                out.push(self.prev_sample * (1.0 - t as f32) + s * t as f32);
                self.phase += self.ratio;
            }
            self.phase -= 1.0;
            self.prev_sample = s;
        }
        out
    }
}

/// A thread-safe buffer that the WASAPI capture thread pushes PCM into
/// and the Tauri command thread polls from.
type SharedBuffer = Arc<Mutex<Vec<u8>>>;
type SharedError = Arc<Mutex<Option<String>>>;

/// Append only when capture is still unpaused while holding the buffer lock.
/// Pairing this with `LiveStream::set_paused` closes the race where a producer
/// observed `false`, was descheduled, and appended after pause had returned.
fn append_if_running(buffer: &SharedBuffer, pause: &AtomicBool, bytes: &[u8]) {
    let mut output = buffer.lock();
    if !pause.load(Ordering::SeqCst) {
        output.extend_from_slice(bytes);
    }
}

/// Running live stream handle. Dropping this signals the threads to stop
/// but does NOT join them — call `stop()` for a clean shutdown.
pub struct LiveStream {
    stop_flag: Arc<AtomicBool>,
    pause_flag: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
    buffer: SharedBuffer,
    error: SharedError,
}

impl LiveStream {
    /// Pause or resume emission. While paused, capture keeps running but
    /// no PCM is appended to the buffer (and buffered data stays intact).
    pub fn set_paused(&self, paused: bool) {
        self.pause_flag.store(paused, Ordering::SeqCst);
        if paused {
            // Wait for any producer that entered before the flag changed.
            // Once this lock round-trip completes, no later append can pass
            // `append_if_running` until the stream is resumed.
            drop(self.buffer.lock());
        }
    }

    /// Drain all buffered PCM bytes (thread-safe, brief lock).
    pub fn poll(&self) -> Vec<u8> {
        let mut buf = self.buffer.lock();
        std::mem::take(&mut *buf)
    }

    /// Return a capture-thread failure once so the frontend can surface it.
    pub fn take_error(&self) -> Option<String> {
        self.error.lock().take()
    }

    /// Signal stop, join the capture thread, return any remaining bytes.
    pub fn stop(&mut self) -> Vec<u8> {
        self.stop_flag.store(true, Ordering::SeqCst);
        if let Some(h) = self.handle.take() {
            let _ = h.join();
        }
        let mut buf = self.buffer.lock();
        std::mem::take(&mut *buf)
    }
}

/// State managed by Tauri for the live PCM stream.
pub struct LiveStreamState {
    pub stream: Mutex<Option<LiveStream>>,
}

impl LiveStreamState {
    pub fn new() -> Self {
        Self {
            stream: Mutex::new(None),
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// WASAPI live capture (Windows only)
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(windows)]
pub mod wasapi_live {
    use super::*;
    use windows::core::GUID;
    use windows::Win32::Media::Audio::*;
    use windows::Win32::System::Com::*;

    const KSDATAFORMAT_SUBTYPE_IEEE_FLOAT: GUID =
        GUID::from_u128(0x00000003_0000_0010_8000_00aa00389b71);
    const WAVE_FORMAT_IEEE_FLOAT: u16 = 0x0003;
    const WAVE_FORMAT_EXTENSIBLE: u16 = 0xFFFE;
    const AUDCLNT_BUFFERFLAGS_SILENT: u32 = 0x00000002;

    /// Initialize COM on the current thread, tolerating already-initialized apartments.
    fn com_init_tolerant() -> Result<(), String> {
        unsafe {
            let hr = CoInitializeEx(None, COINIT_MULTITHREADED);
            if hr.is_ok() || hr == windows::core::HRESULT(1) {
                return Ok(());
            }
            let hr2 = CoInitializeEx(None, COINIT_APARTMENTTHREADED);
            if hr2.is_ok() || hr2 == windows::core::HRESULT(1) {
                return Ok(());
            }
            Err("COM initialization failed for live capture".into())
        }
    }

    /// Start a live PCM stream that captures system loopback, microphone, or both.
    ///
    /// `source` is one of: "system", "mic", "mixed".
    /// `mic_device_id` / `render_device_id` are optional device overrides.
    pub fn start_live_stream(
        source: &str,
        mic_device_id: Option<&str>,
        render_device_id: Option<&str>,
        pause: Arc<AtomicBool>,
    ) -> Result<LiveStream, String> {
        let stop_flag = Arc::new(AtomicBool::new(false));
        let buffer: SharedBuffer = Arc::new(Mutex::new(Vec::new()));
        let error: SharedError = Arc::new(Mutex::new(None));

        let stop = stop_flag.clone();
        let pause_flag = pause.clone();
        let buf = buffer.clone();
        let capture_error = error.clone();
        let src = source.to_string();
        let mic_id = mic_device_id.map(|s| s.to_string());
        let ren_id = render_device_id.map(|s| s.to_string());

        let handle = std::thread::Builder::new()
            .name("live-pcm".into())
            .spawn(move || {
                if let Err(e) =
                    live_capture_loop(&src, mic_id.as_deref(), ren_id.as_deref(), stop, pause, buf)
                {
                    eprintln!("[live_pcm] capture error: {e}");
                    *capture_error.lock() = Some(e);
                }
            })
            .map_err(|e| format!("failed to spawn live-pcm thread: {e}"))?;

        Ok(LiveStream {
            stop_flag,
            pause_flag,
            handle: Some(handle),
            buffer,
            error,
        })
    }

    /// The live capture loop. For "system" or "mic", uses a single WASAPI client.
    /// For "mixed", uses two clients running in the same thread with simple interleaved reads.
    /// While `pause` is set, capture keeps draining devices but no PCM is emitted.
    fn live_capture_loop(
        source: &str,
        mic_device_id: Option<&str>,
        render_device_id: Option<&str>,
        stop: Arc<AtomicBool>,
        pause: Arc<AtomicBool>,
        buffer: SharedBuffer,
    ) -> Result<(), String> {
        com_init_tolerant()?;

        match source {
            "system" => {
                let mut ctx = LiveCaptureContext::open_loopback(render_device_id)?;
                run_single_capture(&mut ctx, &stop, &pause, &buffer);
                ctx.shutdown();
            }
            "mic" => {
                let mut ctx = LiveCaptureContext::open_mic(mic_device_id)?;
                run_single_capture(&mut ctx, &stop, &pause, &buffer);
                ctx.shutdown();
            }
            "mixed" => {
                // Run both captures. For simplicity, we use two threads that each
                // push into a separate buffer, and a mixer thread that combines them.
                let mic_buf: SharedBuffer = Arc::new(Mutex::new(Vec::new()));
                let sys_buf: SharedBuffer = Arc::new(Mutex::new(Vec::new()));

                let mic_stop = stop.clone();
                let sys_stop = stop.clone();
                let mic_pause = pause.clone();
                let sys_pause = pause.clone();
                let mic_b = mic_buf.clone();
                let sys_b = sys_buf.clone();
                let mic_id_owned = mic_device_id.map(|s| s.to_string());
                let ren_id_owned = render_device_id.map(|s| s.to_string());

                let mic_handle = std::thread::Builder::new()
                    .name("live-pcm-mic".into())
                    .spawn(move || {
                        com_init_tolerant().ok();
                        if let Ok(mut ctx) = LiveCaptureContext::open_mic(mic_id_owned.as_deref()) {
                            run_single_capture(&mut ctx, &mic_stop, &mic_pause, &mic_b);
                            ctx.shutdown();
                        }
                    })
                    .map_err(|e| format!("spawn mic: {e}"))?;

                let sys_handle = std::thread::Builder::new()
                    .name("live-pcm-sys".into())
                    .spawn(move || {
                        com_init_tolerant().ok();
                        if let Ok(mut ctx) =
                            LiveCaptureContext::open_loopback(ren_id_owned.as_deref())
                        {
                            run_single_capture(&mut ctx, &sys_stop, &sys_pause, &sys_b);
                            ctx.shutdown();
                        }
                    })
                    .map_err(|e| format!("spawn sys: {e}"))?;

                // Mixer loop: read from both buffers, average, push to output
                while !stop.load(Ordering::SeqCst) {
                    let mic_data = {
                        let mut b = mic_buf.lock();
                        std::mem::take(&mut *b)
                    };
                    let sys_data = {
                        let mut b = sys_buf.lock();
                        std::mem::take(&mut *b)
                    };

                    if !pause.load(Ordering::SeqCst) {
                        let mixed = mix_pcm16(&mic_data, &sys_data);
                        if !mixed.is_empty() {
                            append_if_running(&buffer, &pause, &mixed);
                        }
                    }

                    std::thread::sleep(std::time::Duration::from_millis(10));
                }

                let _ = mic_handle.join();
                let _ = sys_handle.join();

                // Drain any remaining data from sub-buffers
                let final_mic: Vec<u8> = { std::mem::take(&mut *mic_buf.lock()) };
                let final_sys: Vec<u8> = { std::mem::take(&mut *sys_buf.lock()) };
                if !pause.load(Ordering::SeqCst) {
                    let final_mixed = mix_pcm16(&final_mic, &final_sys);
                    if !final_mixed.is_empty() {
                        append_if_running(&buffer, &pause, &final_mixed);
                    }
                }
            }
            other => return Err(format!("unknown live source: {other}")),
        }

        Ok(())
    }

    /// Holds a WASAPI client, capture client, and resampler for one audio stream.
    struct LiveCaptureContext {
        audio_client: IAudioClient,
        capture_client: IAudioCaptureClient,
        resampler: Resampler,
        src_channels: u16,
        src_bits: u16,
        is_ieee_float: bool,
        mix_format_ptr: *mut WAVEFORMATEX,
    }

    // SAFETY: COM objects are used only within the thread that created them.
    // The thread owns all pointers exclusively.
    unsafe impl Send for LiveCaptureContext {}

    impl LiveCaptureContext {
        fn open_loopback(render_device_id: Option<&str>) -> Result<Self, String> {
            Self::open(render_device_id, false)
        }

        fn open_mic(mic_device_id: Option<&str>) -> Result<Self, String> {
            Self::open(mic_device_id, true)
        }

        fn open(device_id: Option<&str>, is_capture: bool) -> Result<Self, String> {
            unsafe {
                let enumerator: IMMDeviceEnumerator =
                    CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)
                        .map_err(|e| format!("CoCreateInstance: {e}"))?;

                let flow = if is_capture { eCapture } else { eRender };
                let device = if let Some(id) = device_id {
                    let id_wide: Vec<u16> = id.encode_utf16().chain(std::iter::once(0)).collect();
                    enumerator
                        .GetDevice(windows::core::PCWSTR(id_wide.as_ptr()))
                        .map_err(|_| format!("device not found: {id}"))?
                } else {
                    enumerator
                        .GetDefaultAudioEndpoint(flow, eConsole)
                        .map_err(|e| format!("GetDefaultAudioEndpoint: {e}"))?
                };

                let audio_client: IAudioClient = device
                    .Activate::<IAudioClient>(CLSCTX_ALL, None)
                    .map_err(|e| format!("Activate IAudioClient: {e}"))?;

                let mix_format_ptr = audio_client
                    .GetMixFormat()
                    .map_err(|e| format!("GetMixFormat: {e}"))?;

                if mix_format_ptr.is_null() {
                    return Err("GetMixFormat returned null".into());
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
                    return Err(format!(
                        "unsupported format: tag={} bits={} float={}",
                        w_format_tag, src_bits, is_ieee_float
                    ));
                }

                let mut stream_flags = AUDCLNT_STREAMFLAGS_NOPERSIST;
                if !is_capture {
                    stream_flags |= AUDCLNT_STREAMFLAGS_LOOPBACK;
                }

                let buffer_duration: i64 = 500_000; // 50ms buffer
                audio_client
                    .Initialize(
                        AUDCLNT_SHAREMODE_SHARED,
                        stream_flags,
                        buffer_duration,
                        0,
                        mix_format_ptr,
                        None,
                    )
                    .map_err(|e| format!("Initialize: {e}"))?;

                let capture_client: IAudioCaptureClient = audio_client
                    .GetService()
                    .map_err(|e| format!("GetService: {e}"))?;

                audio_client.Start().map_err(|e| format!("Start: {e}"))?;

                let resampler = Resampler::new(src_sample_rate);

                Ok(LiveCaptureContext {
                    audio_client,
                    capture_client,
                    resampler,
                    src_channels,
                    src_bits,
                    is_ieee_float,
                    mix_format_ptr,
                })
            }
        }

        fn shutdown(&mut self) {
            unsafe {
                let _ = self.audio_client.Stop();
                CoTaskMemFree(Some(self.mix_format_ptr as *const _));
            }
        }
    }

    /// Run a single WASAPI capture loop, pushing PCM16 LE bytes into `buffer`.
    /// While `pause` is set, packets are still drained (WASAPI shared mode
    /// requires continuous reads) but not buffered.
    fn run_single_capture(
        ctx: &mut LiveCaptureContext,
        stop: &Arc<AtomicBool>,
        pause: &Arc<AtomicBool>,
        buffer: &SharedBuffer,
    ) {
        loop {
            if stop.load(Ordering::SeqCst) {
                break;
            }

            let packet_frames = unsafe { ctx.capture_client.GetNextPacketSize() };
            let packet_frames = match packet_frames {
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

            let mono_f32 = unsafe {
                let mut data_ptr: *mut u8 = std::ptr::null_mut();
                let mut num_frames: u32 = 0;
                let mut flags: u32 = 0;
                let mut qpc_position: u64 = 0;

                if let Err(e) = ctx.capture_client.GetBuffer(
                    &mut data_ptr,
                    &mut num_frames,
                    &mut flags,
                    None,
                    Some(&mut qpc_position),
                ) {
                    eprintln!("[live_pcm] GetBuffer: {e}");
                    std::thread::sleep(std::time::Duration::from_millis(5));
                    continue;
                }

                let is_silent = (flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0;
                let mono = if is_silent || data_ptr.is_null() || num_frames == 0 {
                    vec![0.0f32; num_frames as usize]
                } else {
                    convert_to_mono_f32(
                        data_ptr,
                        num_frames,
                        ctx.src_channels,
                        ctx.src_bits,
                        ctx.is_ieee_float,
                    )
                    .unwrap_or_else(|_| vec![0.0f32; num_frames as usize])
                };

                let _ = ctx.capture_client.ReleaseBuffer(num_frames);
                mono
            };

            if pause.load(Ordering::SeqCst) {
                continue;
            }
            let resampled = ctx.resampler.process(&mono_f32);
            if !resampled.is_empty() {
                let pcm = f32_to_i16_bytes(&resampled);
                append_if_running(buffer, pause, &pcm);
            }
        }
    }

    /// Convert interleaved multi-channel audio to mono f32.
    ///
    /// # Safety
    /// `data_ptr` must point to `num_frames * channels` samples in the
    /// format described by `(is_ieee_float, bits)`.
    unsafe fn convert_to_mono_f32(
        data_ptr: *const u8,
        num_frames: u32,
        channels: u16,
        bits: u16,
        is_ieee_float: bool,
    ) -> Result<Vec<f32>, String> {
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
                return Err(format!(
                    "unsupported: float={} bits={} channels={}",
                    is_ieee_float, bits, channels
                ));
            }
        }
        Ok(mono)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Mock live backend for non-Windows / tests
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(not(windows))]
pub mod mock_live {
    use super::*;

    pub fn start_live_stream(
        source: &str,
        _mic_device_id: Option<&str>,
        _render_device_id: Option<&str>,
        pause: Arc<AtomicBool>,
    ) -> Result<LiveStream, String> {
        let stop_flag = Arc::new(AtomicBool::new(false));
        let pause_flag = pause.clone();
        let buffer: SharedBuffer = Arc::new(Mutex::new(Vec::new()));
        let error: SharedError = Arc::new(Mutex::new(None));
        let stop = stop_flag.clone();
        let buf = buffer.clone();
        let src = source.to_string();

        let handle = std::thread::Builder::new()
            .name("live-pcm-mock".into())
            .spawn(move || {
                // Generate silence at 16kHz mono 16-bit LE: 320 bytes = 10ms per chunk
                let chunk = vec![0u8; 320];
                while !stop.load(Ordering::SeqCst) {
                    if src != "none" && !pause.load(Ordering::SeqCst) {
                        append_if_running(&buf, &pause, &chunk);
                    }
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
            })
            .map_err(|e| format!("spawn mock live: {e}"))?;

        Ok(LiveStream {
            stop_flag,
            pause_flag,
            handle: Some(handle),
            buffer,
            error,
        })
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Unified entry point
// ─────────────────────────────────────────────────────────────────────────────

/// Start a live PCM stream. Platform-aware: WASAPI on Windows, mock elsewhere.
/// `pause` controls emission: while set, capture keeps running but no PCM is buffered.
pub fn start_live_stream(
    source: &str,
    mic_device_id: Option<&str>,
    render_device_id: Option<&str>,
    pause: Arc<AtomicBool>,
) -> Result<LiveStream, String> {
    #[cfg(windows)]
    {
        wasapi_live::start_live_stream(source, mic_device_id, render_device_id, pause)
    }
    #[cfg(not(windows))]
    {
        mock_live::start_live_stream(source, mic_device_id, render_device_id, pause)
    }
}

// ─────────────────────────────────────────────────────────────────────────────
// Tests
// ─────────────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    /// Create a mock LiveStream that generates PCM16 silence at 16kHz mono.
    fn mock_live_stream(chunk_interval_ms: u64) -> LiveStream {
        let stop_flag = Arc::new(AtomicBool::new(false));
        let pause_flag = Arc::new(AtomicBool::new(false));
        let buffer: SharedBuffer = Arc::new(Mutex::new(Vec::new()));
        let error: SharedError = Arc::new(Mutex::new(None));
        let stop = stop_flag.clone();
        let pause = pause_flag.clone();
        let buf = buffer.clone();
        let handle = std::thread::spawn(move || {
            // 16kHz mono 16-bit = 32 bytes/ms. 10ms chunk = 320 bytes.
            let chunk = vec![0u8; 320];
            while !stop.load(Ordering::SeqCst) {
                if !pause.load(Ordering::SeqCst) {
                    append_if_running(&buf, &pause, &chunk);
                }
                std::thread::sleep(std::time::Duration::from_millis(chunk_interval_ms));
            }
        });
        LiveStream {
            stop_flag,
            pause_flag,
            handle: Some(handle),
            buffer,
            error,
        }
    }

    /// Wait for data to appear in the stream, polling every 10ms up to `timeout`.
    fn wait_for_data(stream: &LiveStream, timeout: std::time::Duration) -> Vec<u8> {
        let deadline = std::time::Instant::now() + timeout;
        loop {
            let data = stream.poll();
            if !data.is_empty() {
                return data;
            }
            if std::time::Instant::now() >= deadline {
                return data;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
    }

    #[test]
    fn test_f32_to_i16_bytes() {
        let samples = vec![0.0f32, 1.0, -1.0, 0.5];
        let bytes = f32_to_i16_bytes(&samples);
        assert_eq!(bytes.len(), 8);
        assert_eq!(i16::from_le_bytes([bytes[0], bytes[1]]), 0);
        assert_eq!(i16::from_le_bytes([bytes[2], bytes[3]]), 32767);
        assert_eq!(i16::from_le_bytes([bytes[4], bytes[5]]), -32767);
    }

    #[test]
    fn test_resampler_basic() {
        let mut resampler = Resampler::new(48000);
        let input = vec![1.0f32, 0.0, 0.0, 0.0, 0.0, 0.0];
        let output = resampler.process(&input);
        assert!(!output.is_empty());
    }

    #[test]
    fn test_resampler_silence() {
        let mut resampler = Resampler::new(44100);
        let input = vec![0.0f32; 1024];
        let output = resampler.process(&input);
        for &s in &output {
            assert!((s).abs() < 0.001, "expected near-zero, got {s}");
        }
    }

    #[test]
    fn test_shared_buffer_push_poll() {
        let buf: SharedBuffer = Arc::new(Mutex::new(Vec::new()));
        buf.lock().extend_from_slice(&[1, 2, 3, 4]);
        buf.lock().extend_from_slice(&[5, 6]);
        let drained: Vec<u8> = { std::mem::take(&mut *buf.lock()) };
        assert_eq!(drained, vec![1, 2, 3, 4, 5, 6]);
        let empty: Vec<u8> = { std::mem::take(&mut *buf.lock()) };
        assert!(empty.is_empty());
    }

    #[test]
    fn test_live_stream_start_stop() {
        let mut stream = mock_live_stream(10);
        let data = wait_for_data(&stream, std::time::Duration::from_millis(200));
        let remaining = stream.stop();
        let total = data.len() + remaining.len();
        assert!(total >= 320, "expected at least 1 chunk, got {total} bytes");
    }

    #[test]
    fn test_live_stream_poll_returns_data() {
        let stream = mock_live_stream(10);
        let data = wait_for_data(&stream, std::time::Duration::from_millis(200));
        assert!(!data.is_empty(), "poll should return buffered data");
        // poll drains — second poll should be empty or very small
        std::thread::sleep(std::time::Duration::from_millis(20));
        let data2 = stream.poll();
        assert!(
            data2.len() <= data.len() + 320,
            "second poll should be smaller"
        );
        let mut s = stream;
        s.stop();
    }

    #[test]
    fn test_stop_returns_remaining_data() {
        let mut stream = mock_live_stream(10);
        wait_for_data(&stream, std::time::Duration::from_millis(200));
        std::thread::sleep(std::time::Duration::from_millis(50));
        let remaining = stream.stop();
        assert!(
            !remaining.is_empty(),
            "stop should return any unpolled data"
        );
    }

    #[test]
    fn test_empty_source_stops_gracefully() {
        // "none" source: start_live_stream on non-windows returns mock with no data
        // for "none". On windows, it tries WASAPI which will fail — handle gracefully.
        match super::start_live_stream("none", None, None, Arc::new(AtomicBool::new(false))) {
            Ok(mut stream) => {
                std::thread::sleep(std::time::Duration::from_millis(30));
                let data = stream.stop();
                assert!(data.is_empty(), "none source should produce no data");
            }
            // On Windows, "none" is an unknown source — that's acceptable
            Err(e) => assert!(e.contains("unknown"), "unexpected error: {e}"),
        }
    }

    #[test]
    fn test_stop_flag_terminates_thread() {
        let mut stream = mock_live_stream(10);
        std::thread::sleep(std::time::Duration::from_millis(50));
        let t0 = std::time::Instant::now();
        stream.stop();
        let elapsed = t0.elapsed();
        assert!(elapsed.as_millis() < 500, "stop took too long: {elapsed:?}");
    }

    #[test]
    fn test_poll_is_idempotent_when_empty() {
        let stream = mock_live_stream(50);
        // Don't wait for data; poll immediately — should return empty without panic
        let _ = stream.poll();
        let _ = stream.poll();
        let mut s = stream;
        s.stop();
    }

    fn i16_le(v: i16) -> [u8; 2] {
        v.to_le_bytes()
    }

    #[test]
    fn test_mix_pcm16_silent_mic_passes_system_audio() {
        // Silent mic (empty track) + loud system: output must equal system audio,
        // so captions survive when the microphone picks up nothing.
        let mic: Vec<u8> = Vec::new();
        let mut sys = Vec::new();
        sys.extend_from_slice(&i16_le(20_000));
        sys.extend_from_slice(&i16_le(-12_000));
        let mixed = mix_pcm16(&mic, &sys);
        assert_eq!(mixed, sys);
    }

    #[test]
    fn test_mix_pcm16_averages_both_tracks() {
        let mut mic = Vec::new();
        mic.extend_from_slice(&i16_le(1_000));
        mic.extend_from_slice(&i16_le(-500));
        let mut sys = Vec::new();
        sys.extend_from_slice(&i16_le(3_000));
        sys.extend_from_slice(&i16_le(1_500));
        let mixed = mix_pcm16(&mic, &sys);
        assert_eq!(mixed.len(), 4);
        assert_eq!(i16::from_le_bytes([mixed[0], mixed[1]]), 2_000);
        assert_eq!(i16::from_le_bytes([mixed[2], mixed[3]]), 500);
    }

    #[test]
    fn test_mix_pcm16_clamps_full_scale() {
        let mic = i16_le(32_767).to_vec();
        let sys = i16_le(32_767).to_vec();
        let mixed = mix_pcm16(&mic, &sys);
        assert_eq!(i16::from_le_bytes([mixed[0], mixed[1]]), 32_767);
    }

    #[test]
    fn test_pause_stops_emission_until_resumed() {
        let mut stream = mock_live_stream(10);
        let _ = wait_for_data(&stream, std::time::Duration::from_millis(200));

        stream.set_paused(true);
        let _ = stream.poll();
        std::thread::sleep(std::time::Duration::from_millis(60));
        assert!(
            stream.poll().is_empty(),
            "no PCM should be emitted while paused"
        );

        stream.set_paused(false);
        let resumed = wait_for_data(&stream, std::time::Duration::from_millis(200));
        assert!(!resumed.is_empty(), "emission must resume after unpause");
        stream.stop();
    }
}
