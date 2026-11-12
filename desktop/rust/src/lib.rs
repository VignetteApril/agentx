//! Configuration and account isolation shared independently of the native UI.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub type Result<T> = std::result::Result<T, String>;
pub const DATA_ENV: &str = "CODEX_ELECTRON_USER_DATA_PATH";
pub const REMOVE_ENV: &[&str] = &[
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "CODEX_SQLITE_HOME",
    "ELECTRON_RUN_AS_NODE",
];

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Profile {
    pub name: String,
    pub kind: String,
    #[serde(default = "codex_command")]
    pub command: String,
    #[serde(default)]
    pub created_at: u64,
}

fn codex_command() -> String {
    "codex".into()
}

pub fn valid_profile_name(name: &str) -> bool {
    if name.is_empty()
        || name.len() > 64
        || !name.as_bytes()[0].is_ascii_alphanumeric()
        || !name
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
        || name.ends_with('.')
    {
        return false;
    }
    let first = name.split('.').next().unwrap_or("").to_ascii_uppercase();
    !matches!(
        first.as_str(),
        "CON" | "PRN" | "AUX" | "NUL" | "CONIN$" | "CONOUT$"
    ) && !(first.len() == 4
        && (first.starts_with("COM") || first.starts_with("LPT"))
        && matches!(first.as_bytes()[3], b'1'..=b'9'))
}

pub fn default_config() -> Value {
    json!({"version": 1, "profiles": [
        {"name": "codex-1", "kind": "codex", "command": "codex", "created_at": 0},
        {"name": "codex-2", "kind": "codex", "command": "codex", "created_at": 0}
    ], "settings": {}})
}

#[derive(Clone, Debug)]
pub struct Config {
    pub root: PathBuf,
    pub value: Value,
}

impl Config {
    /// A missing configuration is represented in memory until explicitly saved.
    pub fn load(root: &Path) -> Result<Self> {
        let root = absolute(root)?;
        let path = root.join("config.json");
        let value = match fs::read_to_string(&path) {
            Ok(text) => serde_json::from_str(text.trim_start_matches('\u{feff}'))
                .map_err(|e| format!("账号配置无法读取：{}：{e}", path.display()))?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => default_config(),
            Err(e) => return Err(format!("无法读取账号配置：{e}")),
        };
        if !value.is_object() || !value.get("profiles").is_some_and(Value::is_array) {
            return Err("config.json 必须包含 profiles 数组。".into());
        }
        let config = Self { root, value };
        config.codex_profiles()?;
        Ok(config)
    }

    pub fn codex_profiles(&self) -> Result<Vec<Profile>> {
        let mut names = HashSet::new();
        let mut profiles = Vec::new();
        for raw in self.value["profiles"]
            .as_array()
            .ok_or("profiles 必须是数组")?
        {
            if let Some(name) = raw.get("name").and_then(Value::as_str) {
                if !names.insert(name.to_ascii_lowercase()) {
                    return Err(format!("账号名在 Windows 上重名：{name}"));
                }
            }
            if raw.get("kind").and_then(Value::as_str) != Some("codex") {
                continue;
            }
            let profile: Profile = serde_json::from_value(raw.clone())
                .map_err(|e| format!("Codex 账号配置有误：{e}"))?;
            if !valid_profile_name(&profile.name) {
                return Err(format!("账号名不适用于 Windows：{}", profile.name));
            }
            profiles.push(profile);
        }
        Ok(profiles)
    }

    /// Preserve other agents and all unknown Python configuration settings.
    pub fn ensure_two_profiles(&mut self) -> Result<Vec<Profile>> {
        let mut profiles = self.codex_profiles()?;
        let mut changed = !self.root.join("config.json").is_file();
        let mut names: HashSet<String> = self.value["profiles"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|p| p["name"].as_str().map(str::to_ascii_lowercase))
            .collect();
        let mut next = 1;
        while profiles.len() < 2 {
            let name = loop {
                let name = format!("codex-{next}");
                next += 1;
                if names.insert(name.to_ascii_lowercase()) {
                    break name;
                }
            };
            let profile = Profile {
                name,
                kind: "codex".into(),
                command: codex_command(),
                created_at: now_secs(),
            };
            self.value["profiles"]
                .as_array_mut()
                .unwrap()
                .push(json!(profile));
            profiles.push(profile);
            changed = true;
        }
        if changed {
            self.save()?;
        }
        profiles.truncate(2);
        Ok(profiles)
    }

