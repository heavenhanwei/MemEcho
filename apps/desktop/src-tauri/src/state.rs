use std::path::PathBuf;
use std::time::Instant;

/// Recording state machine.
///
/// Transitions:
///   Idle → Recording (start)
///   Recording → Paused (pause)
///   Paused → Recording (resume)
///   Recording → Idle (stop)
///   Paused → Idle (stop)
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RecordingStatus {
    Idle,
    Recording,
    Paused,
}

#[derive(Debug)]
pub struct CaptureState {
    pub status: RecordingStatus,
    pub session_id: Option<String>,
    pub mic_path: Option<PathBuf>,
    pub loopback_path: Option<PathBuf>,
    pub sample_rate: u32,
    pub started_at: Option<chrono::DateTime<chrono::Utc>>,
    pub last_write_pos: u64,
    pub recording_start: Option<Instant>,
    pub paused_duration: std::time::Duration,
    pub pause_start: Option<Instant>,
}

impl CaptureState {
    pub fn new() -> Self {
        Self {
            status: RecordingStatus::Idle,
            session_id: None,
            mic_path: None,
            loopback_path: None,
            sample_rate: 16000,
            started_at: None,
            last_write_pos: 0,
            recording_start: None,
            paused_duration: std::time::Duration::ZERO,
            pause_start: None,
        }
    }

    pub fn start_recording(
        &mut self,
        session_id: String,
        mic_path: PathBuf,
        loopback_path: PathBuf,
    ) -> Result<(), StateError> {
        if self.status != RecordingStatus::Idle {
            return Err(StateError::InvalidTransition {
                from: format!("{:?}", self.status),
                to: "Recording".into(),
            });
        }
        self.status = RecordingStatus::Recording;
        self.session_id = Some(session_id);
        self.mic_path = Some(mic_path);
        self.loopback_path = Some(loopback_path);
        self.started_at = Some(chrono::Utc::now());
        self.last_write_pos = 0;
        self.recording_start = Some(Instant::now());
        self.paused_duration = std::time::Duration::ZERO;
        self.pause_start = None;
        Ok(())
    }

    pub fn pause(&mut self) -> Result<(), StateError> {
        if self.status != RecordingStatus::Recording {
            return Err(StateError::InvalidTransition {
                from: format!("{:?}", self.status),
                to: "Paused".into(),
            });
        }
        self.status = RecordingStatus::Paused;
        self.pause_start = Some(Instant::now());
        Ok(())
    }

    pub fn resume(&mut self) -> Result<(), StateError> {
        if self.status != RecordingStatus::Paused {
            return Err(StateError::InvalidTransition {
                from: format!("{:?}", self.status),
                to: "Recording".into(),
            });
        }
        if let Some(pause_start) = self.pause_start.take() {
            self.paused_duration += pause_start.elapsed();
        }
        self.status = RecordingStatus::Recording;
        Ok(())
    }

    pub fn stop(&mut self) -> Result<StopInfo, StateError> {
        if self.status != RecordingStatus::Recording && self.status != RecordingStatus::Paused {
            return Err(StateError::InvalidTransition {
                from: format!("{:?}", self.status),
                to: "Idle".into(),
            });
        }
        let info = StopInfo {
            session_id: self.session_id.take().unwrap_or_default(),
            mic_path: self.mic_path.take().unwrap_or_default(),
            loopback_path: self.loopback_path.take().unwrap_or_default(),
            sample_rate: self.sample_rate,
            started_at: self.started_at.take(),
            last_write_pos: self.last_write_pos,
        };
        self.status = RecordingStatus::Idle;
        self.recording_start = None;
        self.paused_duration = std::time::Duration::ZERO;
        self.pause_start = None;
        Ok(info)
    }

    pub fn elapsed_seconds(&self) -> f64 {
        match self.recording_start {
            Some(start) => {
                let total = start.elapsed();
                let pause = if let Some(ps) = self.pause_start {
                    self.paused_duration + ps.elapsed()
                } else {
                    self.paused_duration
                };
                (total - pause).as_secs_f64()
            }
            None => 0.0,
        }
    }

    pub fn max_duration_secs(&self) -> f64 {
        7200.0 // 2 hours
    }

