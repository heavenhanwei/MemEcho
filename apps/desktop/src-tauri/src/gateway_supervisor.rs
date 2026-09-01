//! Gateway Supervisor: starts, verifies, and stops the local memEcho Gateway.
//!
//! # Runtime modes
//!
//! - [`SupervisorMode::Sidecar`]: the desktop app spawns a managed
//!   `memecho-gateway` process on a random loopback port, waits for the
//!   startup handshake, and terminates the process when the app exits.
//! - [`SupervisorMode::External`]: dev/self-hosted mode. The app connects to
//!   a gateway the user started themselves; no process is managed.
//!
//! # Startup handshake contract (v1)
//!
//! The supervisor passes three environment variables to the child process:
//!
//! - `MEMECHO_GATEWAY_HOST` — always `127.0.0.1` for sidecar mode;
//! - `MEMECHO_GATEWAY_PORT` — the loopback port chosen by the supervisor;
//! - `MEMECHO_GATEWAY_TOKEN` — a fresh one-time bearer token. The token is
//!   never written to disk, never placed in a URL or query parameter, and
//!   never logged.
//!
//! The child must bind `127.0.0.1:$MEMECHO_GATEWAY_PORT` and answer
//! `GET /v1/health` within the startup timeout with:
//!
//! ```json
//! { "status": "ok", "version": "<semver>", "protocol_version": 1 }
//! ```
//!
//! The supervisor probes health with an `Authorization: Bearer <token>`
//! header (never a query parameter), verifies `version` against the expected
//! gateway build and `protocol_version` against [`PROTOCOL_VERSION`], and
//! keeps the resulting URL and token in memory only. On timeout, version
//! mismatch, or early exit the supervisor terminates the child and returns a
//! stable [`SupervisorError`].

use serde::Serialize;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// Startup handshake protocol version understood by this supervisor.
pub const PROTOCOL_VERSION: u32 = 1;

/// Base name of the bundled gateway sidecar executable.
pub const SIDECAR_BASE_NAME: &str = "memecho-gateway";

// Prevent the console-subsystem PyInstaller Sidecar from creating a visible
// terminal window when it is launched by the Windows desktop application.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// Environment variables of the startup handshake contract.
pub const ENV_HOST: &str = "MEMECHO_GATEWAY_HOST";
pub const ENV_PORT: &str = "MEMECHO_GATEWAY_PORT";
pub const ENV_TOKEN: &str = "MEMECHO_GATEWAY_TOKEN";
pub const ENV_ACCESS_TOKEN: &str = "MEMECHO_DEMO_TOKEN";
pub const ENV_DATA_DIR: &str = "MEMECHO_DATA_DIR";

/// Dev override: absolute path to a gateway executable to run as sidecar.
pub const ENV_SIDECAR_OVERRIDE: &str = "MEMECHO_GATEWAY_SIDECAR";

/// Loopback bind host enforced for sidecar mode.
pub const LOOPBACK_HOST: &str = "127.0.0.1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SupervisorMode {
    Sidecar,
    External,
}

/// Stable, user-actionable supervisor failures.
#[derive(Debug, thiserror::Error)]
pub enum SupervisorError {
    #[error("failed to allocate a loopback port: {0}")]
    PortAllocation(String),
    #[error("failed to start the gateway sidecar: {0}")]
    Spawn(String),
    #[error("gateway sidecar exited before becoming healthy (status {0:?})")]
    EarlyExit(Option<i32>),
    #[error("gateway did not answer /v1/health within {0:?}")]
    StartupTimeout(Duration),
    #[error("gateway version mismatch: expected {expected}, found {actual}")]
    VersionMismatch { expected: String, actual: String },
    #[error("gateway protocol mismatch: expected {expected}, found {actual}")]
    ProtocolMismatch { expected: u32, actual: u32 },
    #[error("gateway health check failed: {0}")]
    HealthFailed(String),
    #[error("gateway returned an invalid health payload: {0}")]
    InvalidHealth(String),
    #[error("invalid external gateway URL: {0}")]
    InvalidUrl(String),
}

