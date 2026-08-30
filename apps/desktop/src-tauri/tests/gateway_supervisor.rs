//! Integration tests for the Gateway Supervisor lifecycle and handshake.
//!
//! These tests spawn the injectable `memecho-gateway-testsidecar` binary as a
//! stand-in for the real Python gateway, which cannot yet be packaged as a
//! sidecar executable (documented packaging blocker). They cover:
//!
//! - random loopback port selection and fixed-port dev mode;
//! - startup handshake success, timeout, version/protocol mismatch;
//! - early-exit cleanup and exit cleanup on shutdown;
//! - external (dev) mode against a mock gateway;
//! - the no-token-in-URL rule (the test sidecar requires the token as an
//!   `Authorization: Bearer` header and would otherwise answer 401).

use memecho_desktop_lib::gateway_supervisor::{
    GatewaySupervisor, SupervisorConfig, SupervisorError, SupervisorMode,
};
use std::path::PathBuf;
use std::time::Duration;

const TEST_VERSION: &str = "9.9.9-supervisor-test";

fn sidecar_config() -> SupervisorConfig {
    let mut config = SupervisorConfig::for_sidecar(PathBuf::from(env!(
        "CARGO_BIN_EXE_memecho-gateway-testsidecar"
    )));
    config.expected_version = TEST_VERSION.to_string();
    config
        .extra_env
        .push(("MEMECHO_TEST_VERSION".to_string(), TEST_VERSION.to_string()));
    config.startup_timeout = Duration::from_secs(20);
    config.poll_interval = Duration::from_millis(50);
    config
}

fn port_of(url: &str) -> u16 {
    url.rsplit(':').next().unwrap().parse().unwrap()
}

async fn health_probe(url: &str) -> Result<reqwest::StatusCode, String> {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|e| e.to_string())?;
    match client.get(format!("{}/v1/health", url)).send().await {
        Ok(response) => Ok(response.status()),
        Err(e) => Err(e.to_string()),
    }
}

#[tokio::test]
async fn test_sidecar_starts_on_random_port_and_shuts_down() {
    let mut supervisor = GatewaySupervisor::new();
    let info = supervisor
        .start_sidecar(&sidecar_config())
        .await
        .expect("sidecar should complete the startup handshake");

    assert_eq!(info.mode, SupervisorMode::Sidecar);
    assert!(info.url.starts_with("http://127.0.0.1:"));
    let port = port_of(&info.url);
    assert_ne!(port, 8787, "sidecar must not depend on the fixed dev port");
    assert!(port >= 1024);
    assert!(!info.token.is_empty());
    assert_ne!(info.token, "change-me");
    assert!(
        !info.url.contains(&info.token),
        "token must never be in the URL"
    );
    assert_eq!(info.gateway_version, TEST_VERSION);
    assert_eq!(info.protocol_version, 1);
    assert_eq!(
        supervisor.connection().map(|c| c.url),
        Some(info.url.clone()),
        "runtime connection info stays available in memory"
    );

    supervisor.shutdown().await;
    assert!(
        supervisor.connection().is_none(),
        "shutdown clears the runtime"
    );
    // Exit cleanup: the managed process no longer serves health checks.
    assert!(
        health_probe(&info.url).await.is_err(),
        "sidecar must be terminated on shutdown"
    );
}

#[tokio::test]
async fn test_sidecar_honors_fixed_dev_port() {
    // Allocate a port up front, then ask the supervisor to use it — the
    // dev-mode fixed-port switch.
    let fixed = memecho_desktop_lib::gateway_supervisor::pick_loopback_port().unwrap();
    let mut config = sidecar_config();
    config.port = Some(fixed);

    let mut supervisor = GatewaySupervisor::new();
    let info = supervisor
        .start_sidecar(&config)
        .await
        .expect("sidecar should start on the fixed dev port");
    assert_eq!(port_of(&info.url), fixed);
    supervisor.shutdown().await;
}

