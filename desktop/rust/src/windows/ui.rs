//! Thin native window controller. Official client windows remain independent.
use super::{backend, discover, message, wide};
use agentx_desktop::{root_id, switch_accounts, write_json, Config, LaunchPlan, Profile, Result};
use std::cell::RefCell;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::ptr::{null, null_mut};
use std::sync::mpsc::{self, Receiver, Sender};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Graphics::Gdi::*;
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::SystemServices::{SS_CENTER, SS_LEFT};
use windows_sys::Win32::System::Threading::{CreateMutexW, GetCurrentProcessId};
use windows_sys::Win32::UI::Controls::Dialogs::*;
use windows_sys::Win32::UI::Controls::DRAWITEMSTRUCT;
use windows_sys::Win32::UI::HiDpi::*;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::ReleaseCapture;
use windows_sys::Win32::UI::Shell::*;
use windows_sys::Win32::UI::WindowsAndMessaging::*;

const TAB_1: u16 = 101;
const TAB_2: u16 = 102;
const REOPEN: u16 = 103;
const PICK: u16 = 104;
const CLOSE: u16 = 105;
const DETAILS: u16 = 106;
const SHOW_ALL: u16 = 107;
const INSTALL: u16 = 108;
const REATTACH: u16 = 109;
const MENU: u16 = 110;
const STATUS: i32 = 201;
const READY: u32 = WM_APP + 10;
const RESTORE_BAR: u32 = WM_APP + 11;
const TRAY: u32 = WM_APP + 12;
const UPDATE_DPI: u32 = WM_APP + 13;
const BACKGROUND: u32 = 0x001d1611; // COLORREF: #11161d
const TEXT: u32 = 0x00f0e9e2;
const ACCENT: u32 = 0x00e8d34a;

type WorkerResult = (usize, Result<backend::Client>);

struct State {
    config: Config,
    profiles: Vec<Profile>,
    app: Option<PathBuf>,
    clients: [Option<backend::Client>; 2],
    pending: [bool; 2],
    errors: [Option<String>; 2],
    selected: usize,
    tx: Sender<WorkerResult>,
    rx: Receiver<WorkerResult>,
    hwnd: HWND,
    font: HFONT,
    background: HBRUSH,
    manual_position: bool,
    switching: Option<Instant>,
}

thread_local! { static STATE: RefCell<Option<State>> = const { RefCell::new(None) }; }

impl State {
    fn start(&mut self, index: usize) {
        if self.pending[index] {
            return;
        }
        if self.clients[index].as_ref().is_some_and(|c| c.alive()) {
            self.select(index, true);
            return;
        }
        self.clients[index] = None;
        self.pending[index] = true;
        self.errors[index] = None;
        let root = self.config.root.clone();
        let profile = self.profiles[index].clone();
        let candidate = self.app.clone().or_else(|| self.config.saved_app());
        let tx = self.tx.clone();
        let hwnd = self.hwnd as usize;
        std::thread::spawn(move || {
            let result = discover(candidate.as_deref())
                .and_then(|installation| LaunchPlan::new(&root, &profile, &installation))
                .and_then(|plan| backend::launch(&plan));
            let _ = tx.send((index, result));
            // The receiver owns all UI state; worker threads only post a wakeup.
            unsafe {
                PostMessageW(hwnd as HWND, READY, 0, 0);
            }
        });
        self.update_status();
    }

    fn finish_jobs(&mut self) {
        while let Ok((index, result)) = self.rx.try_recv() {
            self.pending[index] = false;
            match result {
                Ok(mut client) => {
                    let duplicate = self
                        .clients
                        .iter()
                        .flatten()
                        .any(|c| c.alive() && c.instance.pid == client.instance.pid);
                    if duplicate {
                        self.errors[index] =
                            Some("官方客户端未创建独立账号实例，已停止管理该实例。".into());
                        continue;
                    }
                    if index != self.selected {
                        client.hide();
                    }
                    self.clients[index] = Some(client);
                    if index == self.selected {
                        self.select(index, true);
                    }
                }
                Err(error) => self.errors[index] = Some(error),
            }
        }
        self.update_status();
    }

