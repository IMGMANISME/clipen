import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from clipen.controller import (
    ControllerConfig,
    LocalSessionAuthority,
    ProviderState,
    ProviderStatus,
    create_app,
)
from clipen.domain import Rational, SourceSpan
from clipen.planning import (
    PlanningCandidate,
    PlanningContext,
    PlanningRepository,
    PlanningService,
    TranscriptReliability,
)
from clipen.projects import ProjectWorkspace
from clipen.providers import ProviderUsage
from clipen.system_skills import SystemSkillSummary
from test_project_api import request_app


class ApiContextBuilder:
    def __init__(self, context) -> None:
        self.context = context

    def build(self, project_id):
        if project_id != self.context.project_id:
            raise AssertionError("unexpected project")
        return self.context


class ApiSkills:
    release_id = "skills-release-1"
    summaries = (
        SystemSkillSummary(
            "clipen-story-vlog",
            "敘事 Vlog",
            "依事件脈絡整理素材。",
            ("目標長度",),
        ),
    )

    def attempt_snapshot(self, editing_skill_id):
        if editing_skill_id != "clipen-story-vlog":
            raise KeyError(editing_skill_id)
        return "private core rules", "private editing rules"


class ApiProvider:
    calls = 0

    def __init__(self, root):
        self.root = root

    def probe(self):
        return SimpleNamespace(available=True)

    def execute(self, attempt, cancel):
        type(self).calls += 1
        return SimpleNamespace(
            usage=ProviderUsage(120, 20, 40),
            payload={
                "schema_version": 1,
                "title": "東京旅行初剪",
                "summary": "依事件順序整理。",
                "sections": [
                    {
                        "section_id": "section-001",
                        "title": "抵達",
                        "purpose": "建立旅程起點。",
                        "candidate_ids": ["candidate-001"],
                        "edit_notes": "保留現場聲。",
                        "unknowns": [],
                    }
                ],
            },
        )


class PlanningApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        static = self.root / "dist"
        static.mkdir()
        (static / "index.html").write_text("Clipen", encoding="utf-8")
        self.workspace = ProjectWorkspace(self.root / "projects")
        self.project = self.workspace.repository.create_project(
            "request-planning-api-project", "東京 Vlog"
        )
        context = PlanningContext(
            self.project.project_id,
            "剪成自然的旅行紀錄。",
            "inv_" + "b" * 32,
            "analysis_" + "c" * 32,
            (
                PlanningCandidate(
                    "candidate-001",
                    SourceSpan(
                        "av_" + "a" * 32,
                        0,
                        "0",
                        "900000",
                        Rational(1, 90000),
                    ),
                    "毛片 001",
                    "video",
                    "車站外的移動畫面。",
                    TranscriptReliability.NEEDS_REVIEW,
                ),
            ),
        )
        attempts = self.root / "attempts"
        attempts.mkdir()
        ApiProvider.calls = 0
        self.service = PlanningService(
            PlanningRepository(self.workspace.repository),
            attempts,
            Path("schemas/v1/plan-proposal.schema.json"),
            ApiSkills(),
            adapter_factory=ApiProvider,
        )
        self.service.context_builder = ApiContextBuilder(context)
        self.context = context
        self.authority = LocalSessionAuthority()
        self.app = create_app(
            ControllerConfig(static, ApiSkills.summaries),
            ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
            self.authority,
            self.workspace,
            planning_service=self.service,
        )
        self.cookie, self.csrf = self._connect()

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def _request(self, method, path, payload=None, headers=None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        return asyncio.run(
            request_app(self.app, method, path, headers=headers, body=body)
        )

    def _connect(self):
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
        return cookie, json.loads(body)["session"]["csrf_token"]

    def read_headers(self):
        return {"cookie": self.cookie}

    def mutation_headers(self):
        return {
            "origin": "http://localhost:4200",
            "sec-fetch-site": "same-origin",
            "content-type": "application/json",
            "cookie": self.cookie,
            "x-clipen-csrf": self.csrf,
        }

    def wait(self):
        deadline = time.monotonic() + 5
        path = f"/api/projects/{self.project.project_id}/planning"
        while time.monotonic() < deadline:
            status, _, body = self._request("GET", path, headers=self.read_headers())
            self.assertEqual(status, 200)
            payload = json.loads(body)
            if payload["latest_run"] and payload["latest_run"]["state"] not in {
                "queued",
                "running",
            }:
                return payload
            time.sleep(0.01)
        self.fail("planning API did not settle")

    def test_secure_start_edit_and_confirm_flow(self) -> None:
        base = f"/api/projects/{self.project.project_id}"
        unauthorized = self._request("GET", base + "/planning")
        self.assertEqual(unauthorized[0], 401)
        status, _, body = self._request(
            "GET", base + "/planning", headers=self.read_headers()
        )
        self.assertEqual(status, 200)
        planning = json.loads(body)
        self.assertEqual(planning["preflight"]["candidate_count"], 1)
        self.assertNotIn("av_", body.decode("utf-8"))

        command = {
            "request_id": "request-planning-api-start",
            "expected_revision": self.project.revision,
            "editing_skill_id": "clipen-story-vlog",
            "consent": True,
            "expected_context_hash": self.context.content_hash,
        }
        forbidden = self._request(
            "POST",
            base + "/planning/commands/start",
            command,
            {"cookie": self.cookie, "content-type": "application/json"},
        )
        self.assertEqual(forbidden[0], 403)
        accepted = self._request(
            "POST",
            base + "/planning/commands/start",
            command,
            self.mutation_headers(),
        )
        self.assertEqual(accepted[0], 202)
        completed = self.wait()
        self.assertEqual(completed["latest_run"]["state"], "succeeded")
        self.assertEqual(ApiProvider.calls, 1)
        plan = completed["current_plan"]

        editable = {
            "schema_version": 1,
            "title": plan["title"],
            "summary": "改成更精簡的旅程順序。",
            "sections": plan["sections"],
        }
        update = self._request(
            "POST",
            base + "/plan/commands/update",
            {
                "request_id": "request-planning-api-update",
                "plan_id": plan["plan_id"],
                "expected_revision": plan["revision"],
                "plan": editable,
            },
            self.mutation_headers(),
        )
        self.assertEqual(update[0], 200)
        updated = json.loads(update[2])["plan"]
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["status"], "ready_for_review")

        confirm = self._request(
            "POST",
            base + "/plan/commands/confirm",
            {
                "request_id": "request-planning-api-confirm",
                "plan_id": updated["plan_id"],
                "expected_revision": updated["revision"],
            },
            self.mutation_headers(),
        )
        self.assertEqual(confirm[0], 200)
        self.assertEqual(json.loads(confirm[2])["plan"]["status"], "confirmed")

    def test_rejects_unknown_fields_before_work(self) -> None:
        base = f"/api/projects/{self.project.project_id}"
        response = self._request(
            "POST",
            base + "/planning/commands/start",
            {
                "request_id": "request-planning-api-extra",
                "expected_revision": self.project.revision,
                "editing_skill_id": "clipen-story-vlog",
                "consent": True,
                "expected_context_hash": self.context.content_hash,
                "unexpected": True,
            },
            self.mutation_headers(),
        )
        self.assertEqual(response[0], 422)
        self.assertEqual(ApiProvider.calls, 0)


if __name__ == "__main__":
    unittest.main()
