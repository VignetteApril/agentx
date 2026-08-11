import importlib.util
import subprocess
import tempfile
import unittest
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agentxlib.theme import Theme
from agentxlib.ui import human_age, render_dashboard, render_statusline

MODULE_PATH = Path(__file__).resolve().parents[1] / "agentx.py"
spec = importlib.util.spec_from_file_location("agentx_module", MODULE_PATH)
agentx = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = agentx
spec.loader.exec_module(agentx)


class AgentXPureTests(unittest.TestCase):
    def test_session_name_is_stable(self):
        p = Path("/tmp/example-project")
        self.assertEqual(agentx.session_name(p), agentx.session_name(p))
        self.assertTrue(agentx.session_name(p).startswith("agentx-example-project-"))

    def test_different_projects_get_different_sessions(self):
        self.assertNotEqual(agentx.session_name(Path("/tmp/a")), agentx.session_name(Path("/tmp/b")))

    def test_profile_name_validation(self):
        self.assertTrue(agentx.valid_profile_name("codex-1"))
        self.assertTrue(agentx.valid_profile_name("agy.main"))
        self.assertFalse(agentx.valid_profile_name("bad name"))
        self.assertFalse(agentx.valid_profile_name("-bad"))

    def test_tmux_frame_color_validation(self):
        for value in ("colour51", "colour255", "#22d3ee", "default", "cyan"):
            self.assertTrue(agentx.valid_tmux_color(value))
        for value in ("colour256", "#12345", "fg=red", "red,bg=black", ""):
            self.assertFalse(agentx.valid_tmux_color(value))

    def test_codex_launch_command_uses_separate_home(self):
        p = agentx.Profile("codex-1", "codex", "codex", 0)
        cmd = agentx.launch_command(p, resume=False)
        self.assertIn("CODEX_HOME=", cmd)
        self.assertIn("codex-home", cmd)
        self.assertIn("Press Enter to resume", cmd)
        self.assertIn("read -r agentx_recovery_input", cmd)

    def test_codex_resume_command(self):
        p = agentx.Profile("codex-1", "codex", "codex", 0)
        cmd = agentx.launch_command(p, resume=True)
        self.assertIn("resume", cmd)
        self.assertIn("--last", cmd)

    def test_launch_wrapper_resumes_on_enter(self):
        p = agentx.Profile("agy", "agy", "/usr/bin/true", 0)
        proc = subprocess.run(
            ["/bin/sh", "-c", agentx.launch_command(p)],
            input="\n",
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.count("Press Enter to resume"), 2)

    def test_launch_wrapper_stops_on_eof(self):
        p = agentx.Profile("agy", "agy", "/usr/bin/false", 0)
        proc = subprocess.run(
            ["/bin/sh", "-c", agentx.launch_command(p)],
            input="",
            text=True,
            capture_output=True,
        )
        self.assertEqual(proc.returncode, 1)

    def test_existing_session_is_reconfigured_on_open(self):
        config = {"profiles": [{"name": "codex-1", "kind": "codex", "command": "codex"}], "settings": {}}
        original_exists = agentx.tmux_session_exists
        original_configure = agentx.configure_session
        calls = []
        try:
            agentx.tmux_session_exists = lambda name: True
            agentx.configure_session = lambda session, project, cfg: calls.append((session, project))
            result = agentx.create_session(Path("/tmp/existing"), config)
        finally:
            agentx.tmux_session_exists = original_exists
            agentx.configure_session = original_configure
        self.assertEqual(result, agentx.session_name(Path("/tmp/existing")))
        self.assertEqual(len(calls), 1)

    def test_session_configures_dead_pane_recovery(self):
        original_tmux = agentx.tmux
        original_argv0 = sys.argv[0]
        calls = []
        try:
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            sys.argv[0] = str(MODULE_PATH)
            agentx.configure_session("example", Path("/tmp/project"), {
                "profiles": [{"name": "codex-1", "kind": "codex", "command": "codex"}],
                "settings": {},
            })
        finally:
            agentx.tmux = original_tmux
            sys.argv[0] = original_argv0
        self.assertIn(
            ("set-option", "-t", "example", "remain-on-exit-format",
             "Agent exited (status #{pane_dead_status}). Press Ctrl+G R to resume this Agent."),
            calls,
        )
        recovery = next(call for call in calls if call[:4] == ("bind-key", "-T", "prefix", "R"))
        self.assertEqual(recovery[4:7],
                         ("if-shell", "-F", "#{m/r:^agentx-,#{session_name}}"))
        self.assertIn("recover", recovery[7])
        self.assertIn("#W", recovery[7])
        self.assertIn('--session "#{session_name}"', recovery[7])
        self.assertNotIn("/tmp/project", recovery[7])

    def test_session_adds_configurable_agent_frame(self):
        original_tmux = agentx.tmux
        calls = []
        try:
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            agentx.configure_session("example", Path("/tmp/project"), {
                "profiles": [],
                "settings": {"frame_color": "#22d3ee", "frame_background": "#111827"},
            })
        finally:
            agentx.tmux = original_tmux
        self.assertIn(
            ("set-window-option", "-t", "example:0", "pane-border-status", "top"), calls
        )
        frame_format = next(
            call[4] for call in calls
            if call[:4] == ("set-window-option", "-t", "example:0", "pane-border-format")
        )
        self.assertIn("#W", frame_format)
        self.assertIn("#22d3ee", frame_format)
        self.assertIn("#111827", frame_format)
        self.assertIn("project", frame_format)
        self.assertNotIn("AGENTX", frame_format)
        self.assertNotIn("pane_current_command", frame_format)
        self.assertNotIn("┏", frame_format)
        self.assertIn(
            ("set-window-option", "-t", "example:0", "pane-border-lines", "single"), calls
        )
        self.assertIn(
            ("set-option", "-t", "example", "status-justify", "absolute-centre"), calls
        )
        self.assertIn(
            ("set-option", "-t", "example", "window-status-style",
             "fg=colour252,bg=colour239"), calls
        )

    def test_session_frame_can_be_disabled(self):
        original_tmux = agentx.tmux
        calls = []
        try:
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            agentx.configure_session("example", Path("/tmp/project"), {
                "profiles": [], "settings": {"frame_enabled": False},
            })
        finally:
            agentx.tmux = original_tmux
        self.assertIn(
            ("set-window-option", "-t", "example:0", "pane-border-status", "off"), calls
        )

    def test_four_sided_frame_adds_input_disabled_helper_panes(self):
        original_tmux = agentx.tmux
        calls = []
        pane_ids = iter(("%2", "%3", "%4"))

        def fake_tmux(*parts, **kwargs):
            calls.append(parts)
            if parts[0] == "list-panes":
                return SimpleNamespace(returncode=0, stdout="%1\t1\t\n")
            if parts[0] == "split-window":
                return SimpleNamespace(returncode=0, stdout=next(pane_ids) + "\n")
            return SimpleNamespace(returncode=0, stdout="")

        try:
            agentx.tmux = fake_tmux
            agentx.configure_window_frame("example:1", True)
        finally:
            agentx.tmux = original_tmux

        self.assertIn(("set-option", "-p", "-t", "%1", "@agentx_role", "main"), calls)
        for pane_id, role in (("%2", "left"), ("%3", "right"), ("%4", "bottom")):
            self.assertIn(("set-option", "-p", "-t", pane_id, "@agentx_role", role), calls)
            self.assertIn(("select-pane", "-d", "-t", pane_id), calls)
        splits = [call for call in calls if call[0] == "split-window"]
        self.assertEqual(len(splits), 3)
        self.assertTrue(all(("-l", "1") == call[call.index("-l"):call.index("-l") + 2]
                            for call in splits))
        self.assertTrue(any(call[:4] == ("set-hook", "-w", "-t", "example:1")
                            and "after-select-pane" in call for call in calls))
        self.assertIn(("select-pane", "-t", "%1"), calls)

    def test_agent_commands_target_main_pane_inside_frame(self):
        original_tmux = agentx.tmux
        try:
            agentx.tmux = lambda *parts, **kwargs: SimpleNamespace(
                returncode=0,
                stdout="%2\t0\tleft\n%1\t1\tmain\n%3\t0\tright\n%4\t0\tbottom\n",
            )
            self.assertEqual(agentx.agent_pane_target("example:codex-1"), "%1")
        finally:
            agentx.tmux = original_tmux

    def test_status_spacing_centres_default_bar_between_transparent_rows(self):
        original_tmux = agentx.tmux
        calls = []

        def fake_tmux(*parts, **kwargs):
            calls.append(parts)
            if parts[0] == "show-options":
                return SimpleNamespace(returncode=0, stdout="DEFAULT STATUS FORMAT\n")
            return SimpleNamespace(returncode=0, stdout="")

        try:
            agentx.tmux = fake_tmux
            agentx.configure_status_spacing("example")
        finally:
            agentx.tmux = original_tmux

        self.assertIn(("set-option", "-t", "example", "status", "3"), calls)
        self.assertIn(
            ("set-option", "-t", "example", "status-format[0]", "#[bg=default]"), calls
        )
        self.assertIn(
            ("set-option", "-t", "example", "status-format[1]", "DEFAULT STATUS FORMAT"), calls
        )
        self.assertIn(
            ("set-option", "-t", "example", "status-format[2]", "#[bg=default]"), calls
        )

    def test_session_blocks_accidental_ctrl_d_but_allows_explicit_eof(self):
        original_tmux = agentx.tmux
        original_argv0 = sys.argv[0]
        calls = []
        try:
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            sys.argv[0] = str(MODULE_PATH)
            agentx.configure_session("example", Path("/tmp/project"), {"settings": {}})
        finally:
            agentx.tmux = original_tmux
            sys.argv[0] = original_argv0
        self.assertIn(
            ("bind-key", "-T", "root", "C-d", "if-shell", "-F",
             "#{m/r:^agentx-,#{session_name}}",
             "display-message 'Ctrl+D blocked to protect this Agent. Press Ctrl+G Ctrl+D to send EOF intentionally.'",
             "send-keys C-d"),
            calls,
        )
        self.assertIn(
            ("bind-key", "-T", "prefix", "C-d", "if-shell", "-F",
             "#{m/r:^agentx-,#{session_name}}", "send-keys C-d", ""),
            calls,
        )

    def test_session_supports_ctrl_space_agent_cycling(self):
        original_tmux = agentx.tmux
        original_argv0 = sys.argv[0]
        calls = []
        try:
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            sys.argv[0] = str(MODULE_PATH)
            agentx.configure_session("example", Path("/tmp/project"), {
                "profiles": [
                    {"name": "codex-1", "kind": "codex", "command": "codex"},
                    {"name": "codex-2", "kind": "codex", "command": "codex"},
                ],
                "settings": {},
            })
        finally:
            agentx.tmux = original_tmux
            sys.argv[0] = original_argv0
        self.assertIn(
            ("bind-key", "-T", "root", "M-1", "send-keys", "M-1"), calls
        )

        prefix_one = next(
            call for call in calls if call[:4] == ("bind-key", "-T", "prefix", "1")
        )
        self.assertIn("prompt-switch", prefix_one[7])
        self.assertIn("--target codex-1", prefix_one[7])
        self.assertEqual(prefix_one[8], "select-window -t :1")

        ctrl_space = next(
            call for call in calls if call[:4] == ("bind-key", "-T", "root", "C-Space")
        )
        self.assertEqual(ctrl_space[4:7], ("if-shell", "-F", "#{m/r:^agentx-,#{session_name}}"))
        self.assertIn("prompt-switch", ctrl_space[7])
        self.assertIn('--current "#{window_name}"', ctrl_space[7])
        self.assertIn('--client "#{client_name}"', ctrl_space[7])
        self.assertIn('--session "#{session_name}"', ctrl_space[7])
        self.assertNotIn("/tmp/project", ctrl_space[7])
        self.assertEqual(ctrl_space[8], "send-keys C-Space")

    def test_project_bindings_are_identical_across_projects(self):
        original_tmux = agentx.tmux
        config = {
            "profiles": [
                {"name": "codex-1", "kind": "codex", "command": "codex"},
                {"name": "codex-2", "kind": "codex", "command": "codex"},
            ],
            "settings": {},
        }
        calls_a = []
        calls_b = []
        try:
            agentx.tmux = lambda *parts, **kwargs: calls_a.append(parts)
            agentx.configure_session("agentx-a", Path("/tmp/project-a"), config)
            agentx.tmux = lambda *parts, **kwargs: calls_b.append(parts)
            agentx.configure_session("agentx-b", Path("/tmp/project b;safe"), config)
        finally:
            agentx.tmux = original_tmux

        def bindings(calls):
            return [call for call in calls if call and call[0] == "bind-key"]

        self.assertEqual(bindings(calls_a), bindings(calls_b))
        rendered = repr(bindings(calls_b))
        self.assertNotIn("/tmp/project-a", rendered)
        self.assertNotIn("/tmp/project b;safe", rendered)
        self.assertIn('#{session_name}', rendered)

    def test_runtime_project_resolves_and_validates_session_metadata(self):
        original_exists = agentx.tmux_session_exists
        original_project = agentx.project_for_session
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp).resolve()
            session = agentx.session_name(project)
            try:
                agentx.tmux_session_exists = lambda value: value == session
                agentx.project_for_session = lambda value: str(project)
                self.assertEqual(agentx.runtime_project(session=session), project)
                with self.assertRaises(agentx.AgentXError):
                    agentx.runtime_project(str(MODULE_PATH.parent), session)
                agentx.project_for_session = lambda value: "?"
                with self.assertRaises(agentx.AgentXError):
                    agentx.runtime_project(session=session)
            finally:
                agentx.tmux_session_exists = original_exists
                agentx.project_for_session = original_project

    def test_prompt_switch_builds_direct_tmux_menu_for_next_agent(self):
        project = MODULE_PATH.parent.resolve()
        session = agentx.session_name(project)
        args = type("Args", (), {
            "project": None,
            "session": session,
            "current": "codex-1",
            "client": "client-1",
            "target": None,
        })()
        original_load = agentx.load_config
        original_tmux = agentx.tmux
        original_exists = agentx.tmux_session_exists
        original_project = agentx.project_for_session
        calls = []
        try:
            agentx.load_config = lambda: {
                "profiles": [
                    {"name": "codex-1", "kind": "codex", "command": "codex"},
                    {"name": "codex-2", "kind": "codex", "command": "codex"},
                ],
                "settings": {},
            }
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            agentx.tmux_session_exists = lambda value: value == session
            agentx.project_for_session = lambda value: str(project)
            agentx.cmd_prompt_switch(args)
        finally:
            agentx.load_config = original_load
            agentx.tmux = original_tmux
            agentx.tmux_session_exists = original_exists
            agentx.project_for_session = original_project
        menu = calls[0]
        self.assertEqual(menu[:5], ("display-menu", "-c", "client-1", "-T",
                                    "Switch to codex-2: transfer Context Pack?"))
        self.assertIn("Yes — capture + transfer", menu)
        self.assertIn("No — switch only", menu)
        self.assertTrue(any("transfer codex-2" in part and "--defer-switch" in part for part in menu))
        self.assertIn(f"select-window -t {session}:codex-2", menu)

    def test_popup_switch_is_deferred_and_targets_agent_name(self):
        args = type("Args", (), {
            "profile": "codex-2",
            "project": str(MODULE_PATH.parent),
            "defer_switch": True,
        })()
        originals = {
            "load_config": agentx.load_config,
            "create_session": agentx.create_session,
            "active_agent": agentx.active_agent,
            "current_window_states": agentx.current_window_states,
            "capture_context_pack": agentx.capture_context_pack,
            "send_text": agentx.send_text,
            "mark_context_pack_transferred": agentx.mark_context_pack_transferred,
            "update_project": agentx.update_project,
            "tmux": agentx.tmux,
        }
        calls = []
        injections = []
        marked = []
        try:
            agentx.load_config = lambda: {
                "profiles": [
                    {"name": "codex-1", "kind": "codex", "command": "codex"},
                    {"name": "codex-2", "kind": "codex", "command": "codex"},
                ],
                "settings": {},
            }
            agentx.create_session = lambda *parts, **kwargs: "example-session"
            agentx.active_agent = lambda session: "codex-1"
            agentx.current_window_states = lambda session: [
                {"name": "codex-1", "dead": "0"}, {"name": "codex-2", "dead": "0"},
            ]
            agentx.capture_context_pack = lambda project, current, target: Path("/tmp/context")
            agentx.send_text = lambda target, prompt: injections.append((target, prompt))
            agentx.mark_context_pack_transferred = lambda directory, timestamp: marked.append(directory)
            agentx.update_project = lambda *parts, **kwargs: None
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            agentx.cmd_switch(args)
        finally:
            for name, value in originals.items():
                setattr(agentx, name, value)
        self.assertFalse(any(call[0] == "select-window" for call in calls))
        deferred = next(call for call in calls if call[:2] == ("run-shell", "-b"))
        self.assertIn("example-session:codex-2", deferred[2])
        self.assertIn("sleep 0.2", deferred[2])
        self.assertEqual(injections[0][0], "example-session:codex-2")
        self.assertIn("context-pack.md", injections[0][1])
        self.assertEqual(marked, [Path("/tmp/context")])

    def test_recover_only_respawns_a_dead_profile(self):
        args = type("Args", (), {"project": str(MODULE_PATH.parent), "profile": "codex-1"})()
        original_load = agentx.load_config
        original_exists = agentx.tmux_session_exists
        original_states = agentx.current_window_states
        original_respawn = agentx.respawn_profile
        original_tmux = agentx.tmux
        respawns = []
        messages = []
        try:
            agentx.load_config = lambda: {
                "profiles": [{"name": "codex-1", "kind": "codex", "command": "codex"}],
                "settings": {},
            }
            agentx.tmux_session_exists = lambda name: True
            agentx.current_window_states = lambda session: [
                {"index": "1", "name": "codex-1", "active": "1", "dead": "1", "command": ""}
            ]
            agentx.respawn_profile = lambda session, profile, project, resume: respawns.append(
                (session, profile.name, project, resume)
            )
            agentx.tmux = lambda *parts, **kwargs: messages.append(parts)
            agentx.cmd_recover(args)
        finally:
            agentx.load_config = original_load
            agentx.tmux_session_exists = original_exists
            agentx.current_window_states = original_states
            agentx.respawn_profile = original_respawn
            agentx.tmux = original_tmux
        self.assertEqual(len(respawns), 1)
        self.assertTrue(respawns[0][3])
        self.assertEqual(messages[0][0], "display-message")

    def test_recover_does_not_replace_a_running_profile(self):
        args = type("Args", (), {"project": str(MODULE_PATH.parent), "profile": "codex-1"})()
        original_load = agentx.load_config
        original_exists = agentx.tmux_session_exists
        original_states = agentx.current_window_states
        original_respawn = agentx.respawn_profile
        original_tmux = agentx.tmux
        respawns = []
        try:
            agentx.load_config = lambda: {
                "profiles": [{"name": "codex-1", "kind": "codex", "command": "codex"}],
                "settings": {},
            }
            agentx.tmux_session_exists = lambda name: True
            agentx.current_window_states = lambda session: [
                {"index": "1", "name": "codex-1", "active": "1", "dead": "0", "command": "codex"}
            ]
            agentx.respawn_profile = lambda *parts, **kwargs: respawns.append(parts)
            agentx.tmux = lambda *parts, **kwargs: None
            agentx.cmd_recover(args)
        finally:
            agentx.load_config = original_load
            agentx.tmux_session_exists = original_exists
            agentx.current_window_states = original_states
            agentx.respawn_profile = original_respawn
            agentx.tmux = original_tmux
        self.assertEqual(respawns, [])

    def test_dashboard_and_statusline_render_saved_context(self):
        workspace = {"git": {"branch": "feature/ui", "modified_files": ["a.py"],
                              "staged_files": ["b.py"], "untracked_files": ["c.py"]}}
        context = {"transfer": {"from_agent": "codex-1", "to_agent": "codex-2",
                                "created_at": None, "file": "context-pack.md"}}
        output = render_dashboard({"name": "demo"}, workspace, context, "", [], Theme(False), width=80)
        self.assertIn("AGENTX // CONTROL DECK", output)
        self.assertIn("PROJECT  demo", output)
        self.assertIn("SIGNAL   feature/ui  //  Δ 3", output)
        self.assertIn("codex-1 → codex-2", output)
        self.assertIn("context-pack.md", output)
        self.assertIn("1 modified · 1 staged · 1 untracked", output)
        self.assertEqual(render_statusline(workspace, context), "⌁ feature/ui  ◈ Δ3  ⟲ never")

        narrow = render_dashboard({"name": "a-very-long-project-name"}, workspace, context,
                                  "", [], Theme(False), width=48)
        self.assertTrue(all(len(line) <= 48 for line in narrow.splitlines()))
        self.assertNotIn("\033[", narrow)

    def test_human_age(self):
        from datetime import timezone
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(human_age("2026-01-01T11:58:00+00:00", now), "2m ago")

    def test_project_id_is_stable_and_path_specific(self):
        self.assertEqual(agentx.project_id("/tmp/a"), agentx.project_id(Path("/tmp/a")))
        self.assertNotEqual(agentx.project_id("/tmp/a"), agentx.project_id("/tmp/b"))

    def test_git_status_parser(self):
        modified, staged, untracked = agentx.parse_git_status([
            " M app/a.py", "A  app/b.py", "?? test/c.py", "MM both.py",
        ])
        self.assertEqual(modified, ["app/a.py", "both.py"])
        self.assertEqual(staged, ["app/b.py", "both.py"])
        self.assertEqual(untracked, ["test/c.py"])

    def test_terminal_output_is_cleaned_and_bounded(self):
        text = "old\n\x1b[31mred\x1b[0m\r\nlatest\x00"
        cleaned, truncated = agentx.clean_terminal_output(text, max_lines=2, max_bytes=1024)
        self.assertEqual(cleaned, "red\nlatest")
        self.assertTrue(truncated)
        cleaned, truncated = agentx.clean_terminal_output("a\n" + "中" * 20,
                                                           max_lines=10, max_bytes=12)
        self.assertLessEqual(len(cleaned.encode("utf-8")), 12)
        self.assertTrue(truncated)

    def test_context_pack_contains_deterministic_workspace_and_artifacts(self):
        from agentxlib.models import WorkspaceSnapshot
        snapshot = WorkspaceSnapshot(
            "/tmp/project", "2026-01-01T00:00:00+00:00", "main", "abc",
            [" M app.py"], ["app.py"], [], [], "1 file changed", ["abc initial"],
            False, 12, "codex-1", "/tmp/project",
        )
        markdown = agentx.format_context_pack(
            snapshot, source_agent="codex-1", target_agent="codex-2",
            created_at="2026-01-01T00:00:00+00:00", directory=Path("/tmp/context"),
            recent_output="user request", output_truncated=False,
        )
        self.assertIn("# AgentX Context Pack", markdown)
        self.assertIn("Source Agent: codex-1", markdown)
        self.assertIn("Target Agent: codex-2", markdown)
        self.assertIn(" M app.py", markdown)
        self.assertIn("/tmp/context/workspace.diff", markdown)
        self.assertIn("user request", markdown)

    def test_context_capture_is_local_and_writes_schema_v2(self):
        from agentxlib.models import WorkspaceSnapshot
        originals = {
            "STATE_DIR": agentx.STATE_DIR,
            "load_config": agentx.load_config,
            "tmux_session_exists": agentx.tmux_session_exists,
            "active_agent": agentx.active_agent,
            "capture_output": agentx.capture_output,
            "collect_workspace": agentx.collect_workspace,
            "send_text": agentx.send_text,
        }
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            snapshot = WorkspaceSnapshot(
                str(project), "2026-01-01T00:00:00+00:00", "main", "abc",
                [" M app.py"], ["app.py"], [], [], "1 file changed", [], False, 4,
                "codex-1", str(project),
            )
            try:
                agentx.STATE_DIR = Path(temp) / "state"
                agentx.load_config = lambda: {
                    "profiles": [
                        {"name": "codex-1", "kind": "codex", "command": "codex"},
                        {"name": "codex-2", "kind": "codex", "command": "codex"},
                    ],
                    "settings": {"recent_output_lines": 300, "max_diff_size": 1024},
                }
                agentx.tmux_session_exists = lambda session: True
                agentx.active_agent = lambda session: "codex-1"
                agentx.capture_output = lambda *parts, **kwargs: "\x1b[31mrecent request\x1b[0m"
                agentx.collect_workspace = lambda *parts, **kwargs: (snapshot, "diff", "")
                agentx.send_text = lambda *parts, **kwargs: self.fail(
                    "Context capture must not message the source Agent"
                )
                directory = agentx.capture_context_pack(project, "codex-1", "codex-2")
            finally:
                for name, value in originals.items():
                    setattr(agentx, name, value)
            context = agentx.read_json(directory / "context.json")
            self.assertEqual(context["version"], 2)
            self.assertEqual(context["transfer"]["from_agent"], "codex-1")
            self.assertEqual(context["transfer"]["to_agent"], "codex-2")
            self.assertEqual(context["transfer"]["status"], "captured")
            self.assertEqual((directory / "recent-output.txt").read_text(), "recent request")

    def test_transfer_does_not_switch_when_target_injection_fails(self):
        args = type("Args", (), {
            "profile": "codex-2", "project": str(MODULE_PATH.parent),
            "defer_switch": True,
        })()
        originals = {
            "load_config": agentx.load_config,
            "create_session": agentx.create_session,
            "active_agent": agentx.active_agent,
            "current_window_states": agentx.current_window_states,
            "capture_context_pack": agentx.capture_context_pack,
            "send_text": agentx.send_text,
            "update_project": agentx.update_project,
            "tmux": agentx.tmux,
        }
        updates = []
        tmux_calls = []
        try:
            agentx.load_config = lambda: {
                "profiles": [
                    {"name": "codex-1", "kind": "codex", "command": "codex"},
                    {"name": "codex-2", "kind": "codex", "command": "codex"},
                ], "settings": {},
            }
            agentx.create_session = lambda *parts, **kwargs: "example-session"
            agentx.active_agent = lambda session: "codex-1"
            agentx.current_window_states = lambda session: [
                {"name": "codex-1", "dead": "0"}, {"name": "codex-2", "dead": "0"},
            ]
            agentx.capture_context_pack = lambda *parts: Path("/tmp/context")
            agentx.send_text = lambda *parts: (_ for _ in ()).throw(
                agentx.AgentXError("injection failed")
            )
            agentx.update_project = lambda *parts, **kwargs: updates.append(kwargs)
            agentx.tmux = lambda *parts, **kwargs: tmux_calls.append(parts)
            with self.assertRaises(agentx.AgentXError):
                agentx.cmd_transfer(args)
        finally:
            for name, value in originals.items():
                setattr(agentx, name, value)
        self.assertEqual(updates, [])
        self.assertFalse(any(call[0] in {"select-window", "run-shell"} for call in tmux_calls))

    def test_diff_limit(self):
        outputs = {
            ("rev-parse", "--show-toplevel"): "/tmp/project\n",
            ("status", "--short"): " M a.py\n",
            ("diff",): "x" * 20,
            ("diff", "--cached"): "y" * 20,
            ("branch", "--show-current"): "main\n",
            ("rev-parse", "HEAD"): "abc\n",
            ("diff", "--stat"): "1 file changed\n",
            ("log", "-5", "--pretty=format:%h %s"): "abc initial\n",
        }
        def runner(project, args):
            return subprocess.CompletedProcess(args, 0, outputs.get(tuple(args), ""), "")
        snapshot, diff, staged = agentx.collect_workspace(Path("/tmp/project"), max_diff_size=30, runner=runner)
        self.assertTrue(snapshot.diff_truncated)
        self.assertEqual(snapshot.diff_size, 40)
        self.assertEqual((diff, staged), ("", ""))

    def test_non_git_workspace(self):
        def runner(project, args):
            return subprocess.CompletedProcess(args, 128, "", "not a repository")
        snapshot, diff, staged = agentx.collect_workspace(Path("/tmp/project"), runner=runner)
        self.assertIsNone(snapshot.git_root)
        self.assertIsNone(snapshot.branch)
        self.assertEqual((diff, staged), ("", ""))

    def test_context_history_preserves_previous_context_pack(self):
        from agentxlib.context import mark_context_pack_transferred, save_context_pack
        from agentxlib.models import WorkspaceSnapshot
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            project = Path(temp) / "project"
            project.mkdir()
            snapshot = WorkspaceSnapshot(str(project), datetime.now().isoformat(), None, None,
                                         [], [], [], [], "", [], False, 0)
            save_context_pack(state, project, "codex-1", "codex-2",
                              "2026-01-01T00:00:00+00:00", "first", snapshot,
                              "", "", "output one")
            save_context_pack(state, project, "codex-2", "codex-1",
                              "2026-01-01T00:01:00+00:00", "second", snapshot,
                              "", "", "output two")
            directory = agentx.project_dir(state, project)
            self.assertEqual((directory / "context-pack.md").read_text().strip(), "second")
            self.assertEqual(agentx.read_json(directory / "context.json")["version"], 2)
            mark_context_pack_transferred(directory, "2026-01-01T00:01:01+00:00")
            current_context = agentx.read_json(directory / "context.json")
            self.assertEqual(current_context["transfer"]["status"], "injected")
            self.assertEqual(current_context["transfer"]["transferred_at"],
                             "2026-01-01T00:01:01+00:00")
            archived = list((directory / "history").glob("*/context-pack.md"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_text().strip(), "first")


if __name__ == "__main__":
    unittest.main()
