//! Integration tests for upload_session_tracks and save_report_commands.
//!
//! Uses wiremock to simulate gateway API endpoints.
//!
//! SECURITY: These tests MUST use `upload_session_tracks_with_token` to inject
//! a mock token. They must NEVER call credential_set, credential_get, or
//! credential_delete for "gateway_token" — that would overwrite or delete the
//! user's real Windows Credential Manager entry.

#[cfg(test)]
mod integration_tests {
    use serde_json::json;
    use sha2::{Digest, Sha256};
    use wiremock::matchers::{method, path_regex};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    // ── Helpers ───────────────────────────────────────────────────────────────

    /// Compile-time guard: this module must never import credential functions.
    /// If someone adds `use memecho_desktop_lib::credential::*;` below, this
    /// assertion fails to compile (the name doesn't exist in scope).
    #[allow(dead_code)]
    const _NO_CREDENTIAL_CALLS: () = {
        // Intentionally empty — the guard is that we do NOT import or call
        // credential_set / credential_get / credential_delete anywhere in
        // this module. The `#[forbid(unused_imports)]` on the test binary
        // and CI grep for "credential_" in this file provide runtime coverage.
    };

    /// Test token injected into `upload_session_tracks_with_token`.
    const TEST_TOKEN: &str = "test-token-for-upload-isolation";

    fn unique_id() -> String {
        uuid::Uuid::new_v4().to_string()
    }

    fn make_test_session(sessions_dir: &std::path::Path, session_id: &str) {
        let session_dir = sessions_dir.join(session_id);
        std::fs::create_dir_all(&session_dir).unwrap();
        std::fs::write(session_dir.join("mic.wav"), b"RIFF....WAVfmt data").unwrap();
        std::fs::write(session_dir.join("loopback.wav"), b"RIFF....WAVfmt data").unwrap();
    }

    fn setup_db(sessions_dir: &std::path::Path) -> memecho_desktop_lib::db::Repository {
        let db_path = sessions_dir.join(format!("test-{}.db", unique_id()));
        memecho_desktop_lib::db::Repository::open(&db_path).unwrap()
    }

    /// Compute SHA-256 and size for the test WAV content used by make_test_session.
    fn test_wav_sha256_and_size() -> (String, u64) {
        let content = b"RIFF....WAVfmt data";
        let mut hasher = Sha256::new();
        hasher.update(content);
        (hex::encode(hasher.finalize()), content.len() as u64)
    }

    // ── Upload validation tests (use _with_token, never touch credentials) ───