    pub fn is_at_max_duration(&self) -> bool {
        self.elapsed_seconds() >= self.max_duration_secs()
    }
}

#[derive(Debug)]
pub struct StopInfo {
    pub session_id: String,
    pub mic_path: PathBuf,
    pub loopback_path: PathBuf,
    pub sample_rate: u32,
    pub started_at: Option<chrono::DateTime<chrono::Utc>>,
    pub last_write_pos: u64,
}

#[derive(Debug, thiserror::Error)]
pub enum StateError {
    #[error("invalid state transition: {from} → {to}")]
    InvalidTransition { from: String, to: String },
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn test_initial_state_is_idle() {
        let state = CaptureState::new();
        assert_eq!(state.status, RecordingStatus::Idle);
        assert!(state.session_id.is_none());
    }

    #[test]
    fn test_start_recording_from_idle() {
        let mut state = CaptureState::new();
        let result = state.start_recording(
            "sess-1".into(),
            PathBuf::from("mic.wav"),
            PathBuf::from("loop.wav"),
        );
        assert!(result.is_ok());
        assert_eq!(state.status, RecordingStatus::Recording);
        assert_eq!(state.session_id.as_deref(), Some("sess-1"));
    }

    #[test]
    fn test_start_recording_fails_if_not_idle() {
        let mut state = CaptureState::new();
        state
            .start_recording("s1".into(), PathBuf::from("m.wav"), PathBuf::from("l.wav"))
            .unwrap();
        let result =
            state.start_recording("s2".into(), PathBuf::from("m.wav"), PathBuf::from("l.wav"));
        assert!(result.is_err());
    }

    #[test]
    fn test_pause_from_recording() {
        let mut state = CaptureState::new();
        state
            .start_recording("s1".into(), PathBuf::from("m.wav"), PathBuf::from("l.wav"))
            .unwrap();
        assert!(state.pause().is_ok());
        assert_eq!(state.status, RecordingStatus::Paused);
    }

    #[test]
    fn test_pause_fails_from_idle() {
        let mut state = CaptureState::new();
        assert!(state.pause().is_err());
    }

    #[test]
    fn test_resume_from_paused() {
        let mut state = CaptureState::new();
        state
            .start_recording("s1".into(), PathBuf::from("m.wav"), PathBuf::from("l.wav"))
            .unwrap();
        state.pause().unwrap();
        assert!(state.resume().is_ok());
        assert_eq!(state.status, RecordingStatus::Recording);
    }

    #[test]
    fn test_resume_fails_from_idle() {
        let mut state = CaptureState::new();
        assert!(state.resume().is_err());
    }

    #[test]
    fn test_stop_from_recording() {
        let mut state = CaptureState::new();
        state
            .start_recording("s1".into(), PathBuf::from("m.wav"), PathBuf::from("l.wav"))
            .unwrap();
        let info = state.stop().unwrap();
        assert_eq!(info.session_id, "s1");
        assert_eq!(state.status, RecordingStatus::Idle);
        assert!(state.session_id.is_none());
    }

    #[test]
    fn test_stop_from_paused() {
        let mut state = CaptureState::new();
        state
            .start_recording("s1".into(), PathBuf::from("m.wav"), PathBuf::from("l.wav"))
            .unwrap();
        state.pause().unwrap();
        let info = state.stop().unwrap();
        assert_eq!(info.session_id, "s1");
        assert_eq!(state.status, RecordingStatus::Idle);
    }

    #[test]
    fn test_stop_fails_from_idle() {
        let mut state = CaptureState::new();
        assert!(state.stop().is_err());
    }

    #[test]
    fn test_pause_resume_preserves_data() {
        let mut state = CaptureState::new();
        state
            .start_recording("s1".into(), PathBuf::from("m.wav"), PathBuf::from("l.wav"))
            .unwrap();
        state.last_write_pos = 1024;
        state.pause().unwrap();
        state.resume().unwrap();
        assert_eq!(state.last_write_pos, 1024);
    }

    #[test]
    fn test_max_duration_is_two_hours() {
        let state = CaptureState::new();
        assert_eq!(state.max_duration_secs(), 7200.0);
    }
}
