#!/usr/bin/env python3
"""Build the Rust Windows x64 controller and a credential-free portable ZIP."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "desktop/rust"
TARGET = "x86_64-pc-windows-msvc"
VERSION = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (CRATE / "Cargo.toml").read_text()).group(1)


def run(args: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=CRATE, env=env, check=True)


def verify_executable(path: Path) -> None:
    with path.open("rb") as handle:
        header = handle.read(64)
        if len(header) != 64 or header[:2] != b"MZ":
            raise RuntimeError("Build output is not a Windows executable.")
        handle.seek(struct.unpack_from("<I", header, 60)[0])
        pe = handle.read(96)
    if pe[:4] != b"PE\0\0" or struct.unpack_from("<H", pe, 4)[0] != 0x8664:
        raise RuntimeError("Expected a Windows x64 PE executable.")
    if struct.unpack_from("<H", pe, 24)[0] != 0x20B:
        raise RuntimeError("Expected PE32+ output.")
    if struct.unpack_from("<H", pe, 92)[0] != 2:
        raise RuntimeError("Expected the Windows GUI subsystem (no console window).")
    verify_icon_resources(path.read_bytes())


def verify_icon_resources(data: bytes) -> None:
    pe = struct.unpack_from("<I", data, 60)[0]
    optional = pe + 24
    count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    sections = [struct.unpack_from("<IIII", data, optional + optional_size + i * 40 + 8)
                for i in range(count)]

    def offset(rva: int) -> int:
        for virtual_size, address, raw_size, raw in sections:
            if address <= rva < address + max(virtual_size, raw_size):
                return raw + rva - address
        raise RuntimeError("Invalid PE resource address.")

    resource_rva = struct.unpack_from("<I", data, optional + 112 + 2 * 8)[0]
    if not resource_rva:
        raise RuntimeError("The executable is missing the embedded AgentX icon.")
    base = offset(resource_rva)
    resources = {}

    def walk(relative: int, ids: tuple[int, ...] = ()) -> None:
        if len(ids) > 3:
            raise RuntimeError("Invalid PE resource directory depth.")
        directory = base + relative
        named, numbered = struct.unpack_from("<HH", data, directory + 12)
        if named + numbered > 1024:
            raise RuntimeError("Invalid PE resource directory size.")
        for index in range(named + numbered):
            name, child = struct.unpack_from("<II", data, directory + 16 + index * 8)
            if name & 0x80000000:
                continue
            key = ids + (name,)
            if child & 0x80000000:
                walk(child & 0x7FFFFFFF, key)
            else:
                address, size = struct.unpack_from("<II", data, base + child)
                start = offset(address)
                resources[key] = data[start:start + size]

    walk(0)
    group = next((value for key, value in resources.items() if key[:2] == (14, 1)), None)
    icon = (CRATE / "assets/AgentX.ico").read_bytes()
    if group is None or group[:6] != icon[:6]:
        raise RuntimeError("The executable's AgentX icon group does not match the source icon.")
    for index in range(struct.unpack_from("<H", icon, 4)[0]):
        entry = 6 + index * 16
        size, address = struct.unpack_from("<II", icon, entry + 8)
        group_entry = 6 + index * 14
        resource_id = struct.unpack_from("<H", group, group_entry + 12)[0]
        embedded = next((value for key, value in resources.items() if key[:2] == (3, resource_id)), None)
        if group[group_entry:group_entry + 12] != icon[entry:entry + 12] or embedded != icon[address:address + size]:
            raise RuntimeError("An embedded AgentX icon image differs from the source icon.")


def source_hash() -> str:
    digest = hashlib.sha256()
    paths = [CRATE / "Cargo.toml", CRATE / "Cargo.lock", CRATE / "build.rs",
             *sorted((CRATE / "src").rglob("*.rs")), *sorted((CRATE / "assets").glob("*"))]
    for path in paths:
        digest.update(path.relative_to(CRATE).as_posix().encode())
        content = path.read_bytes()
        if path.suffix in {".rs", ".toml", ".lock"}:
            content = content.replace(b"\r\n", b"\n")
        digest.update(content)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="Package an already-built Windows x64 executable.")
    parser.add_argument("--linker", type=Path, help="Optional cross linker, for example rust-lld.")
    parser.add_argument("--sysroot", type=Path, help="Optional xwin or POSIX-style Windows MSVC sysroot.")
    args = parser.parse_args()
    env = os.environ.copy()
    run(["cargo", "fmt", "--", "--check"])
    run(["cargo", "test", "--locked"])
    if args.binary:
        binary = args.binary.resolve()
    else:
        flags = ["-C", "target-feature=+crt-static"]
        if args.sysroot:
            root = args.sysroot.resolve()
            suffixes = (["lib/x86_64-unknown-windows-msvc"]
                        if (root / "lib/x86_64-unknown-windows-msvc").is_dir() else
                        ["crt/lib/x86_64", "sdk/lib/um/x86_64", "sdk/lib/ucrt/x86_64"])
            for suffix in suffixes:
                folder = root / suffix
                if not folder.is_dir():
                    raise RuntimeError(f"Windows SDK library directory missing: {folder}")
                flags += ["-L", f"native={folder}"]
        if args.linker:
            env["CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER"] = str(args.linker.resolve())
        run(["cargo", "build", "--locked", "--release", "--target", TARGET,
             "--config", f"target.{TARGET}.rustflags=" + json.dumps(flags)], env=env)
        binary = CRATE / "target" / TARGET / "release/AgentX.exe"
    verify_executable(binary)
    destination = ROOT / ".build/windows/AgentX-Windows-x64"
    destination.mkdir(parents=True, exist_ok=True)
    staged = destination / "AgentX.exe"
    if binary.resolve() != staged.resolve():
        shutil.copy2(binary, staged)
    runtime_check = {"status": "not_run", "reason": "The build host is not Windows."}
    if platform.system() == "Windows":
        report = destination / "window-test.json"
        completed = subprocess.run([str(staged), "--self-test", str(report)], timeout=30)
        checks = json.loads(report.read_text(encoding="utf-8-sig"))
        required = ["first_tab", "second_tab", "restore_on_close", "different_processes", "clients_remain_running", "hidden_helpers_remain_hidden", "application_icon_loaded"]
        if completed.returncode or not all(checks.get(key) is True for key in required):
            raise RuntimeError(f"Native window validation failed: {checks}")
        runtime_check = {"status": "passed", "scope": "local fixture windows; no official account requests", "checks": checks}
    readme = ROOT / "desktop/rust/WINDOWS.md"
    shutil.copy2(readme, destination / "使用说明.md")
    shutil.copy2(CRATE / "assets/AgentX.ico", destination / "AgentX.ico")
    shutil.copy2(CRATE / "create-desktop-shortcut.ps1", destination / "create-desktop-shortcut.ps1")
    (destination / "创建桌面快捷方式.cmd").write_bytes(
        ('@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0create-desktop-shortcut.ps1"\r\n'
         'if errorlevel 1 (echo Shortcut creation failed. & pause & exit /b 1)\r\n'
         'echo Desktop shortcut created.\r\npause\r\n').encode("ascii"))
    (destination / "检查环境.cmd").write_bytes(
        ('@echo off\r\ncd /d "%~dp0"\r\n'
        'start "" /wait "%~dp0AgentX.exe" --doctor "%~dp0diagnostics.json"\r\n'
        'if errorlevel 1 (echo Environment check failed. & pause & exit /b 1)\r\n'
        'echo Report saved to diagnostics.json\r\npause\r\n').encode("ascii"))
    (destination / "测试窗口切换.cmd").write_bytes(
        ('@echo off\r\ncd /d "%~dp0"\r\n'
        'start "" /wait "%~dp0AgentX.exe" --self-test "%~dp0window-test.json"\r\n'
        'if errorlevel 1 (echo Window test failed. & pause & exit /b 1)\r\n'
        'echo Report saved to window-test.json\r\npause\r\n').encode("ascii"))
    metadata = {"version": VERSION, "target": TARGET, "build_host": platform.system(),
        "source_commit": os.environ.get("GITHUB_SHA"),
        "binary_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(), "source_sha256": source_hash(),
        "icon_sha256": hashlib.sha256((destination / "AgentX.ico").read_bytes()).hexdigest(),
        "runtime_validation": runtime_check,
        "official_client_login_validation": "not performed by this build script",
        "contains_account_credentials": False}
    (destination / "build-info.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    archive = destination.parent / f"AgentX-Windows-x64-{VERSION}.zip"
    files = [staged, destination / "使用说明.md", destination / "检查环境.cmd",
             destination / "测试窗口切换.cmd", destination / "build-info.json", destination / "AgentX.ico",
             destination / "create-desktop-shortcut.ps1", destination / "创建桌面快捷方式.cmd"]
    if runtime_check["status"] == "passed":
        files.append(destination / "window-test.json")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in files:
            output.write(path, "AgentX-Windows-x64/" + path.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"EXE: {staged}\nZIP: {archive}\nSHA256: {digest}")


if __name__ == "__main__":
    main()
