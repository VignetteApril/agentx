use agentx_desktop::*;
use serde_json::json;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        static NEXT: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "agentx-rust-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn fake_app(root: &Path, machine: u16, markers: &[u8]) -> PathBuf {
    fs::create_dir_all(root.join("resources")).unwrap();
    let mut data = vec![0; 128];
    data[..2].copy_from_slice(b"MZ");
    data[60..64].copy_from_slice(&64u32.to_le_bytes());
    data[64..68].copy_from_slice(b"PE\0\0");
    data[68..70].copy_from_slice(&machine.to_le_bytes());
    let exe = root.join("ChatGPT.exe");
    fs::write(&exe, data).unwrap();
    fs::write(root.join("resources/app.asar"), markers).unwrap();
    exe
}

#[test]
fn names_reject_traversal_reserved_devices_and_trailing_periods() {
    for name in [
        "", "../x", "a/b", "a\\b", ".", "..", "a ", "a.", "CON", "nul.txt", "COM1", "Lpt9.log",
        "-bad", "bad name",
    ] {
        assert!(!valid_profile_name(name), "{name}");
    }
    for name in ["codex-1", "codex_2", "work.main", "CONSOLE", "COM10", "a"] {
        assert!(valid_profile_name(name), "{name}");
    }
}

#[test]
fn plans_keep_both_state_directories_separate_and_do_not_create_files() {
    let temp = Temp::new();
    let exe = fake_app(
        &temp.0.join("official client 中文"),
        0x8664,
        b"CODEX_HOME CODEX_ELECTRON_USER_DATA_PATH",
    );
    let install = Installation::inspect(&exe).unwrap();
    let root = temp.0.join("state");
    let config = Config::load(&root).unwrap();
    let profiles = config.codex_profiles().unwrap();
    let a = LaunchPlan::new(&root, &profiles[0], &install).unwrap();
    let b = LaunchPlan::new(&root, &profiles[1], &install).unwrap();
    assert_ne!(a.codex_home, b.codex_home);
    assert_ne!(a.user_data, b.user_data);
    assert_eq!(a.environment.len(), 2);
    assert_eq!(a.environment[DATA_ENV], a.user_data.to_string_lossy());
    assert!(a.arguments[0].contains("--user-data-dir="));
    assert!(!root.exists());
    for key in REMOVE_ENV {
        assert!(a.remove_environment.contains(&key.to_string()));
    }
}

#[test]
fn configuration_preserves_other_agents_and_unknown_fields() {
    let temp = Temp::new();
    let original = json!({"version":1,"custom":{"x":42},"profiles":[
        {"name":"agy","kind":"agy","command":"custom-agy","extra":"keep"},
        {"name":"work","kind":"codex","command":"custom-codex","extra":"keep"}],
        "settings":{"memory_path":"vault","frame_color":"cyan"}});
    write_json(&temp.0.join("config.json"), &original).unwrap();
    let mut config = Config::load(&temp.0).unwrap();
    let profiles = config.ensure_two_profiles().unwrap();
    assert_eq!(profiles[0].name, "work");
    config.save_app(&temp.0.join("ChatGPT.exe")).unwrap();
    let again = Config::load(&temp.0).unwrap();
    assert_eq!(again.value["custom"], original["custom"]);
    assert_eq!(again.value["profiles"][0], original["profiles"][0]);
    assert_eq!(again.value["profiles"][1], original["profiles"][1]);
    assert_eq!(again.value["settings"]["memory_path"], "vault");
    assert_eq!(again.value["settings"]["frame_color"], "cyan");
    assert_eq!(again.codex_profiles().unwrap().len(), 2);
}

#[test]
fn existing_two_profiles_are_read_without_rewriting_config() {
    let temp = Temp::new();
    let text = serde_json::to_string(&default_config()).unwrap();
    fs::write(temp.0.join("config.json"), &text).unwrap();
    Config::load(&temp.0)
        .unwrap()
        .ensure_two_profiles()
        .unwrap();
    assert_eq!(
        fs::read_to_string(temp.0.join("config.json")).unwrap(),
        text
    );
}

#[test]
fn case_insensitive_profile_collisions_are_rejected() {
    let temp = Temp::new();
    let mut config = default_config();
    config["profiles"][1]["name"] = json!("CODEX-1");
    write_json(&temp.0.join("config.json"), &config).unwrap();
    assert!(Config::load(&temp.0).unwrap_err().contains("重名"));
}

