mod audio;
mod commands;
pub mod credential;
pub mod db;
pub mod evidence;
pub mod gateway_check;
pub mod gateway_supervisor;
pub mod importer;
pub mod llm_config;
mod paths;
mod recovery;
pub mod report;
mod state;
pub mod upload;

use audio::AudioCapture;
use audio::LiveStreamState;
use parking_lot::Mutex;
use state::CaptureState;
use std::sync::Arc;
use tauri::Manager;

pub struct AppState {
    pub capture: Arc<Mutex<CaptureState>>,
    pub audio: Arc<Mutex<AudioCapture>>,
    pub live: Arc<LiveStreamState>,
    pub sessions_dir: std::path::PathBuf,
    pub db: db::Repository,
    pub gateway: Arc<tokio::sync::Mutex<gateway_supervisor::GatewaySupervisor>>,
}

pub fn run() {
    let sessions_dir = paths::sessions_dir();
    let db_path = sessions_dir.join("memecho.db");
    let db = db::Repository::open(&db_path).expect("failed to open local database");

    let gateway = Arc::new(tokio::sync::Mutex::new(
        gateway_supervisor::GatewaySupervisor::new(),
    ));

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            capture: Arc::new(Mutex::new(CaptureState::new())),
            audio: Arc::new(Mutex::new(AudioCapture::new())),
            live: Arc::new(LiveStreamState::new()),
            sessions_dir,
            db,
            gateway: gateway.clone(),
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
            commands::list_source_relations,
            commands::read_evidence_clip,
            commands::import_media_file,
            commands::import_text_content,
            commands::upload_session_tracks,
            commands::save_report_files,
            commands::check_gateway,
            commands::set_gateway_url,
            commands::get_gateway_url,
            commands::gateway_connection,
            commands::start_gateway_sidecar,
            commands::get_provider_profiles_config_path,
            commands::open_provider_profiles_config,
            commands::get_llm_config,
            commands::set_llm_config_file,
            commands::start_live_stream,
            commands::pause_live_stream,
            commands::resume_live_stream,
            commands::poll_live_pcm,
            commands::stop_live_stream,
        ])
        .build(tauri::generate_context!())
        .expect("error while building memEcho");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            let state = app_handle.state::<AppState>();
            tauri::async_runtime::block_on(async move {
                state.gateway.lock().await.shutdown().await;
            });
        }
    });
}