    fn select(&mut self, index: usize, activate: bool) {
        if index > 1 {
            return;
        }
        self.selected = index;
        self.manual_position = false;
        self.switching = Some(Instant::now());
        let running: Vec<_> = self
            .clients
            .iter()
            .enumerate()
            .filter_map(|(i, c)| c.as_ref().filter(|c| c.alive()).map(|_| i))
            .collect();
        let policy = switch_accounts(index, &running).unwrap();
        // Show first so a delayed or closed selected client never hides all work.
        if self.clients[policy.show]
            .as_ref()
            .is_some_and(|c| c.alive())
        {
            let client = self.clients[policy.show].as_mut().unwrap();
            client.show(activate);
            for other in policy.hide {
                if let Some(client) = &mut self.clients[other] {
                    client.hide();
                }
            }
        } else {
            self.clients[index] = None;
            if !self.pending[index] {
                self.start(index);
            }
        }
        self.update_status();
        unsafe {
            ShowWindowAsync(self.hwnd, SW_SHOWNOACTIVATE);
            InvalidateRect(self.hwnd, null(), 0);
        }
        self.follow();
    }

    fn update_status(&self) {
        let index = self.selected;
        let text = if let Some(error) = &self.errors[index] {
            if error.contains("未找到") {
                "请安装官方客户端，或从菜单选择客户端 EXE".into()
            } else {
                format!("账号 {} 启动失败 · 菜单中可查看详情", index + 1)
            }
        } else if self.pending[index] {
            format!("账号 {} 正在启动…", index + 1)
        } else if self.clients[index].as_ref().is_some_and(|c| c.alive()) {
            format!("账号 {} 已启动 · 切换标签，后台继续运行", index + 1)
        } else {
            format!("账号 {} 已关闭 · 点击重开", index + 1)
        };
        unsafe {
            SetWindowTextW(GetDlgItem(self.hwnd, STATUS), wide(&text).as_ptr());
            for id in [TAB_1, TAB_2] {
                InvalidateRect(GetDlgItem(self.hwnd, i32::from(id)), null(), 0);
            }
        }
    }

    fn tick(&mut self) {
        self.finish_jobs();
        if self.pending[self.selected]
            || self.errors[self.selected].is_some()
            || !self.clients.iter().flatten().any(|c| c.alive())
        {
            unsafe {
                ShowWindowAsync(self.hwnd, SW_SHOWNOACTIVATE);
            }
            return;
        }
        let front = backend::foreground_pid();
        if front == unsafe { GetCurrentProcessId() } {
            return;
        }
        if self
            .switching
            .is_some_and(|t| t.elapsed() < Duration::from_millis(700))
        {
            self.follow();
            return;
        }
        if let Some(index) = self.clients.iter().position(|client| {
            client
                .as_ref()
                .is_some_and(|c| c.instance.pid == front && c.alive())
        }) {
            if index != self.selected {
                self.select(index, false);
            }
            unsafe {
                ShowWindowAsync(self.hwnd, SW_SHOWNOACTIVATE);
            }
            self.follow();
        } else {
            unsafe {
                ShowWindowAsync(self.hwnd, SW_HIDE);
            }
        }
    }

    fn follow(&self) {
        if self.manual_position {
            return;
        }
        let Some(window) = self.clients[self.selected]
            .as_ref()
            .and_then(|c| c.main_window())
        else {
            return;
        };
        unsafe {
            let monitor = MonitorFromWindow(window.handle as HWND, MONITOR_DEFAULTTONEAREST);
            let mut info: MONITORINFO = std::mem::zeroed();
            info.cbSize = std::mem::size_of::<MONITORINFO>() as u32;
            if GetMonitorInfoW(monitor, &mut info) == 0 {
                return;
            }
            let scale = GetDpiForWindow(window.handle as HWND).max(96) as i32;
            let height = 72 * scale / 96;
            let width = (window.rect.right - window.rect.left)
                .max(480 * scale / 96)
                .min(info.rcWork.right - info.rcWork.left);
            let x = window
                .rect
                .left
                .max(info.rcWork.left)
                .min(info.rcWork.right - width);
            let above = window.rect.top - height - 4;
            let below = window.rect.bottom + 4;
            let y = if above >= info.rcWork.top {
                above
            } else if below + height <= info.rcWork.bottom {
                below
            } else {
                info.rcWork.top
            };
            let mut current: RECT = std::mem::zeroed();
            GetWindowRect(self.hwnd, &mut current);
            if current.left != x
                || current.top != y
                || current.right - current.left != width
                || current.bottom - current.top != height
            {
                SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, width, height, SWP_NOACTIVATE);
            }
        }
    }

    fn restore_all(&mut self) {
        for client in self.clients.iter_mut().flatten() {
            client.show(false);
        }
    }
}

