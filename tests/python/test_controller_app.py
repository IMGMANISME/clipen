import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest

from clipen.controller import (
    ControllerConfig,
    LocalSessionAuthority,
    ProviderState,
    ProviderStatus,
    create_app,
)
from clipen.system_skills import SystemSkillSummary


@dataclass(frozen=True)
class ASGIResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None

    def json(self) -> object:
        return json.loads(self.body.decode("utf-8"))


async def asgi_request(
    app,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
) -> ASGIResponse:
    header_items = {
        "host": "localhost:4200",
        **(headers or {}),
    }
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (key.lower().encode("ascii"), value.encode("latin-1"))
            for key, value in header_items.items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 4200),
    }
    received = False
    messages = []

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(message for message in messages if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return ASGIResponse(
        status=start["status"],
        headers=tuple(
            (key.decode("latin-1"), value.decode("latin-1"))
            for key, value in start["headers"]
        ),
        body=body,
    )


class ControllerAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.static_root = Path(self.temporary.name)
        (self.static_root / "assets").mkdir()
        (self.static_root / "index.html").write_text("<main>Clipen</main>", encoding="utf-8")
        (self.static_root / "assets" / "app.js").write_text("export {}", encoding="utf-8")
        self.authority = LocalSessionAuthority("b" * 32)
        self.provider = ProviderStatus("Codex", ProviderState.READY, "AI 已連接")
        self.app = create_app(
            ControllerConfig(self.static_root), self.provider, self.authority
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
    ) -> ASGIResponse:
        return asyncio.run(asgi_request(self.app, method, path, headers))

    def bootstrap(self) -> ASGIResponse:
        return self.request(
            "POST",
            "/api/session/bootstrap",
            {
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "x-clipen-bootstrap": "b" * 32,
            },
        )

    def test_bootstrap_sets_private_cookie_and_status_requires_it(self) -> None:
        unauthenticated = self.request("GET", "/api/status")
        self.assertEqual(unauthenticated.status, 401)

        bootstrap = self.bootstrap()
        self.assertEqual(bootstrap.status, 200)
        set_cookie = bootstrap.header("set-cookie")
        self.assertIsNotNone(set_cookie)
        assert set_cookie is not None
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=strict", set_cookie)
        cookie = set_cookie.split(";", 1)[0]

        status = self.request("GET", "/api/status", {"cookie": cookie})
        self.assertEqual(status.status, 200)
        payload = status.json()
        assert isinstance(payload, dict)
        self.assertEqual(payload["provider"]["state"], "ready")
        self.assertTrue(payload["session"]["csrf_token"])

    def test_rejects_invalid_host_origin_and_cross_site_mutation(self) -> None:
        bad_host = self.request("GET", "/api/health", {"host": "example.com"})
        self.assertEqual(bad_host.status, 400)

        bad_origin = self.request(
            "POST",
            "/api/session/bootstrap",
            {
                "origin": "https://example.com",
                "sec-fetch-site": "cross-site",
                "x-clipen-bootstrap": "b" * 32,
            },
        )
        self.assertEqual(bad_origin.status, 403)

        missing_fetch_metadata = self.request(
            "POST",
            "/api/session/bootstrap",
            {
                "origin": "http://localhost:4200",
                "x-clipen-bootstrap": "b" * 32,
            },
        )
        self.assertEqual(missing_fetch_metadata.status, 403)

    def test_future_mutations_require_session_and_csrf(self) -> None:
        bootstrap = self.bootstrap()
        set_cookie = bootstrap.header("set-cookie")
        assert set_cookie is not None
        cookie = set_cookie.split(";", 1)[0]

        rejected = self.request(
            "POST",
            "/api/future-command",
            {
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "cookie": cookie,
            },
        )
        self.assertEqual(rejected.status, 403)
        self.assertEqual(rejected.json()["error"]["code"], "csrf_failed")

    def test_bootstrap_expires_but_is_idempotent_during_its_window(self) -> None:
        clock = [100.0]
        authority = LocalSessionAuthority("x" * 32, now=lambda: clock[0])
        first = authority.bootstrap("x" * 32)
        second = authority.bootstrap("x" * 32)
        self.assertEqual(first, second)

        clock[0] += 301
        self.assertIsNone(authority.bootstrap("x" * 32))

    def test_static_and_api_responses_have_security_cache_headers(self) -> None:
        index = self.request("GET", "/")
        self.assertEqual(index.status, 200)
        self.assertEqual(index.header("cache-control"), "no-store")
        self.assertEqual(index.header("x-frame-options"), "DENY")
        self.assertIn("frame-ancestors 'none'", index.header("content-security-policy") or "")

        asset = self.request("GET", "/assets/app.js")
        self.assertEqual(asset.status, 200)
        self.assertEqual(asset.header("cache-control"), "public, max-age=31536000, immutable")

        health = self.request("GET", "/api/health")
        self.assertEqual(health.status, 200)
        self.assertEqual(health.header("cache-control"), "no-store")

    def test_system_skill_catalog_requires_session_and_never_exposes_content(self) -> None:
        summary = SystemSkillSummary(
            "clipen-clean-cut",
            "通用精簡",
            "保留原始順序並改善節奏。",
            ("目標長度", "畫幅"),
        )
        self.app = create_app(
            ControllerConfig(self.static_root, (summary,)),
            self.provider,
            self.authority,
        )
        unauthenticated = self.request("GET", "/api/system-skills")
        self.assertEqual(unauthenticated.status, 401)

        bootstrap = self.bootstrap()
        cookie_header = bootstrap.header("set-cookie")
        assert cookie_header is not None
        catalog = self.request(
            "GET",
            "/api/system-skills",
            {"cookie": cookie_header.split(";", 1)[0]},
        )
        self.assertEqual(catalog.status, 200)
        payload = catalog.json()
        self.assertEqual(payload["skills"], [summary.as_public_dict()])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("SKILL.md", serialized)
        self.assertNotIn("content", serialized)
        self.assertNotIn("clipen-core", serialized)


if __name__ == "__main__":
    unittest.main()
