import json
from pathlib import Path
import stat
import tempfile
import textwrap
import unittest

from clipen.providers import (
    CodexAttempt,
    CodexExecAdapter,
    ProviderProtocolError,
    ProviderTimedOut,
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
        self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
        self.assertEqual(arguments[-1], "-")
        self.assertEqual(adapter.version(), "codex-cli fake-1")

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


if __name__ == "__main__":
    unittest.main()