fn mutate(action: impl FnOnce(&mut State)) {
    STATE.with(|slot| {
        if let Ok(mut slot) = slot.try_borrow_mut() {
            if let Some(state) = slot.as_mut() {
                action(state);
            }
        }
    });
}

unsafe fn tray(hwnd: HWND, add: bool) {
    let mut data: NOTIFYICONDATAW = std::mem::zeroed();
    data.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
    data.hWnd = hwnd;
    data.uID = 1;
    data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
    data.uCallbackMessage = TRAY;
    data.hIcon = super::app_icon(true);
    let tip = wide("AgentX Codex · 独立账号切换");
    data.szTip[..tip.len()].copy_from_slice(&tip);
    Shell_NotifyIconW(if add { NIM_ADD } else { NIM_DELETE }, &data);
}

unsafe fn menu(hwnd: HWND) {
    let menu = CreatePopupMenu();
    for (id, text) in [
        (TAB_1, "账号 1"),
        (TAB_2, "账号 2"),
        (REOPEN, "重开所选账号"),
        (REATTACH, "贴近当前窗口"),
        (SHOW_ALL, "显示两个客户端"),
        (PICK, "选择官方客户端…"),
        (INSTALL, "下载官方 Windows 客户端"),
        (DETAILS, "启动问题详情…"),
        (CLOSE, "关闭外框，保留客户端运行"),
    ] {
        AppendMenuW(menu, MF_STRING, usize::from(id), wide(text).as_ptr());
    }
    let mut position = POINT { x: 0, y: 0 };
    GetCursorPos(&mut position);
    SetForegroundWindow(hwnd);
    let chosen = TrackPopupMenu(
        menu,
        TPM_RETURNCMD | TPM_RIGHTBUTTON,
        position.x,
        position.y,
        0,
        hwnd,
        null(),
    );
    DestroyMenu(menu);
    if chosen > 0 {
        PostMessageW(hwnd, WM_COMMAND, chosen as usize, 0);
    }
}

unsafe fn pick_app(hwnd: HWND) -> Option<PathBuf> {
    let mut path = vec![0u16; 32768];
    let filter = wide("官方客户端\0ChatGPT.exe;Codex.exe\0应用程序\0*.exe\0\0");
    let title = wide("选择已安装的官方 Codex 客户端");
    let mut dialog: OPENFILENAMEW = std::mem::zeroed();
    dialog.lStructSize = std::mem::size_of::<OPENFILENAMEW>() as u32;
    dialog.hwndOwner = hwnd;
    dialog.lpstrFilter = filter.as_ptr();
    dialog.lpstrFile = path.as_mut_ptr();
    dialog.nMaxFile = path.len() as u32;
    dialog.lpstrTitle = title.as_ptr();
    dialog.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR;
    if GetOpenFileNameW(&mut dialog) == 0 {
        return None;
    }
    let length = path.iter().position(|x| *x == 0).unwrap_or(path.len());
    Some(PathBuf::from(String::from_utf16_lossy(&path[..length])))
}

