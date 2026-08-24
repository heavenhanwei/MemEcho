use serde::{Deserialize, Serialize};
use std::path::Path;

const CONFIG_FILE_NAME: &str = "llm_config.json";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct LlmConfig {
    #[serde(default)]
    pub text_endpoint: String,
    #[serde(default)]
    pub text_model: String,
    #[serde(default)]
    pub audio_endpoint: String,
    #[serde(default)]
    pub workspace_id: String,
}

fn config_path(sessions_dir: &Path) -> std::path::PathBuf {
    sessions_dir
        .parent()
        .unwrap_or(sessions_dir)
        .join(CONFIG_FILE_NAME)
}

pub fn load_llm_config(sessions_dir: &Path) -> LlmConfig {
    let path = config_path(sessions_dir);
    std::fs::read_to_string(&path)
        .ok()
        .and_then(|data| serde_json::from_str(&data).ok())
        .unwrap_or_default()
}

pub fn save_llm_config(sessions_dir: &Path, config: &LlmConfig) -> Result<(), String> {
    let path = config_path(sessions_dir);
    let json =
        serde_json::to_string_pretty(config).map_err(|e| format!("config serialize: {}", e))?;
    std::fs::write(&path, json).map_err(|e| format!("config write: {}", e))?;
    Ok(())
}
