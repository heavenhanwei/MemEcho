use reqwest::Client;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::Read;
use std::path::Path;
use std::time::Duration;
use thiserror::Error;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(300);
const MAX_RETRIES: u32 = 3;
const CHUNK_SIZE: usize = 4 * 1024 * 1024; // 4 MiB default
const MAX_CHUNK_SIZE: usize = 8 * 1024 * 1024; // 8 MiB hard cap for server-provided chunk_size
const MAX_GATEWAY_ERROR_BODY: usize = 512;

#[derive(Debug, Error)]
pub enum UploadError {
    #[error("invalid session id")]
    InvalidSessionId,
    #[error("invalid gateway session id")]
    InvalidGatewaySessionId,
    #[error("invalid gateway URL: {0}")]
    InvalidGatewayUrl(String),
    #[error("credential error: {0}")]
    Credential(String),
    #[error("audio file not found: {0}")]
    FileNotFound(String),
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),
    #[error("http error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("gateway error {status}: {body}")]
    GatewayError { status: u16, body: String },
    #[error("checksum mismatch for {track}: expected {expected}, got {actual}")]
    ChecksumMismatch {
        track: String,
        expected: String,
        actual: String,
    },
    #[error("upload id mismatch for {track}: expected {expected}, got {actual}")]
    UploadIdMismatch {
        track: String,
        expected: String,
        actual: String,
    },
    #[error("size mismatch for {track}: expected {expected}, got {actual}")]
    SizeMismatch {
        track: String,
        expected: u64,
        actual: u64,
    },
    #[error("invalid chunk_size from server: {0} (must be 1..={max})", max = MAX_CHUNK_SIZE)]
    InvalidChunkSize(usize),
    #[error("upload failed after {attempts} retries: {reason}")]
    RetryExhausted { attempts: u32, reason: String },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TrackUploadResult {
    pub track: String,
    pub upload_id: String,
    pub size: u64,
    pub sha256: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UploadSessionTracksResult {
    pub uploads: Vec<TrackUploadResult>,
    pub total_bytes: u64,
}

#[derive(Debug, Deserialize)]
struct CreateUploadResponse {
    upload_id: String,
    chunk_size: usize,
}

#[derive(Debug, Deserialize)]
struct CompleteUploadResponse {
    upload_id: String,
    size: u64,
    sha256: String,
}

/// Validate a session ID (alphanumeric + hyphens only).
fn validate_id(id: &str) -> Result<(), UploadError> {
    if id.is_empty() || !id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
        return Err(UploadError::InvalidSessionId);
    }
    Ok(())
}

/// Validate a gateway session ID (same rules).
fn validate_gateway_id(id: &str) -> Result<(), UploadError> {
    if id.is_empty() || !id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
        return Err(UploadError::InvalidGatewaySessionId);
    }
    Ok(())
}

/// Validate gateway URL: must be https or localhost http for dev.
fn validate_gateway_url(url_str: &str) -> Result<url::Url, UploadError> {
    let parsed =
        url::Url::parse(url_str).map_err(|e| UploadError::InvalidGatewayUrl(e.to_string()))?;
    match parsed.scheme() {
        "https" => Ok(parsed),
        "http" => {
            let host = parsed.host_str().unwrap_or("");
            // Normalize IPv6 brackets: [::1] -> ::1
            let normalized = host.trim_start_matches('[').trim_end_matches(']');
            if normalized == "localhost" || normalized == "127.0.0.1" || normalized == "::1" {
                Ok(parsed)
            } else {
                Err(UploadError::InvalidGatewayUrl(
                    "http only allowed for localhost".into(),
                ))
            }
        }
        other => Err(UploadError::InvalidGatewayUrl(format!(
            "unsupported scheme: {}",
            other
        ))),
    }
}

/// Truncate and sanitize a gateway error body to avoid leaking huge or sensitive content.
fn sanitize_error_body(body: &str) -> String {
    let truncated: String = body.chars().take(MAX_GATEWAY_ERROR_BODY).collect();
    if truncated.len() < body.len() {
        format!("{}…(truncated)", truncated)
    } else {
        truncated
    }
}