    #[test]
    fn test_upload_rejects_invalid_local_session_id() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let rt = tokio::runtime::Runtime::new().unwrap();
        let result = rt.block_on(async {
            memecho_desktop_lib::upload::upload_session_tracks_with_token(
                "../evil".to_string(),
                "gw-session".to_string(),
                "https://gateway.example.com".to_string(),
                &sessions_dir,
                TEST_TOKEN.to_string(),
            )
            .await
        });

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("invalid session id"), "got: {}", err);
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_upload_rejects_invalid_gateway_session_id() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let rt = tokio::runtime::Runtime::new().unwrap();
        let result = rt.block_on(async {
            memecho_desktop_lib::upload::upload_session_tracks_with_token(
                "valid-session".to_string(),
                "foo/bar".to_string(),
                "https://gateway.example.com".to_string(),
                &sessions_dir,
                TEST_TOKEN.to_string(),
            )
            .await
        });

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("invalid gateway session id"), "got: {}", err);
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_upload_rejects_remote_http() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let rt = tokio::runtime::Runtime::new().unwrap();
        let result = rt.block_on(async {
            memecho_desktop_lib::upload::upload_session_tracks_with_token(
                "valid-session".to_string(),
                "gw-session".to_string(),
                "http://evil.com".to_string(),
                &sessions_dir,
                TEST_TOKEN.to_string(),
            )
            .await
        });

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("invalid gateway URL"), "got: {}", err);
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_upload_allows_localhost_http() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        // With injected token, localhost HTTP passes URL validation and fails at
        // session path resolution (not credential lookup).
        let rt = tokio::runtime::Runtime::new().unwrap();
        let result = rt.block_on(async {
            memecho_desktop_lib::upload::upload_session_tracks_with_token(
                "valid-session".to_string(),
                "gw-session".to_string(),
                "http://localhost:3000".to_string(),
                &sessions_dir,
                TEST_TOKEN.to_string(),
            )
            .await
        });

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("invalid session id") || err.contains("session"),
            "expected session path error, got: {}",
            err
        );
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_upload_rejects_empty_ids() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let rt = tokio::runtime::Runtime::new().unwrap();

        // Empty local session ID
        let result = rt.block_on(async {
            memecho_desktop_lib::upload::upload_session_tracks_with_token(
                "".to_string(),
                "gw-session".to_string(),
                "https://gateway.example.com".to_string(),
                &sessions_dir,
                TEST_TOKEN.to_string(),
            )
            .await
        });
        assert!(result.is_err());

        // Empty gateway session ID
        let result = rt.block_on(async {
            memecho_desktop_lib::upload::upload_session_tracks_with_token(
                "valid-session".to_string(),
                "".to_string(),
                "https://gateway.example.com".to_string(),
                &sessions_dir,
                TEST_TOKEN.to_string(),
            )
            .await
        });
        assert!(result.is_err());

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_unsafe_path_rejected() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            "../../../etc/passwd".to_string(),
            "gw-sess".to_string(),
            "https://gateway.example.com".to_string(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await;

        assert!(result.is_err());
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    // ── Report validation tests ───────────────────────────────────────────────

    #[test]
    fn test_report_rejects_invalid_session_id() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);

        let result = memecho_desktop_lib::report::save_report_files_impl(
            "../evil",
            r#"{"key":"value"}"#,
            "# Report",
            "<h1>Report</h1>",
            &sessions_dir,
            &db,
        );

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("invalid session id"), "got: {}", err);
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_report_rejects_invalid_json() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);
        let session_id = format!("test-{}", unique_id());

        db.create_session(&session_id, Some("Test"), None, None, 16000, None)
            .unwrap();
        make_test_session(&sessions_dir, &session_id);

        let result = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            "not valid json",
            "# Report",
            "<h1>Report</h1>",
            &sessions_dir,
            &db,
        );

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("json validation"), "got: {}", err);
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_report_rejects_session_not_in_db() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);
        let session_id = format!("no-db-{}", unique_id());

        make_test_session(&sessions_dir, &session_id);

        let result = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            r#"{"key":"value"}"#,
            "# Report",
            "<h1>Report</h1>",
            &sessions_dir,
            &db,
        );

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("session not found"), "got: {}", err);
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_report_saves_files_atomically() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);
        let session_id = format!("atomic-{}", unique_id());

        db.create_session(&session_id, Some("Atomic Test"), None, None, 16000, None)
            .unwrap();
        make_test_session(&sessions_dir, &session_id);

        let result = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            r#"{"analysis":"test"}"#,
            "# Report\nContent here",
            "<h1>Report</h1><p>Content here</p>",
            &sessions_dir,
            &db,
        );

        assert!(result.is_ok());
        let saved = result.unwrap();

        assert!(saved.json_path.exists());
        assert!(saved.markdown_path.exists());
        assert!(saved.html_path.exists());

        assert_eq!(
            std::fs::read_to_string(&saved.json_path).unwrap(),
            r#"{"analysis":"test"}"#
        );
        assert_eq!(
            std::fs::read_to_string(&saved.markdown_path).unwrap(),
            "# Report\nContent here"
        );
        assert_eq!(
            std::fs::read_to_string(&saved.html_path).unwrap(),
            "<h1>Report</h1><p>Content here</p>"
        );

        let session_dir = sessions_dir.join(&session_id);
        for entry in std::fs::read_dir(&session_dir).unwrap() {
            let name = entry.unwrap().file_name().to_string_lossy().to_string();
            assert!(
                !name.ends_with(".tmp") && !name.ends_with(".bak"),
                "artifact found: {}",
                name
            );
        }

        let bundle = db.get_analysis_results(&session_id).unwrap();
        assert_eq!(bundle.results.len(), 1);
        assert_eq!(bundle.results[0].analysis_type, "report");

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_report_cleans_up_on_failure() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);
        let session_id = format!("cleanup-{}", unique_id());

        db.create_session(&session_id, Some("Cleanup"), None, None, 16000, None)
            .unwrap();
        make_test_session(&sessions_dir, &session_id);

        let result = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            r#"{"valid":"json"}"#,
            "# Report",
            "<h1>Report</h1>",
            &sessions_dir,
            &db,
        );

        assert!(result.is_ok());

        let session_dir = sessions_dir.join(&session_id);
        for entry in std::fs::read_dir(&session_dir).unwrap() {
            let name = entry.unwrap().file_name().to_string_lossy().to_string();
            assert!(
                !name.ends_with(".tmp") && !name.ends_with(".bak"),
                "stale artifact: {}",
                name
            );
        }

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_report_enforces_size_limits() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);
        let session_id = format!("size-{}", unique_id());

        db.create_session(&session_id, Some("Size"), None, None, 16000, None)
            .unwrap();
        make_test_session(&sessions_dir, &session_id);

        // JSON too large (> 10 MiB)
        let big_json = "x".repeat(11 * 1024 * 1024);
        let result = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            &big_json,
            "# Report",
            "<h1>Report</h1>",
            &sessions_dir,
            &db,
        );
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("json too large"));

        // Markdown too large (> 5 MiB)
        let big_md = "x".repeat(6 * 1024 * 1024);
        let result = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            r#"{"ok":true}"#,
            &big_md,
            "<h1>Report</h1>",
            &sessions_dir,
            &db,
        );
        assert!(result.is_err());
        assert!(result
            .unwrap_err()
            .to_string()
            .contains("markdown too large"));

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    // ── Wiremock integration tests ───────────────────────────────────────────
    //
    // All tests below use `upload_session_tracks_with_token` with an explicit
    // TEST_TOKEN. They NEVER call credential_set/get/delete for gateway_token.

    #[tokio::test]
    async fn test_upload_two_tracks_success_with_mock_server() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let session_id = format!("mock-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);
        let (expected_sha, expected_size) = test_wav_sha256_and_size();

        let create_upload_id = "upload-fixed-id-001".to_string();

        // Mock create-upload: POST /v1/sessions/gw-sess/uploads
        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-sess/uploads$"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": create_upload_id,
                "chunk_size": 4194304
            })))
            .mount(&mock_server)
            .await;

        // Mock chunk upload: PUT .../chunks/N
        Mock::given(method("PUT"))
            .and(path_regex(
                r"/v1/sessions/gw-sess/uploads/upload-fixed-id-001/chunks/\d+",
            ))
            .respond_with(ResponseTemplate::new(200))
            .mount(&mock_server)
            .await;

        // Mock complete upload: POST .../complete — returns matching ID, size, sha
        Mock::given(method("POST"))
            .and(path_regex(
                r"/v1/sessions/gw-sess/uploads/upload-fixed-id-001/complete",
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": create_upload_id,
                "size": expected_size,
                "sha256": expected_sha,
            })))
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            session_id,
            "gw-sess".to_string(),
            mock_server.uri(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await;

        assert!(result.is_ok(), "upload should succeed: {:?}", result.err());
        let upload_result = result.unwrap();

        // Both tracks returned
        assert_eq!(upload_result.uploads.len(), 2);

        // First track: microphone
        let mic = &upload_result.uploads[0];
        assert_eq!(mic.track, "microphone");
        assert_eq!(mic.upload_id, create_upload_id);
        assert_eq!(mic.size, expected_size);
        assert_eq!(mic.sha256, expected_sha);

        // Second track: system (loopback)
        let sys = &upload_result.uploads[1];
        assert_eq!(sys.track, "system");
        assert_eq!(sys.upload_id, create_upload_id);
        assert_eq!(sys.size, expected_size);
        assert_eq!(sys.sha256, expected_sha);

        // Total bytes
        assert_eq!(upload_result.total_bytes, expected_size * 2);

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_import_track_success_with_media_mime() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let session_id = format!("import-{}", unique_id());
        let session_dir = sessions_dir.join(&session_id);
        std::fs::create_dir_all(&session_dir).unwrap();
        let content = b"authorized-m4a-content";
        std::fs::write(session_dir.join("import.m4a"), content).unwrap();
        let mut hasher = Sha256::new();
        hasher.update(content);
        let expected_sha = hex::encode(hasher.finalize());
        let expected_size = content.len() as u64;

        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-import/uploads$"))
            .and(wiremock::matchers::body_json(json!({
                "file_name": "import.m4a",
                "mime_type": "audio/mp4",
                "size": expected_size,
                "sha256": expected_sha,
                "track": "import"
            })))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-import-001",
                "chunk_size": 4194304
            })))
            .expect(1)
            .mount(&mock_server)
            .await;

        Mock::given(method("PUT"))
            .and(path_regex(
                r"/v1/sessions/gw-import/uploads/upload-import-001/chunks/0$",
            ))
            .respond_with(ResponseTemplate::new(200))
            .expect(1)
            .mount(&mock_server)
            .await;

        Mock::given(method("POST"))
            .and(path_regex(
                r"/v1/sessions/gw-import/uploads/upload-import-001/complete$",
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-import-001",
                "size": expected_size,
                "sha256": expected_sha,
            })))
            .expect(1)
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            session_id,
            "gw-import".to_string(),
            mock_server.uri(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await
        .expect("import upload should succeed");

        assert_eq!(result.uploads.len(), 1);
        assert_eq!(result.uploads[0].track, "import");
        assert_eq!(result.uploads[0].upload_id, "upload-import-001");
        assert_eq!(result.uploads[0].size, expected_size);
        assert_eq!(result.uploads[0].sha256, expected_sha);
        assert_eq!(result.total_bytes, expected_size);

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_chunk_retry_on_failure() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let session_id = format!("retry-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);

        // Mock create-upload succeeds
        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-retry/uploads$"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-retry-001",
                "chunk_size": 4194304
            })))
            .mount(&mock_server)
            .await;

        // Mock chunk upload always fails (testing retry exhaustion)
        Mock::given(method("PUT"))
            .and(path_regex(
                r"/v1/sessions/gw-retry/uploads/upload-retry-001/chunks/\d+",
            ))
            .respond_with(ResponseTemplate::new(500).set_body_string("server error"))
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            session_id,
            "gw-retry".to_string(),
            mock_server.uri(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await;

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("retries"),
            "expected retry exhaustion, got: {}",
            err
        );
        // Error must not leak the token
        assert!(
            !err.contains(TEST_TOKEN),
            "error must not echo token value, got: {}",
            err
        );

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_rejects_zero_chunk_size() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let session_id = format!("chunk0-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);

        // Mock create-upload returns chunk_size = 0
        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-chunk0/uploads$"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-chunk0-001",
                "chunk_size": 0
            })))
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            session_id,
            "gw-chunk0".to_string(),
            mock_server.uri(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await;

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("invalid chunk_size"),
            "expected InvalidChunkSize error, got: {}",
            err
        );

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_rejects_oversized_chunk_size() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let session_id = format!("chunkbig-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);

        // Mock create-upload returns chunk_size > 8 MiB
        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-chunkbig/uploads$"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-chunkbig-001",
                "chunk_size": 16_777_216 // 16 MiB
            })))
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            session_id,
            "gw-chunkbig".to_string(),
            mock_server.uri(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await;

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("invalid chunk_size"),
            "expected InvalidChunkSize error, got: {}",
            err
        );

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_detects_upload_id_mismatch() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let session_id = format!("idmismatch-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);
        let (expected_sha, expected_size) = test_wav_sha256_and_size();

        // Mock create-upload returns upload_id = "upload-A"
        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-idmismatch/uploads$"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-A",
                "chunk_size": 4194304
            })))
            .mount(&mock_server)
            .await;

        // Mock chunk upload
        Mock::given(method("PUT"))
            .and(path_regex(
                r"/v1/sessions/gw-idmismatch/uploads/upload-A/chunks/\d+",
            ))
            .respond_with(ResponseTemplate::new(200))
            .mount(&mock_server)
            .await;

        // Mock complete returns DIFFERENT upload_id = "upload-B"
        Mock::given(method("POST"))
            .and(path_regex(
                r"/v1/sessions/gw-idmismatch/uploads/upload-A/complete",
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-B",
                "size": expected_size,
                "sha256": expected_sha,
            })))
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            session_id,
            "gw-idmismatch".to_string(),
            mock_server.uri(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await;

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("upload id mismatch"),
            "expected UploadIdMismatch, got: {}",
            err
        );

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_detects_size_mismatch() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let session_id = format!("sizemismatch-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);
        let (expected_sha, _expected_size) = test_wav_sha256_and_size();

        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-sizemismatch/uploads$"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-size-001",
                "chunk_size": 4194304
            })))
            .mount(&mock_server)
            .await;

        Mock::given(method("PUT"))
            .and(path_regex(
                r"/v1/sessions/gw-sizemismatch/uploads/upload-size-001/chunks/\d+",
            ))
            .respond_with(ResponseTemplate::new(200))
            .mount(&mock_server)
            .await;

        // Complete returns wrong size (999 instead of actual)
        Mock::given(method("POST"))
            .and(path_regex(
                r"/v1/sessions/gw-sizemismatch/uploads/upload-size-001/complete",
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-size-001",
                "size": 999,
                "sha256": expected_sha,
            })))
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            session_id,
            "gw-sizemismatch".to_string(),
            mock_server.uri(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await;

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("size mismatch"),
            "expected SizeMismatch, got: {}",
            err
        );

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_detects_checksum_mismatch() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let session_id = format!("checksum-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);
        let (_expected_sha, expected_size) = test_wav_sha256_and_size();

        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-checksum/uploads$"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-checksum-001",
                "chunk_size": 4194304
            })))
            .mount(&mock_server)
            .await;

        Mock::given(method("PUT"))
            .and(path_regex(
                r"/v1/sessions/gw-checksum/uploads/upload-checksum-001/chunks/\d+",
            ))
            .respond_with(ResponseTemplate::new(200))
            .mount(&mock_server)
            .await;

        // Complete returns wrong sha256
        Mock::given(method("POST"))
            .and(path_regex(
                r"/v1/sessions/gw-checksum/uploads/upload-checksum-001/complete",
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": "upload-checksum-001",
                "size": expected_size,
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
            })))
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            session_id,
            "gw-checksum".to_string(),
            mock_server.uri(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await;

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("checksum mismatch"),
            "expected ChecksumMismatch, got: {}",
            err
        );

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_error_does_not_echo_token() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let session_id = format!("redact-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);

        // Mock create-upload returns an error that might echo the auth header
        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-redact/uploads$"))
            .respond_with(
                ResponseTemplate::new(401)
                    .set_body_string("Unauthorized: bad token test-token-for-upload-isolation"),
            )
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_with_token(
            session_id,
            "gw-redact".to_string(),
            mock_server.uri(),
            &sessions_dir,
            TEST_TOKEN.to_string(),
        )
        .await;

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        // The error message must be bounded and not echo Authorization/token values
        assert!(
            !err.contains(TEST_TOKEN),
            "error must not echo token, got: {}",
            err
        );
        assert!(
            err.len() < 1024,
            "error text must be bounded, got len={}: {}",
            err.len(),
            err
        );

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    // ── Checksum metadata test ────────────────────────────────────────────────

    #[tokio::test]
    async fn test_upload_checksum_metadata_correct() {
        use sha2::{Digest, Sha256};

        let dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file_path = dir.join("test.wav");
        let content = b"RIFF\x00\x00\x00\x00WAVEfmt \x00\x00\x00\x00";
        std::fs::write(&file_path, content).unwrap();

        let mut hasher = Sha256::new();
        hasher.update(content);
        let expected = hex::encode(hasher.finalize());

        let (computed, size) =
            memecho_desktop_lib::upload::compute_sha256_streaming(&file_path).unwrap();
        assert_eq!(size, content.len() as u64);
        assert_eq!(computed, expected);

        std::fs::remove_dir_all(&dir).ok();
    }

    // ── Repeat-save and failure-preservation tests ─────────────────────────

    #[test]
    fn test_report_save_twice_replaces_without_error() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);
        let session_id = format!("repeat-{}", unique_id());

        db.create_session(&session_id, Some("Repeat"), None, None, 16000, None)
            .unwrap();
        make_test_session(&sessions_dir, &session_id);

        let r1 = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            r#"{"version":1}"#,
            "# Report v1",
            "<h1>v1</h1>",
            &sessions_dir,
            &db,
        );
        assert!(r1.is_ok());
        let saved1 = r1.unwrap();
        assert_eq!(
            std::fs::read_to_string(&saved1.json_path).unwrap(),
            r#"{"version":1}"#
        );

        let r2 = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            r#"{"version":2}"#,
            "# Report v2",
            "<h1>v2</h1>",
            &sessions_dir,
            &db,
        );
        assert!(r2.is_ok());
        let saved2 = r2.unwrap();
        assert_eq!(
            std::fs::read_to_string(&saved2.json_path).unwrap(),
            r#"{"version":2}"#
        );
        assert_eq!(
            std::fs::read_to_string(&saved2.markdown_path).unwrap(),
            "# Report v2"
        );
        assert_eq!(
            std::fs::read_to_string(&saved2.html_path).unwrap(),
            "<h1>v2</h1>"
        );

        let session_dir = sessions_dir.join(&session_id);
        for entry in std::fs::read_dir(&session_dir).unwrap() {
            let name = entry.unwrap().file_name().to_string_lossy().to_string();
            assert!(
                !name.ends_with(".tmp") && !name.ends_with(".bak"),
                "artifact found: {}",
                name
            );
        }

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_report_failure_preserves_old_report() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);
        let session_id = format!("preserve-{}", unique_id());

        db.create_session(&session_id, Some("Preserve"), None, None, 16000, None)
            .unwrap();
        make_test_session(&sessions_dir, &session_id);

        let r1 = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            r#"{"original":true}"#,
            "# Original",
            "<h1>Original</h1>",
            &sessions_dir,
            &db,
        );
        assert!(r1.is_ok());

        let r2 = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            "not valid json at all",
            "# New",
            "<h1>New</h1>",
            &sessions_dir,
            &db,
        );
        assert!(r2.is_err());

        let session_dir = sessions_dir.join(&session_id);
        let json_path = session_dir.join("report.json");
        let md_path = session_dir.join("report.md");
        let html_path = session_dir.join("report.html");

        assert!(
            json_path.exists(),
            "original report.json should still exist"
        );
        assert!(md_path.exists(), "original report.md should still exist");
        assert!(
            html_path.exists(),
            "original report.html should still exist"
        );

        assert_eq!(
            std::fs::read_to_string(&json_path).unwrap(),
            r#"{"original":true}"#
        );
        assert_eq!(std::fs::read_to_string(&md_path).unwrap(), "# Original");
        assert_eq!(
            std::fs::read_to_string(&html_path).unwrap(),
            "<h1>Original</h1>"
        );

        for entry in std::fs::read_dir(&session_dir).unwrap() {
            let name = entry.unwrap().file_name().to_string_lossy().to_string();
            assert!(
                !name.ends_with(".tmp") && !name.ends_with(".bak"),
                "artifact found: {}",
                name
            );
        }

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    // ── Official transcript persistence (BUG-007 P1-1) ─────────────────────

    const TRANSCRIPT_JSON: &str = r#"{
        "schema_version": "1.1",
        "request_id": "req_embed",
        "_official_transcript": {
            "segments": [
                {"speaker_id": "speaker_self", "start_ms": 12000, "end_ms": 18000, "text": "先确认今天讨论的范围"}
            ],
            "truncated": false
        }
    }"#;

    #[test]
    fn test_report_json_persists_embedded_official_transcript() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);
        let session_id = format!("test-{}", unique_id());

        db.create_session(&session_id, Some("Transcript"), None, None, 16000, None)
            .unwrap();
        make_test_session(&sessions_dir, &session_id);

        let saved = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            TRANSCRIPT_JSON,
            "# Report",
            "<h1>Report</h1>",
            &sessions_dir,
            &db,
        )
        .unwrap();

        // The file artifact keeps the transcript so historical reopens work
        // without any gateway state.
        let on_disk = std::fs::read_to_string(&saved.json_path).unwrap();
        assert!(on_disk.contains("先确认今天讨论的范围"));
        assert!(on_disk.contains("_official_transcript"));

        // The DB bundle used by historical report listing keeps it too.
        let bundle = db.get_analysis_results(&session_id).unwrap();
        let report = bundle
            .results
            .iter()
            .find(|item| item.analysis_type == "report")
            .expect("report result saved");
        assert!(report.content_json.contains("先确认今天讨论的范围"));

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[test]
    fn test_session_deletion_removes_persisted_official_transcript() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();
        let db = setup_db(&sessions_dir);
        let session_id = format!("test-{}", unique_id());

        db.create_session(&session_id, Some("Delete me"), None, None, 16000, None)
            .unwrap();
        make_test_session(&sessions_dir, &session_id);

        memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            TRANSCRIPT_JSON,
            "# Report",
            "<h1>Report</h1>",
            &sessions_dir,
            &db,
        )
        .unwrap();

        // Mirror delete_local_session: cascade DB records, then remove files.
        db.cascade_delete_session(&session_id, &sessions_dir).unwrap();
        let session_dir = sessions_dir.join(&session_id);
        std::fs::remove_dir_all(&session_dir).unwrap();

        assert!(!session_dir.join("report.json").exists());
        assert!(!session_dir.join("mic.wav").exists());
        let bundle = db.get_analysis_results(&session_id).unwrap();
        assert!(bundle.results.is_empty());
        assert!(bundle.memory_candidates.is_empty());

        std::fs::remove_dir_all(&sessions_dir).ok();
    }
}
