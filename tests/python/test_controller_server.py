import errno
import io
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

from clipen.controller import LocalSessionAuthority, ProviderState, ProviderStatus
from clipen.controller.server import (
    ExistingController,
    PortUnavailable,
    ServerPaths,
    _InstanceLease,
    _bind_socket,
    _read_instance_record,
    _terminal_reopen_loop,
    probe_codex,
    run_server,
)


class FakeCapabilities:
    def __init__(self, supported: bool) -> None:
        self.supports_bounded_attempts = supported


class FakeProbe:
    def __init__(
        self,
        *,
        authenticated: bool = True,
        supported: bool = True,
        confined: bool = True,
    ) -> None:
        self.authenticated = authenticated
        self.capabilities = FakeCapabilities(supported)
        self.confinement_verified = confined


class ControllerServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.attempt_root = self.root / "attempts"
        self.attempt_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_provider_probe_maps_product_states(self) -> None:
        cases = (
            (FakeProbe(), ProviderState.READY),
            (FakeProbe(authenticated=False), ProviderState.LOGIN_REQUIRED),
            (FakeProbe(supported=False), ProviderState.UNSUPPORTED),
            (FakeProbe(confined=False), ProviderState.CONFINEMENT_FAILED),
        )
        for result, expected in cases:
            with self.subTest(expected=expected):
                adapter = mock.Mock()
                adapter.probe.return_value = result
                with mock.patch(
                    "clipen.controller.server.CodexExecAdapter", return_value=adapter
                ):
                    status = probe_codex(self.attempt_root)
                self.assertEqual(status.state, expected)

    def test_occupied_primary_port_uses_the_next_available_port(self) -> None:
        occupied = mock.Mock()
        occupied.bind.side_effect = OSError(errno.EADDRINUSE, "already in use")
        available = mock.Mock()
        with mock.patch(
            "clipen.controller.server.socket.socket",
            side_effect=(occupied, available),
        ):
            listener, port = _bind_socket(4200, 4201)

        self.assertIs(listener, available)
        self.assertEqual(port, 4201)
        occupied.close.assert_called_once_with()
        available.setsockopt.assert_called_once_with(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )
        available.bind.assert_called_once_with(("127.0.0.1", 4201))
        available.listen.assert_called_once_with(2048)

    def test_port_search_never_terminates_the_occupying_process(self) -> None:
        listeners = (mock.Mock(), mock.Mock())
        for listener in listeners:
            listener.bind.side_effect = OSError(errno.EADDRINUSE, "already in use")
        with mock.patch(
            "clipen.controller.server.socket.socket", side_effect=listeners
        ):
            with self.assertRaises(PortUnavailable):
                _bind_socket(4200, 4201)
        for listener in listeners:
            listener.close.assert_called_once_with()

    def test_instance_lease_is_private_exclusive_and_removes_its_registry(self) -> None:
        lease = _InstanceLease.acquire(self.root)
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertIsNone(_InstanceLease.acquire(self.root))
        lease.publish(4207)
        self.assertEqual(
            _read_instance_record(self.root / ".controller.json"),
            (4207, lease.secret),
        )
        self.assertEqual((self.root / ".controller.json").stat().st_mode & 0o777, 0o600)

        lease.close()
        self.assertFalse((self.root / ".controller.json").exists())
        replacement = _InstanceLease.acquire(self.root)
        self.assertIsNotNone(replacement)
        assert replacement is not None
        replacement.close()

    def test_instance_lease_fails_clearly_without_posix_file_locking(self) -> None:
        with mock.patch("clipen.controller.server._fcntl", None):
            with self.assertRaisesRegex(RuntimeError, "尚不支援 Windows"):
                _InstanceLease.acquire(self.root)

        self.assertFalse((self.root / ".controller.lock").exists())

    def test_second_launch_reopens_the_authenticated_existing_controller(self) -> None:
        static = self.root / "dist"
        static.mkdir()
        (static / "index.html").write_text("clipen", encoding="utf-8")
        existing = ExistingController(
            "http://localhost:4207",
            "http://localhost:4207/#bootstrap=fresh-token",
        )
        opened: list[str] = []
        output = io.StringIO()
        with (
            mock.patch(
                "clipen.controller.server._InstanceLease.acquire", return_value=None
            ),
            mock.patch(
                "clipen.controller.server._find_existing_controller",
                return_value=existing,
            ),
            mock.patch("sys.stdout", output),
        ):
            result = run_server(
                ServerPaths(static, self.attempt_root),
                ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
                LocalSessionAuthority("z" * 32),
                mock.Mock(),
                browser_open=lambda url: opened.append(url) is None,
            )

        self.assertEqual(result, 0)
        self.assertEqual(opened, [existing.connect_url])
        self.assertIn("Clipen 已在執行", output.getvalue())
        self.assertIn("UI：\nhttp://localhost:4207\n", output.getvalue())

    def test_pressing_enter_renews_handoff_and_reopens_the_browser(self) -> None:
        authority = LocalSessionAuthority("t" * 32)
        opened: list[str] = []
        input_stream = io.StringIO("\n")
        with mock.patch.object(sys, "stdin", input_stream):
            _terminal_reopen_loop(
                "http://localhost:4201",
                authority,
                lambda url: opened.append(url) is None,
                lambda: False,
            )
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].startswith("http://localhost:4201/#bootstrap="))
        self.assertNotIn("t" * 32, opened[0])

    def test_server_paths_reject_symlink_attempt_root(self) -> None:
        static = self.root / "dist"
        static.mkdir()
        (static / "index.html").write_text("clipen", encoding="utf-8")
        real_attempts = self.root / "real-attempts"
        real_attempts.mkdir()
        linked = self.root / "linked-attempts"
        linked.symlink_to(real_attempts, target_is_directory=True)

        with self.assertRaises(ValueError):
            ServerPaths(static, linked).prepare()


if __name__ == "__main__":
    unittest.main()