/// Strip a known token value from an error body to prevent leaking credentials.
fn strip_token(body: &str, token: &str) -> String {
    if token.is_empty() || !body.contains(token) {
        return body.to_string();
    }
    body.replace(token, "[REDACTED]")
}

/// Compute SHA-256 of a file by streaming it in chunks.
pub fn compute_sha256_streaming(path: &Path) -> Result<(String, u64), UploadError> {
    let mut file = std::fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; CHUNK_SIZE];
    let mut total: u64 = 0;
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        total += n as u64;
    }
    Ok((hex::encode(hasher.finalize()), total))
}

/// Upload a single track with retry support.
async fn upload_track(
    client: &Client,
    base_url: &url::Url,
    gateway_session_id: &str,
    token: &str,
    track_name: &str,
    file_path: &Path,
) -> Result<TrackUploadResult, UploadError> {
    let (sha256, file_size) = compute_sha256_streaming(file_path)?;

    // Create upload
    let file_name = file_path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let create_url = format!(
        "{}/v1/sessions/{}/uploads",
        base_url.as_str().trim_end_matches('/'),
        gateway_session_id
    );

    let create_resp = client
        .post(&create_url)
        .bearer_auth(token)
        .json(&serde_json::json!({
            "track": track_name,
            "file_name": file_name,
            "mime_type": "audio/wav",
            "size": file_size,
            "sha256": sha256,
        }))
        .send()
        .await?;

    if !create_resp.status().is_success() {
        let status = create_resp.status().as_u16();
        let body = create_resp.text().await.unwrap_or_default();
        return Err(UploadError::GatewayError {
            status,
            body: sanitize_error_body(&strip_token(&body, token)),
        });
    }

    let create_data: CreateUploadResponse = create_resp.json().await?;
    let upload_id = create_data.upload_id;
    if create_data.chunk_size == 0 || create_data.chunk_size > MAX_CHUNK_SIZE {
        return Err(UploadError::InvalidChunkSize(create_data.chunk_size));
    }
    let chunk_size = create_data.chunk_size;

    // Upload chunks with retry
    let mut file = std::fs::File::open(file_path)?;
    let mut chunk_index: u32 = 0;
    let mut buf = vec![0u8; chunk_size];

    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }

        let chunk_url = format!(
            "{}/v1/sessions/{}/uploads/{}/chunks/{}",
            base_url.as_str().trim_end_matches('/'),
            gateway_session_id,
            upload_id,
            chunk_index
        );

        let mut last_err = String::new();
        let mut success = false;
        for attempt in 0..MAX_RETRIES {
            match client
                .put(&chunk_url)
                .bearer_auth(token)
                .header("Content-Type", "application/octet-stream")
                .body(buf[..n].to_vec())
                .send()
                .await
            {
                Ok(resp) if resp.status().is_success() => {
                    success = true;
                    break;
                }
                Ok(resp) => {
                    last_err = format!("status {}", resp.status());
                }
                Err(e) => {
                    last_err = e.to_string();
                }
            }
            // Backoff before retry
            if attempt + 1 < MAX_RETRIES {
                tokio::time::sleep(Duration::from_millis(100 * (attempt as u64 + 1))).await;
            }
        }
        if !success {
            return Err(UploadError::RetryExhausted {
                attempts: MAX_RETRIES,
                reason: format!("chunk {} failed: {}", chunk_index, last_err),
            });
        }

        chunk_index += 1;
    }

    // Complete upload
    let complete_url = format!(
        "{}/v1/sessions/{}/uploads/{}/complete",
        base_url.as_str().trim_end_matches('/'),
        gateway_session_id,
        upload_id
    );

    let complete_resp = client
        .post(&complete_url)
        .bearer_auth(token)
        .json(&serde_json::json!({
            "upload_id": upload_id,
            "sha256": sha256,
        }))
        .send()
        .await?;

    if !complete_resp.status().is_success() {
        let status = complete_resp.status().as_u16();
        let body = complete_resp.text().await.unwrap_or_default();
        return Err(UploadError::GatewayError {
            status,
            body: sanitize_error_body(&strip_token(&body, token)),
        });
    }

    let complete_data: CompleteUploadResponse = complete_resp.json().await?;

    // Verify upload_id roundtrip
    if complete_data.upload_id != upload_id {
        return Err(UploadError::UploadIdMismatch {
            track: track_name.to_string(),
            expected: upload_id,
            actual: complete_data.upload_id,
        });
    }

    // Verify size matches local file
    if complete_data.size != file_size {
        return Err(UploadError::SizeMismatch {
            track: track_name.to_string(),
            expected: file_size,
            actual: complete_data.size,
        });
    }

    // Verify checksum
    if complete_data.sha256 != sha256 {
        return Err(UploadError::ChecksumMismatch {
            track: track_name.to_string(),
            expected: sha256,
            actual: complete_data.sha256,
        });
    }

    Ok(TrackUploadResult {
        track: track_name.to_string(),
        upload_id: complete_data.upload_id,
        size: complete_data.size,
        sha256: complete_data.sha256,
    })
}

