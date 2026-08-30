//! Injectable stand-in for the memEcho Gateway sidecar, used by the
//! supervisor integration tests. It honors the startup handshake contract:
//!
//! - reads `MEMECHO_GATEWAY_PORT` and `MEMECHO_GATEWAY_TOKEN`;
//! - serves `GET /v1/health` on `127.0.0.1:<port>`;
//! - requires `Authorization: Bearer <token>` when a token is set (proving
//!   the supervisor sends the token as a header, never in the URL);
//! - reports `version` / `protocol_version` that tests can override via
//!   `MEMECHO_TEST_VERSION` / `MEMECHO_TEST_PROTOCOL`.
//!
//! Test knobs: `MEMECHO_TEST_EXIT_EARLY` exits with status 3 before binding;
//! `MEMECHO_TEST_STARTUP_DELAY_SECS` delays startup to exercise timeouts.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::time::Duration;

fn main() {
    if std::env::var_os("MEMECHO_TEST_EXIT_EARLY").is_some() {
        std::process::exit(3);
    }
    if let Ok(delay) = std::env::var("MEMECHO_TEST_STARTUP_DELAY_SECS") {
        if let Ok(seconds) = delay.parse::<u64>() {
            std::thread::sleep(Duration::from_secs(seconds));
        }
    }

    let port: u16 = std::env::var("MEMECHO_GATEWAY_PORT")
        .ok()
        .and_then(|value| value.parse().ok())
        .expect("MEMECHO_GATEWAY_PORT must be set");
    let listener =
        TcpListener::bind(("127.0.0.1", port)).expect("test sidecar failed to bind loopback");

    let version = std::env::var("MEMECHO_TEST_VERSION").unwrap_or_else(|_| "0.1.0".into());
    let protocol = std::env::var("MEMECHO_TEST_PROTOCOL").unwrap_or_else(|_| "1".into());
    let token = std::env::var("MEMECHO_GATEWAY_TOKEN").unwrap_or_default();

    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        let version = version.clone();
        let protocol = protocol.clone();
        let token = token.clone();
        std::thread::spawn(move || handle(stream, &version, &protocol, &token));
    }
}

fn handle(mut stream: TcpStream, version: &str, protocol: &str, token: &str) {
    let mut data: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 2048];
    loop {
        match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => {
                data.extend_from_slice(&chunk[..n]);
                if data.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
                if data.len() > 64 * 1024 {
                    return;
                }
            }
            Err(_) => return,
        }
    }

    let text = String::from_utf8_lossy(&data);
    let first_line = text.lines().next().unwrap_or("");
    let wants_health = first_line.starts_with("GET /v1/health");
    let authorized = token.is_empty()
        || text.lines().any(|line| {
            line.to_ascii_lowercase().starts_with("authorization:")
                && line.splitn(2, ':').nth(1).map(str::trim)
                    == Some(format!("Bearer {}", token).as_str())
        });

    let (status, body) = if !wants_health {
        ("404 Not Found", "{}".to_string())
    } else if !authorized {
        (
            "401 Unauthorized",
            r#"{"detail":"invalid gateway token"}"#.to_string(),
        )
    } else {
        (
            "200 OK",
            format!(
                r#"{{"status":"ok","provider":"test-sidecar","version":"{}","protocol_version":{}}}"#,
                version, protocol
            ),
        )
    };

    let response = format!(
        "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        status,
        body.len(),
        body
    );
    let _ = stream.write_all(response.as_bytes());
}
