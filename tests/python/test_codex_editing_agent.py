from pathlib import Path
import tempfile
import unittest

from clipen.providers.codex_editing import CodexEditingAgent, _PROMPT


class FakeRepository:
    pass


class CodexEditingAgentTests(unittest.TestCase):
    def test_prompt_requires_short_trimmed_style_proof_before_full_edit(self):
        self.assertIn("45-60 second STYLE PROOF", _PROMPT)
        self.assertIn("1.5-5 seconds", _PROMPT)
        self.assertIn("untrimmed candidate by default", _PROMPT)
        self.assertIn("never the full film", _PROMPT)
        self.assertIn("no more than ten", _PROMPT)
        self.assertIn("empty parameters object", _PROMPT)

    def test_command_is_ephemeral_and_registers_only_project_scoped_mcp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            attempts = root / "attempts"
            workspace = attempts / "agent-test"
            projects.mkdir()
            attempts.mkdir()
            workspace.mkdir()
            agent = CodexEditingAgent(
                projects,
                attempts,
                FakeRepository(),
                binary="/bin/echo",
                python="/usr/bin/python3",
                environment={"PATH": "/usr/bin:/bin", "HOME": str(root)},
            )

            command = agent.command(
                "prj_" + "a" * 32,
                "editrun_" + "b" * 32,
                workspace,
                workspace / "final.txt",
            )
            joined = " ".join(command)

        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("--ask-for-approval", command)
        self.assertIn("mcp_servers.clipen.command", joined)
        self.assertIn("clipen.mcp_editing", joined)
        self.assertIn("mcp_servers.clipen.required=true", command)
        self.assertIn(
            'mcp_servers.clipen.default_tools_approval_mode="approve"',
            command,
        )
        self.assertIn("mcp_servers.clipen.enabled_tools", joined)
        self.assertIn("editrun_" + "b" * 32, joined)
        self.assertNotIn("dangerously-bypass", joined)

    def test_command_preserves_virtual_environment_python_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects"
            attempts = root / "attempts"
            workspace = attempts / "agent-test"
            projects.mkdir()
            attempts.mkdir()
            workspace.mkdir()
            venv = root / "venv"
            (venv / "bin").mkdir(parents=True)
            python = venv / "bin" / "python"
            python.symlink_to("/usr/bin/python3")
            agent = CodexEditingAgent(
                projects,
                attempts,
                FakeRepository(),
                binary="/bin/echo",
                python=python,
                environment={"PATH": "/usr/bin:/bin", "HOME": str(root)},
            )

            command = agent.command(
                "prj_" + "a" * 32,
                "editrun_" + "b" * 32,
                workspace,
                workspace / "final.txt",
            )

        self.assertIn(f'mcp_servers.clipen.command="{python}"', command)
        self.assertNotIn('mcp_servers.clipen.command="/usr/bin/python3"', command)


if __name__ == "__main__":
    unittest.main()
