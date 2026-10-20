import asyncio
import json
from pathlib import Path
from unittest.mock import patch
import tempfile
import time
import unittest

from clipen.domain import (
    ClipSource,
    EditTimeline,
    MediaClip,
    PlanRevision,
    PlanSection,
    PlanSourceRange,
    PlanStatus,
    Rational,
    SourceSpan,
    Track,
    TrackKind,
)
from clipen.controller import (
    ControllerConfig,
    LocalSessionAuthority,
    ProviderState,
    ProviderStatus,
    create_app,
)
from clipen.editing import (
    EditAgentRepository,
    EditAgentCoordinator,
    EditAgentTools,
    OutputSettings,
    TimelineRepository,
)
from clipen.planning import (
    PlanningCandidate,
    PlanningContext,
    TranscriptReliability,
)
from clipen.projects import ProjectError, ProjectRepository
from clipen.providers import ProviderCancelled
from test_project_api import request_app


class StaticPlanningInput:
    def __init__(self, plan, context):
        self.plan = plan
        self.context = context

    def execution_input(self, project_id):
        if project_id != self.context.project_id:
            raise AssertionError("wrong project")
        return self.plan, self.context


class EmptyArtifacts:
    def get(self, project_id, artifact_id):
        raise ProjectError("missing", "missing", status_code=404)

    def path(self, record):
        raise AssertionError("unreachable")


class SuccessfulAgent:
    def __init__(self, projects_root, attempt_root, repository):
        self.repository = repository

    def execute(self, project_id, run_id, *, cancel):
        self.repository.activate(project_id, run_id)
        self.repository.finish(project_id, run_id, "fake provider finished")
        self.repository.record_provider_outcome(
            project_id,
            run_id,
            "agent-fake-success",
            usage={
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "output_tokens": 30,
            },
        )


class BlockingAgent:
    def __init__(self, projects_root, attempt_root, repository):
        self.repository = repository

    def execute(self, project_id, run_id, *, cancel):
        self.repository.activate(project_id, run_id)
        cancel.wait(2)
        raise ProviderCancelled("controller stopped")


class EditAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.projects = ProjectRepository(Path(self.temporary.name) / "projects")
        self.project = self.projects.create_project("request-project", "AI 剪輯")
        self.span = SourceSpan(
            "av_" + "a" * 32,
            0,
            "0",
            "900000",
            Rational(1, 90000),
        )
        self.context = PlanningContext(
            self.project.project_id,
            "剪成旅行影片",
            "inv_" + "e" * 32,
            "analysis_" + "f" * 32,
            (
                PlanningCandidate(
                    "candidate-street",
                    self.span,
                    "毛片 001",
                    "video",
                    "早上的東京街景",
                    TranscriptReliability.UNAVAILABLE,
                ),
            ),
        )
        self.plan = PlanRevision.create(
            plan_id="plan-" + "c" * 32,
            project_id=self.project.project_id,
            revision=1,
            material_snapshot_id=self.context.material_snapshot_id,
            analysis_snapshot_id=self.context.analysis_snapshot_id,
            system_skill_release="system-skill-release-1",
            title="東京第一天",
            summary="依照旅行順序完成已確認的剪輯。",
            sections=(
                PlanSection(
                    "section-001",
                    "抵達東京",
                    "以早晨街景建立旅程開場。",
                    (PlanSourceRange("candidate-street", self.span),),
                    "加入章節字卡，並以緩慢推進強調抵達感。",
                ),
            ),
            status=PlanStatus.CONFIRMED,
        )
        self.timeline = EditTimeline(
            "timeline-" + "b" * 32,
            self.project.project_id,
            self.plan.plan_id,
            self.plan.revision,
            self.plan.content_hash,
            Rational(30, 1),
            1920,
            1080,
            300,
            (
                Track(
                    "video-main",
                    TrackKind.VIDEO,
                    (MediaClip("clip-0001", 0, 300, ClipSource(self.span)),),
                ),
            ),
        )
        TimelineRepository(self.projects).save(
            self.project.project_id,
            "request-timeline",
            self.timeline.plan_id,
            self.timeline.plan_revision,
            OutputSettings("landscape_1080p", 30),
            self.timeline,
        )
        self.repository = EditAgentRepository(self.projects)

    def tearDown(self):
        self.temporary.cleanup()

    def create_tools(self, *, calls=20):
        run, created = self.repository.create_run(
            self.project.project_id,
            "request-edit-run",
            max_tool_calls=calls,
            max_preview_renders=2,
        )
        self.assertTrue(created)
        tools = EditAgentTools(
            self.repository,
            self.project.project_id,
            run.run_id,
            planning=StaticPlanningInput(self.plan, self.context),
            artifacts=EmptyArtifacts(),
        )
        tools.activate()
        return run, tools

    def test_run_is_idempotent_and_starts_with_immutable_base_revision(self):
        run, created = self.repository.create_run(
            self.project.project_id,
            "request-edit-run",
            max_tool_calls=12,
            max_preview_renders=2,
        )
        replay, replay_created = self.repository.create_run(
            self.project.project_id,
            "request-edit-run",
            max_tool_calls=12,
            max_preview_renders=2,
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay, run)
        revision = self.repository.current_revision(self.project.project_id, run.run_id)
        self.assertEqual(revision.ordinal, 0)
        self.assertEqual(revision.timeline, self.timeline)
        with self.projects.connect(self.project.project_id) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 17)

    def test_tools_create_revisions_undo_and_finish_without_paths(self):
        run, tools = self.create_tools()

        status = tools.call("get_edit_status", {})
        direction = status.payload["confirmed_direction"]
        self.assertEqual(direction["authority"], "user_confirmed")
        self.assertEqual(direction["title"], "東京第一天")
        self.assertIn("章節字卡", direction["sections"][0]["edit_notes"])
        self.assertEqual(
            direction["proof_constraints"]["target_duration_seconds"],
            {"minimum": 45, "maximum": 60},
        )

        searched = tools.call("search_media", {"query": "東京", "limit": 10})
        self.assertEqual(searched.payload["returned"], 1)
        self.assertNotIn(str(self.projects.root), json.dumps(searched.payload))

        replaced = tools.call(
            "replace_sequence",
            {
                "clips": [
                    {
                        "candidate_id": "candidate-street",
                        "start_offset_seconds": offset,
                        "duration_seconds": 4,
                    }
                    for offset in (0, 0.5, 1, 1.5, 2, 2.5, 3, 4)
                ],
                "summary": "以精確入出點建立短樣片",
            },
        )
        self.assertEqual(replaced.payload["revision"]["ordinal"], 1)
        sequence = self.repository.current_revision(
            self.project.project_id, run.run_id
        ).timeline
        self.assertEqual(sequence.duration_frames, 960)
        self.assertTrue(all(item.duration_frames == 120 for item in sequence.tracks[0].items))
        titled = tools.call(
            "add_title",
            {
                "start_frame": 0,
                "duration_frames": 60,
                "text": "東京第一天",
                "style_id": "title.chapter",
                "summary": "加入章節字卡",
            },
        )
        self.assertEqual(titled.payload["revision"]["ordinal"], 2)
        effected = tools.call(
            "set_clip_effect",
            {
                "item_id": "agent-video-0001",
                "effect_id": "video.slow_push",
                "parameters": {"scale_from": 1.0, "scale_to": 1.08},
                "summary": "加入緩慢推進",
            },
        )
        self.assertEqual(effected.payload["revision"]["ordinal"], 3)
        undone = tools.call("undo_revision", {})
        self.assertEqual(undone.payload["revision"]["ordinal"], 2)
        restored = tools.call(
            "set_clip_effect",
            {
                "item_id": "agent-video-0001",
                "effect_id": "video.slow_push",
                "parameters": {},
                "summary": "使用安全預設加入緩慢推進",
            },
        )
        self.assertEqual(restored.payload["revision"]["ordinal"], 3)
        restored_timeline = self.repository.current_revision(
            self.project.project_id, run.run_id
        ).timeline
        self.assertEqual(
            restored_timeline.tracks[0].items[0].effects[0].parameters,
            {"scale_from": 1.0, "scale_to": 1.08},
        )
        with patch('clipen.editing.production_preview.finish_review_status',return_value={'reviewed_revision':True}):
            final = tools.call("finish_edit", {"summary": "完成第一版旅行初剪"})
        self.assertEqual(final.payload["state"], "succeeded")
        self.assertEqual(
            final.payload["final_revision_id"],
            restored.payload["revision"]["revision_id"],
        )
        self.assertEqual(
            self.repository.get(self.project.project_id, run.run_id).tool_calls,
            8,
        )

        recorded = self.repository.record_provider_outcome(
            self.project.project_id,
            run.run_id,
            "agent-attempt-0001",
            usage={
                "input_tokens": 1200,
                "cached_input_tokens": 200,
                "output_tokens": 300,
            },
        )
        self.assertEqual(recorded.usage["input_tokens"], 1200)
        self.assertEqual(recorded.to_public()["usage"]["total_tokens"], 1500)

    def test_style_proof_rejects_whole_candidates_and_long_total_runtime(self):
        _, tools = self.create_tools()
        whole_candidates = {
            "clips": [
                {
                    "candidate_id": "candidate-street",
                    "start_offset_seconds": 0,
                    "duration_seconds": 10,
                }
                for _ in range(8)
            ],
            "summary": "invalid proof",
        }
        with self.assertRaises(ProjectError) as duplicate:
            tools.call("replace_sequence", whole_candidates)
        self.assertEqual(duplicate.exception.code, "agent_input_invalid")

        too_short = {
            "clips": [
                {
                    "candidate_id": "candidate-street",
                    "start_offset_seconds": offset,
                    "duration_seconds": 1,
                }
                for offset in range(8)
            ],
            "summary": "too short",
        }
        with self.assertRaises(ProjectError) as runtime:
            tools.call("replace_sequence", too_short)
        self.assertEqual(runtime.exception.code, "agent_input_invalid")
        with self.assertRaises(ProjectError) as unfinished:
            tools.call("finish_edit", {"summary": "base timeline is not a proof"})
        self.assertEqual(unfinished.exception.code, "style_proof_invalid")

    def test_candidate_inspection_uses_the_explicit_run_budget(self):
        _, tools = self.create_tools(calls=20)
        for _ in range(20):
            inspected = tools.call(
                "inspect_clip", {"candidate_id": "candidate-street"}
            )
            self.assertEqual(inspected.payload["image_count"], 0)
        with self.assertRaises(ProjectError) as exhausted:
            tools.call("inspect_clip", {"candidate_id": "candidate-street"})
        self.assertEqual(exhausted.exception.code, "tool_budget_exhausted")

    def test_tool_budget_interrupts_run(self):
        run, tools = self.create_tools(calls=1)
        tools.call("get_edit_status", {})
        with self.assertRaises(ProjectError) as context:
            tools.call("inspect_sequence", {})
        self.assertEqual(context.exception.code, "tool_budget_exhausted")
        self.assertEqual(
            self.repository.get(self.project.project_id, run.run_id).state,
            "interrupted",
        )

    def test_rejects_unknown_tool_and_unknown_arguments(self):
        _, tools = self.create_tools()
        with self.assertRaises(ProjectError) as unknown:
            tools.call("run_shell", {"command": "cat /etc/passwd"})
        self.assertEqual(unknown.exception.code, "tool_not_allowed")
        with self.assertRaises(ProjectError) as fields:
            tools.call("search_media", {"path": "/Volumes/private"})
        self.assertEqual(fields.exception.code, "agent_input_invalid")
        with self.assertRaises(ProjectError) as effect:
            tools.call(
                "set_clip_effect",
                {
                    "item_id": "clip-0001",
                    "effect_id": "video.punch_in",
                    "parameters": {"scale": 99},
                    "summary": "invalid scale",
                },
            )
        self.assertEqual(effect.exception.code, "agent_input_invalid")

    def test_coordinator_runs_one_shot_provider_and_reports_usage(self):
        coordinator = EditAgentCoordinator(
            self.repository,
            Path(self.temporary.name),
            adapter_factory=SuccessfulAgent,
        )
        try:
            run = coordinator.start(
                self.project.project_id,
                "request-agent-coordinator",
                True,
                max_tool_calls=10,
                max_preview_renders=1,
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                latest = coordinator.status(self.project.project_id)["latest_run"]
                if latest and latest["state"] == "succeeded":
                    break
                time.sleep(0.01)
            else:
                self.fail("agent coordinator did not finish")
        finally:
            coordinator.close()

        self.assertEqual(latest["run_id"], run.run_id)
        self.assertEqual(latest["usage"]["input_tokens"], 100)
        self.assertIsNotNone(latest["final_revision_id"])

    def test_authenticated_controller_starts_and_reads_edit_agent(self):
        static = Path(self.temporary.name) / "dist"
        static.mkdir()
        (static / "index.html").write_text("Clipen", encoding="utf-8")
        coordinator = EditAgentCoordinator(
            self.repository,
            Path(self.temporary.name),
            adapter_factory=SuccessfulAgent,
        )
        authority = LocalSessionAuthority()
        app = create_app(
            ControllerConfig(static),
            ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
            authority,
            edit_agents=coordinator,
        )

        def request(method, path, payload=None, headers=None):
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            return asyncio.run(request_app(app, method, path, headers=headers, body=body))

        try:
            _, connected_headers, _ = request(
                "POST",
                "/api/session/connect",
                headers={
                    "origin": "http://localhost:4200",
                    "sec-fetch-site": "same-origin",
                },
            )
            cookie = connected_headers["set-cookie"].split(";", 1)[0]
            _, _, status_body = request("GET", "/api/status", headers={"cookie": cookie})
            csrf = json.loads(status_body)["session"]["csrf_token"]
            base = f"/api/projects/{self.project.project_id}/edit-agent"
            mutation_headers = {
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "cookie": cookie,
                "x-clipen-csrf": csrf,
            }

            denied = request(
                "POST",
                base + "/commands/start",
                {"request_id": "request-api-agent-denied", "consent": False},
                mutation_headers,
            )
            self.assertEqual(denied[0], 422)
            started = request(
                "POST",
                base + "/commands/start",
                {"request_id": "request-api-agent-start", "consent": True},
                mutation_headers,
            )
            self.assertEqual(started[0], 202)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                response = request("GET", base, headers={"cookie": cookie})
                status = json.loads(response[2])
                latest = status["latest_run"]
                if latest and latest["state"] == "succeeded":
                    break
                time.sleep(0.01)
            else:
                self.fail("controller edit agent did not finish")
        finally:
            coordinator.close()

        self.assertEqual(latest["usage"]["output_tokens"], 30)
        self.assertEqual(latest["budget"]["max_tool_calls"], 96)
        self.assertEqual(status["result"]["revision_id"], latest["final_revision_id"])
        self.assertEqual(status["result"]["timeline"]["timeline_id"], self.timeline.timeline_id)

    def test_controller_shutdown_marks_active_agent_interrupted(self):
        coordinator = EditAgentCoordinator(
            self.repository,
            Path(self.temporary.name),
            adapter_factory=BlockingAgent,
        )
        run = coordinator.start(
            self.project.project_id,
            "request-agent-shutdown",
            True,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.repository.get(self.project.project_id, run.run_id).state == "running":
                break
            time.sleep(0.01)
        coordinator.close()

        stopped = self.repository.get(self.project.project_id, run.run_id)
        self.assertEqual(stopped.state, "interrupted")
        self.assertEqual(stopped.error_code, "controller_stopped")

    def test_recovery_seals_successful_run_missing_provider_usage(self):
        run, _ = self.repository.create_run(
            self.project.project_id,
            "request-agent-finished-before-usage",
        )
        self.repository.activate(self.project.project_id, run.run_id)
        self.repository.finish(self.project.project_id, run.run_id, "已完成剪輯")
        coordinator = EditAgentCoordinator(
            self.repository,
            Path(self.temporary.name),
            adapter_factory=SuccessfulAgent,
        )

        self.assertEqual(coordinator.recover(), 1)

        recovered = self.repository.get(self.project.project_id, run.run_id)
        self.assertEqual(recovered.state, "succeeded")
        self.assertIsNone(recovered.usage)
        self.assertEqual(
            recovered.provider_attempt_id,
            "recovered-" + run.run_id.removeprefix("editrun_"),
        )


if __name__ == "__main__":
    unittest.main()
