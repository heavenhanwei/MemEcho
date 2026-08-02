mod audio;
mod commands;
pub mod credential;
pub mod db;
pub mod evidence;
pub mod importer;
mod paths;
mod recovery;
pub mod report;
mod state;
pub mod upload;

use audio::AudioCapture;
use parking_lot::Mutex;
use state::CaptureState;
use std::sync::Arc;

pub struct AppState {
    pub capture: Arc<Mutex<CaptureState>>,
    pub audio: Arc<Mutex<AudioCapture>>,
    pub sessions_dir: std::path::PathBuf,
    pub db: db::Repository,
}

pub fn run() {
    let sessions_dir = paths::sessions_dir();
    let db_path = sessions_dir.join("memecho.db");
    let db = db::Repository::open(&db_path).expect("failed to open local database");

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            capture: Arc::new(Mutex::new(CaptureState::new())),
            audio: Arc::new(Mutex::new(AudioCapture::new())),
            sessions_dir,
            db,
        })
        .invoke_handler(tauri::generate_handler![
            commands::list_audio_devices,
            commands::start_capture,
            commands::pause_capture,
            commands::resume_capture,
            commands::stop_capture,
            commands::list_recoverable_sessions,
            commands::delete_local_session,
            commands::recover_session,
            commands::credential_set,
            commands::credential_get,
            commands::credential_delete,
            commands::create_local_session,
            commands::update_local_session,
            commands::list_local_sessions,
            commands::get_local_session,
            commands::save_analysis_results,
            commands::get_analysis_results,
            commands::read_evidence_clip,
            commands::import_media_file,
            commands::import_text_content,
            commands::upload_session_tracks,
            commands::save_report_files,
        ])
        .run(tauri::generate_context!())
        .expect("error while running memEcho");
}