fn action(hwnd: HWND, id: u16) {
    // Modal operations execute outside a State borrow so reentrant messages can
    // keep painting and draining completed worker jobs without aliasing state.
    match id {
        MENU => unsafe { menu(hwnd) },
        PICK => {
            if let Some(path) = unsafe { pick_app(hwnd) } {
                match agentx_desktop::Installation::inspect(&path).and_then(super::compatible) {
                    Ok(_) => mutate(|state| {
                        if let Err(error) = state.config.save_app(&path) {
                            state.errors[state.selected] = Some(error);
                            return;
                        }
                        state.app = Some(path);
                        for index in 0..2 {
                            if !state.clients[index].as_ref().is_some_and(|c| c.alive()) {
                                state.start(index);
                            }
                        }
                    }),
                    Err(error) => message(hwnd, "客户端检查失败", &error),
                }
            }
        }
        DETAILS => {
            let text = STATE.with(|slot| {
                slot.borrow()
                    .as_ref()
                    .map(|state| {
                        (0..2)
                            .map(|index| {
                                format!(
                                    "账号 {}：{}",
                                    index + 1,
                                    state.errors[index].as_deref().unwrap_or(
                                        if state.clients[index].as_ref().is_some_and(|c| c.alive())
                                        {
                                            "已启动"
                                        } else {
                                            "尚未启动"
                                        }
                                    )
                                )
                            })
                            .collect::<Vec<_>>()
                            .join("\n\n")
                    })
                    .unwrap_or_default()
            });
            message(hwnd, "客户端启动情况", &text);
        }
        INSTALL => unsafe {
            ShellExecuteW(
                hwnd,
                wide("open").as_ptr(),
                wide(
                    "https://get.microsoft.com/installer/download/9PLM9XGG6VKS?cid=website_cta_psi",
                )
                .as_ptr(),
                null(),
                null(),
                SW_SHOWNORMAL,
            );
        },
        CLOSE => unsafe {
            DestroyWindow(hwnd);
        },
        _ => mutate(|state| match id {
            TAB_1 => state.select(0, true),
            TAB_2 => state.select(1, true),
            REOPEN => state.start(state.selected),
            REATTACH => {
                state.manual_position = false;
                state.select(state.selected, true);
            }
            SHOW_ALL => {
                state.restore_all();
                unsafe {
                    ShowWindowAsync(hwnd, SW_SHOWNOACTIVATE);
                }
            }
            _ => {}
        }),
    }
}

unsafe fn layout(hwnd: HWND) {
    let mut bounds: RECT = std::mem::zeroed();
    GetClientRect(hwnd, &mut bounds);
    let dpi = GetDpiForWindow(hwnd).max(96) as i32;
    let scale = |value| value * dpi / 96;
    let width = bounds.right;
    let brand = scale(80);
    let close = scale(32);
    let segment = (width - brand - close - scale(24)).max(100) / 2;
    for (id, x, y, width, height) in [
        (TAB_1 as i32, brand, scale(8), segment, scale(30)),
        (
            TAB_2 as i32,
            brand + segment + scale(2),
            scale(8),
            segment,
            scale(30),
        ),
        (
            CLOSE as i32,
            bounds.right - close - scale(8),
            scale(7),
            close,
            scale(30),
        ),
        (STATUS, scale(14), scale(46), width - scale(126), scale(18)),
        (
            REOPEN as i32,
            width - scale(110),
            scale(43),
            scale(52),
            scale(22),
        ),
        (
            MENU as i32,
            width - scale(54),
            scale(43),
            scale(40),
            scale(22),
        ),
    ] {
        SetWindowPos(
            GetDlgItem(hwnd, id),
            null_mut(),
            x,
            y,
            width,
            height,
            SWP_NOZORDER | SWP_NOACTIVATE,
        );
    }
    let region = CreateRoundRectRgn(
        0,
        0,
        bounds.right + 1,
        bounds.bottom + 1,
        scale(16),
        scale(16),
    );
    if SetWindowRgn(hwnd, region, 1) == 0 {
        DeleteObject(region);
    }
}