/// Supervisor policy for one sidecar launch.
#[derive(Debug, Clone)]
pub struct SupervisorConfig {
    /// Executable to spawn.
    pub program: PathBuf,
    /// Extra CLI arguments after the program name.
    pub args: Vec<String>,
    /// Extra environment variables (test/dev injection; secrets are always
    /// passed through `MEMECHO_GATEWAY_TOKEN` only).
    pub extra_env: Vec<(String, String)>,
    /// Writable working directory used by the frozen Gateway for SQLite,
    /// uploads, and temporary media. Installed applications must never use
    /// the read-only Program Files directory for runtime state.
    pub working_dir: Option<PathBuf>,
    /// Gateway version the handshake must report.
    pub expected_version: String,
    /// Fixed port (dev mode). `None` selects a random loopback port.
    pub port: Option<u16>,
    /// Total time allowed for the startup handshake.
    pub startup_timeout: Duration,
    /// Delay between health probes.
    pub poll_interval: Duration,
}

impl SupervisorConfig {
    /// Production defaults for the bundled sidecar executable.
    pub fn for_sidecar(program: PathBuf) -> Self {
        Self {
            program,
            args: Vec::new(),
            extra_env: Vec::new(),
            working_dir: None,
            expected_version: env!("CARGO_PKG_VERSION").to_string(),
            port: None,
            startup_timeout: Duration::from_secs(30),
            poll_interval: Duration::from_millis(250),
        }
    }

    /// Configure the per-user writable directory for an installed sidecar.
    pub fn with_data_dir(mut self, data_dir: PathBuf) -> Self {
        self.extra_env.push((
            ENV_DATA_DIR.to_string(),
            data_dir.to_string_lossy().into_owned(),
        ));
        self.working_dir = Some(data_dir);
        self
    }
}

/// Connection details for the currently active gateway runtime.
///
/// Held in memory only; never serialized to disk. `token` is exposed to the
/// webview over the local IPC bridge so the frontend can set `Authorization`
/// headers — it is never embedded in `url`.
#[derive(Debug, Clone, Serialize)]
pub struct GatewayConnectionInfo {
    pub mode: SupervisorMode,
    pub url: String,
    pub token: String,
    pub gateway_version: String,
    pub protocol_version: u32,
}

struct RuntimeState {
    info: GatewayConnectionInfo,
    child: Option<tokio::process::Child>,
}

/// Allocate a free loopback port by briefly binding `127.0.0.1:0`.
pub fn pick_loopback_port() -> Result<u16, SupervisorError> {
    let listener = std::net::TcpListener::bind((LOOPBACK_HOST, 0))
        .map_err(|e| SupervisorError::PortAllocation(e.to_string()))?;
    let port = listener
        .local_addr()
        .map_err(|e| SupervisorError::PortAllocation(e.to_string()))?
        .port();
    drop(listener);
    Ok(port)
}

/// Generate the one-time bearer token for this run.
pub fn generate_token() -> String {
    uuid::Uuid::new_v4().simple().to_string()
}

/// Build the sidecar spawn command per the handshake contract.
///
/// Returns a `std::process::Command` (converted to `tokio::process::Command`
/// at spawn time) so tests can introspect args/envs via `get_args`/`get_envs`.
/// The token is passed only through `MEMECHO_GATEWAY_TOKEN`; it never appears
/// in the argument vector (which is visible in process listings).
pub fn build_sidecar_command(
    program: &Path,
    args: &[String],
    extra_env: &[(String, String)],
    port: u16,
    token: &str,
) -> std::process::Command {
    let mut cmd = std::process::Command::new(program);
    cmd.args(args)
        .env(ENV_HOST, LOOPBACK_HOST)
        .env(ENV_PORT, port.to_string())
        // Keep the supervisor-facing name for compatibility and provide the
        // application setting consumed by pydantic-settings in the Gateway.
        .env(ENV_TOKEN, token)
        .env(ENV_ACCESS_TOKEN, token);
    for (key, value) in extra_env {
        cmd.env(key, value);
    }
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);
    cmd.stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());
    cmd
}