/// Testable upload entry: upload both mic and loopback tracks with an explicit token.
///
/// This function accepts the gateway token directly so integration tests can
/// inject a mock token without touching Windows Credential Manager.
pub async fn upload_session_tracks_with_token(
    local_session_id: String,
    gateway_session_id: String,
    gateway_base_url: String,
    sessions_dir: &Path,
    token: String,
) -> Result<UploadSessionTracksResult, UploadError> {
    // Validate IDs
    validate_id(&local_session_id)?;
    validate_gateway_id(&gateway_session_id)?;

    // Validate URL
    let base_url = validate_gateway_url(&gateway_base_url)?;

    // Locate session directory and validate path
    let session_dir = crate::paths::validate_session_path(&local_session_id, sessions_dir)
        .map_err(|_| UploadError::InvalidSessionId)?;

    // Locate WAV files
    let mic_path = crate::paths::mic_wav_path(&session_dir);
    let loopback_path = crate::paths::loopback_wav_path(&session_dir);

    if !mic_path.exists() {
        return Err(UploadError::FileNotFound("mic.wav not found".into()));
    }
    if !loopback_path.exists() {
        return Err(UploadError::FileNotFound("loopback.wav not found".into()));
    }

    let client = Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .build()?;

    // Upload both tracks
    let mic_result = upload_track(
        &client,
        &base_url,
        &gateway_session_id,
        &token,
        "microphone",
        &mic_path,
    )
    .await?;
    let loopback_result = upload_track(
        &client,
        &base_url,
        &gateway_session_id,
        &token,
        "system",
        &loopback_path,
    )
    .await?;

    let total_bytes = mic_result.size + loopback_result.size;

    Ok(UploadSessionTracksResult {
        uploads: vec![mic_result, loopback_result],
        total_bytes,
    })
}

/// Production entry point: upload both mic and loopback tracks for a session.
///
/// Reads the gateway token from Windows Credential Manager, then delegates to
/// `upload_session_tracks_with_token`.
pub async fn upload_session_tracks_impl(
    local_session_id: String,
    gateway_session_id: String,
    gateway_base_url: String,
    sessions_dir: &Path,
) -> Result<UploadSessionTracksResult, UploadError> {
    let token = crate::credential::credential_get("gateway_token")
        .map_err(|e| UploadError::Credential(format!("failed to read gateway_token: {}", e)))?;

    upload_session_tracks_with_token(
        local_session_id,
        gateway_session_id,
        gateway_base_url,
        sessions_dir,
        token,
    )
    .await
    .map_err(sanitize_upload_error)
}

