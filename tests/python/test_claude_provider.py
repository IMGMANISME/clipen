import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from clipen import cli
from clipen.provider_registry import PROVIDERS, editing_agent_factory, get_provider, implemented_providers
from clipen.providers import (
    ProviderAuthenticationError,
    ProviderProtocolError,
    ProviderUsageLimitError,
)
from clipen.providers.claude import ClaudeCliAdapter, REQUIRED_FLAGS
from clipen.providers.claude_editing import ClaudeEditingAgent, allowed_tools
from clipen.providers.codex_editing import CodexEditingAgent
from clipen.providers.editing_agent import _MCP_TOOLS


RUN = "editrun_" + "b" * 32
PROJECT = "prj_" + "a" * 32


def stream(*events):
    return "".join(json.dumps(event) + "\n" for event in events)


INIT = {"type": "system", "subtype": "init", "mcp_servers": [{"name": "clipen", "status": "connected"}]}
SUCCESS = {"type": "result", "subtype": "success", "is_error": False, "result": "done",
           "usage": {"input_tokens": 120, "cache_creation_input_tokens": 3000,
                     "cache_read_input_tokens": 90000, "output_tokens": 4500}}


class ProviderRegistryTests(unittest.TestCase):
    def test_codex_and_claude_edit_while_local_is_reserved(self):
        self.assertEqual(implemented_providers(), ("codex", "claude"))
        self.assertIs(editing_agent_factory("codex"), CodexEditingAgent)
        self.assertIs(editing_agent_factory("claude"), ClaudeEditingAgent)
        self.assertFalse(PROVIDERS["local"].implemented)
        with self.assertRaises(ValueError):
            editing_agent_factory("local")
        with self.assertRaises(ValueError):
            get_provider("gemini")
        from clipen.provider_registry import exec_adapter_factory
        from clipen.providers.claude import ClaudeExecAdapter
        from clipen.providers.codex import CodexExecAdapter
        self.assertIs(exec_adapter_factory("codex"), CodexExecAdapter)
        self.assertIs(exec_adapter_factory("claude"), ClaudeExecAdapter)
        with self.assertRaises(ValueError):
            exec_adapter_factory("local")

    def test_cli_routes_each_provider_to_its_own_probe(self):
        calls = []
        workspace = SimpleNamespace(root=Path("/w"), projects_root=Path("/w/.clipen/projects"), workspace_id="wsp_" + "1" * 32)
        with (
            mock.patch("clipen.cli._repo_root", return_value=Path("/repo")),
            mock.patch("clipen.cli._data_root", return_value=Path(tempfile.mkdtemp())),
            mock.patch("clipen.cli.prepare_workspace", return_value=workspace),
            mock.patch("clipen.cli.inspect_workspace_projects", return_value=SimpleNamespace(project_count=0)),
            mock.patch("clipen.cli.verify_system_skill_release"),
            mock.patch("clipen.cli.probe_codex", side_effect=lambda path: calls.append("codex")),
            mock.patch("clipen.cli.probe_claude", side_effect=lambda path: calls.append("claude")),
            mock.patch("clipen.cli.run_server", return_value=0),
            redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(cli.main(["claude"]), 0)
            self.assertEqual(cli.main(["codex"]), 0)
            self.assertEqual(cli.main(["local"]), 2)
            self.assertEqual(cli.main(["gemini"]), 2)
            self.assertEqual(cli.main([]), 2)
        self.assertEqual(calls, ["claude", "codex"])


class ClaudeEditingAgentTests(unittest.TestCase):
    def agent(self, root, repository=None):
        projects = root / "projects"; attempts = root / "attempts"
        projects.mkdir(exist_ok=True); attempts.mkdir(exist_ok=True)
        return ClaudeEditingAgent(projects, attempts, repository or SimpleNamespace(), binary="/bin/echo",
                                  python="/usr/bin/python3", environment={"PATH": "/usr/bin", "HOME": str(root),
                                                                           "ANTHROPIC_API_KEY": "test", "SECRET": "x"})

    def test_command_exposes_only_clipen_mcp_tools_and_no_user_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = self.agent(root)
            workspace = root / "attempts" / "agent-test"; workspace.mkdir()
            command = agent.command(PROJECT, RUN, workspace, workspace / "final.txt")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--allowedTools") + 1].split(","),
                         [f"mcp__clipen__{name}" for name in _MCP_TOOLS])
        self.assertEqual(allowed_tools().count("mcp__clipen__"), len(_MCP_TOOLS))
        for flag in ("--print", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(command[command.index("--setting-sources") + 1], "project")
        config = json.loads(command[command.index("--mcp-config") + 1])
        server = config["mcpServers"]["clipen"]
        self.assertEqual(server["command"], "/usr/bin/python3")
        self.assertIn(RUN, server["args"])
        self.assertNotIn("bypassPermissions", command)
        self.assertNotIn("SECRET", agent.environment)
        self.assertEqual(agent.environment["ANTHROPIC_API_KEY"], "test")
        self.assertEqual(agent.environment["MCP_TOOL_TIMEOUT"], "600000")

    def test_success_records_cached_and_created_input_as_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            outcomes = []
            finished = SimpleNamespace(provider="claude", state="succeeded", final_revision_id="editrev_" + "c" * 32)
            repository = SimpleNamespace(production_session=lambda *args: None, get=lambda *args: finished,
                                         record_provider_outcome=lambda *args, **kwargs: outcomes.append(kwargs) or finished)
            agent = self.agent(Path(temporary), repository)

            def success(command, workspace, events, diagnostics, timeout, cancel):
                events.write_text(stream(INIT, {"type": "assistant"}, SUCCESS)); diagnostics.write_text(""); return 0
            with patch.object(agent, "_run", side_effect=success):
                result = agent.execute(PROJECT, RUN)
        self.assertEqual(result.event_types, ("system.init", "assistant", "result.success"))
        self.assertEqual(outcomes[0]["usage"], {"input_tokens": 93120, "cached_input_tokens": 90000, "output_tokens": 4500})
        self.assertNotIn("error_code", outcomes[0])

    def test_failures_are_classified_and_never_replayed(self):
        cases = [
            (stream(INIT, {**SUCCESS, "subtype": "error_during_execution", "is_error": True,
                           "result": "Invalid API key · Please run /login"}), 1, ProviderAuthenticationError, "provider_authentication_required"),
            # Recorded from a real run whose OAuth login expired mid-production.
            (stream(INIT, {**SUCCESS, "is_error": True, "terminal_reason": "api_error",
                           "result": "Failed to authenticate: OAuth session expired and could not be refreshed"}), 0,
             ProviderAuthenticationError, "provider_authentication_required"),
            (stream(INIT, {**SUCCESS, "is_error": True, "result": "API Error", "api_error_status": 429}), 1,
             ProviderUsageLimitError, "provider_usage_limit"),
            (stream({**INIT, "mcp_servers": [{"name": "clipen", "status": "failed"}]}, SUCCESS), 0,
             ProviderProtocolError, "provider_protocol_error"),
            ("", 1, ProviderProtocolError, "provider_protocol_error"),
        ]
        for output, code, error, error_code in cases:
            with self.subTest(error_code=error_code), tempfile.TemporaryDirectory() as temporary:
                outcomes = []
                repository = SimpleNamespace(production_session=lambda *args: None, get=lambda *args: SimpleNamespace(provider="claude"),
                                             record_provider_outcome=lambda *args, **kwargs: outcomes.append(kwargs))
                agent = self.agent(Path(temporary), repository)

                def failed(command, workspace, events, diagnostics, timeout, cancel):
                    events.write_text(output); diagnostics.write_text(""); return code
                with patch.object(agent, "_run", side_effect=failed) as run, self.assertRaises(error):
                    agent.execute(PROJECT, RUN)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(outcomes[0]["error_code"], error_code)

    def test_refuses_a_run_created_for_another_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = SimpleNamespace(production_session=lambda *args: None, get=lambda *args: SimpleNamespace(provider="codex"))
            with self.assertRaises(ValueError):
                self.agent(Path(temporary), repository).execute(PROJECT, RUN)


class ClaudeProbeTests(unittest.TestCase):
    def fake_cli(self, root, *, logged_in=True, flags=REQUIRED_FLAGS):
        script = root / "claude"
        help_text = " ".join(flags)
        script.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  --version) echo '9.9.9 (Claude Code)';;\n"
            f"  --help) echo '{help_text}';;\n"
            f"  auth) echo '{{\"loggedIn\": {str(logged_in).lower()}}}';;\n"
            "esac\n")
        script.chmod(0o700)
        return script

    def test_probe_reads_version_flags_and_login_without_a_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root)}
            ready = ClaudeCliAdapter(self.fake_cli(root), env).probe()
            self.assertEqual(ready.version, "9.9.9 (Claude Code)")
            self.assertTrue(ready.authenticated and ready.supports_bounded_attempts)
            logged_out = ClaudeCliAdapter(self.fake_cli(root, logged_in=False), env).probe()
            self.assertFalse(logged_out.authenticated)
            old = ClaudeCliAdapter(self.fake_cli(root, flags=REQUIRED_FLAGS[:-1]), env).probe()
            self.assertEqual(old.missing_flags, ("--setting-sources",))

    def test_controller_status_maps_probe_states(self):
        from clipen.controller.app import ProviderState
        from clipen.controller.server import probe_claude
        from clipen.providers.claude import ClaudeProbeResult
        with patch("clipen.controller.server.ClaudeCliAdapter") as adapter:
            adapter.return_value.probe.return_value = ClaudeProbeResult("1", True, ())
            ready = probe_claude(Path("."))
            adapter.return_value.probe.return_value = ClaudeProbeResult("1", False, ())
            login = probe_claude(Path("."))
        self.assertEqual((ready.state, ready.provider_id), (ProviderState.READY, "claude"))
        self.assertIs(login.state, ProviderState.LOGIN_REQUIRED)


