from pathlib import Path
import socket
import tempfile
import unittest
from unittest import mock

from clipen.controller import ProviderState
from clipen.controller.server import PortUnavailable, ServerPaths, _bind_socket, probe_codex


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

    def test_fixed_port_conflict_stops_instead_of_selecting_another_port(self) -> None:
        listener = mock.Mock()
        listener.bind.side_effect = OSError("already in use")
        with mock.patch("clipen.controller.server.socket.socket", return_value=listener):
            with self.assertRaises(PortUnavailable):
                _bind_socket()
        listener.setsockopt.assert_called_once_with(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )
        listener.close.assert_called_once_with()

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
