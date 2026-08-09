use serde::{Deserialize, Serialize};
use std::path::Path;
use std::time::Duration;

const HEALTH_TIMEOUT: Duration = Duration::from_secs(5);
const DEFAULT_GATEWAY_URL: &str = "http://127.0.0.1:8787";
const CONFIG_FILE_NAME: &str = "gateway.json";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GatewayStatus {
    pub ok: bool,
    pub url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct GatewayConfig {
    #[serde(default = "default_gateway_url")]
    pub url: String,
}

fn default_gateway_url() -> String {
    DEFAULT_GATEWAY_URL.to_string()
}

fn config_path(sessions_dir: &Path) -> std::path::PathBuf {
    sessions_dir
        .parent()
        .unwrap_or(sessions_dir)
        .join(CONFIG_FILE_NAME)
}

pub fn load_saved_gateway_url(sessions_dir: &Path) -> Option<String> {
    let path = config_path(sessions_dir);
    match std::fs::read_to_string(&path) {
        Ok(data) => serde_json::from_str::<GatewayConfig>(&data)
            .map(|c| c.url)
            .ok()
            .filter(|url| validate_gateway_url(url).is_ok()),
        Err(_) => None,
    }
}

pub fn load_gateway_url(sessions_dir: &Path) -> String {
    load_saved_gateway_url(sessions_dir).unwrap_or_else(default_gateway_url)
}

pub fn save_gateway_url(sessions_dir: &Path, url: &str) -> Result<(), String> {
    validate_gateway_url(url)?;
    let path = config_path(sessions_dir);
    let config = GatewayConfig {
        url: url.to_string(),
    };
    let json =
        serde_json::to_string_pretty(&config).map_err(|e| format!("config serialize: {}", e))?;
    std::fs::write(&path, json).map_err(|e| format!("config write: {}", e))?;
    Ok(())
}

/// Validate a gateway URL.
///
/// - HTTPS is always accepted.
/// - HTTP is accepted only for localhost / 127.0.0.1 / ::1 (local development).
/// - All other HTTP targets are rejected to prevent accidental production misconfiguration.
pub fn validate_gateway_url(url_str: &str) -> Result<(), String> {
    let parsed = url::Url::parse(url_str).map_err(|e| format!("invalid URL: {}", e))?;
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("gateway URL must not contain credentials".into());
    }
    if parsed.query().is_some() || parsed.fragment().is_some() {
        return Err("gateway URL must not contain a query or fragment".into());
    }
    if parsed.path() != "/" && !parsed.path().is_empty() {
        return Err("gateway URL must be an origin without a path".into());
    }
    match parsed.scheme() {
        "https" => Ok(()),
        "http" => {
            let host = parsed.host_str().unwrap_or("");
            let normalized = host.trim_start_matches('[').trim_end_matches(']');
            if normalized == "localhost" || normalized == "127.0.0.1" || normalized == "::1" {
                Ok(())
            } else {
                Err(
                    "HTTP gateway URLs are only allowed for localhost — use HTTPS for production"
                        .into(),
                )
            }
        }
        other => Err(format!("unsupported URL scheme: {} — use https://", other)),
    }
}

fn normalize_url(base: &str) -> String {
    let trimmed = base.trim_end_matches('/');
    format!("{}/v1/health", trimmed)
}

