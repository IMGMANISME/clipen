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
from clipen.analysis import PreparationRepository
from clipen.media import (
    InventoryCoordinator,
    InventoryRepository,
    InventoryState,
    MediaProbeResult,
    MediaStream,
)
from clipen.projects import DirectoryRole, NativeDirectoryPicker, ProjectWorkspace
from clipen.projects import TimelineValidationRepository
from clipen.domain import ValidationIssue, ValidationReport, ValidationSeverity


class FakePicker(NativeDirectoryPicker):
    def __init__(self, *paths: Path | None) -> None:
        self.paths = list(paths)

    def choose(self, role: DirectoryRole) -> Path | None:
        del role
        return self.paths.pop(0)


class ApiProbe:
    def version(self) -> str:
        return "ffprobe version fake"

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


class ApiPreparationCoordinator:
    def __init__(self, repository: PreparationRepository) -> None:
        self.repository = repository

    def start(
        self,
        project_id,
        request_id,
        expected_revision,
        scope_id,
        language="zh",
        vocabulary=(),
    ):
        return self.repository.create_run_with_status(
            project_id,
            request_id,
            expected_revision,
            scope_id,
            recipe={"id": "test", "language": language, "vocabulary": vocabulary},
            model={"id": "test"},
        )[0]

    def cancel(self, project_id, run_id):
        return self.repository.request_cancel(project_id, run_id)

    def resume(self, project_id, run_id, request_id):
        return self.repository.resume_interrupted(project_id, run_id, request_id)[0]


