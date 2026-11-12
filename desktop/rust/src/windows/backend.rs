use super::{powershell, wide};
use agentx_desktop::{write_json, LaunchPlan, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs::{self, OpenOptions};
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_EXTENDED_FRAME_BOUNDS};
use windows_sys::Win32::System::Threading::*;
use windows_sys::Win32::UI::Shell::CommandLineToArgvW;
use windows_sys::Win32::UI::WindowsAndMessaging::*;

fn path_key(path: &Path) -> String {
    path.to_string_lossy()
        .trim_start_matches(r"\\?\")
        .replace('/', "\\")
        .to_lowercase()
}

fn fingerprint(pid: u32) -> Option<(u64, PathBuf)> {
    unsafe {
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if process.is_null() {
            return None;
        }
        let mut code = 0;
        let alive = GetExitCodeProcess(process, &mut code) != 0 && code == 259;
        let (mut born, mut exited, mut kernel, mut user) = (
            std::mem::zeroed::<FILETIME>(),
            std::mem::zeroed(),
            std::mem::zeroed(),
            std::mem::zeroed(),
        );
        let times = GetProcessTimes(process, &mut born, &mut exited, &mut kernel, &mut user) != 0;
        let mut path = vec![0u16; 32768];
        let mut length = path.len() as u32;
        let image = QueryFullProcessImageNameW(process, 0, path.as_mut_ptr(), &mut length) != 0;
        CloseHandle(process);
        if !alive || !times || !image {
            return None;
        }
        Some((
            (u64::from(born.dwHighDateTime) << 32) | u64::from(born.dwLowDateTime),
            PathBuf::from(String::from_utf16_lossy(&path[..length as usize])),
        ))
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Instance {
    pub pid: u32,
    pub born: u64,
    pub executable: PathBuf,
    pub user_data: PathBuf,
    #[serde(default)]
    pub windows: Vec<usize>,
}

pub struct Client {
    pub instance: Instance,
    record: Option<PathBuf>,
    // Dropping a child handle does not terminate the client process.
    _child: Option<Child>,
}

#[derive(Clone, Copy)]
pub struct Window {
    pub handle: usize,
    pub rect: RECT,
    pub visible: bool,
    pub primary: bool,
}

pub fn process_windows(pid: u32) -> Vec<Window> {
    struct Collect {
        pid: u32,
        windows: Vec<Window>,
    }
    unsafe extern "system" fn callback(hwnd: HWND, data: LPARAM) -> i32 {
        let state = &mut *(data as *mut Collect);
        let mut pid = 0;
        GetWindowThreadProcessId(hwnd, &mut pid);
        if pid != state.pid {
            return 1;
        }
        let mut rect: RECT = std::mem::zeroed();
        if GetWindowRect(hwnd, &mut rect) == 0 || rect.right <= rect.left || rect.bottom <= rect.top
        {
            return 1;
        }
        let primary = GetWindow(hwnd, GW_OWNER).is_null()
            && GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW as isize == 0
            && GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION as isize != 0
            && GetWindowTextLengthW(hwnd) > 0;
        let mut visible_bounds = rect;
        if DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS as u32,
            &mut visible_bounds as *mut _ as *mut _,
            std::mem::size_of::<RECT>() as u32,
        ) >= 0
            && visible_bounds.right > visible_bounds.left
            && visible_bounds.bottom > visible_bounds.top
        {
            rect = visible_bounds;
        }
        state.windows.push(Window {
            handle: hwnd as usize,
            rect,
            visible: IsWindowVisible(hwnd) != 0,
            primary,
        });
        1
    }
    let mut collect = Collect {
        pid,
        windows: Vec::new(),
    };
    unsafe {
        EnumWindows(Some(callback), &mut collect as *mut _ as isize);
    }
    collect.windows
}

fn command_line_args(text: &str) -> Vec<String> {
    unsafe {
        let mut length = 0;
        let args = CommandLineToArgvW(wide(text).as_ptr(), &mut length);
        if args.is_null() {
            return Vec::new();
        }
        let mut values = Vec::new();
        for item in std::slice::from_raw_parts(args, length as usize) {
            let mut n = 0;
            while *item.add(n) != 0 {
                n += 1;
            }
            values.push(String::from_utf16_lossy(std::slice::from_raw_parts(
                *item, n,
            )));
        }
        LocalFree(args as *mut _);
        values
    }
}

fn find_process(plan: &LaunchPlan) -> Option<Instance> {
    let text = powershell(r#"[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); @(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe' OR Name='Codex.exe'" | Select-Object ProcessId,ExecutablePath,CommandLine) | ConvertTo-Json -Compress"#).ok()?;
    let data: Value = serde_json::from_str(text.trim_start_matches('\u{feff}')).ok()?;
    let candidates = if let Some(array) = data.as_array() {
        array.clone()
    } else {
        vec![data]
    };
    for candidate in candidates {
        let pid = candidate["ProcessId"]
            .as_u64()
            .and_then(|p| u32::try_from(p).ok());
        let command_line = candidate["CommandLine"].as_str();
        let executable = candidate["ExecutablePath"].as_str().map(PathBuf::from);
        let (Some(pid), Some(command_line), Some(executable)) = (pid, command_line, executable)
        else {
            continue;
        };
        if path_key(&executable) != path_key(&plan.executable) {
            continue;
        }
        let expected = format!("--user-data-dir={}", plan.user_data.display());
        if !command_line_args(command_line)
            .iter()
            .any(|a| a.eq_ignore_ascii_case(&expected))
        {
            continue;
        }
        if let Some((born, actual)) = fingerprint(pid) {
            if path_key(&actual) == path_key(&plan.executable) {
                return Some(Instance {
                    pid,
                    born,
                    executable: actual,
                    user_data: plan.user_data.clone(),
                    windows: Vec::new(),
                });
            }
        }
    }
    None
}

pub fn launch(plan: &LaunchPlan) -> Result<Client> {
    plan.paths().prepare()?;
    let record = plan.profile_dir.join("desktop-instance.json");
    let saved = fs::read(&record)
        .ok()
        .and_then(|v| serde_json::from_slice::<Instance>(&v).ok());
    let saved = saved.filter(|instance| {
        path_key(&instance.user_data) == path_key(&plan.user_data)
            && fingerprint(instance.pid).is_some_and(|(born, path)| {
                born == instance.born && path_key(&path) == path_key(&plan.executable)
            })
    });
    let (mut instance, mut child) = if let Some(instance) = saved.or_else(|| find_process(plan)) {
        (instance, None)
    } else {
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(plan.profile_dir.join("desktop.log"))
            .map_err(|e| e.to_string())?;
        let error_log = log.try_clone().map_err(|e| e.to_string())?;
        let mut command = Command::new(&plan.executable);
        command
            .args(&plan.arguments)
            .current_dir(&plan.profile_dir)
            .stdin(Stdio::null())
            .stdout(log)
            .stderr(error_log)
            .creation_flags(CREATE_NO_WINDOW);
        for key in &plan.remove_environment {
            command.env_remove(key);
        }
        command.envs(&plan.environment);
        let child = command
            .spawn()
            .map_err(|e| format!("官方客户端启动失败：{e}"))?;
        let (born, executable) =
            fingerprint(child.id()).ok_or("客户端启动后立即退出，请检查桌面日志。")?;
        (
            Instance {
                pid: child.id(),
                born,
                executable,
                user_data: plan.user_data.clone(),
                windows: Vec::new(),
            },
            Some(child),
        )
    };
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let windows = process_windows(instance.pid);
        let visible: Vec<_> = windows
            .iter()
            .filter(|w| w.visible)
            .map(|w| w.handle)
            .collect();
        if windows.iter().any(|w| {
            w.primary
                && w.visible
                && w.rect.right - w.rect.left > 350
                && w.rect.bottom - w.rect.top > 250
        }) {
            instance.windows = visible;
            break;
        }
        if !windows.is_empty() && !instance.windows.is_empty() {
            break;
        }
        if !fingerprint(instance.pid).is_some_and(|(born, _)| born == instance.born) {
            if let Some(existing) = find_process(plan) {
                instance = existing;
                child = None;
                continue;
            }
            return Err("客户端已退出，可能尚未正确安装官方 Windows 版。".into());
        }
        if Instant::now() >= deadline {
            return Err(
                "客户端进程已启动，但 30 秒内未找到窗口。请查看 desktop.log 后点击重开。".into(),
            );
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let client = Client {
        instance,
        record: Some(record),
        _child: child,
    };
    client.save();
    Ok(client)
}

impl Client {
    pub fn fixture(child: Child) -> Result<Self> {
        let (born, executable) = fingerprint(child.id()).ok_or("测试窗口进程启动失败")?;
        Ok(Self {
            instance: Instance {
                pid: child.id(),
                born,
                executable,
                user_data: PathBuf::new(),
                windows: Vec::new(),
            },
            record: None,
            _child: Some(child),
        })
    }

    pub fn alive(&self) -> bool {
        fingerprint(self.instance.pid).is_some_and(|(born, path)| {
            born == self.instance.born && path_key(&path) == path_key(&self.instance.executable)
        })
    }

    fn save(&self) {
        if let Some(path) = &self.record {
            let _ = write_json(path, &self.instance);
        }
    }

    pub fn main_window(&self) -> Option<Window> {
        if !self.alive() {
            return None;
        }
        process_windows(self.instance.pid)
            .into_iter()
            .filter(|w| w.rect.right - w.rect.left > 200 && w.rect.bottom - w.rect.top > 100)
            .filter(|w| w.visible || self.instance.windows.contains(&w.handle) || w.primary)
            // Electron also owns large hidden, untitled helper windows. Prefer
            // the titled application window before comparing window areas.
            .max_by_key(|w| {
                (
                    w.primary,
                    w.visible,
                    i64::from(w.rect.right - w.rect.left) * i64::from(w.rect.bottom - w.rect.top),
                )
            })
    }

    pub fn show(&mut self, activate: bool) {
        if !self.alive() {
            return;
        }
        let windows = process_windows(self.instance.pid);
        let mut chosen: Vec<_> = windows
            .iter()
            .filter(|w| w.visible || self.instance.windows.contains(&w.handle))
            .map(|w| w.handle)
            .collect();
        if chosen.is_empty() {
            if let Some(window) = self.main_window() {
                chosen.push(window.handle);
            }
        }
        for handle in &chosen {
            let hwnd = *handle as HWND;
            unsafe {
                ShowWindowAsync(
                    hwnd,
                    if IsIconic(hwnd) != 0 {
                        SW_RESTORE
                    } else {
                        SW_SHOW
                    },
                );
            }
        }
        if activate {
            if let Some(main) = self.main_window() {
                unsafe {
                    SetForegroundWindow(main.handle as HWND);
                }
            }
        }
        if !chosen.is_empty() {
            self.instance.windows = chosen;
            self.save();
        }
    }

    pub fn hide(&mut self) {
        if !self.alive() {
            return;
        }
        let windows: Vec<_> = process_windows(self.instance.pid)
            .into_iter()
            .filter(|w| w.visible)
            .map(|w| w.handle)
            .collect();
        if !windows.is_empty() {
            self.instance.windows = windows.clone();
            self.save();
        }
        for handle in windows {
            unsafe {
                ShowWindowAsync(handle as HWND, SW_HIDE);
            }
        }
    }

    /// Only the self-test calls this, for a child started with --fixture.
    pub fn stop_fixture(&mut self) {
        if self.record.is_none() {
            if let Some(child) = &mut self._child {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

pub fn foreground_pid() -> u32 {
    let mut pid = 0;
    unsafe {
        GetWindowThreadProcessId(GetForegroundWindow(), &mut pid);
    }
    pid
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn command_lines_with_unicode_spaces_and_quotes_remain_single_arguments() {
        let command = r#""C:\Program Files\OpenAI\ChatGPT.exe" "--user-data-dir=C:\Users\张三\账号 1\desktop-data""#;
        let args = command_line_args(command);
        assert_eq!(
            args,
            vec![
                r"C:\Program Files\OpenAI\ChatGPT.exe",
                r"--user-data-dir=C:\Users\张三\账号 1\desktop-data"
            ]
        );
    }
}
