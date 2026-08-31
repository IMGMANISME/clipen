import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import clipen.bootstrap as bootstrap


class TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        (self.repo / "requirements").mkdir(parents=True)
        (self.repo / "pyproject.toml").write_text("[project]\nname='clipen'\n", encoding="utf-8")
        (self.repo / "requirements" / "runtime.txt").write_text(
            "starlette==1.6.0\n", encoding="utf-8"
        )
        self.data = self.root / "data"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fingerprint_changes_with_runtime_contract(self) -> None:
        first = bootstrap.runtime_fingerprint(self.repo)
        (self.repo / "requirements" / "runtime.txt").write_text(
            "starlette==1.5.1\n", encoding="utf-8"
        )
        second = bootstrap.runtime_fingerprint(self.repo)
        self.assertNotEqual(first, second)

    def test_declining_first_setup_leaves_no_runtime(self) -> None:
        output = io.StringIO()
        runtime = bootstrap.ensure_runtime(
            self.repo,
            data_root=self.data,
            stdin=TTYInput("n\n"),
            stdout=output,
        )
        self.assertIsNone(runtime)
        self.assertIn("已取消", output.getvalue())
        self.assertEqual(list((self.data / "runtimes").glob("[0-9a-f]*")), [])

    def test_noninteractive_first_setup_fails_closed(self) -> None:
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.ensure_runtime(
                self.repo,
                data_root=self.data,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
            )

    def test_successful_runtime_is_reused_without_prompt_or_reinstall(self) -> None:
        installs = []

        def fake_install(staging: Path, repo: Path, fingerprint: str, log_path: Path) -> None:
            installs.append(staging)
            python = bootstrap.runtime_python(staging)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o700)
            receipt = {
                "schema": 1,
                "fingerprint": fingerprint,
                "repo_root": str(repo.resolve()),
            }
            (staging / "clipen-runtime.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            log_path.write_text("ok", encoding="utf-8")

        with mock.patch("clipen.bootstrap._install_runtime", side_effect=fake_install):
            first = bootstrap.ensure_runtime(
                self.repo,
                data_root=self.data,
                stdin=TTYInput("y\n"),
                stdout=io.StringIO(),
            )
            second = bootstrap.ensure_runtime(
                self.repo,
                data_root=self.data,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
            )

        self.assertEqual(first, second)
        self.assertEqual(len(installs), 1)
        assert first is not None
        self.assertTrue(bootstrap.runtime_is_ready(
            first, bootstrap.runtime_fingerprint(self.repo), self.repo
        ))


if __name__ == "__main__":
    unittest.main()
