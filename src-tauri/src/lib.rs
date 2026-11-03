use std::io::{BufRead, BufReader, Read, Write};
use std::os::windows::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};

use tauri::{Manager, State};

const CREATE_NO_WINDOW: u32 = 0x08000000;

#[derive(Default)]
struct ApiChild(Mutex<Option<Child>>);

/// Checks whether a ParkingAI API is already answering `GET /health` on `port`,
/// without pulling in a full HTTP client crate. Returns true only if the
/// response looks like our own healthy backend, not just any listener on that
/// port (e.g. a stray unrelated process holding it).
fn parkingai_backend_is_healthy(port: &str) -> bool {
    let Ok(mut stream) = std::net::TcpStream::connect_timeout(
        &format!("127.0.0.1:{port}").parse().unwrap(),
        Duration::from_millis(500),
    ) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
    let request = format!("GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    let _ = stream.read_to_string(&mut response);
    response.contains("200") && response.contains("\"status\":\"ok\"")
}

#[tauri::command]
fn ensure_api(state: State<'_, ApiChild>) -> Result<String, String> {
    let mut process_guard = state.0.lock().map_err(|err| err.to_string())?;
    let port = std::env::var("PARKINGAI_PORT").unwrap_or_else(|_| "8766".to_string());
    let address = format!("http://127.0.0.1:{port}");

    if let Some(child) = process_guard.as_mut() {
        match child.try_wait() {
            Ok(None) => return Ok(address),
            Ok(Some(_)) => *process_guard = None,
            Err(error) => return Err(format!("Could not inspect API process: {error}")),
        }
    }

    // Another ParkingAI backend (started by a previous Tauri session, or manually)
    // may already own this port. Reuse it instead of spawning a second process that
    // would only fail to bind and linger uselessly -- this is the single source of
    // truth for "who owns the backend", not an in-memory guard scoped to this run.
    if parkingai_backend_is_healthy(&port) {
        return Ok(address);
    }

    let project_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or_else(|| "Could not resolve ParkingAI project directory.".to_string())?
        .to_path_buf();
    let engine_dir = project_dir.join("ai-engine");
    let python_path = std::env::var_os("PARKINGAI_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| engine_dir.join(".venv").join("Scripts").join("python.exe"));
    let script_path = engine_dir.join("app").join("run_api.py");

    if !python_path.exists() {
        return Err(format!("Python runtime not found: {}", python_path.display()));
    }
    if !script_path.exists() {
        return Err(format!("ParkingAI API entry point not found: {}", script_path.display()));
    }

    let mut child = Command::new(&python_path)
        .arg(&script_path)
        .current_dir(engine_dir.join("app"))
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|error| format!("Failed to start ParkingAI API: {error}"))?;

    if let Some(stderr) = child.stderr.take() {
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines() {
                match line {
                    Ok(line) => eprintln!("[ParkingAI API] {line}"),
                    Err(_) => break,
                }
            }
        });
    }

    let deadline = Instant::now() + Duration::from_secs(15);

    while Instant::now() < deadline {
        if let Some(exit) = child.try_wait().map_err(|error| error.to_string())? {
            // A port collision with a non-ParkingAI process (or a prior unhealthy
            // instance) surfaces here as a normal, visible startup failure instead
            // of a silently lingering duplicate process.
            return Err(format!(
                "ParkingAI API exited during startup ({exit}). Port {port} may already be in use by another process; check its environment configuration."
            ));
        }
        if parkingai_backend_is_healthy(&port) {
            *process_guard = Some(child);
            return Ok(address);
        }
        thread::sleep(Duration::from_millis(200));
    }

    let _ = child.kill();
    let _ = child.wait();
    Err("ParkingAI API did not become ready within 15 seconds.".to_string())
}

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            app.manage(ApiChild::default());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![greet, ensure_api])
        .build(tauri::generate_context!())
        .expect("error while building Tauri application");

    app.run(|app, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            if let Some(state) = app.try_state::<ApiChild>() {
                if let Ok(mut process_guard) = state.0.lock() {
                    if let Some(mut child) = process_guard.take() {
                        let _ = child.kill();
                        let _ = child.wait();
                    }
                }
            }
        }
    });
}