/// Sanitize upload errors to strip any token values that may have leaked
/// into error messages (e.g., from reqwest debug formatting).
fn sanitize_upload_error(err: UploadError) -> UploadError {
    let msg = err.to_string();
    // Strip any bearer token or Authorization header values
    let sanitized = sanitize_error_body(&msg);
    if sanitized != msg {
        UploadError::Credential("authentication error (details redacted)".into())
    } else {
        err
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_validate_id_valid() {
        assert!(validate_id("abc-123").is_ok());
        assert!(validate_id("550e8400-e29b-41d4-a716-446655440000").is_ok());
    }

    #[test]
    fn test_validate_id_invalid() {
        assert!(validate_id("").is_err());
        assert!(validate_id("../evil").is_err());
        assert!(validate_id("foo/bar").is_err());
        assert!(validate_id("hello world").is_err());
    }

    #[test]
    fn test_validate_gateway_id() {
        assert!(validate_gateway_id("gw-123").is_ok());
        assert!(validate_gateway_id("").is_err());
        assert!(validate_gateway_id("../evil").is_err());
    }

    #[test]
    fn test_validate_gateway_url_https() {
        assert!(validate_gateway_url("https://gateway.example.com").is_ok());
        assert!(validate_gateway_url("https://gateway.example.com:8080").is_ok());
    }

    #[test]
    fn test_validate_gateway_url_localhost_http() {
        assert!(validate_gateway_url("http://localhost:3000").is_ok());
        assert!(validate_gateway_url("http://127.0.0.1:3000").is_ok());
        assert!(validate_gateway_url("http://[::1]:3000").is_ok());
    }

    #[test]
    fn test_validate_gateway_url_rejects_remote_http() {
        assert!(validate_gateway_url("http://example.com").is_err());
        assert!(validate_gateway_url("http://192.168.1.1:3000").is_err());
    }

    #[test]
    fn test_validate_gateway_url_rejects_other_schemes() {
        assert!(validate_gateway_url("ftp://example.com").is_err());
        assert!(validate_gateway_url("ws://localhost").is_err());
    }

    #[test]
    fn test_compute_sha256_streaming() {
        let dir = std::env::temp_dir().join("memecho_sha_test");
        std::fs::create_dir_all(&dir).unwrap();
        let file_path = dir.join("test.bin");
        std::fs::write(&file_path, b"hello world").unwrap();
        let (hash, size) = compute_sha256_streaming(&file_path).unwrap();
        assert_eq!(size, 11);
        // SHA-256 of "hello world"
        assert_eq!(
            hash,
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_compute_sha256_empty_file() {
        let dir = std::env::temp_dir().join("memecho_sha_empty");
        std::fs::create_dir_all(&dir).unwrap();
        let file_path = dir.join("empty.bin");
        std::fs::write(&file_path, b"").unwrap();
        let (hash, size) = compute_sha256_streaming(&file_path).unwrap();
        assert_eq!(size, 0);
        assert_eq!(
            hash,
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn test_sanitize_error_body_short() {
        let body = "not found";
        assert_eq!(sanitize_error_body(body), "not found");
    }

    #[test]
    fn test_sanitize_error_body_truncates_large() {
        let body = "x".repeat(1000);
        let sanitized = sanitize_error_body(&body);
        assert!(sanitized.len() < 600);
        assert!(sanitized.ends_with("…(truncated)"));
    }

    #[test]
    fn test_chunk_size_capped_to_max() {
        // Verify constants are consistent
        assert_eq!(MAX_CHUNK_SIZE, 8 * 1024 * 1024);
        assert!(CHUNK_SIZE <= MAX_CHUNK_SIZE);
    }

    #[test]
    fn test_strip_token_redacts_value() {
        let body = "Unauthorized: bad token my-secret-token-123";
        let stripped = strip_token(body, "my-secret-token-123");
        assert_eq!(stripped, "Unauthorized: bad token [REDACTED]");
        assert!(!stripped.contains("my-secret-token-123"));
    }

    #[test]
    fn test_strip_token_noop_when_absent() {
        let body = "some error message";
        let stripped = strip_token(body, "not-present");
        assert_eq!(stripped, "some error message");
    }

    #[test]
    fn test_strip_token_noop_when_empty() {
        let body = "some error message";
        let stripped = strip_token(body, "");
        assert_eq!(stripped, "some error message");
    }

    #[test]
    fn test_strip_token_multiple_occurrences() {
        let body = "token abc and abc again";
        let stripped = strip_token(body, "abc");
        assert_eq!(stripped, "token [REDACTED] and [REDACTED] again");
    }

    #[test]
    fn test_invalid_chunk_size_error_message() {
        let err = UploadError::InvalidChunkSize(0);
        assert!(err.to_string().contains("invalid chunk_size"));
        assert!(err.to_string().contains("must be 1..="));

        let err = UploadError::InvalidChunkSize(16 * 1024 * 1024);
        assert!(err.to_string().contains("16777216"));
    }
}
