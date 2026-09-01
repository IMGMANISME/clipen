from pathlib import Path
import tempfile
import unittest
from unittest import mock

from clipen import cli
from clipen.system_skills import SystemSkillVerificationError


class CliTests(unittest.TestCase):
    def test_verifies_system_skills_before_provider_probe_and_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            release = mock.sentinel.release
            provider = mock.sentinel.provider
            calls: list[str] = []

            def verify(path: Path):
                calls.append("verify")
                self.assertEqual(path, root / "resources" / "system-skills")
                return release

            def probe(path: Path):
                calls.append("probe")
                self.assertEqual(path, data / "provider-attempts")
                return provider

            def run(paths, received_provider, authority, received_release):
                calls.append("run")
                self.assertEqual(received_provider, provider)
                self.assertEqual(received_release, release)
                return 0

            with (
                mock.patch("clipen.cli._repo_root", return_value=root),
                mock.patch("clipen.cli._data_root", return_value=data),
                mock.patch("clipen.cli.verify_system_skill_release", side_effect=verify),
                mock.patch("clipen.cli.probe_codex", side_effect=probe),
                mock.patch("clipen.cli.run_server", side_effect=run),
            ):
                self.assertEqual(cli.main(["codex"]), 0)

            self.assertEqual(calls, ["verify", "probe", "run"])

    def test_invalid_system_skill_release_stops_before_provider_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch("clipen.cli._repo_root", return_value=root),
                mock.patch("clipen.cli._data_root", return_value=root / "data"),
                mock.patch(
                    "clipen.cli.verify_system_skill_release",
                    side_effect=SystemSkillVerificationError("invalid"),
                ),
                mock.patch("clipen.cli.probe_codex") as probe,
                mock.patch("clipen.cli.run_server") as run,
            ):
                self.assertEqual(cli.main(["codex"]), 1)

            probe.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
