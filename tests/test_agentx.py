import importlib.util
import os
import subprocess
import tempfile
import unittest
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agentxlib.cockpit import (CockpitAction, CockpitAgent, CockpitState, _read_key,
                               reduce_cockpit_key, render_cockpit)
from agentxlib.theme import AGENTX_PALETTE, Theme
from agentxlib.ui import display_width, human_age, render_dashboard, render_statusline
from agentxlib.memory import add_memory, context_memory, format_memory_section, initialise_memory, search_memory

MODULE_PATH = Path(__file__).resolve().parents[1] / "agentx.py"
spec = importlib.util.spec_from_file_location("agentx_module", MODULE_PATH)
agentx = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = agentx
spec.loader.exec_module(agentx)


class AgentXPureTests(unittest.TestCase):
    def test_agentx_palette_contract_and_legacy_aliases(self):
        palette = AGENTX_PALETTE
        self.assertEqual(
            (palette.background, palette.surface, palette.border, palette.primary,
             palette.secondary, palette.text, palette.muted, palette.success,
             palette.warning, palette.danger),
            ("colour233", "colour234", "colour238", "colour51", "colour141",
             "colour252", "colour244", "colour48", "colour220", "colour203"),
        )
        self.assertEqual(palette.tmux_color("accent"), palette.primary)
        self.assertEqual(palette.tmux_color("panel"), palette.border)
        self.assertEqual(palette.tmux_color("bg"), palette.background)
        self.assertEqual(palette.tmux_color("bold"), palette.text)
        self.assertEqual(palette.tmux_color("inverse"), palette.primary)

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

    def test_cockpit_popup_is_fullscreen_and_tmux_32_compatible(self):
        parts = agentx.cockpit_popup_args()
        self.assertEqual(parts[:6],
                         ["display-popup", "-w", "100%", "-h", "100%", "-E"])
        self.assertEqual(len(parts), 7)
        command = parts[6]
        self.assertIn("cockpit", command)
        self.assertIn('--session "#{session_name}"', command)
        self.assertIn('--current "#{window_name}"', command)
        self.assertIn('--client "#{client_name}"', command)
        self.assertNotIn("-B", parts)

    def test_attach_queues_cockpit_outside_and_inside_tmux(self):
        original_exec = agentx.os.execvp
        original_tmux = agentx.tmux
        previous_tmux = os.environ.get("TMUX")
        exec_calls = []
        tmux_calls = []
        try:
            os.environ.pop("TMUX", None)
            agentx.os.execvp = lambda program, parts: exec_calls.append((program, parts))
            agentx.attach_session("agentx-demo", cockpit=True)

            os.environ["TMUX"] = "/tmp/tmux,1,0"
            agentx.tmux = lambda *parts, **kwargs: tmux_calls.append(parts)
            agentx.attach_session("agentx-demo", cockpit=True)
        finally:
            agentx.os.execvp = original_exec
            agentx.tmux = original_tmux
            if previous_tmux is None:
                os.environ.pop("TMUX", None)
            else:
                os.environ["TMUX"] = previous_tmux

        self.assertEqual(exec_calls[0][0], "tmux")
        self.assertEqual(exec_calls[0][1][:5],
                         ["tmux", "attach-session", "-t", "agentx-demo", ";"])
        self.assertEqual(exec_calls[0][1][5:11], agentx.cockpit_popup_args()[:6])
        self.assertEqual(tmux_calls[0][:4], ("switch-client", "-t", "agentx-demo", ";"))
        self.assertEqual(list(tmux_calls[0][4:10]), agentx.cockpit_popup_args()[:6])

    def test_deferred_cockpit_switch_targets_only_the_invoking_client(self):
        original_tmux = agentx.tmux
        calls = []
        try:
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            agentx.schedule_window_switch(
                "agentx-demo:codex-2", "Switched", "/dev/ttys009"
            )
            agentx.schedule_window_switch("agentx-demo:codex-1", "Switched")
        finally:
            agentx.tmux = original_tmux

        client_command = calls[0][2]
        self.assertIn(
            "tmux switch-client -c /dev/ttys009 -t agentx-demo:codex-2",
            client_command,
        )
        self.assertNotIn("select-window", client_command)
        self.assertIn("tmux select-window -t agentx-demo:codex-1", calls[1][2])

    def test_codex_launch_command_uses_separate_home(self):
        p = agentx.Profile("codex-1", "codex", "codex", 0)
        cmd = agentx.launch_command(p, resume=False)
        self.assertIn("CODEX_HOME=", cmd)
        self.assertIn("codex-home", cmd)
        self.assertIn("Press Enter to resume", cmd)
        self.assertIn("read -r agentx_recovery_input", cmd)
        self.assertIn("@agentx_state running", cmd)
        self.assertIn("@agentx_state exited", cmd)

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

    def test_open_defaults_to_cockpit_but_explicit_profile_bypasses_it(self):
        config = {
            "profiles": [
                {"name": "codex-1", "kind": "codex", "command": "codex"},
                {"name": "codex-2", "kind": "codex", "command": "codex"},
            ],
            "settings": {},
        }
        project = Path("/tmp/project")
        originals = {
            "load_config": agentx.load_config,
            "project_path": agentx.project_path,
            "create_session": agentx.create_session,
            "active_agent": agentx.active_agent,
            "update_project": agentx.update_project,
            "attach_session": agentx.attach_session,
        }
        attachments = []
        try:
            agentx.load_config = lambda: config
            agentx.project_path = lambda value=None: project
            agentx.create_session = lambda *parts, **kwargs: "example-session"
            agentx.active_agent = lambda session: "codex-1"
            agentx.update_project = lambda *parts, **kwargs: None
            agentx.attach_session = lambda session, window=None, cockpit=False: attachments.append(
                (session, window, cockpit)
            )
            agentx.cmd_open(SimpleNamespace(project=str(project), profile=None, resume=False))
            agentx.cmd_open(SimpleNamespace(
                project=str(project), profile="codex-2", resume=False,
            ))
        finally:
            for name, value in originals.items():
                setattr(agentx, name, value)

        self.assertEqual(attachments, [
            ("example-session", None, True),
            ("example-session", "codex-2", False),
        ])

    def test_cockpit_switch_updates_project_and_defers_window_selection(self):
        config = {
            "profiles": [
                {"name": "codex-1", "kind": "codex", "command": "codex"},
                {"name": "codex-2", "kind": "codex", "command": "codex"},
            ],
            "settings": {},
        }
        originals = {
            "runtime_project": agentx.runtime_project,
            "load_config": agentx.load_config,
            "current_window_states": agentx.current_window_states,
            "run_cockpit": agentx.run_cockpit,
            "update_project": agentx.update_project,
            "schedule_window_switch": agentx.schedule_window_switch,
        }
        updates = []
        switches = []
        try:
            agentx.runtime_project = lambda project=None, session=None: Path("/tmp/project")
            agentx.load_config = lambda: config
            agentx.current_window_states = lambda session: [
                {"name": "codex-1", "dead": "0"},
                {"name": "codex-2", "dead": "0"},
            ]
            agentx.run_cockpit = lambda *parts, **kwargs: CockpitAction(
                "switch", "codex-2"
            )
            agentx.update_project = lambda *parts, **kwargs: updates.append(kwargs)
            agentx.schedule_window_switch = lambda *parts: switches.append(parts)
            agentx.cmd_cockpit(SimpleNamespace(
                session="example", current="codex-1", client="/dev/ttys001",
            ))
        finally:
            for name, value in originals.items():
                setattr(agentx, name, value)

        self.assertEqual(updates[0]["previous_agent"], "codex-1")
        self.assertEqual(updates[0]["active_agent"], "codex-2")
        self.assertEqual(switches[0][0], "example:codex-2")
        self.assertEqual(switches[0][2], "/dev/ttys001")

    def test_cockpit_transfer_failure_reopens_with_notice(self):
        config = {
            "profiles": [
                {"name": "codex-1", "kind": "codex", "command": "codex"},
                {"name": "codex-2", "kind": "codex", "command": "codex"},
            ],
            "settings": {},
        }
        originals = {
            "runtime_project": agentx.runtime_project,
            "load_config": agentx.load_config,
            "current_window_states": agentx.current_window_states,
            "run_cockpit": agentx.run_cockpit,
            "cmd_transfer": agentx.cmd_transfer,
        }
        actions = iter([CockpitAction("transfer", "codex-2"), CockpitAction("close")])
        notices = []
        transfers = []
        try:
            agentx.runtime_project = lambda project=None, session=None: Path("/tmp/project")
            agentx.load_config = lambda: config
            agentx.current_window_states = lambda session: [
                {"name": "codex-1", "dead": "0"},
                {"name": "codex-2", "dead": "0"},
            ]
            def fake_cockpit(*parts, **kwargs):
                notices.append(kwargs.get("notice"))
                return next(actions)
            agentx.run_cockpit = fake_cockpit
            def fail_transfer(args):
                transfers.append(args)
                raise agentx.AgentXError("injection failed")
            agentx.cmd_transfer = fail_transfer
            agentx.cmd_cockpit(SimpleNamespace(
                session="example", current="codex-1", client="/dev/ttys001",
            ))
        finally:
            for name, value in originals.items():
                setattr(agentx, name, value)

        self.assertTrue(transfers[0].defer_switch)
        self.assertEqual(transfers[0].source, "codex-1")
        self.assertEqual(notices, [None, "Transfer failed: injection failed"])

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
             "Agent exited (status #{pane_dead_status}). Press Ctrl+G R to resume all exited Agents."),
            calls,
        )
        recovery = next(call for call in calls if call[:4] == ("bind-key", "-T", "prefix", "R"))
        self.assertEqual(recovery[4:7],
                         ("if-shell", "-F", "#{m/r:^agentx-,#{session_name}}"))
        self.assertIn("recover", recovery[7])
        self.assertIn("--all", recovery[7])
        self.assertIn('--session "#{session_name}"', recovery[7])
        self.assertNotIn("/tmp/project", recovery[7])

    def test_tmux_32_newer_cosmetic_options_are_best_effort(self):
        original_tmux = agentx.tmux
        calls = []

        def fake_tmux(*parts, **kwargs):
            calls.append((parts, kwargs))

        try:
            agentx.tmux = fake_tmux
            agentx.configure_session("example", Path("/tmp/project"), {
                "profiles": [],
                "settings": {},
            })
        finally:
            agentx.tmux = original_tmux

        optional = {
            "remain-on-exit-format",
            "popup-style",
            "popup-border-style",
            "menu-style",
            "menu-selected-style",
        }
        option_calls = {
            parts[3]: kwargs
            for parts, kwargs in calls
            if parts[:3] == ("set-option", "-t", "example") and len(parts) >= 5
        }
        for name in optional:
            self.assertEqual(option_calls[name], {"check": False, "capture": True})
        self.assertEqual(option_calls["status-style"], {})

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
        self.assertIn("╭── ", frame_format)
        self.assertIn("#{R:─,", frame_format)
        self.assertIn("#{pane_width}", frame_format)
        self.assertIn("#{w:window_name}", frame_format)
        self.assertIn("#{w:#{b:@agentx_project}}", frame_format)
        self.assertIn("╮", frame_format)
        self.assertIn("#{e|+:11,", frame_format)
        self.assertNotIn("AGENTX", frame_format)
        self.assertNotIn("pane_current_command", frame_format)
        self.assertNotIn("nobold", frame_format)
        self.assertNotIn("align=left,bg=", frame_format)
        self.assertNotIn("┏", frame_format)
        self.assertIn(
            ("set-window-option", "-t", "example:0", "pane-border-lines", "spaces"), calls
        )
        self.assertIn(
            ("set-window-option", "-t", "example:0", "pane-active-border-style",
             "fg=colour238,bg=#111827"), calls
        )
        self.assertIn(
            ("set-option", "-t", "example", "status-justify", "left"), calls
        )
        self.assertIn(
            ("set-option", "-t", "example", "window-status-style",
             "fg=colour244,bg=#20242e"), calls
        )
        self.assertIn(
            ("set-option", "-t", "example", "popup-border-style",
             "fg=colour233,bg=colour233"), calls
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

    def test_frame_does_not_add_decoration_panes(self):
        original_tmux = agentx.tmux
        calls = []

        def fake_tmux(*parts, **kwargs):
            calls.append(parts)
            if parts[0] == "list-panes":
                return SimpleNamespace(returncode=0, stdout="%1\t1\t\n")
            return SimpleNamespace(returncode=0, stdout="")

        try:
            agentx.tmux = fake_tmux
            agentx.configure_window_frame("example:1", True, "black")
        finally:
            agentx.tmux = original_tmux

        self.assertIn(("set-option", "-p", "-t", "%1", "@agentx_role", "main"), calls)
        self.assertIn(("set-window-option", "-t", "example:1", "window-style", "bg=black"), calls)
        self.assertIn(("set-option", "-p", "-t", "%1", "window-style", "bg=default"), calls)
        self.assertFalse(any(call[0] == "split-window" for call in calls))
        self.assertIn(("set-hook", "-wu", "-t", "example:1", "window-resized"), calls)
        self.assertIn(("set-hook", "-wu", "-t", "example:1", "after-select-pane"), calls)
        self.assertIn(("select-pane", "-t", "%1"), calls)

    def test_existing_gutters_are_removed_without_replacing_agent_pane(self):
        original_tmux = agentx.tmux
        calls = []

        def fake_tmux(*parts, **kwargs):
            calls.append(parts)
            if parts[0] == "list-panes":
                return SimpleNamespace(returncode=0, stdout=(
                    "%1\t1\tmain\n%2\t0\tleft\n%3\t0\tright\n%4\t0\tbottom\n"
                ))
            return SimpleNamespace(returncode=0, stdout="")

        try:
            agentx.tmux = fake_tmux
            agentx.configure_window_frame("example:1", True, "black")
        finally:
            agentx.tmux = original_tmux

        self.assertIn(("kill-pane", "-t", "%2"), calls)
        self.assertIn(("kill-pane", "-t", "%3"), calls)
        self.assertIn(("kill-pane", "-t", "%4"), calls)
        self.assertFalse(any(call[:2] == ("kill-pane", "-t") and call[2] == "%1"
                             for call in calls))
        self.assertFalse(any(call[0] == "split-window" for call in calls))

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

    def test_status_spacing_uses_a_single_line_footer(self):
        original_tmux = agentx.tmux
        calls = []
        try:
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            agentx.configure_status_spacing("example")
        finally:
            agentx.tmux = original_tmux

        self.assertEqual(calls, [("set-option", "-t", "example", "status", "on")])

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
        for profile in ("codex-1", "codex-2"):
            self.assertIn(
                ("set-window-option", "-t", f"example:{profile}",
                 "window-status-style", "fg=colour244,bg=#20242e"), calls
            )
            self.assertIn(
                ("set-window-option", "-t", f"example:{profile}",
                 "window-status-current-format", " #[fg=colour233]#I  #W "), calls
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

        cockpit = next(
            call for call in calls if call[:4] == ("bind-key", "-T", "prefix", "x")
        )
        self.assertEqual(cockpit[4:7],
                         ("if-shell", "-F", "#{m/r:^agentx-,#{session_name}}"))
        self.assertIn("display-popup -w 100% -h 100% -E", cockpit[7])
        self.assertIn('--session "#{session_name}"', cockpit[7])
        self.assertIn('--current "#{window_name}"', cockpit[7])
        self.assertIn('--client "#{client_name}"', cockpit[7])
        self.assertNotIn(" -B ", f" {cockpit[7]} ")
        self.assertEqual(cockpit[8], "")

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

    def test_missing_profile_windows_do_not_leak_tmux_errors(self):
        original_tmux = agentx.tmux
        calls = []
        config = {
            "profiles": [
                {"name": "codex-1", "kind": "codex", "command": "codex"},
                {"name": "codex-2", "kind": "codex", "command": "codex"},
                {"name": "agy", "kind": "agy", "command": "agy"},
            ],
            "settings": {},
        }
        try:
            def fake_tmux(*parts, **kwargs):
                calls.append((parts, kwargs))
                return SimpleNamespace(returncode=0, stdout="")

            agentx.tmux = fake_tmux
            agentx.configure_session("agentx-example", Path("/tmp/project"), config)
        finally:
            agentx.tmux = original_tmux

        profile_window_calls = [
            (parts, kwargs)
            for parts, kwargs in calls
            if parts[:3] == ("set-window-option", "-t", "agentx-example:codex-2")
            or parts[:3] == ("set-window-option", "-t", "agentx-example:agy")
        ]
        self.assertEqual(len(profile_window_calls), 10)
        self.assertTrue(all(kwargs.get("check") is False for _, kwargs in profile_window_calls))
        self.assertTrue(all(kwargs.get("capture") is True for _, kwargs in profile_window_calls))

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

    def test_recovery_prompt_detection_supports_pre_upgrade_sessions(self):
        original_target = agentx.agent_pane_target
        original_tmux = agentx.tmux
        calls = []
        try:
            agentx.agent_pane_target = lambda target: "%1"
            agentx.tmux = lambda *parts, **kwargs: (
                calls.append(parts) or SimpleNamespace(
                    returncode=0,
                    stdout=("Error details\n\nAgent exited (status 1). "
                            "Press Enter to resume, or Ctrl+C to\nstop.\n"),
                )
            )
            self.assertTrue(agentx.pane_waiting_for_recovery(
                "example", {"name": "codex-1", "dead": "0"},
            ))
            self.assertFalse(agentx.pane_waiting_for_recovery(
                "example", {"name": "codex-1", "dead": "0", "agentx_state": "running"},
            ))
        finally:
            agentx.agent_pane_target = original_target
            agentx.tmux = original_tmux

        self.assertEqual(len(calls), 1)

    def test_new_reapplies_session_style_after_recreating_a_window(self):
        args = type("Args", (), {
            "project": str(MODULE_PATH.parent), "profile": "codex-2", "resume": True,
        })()
        originals = {
            "load_config": agentx.load_config,
            "create_session": agentx.create_session,
            "respawn_profile": agentx.respawn_profile,
            "configure_session": agentx.configure_session,
            "attach_session": agentx.attach_session,
        }
        calls = []
        config = {
            "profiles": [{"name": "codex-2", "kind": "codex", "command": "codex"}],
            "settings": {},
        }
        try:
            agentx.load_config = lambda: config
            agentx.create_session = lambda *parts, **kwargs: "example-session"
            agentx.respawn_profile = lambda *parts, **kwargs: calls.append("respawn")
            agentx.configure_session = lambda *parts, **kwargs: calls.append("configure")
            agentx.attach_session = lambda *parts, **kwargs: calls.append("attach")
            agentx.cmd_new(args)
        finally:
            for name, value in originals.items():
                setattr(agentx, name, value)

        self.assertEqual(calls, ["respawn", "configure", "attach"])

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

    def test_recover_all_restores_dead_waiting_and_missing_agents_only(self):
        args = SimpleNamespace(
            project=str(MODULE_PATH.parent), session=None, profile=None, all=True,
        )
        config = {
            "profiles": [
                {"name": "dead", "kind": "codex", "command": "codex"},
                {"name": "waiting", "kind": "codex", "command": "codex"},
                {"name": "running", "kind": "codex", "command": "codex"},
                {"name": "missing", "kind": "agy", "command": "agy"},
            ],
            "settings": {},
        }
        originals = {
            "load_config": agentx.load_config,
            "tmux_session_exists": agentx.tmux_session_exists,
            "current_window_states": agentx.current_window_states,
            "respawn_profile": agentx.respawn_profile,
            "pane_waiting_for_recovery": agentx.pane_waiting_for_recovery,
            "agent_pane_target": agentx.agent_pane_target,
            "configure_session": agentx.configure_session,
            "tmux": agentx.tmux,
        }
        respawns = []
        calls = []
        configured = []
        try:
            agentx.load_config = lambda: config
            agentx.tmux_session_exists = lambda name: True
            agentx.current_window_states = lambda session: [
                {"name": "dead", "dead": "1"},
                {"name": "waiting", "dead": "0", "agentx_state": "exited"},
                {"name": "running", "dead": "0", "agentx_state": "running"},
            ]
            agentx.respawn_profile = lambda session, profile, project, resume: respawns.append(
                (profile.name, resume)
            )
            agentx.pane_waiting_for_recovery = lambda session, row: (
                row.get("agentx_state") == "exited"
            )
            agentx.agent_pane_target = lambda target: f"main:{target}"
            agentx.configure_session = lambda *parts: configured.append(parts)
            agentx.tmux = lambda *parts, **kwargs: calls.append(parts)
            agentx.cmd_recover(args)
        finally:
            for name, value in originals.items():
                setattr(agentx, name, value)

        self.assertEqual(respawns, [("dead", True), ("missing", True)])
        self.assertIn(
            ("send-keys", "-t", "main:" + agentx.session_name(MODULE_PATH.parent) + ":waiting",
             "Enter"),
            calls,
        )
        self.assertEqual(len(configured), 1)
        message = next(call for call in calls if call[0] == "display-message")
        self.assertIn("dead, waiting, missing", message[-1])
        self.assertNotIn("running", message[-1])

    def test_cockpit_renderer_is_bounded_adaptive_and_scrolls_unicode(self):
        agents = [
            CockpitAgent(f"profile-{index:02d}", "online", "CODEX")
            for index in range(11)
        ] + [CockpitAgent("协作代理十一", "dead", "AGY")]
        for width, height in ((80, 24), (48, 16), (40, 12)):
            with self.subTest(width=width, height=height):
                output = render_cockpit(
                    width, height, "语音情绪分析系统", "~/工作区/很长的项目路径",
                    "0.5.0", agents, "profile-05", Theme(False), selected=11,
                )
                lines = output.split("\n")
                self.assertEqual(len(lines), height)
                self.assertTrue(all(display_width(line) <= width for line in lines))
                self.assertIn("协作代理十一", output)
                self.assertNotIn("profile-00", output)
                self.assertNotIn("\033[", output)
                self.assertEqual(
                    output,
                    render_cockpit(
                        width, height, "语音情绪分析系统", "~/工作区/很长的项目路径",
                        "0.5.0", agents, "profile-05", Theme(False), selected=11,
                    ),
                )

        previous_no_color = os.environ.get("NO_COLOR")
        try:
            os.environ["NO_COLOR"] = "1"
            no_color = render_cockpit(
                80, 24, "项目", "~/项目", "0.5.0", agents,
                "profile-05", Theme(True), selected=11,
            )
        finally:
            if previous_no_color is None:
                os.environ.pop("NO_COLOR", None)
            else:
                os.environ["NO_COLOR"] = previous_no_color
        self.assertNotIn("\033[", no_color)

    def test_cockpit_reducer_browse_semantics(self):
        agents = [
            CockpitAgent("current", "online"),
            CockpitAgent("ready", "online"),
            CockpitAgent("dead", "dead"),
            CockpitAgent("missing", "missing"),
        ]

        state, action = reduce_cockpit_key(
            agents, "current", CockpitState(selected=0), "2",
        )
        self.assertEqual(state, CockpitState(selected=1))
        self.assertIsNone(action)

        state, action = reduce_cockpit_key(agents, "current", state, "enter")
        self.assertEqual(state, CockpitState(selected=1, mode="confirm"))
        self.assertIsNone(action)

        current_state, action = reduce_cockpit_key(
            agents, "current", CockpitState(selected=0), "enter",
        )
        self.assertEqual(current_state.selected, 0)
        self.assertEqual((action.kind, action.target), ("close", None))

        dead_state, action = reduce_cockpit_key(
            agents, "current", CockpitState(selected=2), "enter",
        )
        self.assertEqual(dead_state.selected, 2)
        self.assertEqual((action.kind, action.target), ("switch", "dead"))

        missing_state, action = reduce_cockpit_key(
            agents, "current", CockpitState(), "4",
        )
        self.assertEqual(missing_state.selected, 3)
        self.assertIn("agentx new missing", missing_state.error)
        self.assertIsNone(action)

        wrapped, action = reduce_cockpit_key(agents, "current", missing_state, "down")
        self.assertEqual(wrapped.selected, 0)
        self.assertIsNone(action)
        _, action = reduce_cockpit_key(agents, "current", wrapped, "escape")
        self.assertEqual((action.kind, action.target), ("close", None))

    def test_cockpit_reducer_confirmation_semantics(self):
        agents = [CockpitAgent("current"), CockpitAgent("target")]
        confirm = CockpitState(selected=1, mode="confirm")

        _, action = reduce_cockpit_key(agents, "current", confirm, "enter")
        self.assertEqual((action.kind, action.target), ("transfer", "target"))
        _, action = reduce_cockpit_key(agents, "current", confirm, "n")
        self.assertEqual((action.kind, action.target), ("switch", "target"))

        switch_state, action = reduce_cockpit_key(agents, "current", confirm, "right")
        self.assertEqual(switch_state.confirm_index, 1)
        self.assertIsNone(action)
        _, action = reduce_cockpit_key(agents, "current", switch_state, "enter")
        self.assertEqual((action.kind, action.target), ("switch", "target"))

        cancel_state, action = reduce_cockpit_key(
            agents, "current", CockpitState(1, "confirm", 2), "enter",
        )
        self.assertEqual(cancel_state, CockpitState(selected=1, mode="browse"))
        self.assertIsNone(action)
        _, action = reduce_cockpit_key(agents, "current", cancel_state, "ctrl-c")
        self.assertEqual((action.kind, action.target), ("close", None))

    def test_cockpit_arrow_decoder_does_not_consume_the_next_key(self):
        reader, writer = os.pipe()
        try:
            os.write(writer, b"\x1b[B\n")
            self.assertEqual(_read_key(reader), "down")
            self.assertEqual(_read_key(reader), "enter")
        finally:
            os.close(reader)
            os.close(writer)

    def test_dashboard_and_statusline_render_saved_context(self):
        workspace = {"timestamp": None,
                     "git": {"branch": "feature/ui", "modified_files": ["a.py"],
                              "staged_files": ["b.py"], "untracked_files": ["c.py"]}}
        context = {"transfer": {"from_agent": "codex-1", "to_agent": "codex-2",
                                "created_at": None, "file": "context-pack.md", "status": "injected"}}
        output = render_dashboard({"name": "demo"}, workspace, context, "", [], Theme(False), width=80)
        self.assertIn("AGENTX // CONTROL DECK", output)
        self.assertIn("PROJECT  demo", output)
        self.assertIn("SIGNAL   feature/ui  //  Δ 3", output)
        self.assertIn("codex-1 → codex-2", output)
        self.assertIn("context-pack.md", output)
        self.assertIn("✓ INJECTED", output)
        self.assertIn("UPDATED    never", output)
        self.assertIn("1 modified · 1 staged · 1 untracked", output)
        self.assertEqual(render_statusline(workspace, context), "⌁ feature/ui  ◈ Δ3  ↻ never  ⟲ never")

        narrow = render_dashboard({"name": "a-very-long-project-name"}, workspace, context,
                                  "", [], Theme(False), width=48)
        self.assertTrue(all(len(line) <= 48 for line in narrow.splitlines()))
        self.assertNotIn("\033[", narrow)

    def test_dashboard_sections_and_wide_unicode_keep_terminal_width(self):
        workspace = {"timestamp": "2026-01-01T12:00:00+00:00",
                     "git": {"branch": "功能/界面", "modified_files": ["界面.py"],
                              "staged_files": [], "untracked_files": []}}
        context = {"transfer": {"from_agent": "codex-一", "to_agent": "agy",
                                "created_at": None, "file": "/tmp/上下文/context-pack.md",
                                "status": "captured"}}
        windows = [{"name": "编码代理一号", "active": "1", "dead": "0"}]
        agents = render_dashboard({"name": "项目界面"}, workspace, context, "", windows,
                                  Theme(False), width=48, section="agents")
        context_view = render_dashboard({"name": "项目界面"}, workspace, context, "", windows,
                                        Theme(False), width=48, section="context")
        self.assertIn("AGENT MATRIX", agents)
        self.assertNotIn("CONTEXT BUS", agents)
        self.assertIn("CONTEXT BUS", context_view)
        self.assertNotIn("AGENT MATRIX", context_view)
        self.assertIn("context-pack.md", context_view)
        self.assertTrue(all(display_width(line) <= 48 for line in agents.splitlines()))
        self.assertTrue(all(display_width(line) <= 48 for line in context_view.splitlines()))

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

    def test_local_memory_prefers_project_notes_and_formats_a_bounded_context_section(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vault"
            project = Path(temp) / "project"
            project.mkdir()
            initialise_memory(root)
            project_note = add_memory(
                root, "Queue decision", "Use the queue to isolate inference latency.", project,
                ["architecture", "queue"],
            )
            add_memory(root, "General queue", "A generic note.", None, ["queue"])
            notes = context_memory(root, project, "feature/queue", ["queue.py"])
            self.assertEqual(notes[0].path, project_note)
            self.assertIn("Queue decision", format_memory_section(notes, root))
            self.assertIn("projects/project", format_memory_section(notes, root))
            self.assertEqual(search_memory(root, "missing term", project)[0].path, project_note)

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