unsafe fn draw_button(draw: &DRAWITEMSTRUCT) {
    let selected = STATE
        .with(|slot| {
            slot.try_borrow()
                .ok()
                .and_then(|s| s.as_ref().map(|s| s.selected))
        })
        .unwrap_or(0);
    let tab = draw.CtlID == u32::from(TAB_1) || draw.CtlID == u32::from(TAB_2);
    let active = draw.CtlID == u32::from(TAB_1) + selected as u32;
    let brush = CreateSolidBrush(if tab && active {
        0x004e3c28
    } else {
        BACKGROUND
    });
    FillRect(draw.hDC, &draw.rcItem, brush);
    DeleteObject(brush);
    SetBkMode(draw.hDC, TRANSPARENT as i32);
    SetTextColor(draw.hDC, if tab && active { ACCENT } else { TEXT });
    let text = match draw.CtlID as u16 {
        TAB_1 => "账号 1",
        TAB_2 => "账号 2",
        CLOSE => "×",
        REOPEN => "重开",
        MENU => "···",
        _ => "",
    };
    let mut bounds = draw.rcItem;
    DrawTextW(
        draw.hDC,
        wide(text).as_ptr(),
        -1,
        &mut bounds,
        DT_CENTER | DT_VCENTER | DT_SINGLELINE,
    );
}

