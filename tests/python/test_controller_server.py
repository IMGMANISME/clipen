import asyncio
import errno
import http.client
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clipen.controller import LocalSessionAuthority, ProviderState, ProviderStatus
from clipen.controller.server import (
    ExistingController,
    PortUnavailable,
    ServerPaths,
    _bind_socket,
    _InstanceLease,
    _print_ui_url,
    _read_instance_record,
    _serve,
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
                with mock.patch("clipen.controller.server.CodexExecAdapter", return_value=adapter):
                    status = probe_codex(self.attempt_root)
                self.assertEqual(status.state, expected)

    def test_occupied_primary_port_uses_an_os_assigned_port(self) -> None:
        occupied = mock.Mock()
        occupied.bind.side_effect = OSError(errno.EADDRINUSE, "already in use")
        available = mock.Mock()
        available.getsockname.return_value = ("127.0.0.1", 53127)
        with mock.patch(
            "clipen.controller.server.socket.socket",
            side_effect=(occupied, available),
        ):
            listener, port = _bind_socket()

        self.assertIs(listener, available)
        self.assertEqual(port, 53127)
        occupied.close.assert_called_once_with()
        available.setsockopt.assert_called_once_with(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )
        available.bind.assert_called_once_with(("127.0.0.1", 0))
        available.listen.assert_called_once_with(2048)

    def test_port_search_never_terminates_the_occupying_process(self) -> None:
        listeners = (mock.Mock(), mock.Mock())
        for listener in listeners:
            listener.bind.side_effect = OSError(errno.EADDRINUSE, "already in use")
        with mock.patch("clipen.controller.server.socket.socket", side_effect=listeners):
            with self.assertRaises(PortUnavailable):
                _bind_socket()
        for listener in listeners:
            listener.close.assert_called_once_with()

    def test_primary_port_is_preferred_when_available(self) -> None:
        available = mock.Mock()
        available.getsockname.return_value = ("127.0.0.1", 4200)
        with mock.patch("clipen.controller.server.socket.socket", return_value=available) as factory:
            listener, port = _bind_socket()
        self.assertIs(listener, available)
        self.assertEqual(port, 4200)
        factory.assert_called_once()
        available.bind.assert_called_once_with(("127.0.0.1", 4200))

    def test_restricted_preferred_port_also_falls_back(self) -> None:
        restricted, available = mock.Mock(), mock.Mock()
        restricted.bind.side_effect = OSError(errno.EACCES, "restricted")
        available.getsockname.return_value = ("127.0.0.1", 53127)
        with mock.patch("clipen.controller.server.socket.socket", side_effect=(restricted, available)):
            listener, port = _bind_socket()
        self.assertIs(listener, available)
        self.assertEqual(port, 53127)
        restricted.close.assert_called_once_with()

    def test_unexpected_socket_failure_is_reported_and_closed(self) -> None:
        failed = mock.Mock()
        failed.bind.side_effect = OSError(errno.EMFILE, "too many files")
        with mock.patch("clipen.controller.server.socket.socket", return_value=failed):
            with self.assertRaises(PortUnavailable):
                _bind_socket()
        failed.close.assert_called_once_with()

    def test_instance_lease_is_private_exclusive_and_removes_its_registry(self) -> None:
        lease = _InstanceLease.acquire(self.root)
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertIsNone(_InstanceLease.acquire(self.root))
        lease.publish(53127)
        self.assertEqual(
            _read_instance_record(self.root / ".controller.json"),
            (53127, lease.secret),
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

    def test_dynamic_port_serves_sessions_and_second_launch_reopens_it(self) -> None:
        # Reserve a real port without interfering with an existing user server.
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(occupied.close)
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        occupied_port = occupied.getsockname()[1]
        static = self.root / "dist"
        static.mkdir()
        (static / "index.html").write_text("clipen", encoding="utf-8")
        paths = ServerPaths(static, self.attempt_root)
        lease = _InstanceLease.acquire(self.root)
        assert lease is not None
        provider = ProviderStatus("Codex", ProviderState.LOGIN_REQUIRED, "請登入")

        def request(port, method, path, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request(method, path, headers={"Host": f"localhost:{port}", **(headers or {})})
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()

        async def exercise():
            from clipen.controller import server as module

            ready = asyncio.Event()
            opened = []
            servers = []
            server_class = module.uvicorn.Server

            def server_factory(config):
                server = server_class(config)
                servers.append(server)
                return server

            def browser_open(url):
                opened.append(url)
                ready.set()
                return True

            with (
                mock.patch.object(module, "_bind_socket", side_effect=lambda: _bind_socket(occupied_port)),
                mock.patch.object(module.uvicorn, "Server", side_effect=server_factory),
                mock.patch.object(sys, "stdin", io.StringIO()),
                mock.patch.object(sys, "stdout", io.StringIO()),
            ):
                serving = asyncio.create_task(
                    _serve(paths, provider, LocalSessionAuthority(), browser_open=browser_open, lease=lease)
                )
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                    record = _read_instance_record(self.root / ".controller.json")
                    assert record is not None
                    port, _ = record
                    self.assertNotEqual(port, occupied_port)
                    url = f"http://localhost:{port}"
                    self.assertEqual(opened, [url])
                    self.assertEqual(servers[0].config.port, port)
                    status, _, body = await asyncio.to_thread(request, port, "GET", "/")
                    self.assertEqual((status, body), (200, b"clipen"))
                    mutation = {"Origin": url, "Sec-Fetch-Site": "same-origin"}
                    status, headers, _ = await asyncio.to_thread(
                        request, port, "POST", "/api/session/connect", mutation
                    )
                    self.assertEqual(status, 200)
                    cookie = headers["set-cookie"].split(";", 1)[0]
                    status, _, body = await asyncio.to_thread(request, port, "GET", "/api/status", {"Cookie": cookie})
                    self.assertEqual(status, 200)
                    csrf = json.loads(body)["session"]["csrf_token"]
                    status, _, _ = await asyncio.to_thread(
                        request,
                        port,
                        "POST",
                        "/api/future-command",
                        {**mutation, "Cookie": cookie, "X-Clipen-CSRF": csrf},
                    )
                    self.assertEqual(status, 405)  # Passed authentication and CSRF.
                    status, _, _ = await asyncio.to_thread(
                        request,
                        port,
                        "POST",
                        "/api/session/connect",
                        {**mutation, "Origin": f"http://localhost:{occupied_port}"},
                    )
                    self.assertEqual(status, 403)
                    status, _, _ = await asyncio.to_thread(
                        request, port, "GET", "/api/health", {"Host": f"localhost:{occupied_port}"}
                    )
                    self.assertEqual(status, 400)
                    status, _, _ = await asyncio.to_thread(
                        request, port, "POST", "/api/instance/reopen", {"X-Clipen-Instance": "incorrect"}
                    )
                    self.assertEqual(status, 404)
                    result = await asyncio.to_thread(
                        run_server, paths, provider, LocalSessionAuthority(), browser_open=browser_open
                    )
                    self.assertEqual(result, 0)
                    self.assertEqual(opened, [url, url])
                    self.assertEqual(len(servers), 1)
                finally:
                    for server in servers:
                        server.should_exit = True
                    await asyncio.wait_for(serving, 5)

        try:
            asyncio.run(exercise())
        finally:
            lease.close()
        self.assertFalse((self.root / ".controller.json").exists())
        self.assertEqual(occupied.getsockname()[1], occupied_port)

    def test_registry_rejects_invalid_tcp_ports(self) -> None:
        path = self.root / ".controller.json"
        for port in (0, -1, 65536, True, "53127"):
            with self.subTest(port=port):
                path.write_text(json.dumps({"schema": 1, "port": port, "secret": "s" * 32}))
                path.chmod(0o600)
                self.assertIsNone(_read_instance_record(path))

    def test_second_launch_reopens_the_authenticated_existing_controller(self) -> None:
        static = self.root / "dist"
        static.mkdir()
        (static / "index.html").write_text("clipen", encoding="utf-8")
        existing = ExistingController("http://localhost:4207")
        opened: list[str] = []
        output = io.StringIO()
        with (
            mock.patch("clipen.controller.server._InstanceLease.acquire", return_value=None),
            mock.patch(
                "clipen.controller.server._find_existing_controller",
                return_value=existing,
            ),
            mock.patch("sys.stdout", output),
        ):
            result = run_server(
                ServerPaths(static, self.attempt_root),
                ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
                LocalSessionAuthority(),
                browser_open=lambda url: opened.append(url) is None,
            )

        self.assertEqual(result, 0)
        self.assertEqual(opened, [existing.public_url])
        self.assertIn("Clipen 已在執行", output.getvalue())
        self.assertIn("UI：\nhttp://localhost:4207\n", output.getvalue())

    def test_pressing_enter_reopens_the_fixed_ui_url(self) -> None:
        opened: list[str] = []
        input_stream = io.StringIO("\n")
        with mock.patch.object(sys, "stdin", input_stream):
            _terminal_reopen_loop(
                "http://localhost:4201",
                lambda url: opened.append(url) is None,
                lambda: False,
            )
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0], "http://localhost:4201")

    def test_terminal_prints_the_fixed_url_as_a_clickable_link(self) -> None:
        output = io.StringIO()
        with (
            mock.patch("sys.stdout", output),
            mock.patch.object(output, "isatty", return_value=True),
        ):
            _print_ui_url("http://localhost:4201")

        rendered = output.getvalue()
        self.assertIn("\033]8;;http://localhost:4201\033\\", rendered)
        self.assertIn("http://localhost:4201", rendered)

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
