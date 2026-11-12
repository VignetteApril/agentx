// Emit the standard Windows .res format directly so native and cross builds
// embed the same icon without requiring a platform-specific resource compiler.
use std::{env, fs, path::PathBuf};

fn word(data: &[u8], offset: usize) -> u16 {
    u16::from_le_bytes(data[offset..offset + 2].try_into().unwrap())
}

fn dword(data: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes(data[offset..offset + 4].try_into().unwrap())
}

fn resource(output: &mut Vec<u8>, kind: u16, name: u16, data: &[u8]) {
    output.extend_from_slice(&(data.len() as u32).to_le_bytes());
    output.extend_from_slice(&32u32.to_le_bytes());
    for value in [u16::MAX, kind, u16::MAX, name] {
        output.extend_from_slice(&value.to_le_bytes());
    }
    output.extend_from_slice(&0u32.to_le_bytes()); // DataVersion
    let flags: u16 = if kind == 0 { 0 } else { 0x1030 }; // MOVEABLE | PURE | DISCARDABLE
    output.extend_from_slice(&flags.to_le_bytes());
    output.extend_from_slice(&0u16.to_le_bytes()); // Language neutral
    output.extend_from_slice(&0u32.to_le_bytes()); // Version
    output.extend_from_slice(&0u32.to_le_bytes()); // Characteristics
    output.extend_from_slice(data);
    output.resize(output.len().next_multiple_of(4), 0);
}

fn main() {
    println!("cargo:rerun-if-changed=assets/AgentX.ico");
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows") {
        return;
    }
    assert_eq!(env::var("CARGO_CFG_TARGET_ENV").as_deref(), Ok("msvc"));
    let icon = fs::read("assets/AgentX.ico").expect("read the checked-in AgentX icon");
    assert!(icon.len() >= 6 && word(&icon, 0) == 0 && word(&icon, 2) == 1);
    let count = word(&icon, 4);
    assert!(count > 0 && icon.len() >= 6 + usize::from(count) * 16);
    let mut output = Vec::new();
    resource(&mut output, 0, 0, &[]); // Required null resource header
    let mut group = icon[..6].to_vec();
    for index in 0..count {
        let entry = 6 + usize::from(index) * 16;
        let length = dword(&icon, entry + 8) as usize;
        let offset = dword(&icon, entry + 12) as usize;
        let image = icon
            .get(offset..offset.checked_add(length).unwrap())
            .expect("valid ICO image bounds");
        resource(&mut output, 3, index + 1, image); // RT_ICON
        group.extend_from_slice(&icon[entry..entry + 12]);
        group.extend_from_slice(&(index + 1).to_le_bytes());
    }
    resource(&mut output, 14, 1, &group); // RT_GROUP_ICON, application icon ID 1
    let path = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("agentx-icon.res");
    fs::write(&path, output).expect("write the Windows icon resource");
    println!("cargo:rustc-link-arg-bin=AgentX={}", path.display());
}
