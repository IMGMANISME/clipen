import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

from clipen.controller import (
    ControllerConfig,
    LocalSessionAuthority,
    ProviderState,
    ProviderStatus,
    create_app,
)
from clipen.media import (
    InventoryCoordinator,
    InventoryRepository,
    InventoryState,
    MediaProbeResult,
    MediaStream,
)
from clipen.projects import DirectoryRole, NativeDirectoryPicker, ProjectWorkspace


class FakePicker(NativeDirectoryPicker):
    def __init__(self, *paths: Path | None) -> None:
        self.paths = list(paths)

    def choose(self, role: DirectoryRole) -> Path | None:
        del role
        return self.paths.pop(0)


class ApiProbe:
    def probe(self, root, asset, cancel=None) -> MediaProbeResult:
        del root, cancel
        return MediaProbeResult(
            asset.kind,
            "c" * 64,
            asset.size_bytes,
            asset.mtime_ns,
            {"duration_seconds": "1.0", "format_names": ["mov"]},
            (MediaStream(0, "video", "h264", {"width": 320, "height": 180}),),
        )


async def request_app(
    app,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
):
    route_path, separator, query = path.partition("?")
    combined = {"host": "localhost:4200", **(headers or {})}
    if body and "content-length" not in combined:
        combined["content-length"] = str(len(body))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": route_path,
        "raw_path": route_path.encode("ascii"),
        "query_string": query.encode("ascii") if separator else b"",
        "root_path": "",
        "headers": [
            (key.lower().encode("ascii"), value.encode("latin-1"))
            for key, value in combined.items()
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 4200),
    }
    sent_request = False
    messages = []

    async def receive():
        nonlocal sent_request
        if sent_request:
            return {"type": "http.disconnect"}
        sent_request = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(item for item in messages if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"")
        for item in messages
        if item["type"] == "http.response.body"
    )
    response_headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    return start["status"], response_headers, response_body


class ProjectApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.static = self.root / "dist"
        self.static.mkdir()
        (self.static / "index.html").write_text("Clipen", encoding="utf-8")
        self.source = self.root / "External" / "Raw"
        self.reference = self.root / "Reference"
        self.source.mkdir(parents=True)
        (self.source / "clip.mov").write_bytes(b"synthetic-media")
        self.reference.mkdir()
        self.authority = LocalSessionAuthority("b" * 32)
        self.workspace = ProjectWorkspace(
            self.root / "managed" / "projects",
            picker=FakePicker(self.source, self.reference),
        )
        self.inventory_repository = InventoryRepository(self.workspace.repository)
        self.inventories = InventoryCoordinator(
            self.inventory_repository,
            probe_factory=lambda: ApiProbe(),
        )
        self.app = create_app(
            ControllerConfig(self.static),
            ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
            self.authority,
            self.workspace,
            self.inventories,
        )
        self.cookie, self.csrf = self._connect()

    def tearDown(self) -> None:
        self.inventories.close()
        self.temporary.cleanup()

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        headers: dict[str, str] | None = None,
    ):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        return asyncio.run(
            request_app(self.app, method, path, headers=headers, body=body)
        )

    def _connect(self) -> tuple[str, str]:
        status, headers, _ = self._request(
            "POST",
            "/api/session/bootstrap",
            headers={
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "x-clipen-bootstrap": "b" * 32,
            },
        )
        self.assertEqual(status, 200)
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, body = self._request(
            "GET", "/api/status", headers={"cookie": cookie}
        )
        self.assertEqual(status, 200)
        csrf = json.loads(body)["session"]["csrf_token"]
        return cookie, csrf

    def mutation_headers(self) -> dict[str, str]:
        return {
            "origin": "http://localhost:4200",
            "sec-fetch-site": "same-origin",
            "content-type": "application/json",
            "cookie": self.cookie,
            "x-clipen-csrf": self.csrf,
        }

    @staticmethod
    def json_body(body: bytes) -> dict:
        return json.loads(body.decode("utf-8"))

    def create_project(self):
        status, _, body = self._request(
            "POST",
            "/api/commands/create-project",
            {"request_id": "request-api-create", "name": "咖啡教學"},
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 201)
        return self.json_body(body)["project"]

    def save_source(self, project: dict) -> dict:
        status, _, body = self._request(
            "POST",
            "/api/system/commands/select-directory",
            {"request_id": "request-api-inventory-pick", "role": "source"},
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 200)
        selection = self.json_body(body)["selection"]
        status, _, body = self._request(
            "POST",
            f"/api/projects/{project['project_id']}/commands/set-directory",
            {
                "request_id": "request-api-inventory-save",
                "expected_revision": project["revision"],
                "role": "source",
                "selection_token": selection["selection_token"],
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 200)
        return self.json_body(body)["project"]

    def test_create_list_get_and_update_project(self) -> None:
        project = self.create_project()
        status, _, body = self._request(
            "GET", "/api/projects", headers={"cookie": self.cookie}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.json_body(body)["projects"], [project])

        status, _, body = self._request(
            "POST",
            f"/api/projects/{project['project_id']}/commands/update-details",
            {
                "request_id": "request-api-update",
                "expected_revision": project["revision"],
                "name": "手沖咖啡教學",
                "brief": "希望步驟清楚、節奏俐落。",
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 200)
        updated = self.json_body(body)["project"]
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["brief"], "希望步驟清楚、節奏俐落。")

        status, _, body = self._request(
            "GET",
            f"/api/projects/{project['project_id']}",
            headers={"cookie": self.cookie},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.json_body(body)["project"], updated)

    def test_select_save_and_remove_directory_without_exposing_identity(self) -> None:
        project = self.create_project()
        status, _, body = self._request(
            "POST",
            "/api/system/commands/select-directory",
            {"request_id": "request-api-pick", "role": "source"},
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 200)
        selection = self.json_body(body)["selection"]
        self.assertNotIn("canonical_path", selection)
        self.assertNotIn("device", selection)

        status, _, body = self._request(
            "POST",
            f"/api/projects/{project['project_id']}/commands/set-directory",
            {
                "request_id": "request-api-save-directory",
                "expected_revision": project["revision"],
                "role": "source",
                "selection_token": selection["selection_token"],
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 200)
        saved = self.json_body(body)["project"]
        self.assertEqual(saved["directories"][0]["availability"], "available")
        self.assertNotIn("canonical_path", saved["directories"][0])
        self.assertNotIn("inode", saved["directories"][0])

        status, _, body = self._request(
            "POST",
            f"/api/projects/{project['project_id']}/commands/remove-directory",
            {
                "request_id": "request-api-remove-directory",
                "expected_revision": saved["revision"],
                "role": "source",
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.json_body(body)["project"]["directories"], [])

    def test_stale_revision_and_request_reuse_return_conflicts(self) -> None:
        project = self.create_project()
        command = {
            "request_id": "request-api-idempotent",
            "expected_revision": project["revision"],
            "name": "更新名稱",
            "brief": "",
        }
        path = f"/api/projects/{project['project_id']}/commands/update-details"
        first = self._request("POST", path, command, headers=self.mutation_headers())
        replay = self._request("POST", path, command, headers=self.mutation_headers())
        self.assertEqual(first[2], replay[2])

        stale = self._request(
            "POST",
            path,
            {
                "request_id": "request-api-stale",
                "expected_revision": project["revision"],
                "name": "過期名稱",
                "brief": "",
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(stale[0], 409)
        self.assertEqual(self.json_body(stale[2])["error"]["code"], "revision_conflict")

    def test_mutations_reject_missing_csrf_bad_json_and_large_body(self) -> None:
        unauthenticated = self._request("GET", "/api/projects")
        self.assertEqual(unauthenticated[0], 401)

        no_csrf = self._request(
            "POST",
            "/api/commands/create-project",
            {"request_id": "request-api-no-csrf", "name": "不應建立"},
            headers={
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "cookie": self.cookie,
            },
        )
        self.assertEqual(no_csrf[0], 403)

        invalid = asyncio.run(
            request_app(
                self.app,
                "POST",
                "/api/commands/create-project",
                headers=self.mutation_headers(),
                body=b"not-json",
            )
        )
        self.assertEqual(invalid[0], 400)

        too_large = asyncio.run(
            request_app(
                self.app,
                "POST",
                "/api/commands/create-project",
                headers=self.mutation_headers(),
                body=b"{" + b"x" * (64 * 1024) + b"}",
            )
        )
        self.assertEqual(too_large[0], 413)

    def test_inventory_api_starts_polls_and_lists_sanitized_results(self) -> None:
        project = self.save_source(self.create_project())
        status, _, body = self._request(
            "POST",
            f"/api/projects/{project['project_id']}/commands/start-inventory",
            {
                "request_id": "request-api-inventory-start",
                "expected_revision": project["revision"],
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 202)
        inventory = self.json_body(body)["inventory"]
        self.assertNotIn(str(self.source), body.decode("utf-8"))

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, body = self._request(
                "GET",
                f"/api/projects/{project['project_id']}/inventory",
                headers={"cookie": self.cookie},
            )
            self.assertEqual(status, 200)
            payload = self.json_body(body)
            if payload["latest"]["state"] == "succeeded":
                break
            time.sleep(0.01)
        else:
            self.fail("inventory API did not reach succeeded")

        self.assertEqual(payload["current"], payload["latest"])
        status, _, body = self._request(
            "GET",
            f"/api/projects/{project['project_id']}/inventories/"
            f"{inventory['run_id']}?offset=0&limit=25",
            headers={"cookie": self.cookie},
        )
        self.assertEqual(status, 200)
        page = self.json_body(body)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["relative_path"], "clip.mov")
        self.assertEqual(page["items"][0]["probe_status"], "ready")
        self.assertNotIn("fingerprint", repr(page))
        self.assertNotIn(str(self.source), repr(page))

        invalid = self._request(
            "GET",
            f"/api/projects/{project['project_id']}/inventories/"
            f"{inventory['run_id']}?limit=201",
            headers={"cookie": self.cookie},
        )
        self.assertEqual(invalid[0], 422)

    def test_inventory_cancel_endpoint_is_csrf_protected_and_idempotent(self) -> None:
        project = self.save_source(self.create_project())
        queued = self.inventory_repository.create_run(
            project["project_id"],
            "request-api-inventory-queued",
            project["revision"],
        )
        path = (
            f"/api/projects/{project['project_id']}/inventories/"
            f"{queued.run_id}/commands/cancel"
        )
        rejected = self._request(
            "POST",
            path,
            {},
            headers={
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "cookie": self.cookie,
            },
        )
        self.assertEqual(rejected[0], 403)

        first = self._request("POST", path, {}, headers=self.mutation_headers())
        replay = self._request("POST", path, {}, headers=self.mutation_headers())
        self.assertEqual(first[0], 200)
        self.assertEqual(first[2], replay[2])
        self.assertEqual(
            self.json_body(first[2])["inventory"]["state"],
            InventoryState.CANCELLED.value,
        )


if __name__ == "__main__":
    unittest.main()
