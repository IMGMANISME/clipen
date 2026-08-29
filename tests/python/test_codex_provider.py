import json
from pathlib import Path
import stat
import tempfile
import textwrap
import threading
import time
import unittest

from clipen.providers import (
    CodexAttempt,
    CodexExecAdapter,
    ProviderAuthenticationError,
    ProviderCancelled,
    ProviderNetworkError,
    ProviderPermissionError,
    ProviderProtocolError,
    ProviderTimedOut,
    ProviderUsageLimitError,
)


SUCCESSFUL_FAKE = r"""
import json
import pathlib
import sys

if "--version" in sys.argv:
    print("codex-cli fake-1")
    raise SystemExit(0)

arguments = sys.argv[1:]
prompt = sys.stdin.read()
output = pathlib.Path(arguments[arguments.index("--output-last-message") + 1])
output.write_text(json.dumps({"plan": "candidate"}), encoding="utf-8")
pathlib.Path("observation.json").write_text(
    json.dumps({"arguments": arguments, "prompt": prompt}), encoding="utf-8"
)
print(json.dumps({"type": "thread.started"}))
print(json.dumps({"type": "turn.completed"}))
"""

PROBE_FAKE = r"""
import sys

arguments = sys.argv[1:]
if arguments == ["--version"]:
    print("codex-cli fake-1")
elif arguments == ["exec", "--help"]:
    print("--config --strict-config --ephemeral --ignore-user-config --ignore-rules")
    print("--json --output-schema --output-last-message")
elif arguments == ["sandbox", "--help"]:
    print("--permission-profile")
elif arguments == ["login", "status"]:
    print("Logged in using test fixture")
elif arguments and arguments[0] == "sandbox":
    print("clipen-confinement-ok")
else:
    raise SystemExit(2)
"""


class CodexProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "attempt"
        self.workspace.mkdir()
        self.schema = self.workspace / "output.schema.json"
        self.schema.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "required": ["plan"],
                    "properties": {"plan": {"type": "string"}},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_codex(self, body: str) -> Path:
        binary = self.root / f"fake-codex-{len(list(self.root.glob('fake-codex-*')))}"
        binary.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return binary

    def attempt(self, **changes) -> CodexAttempt:
        values = {
            "attempt_id": "attempt-1",
            "workspace": self.workspace,
            "output_schema": self.schema,
            "prompt": "Return the approved candidate plan.",
            "timeout_seconds": 5,
        }
        values.update(changes)
        return CodexAttempt(**values)

    def test_executes_one_ephemeral_attempt_with_prompt_on_stdin(self) -> None:
        adapter = CodexExecAdapter(
            self.root, self.fake_codex(SUCCESSFUL_FAKE), environment={"PATH": "/usr/bin"}
        )
        result = adapter.execute(self.attempt())

        observation = json.loads((self.workspace / "observation.json").read_text(encoding="utf-8"))
        arguments = observation["arguments"]
        self.assertEqual(result.payload, {"plan": "candidate"})
        self.assertEqual(result.event_types, ("thread.started", "turn.completed"))
        self.assertEqual(observation["prompt"], "Return the approved candidate plan.")
        self.assertNotIn(observation["prompt"], arguments)
        self.assertIn("--ephemeral", arguments)
        self.assertIn("--ignore-user-config", arguments)
        self.assertIn("--ignore-rules", arguments)
        self.assertIn("--strict-config", arguments)
        self.assertNotIn("--sandbox", arguments)
        self.assertEqual(arguments[arguments.index("--ask-for-approval") + 1], "never")
        self.assertTrue(
            any(argument.startswith("default_permissions=") for argument in arguments)
        )
        self.assertTrue(
            any("filesystem=" in argument and '":workspace_roots"="read"' in argument
                for argument in arguments)
        )
        self.assertIn(
            "permissions.clipen_attempt.network.enabled=false",
            arguments,
        )
        self.assertIn('shell_environment_policy.inherit="none"', arguments)
        self.assertEqual(arguments[-1], "-")
        self.assertEqual(adapter.version(), "codex-cli fake-1")

    def test_probe_checks_auth_and_required_cli_capabilities_without_an_attempt(self) -> None:
        adapter = CodexExecAdapter(
            self.root, self.fake_codex(PROBE_FAKE), environment={"PATH": "/usr/bin"}
        )

        result = adapter.probe()

        self.assertTrue(result.authenticated)
        self.assertTrue(result.capabilities.supports_bounded_attempts)
        self.assertTrue(result.confinement_verified)
        self.assertTrue(result.available)

    def test_probe_fails_closed_when_confinement_canary_fails(self) -> None:
        fake = self.fake_codex(PROBE_FAKE.replace(
            'print("clipen-confinement-ok")', 'print("unexpected"); raise SystemExit(1)'
        ))
        result = CodexExecAdapter(
            self.root, fake, environment={"PATH": "/usr/bin"}
        ).probe()

        self.assertFalse(result.confinement_verified)
        self.assertFalse(result.available)

    def test_requires_schema_and_output_to_stay_inside_workspace(self) -> None:
        outside = self.root / "outside.schema.json"
        outside.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.attempt(output_schema=outside)

        adapter = CodexExecAdapter(
            self.root, self.fake_codex(SUCCESSFUL_FAKE), environment={"PATH": "/usr/bin"}
        )
        with self.assertRaises(ValueError):
            adapter.command(self.attempt(), self.root / "outside-output.json")

    def test_requires_workspace_to_be_under_managed_attempt_root(self) -> None:
        other_root = self.root / "other-managed-root"
        other_root.mkdir()
        adapter = CodexExecAdapter(
            other_root, self.fake_codex(SUCCESSFUL_FAKE), environment={"PATH": "/usr/bin"}
        )
        with self.assertRaises(ValueError):
            adapter.command(self.attempt(), self.workspace / "new-output.json")

    def test_rejects_symlink_workspace(self) -> None:
        link = self.root / "attempt-link"
        link.symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.attempt(workspace=link)

    def test_rejects_identifier_that_could_escape_output_workspace(self) -> None:
        with self.assertRaises(ValueError):
            self.attempt(attempt_id="../outside")

    def test_rejects_invalid_provider_protocol(self) -> None:
        fake = self.fake_codex(
            """
            import pathlib
            import sys
            arguments = sys.argv[1:]
            output = pathlib.Path(arguments[arguments.index("--output-last-message") + 1])
            output.write_text("not-json", encoding="utf-8")
            print("not-jsonl")
            """
        )
        with self.assertRaises(ProviderProtocolError):
            CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(self.attempt())

    def test_timeout_terminates_the_attempt(self) -> None:
        fake = self.fake_codex(
            """
            import time
            time.sleep(30)
            """
        )
        with self.assertRaises(ProviderTimedOut):
            CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(
                self.attempt(timeout_seconds=0.05)
            )

    def test_cancel_token_terminates_the_attempt(self) -> None:
        fake = self.fake_codex(
            """
            import time
            time.sleep(30)
            """
        )
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(ProviderCancelled):
                CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(
                    self.attempt(), cancelled
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)

    def test_rejects_diagnostic_output_over_the_capture_limit(self) -> None:
        fake = self.fake_codex(
            """
            import sys
            sys.stdout.write("x" * 2_000_001)
            """
        )
        with self.assertRaises(ProviderProtocolError):
            CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(
                self.attempt()
            )

    def test_requires_object_result_and_completed_turn_event(self) -> None:
        non_object = self.fake_codex(
            """
            import pathlib
            import sys
            arguments = sys.argv[1:]
            output = pathlib.Path(arguments[arguments.index("--output-last-message") + 1])
            output.write_text("[]", encoding="utf-8")
            print('{"type":"turn.completed"}')
            """
        )
        with self.assertRaises(ProviderProtocolError):
            CodexExecAdapter(
                self.root, non_object, environment={"PATH": "/usr/bin"}
            ).execute(self.attempt())

        missing_terminal_event = self.fake_codex(
            """
            import pathlib
            import sys
            arguments = sys.argv[1:]
            output = pathlib.Path(arguments[arguments.index("--output-last-message") + 1])
            output.write_text("{}", encoding="utf-8")
            print('{"type":"thread.started"}')
            """
        )
        self.workspace.joinpath("provider-output-attempt-1.json").unlink(missing_ok=True)
        with self.assertRaises(ProviderProtocolError):
            CodexExecAdapter(
                self.root, missing_terminal_event, environment={"PATH": "/usr/bin"}
            ).execute(self.attempt())

    def test_validates_result_against_the_local_output_schema(self) -> None:
        missing_required_property = self.fake_codex(
            """
            import pathlib
            import sys
            arguments = sys.argv[1:]
            output = pathlib.Path(arguments[arguments.index("--output-last-message") + 1])
            output.write_text("{}", encoding="utf-8")
            print('{"type":"turn.completed"}')
            """
        )
        with self.assertRaises(ProviderProtocolError):
            CodexExecAdapter(
                self.root, missing_required_property, environment={"PATH": "/usr/bin"}
            ).execute(self.attempt())

    def test_rejects_invalid_json_schema_before_launch(self) -> None:
        self.schema.write_text(
            json.dumps({"type": "definitely-not-a-json-schema-type"}), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            self.attempt()

    def test_classifies_provider_failures_for_product_recovery(self) -> None:
        cases = (
            ("not logged in", ProviderAuthenticationError),
            ("usage limit reached", ProviderUsageLimitError),
            ("connection failed", ProviderNetworkError),
            ("sandbox operation not permitted", ProviderPermissionError),
        )
        for index, (diagnostic, expected) in enumerate(cases):
            with self.subTest(diagnostic=diagnostic):
                self.workspace.joinpath("provider-output-attempt-1.json").unlink(missing_ok=True)
                fake = self.fake_codex(
                    f"""
                    import sys
                    sys.stderr.write({diagnostic!r})
                    raise SystemExit({index + 1})
                    """
                )
                with self.assertRaises(expected):
                    CodexExecAdapter(
                        self.root, fake, environment={"PATH": "/usr/bin"}
                    ).execute(self.attempt())


if __name__ == "__main__":
    unittest.main()