#[test]
fn cross_agent_profile_name_collisions_are_rejected() {
    let temp = Temp::new();
    let config = json!({"profiles":[
        {"name":"work","kind":"codex","command":"codex"},
        {"name":"WORK","kind":"agy","command":"agy"}]});
    write_json(&temp.0.join("config.json"), &config).unwrap();
    assert!(Config::load(&temp.0).unwrap_err().contains("重名"));
}

#[test]
fn utf8_bom_configuration_can_be_read() {
    let temp = Temp::new();
    fs::write(
        temp.0.join("config.json"),
        format!("\u{feff}{}", default_config()),
    )
    .unwrap();
    assert_eq!(
        Config::load(&temp.0)
            .unwrap()
            .codex_profiles()
            .unwrap()
            .len(),
        2
    );
}

#[test]
fn malformed_configuration_is_preserved() {
    let temp = Temp::new();
    fs::write(temp.0.join("config.json"), "user's broken file").unwrap();
    assert!(Config::load(&temp.0).is_err());
    assert_eq!(
        fs::read_to_string(temp.0.join("config.json")).unwrap(),
        "user's broken file"
    );
}

#[test]
fn preparing_profiles_preserves_existing_authentication() {
    let temp = Temp::new();
    let paths = ProfilePaths::new(&temp.0, "codex-1").unwrap();
    paths.prepare().unwrap();
    let auth = paths.codex_home.join("auth.json");
    fs::write(&auth, "existing private login").unwrap();
    paths.prepare().unwrap();
    assert_eq!(fs::read_to_string(&auth).unwrap(), "existing private login");
    let second = ProfilePaths::new(&temp.0, "codex-2").unwrap();
    second.prepare().unwrap();
    assert!(!second.codex_home.join("auth.json").exists());
}

#[cfg(unix)]
#[test]
fn linked_profile_and_linked_auth_directory_are_rejected() {
    use std::os::unix::fs::symlink;
    let temp = Temp::new();
    let second = ProfilePaths::new(&temp.0, "codex-2").unwrap();
    second.prepare().unwrap();
    symlink(&second.profile_dir, temp.0.join("profiles/codex-1")).unwrap();
    assert!(ProfilePaths::new(&temp.0, "codex-1").is_err());
    fs::remove_file(temp.0.join("profiles/codex-1")).unwrap();
    fs::create_dir(temp.0.join("profiles/codex-1")).unwrap();
    symlink(
        &second.codex_home,
        temp.0.join("profiles/codex-1/codex-home"),
    )
    .unwrap();
    assert!(ProfilePaths::new(&temp.0, "codex-1").is_err());
}

#[test]
fn unsupported_client_is_rejected_before_any_profile_creation() {
    let temp = Temp::new();
    let exe = fake_app(&temp.0.join("official"), 0x8664, b"CODEX_HOME");
    assert!(Installation::inspect(&exe)
        .unwrap_err()
        .contains("隔离入口"));
    assert!(!temp.0.join("profiles").exists());
}

#[test]
fn invalid_pe_and_wrong_architecture_are_rejected() {
    let temp = Temp::new();
    let exe = fake_app(&temp.0, 0x14c, b"CODEX_HOME CODEX_ELECTRON_USER_DATA_PATH");
    assert!(Installation::inspect(&exe).is_err());
    fs::write(&exe, b"not an executable").unwrap();
    assert!(pe_machine(&exe).is_err());
}

#[test]
fn marker_matching_handles_block_boundaries() {
    let temp = Temp::new();
    let path = temp.0.join("archive");
    let mut bytes = vec![b'x'; 256 * 1024 - 7];
    bytes.extend_from_slice(DATA_ENV.as_bytes());
    fs::write(&path, bytes).unwrap();
    assert!(contains_token(&path, DATA_ENV.as_bytes()).unwrap());
    assert!(!contains_token(&path, b"not present").unwrap());
}

#[test]
fn switching_only_changes_visibility_and_excludes_the_selected_account() {
    assert_eq!(
        switch_accounts(0, &[0, 1]).unwrap(),
        Switch {
            show: 0,
            hide: vec![1]
        }
    );
    assert_eq!(
        switch_accounts(1, &[0, 1]).unwrap(),
        Switch {
            show: 1,
            hide: vec![0]
        }
    );
    assert_eq!(
        switch_accounts(1, &[0]).unwrap(),
        Switch {
            show: 1,
            hide: vec![]
        }
    );
    assert!(switch_accounts(2, &[0, 1]).is_err());
}

#[test]
fn root_identifier_is_case_and_separator_independent() {
    assert_eq!(
        root_id(Path::new("C:/Users/Someone/.agentx")),
        root_id(Path::new("c:\\users\\someone\\.agentx"))
    );
    assert_ne!(root_id(Path::new("C:/one")), root_id(Path::new("C:/two")));
}