    pub fn saved_app(&self) -> Option<PathBuf> {
        self.value
            .get("settings")?
            .get("desktop_app_path")?
            .as_str()
            .map(PathBuf::from)
    }

    pub fn save_app(&mut self, app: &Path) -> Result<()> {
        if !self.value.get("settings").is_some_and(Value::is_object) {
            self.value["settings"] = json!({});
        }
        self.value["settings"]["desktop_app_path"] = json!(app);
        self.save()
    }

    pub fn save(&self) -> Result<()> {
        write_json(&self.root.join("config.json"), &self.value)
    }
}

pub fn absolute(path: &Path) -> Result<PathBuf> {
    if path.as_os_str().is_empty() {
        return Err("账号数据目录不能为空。".into());
    }
    if path.is_absolute() {
        Ok(path.to_owned())
    } else {
        std::env::current_dir()
            .map(|p| p.join(path))
            .map_err(|e| e.to_string())
    }
}

pub fn default_root() -> Result<PathBuf> {
    if let Some(value) = std::env::var_os("AGENTX_HOME") {
        return absolute(Path::new(&value));
    }
    let home = std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .ok_or("无法定位用户目录。请设置 AGENTX_HOME。")?;
    absolute(&PathBuf::from(home).join(".agentx"))
}

pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let parent = path.parent().ok_or("配置文件没有父目录")?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temp = parent.join(format!(".agentx-{}-{nonce}.tmp", std::process::id()));
    let operation = || -> Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temp)
            .map_err(|e| e.to_string())?;
        let data = serde_json::to_vec_pretty(value).map_err(|e| e.to_string())?;
        file.write_all(&data)
            .and_then(|_| file.write_all(b"\n"))
            .and_then(|_| file.sync_all())
            .map_err(|e| e.to_string())?;
        drop(file);
        fs::rename(&temp, path).map_err(|e| format!("无法保存 {}：{e}", path.display()))
    };
    let result = operation();
    if result.is_err() {
        let _ = fs::remove_file(temp);
    }
    result
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

