#![cfg_attr(windows, windows_subsystem = "windows")]

#[cfg(windows)]
mod windows;

#[cfg(not(windows))]
use agentx_desktop::Installation;
use agentx_desktop::{default_root, Config, LaunchPlan, Result};
use std::path::PathBuf;

#[derive(Default, Debug)]
struct Options {
    root: Option<PathBuf>,
    app: Option<PathBuf>,
    plan: Option<String>,
    doctor: Option<PathBuf>,
    self_test: Option<PathBuf>,
    fixture: Option<usize>,
    help: bool,
}

fn options() -> Result<Options> {
    let mut options = Options::default();
    let mut args = std::env::args_os().skip(1);
    while let Some(arg) = args.next() {
        let text = arg.to_string_lossy();
        match text.as_ref() {
            "--home" => {
                options.root = Some(args.next().map(PathBuf::from).ok_or("--home 需要目录")?)
            }
            "--app" => {
                options.app = Some(
                    args.next()
                        .map(PathBuf::from)
                        .ok_or("--app 需要 EXE 路径")?,
                )
            }
            "--plan-json" => {
                options.plan = Some(
                    args.next()
                        .ok_or("--plan-json 需要账号名")?
                        .to_string_lossy()
                        .into_owned(),
                )
            }
            "--doctor" => {
                options.doctor = Some(
                    args.next()
                        .map(PathBuf::from)
                        .ok_or("--doctor 需要报告文件路径")?,
                )
            }
            "--self-test" => {
                options.self_test = Some(
                    args.next()
                        .map(PathBuf::from)
                        .ok_or("--self-test 需要报告文件路径")?,
                )
            }
            "--fixture" => {
                options.fixture = Some(
                    args.next()
                        .ok_or("缺少测试窗口编号")?
                        .to_string_lossy()
                        .parse()
                        .map_err(|_| "测试窗口编号无效")?,
                )
            }
            "--help" | "-h" => options.help = true,
            _ => return Err(format!("未知参数：{text}")),
        }
    }
    if usize::from(options.plan.is_some())
        + usize::from(options.doctor.is_some())
        + usize::from(options.self_test.is_some())
        + usize::from(options.fixture.is_some())
        > 1
    {
        return Err("启动计划、环境检查和窗口测试不能同时运行。".into());
    }
    Ok(options)
}

const HELP: &str = "AgentX Codex — Windows 原生账号外框\n\
  AgentX.exe                        打开两个独立账号的官方客户端\n\
  AgentX.exe --app <ChatGPT.exe>      指定已安装的官方客户端\n\
  AgentX.exe --home <目录>            指定 AgentX 配置目录\n\
  AgentX.exe --plan-json <账号名>     输出不含凭证的启动计划\n\
  AgentX.exe --doctor <报告.json>     检查安装和账号目录\n\
  AgentX.exe --self-test <报告.json>  用本地测试窗口验证切换，不连接账号\n";

fn run() -> Result<()> {
    let options = options()?;
    #[cfg(windows)]
    if options.help || options.plan.is_some() || options.doctor.is_some() {
        windows::attach_console();
    }
    if options.help {
        println!("{HELP}");
        return Ok(());
    }
    let root = options.root.clone().map(Ok).unwrap_or_else(default_root)?;
    if options.plan.is_some() || options.doctor.is_some() {
        let config = Config::load(&root)?;
        let candidate = options.app.clone().or_else(|| config.saved_app());
        #[cfg(windows)]
        let app = windows::discover(candidate.as_deref());
        #[cfg(not(windows))]
        let app = candidate
            .ok_or_else(|| "请通过 --app 指定 Windows 官方客户端 EXE。".to_string())
            .and_then(|p| Installation::inspect(&p));
        if let Some(profile_name) = options.plan {
            let profile = config
                .codex_profiles()?
                .into_iter()
                .find(|p| p.name == profile_name)
                .ok_or_else(|| format!("未找到账号：{profile_name}"))?;
            let plan = LaunchPlan::new(&root, &profile, &app?)?;
            println!(
                "{}",
                serde_json::to_string(&plan).map_err(|e| e.to_string())?
            );
        } else if let Some(path) = options.doctor {
            let report = serde_json::json!({
                "platform": std::env::consts::OS,
                "architecture": std::env::consts::ARCH,
                "root": config.root,
                "profiles": config.codex_profiles()?.iter().map(|p| &p.name).collect::<Vec<_>>(),
                "installation": app.as_ref().ok(),
                "installation_error": app.as_ref().err(),
                "runtime_validation": "Environment inspection only; no account requests or tasks were sent.",
            });
            agentx_desktop::write_json(&path, &report)?;
            println!("{}", path.display());
        }
        return Ok(());
    }
    #[cfg(windows)]
    {
        windows::run(root, options.app, options.self_test, options.fixture)
    }
    #[cfg(not(windows))]
    {
        Err("账号外框需要 Windows。共享核心可以在当前系统运行 cargo test。".into())
    }
}

fn main() {
    if let Err(error) = run() {
        eprintln!("AgentX: {error}");
        #[cfg(windows)]
        if !std::env::args().any(|arg| {
            matches!(
                arg.as_str(),
                "--plan-json" | "--doctor" | "--self-test" | "--fixture" | "--help" | "-h"
            )
        }) {
            windows::show_error(&error);
        }
        std::process::exit(1);
    }
}