/// Locate the bundled gateway sidecar executable, if present.
///
/// Search order: `MEMECHO_GATEWAY_SIDECAR` override (dev hook), then the
/// current executable's directory and its `binaries/` subdirectory.
/// Returns `None` until sidecar packaging lands (see the open-source-edition
/// sidecar docs for the packaging blocker).
pub fn resolve_sidecar_binary() -> Option<PathBuf> {
    if let Ok(explicit) = std::env::var(ENV_SIDECAR_OVERRIDE) {
        let path = PathBuf::from(explicit);
        if path.is_file() {
            return Some(path);
        }
    }
    let exe_name = if cfg!(windows) {
        format!("{}.exe", SIDECAR_BASE_NAME)
    } else {
        SIDECAR_BASE_NAME.to_string()
    };
    if let Ok(current) = std::env::current_exe() {
        if let Some(dir) = current.parent() {
            for candidate in [dir.join(&exe_name), dir.join("binaries").join(&exe_name)] {
                if candidate.is_file() {
                    return Some(candidate);
                }
            }
        }
    }
    None
}

#[derive(Debug)]
struct HealthPayload {
    status: String,
    version: String,
    protocol_version: Option<u32>,
}

enum ProbeOutcome {
    Healthy(HealthPayload),
    /// HTTP error or transport failure; the gateway may still be starting.
    NotReady,
    /// The process answered but violated the handshake contract.
    Invalid(String),
}

async fn probe_health(client: &reqwest::Client, base_url: &str, token: &str) -> ProbeOutcome {
    let base = base_url.trim_end_matches('/');
    let mut request = client.get(format!("{}/v1/health", base));
    if !token.is_empty() {
        // Header transport only — the token must never reach a URL.
        request = request.bearer_auth(token);
    }
    let response = match request.send().await {
        Ok(response) => response,
        Err(_) => return ProbeOutcome::NotReady,
    };
    if !response.status().is_success() {
        return ProbeOutcome::NotReady;
    }
    let body: serde_json::Value = match response.json().await {
        Ok(body) => body,
        Err(e) => return ProbeOutcome::Invalid(e.to_string()),
    };
    let status = match body.get("status").and_then(|v| v.as_str()) {
        Some(status) => status.to_string(),
        None => return ProbeOutcome::Invalid("missing \"status\" field".into()),
    };
    let version = match body.get("version").and_then(|v| v.as_str()) {
        Some(version) => version.to_string(),
        None => return ProbeOutcome::Invalid("missing \"version\" field".into()),
    };
    let protocol_version = body.get("protocol_version").and_then(|v| v.as_u64());
    ProbeOutcome::Healthy(HealthPayload {
        status,
        version,
        protocol_version: protocol_version.map(|v| v as u32),
    })
}

/// Manage one gateway runtime (sidecar process or external dev gateway).
pub struct GatewaySupervisor {
    runtime: Option<RuntimeState>,
}

impl Default for GatewaySupervisor {
    fn default() -> Self {
        Self::new()
    }
}

impl GatewaySupervisor {
    pub fn new() -> Self {
        Self { runtime: None }
    }

    /// Connection info for the active runtime, if any.
    pub fn connection(&mut self) -> Option<GatewayConnectionInfo> {
        let exited = self
            .runtime
            .as_mut()
            .and_then(|runtime| runtime.child.as_mut())
            .and_then(|child| child.try_wait().ok().flatten())
            .is_some();
        if exited {
            self.runtime = None;
        }
        self.runtime.as_ref().map(|runtime| runtime.info.clone())
    }

    /// Spawn a managed gateway sidecar and complete the startup handshake.
    ///
    /// Replaces (and stops) any previously active runtime. On any handshake
    /// failure the child process is terminated before the error is returned.
    pub async fn start_sidecar(
        &mut self,
        config: &SupervisorConfig,
    ) -> Result<GatewayConnectionInfo, SupervisorError> {
        self.shutdown().await;

        if let Some(working_dir) = &config.working_dir {
            std::fs::create_dir_all(working_dir)
                .map_err(|error| SupervisorError::Spawn(error.to_string()))?;
        }

        let port = match config.port {
            Some(port) => port,
            None => pick_loopback_port()?,
        };
        let token = generate_token();
        let mut command = build_sidecar_command(
            &config.program,
            &config.args,
            &config.extra_env,
            port,
            &token,
        );
        if let Some(working_dir) = &config.working_dir {
            command.current_dir(working_dir);
            let stdout = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(working_dir.join("gateway.stdout.log"))
                .map_err(|error| SupervisorError::Spawn(error.to_string()))?;
            let stderr = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(working_dir.join("gateway.stderr.log"))
                .map_err(|error| SupervisorError::Spawn(error.to_string()))?;
            command.stdout(stdout).stderr(stderr);
        }
        let mut command = tokio::process::Command::from(command);
        // Do not let the bundled Gateway outlive the desktop process.  The
        // explicit shutdown path below handles normal exits; kill_on_drop is
        // the safety net for window teardown, panics, or an interrupted app
        // shutdown where the final Tauri Exit event is never delivered.
        command.kill_on_drop(true);
        let mut child = command
            .spawn()
            .map_err(|e| SupervisorError::Spawn(e.to_string()))?;

        let url = format!("http://{}:{}", LOOPBACK_HOST, port);
        match await_handshake(&url, &token, config, &mut child).await {
            Ok((gateway_version, protocol_version)) => {
                let info = GatewayConnectionInfo {
                    mode: SupervisorMode::Sidecar,
                    url,
                    token,
                    gateway_version,
                    protocol_version,
                };
                self.runtime = Some(RuntimeState {
                    info: info.clone(),
                    child: Some(child),
                });
                Ok(info)
            }
            Err(error) => {
                terminate(&mut child).await;
                Err(error)
            }
        }
    }

