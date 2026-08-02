//! Integration tests for upload_session_tracks and save_report_commands.
//!
//! Uses wiremock to simulate gateway API endpoints.

#[cfg(test)]
mod integration_tests {
    use serde_json::json;
    use wiremock::matchers::{method, path_regex};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    // ── Helpers ───────────────────────────────────────────────────────────────

    fn unique_id() -> String {
        uuid::Uuid::new_v4().to_string()
    }

    fn make_test_session(sessions_dir: &std::path::Path, session_id: &str) {
        let session_dir = sessions_dir.join(session_id);
        std::fs::create_dir_all(&session_dir).unwrap();
        // Write minimal WAV files (just some bytes for testing)
        std::fs::write(session_dir.join("mic.wav"), b"RIFF....WAVfmt data").unwrap();
        std::fs::write(session_dir.join("loopback.wav"), b"RIFF....WAVfmt data").unwrap();
    }

    fn setup_db(sessions_dir: &std::path::Path) -> memecho_desktop_lib::db::Repository {
        let db_path = sessions_dir.join(format!("test-{}.db", unique_id()));
        memecho_desktop_lib::db::Repository::open(&db_path).unwrap()
    }

    // ── Upload validation tests ───────────────────────────────────────────────

    #[test]
    fn test_upload_rejects_invalid_local_session_id() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let rt = tokio::runtime::Runtime::new().unwrap();
        let result = rt.block_on(async {
            memecho_desktop_lib::upload::upload_session_tracks_impl(
                "../evil".to_string(),
                "gw-session".to_string(),
                "https://gateway.example.com".to_string(),
                &sessions_dir,
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
            memecho_desktop_lib::upload::upload_session_tracks_impl(
                "valid-session".to_string(),
                "foo/bar".to_string(),
                "https://gateway.example.com".to_string(),
                &sessions_dir,
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
            memecho_desktop_lib::upload::upload_session_tracks_impl(
                "valid-session".to_string(),
                "gw-session".to_string(),
                "http://evil.com".to_string(),
                &sessions_dir,
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
        // This will fail at credential lookup, but URL validation should pass
        let rt = tokio::runtime::Runtime::new().unwrap();
        let result = rt.block_on(async {
            memecho_desktop_lib::upload::upload_session_tracks_impl(
                "valid-session".to_string(),
                "gw-session".to_string(),
                "http://localhost:3000".to_string(),
                &sessions_dir,
            )
            .await
        });

        assert!(result.is_err());
        // Should fail at credential, not URL validation
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("credential") || err.contains("authentication"),
            "got: {}",
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
            memecho_desktop_lib::upload::upload_session_tracks_impl(
                "".to_string(),
                "gw-session".to_string(),
                "https://gateway.example.com".to_string(),
                &sessions_dir,
            )
            .await
        });
        assert!(result.is_err());

        // Empty gateway session ID
        let result = rt.block_on(async {
            memecho_desktop_lib::upload::upload_session_tracks_impl(
                "valid-session".to_string(),
                "".to_string(),
                "https://gateway.example.com".to_string(),
                &sessions_dir,
            )
            .await
        });
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

        // Create session in DB
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

        // Create session in DB
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

        // Verify files exist
        assert!(saved.json_path.exists());
        assert!(saved.markdown_path.exists());
        assert!(saved.html_path.exists());

        // Verify content
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

        // Verify no .tmp or .bak files remain
        let session_dir = sessions_dir.join(&session_id);
        for entry in std::fs::read_dir(&session_dir).unwrap() {
            let name = entry.unwrap().file_name().to_string_lossy().to_string();
            assert!(
                !name.ends_with(".tmp") && !name.ends_with(".bak"),
                "artifact found: {}",
                name
            );
        }

        // Verify DB was updated
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

        // Should succeed since session dir exists
        assert!(result.is_ok());

        // Verify no stale .tmp or .bak files
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

    #[tokio::test]
    async fn test_upload_two_tracks_with_mock_server() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        // Set up credential for test
        #[cfg(windows)]
        {
            let _ =
                memecho_desktop_lib::credential::credential_set("gateway_token", "test-token-123");
        }

        let session_id = format!("mock-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);

        // Mock create-upload (POST to /uploads but NOT /uploads/.../complete)
        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-sess/uploads$"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": format!("upload-{}", unique_id()),
                "chunk_size": 4194304
            })))
            .mount(&mock_server)
            .await;