#[tokio::test]
async fn test_sidecar_version_mismatch_returns_stable_error_and_cleans_up() {
    let mut config = sidecar_config();
    config.expected_version = "1.0.0".to_string();
    // Test sidecar keeps reporting TEST_VERSION → mismatch.

    let mut supervisor = GatewaySupervisor::new();
    let error = supervisor
        .start_sidecar(&config)
        .await
        .expect_err("version mismatch must fail the handshake");
    assert!(matches!(error, SupervisorError::VersionMismatch { .. }));
    assert!(supervisor.connection().is_none());
}

#[tokio::test]
async fn test_sidecar_protocol_mismatch_returns_stable_error() {
    let mut config = sidecar_config();
    config
        .extra_env
        .push(("MEMECHO_TEST_PROTOCOL".to_string(), "2".to_string()));

    let mut supervisor = GatewaySupervisor::new();
    let error = supervisor
        .start_sidecar(&config)
        .await
        .expect_err("protocol mismatch must fail the handshake");
    assert!(matches!(error, SupervisorError::ProtocolMismatch { .. }));
    assert!(supervisor.connection().is_none());
}

#[tokio::test]
async fn test_sidecar_startup_timeout_returns_stable_error() {
    let mut config = sidecar_config();
    // The test sidecar sleeps before binding, so the handshake never starts.
    config.extra_env.push((
        "MEMECHO_TEST_STARTUP_DELAY_SECS".to_string(),
        "30".to_string(),
    ));
    config.startup_timeout = Duration::from_millis(600);

    let mut supervisor = GatewaySupervisor::new();
    let started = std::time::Instant::now();
    let error = supervisor
        .start_sidecar(&config)
        .await
        .expect_err("late startup must time out");
    assert!(matches!(error, SupervisorError::StartupTimeout(_)));
    assert!(started.elapsed() < Duration::from_secs(10));
    assert!(supervisor.connection().is_none());
}

#[tokio::test]
async fn test_sidecar_early_exit_returns_stable_error() {
    let mut config = sidecar_config();
    config
        .extra_env
        .push(("MEMECHO_TEST_EXIT_EARLY".to_string(), "1".to_string()));

    let mut supervisor = GatewaySupervisor::new();
    let error = supervisor
        .start_sidecar(&config)
        .await
        .expect_err("early exit must fail the handshake");
    assert!(
        matches!(error, SupervisorError::EarlyExit(Some(3))),
        "expected stable EarlyExit(3), got: {:?}",
        error
    );
    assert!(supervisor.connection().is_none());
}

#[tokio::test]
async fn test_external_dev_mode_attaches_without_managing_a_process() {
    use wiremock::matchers::{method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/v1/health"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "status": "ok",
            "provider": "mock",
            "version": "0.9.0-dev",
            "protocol_version": 1
        })))
        .mount(&server)
        .await;

    let mut supervisor = GatewaySupervisor::new();
    let info = supervisor
        .connect_external(&server.uri())
        .await
        .expect("dev mode should attach to an external gateway");
    assert_eq!(info.mode, SupervisorMode::External);
    assert_eq!(info.url, server.uri());
    assert!(
        info.token.is_empty(),
        "external credentials stay in the OS credential store"
    );
    assert_eq!(info.gateway_version, "0.9.0-dev");

    // Shutdown must be a no-op for unmanaged external runtimes.
    supervisor.shutdown().await;
    assert!(supervisor.connection().is_none());
    // The mock gateway itself must keep running (we never managed it).
    assert_eq!(
        health_probe(&server.uri()).await.unwrap(),
        reqwest::StatusCode::OK
    );
}

#[tokio::test]
async fn test_handshake_token_travels_as_header_and_is_not_reused_across_runs() {
    let mut supervisor = GatewaySupervisor::new();

    // The test sidecar answers 401 unless the exact bearer token arrives as
    // an Authorization header; a successful handshake therefore proves header
    // transport (never URL/query transport).
    let first = supervisor
        .start_sidecar(&sidecar_config())
        .await
        .expect("handshake should authenticate via bearer header");

    // Restart: each run gets a fresh one-time token.
    let second = supervisor
        .start_sidecar(&sidecar_config())
        .await
        .expect("restart should complete a fresh handshake");
    assert_ne!(first.token, second.token);
    assert!(supervisor.connection().is_some());

    supervisor.shutdown().await;
}