    /// Attach to an externally started gateway (dev/self-hosted mode).
    ///
    /// The URL is validated (no credentials or query parameters allowed) and
    /// `/v1/health` is probed once. The reported version is recorded but not
    /// enforced — external gateways are an explicit, user-managed choice.
    /// Tokens for remote gateways stay in the OS credential store and are not
    /// part of the supervisor runtime.
    pub async fn connect_external(
        &mut self,
        url: &str,
    ) -> Result<GatewayConnectionInfo, SupervisorError> {
        crate::gateway_check::validate_gateway_url(url).map_err(SupervisorError::InvalidUrl)?;
        self.shutdown().await;

        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(5))
            .build()
            .map_err(|e| SupervisorError::HealthFailed(e.to_string()))?;
        let (gateway_version, protocol_version) = match probe_health(&client, url, "").await {
            ProbeOutcome::Healthy(payload) => (
                payload.version,
                payload.protocol_version.unwrap_or(PROTOCOL_VERSION),
            ),
            ProbeOutcome::NotReady => {
                return Err(SupervisorError::HealthFailed(format!(
                    "external gateway at {} is not reachable or unhealthy",
                    url
                )))
            }
            ProbeOutcome::Invalid(reason) => return Err(SupervisorError::InvalidHealth(reason)),
        };

        let info = GatewayConnectionInfo {
            mode: SupervisorMode::External,
            url: url.trim_end_matches('/').to_string(),
            token: String::new(),
            gateway_version,
            protocol_version,
        };
        self.runtime = Some(RuntimeState {
            info: info.clone(),
            child: None,
        });
        Ok(info)
    }

    /// Stop the managed sidecar (best-effort graceful kill + reap) and forget
    /// the runtime. Safe to call repeatedly.
    pub async fn shutdown(&mut self) {
        if let Some(state) = self.runtime.take() {
            if let Some(mut child) = state.child {
                terminate(&mut child).await;
            }
        }
    }
}

async fn await_handshake(
    url: &str,
    token: &str,
    config: &SupervisorConfig,
    child: &mut tokio::process::Child,
) -> Result<(String, u32), SupervisorError> {
    let deadline = Instant::now() + config.startup_timeout;
    let probe_timeout = config
        .poll_interval
        .saturating_mul(4)
        .max(Duration::from_secs(1));
    let client = reqwest::Client::builder()
        .timeout(probe_timeout)
        .build()
        .map_err(|e| SupervisorError::HealthFailed(e.to_string()))?;

    loop {
        if let Ok(Some(status)) = child.try_wait() {
            return Err(SupervisorError::EarlyExit(status.code()));
        }
        match probe_health(&client, url, token).await {
            ProbeOutcome::Healthy(payload) => {
                if payload.status != "ok" {
                    return Err(SupervisorError::HealthFailed(format!(
                        "gateway reported status {:?}",
                        payload.status
                    )));
                }
                if payload.version != config.expected_version {
                    return Err(SupervisorError::VersionMismatch {
                        expected: config.expected_version.clone(),
                        actual: payload.version,
                    });
                }
                let protocol_version = payload.protocol_version.unwrap_or(PROTOCOL_VERSION);
                if protocol_version != PROTOCOL_VERSION {
                    return Err(SupervisorError::ProtocolMismatch {
                        expected: PROTOCOL_VERSION,
                        actual: protocol_version,
                    });
                }
                return Ok((payload.version, protocol_version));
            }
            ProbeOutcome::NotReady => {}
            ProbeOutcome::Invalid(reason) => {
                return Err(SupervisorError::InvalidHealth(reason));
            }
        }
        if Instant::now() >= deadline {
            return Err(SupervisorError::StartupTimeout(config.startup_timeout));
        }
        tokio::time::sleep(config.poll_interval).await;
    }
}

