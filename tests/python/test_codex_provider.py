import json
import stat
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

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
from clipen.providers.recovery import (
    AttemptReceiptStore,
    ProviderAttemptReceipt,
    ProviderAttemptState,
    ProviderUsage,
    sha256_bytes,
    sha256_file,
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
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 120, "cached_input_tokens": 80, "output_tokens": 24
}}))
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

    def test_image_attachments_are_ordered_confined_and_bound_to_receipt(self) -> None:
        images = []
        for name in ("a.png", "b.png"):
            path = self.workspace / name
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
            images.append(path)
        attempt = self.attempt(images=tuple(images))
        adapter = CodexExecAdapter(self.root, self.fake_codex(SUCCESSFUL_FAKE), environment={"PATH": "/usr/bin"})
        adapter.execute(attempt)
        arguments = json.loads((self.workspace / "observation.json").read_text())["arguments"]
        attached = [arguments[i + 1] for i, value in enumerate(arguments) if value == "--image"]
        self.assertEqual(attached, [str(path.resolve()) for path in images])
        self.assertEqual(adapter.reconcile(attempt).state, ProviderAttemptState.SUCCEEDED)
        images[0].write_bytes(b"\x89PNG\r\n\x1a\nchanged")
        self.assertIsNone(adapter.reconcile(attempt).result)

    def test_image_bounds_and_escape_are_rejected_before_launch(self) -> None:
        outside = self.root / "outside.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\n")
        linked = self.workspace / "linked.png"
        linked.symlink_to(outside)
        for images in ((outside,), (linked,), (outside,) * 9):
            with self.subTest(images=images), self.assertRaises(ValueError):
                self.attempt(images=images)
        oversized = self.workspace / "oversized.png"
        oversized.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024**2))
        with self.assertRaises(ValueError):
            self.attempt(images=(oversized,))

    def test_executes_one_ephemeral_attempt_with_prompt_on_stdin(self) -> None:
        adapter = CodexExecAdapter(self.root, self.fake_codex(SUCCESSFUL_FAKE), environment={"PATH": "/usr/bin"})
        result = adapter.execute(self.attempt())

        observation = json.loads((self.workspace / "observation.json").read_text(encoding="utf-8"))
        arguments = observation["arguments"]
        self.assertEqual(result.payload, {"plan": "candidate"})
        self.assertEqual(result.event_types, ("thread.started", "turn.completed"))
        self.assertEqual(result.usage, ProviderUsage(120, 80, 24))
        self.assertEqual(observation["prompt"], "Return the approved candidate plan.")
        self.assertNotIn(observation["prompt"], arguments)
        self.assertIn("--ephemeral", arguments)
        self.assertIn("--ignore-user-config", arguments)
        self.assertIn("--ignore-rules", arguments)
        self.assertIn("--strict-config", arguments)
        self.assertNotIn("--sandbox", arguments)
        self.assertEqual(arguments[arguments.index("--ask-for-approval") + 1], "never")
        self.assertTrue(any(argument.startswith("default_permissions=") for argument in arguments))
        self.assertTrue(
            any("filesystem=" in argument and '":workspace_roots"="read"' in argument for argument in arguments)
        )
        self.assertIn(
            "permissions.clipen_attempt.network.enabled=false",
            arguments,
        )
        self.assertIn('shell_environment_policy.inherit="none"', arguments)
        self.assertEqual(arguments[-1], "-")
        self.assertEqual(adapter.version(), "codex-cli fake-1")

        reconciled = CodexExecAdapter(
            self.root, self.fake_codex(PROBE_FAKE), environment={"PATH": "/usr/bin"}
        ).reconcile(self.attempt())
        self.assertEqual(reconciled.state, ProviderAttemptState.SUCCEEDED)
        self.assertIsNotNone(reconciled.result)
        self.assertEqual(reconciled.result.payload, result.payload)
        self.assertEqual(reconciled.result.event_types, result.event_types)
        self.assertEqual(reconciled.result.usage, result.usage)
        receipt_text = AttemptReceiptStore(self.workspace, "attempt-1").path.read_text(encoding="utf-8")
        self.assertNotIn("Return the approved candidate plan", receipt_text)

    def test_probe_checks_auth_and_required_cli_capabilities_without_an_attempt(self) -> None:
        adapter = CodexExecAdapter(self.root, self.fake_codex(PROBE_FAKE), environment={"PATH": "/usr/bin"})

        result = adapter.probe()

        self.assertTrue(result.authenticated)
        self.assertTrue(result.capabilities.supports_bounded_attempts)
        self.assertTrue(result.confinement_verified)
        self.assertTrue(result.available)

    def test_probe_fails_closed_when_confinement_canary_fails(self) -> None:
        fake = self.fake_codex(
            PROBE_FAKE.replace('print("clipen-confinement-ok")', 'print("unexpected"); raise SystemExit(1)')
        )
        result = CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).probe()

        self.assertFalse(result.confinement_verified)
        self.assertFalse(result.available)

    def test_requires_schema_and_output_to_stay_inside_workspace(self) -> None:
        outside = self.root / "outside.schema.json"
        outside.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.attempt(output_schema=outside)

        adapter = CodexExecAdapter(self.root, self.fake_codex(SUCCESSFUL_FAKE), environment={"PATH": "/usr/bin"})
        with self.assertRaises(ValueError):
            adapter.command(self.attempt(), self.root / "outside-output.json")

    def test_requires_workspace_to_be_under_managed_attempt_root(self) -> None:
        other_root = self.root / "other-managed-root"
        other_root.mkdir()
        adapter = CodexExecAdapter(other_root, self.fake_codex(SUCCESSFUL_FAKE), environment={"PATH": "/usr/bin"})
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
        self.assertEqual(
            AttemptReceiptStore(self.workspace, "attempt-1").read().state,
            ProviderAttemptState.TIMED_OUT,
        )

    def test_timeout_terminates_the_entire_process_group(self) -> None:
        child_marker = self.root / "timeout-child-survived"
        fake = self.fake_codex(
            f"""
            import subprocess
            import sys
            import time
            subprocess.Popen([
                sys.executable,
                "-c",
                "import pathlib,time; time.sleep(0.4); "
                "pathlib.Path({str(child_marker)!r}).write_text('survived')",
            ])
            time.sleep(30)
            """
        )
        with self.assertRaises(ProviderTimedOut):
            CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(
                self.attempt(timeout_seconds=0.05)
            )
        time.sleep(0.6)
        self.assertFalse(child_marker.exists())

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
                CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(self.attempt(), cancelled)
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(
            AttemptReceiptStore(self.workspace, "attempt-1").read().state,
            ProviderAttemptState.CANCELLED,
        )

    def test_cancel_terminates_the_entire_process_group(self) -> None:
        child_marker = self.root / "cancel-child-survived"
        fake = self.fake_codex(
            f"""
            import subprocess
            import sys
            import time
            subprocess.Popen([
                sys.executable,
                "-c",
                "import pathlib,time; time.sleep(0.5); "
                "pathlib.Path({str(child_marker)!r}).write_text('survived')",
            ])
            time.sleep(30)
            """
        )
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(ProviderCancelled):
                CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(self.attempt(), cancelled)
        finally:
            timer.cancel()
        time.sleep(0.7)
        self.assertFalse(child_marker.exists())

    def test_reconcile_recovers_completed_artifacts_after_running_receipt(self) -> None:
        output = self.workspace / "provider-output-attempt-1.json"
        events = self.workspace / "provider-events-attempt-1.jsonl"
        diagnostics = self.workspace / "provider-diagnostics-attempt-1.log"
        output.write_text(json.dumps({"plan": "recovered"}), encoding="utf-8")
        events.write_text(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 9,
                        "cached_input_tokens": 4,
                        "output_tokens": 3,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        diagnostics.write_text("", encoding="utf-8")
        store = AttemptReceiptStore(self.workspace, "attempt-1")
        store.write(
            ProviderAttemptReceipt(
                attempt_id="attempt-1",
                state=ProviderAttemptState.RUNNING,
                prompt_sha256=sha256_bytes(b"Return the approved candidate plan."),
                schema_sha256=sha256_file(self.schema, 1_000_000),
                started_at="2026-09-01T00:00:00+00:00",
                pid=123,
            )
        )

        adapter = CodexExecAdapter(self.root, self.fake_codex(PROBE_FAKE), environment={"PATH": "/usr/bin"})
        reconciled = adapter.reconcile(self.attempt())

        self.assertEqual(reconciled.state, ProviderAttemptState.SUCCEEDED)
        self.assertEqual(reconciled.result.payload, {"plan": "recovered"})
        self.assertEqual(reconciled.result.usage, ProviderUsage(9, 4, 3))
        self.assertEqual(store.read().state, ProviderAttemptState.SUCCEEDED)

    def test_reconcile_fails_closed_for_tampered_terminal_output(self) -> None:
        adapter = CodexExecAdapter(self.root, self.fake_codex(SUCCESSFUL_FAKE), environment={"PATH": "/usr/bin"})
        adapter.execute(self.attempt())
        (self.workspace / "provider-output-attempt-1.json").write_text(
            json.dumps({"plan": "tampered"}), encoding="utf-8"
        )

        reconciled = adapter.reconcile(self.attempt())

        self.assertEqual(reconciled.state, ProviderAttemptState.UNKNOWN)
        self.assertIsNone(reconciled.result)

    def test_reconcile_marks_prepared_attempt_as_interrupted(self) -> None:
        store = AttemptReceiptStore(self.workspace, "attempt-1")
        store.write(
            ProviderAttemptReceipt(
                attempt_id="attempt-1",
                state=ProviderAttemptState.PREPARED,
                prompt_sha256=sha256_bytes(b"Return the approved candidate plan."),
                schema_sha256=sha256_file(self.schema, 1_000_000),
            )
        )
        adapter = CodexExecAdapter(self.root, self.fake_codex(PROBE_FAKE), environment={"PATH": "/usr/bin"})

        reconciled = adapter.reconcile(self.attempt())

        self.assertEqual(reconciled.state, ProviderAttemptState.INTERRUPTED)
        self.assertEqual(store.read().state, ProviderAttemptState.INTERRUPTED)

    def test_rejects_diagnostic_output_over_the_capture_limit(self) -> None:
        fake = self.fake_codex(
            """
            import sys
            sys.stdout.write("x" * 2_000_001)
            """
        )
        with self.assertRaises(ProviderProtocolError):
            CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(self.attempt())

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
            CodexExecAdapter(self.root, non_object, environment={"PATH": "/usr/bin"}).execute(self.attempt())

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
        with self.assertRaises(ProviderProtocolError):
            CodexExecAdapter(self.root, missing_terminal_event, environment={"PATH": "/usr/bin"}).execute(
                self.attempt(attempt_id="attempt-missing-event")
            )

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
            CodexExecAdapter(self.root, missing_required_property, environment={"PATH": "/usr/bin"}).execute(
                self.attempt()
            )

    def test_rejects_invalid_json_schema_before_launch(self) -> None:
        self.schema.write_text(json.dumps({"type": "definitely-not-a-json-schema-type"}), encoding="utf-8")
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
                fake = self.fake_codex(
                    f"""
                    import sys
                    sys.stderr.write({diagnostic!r})
                    raise SystemExit({index + 1})
                    """
                )
                with self.assertRaises(expected):
                    CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(
                        self.attempt(attempt_id=f"attempt-failure-{index}")
                    )

    def test_jsonl_only_usage_limit_is_classified_and_preserves_unknown_usage(self):
        for code in (0, 1):
            with self.subTest(returncode=code):
                fake = self.fake_codex(
                    """
                    import json
                    print(json.dumps({'type':'error','message':"You've hit your usage limit."}))
                    print(json.dumps({'type':'turn.failed','error':{'message':"You've hit your usage limit."}}))
                    raise SystemExit("""
                    + str(code)
                    + """)
                """
                )
                attempt = self.attempt(attempt_id=f"quota-jsonl-{code}")
                with self.assertRaises(ProviderUsageLimitError):
                    CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(attempt)
                receipt = AttemptReceiptStore(attempt.workspace, attempt.attempt_id).read()
                self.assertEqual(receipt.error_code, "usage_limit")
                self.assertIsNone(receipt.usage)

    def test_failure_classifier_ignores_ordinary_model_text_and_recovered_errors(self):
        for events in (
            [{"type": "item.completed", "item": {"text": "usage limit"}}, {"type": "turn.completed"}],
            [{"type": "error", "message": "rate limit"}, {"type": "turn.completed"}],
        ):
            CodexExecAdapter._raise_for_failed_turn("\n".join(json.dumps(e) for e in events).encode())

    def test_failed_attempt_preserves_reported_usage(self) -> None:
        fake = self.fake_codex(
            """
            import json
            print(json.dumps({
                "type": "turn.failed",
                "usage": {
                    "input_tokens": 40,
                    "cached_input_tokens": 10,
                    "output_tokens": 2,
                },
            }))
            raise SystemExit(1)
            """
        )
        with self.assertRaises(ProviderProtocolError):
            CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(self.attempt())
        receipt = AttemptReceiptStore(self.workspace, "attempt-1").read()
        self.assertEqual(receipt.state, ProviderAttemptState.FAILED)
        self.assertEqual(receipt.usage, ProviderUsage(40, 10, 2))

    def test_persisted_provider_artifacts_redact_prompt_and_credentials(self) -> None:
        fake = self.fake_codex(
            """
            import json
            import sys
            prompt = sys.stdin.read()
            print(json.dumps({"type": "error", "message": prompt}))
            sys.stderr.write(prompt)
            sys.stderr.write(" Authorization: Bearer secret-token-value")
            sys.stderr.write(" api_key=sk-abcdefghijklmnop123456")
            raise SystemExit(1)
            """
        )
        attempt = self.attempt(prompt="private synthetic prompt")
        with self.assertRaises(ProviderProtocolError):
            CodexExecAdapter(self.root, fake, environment={"PATH": "/usr/bin"}).execute(attempt)

        persisted = b"".join(
            path.read_bytes() for path in self.workspace.iterdir() if path.name.startswith("provider-")
        )
        self.assertNotIn(b"private synthetic prompt", persisted)
        self.assertNotIn(b"secret-token-value", persisted)
        self.assertNotIn(b"sk-abcdefghijklmnop123456", persisted)
        self.assertIn(b"[REDACTED_PROMPT]", persisted)
        self.assertIn(b"[REDACTED_SECRET]", persisted)


if __name__ == "__main__":
    unittest.main()
