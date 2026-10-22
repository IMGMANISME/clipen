import json
from pathlib import Path
import tempfile
import unittest

from clipen.providers import (
    CodexAttempt,
    ProviderAttemptState,
    ProviderAuthenticationError,
    ProviderProtocolError,
)
from clipen.providers.claude import ClaudeExecAdapter
from clipen.providers.recovery import AttemptReceiptStore


SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"], "additionalProperties": False}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def fake_claude(root: Path, events: list[dict], *, exit_code: int = 0) -> Path:
    """A stand-in CLI that records its stdin and prints a fixed stream."""
    script = root / "claude"
    body = "\n".join(json.dumps(event) for event in events)
    script.write_text(
        "#!/usr/bin/python3\n"
        "import sys\n"
        f"open({str(root / 'stdin.jsonl')!r}, 'wb').write(sys.stdin.buffer.read())\n"
        f"sys.stdout.write({body + chr(10)!r})\n"
        f"sys.exit({exit_code})\n")
    script.chmod(0o700)
    return script


def init():
    return {"type": "system", "subtype": "init", "mcp_servers": []}


def success(payload):
    return {"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": payload,
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5}}


class ClaudeExecAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.attempts = self.root / "attempts"; self.attempts.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def attempt(self, name="plan-1", images=0):
        workspace = self.attempts / name; workspace.mkdir()
        schema = workspace / "schema.json"; schema.write_text(json.dumps(SCHEMA))
        paths = []
        for index in range(images):
            path = workspace / f"frame-{index}.png"; path.write_bytes(PNG); paths.append(path)
        return CodexAttempt(name, workspace, schema, "請產生標題", timeout_seconds=30, images=tuple(paths))

    def adapter(self, events, **kwargs):
        return ClaudeExecAdapter(self.attempts, fake_claude(self.root, events, **kwargs), {"PATH": "/usr/bin:/bin", "HOME": str(self.root)})

    def test_schema_bound_attempt_matches_codex_receipts_and_reconcile(self):
        adapter = self.adapter([init(), success({"title": "海邊的早餐"})])
        attempt = self.attempt(images=2)
        result = adapter.execute(attempt)
        self.assertEqual(result.payload, {"title": "海邊的早餐"})
        self.assertEqual(result.usage.to_dict(), {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5})
        receipt = AttemptReceiptStore(attempt.workspace, attempt.attempt_id).read()
        self.assertIs(receipt.state, ProviderAttemptState.SUCCEEDED)
        self.assertTrue((attempt.workspace / "provider-output-plan-1.json").is_file())
        reconciled = adapter.reconcile(attempt)
        self.assertIs(reconciled.state, ProviderAttemptState.SUCCEEDED)
        self.assertEqual(reconciled.result.payload, result.payload)
        # The approved snapshots travel as base64 image blocks next to the prompt.
        message = json.loads((self.root / "stdin.jsonl").read_text())
        content = message["message"]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "請產生標題"})
        self.assertEqual([block["type"] for block in content[1:]], ["image", "image"])
        self.assertEqual(content[1]["source"]["media_type"], "image/png")

    def test_command_disables_tools_mcp_and_user_customisation(self):
        adapter = self.adapter([])
        attempt = self.attempt()
        command = adapter.command(attempt, attempt.workspace / "provider-output-plan-1.json")
        for flag in ("--print", "--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(json.loads(command[command.index("--json-schema") + 1]), SCHEMA)
        self.assertNotIn("--mcp-config", command)

    def test_schema_mismatch_and_login_failures_are_typed_and_receipted(self):
        cases = [
            ([init(), success({"wrong": 1})], 0, ProviderProtocolError, ProviderAttemptState.FAILED),
            ([init(), {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"}], 1,
             ProviderAuthenticationError, ProviderAttemptState.FAILED),
            ([], 1, ProviderProtocolError, ProviderAttemptState.FAILED),
        ]
        for index, (events, code, error, state) in enumerate(cases):
            with self.subTest(index=index):
                adapter = self.adapter(events, exit_code=code)
                attempt = self.attempt(f"plan-{index + 10}")
                with self.assertRaises(error):
                    adapter.execute(attempt)
                self.assertIs(AttemptReceiptStore(attempt.workspace, attempt.attempt_id).read().state, state)
                with self.assertRaises(ProviderProtocolError):
                    adapter.execute(attempt)  # never replays an attempt that has a receipt


if __name__ == "__main__":
    unittest.main()
