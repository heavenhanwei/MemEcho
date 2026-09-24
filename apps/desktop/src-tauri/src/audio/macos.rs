//! macOS native audio bridge.
//!
//! The packaged Swift helper emits 16 kHz mono PCM16-LE on stdout. Keeping
//! ScreenCaptureKit and AVFoundation in a small native process gives macOS a
//! normal TCC permission boundary while the Rust recording/recovery pipeline
//! remains shared with Windows.

use super::capture::{AudioBackend, AudioDevice, CaptureError, CaptureThreadResult};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

pub const AUDIO_HELPER_BASE_NAME: &str = "memecho-audio-capture";
pub const AUDIO_HELPER_OVERRIDE: &str = "MEMECHO_MACOS_AUDIO_HELPER";

#[derive(Debug, Clone, Copy)]
pub enum MacAudioSource {
    Microphone,
    System,
}

impl MacAudioSource {
    fn as_arg(self) -> &'static str {
        match self {
            Self::Microphone => "mic",
            Self::System => "system",
        }
    }
}

pub fn devices() -> Vec<AudioDevice> {
    vec![
        AudioDevice {
            id: "macos:default-microphone".into(),
            name: "系统默认麦克风".into(),
            is_input: true,
            is_default: true,
        },
        AudioDevice {
            id: "macos:system-audio".into(),
            name: "系统声音（ScreenCaptureKit）".into(),
            is_input: false,
            is_default: true,
        },
    ]
}

/// Resolve the helper in a signed app bundle or through an explicit dev hook.
pub fn resolve_audio_helper() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var(AUDIO_HELPER_OVERRIDE) {
        let path = PathBuf::from(explicit);
        if path.is_file() {
            return Some(path);
        }
    }
    if let Ok(current) = std::env::current_exe() {
        if let Some(dir) = current.parent() {
            for candidate in [
                dir.join(AUDIO_HELPER_BASE_NAME),
                dir.join("binaries").join(AUDIO_HELPER_BASE_NAME),
            ] {
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        }
    }
    None
}

pub struct MacosAudioBackend {
    source: MacAudioSource,
    helper: PathBuf,
}

impl MacosAudioBackend {
    pub fn new(source: MacAudioSource) -> Result<Self, CaptureError> {
        let helper = resolve_audio_helper().ok_or_else(|| {
            CaptureError::Platform(
                "macOS 音频采集组件未随应用安装；请重新安装完整的 memEcho.app".into(),
            )
        })?;
        Ok(Self { source, helper })
    }
}

impl AudioBackend for MacosAudioBackend {
    fn enumerate_devices(&self) -> Result<Vec<AudioDevice>, CaptureError> {
        Ok(devices())
    }

    fn resolve_device(
        &self,
        device_id: Option<&str>,
        is_capture: bool,
    ) -> Result<AudioDevice, CaptureError> {
        let expected = if is_capture {
            "macos:default-microphone"
        } else {
            "macos:system-audio"
        };
        if let Some(id) = device_id {
            if id != expected {
                return Err(CaptureError::DeviceNotFound(id.to_string()));
            }
        }
        devices()
            .into_iter()
            .find(|device| device.is_input == is_capture)
            .ok_or(CaptureError::NoDevices)
    }

    fn capture_loop(
        &self,
        _device: &AudioDevice,
        wav: &mut crate::audio::wav::StreamingWav,
        stop: Arc<AtomicBool>,
        pause: Arc<AtomicBool>,
        bytes_written: Arc<AtomicU64>,
    ) -> Result<CaptureThreadResult, CaptureError> {
        let (mut child, mut stdout) =
            spawn_helper(&self.helper, self.source.as_arg()).map_err(CaptureError::Platform)?;
        let mut buffer = [0u8; 8192];
        let mut last_flush = std::time::Instant::now();
        let mut first_error: Option<String> = None;

        while !stop.load(Ordering::SeqCst) {
            match stdout.read(&mut buffer) {
                Ok(0) => {
                    let status = child.wait().ok();
                    first_error = Some(helper_exit_message(status));
                    break;
                }
                Ok(read) => {
                    // PCM16 must stay sample-aligned. The helper writes even sized
                    // chunks, but retain this guard at the process boundary.
                    let aligned = read - (read % 2);
                    if aligned > 0 && !pause.load(Ordering::SeqCst) {
                        if let Err(error) = wav.append(&buffer[..aligned]) {
                            first_error = Some(format!("写入 WAV 失败：{error}"));
                            break;
                        }
                    }
                    if last_flush.elapsed() >= std::time::Duration::from_secs(1) {
                        if let Ok(confirmed) = wav.flush_safe() {
                            bytes_written.store(confirmed, Ordering::SeqCst);
                        }
                        last_flush = std::time::Instant::now();
                    }
                }
                Err(error) => {
                    first_error = Some(format!("读取 macOS 音频失败：{error}"));
                    break;
                }
            }
        }

        let _ = child.kill();
        let _ = child.wait();
        if let Err(error) = wav.flush_safe() {
            first_error.get_or_insert_with(|| format!("刷新 WAV 失败：{error}"));
        }
        if let Err(error) = wav.finalize() {
            first_error.get_or_insert_with(|| format!("完成 WAV 失败：{error}"));
        }
        bytes_written.store(wav.flushed_bytes(), Ordering::SeqCst);

        if let Some(error) = first_error {
            return Err(CaptureError::Platform(error));
        }
        Ok(CaptureThreadResult {
            bytes_written: wav.flushed_bytes(),
        })
    }
}

pub fn spawn_live_helper(source: &str) -> Result<(Child, ChildStdout), String> {
    let helper = resolve_audio_helper().ok_or_else(|| {
        "macOS 音频采集组件未随应用安装；请重新安装完整的 memEcho.app".to_string()
    })?;
    spawn_helper(&helper, source)
}

fn spawn_helper(program: &Path, source: &str) -> Result<(Child, ChildStdout), String> {
    let mut child = Command::new(program)
        .arg("--source")
        .arg(source)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|error| format!("无法启动 macOS 音频采集组件：{error}"))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "macOS 音频采集组件没有可读输出".to_string())?;
    Ok((child, stdout))
}

pub(crate) fn helper_exit_message(status: Option<std::process::ExitStatus>) -> String {
    match status.and_then(|value| value.code()) {
        Some(20) => "没有麦克风权限；请在“系统设置 → 隐私与安全性 → 麦克风”中允许 memEcho".into(),
        Some(21) => "没有屏幕与系统录音权限；请在“系统设置 → 隐私与安全性 → 屏幕与系统录音”中允许 memEcho，然后重新启动应用".into(),
        Some(code) => format!("macOS 音频采集组件已退出（错误码 {code}）"),
        None => "macOS 音频采集组件意外退出".into(),
    }
}