unsafe extern "system" fn window_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    match msg {
        WM_CREATE => 0,
        WM_MOUSEACTIVATE => MA_NOACTIVATE as isize,
        WM_COMMAND => {
            action(hwnd, (wparam & 0xffff) as u16);
            0
        }
        READY => {
            mutate(|s| s.finish_jobs());
            0
        }
        RESTORE_BAR => {
            mutate(|s| s.select(s.selected, true));
            0
        }
        WM_TIMER => {
            mutate(|s| s.tick());
            0
        }
        WM_ENTERSIZEMOVE => {
            mutate(|s| s.manual_position = true);
            0
        }
        WM_LBUTTONDOWN => {
            ReleaseCapture();
            SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION as usize, 0);
            0
        }
        WM_SIZE => {
            layout(hwnd);
            0
        }
        WM_DPICHANGED => {
            let suggested = &*(lparam as *const RECT);
            SetWindowPos(
                hwnd,
                null_mut(),
                suggested.left,
                suggested.top,
                suggested.right - suggested.left,
                suggested.bottom - suggested.top,
                SWP_NOACTIVATE | SWP_NOZORDER,
            );
            // SetWindowPos can deliver DPI messages synchronously while State
            // is borrowed. Defer font replacement to the next message turn.
            PostMessageW(hwnd, UPDATE_DPI, 0, 0);
            0
        }
        UPDATE_DPI => {
            let font = make_font(GetDpiForWindow(hwnd).max(96) as i32);
            for id in [
                TAB_1 as i32,
                TAB_2 as i32,
                CLOSE as i32,
                REOPEN as i32,
                MENU as i32,
                STATUS,
            ] {
                SendMessageW(GetDlgItem(hwnd, id), WM_SETFONT, font as usize, 1);
            }
            mutate(|s| {
                let old = s.font;
                s.font = font;
                DeleteObject(old);
            });
            layout(hwnd);
            0
        }
        WM_DRAWITEM => {
            draw_button(&*(lparam as *const DRAWITEMSTRUCT));
            1
        }
        WM_CTLCOLORSTATIC => {
            SetTextColor(wparam as HDC, 0x00b8a394);
            SetBkMode(wparam as HDC, TRANSPARENT as i32);
            STATE
                .with(|slot| {
                    slot.try_borrow()
                        .ok()
                        .and_then(|s| s.as_ref().map(|s| s.background as isize))
                })
                .unwrap_or_else(|| GetStockObject(BLACK_BRUSH) as isize)
        }
        WM_PAINT => {
            let mut paint: PAINTSTRUCT = std::mem::zeroed();
            let dc = BeginPaint(hwnd, &mut paint);
            let brush = CreateSolidBrush(BACKGROUND);
            FillRect(dc, &paint.rcPaint, brush);
            DeleteObject(brush);
            let dpi = GetDpiForWindow(hwnd).max(96) as i32;
            let mut bounds = RECT {
                left: 14 * dpi / 96,
                top: 7 * dpi / 96,
                right: 80 * dpi / 96,
                bottom: 39 * dpi / 96,
            };
            SetTextColor(dc, ACCENT);
            SetBkMode(dc, TRANSPARENT as i32);
            let previous_font = STATE.with(|slot| {
                slot.try_borrow().ok().and_then(|state| {
                    state
                        .as_ref()
                        .filter(|state| !state.font.is_null())
                        .map(|state| SelectObject(dc, state.font))
                })
            });
            DrawTextW(
                dc,
                wide("AgentX").as_ptr(),
                -1,
                &mut bounds,
                DT_LEFT | DT_VCENTER | DT_SINGLELINE,
            );
            if let Some(previous) = previous_font {
                SelectObject(dc, previous);
            }
            EndPaint(hwnd, &paint);
            0
        }
        TRAY => {
            match lparam as u32 {
                WM_LBUTTONUP => mutate(|s| s.select(s.selected, true)),
                WM_RBUTTONUP => menu(hwnd),
                _ => {}
            }
            0
        }
        WM_CONTEXTMENU => {
            menu(hwnd);
            0
        }
        WM_CLOSE => {
            DestroyWindow(hwnd);
            0
        }
        WM_DESTROY => {
            KillTimer(hwnd, 1);
            tray(hwnd, false);
            mutate(|s| s.restore_all());
            PostQuitMessage(0);
            0
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

unsafe fn register(name: &[u16], proc: WNDPROC) -> Result<()> {
    let mut class: WNDCLASSEXW = std::mem::zeroed();
    class.cbSize = std::mem::size_of::<WNDCLASSEXW>() as u32;
    class.lpfnWndProc = proc;
    class.hInstance = GetModuleHandleW(null());
    class.hCursor = LoadCursorW(null_mut(), IDC_ARROW);
    class.lpszClassName = name.as_ptr();
    class.hIcon = super::app_icon(false);
    class.hIconSm = super::app_icon(true);
    if RegisterClassExW(&class) == 0 {
        return Err(format!("无法创建原生窗口类：{}", GetLastError()));
    }
    Ok(())
}

unsafe fn make_font(dpi: i32) -> HFONT {
    CreateFontW(
        -13 * dpi / 96,
        0,
        0,
        0,
        FW_NORMAL as i32,
        0,
        0,
        0,
        DEFAULT_CHARSET as u32,
        OUT_DEFAULT_PRECIS as u32,
        CLIP_DEFAULT_PRECIS as u32,
        CLEARTYPE_QUALITY as u32,
        DEFAULT_PITCH as u32,
        wide("Segoe UI").as_ptr(),
    )
}

unsafe fn controls(hwnd: HWND) -> HFONT {
    let font = make_font(GetDpiForWindow(hwnd).max(96) as i32);
    for (id, text) in [
        (TAB_1, "账号 1"),
        (TAB_2, "账号 2"),
        (CLOSE, "×"),
        (REOPEN, "重开"),
        (MENU, "···"),
    ] {
        let child = CreateWindowExW(
            0,
            wide("BUTTON").as_ptr(),
            wide(text).as_ptr(),
            WS_CHILD | WS_VISIBLE | BS_OWNERDRAW as u32,
            0,
            0,
            100,
            30,
            hwnd,
            id as usize as HMENU,
            GetModuleHandleW(null()),
            null(),
        );
        SendMessageW(child, WM_SETFONT, font as usize, 1);
    }
    let label = CreateWindowExW(
        0,
        wide("STATIC").as_ptr(),
        wide("正在准备两个账号…").as_ptr(),
        WS_CHILD | WS_VISIBLE | SS_LEFT,
        14,
        46,
        300,
        18,
        hwnd,
        STATUS as usize as HMENU,
        GetModuleHandleW(null()),
        null(),
    );
    SendMessageW(label, WM_SETFONT, font as usize, 1);
    layout(hwnd);
    font
}

unsafe fn message_loop() -> Result<()> {
    let mut message: MSG = std::mem::zeroed();
    loop {
        let result = GetMessageW(&mut message, null_mut(), 0, 0);
        if result == 0 {
            return Ok(());
        }
        if result < 0 {
            return Err("Windows 消息循环失败。".into());
        }
        TranslateMessage(&message);
        DispatchMessageW(&message);
    }
}

pub fn run(config: Config, profiles: Vec<Profile>, app: Option<PathBuf>) -> Result<()> {
    unsafe {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        let id = root_id(&config.root);
        let class_name = wide(&format!("AgentXCodexController-{id}"));
        let lock_name = wide(&format!("Local\\AgentXCodex-{id}"));
        let lock = CreateMutexW(null(), 0, lock_name.as_ptr());
        if lock.is_null() {
            return Err("无法创建外框实例锁。".into());
        }
        if GetLastError() == ERROR_ALREADY_EXISTS {
            let existing = FindWindowW(class_name.as_ptr(), null());
            if !existing.is_null() {
                PostMessageW(existing, RESTORE_BAR, 0, 0);
            }
            CloseHandle(lock);
            return Ok(());
        }
        register(&class_name, Some(window_proc))?;
        let (tx, rx) = mpsc::channel();
        let background = CreateSolidBrush(BACKGROUND);
        STATE.with(|slot| {
            *slot.borrow_mut() = Some(State {
                config,
                profiles,
                app,
                clients: [None, None],
                pending: [false; 2],
                errors: [None, None],
                selected: 0,
                tx,
                rx,
                hwnd: null_mut(),
                font: null_mut(),
                background,
                manual_position: false,
                switching: None,
            })
        });
        let hwnd = CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST,
            class_name.as_ptr(),
            wide("AgentX Codex").as_ptr(),
            WS_POPUP | WS_CLIPCHILDREN,
            120,
            60,
            620,
            72,
            null_mut(),
            null_mut(),
            GetModuleHandleW(null()),
            null(),
        );
        if hwnd.is_null() {
            CloseHandle(lock);
            return Err("无法创建账号外框。".into());
        }
        let font = controls(hwnd);
        mutate(|s| {
            s.hwnd = hwnd;
            s.font = font;
        });
        tray(hwnd, true);
        ShowWindow(hwnd, SW_SHOWNOACTIVATE);
        SetTimer(hwnd, 1, 300, None);
        mutate(|s| {
            s.start(0);
            s.start(1);
        });
        let result = message_loop();
        STATE.with(|slot| {
            slot.borrow_mut().take();
        });
        DeleteObject(font);
        DeleteObject(background);
        CloseHandle(lock);
        result
    }
}