async fn terminate(child: &mut tokio::process::Child) {
    // Windows has no SIGTERM equivalent for child processes; kill is the
    // graceful-enough path for a loopback-only sidecar we own.
    let _ = child.kill().await;
    let _ = child.wait().await;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_pick_loopback_port_allocates_usable_port() {
        let port = pick_loopback_port().expect("port allocation should succeed");
        assert!(port >= 1024, "expected a non-privileged port, got {}", port);
    }

    #[test]
    fn test_generate_token_is_random_and_not_default() {
        let first = generate_token();
        let second = generate_token();
        assert_eq!(first.len(), 32);
        assert_ne!(first, second);
        assert_ne!(first, "change-me");
    }

    #[test]
    fn test_build_command_keeps_token_out_of_arguments() {
        let token = generate_token();
        let cmd = build_sidecar_command(
            Path::new("memecho-gateway"),
            &["serve".to_string()],
            &[("MEMECHO_EXTRA".to_string(), "1".to_string())],
            49321,
            &token,
        );
        let args: Vec<_> = cmd
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        assert_eq!(args, vec!["serve"]);
        assert!(
            !args.iter().any(|a| a.contains(&token)),
            "token must never be a CLI argument"
        );

        let envs: Vec<_> = cmd
            .get_envs()
            .filter_map(|(k, v)| {
                v.map(|v| {
                    (
                        k.to_string_lossy().into_owned(),
                        v.to_string_lossy().into_owned(),
                    )
                })
            })
            .collect();
        assert!(envs
            .iter()
            .any(|(k, v)| k == ENV_HOST && v == LOOPBACK_HOST));
        assert!(envs.iter().any(|(k, v)| k == ENV_PORT && v == "49321"));
        assert!(envs.iter().any(|(k, v)| k == ENV_TOKEN && v == &token));
        assert!(envs
            .iter()
            .any(|(k, v)| k == ENV_ACCESS_TOKEN && v == &token));
        assert!(envs.iter().any(|(k, v)| k == "MEMECHO_EXTRA" && v == "1"));
    }

    #[cfg(windows)]
    #[test]
    fn test_windows_sidecar_uses_create_no_window_flag() {
        assert_eq!(CREATE_NO_WINDOW, 0x0800_0000);
    }

    #[tokio::test]
    async fn test_connect_external_rejects_invalid_urls() {
        let mut supervisor = GatewaySupervisor::new();
        // Remote plaintext HTTP is rejected by URL validation.
        let err = supervisor
            .connect_external("http://gateway.example.com")
            .await
            .unwrap_err();
        assert!(matches!(err, SupervisorError::InvalidUrl(_)));
        // Query parameters (a classic token-leak vector) are rejected.
        let err = supervisor
            .connect_external("https://gateway.example.com?token=abc")
            .await
            .unwrap_err();
        assert!(matches!(err, SupervisorError::InvalidUrl(_)));
        assert!(supervisor.connection().is_none());
    }

    #[test]
    fn test_supervisor_config_defaults() {
        let config = SupervisorConfig::for_sidecar(PathBuf::from("memecho-gateway"));
        assert!(
            config.port.is_none(),
            "production default must use a random port"
        );
        assert_eq!(config.expected_version, env!("CARGO_PKG_VERSION"));
        assert!(config.startup_timeout >= Duration::from_secs(5));
        assert!(config.working_dir.is_none());
    }

    #[test]
    fn test_sidecar_data_dir_is_writable_runtime_state() {
        let data_dir = PathBuf::from("runtime-data");
        let config = SupervisorConfig::for_sidecar(PathBuf::from("memecho-gateway"))
            .with_data_dir(data_dir.clone());
        assert_eq!(config.working_dir, Some(data_dir.clone()));
        assert!(config
            .extra_env
            .iter()
            .any(|(key, value)| { key == ENV_DATA_DIR && value == &data_dir.to_string_lossy() }));
    }
}