class ProviderSelectionTests(unittest.TestCase):
    def test_coordinator_records_selected_provider_and_never_runs_another_providers_job(self):
        import test_skill_production as fixtures
        from clipen.editing import EditAgentCoordinator
        f = fixtures.SkillProductionTests(); f.setUp()
        try:
            launched = []

            class Fake:
                def __init__(self, *args, **kwargs): pass
                def execute(self, *args, **kwargs): launched.append(args)
            coordinator = EditAgentCoordinator(f.repository, f.root, adapter_factory=Fake, provider_id="claude")
            self.assertIn("Claude", str(coordinator.provider.display_name))
            key = (f.project.project_id, f.run.run_id)  # created by the fixture for Codex
            coordinator._cancel[key] = threading.Event()
            coordinator._queue.put(key); coordinator._queue.put(None)
            coordinator._loop()
            self.assertEqual(launched, [])
            run = f.repository.get(*key)
            self.assertEqual((run.state, run.error_code), ("failed", "provider_mismatch"))
            with self.assertRaises(ValueError):
                EditAgentCoordinator(f.repository, f.root, provider_id="local")
        finally:
            f.tearDown()

    def test_status_names_the_selected_provider(self):
        from clipen.controller import ControllerConfig, LocalSessionAuthority, ProviderState, ProviderStatus, create_app
        from test_project_api import request_app
        with tempfile.TemporaryDirectory() as temporary:
            static = Path(temporary); (static / "index.html").write_text("Clipen")
            app = create_app(ControllerConfig(static), ProviderStatus("Claude", ProviderState.READY, "ready", "claude"),
                             LocalSessionAuthority())

            def request(method, path, headers=None):
                return asyncio.run(request_app(app, method, path, headers=headers, body=b""))
            _, headers, _ = request("POST", "/api/session/connect", headers={"origin": "http://localhost:4200", "sec-fetch-site": "same-origin"})
            cookie = headers["set-cookie"].split(";", 1)[0]
            _, _, body = request("GET", "/api/status", headers={"cookie": cookie})
            self.assertEqual(json.loads(body)["provider"]["id"], "claude")
            self.assertEqual(json.loads(body)["provider"]["name"], "Claude")


