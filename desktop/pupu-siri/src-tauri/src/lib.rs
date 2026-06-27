#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![start_console, stop_console])
        .run(tauri::generate_context!())
        .expect("error while running PuPu Siri");
}

use std::{
    collections::HashMap,
    fs::{self, OpenOptions},
    net::{SocketAddr, TcpStream},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

fn console_port_is_open() -> bool {
    let Ok(addr) = "127.0.0.1:8770".parse::<SocketAddr>() else {
        return false;
    };
    TcpStream::connect_timeout(&addr, Duration::from_millis(200)).is_ok()
}

fn looks_like_repo_root(path: &Path) -> bool {
    path.join("pupu_console").join("__main__.py").is_file()
}

fn find_repo_root() -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Ok(path) = std::env::current_dir() {
        candidates.push(path);
    }
    if let Ok(path) = std::env::current_exe() {
        if let Some(parent) = path.parent() {
            candidates.push(parent.to_path_buf());
        }
    }
    candidates.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")));

    for candidate in candidates {
        for ancestor in candidate.ancestors() {
            if looks_like_repo_root(ancestor) {
                return Some(ancestor.to_path_buf());
            }
        }
    }
    None
}

fn python_executable(repo_root: &Path) -> PathBuf {
    let venv_python = repo_root.join("ForFun").join("Scripts").join("python.exe");
    if venv_python.is_file() {
        return venv_python;
    }
    PathBuf::from("python")
}

fn launch_result(status: &str, message: &str) -> HashMap<String, String> {
    HashMap::from([
        ("status".to_string(), status.to_string()),
        ("message".to_string(), message.to_string()),
    ])
}

#[cfg(windows)]
fn console_listener_pids() -> Result<Vec<String>, String> {
    let output = Command::new("netstat")
        .arg("-ano")
        .output()
        .map_err(|err| format!("netstat failed: {err}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let mut pids: Vec<String> = stdout
        .lines()
        .filter_map(|line| {
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 5
                && parts[1].ends_with(":8770")
                && parts[3].eq_ignore_ascii_case("LISTENING")
            {
                Some(parts[4].to_string())
            } else {
                None
            }
        })
        .collect();
    pids.sort();
    pids.dedup();
    Ok(pids)
}

#[tauri::command]
fn stop_console() -> Result<HashMap<String, String>, String> {
    #[cfg(windows)]
    {
        let pids = console_listener_pids()?;
        if pids.is_empty() {
            return Ok(launch_result("already_stopped", "PuPu Console is not running."));
        }
        for pid in &pids {
            let mut command = Command::new("taskkill");
            command.arg("/PID").arg(pid).arg("/T").arg("/F");
            command.creation_flags(CREATE_NO_WINDOW);
            let output = command
                .output()
                .map_err(|err| format!("taskkill failed for pid {pid}: {err}"))?;
            if !output.status.success() {
                let stderr = String::from_utf8_lossy(&output.stderr);
                return Err(format!("stop PuPu Console failed for pid {pid}: {stderr}"));
            }
        }
        return Ok(launch_result("stopped", "PuPu Console has been stopped."));
    }

    #[cfg(not(windows))]
    {
        Err("Stopping PuPu Console from PuPu Siri is only implemented on Windows.".to_string())
    }
}

#[tauri::command]
fn start_console() -> Result<HashMap<String, String>, String> {
    if console_port_is_open() {
        return Ok(launch_result("already_running", "PuPu Console is already running."));
    }

    let repo_root = find_repo_root().ok_or_else(|| "Cannot locate PuPu repository root.".to_string())?;
    let log_dir = repo_root.join("logs").join("launcher");
    fs::create_dir_all(&log_dir).map_err(|err| format!("create log dir failed: {err}"))?;
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("console.log"))
        .map_err(|err| format!("open console.log failed: {err}"))?;
    let stderr = OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_dir.join("console.log"))
        .map_err(|err| format!("open console.log failed: {err}"))?;

    let mut command = Command::new(python_executable(&repo_root));
    command
        .arg("-m")
        .arg("pupu_console")
        .current_dir(&repo_root)
        .env("PUPU_REPO_ROOT", &repo_root)
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);

    command
        .spawn()
        .map_err(|err| format!("start PuPu Console failed: {err}"))?;
    Ok(launch_result("started", "PuPu Console is starting."))
}