unsafe extern "system" fn fixture_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    match msg {
        WM_CLOSE => {
            DestroyWindow(hwnd);
            0
        }
        WM_DESTROY => {
            PostQuitMessage(0);
            0
        }
        _ => DefWindowProcW(hwnd, msg, wparam, lparam),
    }
}

fn hidden_fixture_window(pid: u32) -> HWND {
    unsafe {
        FindWindowW(
            wide(&format!("AgentXHiddenWindowFixture-{pid}")).as_ptr(),
            null(),
        )
    }
}

pub fn fixture(index: usize) -> Result<()> {
    unsafe {
        let class = wide(&format!("AgentXWindowFixture-{}", std::process::id()));
        register(&class, Some(fixture_proc))?;
        let hwnd = CreateWindowExW(
            0,
            class.as_ptr(),
            wide(&format!("AgentX 测试窗口 {}（不连接账号）", index + 1)).as_ptr(),
            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            180 + index as i32 * 120,
            180,
            800,
            480,
            null_mut(),
            null_mut(),
            GetModuleHandleW(null()),
            null(),
        );
        if hwnd.is_null() {
            return Err("测试窗口创建失败".into());
        }
        CreateWindowExW(
            0,
            wide("STATIC").as_ptr(),
            wide("窗口切换测试\r\n不连接 Codex，不提交任何请求").as_ptr(),
            WS_CHILD | WS_VISIBLE | SS_CENTER,
            100,
            150,
            600,
            120,
            hwnd,
            null_mut(),
            GetModuleHandleW(null()),
            null(),
        );
        ShowWindow(hwnd, SW_SHOW);
        let tool = CreateWindowExW(
            WS_EX_TOOLWINDOW,
            class.as_ptr(),
            wide("AgentX 测试附属窗口（不连接账号）").as_ptr(),
            WS_OVERLAPPEDWINDOW | WS_VISIBLE,
            560,
            240,
            260,
            140,
            hwnd,
            null_mut(),
            GetModuleHandleW(null()),
            null(),
        );
        if tool.is_null() {
            return Err("测试附属窗口创建失败".into());
        }
        // Match Electron's hidden, untitled helper: it is larger than the real
        // window and must never become the target for focus or bar placement.
        let helper_class = wide(&format!("AgentXHiddenWindowFixture-{}", std::process::id()));
        register(&helper_class, Some(fixture_proc))?;
        let helper = CreateWindowExW(
            0,
            helper_class.as_ptr(),
            wide("").as_ptr(),
            WS_OVERLAPPEDWINDOW,
            60,
            60,
            1000,
            700,
            null_mut(),
            null_mut(),
            GetModuleHandleW(null()),
            null(),
        );
        if helper.is_null() {
            return Err("测试隐藏辅助窗口创建失败".into());
        }
        message_loop()
    }
}