class ClaudeLargeStreamTests(unittest.TestCase):
    def test_long_revisions_with_many_previews_fit_and_tool_echoes_are_dropped(self):
        import json
        from clipen.providers.claude import _stream_events
        from clipen.providers.editing_agent import _MAX_CAPTURE_BYTES
        # A real Claude revision with about 90 previews wrote 34.7 MB and was cut off at 32 MB.
        self.assertGreaterEqual(_MAX_CAPTURE_BYTES, 128 * 1024 * 1024)
        image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'A' * 1000}}
        stream = '\n'.join(json.dumps(e) for e in (
            {'type': 'system', 'subtype': 'init', 'model': 'claude-sonnet-5'},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': [image]}]}, 'tool_use_result': [image]},
            {'type': 'result', 'subtype': 'success', 'result': 'OK'})).encode()
        events = _stream_events(stream, _MAX_CAPTURE_BYTES)
        self.assertEqual(events[1], {'type': 'user'})
        self.assertEqual((events[0]['model'], events[2]['result']), ('claude-sonnet-5', 'OK'))
        with self.assertRaises(Exception):
            _stream_events(b'{not json}', _MAX_CAPTURE_BYTES)


class ClaudeClosingMessageTests(unittest.TestCase):
    def test_the_result_event_carries_the_last_message_to_the_user(self):
        import json
        from pathlib import Path
        from clipen.providers.claude_editing import ClaudeEditingAgent
        stream=(json.dumps({'type':'system','subtype':'init'})+'\n'+json.dumps({'type':'result','subtype':'success','result':'需要你提供音樂檔。'})+'\n').encode()
        self.assertEqual(ClaudeEditingAgent.closing_message(None,stream,Path('unused')),'需要你提供音樂檔。')
        self.assertIsNone(ClaudeEditingAgent.closing_message(None,b'',Path('unused')))


if __name__ == "__main__":
    unittest.main()
