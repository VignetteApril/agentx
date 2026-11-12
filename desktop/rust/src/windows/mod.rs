mod backend;
mod ui;

use agentx_desktop::{Config, Installation, Result};
use std::io::Read;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::HWND;
use windows_sys::Win32::System::Console::{AttachConsole, ATTACH_PARENT_PROCESS};
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Threading::{GetCurrentProcess, IsWow64Process2, CREATE_NO_WINDOW};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetSystemMetrics, LoadImageW, MessageBoxW, HICON, IMAGE_ICON, LR_SHARED, MB_ICONERROR, MB_OK,
    SM_CXICON, SM_CXSMICON, SM_CYICON, SM_CYSMICON,
};

pub fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(Some(0)).collect()
}

pub unsafe fn app_icon(small: bool) -> HICON {
    let (width, height) = if small {
        (SM_CXSMICON, SM_CYSMICON)
    } else {
        (SM_CXICON, SM_CYICON)
    };
    LoadImageW(
        GetModuleHandleW(std::ptr::null()),
        // MAKEINTRESOURCEW(1): an ordinal encoded as an address, not a string.
        std::ptr::without_provenance::<u16>(1),
        IMAGE_ICON,
        GetSystemMetrics(width),
        GetSystemMetrics(height),
        LR_SHARED,
    ) as HICON
}

pub fn attach_console() {
    // No new console is created for normal desktop launches.
    unsafe {
        AttachConsole(ATTACH_PARENT_PROCESS);
    }
}

pub fn show_error(message: &str) {
    unsafe {
        MessageBoxW(
            std::ptr::null_mut(),
            wide(message).as_ptr(),
            wide("AgentX Codex").as_ptr(),
            MB_OK | MB_ICONERROR,
        );
    }
}

pub fn message(owner: HWND, title: &str, text: &str) {
    unsafe {
        MessageBoxW(owner, wide(text).as_ptr(), wide(title).as_ptr(), MB_OK);
    }
}

/// Only constant discovery scripts are executed; paths never become shell code.
fn powershell(script: &str) -> Result<String> {
    let system = std::env::var_os("SystemRoot")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
    let executable = system.join("System32/WindowsPowerShell/v1.0/powershell.exe");
    let mut child = Command::new(executable)
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let mut output = child.stdout.take().unwrap();
    let mut errors = child.stderr.take().unwrap();
    let stdout = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = output.read_to_end(&mut bytes);
        bytes
    });
    let stderr = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = errors.read_to_end(&mut bytes);
        bytes
    });
    let deadline = Instant::now() + Duration::from_secs(12);
    let code = loop {
        if let Some(status) = child.try_wait().map_err(|e| e.to_string())? {
            break status;
        }
        if Instant::now() >= deadline {
            // This is our own short-lived discovery helper, never a client.
            let _ = child.kill();
            let _ = child.wait();
            let _ = stdout.join();
            let _ = stderr.join();
            return Err("Windows 安装检测超时，可点击“选择客户端”手动定位。".into());
        }
        std::thread::sleep(Duration::from_millis(50));
    };
    let data = stdout.join().map_err(|_| "安装检测输出读取失败")?;
    let error = stderr.join().unwrap_or_default();
    if !code.success() {
        return Err(format!(
            "Windows 安装检测失败：{}",
            String::from_utf8_lossy(&error).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&data)
        .trim_start_matches('\u{feff}')
        .trim()
        .to_string())
}

pub fn compatible(installation: Installation) -> Result<Installation> {
    let mut current = 0;
    let mut native = 0;
    unsafe {
        IsWow64Process2(GetCurrentProcess(), &mut current, &mut native);
    }
    if installation.machine == 0xaa64 && native != 0xaa64 {
        return Err("当前电脑需要 x64 版客户端，选中的客户端是 ARM64 版。".into());
    }
    Ok(installation)
}

pub fn discover(explicit: Option<&Path>) -> Result<Installation> {
    if let Some(path) = explicit {
        if path.exists() {
            return Installation::inspect(path).and_then(compatible);
        }
    }
    let mut candidates = Vec::new();
    let locations = powershell(
        r#"[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); Get-AppxPackage -Name 'OpenAI.Codex' | Sort-Object Version -Descending | ForEach-Object { $_.InstallLocation }"#,
    );
    if let Ok(locations) = &locations {
        for location in locations.lines().map(str::trim).filter(|s| !s.is_empty()) {
            let root = PathBuf::from(location);
            candidates.push(root.join("app/ChatGPT.exe"));
            candidates.push(root.join("app/Codex.exe"));
        }
    }
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        let local = PathBuf::from(local);
        for path in [
            "Programs/ChatGPT/ChatGPT.exe",
            "Programs/Codex/Codex.exe",
            "Programs/OpenAI/Codex/Codex.exe",
        ] {
            candidates.push(local.join(path));
        }
    }
    let mut incompatible = None;
    for candidate in candidates.iter().filter(|p| p.is_file()) {
        match Installation::inspect(candidate).and_then(compatible) {
            Ok(installation) => return Ok(installation),
            Err(error) => incompatible = Some(error),
        }
    }
    if let Some(error) = incompatible {
        return Err(error);
    }
    Err(format!(
        "未找到已安装的官方 Codex 客户端。请安装官方 Windows 版，或点击“选择客户端”。{}",
        locations
            .err()
            .map(|e| format!("\n{e}"))
            .unwrap_or_default()
    ))
}

pub fn run(
    root: PathBuf,
    app: Option<PathBuf>,
    self_test: Option<PathBuf>,
    fixture: Option<usize>,
) -> Result<()> {
    if let Some(index) = fixture {
        return ui::fixture(index);
    }
    if let Some(report) = self_test {
        return ui::self_test(&report);
    }
    let mut config = Config::load(&root)?;
    let profiles = config.ensure_two_profiles()?;
    ui::run(config, profiles, app)
}