class ApiAnalysisRuntime:
    def __init__(self, repository: PreparationRepository) -> None:
        self.repository = repository
        self.state = "setup_required"

    def coordinator(self):
        return None

    def public_status(self):
        return {
            "state": self.state,
            "phase": "not_installed" if self.state == "setup_required" else self.state,
            "message": "test ASR setup",
            "error_code": None,
            "completed_units": 0,
            "total_units": 3,
            "notice": {
                "version": "asr-qwen3-1.7b-confidence-v1",
                "download_bytes": 1_200_000_000,
                "installed_bytes": 1_700_000_000,
                "install_location": "/managed/asr",
                "model": {
                    "name": "Fun-ASR Nano 高可信辨識",
                    "source": "https://example.invalid/model",
                    "revision": "fixed",
                    "model_bytes": 481_307_858,
                },
                "license": {
                    "software": "MIT",
                    "software_url": "https://example.invalid/software-license",
                    "model": "Apache-2.0",
                    "model_url": "https://example.invalid/model-license",
                    "notice": "fixed model notice",
                },
                "removable": True,
            },
        }

    def remove(self):
        self.state = "setup_required"


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
        self.authority = LocalSessionAuthority()
        self.workspace = ProjectWorkspace(
            self.root / "managed" / "projects",
            picker=FakePicker(self.source, self.reference),
        )
        self.inventory_repository = InventoryRepository(self.workspace.repository)
        self.inventories = InventoryCoordinator(
            self.inventory_repository,
            probe_factory=lambda: ApiProbe(),
        )
        self.preparation_repository = PreparationRepository(self.workspace.repository)
        self.preparations = ApiPreparationCoordinator(self.preparation_repository)
        self.timeline_validations = TimelineValidationRepository(
            self.workspace.repository
        )
        self.app = create_app(
            ControllerConfig(self.static),
            ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
            self.authority,
            self.workspace,
            self.inventories,
            self.preparations,
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
            "/api/session/connect",
            headers={
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
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

    def test_timeline_validation_status_and_warning_acceptance(self) -> None:
        project = self.create_project()
        path = f"/api/projects/{project['project_id']}/timeline-validation"
        status, _, body = self._request("GET", path, headers={"cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertEqual(self.json_body(body)["state"], "not_ready")

        report = ValidationReport(
            "report_api_warning",
            "timeline_api_primary",
            "capability_api_resolve",
            "b" * 64,
            (
                ValidationIssue(
                    "effect.preview_only",
                    ValidationSeverity.WARNING,
                    "/tracks/0/items/0",
                    "這個效果需要確認。",
                    "播放預覽後再繼續。",
                    "clip_api_title",
                ),
            ),
        )
        self.timeline_validations.publish(project["project_id"], report)
        status, _, body = self._request("GET", path, headers={"cookie": self.cookie})
        payload = self.json_body(body)
        self.assertEqual(payload["state"], "needs_confirmation")
        self.assertFalse(payload["can_execute"])

        command_path = path + "/commands/accept-warnings"
        rejected = self._request(
            "POST",
            command_path,
            {"request_id": "request-api-accept-warning", "report_id": report.report_id},
            headers={
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "cookie": self.cookie,
            },
        )
        self.assertEqual(rejected[0], 403)

        accepted = self._request(
            "POST",
            command_path,
            {"request_id": "request-api-accept-warning", "report_id": report.report_id},
            headers=self.mutation_headers(),
        )
        replay = self._request(
            "POST",
            command_path,
            {"request_id": "request-api-accept-warning", "report_id": report.report_id},
            headers=self.mutation_headers(),
        )
        self.assertEqual(accepted[0], 200)
        self.assertEqual(accepted[2], replay[2])
        self.assertTrue(self.json_body(accepted[2])["can_execute"])

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

    def test_analysis_preflight_start_and_cancel_are_sanitized(self) -> None:
        project = self.save_source(self.create_project())
        started = self._request(
            "POST",
            f"/api/projects/{project['project_id']}/commands/start-inventory",
            {
                "request_id": "request-api-analysis-inventory",
                "expected_revision": project["revision"],
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(started[0], 202)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, _ = self._request(
                "GET",
                f"/api/projects/{project['project_id']}/inventory",
                headers={"cookie": self.cookie},
            )
            self.assertEqual(status, 200)
            current = self.inventory_repository.current_run(project["project_id"])
            if current is not None:
                break
            time.sleep(0.01)
        else:
            self.fail("inventory did not finish before analysis preflight")

        status, _, body = self._request(
            "GET",
            f"/api/projects/{project['project_id']}/analysis",
            headers={"cookie": self.cookie},
        )
        self.assertEqual(status, 200)
        analysis = self.json_body(body)
        self.assertEqual(analysis["runtime"]["state"], "ready")
        self.assertEqual(analysis["scope"]["counts"]["no_audio"], 1)
        self.assertNotIn(str(self.source), body.decode("utf-8"))

        status, _, body = self._request(
            "POST",
            f"/api/projects/{project['project_id']}/commands/start-analysis",
            {
                "request_id": "request-api-analysis-start",
                "expected_revision": project["revision"],
                "scope_id": analysis["scope"]["scope_id"],
                "language": "ja",
                "vocabulary": ["浅草", "スカイツリー"],
            },
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 202)
        run = self.json_body(body)["analysis"]
        self.assertEqual(run["state"], "queued")
        self.assertEqual(
            self.preparation_repository.recipe(project["project_id"], run["run_id"]),
            {
                "id": "test",
                "language": "ja",
                "vocabulary": ["浅草", "スカイツリー"],
            },
        )

        results_path = (
            f"/api/projects/{project['project_id']}/analyses/"
            f"{run['run_id']}/results"
        )
        unauthorized, _, _ = self._request("GET", results_path)
        self.assertEqual(unauthorized, 401)
        result_status, _, result_body = self._request(
            "GET", results_path, headers={"cookie": self.cookie}
        )
        self.assertEqual(result_status, 200)
        results = self.json_body(result_body)["results"]
        self.assertEqual(results["total"], 1)
        self.assertEqual(results["items"][0]["display_name"], "clip.mov")
        self.assertEqual(results["items"][0]["status"], "no_audio")
        self.assertNotIn(str(self.source), result_body.decode("utf-8"))
        invalid_status, _, invalid_body = self._request(
            "GET",
            f"{results_path}?offset=-1",
            headers={"cookie": self.cookie},
        )
        self.assertEqual(invalid_status, 422)
        self.assertEqual(self.json_body(invalid_body)["error"]["code"], "pagination_invalid")

        event_status, event_headers, _ = self._request(
            "GET",
            f"/api/projects/{project['project_id']}/analyses/{run['run_id']}/events",
            headers={"cookie": self.cookie},
        )
        self.assertEqual(event_status, 200)
        self.assertTrue(event_headers["content-type"].startswith("text/event-stream"))
        status, _, body = self._request(
            "POST",
            f"/api/projects/{project['project_id']}/analyses/"
            f"{run['run_id']}/commands/cancel",
            {},
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.json_body(body)["analysis"]["state"], "cancelled")

    def test_analysis_status_explains_optional_runtime_setup(self) -> None:
        app = create_app(
            ControllerConfig(self.static),
            ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
            self.authority,
            self.workspace,
            self.inventories,
            preparation_repository=self.preparation_repository,
        )
        self.app = app
        status, _, body = self._request(
            "GET",
            f"/api/projects/{self.create_project()['project_id']}/analysis",
            headers={"cookie": self.cookie},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.json_body(body)["runtime"]["state"], "setup_required")

    def test_managed_asr_removal_requires_csrf_and_stays_recoverable(self) -> None:
        runtime = ApiAnalysisRuntime(self.preparation_repository)
        runtime.state = "ready"
        self.app = create_app(
            ControllerConfig(self.static),
            ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
            self.authority,
            self.workspace,
            self.inventories,
            preparation_repository=self.preparation_repository,
            analysis_runtime=runtime,
        )
        status, _, body = self._request(
            "POST",
            "/api/system/commands/remove-asr",
            {},
            headers={
                "cookie": self.cookie,
                "content-type": "application/json",
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.json_body(body)["error"]["code"], "csrf_failed")

        status, _, body = self._request(
            "POST",
            "/api/system/commands/remove-asr",
            {},
            headers=self.mutation_headers(),
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.json_body(body)["runtime"]["state"], "setup_required")


if __name__ == "__main__":
    unittest.main()