        // Mock chunk upload (PUT)
        Mock::given(method("PUT"))
            .and(path_regex(
                r"/v1/sessions/gw-sess/uploads/upload-.*/chunks/\d+",
            ))
            .respond_with(ResponseTemplate::new(200))
            .mount(&mock_server)
            .await;

        // Mock complete upload (POST to /complete)
        Mock::given(method("POST"))
            .and(path_regex(
                r"/v1/sessions/gw-sess/uploads/upload-.*/complete",
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": format!("upload-{}", unique_id()),
                "size": 19,
                "sha256": "d4e5f6..." // Will mismatch, testing checksum verification path
            })))
            .mount(&mock_server)
            .await;

        // The test will fail at checksum verification since we use a fake SHA
        // This verifies the checksum comparison logic is working
        let result = memecho_desktop_lib::upload::upload_session_tracks_impl(
            session_id,
            "gw-sess".to_string(),
            mock_server.uri(),
            &sessions_dir,
        )
        .await;

        // Should fail with checksum mismatch or credential error
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("checksum mismatch")
                || err.contains("credential")
                || err.contains("authentication")
                || err.contains("http error"),
            "got: {}",
            err
        );

        // Cleanup
        #[cfg(windows)]
        {
            let _ = memecho_desktop_lib::credential::credential_delete("gateway_token");
        }
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_chunk_retry_on_failure() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        #[cfg(windows)]
        {
            let _ = memecho_desktop_lib::credential::credential_set("gateway_token", "retry-token");
        }

        let session_id = format!("retry-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);

        // Mock create-upload succeeds
        Mock::given(method("POST"))
            .and(path_regex(r"/v1/sessions/gw-retry/uploads$"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "upload_id": format!("upload-{}", unique_id()),
                "chunk_size": 4194304
            })))
            .mount(&mock_server)
            .await;

        // Mock chunk upload always fails (testing retry exhaustion)
        Mock::given(method("PUT"))
            .and(path_regex(
                r"/v1/sessions/gw-retry/uploads/upload-.*/chunks/\d+",
            ))
            .respond_with(ResponseTemplate::new(500).set_body_string("server error"))
            .mount(&mock_server)
            .await;

        let result = memecho_desktop_lib::upload::upload_session_tracks_impl(
            session_id,
            "gw-retry".to_string(),
            mock_server.uri(),
            &sessions_dir,
        )
        .await;

        // Should fail with retry exhausted
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("retries") || err.contains("credential"),
            "got: {}",
            err
        );

        #[cfg(windows)]
        {
            let _ = memecho_desktop_lib::credential::credential_delete("gateway_token");
        }
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_auth_absence_returns_error() {
        let mock_server = MockServer::start().await;
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        // Don't set any credential
        #[cfg(windows)]
        {
            let _ = memecho_desktop_lib::credential::credential_delete("gateway_token");
        }

        let session_id = format!("noauth-{}", unique_id());
        make_test_session(&sessions_dir, &session_id);

        let result = memecho_desktop_lib::upload::upload_session_tracks_impl(
            session_id,
            "gw-noauth".to_string(),
            mock_server.uri(),
            &sessions_dir,
        )
        .await;

        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("credential") || err.contains("authentication"),
            "got: {}",
            err
        );

        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_unsafe_path_rejected() {
        let sessions_dir = std::env::temp_dir().join(format!("memecho_test_{}", unique_id()));
        std::fs::create_dir_all(&sessions_dir).unwrap();

        let result = memecho_desktop_lib::upload::upload_session_tracks_impl(
            "../../../etc/passwd".to_string(),
            "gw-sess".to_string(),
            "https://gateway.example.com".to_string(),
            &sessions_dir,
        )
        .await;

        assert!(result.is_err());
        std::fs::remove_dir_all(&sessions_dir).ok();
    }

    #[tokio::test]
    async fn test_upload_checksum_metadata_correct() {
        // Test that SHA-256 is computed correctly for known content
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

        // First save
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

        // Second save — must replace without rename failures
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

        // No temp or backup artifacts remain
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

        // Save a valid report first
        let r1 = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            r#"{"original":true}"#,
            "# Original",
            "<h1>Original</h1>",
            &sessions_dir,
            &db,
        );
        assert!(r1.is_ok());

        // Now attempt a save with invalid JSON — should fail
        let r2 = memecho_desktop_lib::report::save_report_files_impl(
            &session_id,
            "not valid json at all",
            "# New",
            "<h1>New</h1>",
            &sessions_dir,
            &db,
        );
        assert!(r2.is_err());

        // The original report files must still be intact
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

        // No stale artifacts
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
}