pub async fn check_gateway_health(base_url: &str) -> GatewayStatus {
    let health_url = normalize_url(base_url);
    let client = reqwest::Client::builder()
        .timeout(HEALTH_TIMEOUT)
        .build()
        .unwrap_or_default();

    match client.get(&health_url).send().await {
        Ok(resp) => {
            let status_code = resp.status();
            if status_code.is_success() {
                match resp.json::<serde_json::Value>().await {
                    Ok(body) => GatewayStatus {
                        ok: true,
                        url: base_url.to_string(),
                        provider: body
                            .get("provider")
                            .and_then(|v| v.as_str())
                            .map(String::from),
                        version: body
                            .get("version")
                            .and_then(|v| v.as_str())
                            .map(String::from),
                        error: None,
                    },
                    Err(e) => GatewayStatus {
                        ok: false,
                        url: base_url.to_string(),
                        provider: None,
                        version: None,
                        error: Some(format!("invalid health response: {}", e)),
                    },
                }
            } else {
                GatewayStatus {
                    ok: false,
                    url: base_url.to_string(),
                    provider: None,
                    version: None,
                    error: Some(format!("gateway returned HTTP {}", status_code)),
                }
            }
        }
        Err(e) => {
            let msg = if e.is_timeout() {
                format!(
                    "gateway at {} did not respond within {}s — is it running?",
                    base_url,
                    HEALTH_TIMEOUT.as_secs()
                )
            } else if e.is_connect() {
                format!(
                    "cannot connect to gateway at {} — start it with: python -m uvicorn memecho_gateway.main:app --port 8787",
                    base_url
                )
            } else {
                format!("gateway check failed: {}", e)
            };
            GatewayStatus {
                ok: false,
                url: base_url.to_string(),
                provider: None,
                version: None,
                error: Some(msg),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_gateway_url() {
        assert_eq!(default_gateway_url(), "http://127.0.0.1:8787");
    }

    #[test]
    fn test_normalize_url() {
        assert_eq!(
            normalize_url("http://127.0.0.1:8787"),
            "http://127.0.0.1:8787/v1/health"
        );
        assert_eq!(
            normalize_url("http://127.0.0.1:8787/"),
            "http://127.0.0.1:8787/v1/health"
        );
        assert_eq!(
            normalize_url("https://gateway.example.com"),
            "https://gateway.example.com/v1/health"
        );
    }

    #[test]
    fn test_config_roundtrip() {
        let dir = std::env::temp_dir().join(format!("memecho_gw_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let sessions_dir = dir.join("sessions");
        std::fs::create_dir_all(&sessions_dir).unwrap();

        // Default when no config file
        assert_eq!(load_saved_gateway_url(&sessions_dir), None);
        assert_eq!(load_gateway_url(&sessions_dir), DEFAULT_GATEWAY_URL);

        // Save and reload
        save_gateway_url(&sessions_dir, "https://gw.example.com").unwrap();
        let url = load_gateway_url(&sessions_dir);
        assert_eq!(url, "https://gw.example.com");

        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_config_default_on_corrupt() {
        let dir = std::env::temp_dir().join(format!("memecho_gw_bad_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let sessions_dir = dir.join("sessions");
        std::fs::create_dir_all(&sessions_dir).unwrap();

        // Write corrupt JSON
        let config_path = sessions_dir.parent().unwrap().join(CONFIG_FILE_NAME);
        std::fs::write(&config_path, "not json").unwrap();

        let url = load_gateway_url(&sessions_dir);
        assert_eq!(url, DEFAULT_GATEWAY_URL);

        std::fs::remove_dir_all(&dir).ok();
    }

    #[tokio::test]
    async fn test_check_gateway_health_refused() {
        // Use a port that's almost certainly not listening
        let status = check_gateway_health("http://127.0.0.1:19876").await;
        assert!(!status.ok);
        assert!(status.error.is_some());
        let err = status.error.unwrap();
        // Error should mention either the URL or that connection failed
        assert!(
            err.contains("19876") || err.contains("cannot connect") || err.contains("gateway"),
            "unexpected error: {}",
            err
        );
    }

    #[tokio::test]
    async fn test_check_gateway_health_mock() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};

        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/v1/health"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "status": "ok",
                "provider": "mock",
                "version": "0.1.0"
            })))
            .mount(&server)
            .await;

        let status = check_gateway_health(&server.uri()).await;
        assert!(status.ok);
        assert_eq!(status.provider.as_deref(), Some("mock"));
        assert_eq!(status.version.as_deref(), Some("0.1.0"));
        assert!(status.error.is_none());
    }

    #[tokio::test]
    async fn test_check_gateway_health_500() {
        use wiremock::matchers::{method, path};
        use wiremock::{Mock, MockServer, ResponseTemplate};

        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/v1/health"))
            .respond_with(ResponseTemplate::new(500).set_body_string("internal error"))
            .mount(&server)
            .await;

        let status = check_gateway_health(&server.uri()).await;
        assert!(!status.ok);
        assert!(status.error.as_deref().unwrap().contains("HTTP 500"));
    }

    // ── URL validation tests ────────────────────────────────────────────────

    #[test]
    fn test_validate_gateway_url_https_accepted() {
        assert!(validate_gateway_url("https://gateway.example.com").is_ok());
        assert!(validate_gateway_url("https://gw.example.com:8080").is_ok());
    }

    #[test]
    fn test_validate_gateway_url_localhost_http_accepted() {
        assert!(validate_gateway_url("http://localhost:8787").is_ok());
        assert!(validate_gateway_url("http://127.0.0.1:8787").is_ok());
        assert!(validate_gateway_url("http://[::1]:8787").is_ok());
    }

    #[test]
    fn test_validate_gateway_url_remote_http_rejected() {
        assert!(validate_gateway_url("http://example.com").is_err());
        assert!(validate_gateway_url("http://192.168.1.100:8787").is_err());
        assert!(validate_gateway_url("http://gateway.example.com").is_err());
    }

    #[test]
    fn test_validate_gateway_url_bad_scheme_rejected() {
        assert!(validate_gateway_url("ftp://example.com").is_err());
        assert!(validate_gateway_url("ws://localhost").is_err());
    }

    #[test]
    fn test_validate_gateway_url_bad_format_rejected() {
        assert!(validate_gateway_url("not a url").is_err());
        assert!(validate_gateway_url("").is_err());
    }

    #[test]
    fn test_validate_gateway_url_rejects_non_origin_parts() {
        assert!(validate_gateway_url("https://user:pass@gateway.example.com").is_err());
        assert!(validate_gateway_url("https://gateway.example.com/base").is_err());
        assert!(validate_gateway_url("https://gateway.example.com?token=secret").is_err());
        assert!(validate_gateway_url("https://gateway.example.com#fragment").is_err());
    }

    #[test]
    fn test_load_rejects_invalid_saved_url() {
        let dir = std::env::temp_dir().join(format!("memecho_gw_saved_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let sessions_dir = dir.join("sessions");
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let path = sessions_dir.parent().unwrap().join(CONFIG_FILE_NAME);
        std::fs::write(&path, r#"{"url":"http://remote.example.com"}"#).unwrap();

        assert_eq!(load_saved_gateway_url(&sessions_dir), None);
        assert_eq!(load_gateway_url(&sessions_dir), DEFAULT_GATEWAY_URL);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_save_rejects_remote_http() {
        let dir = std::env::temp_dir().join(format!("memecho_gw_val_{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let sessions_dir = dir.join("sessions");
        std::fs::create_dir_all(&sessions_dir).unwrap();

        // HTTPS works
        assert!(save_gateway_url(&sessions_dir, "https://gw.example.com").is_ok());

        // Localhost HTTP works
        assert!(save_gateway_url(&sessions_dir, "http://127.0.0.1:9000").is_ok());

        // Remote HTTP rejected
        assert!(save_gateway_url(&sessions_dir, "http://192.168.1.1:8787").is_err());

        // Invalid URL rejected
        assert!(save_gateway_url(&sessions_dir, "not-a-url").is_err());

        std::fs::remove_dir_all(&dir).ok();
    }
}