pub fn self_test(report: &Path) -> Result<()> {
    let mut clients = Vec::new();
    let mut operation = || -> Result<serde_json::Value> {
        for index in 0..2 {
            let child = Command::new(std::env::current_exe().map_err(|e| e.to_string())?)
                .args(["--fixture", &index.to_string()])
                .spawn()
                .map_err(|e| e.to_string())?;
            clients.push(backend::Client::fixture(child)?);
        }
        let deadline = Instant::now() + Duration::from_secs(12);
        while clients.iter().any(|c| {
            c.main_window().is_none()
                || hidden_fixture_window(c.instance.pid).is_null()
                || backend::process_windows(c.instance.pid)
                    .iter()
                    .filter(|w| w.visible)
                    .count()
                    < 2
        }) {
            if Instant::now() >= deadline {
                return Err("测试窗口没有就绪。".into());
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        clients[0].show(true);
        clients[1].hide();
        std::thread::sleep(Duration::from_millis(500));
        let first = clients[0].main_window().is_some_and(|w| w.visible)
            && !backend::process_windows(clients[1].instance.pid)
                .iter()
                .any(|w| w.visible);
        clients[1].show(true);
        clients[0].hide();
        std::thread::sleep(Duration::from_millis(500));
        let second = clients[1].main_window().is_some_and(|w| w.visible)
            && !backend::process_windows(clients[0].instance.pid)
                .iter()
                .any(|w| w.visible);
        clients[0].show(false);
        clients[1].show(false);
        std::thread::sleep(Duration::from_millis(200));
        let restored = clients
            .iter()
            .all(|c| c.main_window().is_some_and(|w| w.visible));
        // Windows can constrain initial window sizes on small CI/RDP desktops.
        // Identify the helper directly instead of assuming fixed dimensions.
        let hidden_helpers_remain_hidden = clients.iter().all(|client| {
            let helper = hidden_fixture_window(client.instance.pid);
            !helper.is_null() && unsafe { IsWindowVisible(helper) == 0 }
        });
        Ok(
            serde_json::json!({"first_tab":first, "second_tab":second, "restore_on_close":restored,
            "different_processes":clients[0].instance.pid != clients[1].instance.pid,
            "clients_remain_running":clients.iter().all(|c| c.alive()),
            "hidden_helpers_remain_hidden":hidden_helpers_remain_hidden,
            "application_icon_loaded":unsafe { !super::app_icon(false).is_null() && !super::app_icon(true).is_null() },
            "screen_width":unsafe { GetSystemMetrics(SM_CXSCREEN) },
            "screen_height":unsafe { GetSystemMetrics(SM_CYSCREEN) },
            "uses_official_client":false, "sends_account_requests":false}),
        )
    };
    let result = operation();
    for client in &mut clients {
        client.stop_fixture();
    }
    match result {
        Ok(value) => {
            write_json(report, &value)?;
            if [
                "first_tab",
                "second_tab",
                "restore_on_close",
                "different_processes",
                "clients_remain_running",
                "hidden_helpers_remain_hidden",
                "application_icon_loaded",
            ]
            .iter()
            .any(|key| value[*key] != true)
            {
                return Err(format!("窗口切换测试失败，报告：{}", report.display()));
            }
            Ok(())
        }
        Err(error) => {
            write_json(
                report,
                &serde_json::json!({"error":error,"uses_official_client":false}),
            )?;
            Err(error)
        }
    }
}