fn reject_link(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(meta) => {
            #[cfg(windows)]
            let reparse = {
                use std::os::windows::fs::MetadataExt;
                meta.file_attributes() & 0x400 != 0
            };
            #[cfg(not(windows))]
            let reparse = false;
            if meta.file_type().is_symlink() || reparse {
                return Err(format!(
                    "账号隔离目录不能是符号链接或目录联接：{}",
                    path.display()
                ));
            }
            if !meta.is_dir() {
                return Err(format!("账号目录被文件占用：{}", path.display()));
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.to_string()),
    }
    Ok(())
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProfilePaths {
    pub profile_dir: PathBuf,
    pub codex_home: PathBuf,
    pub user_data: PathBuf,
}

impl ProfilePaths {
    pub fn new(root: &Path, name: &str) -> Result<Self> {
        if !valid_profile_name(name) {
            return Err(format!("无效账号名：{name}"));
        }
        let profiles = absolute(root)?.join("profiles");
        let profile_dir = profiles.join(name);
        let paths = Self {
            codex_home: profile_dir.join("codex-home"),
            user_data: profile_dir.join("desktop-data"),
            profile_dir,
        };
        for path in [
            &profiles,
            &paths.profile_dir,
            &paths.codex_home,
            &paths.user_data,
        ] {
            reject_link(path)?;
        }
        Ok(paths)
    }

    pub fn prepare(&self) -> Result<()> {
        if let Some(profiles) = self.profile_dir.parent() {
            reject_link(profiles)?;
        }
        for path in [&self.profile_dir, &self.codex_home, &self.user_data] {
            reject_link(path)?;
            fs::create_dir_all(path).map_err(|e| e.to_string())?;
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Installation {
    pub executable: PathBuf,
    pub archive: PathBuf,
    pub machine: u16,
}

pub fn contains_token(path: &Path, token: &[u8]) -> Result<bool> {
    if token.is_empty() {
        return Ok(true);
    }
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut tail = Vec::new();
    let mut block = vec![0; 256 * 1024];
    loop {
        let size = file.read(&mut block).map_err(|e| e.to_string())?;
        if size == 0 {
            return Ok(false);
        }
        tail.extend_from_slice(&block[..size]);
        if tail.windows(token.len()).any(|x| x == token) {
            return Ok(true);
        }
        let keep = token.len().saturating_sub(1).min(tail.len());
        tail.drain(..tail.len() - keep);
    }
}

pub fn pe_machine(path: &Path) -> Result<u16> {
    let mut file = File::open(path).map_err(|e| e.to_string())?;
    let mut header = [0; 64];
    file.read_exact(&mut header)
        .map_err(|e| format!("客户端 EXE 文件不完整：{e}"))?;
    if &header[..2] != b"MZ" {
        return Err("选中的文件不是 Windows EXE。".into());
    }
    let offset = u32::from_le_bytes(header[60..64].try_into().unwrap()) as u64;
    if !(64..=16 * 1024 * 1024).contains(&offset) {
        return Err("客户端 EXE 的 PE 头无效。".into());
    }
    file.seek(SeekFrom::Start(offset))
        .map_err(|e| e.to_string())?;
    let mut signature = [0; 6];
    file.read_exact(&mut signature).map_err(|e| e.to_string())?;
    if &signature[..4] != b"PE\0\0" {
        return Err("客户端 EXE 的 PE 签名无效。".into());
    }
    Ok(u16::from_le_bytes([signature[4], signature[5]]))
}

impl Installation {
    pub fn inspect(executable: &Path) -> Result<Self> {
        let executable = absolute(executable)?;
        if !executable.is_file() {
            return Err(format!("客户端不存在：{}", executable.display()));
        }
        let name = executable
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .to_ascii_lowercase();
        if !matches!(name.as_str(), "chatgpt.exe" | "codex.exe") {
            return Err("请选择官方客户端的 ChatGPT.exe 或 Codex.exe。".into());
        }
        let machine = pe_machine(&executable)?;
        if machine != 0x8664 && machine != 0xaa64 {
            return Err("客户端需要是 Windows x64 或 ARM64 版本。".into());
        }
        let parent = executable.parent().ok_or("客户端路径无效")?;
        let archive = parent.join("resources/app.asar");
        if !archive.is_file()
            || !contains_token(&archive, DATA_ENV.as_bytes())?
            || !contains_token(&archive, b"CODEX_HOME")?
        {
            return Err("该客户端版本未找到账号隔离入口，已停止启动以避免共用登录信息。".into());
        }
        Ok(Self {
            executable,
            archive,
            machine,
        })
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct LaunchPlan {
    pub app: PathBuf,
    pub executable: PathBuf,
    pub profile_dir: PathBuf,
    pub codex_home: PathBuf,
    pub user_data: PathBuf,
    pub environment: BTreeMap<String, String>,
    pub remove_environment: Vec<String>,
    pub arguments: Vec<String>,
}

impl LaunchPlan {
    pub fn new(root: &Path, profile: &Profile, installation: &Installation) -> Result<Self> {
        let paths = ProfilePaths::new(root, &profile.name)?;
        let environment = BTreeMap::from([
            (
                "CODEX_HOME".into(),
                paths.codex_home.to_string_lossy().into_owned(),
            ),
            (
                DATA_ENV.into(),
                paths.user_data.to_string_lossy().into_owned(),
            ),
        ]);
        Ok(Self {
            app: installation.executable.clone(),
            executable: installation.executable.clone(),
            arguments: vec![format!("--user-data-dir={}", paths.user_data.display())],
            profile_dir: paths.profile_dir,
            codex_home: paths.codex_home,
            user_data: paths.user_data,
            environment,
            remove_environment: REMOVE_ENV.iter().map(|x| x.to_string()).collect(),
        })
    }

    pub fn paths(&self) -> ProfilePaths {
        ProfilePaths {
            profile_dir: self.profile_dir.clone(),
            codex_home: self.codex_home.clone(),
            user_data: self.user_data.clone(),
        }
    }
}

/// Stable across launches, unlike randomly seeded standard HashMap hashes.
pub fn root_id(root: &Path) -> String {
    let text = root.to_string_lossy().replace('/', "\\").to_lowercase();
    let mut hash = 0xcbf29ce484222325u64;
    for byte in text.bytes() {
        hash = (hash ^ u64::from(byte)).wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

/// Visibility policy is independent of OS handles and never stops processes.
#[derive(Debug, PartialEq, Eq)]
pub struct Switch {
    pub show: usize,
    pub hide: Vec<usize>,
}

pub fn switch_accounts(selected: usize, running: &[usize]) -> Result<Switch> {
    if selected >= 2 {
        return Err("只能选择账号 1 或账号 2。".into());
    }
    Ok(Switch {
        show: selected,
        hide: if running.contains(&selected) {
            running.iter().copied().filter(|x| *x != selected).collect()
        } else {
            Vec::new()
        },
    })
}
