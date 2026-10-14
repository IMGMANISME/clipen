import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from clipen.domain import (
    CapabilityProfile,
    DeliveryMode,
    ElementCapability,
    PlanRevision,
    PlanSection,
    PlanSourceRange,
    PlanStatus,
    Rational,
    SourceSpan,
    TrackKind,
    VerifiedDelivery,
)
from clipen.controller import (
    ControllerConfig,
    LocalSessionAuthority,
    ProviderState,
    ProviderStatus,
    create_app,
)
from clipen.editing import (
    OutputSettings,
    TimelineCompiler,
    TimelineRepository,
    TimelineService,
)
from clipen.planning import (
    PlanningCandidate,
    PlanningContext,
    PlanningRepository,
    TranscriptReliability,
)
from clipen.projects import ProjectError, ProjectRepository, TimelineValidationRepository
from test_project_api import request_app


class EditingTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.projects = ProjectRepository(Path(self.temporary.name) / "projects")
        self.project = self.projects.create_project("request-project", "東京 Vlog")
        self.planning = PlanningRepository(self.projects)
        self.video = SourceSpan("av_" + "a" * 32, 0, "0", "900000", Rational(1, 90000))
        self.audio = SourceSpan("av_" + "b" * 32, 1, "0", "480000", Rational(1, 48000))
        self.context = PlanningContext(
            self.project.project_id,
            "剪成自然的旅行紀錄。",
            "inv_" + "c" * 32,
            "analysis_" + "d" * 32,
            (
                PlanningCandidate("candidate-video", self.video, "畫面", "video", "街景", TranscriptReliability.UNAVAILABLE),
                PlanningCandidate("candidate-audio", self.audio, "現場聲", "audio", "環境聲", TranscriptReliability.UNAVAILABLE),
            ),
        )
        self.ready_plan = PlanRevision.create(
            plan_id="plan-" + "e" * 32,
            project_id=self.project.project_id,
            revision=1,
            material_snapshot_id=self.context.material_snapshot_id,
            analysis_snapshot_id=self.context.analysis_snapshot_id,
            system_skill_release="skills-release-1",
            title="東京旅行初剪",
            summary="依事件順序整理。",
            sections=(
                PlanSection(
                    "section-001",
                    "抵達",
                    "建立旅程起點。",
                    (
                        PlanSourceRange("candidate-video", self.video),
                        PlanSourceRange("candidate-audio", self.audio),
                    ),
                    "保留現場感。",
                    (),
                ),
            ),
            status=PlanStatus.READY_FOR_REVIEW,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def confirmed_plan(self) -> PlanRevision:
        run, _ = self.planning.create_run(
            self.project.project_id,
            "request-plan-run",
            self.project.revision,
            "travel-natural",
            "skills-release-1",
            self.context,
        )
        self.planning.mark_running(self.project.project_id, run.run_id)
        self.planning.publish(self.project.project_id, run.run_id, self.ready_plan, None)
        confirmed, _ = self.planning.confirm_plan(
            self.project.project_id,
            "request-confirm-plan",
            self.ready_plan.plan_id,
            self.ready_plan.revision,
        )
        return confirmed

    @staticmethod
    def verified_profile() -> CapabilityProfile:
        deliveries = tuple(
            VerifiedDelivery(mode, "editing-test", f"report-{mode.value}")
            for mode in DeliveryMode
        )
        return CapabilityProfile(
            "editing-test-profile",
            1,
            (
                ElementCapability(TrackKind.VIDEO, deliveries),
                ElementCapability(TrackKind.AUDIO, deliveries),
            ),
        )

    def test_compiler_creates_exact_sequential_tracks_and_markers(self) -> None:
        plan = self.ready_plan.transition(PlanStatus.CONFIRMED)
        timeline = TimelineCompiler().compile(
            plan, self.context, OutputSettings("landscape_1080p", 30)
        )
        self.assertEqual((timeline.width, timeline.height), (1920, 1080))
        self.assertEqual(timeline.duration_frames, 300)
        self.assertEqual([track.kind for track in timeline.tracks], [TrackKind.VIDEO, TrackKind.AUDIO])
        self.assertEqual(timeline.tracks[0].items[0].duration_frames, 300)
        self.assertEqual(timeline.tracks[1].items[0].duration_frames, 300)
        self.assertEqual(timeline.markers[0].label, "抵達")

    def test_repository_is_durable_and_request_idempotent(self) -> None:
        plan = self.ready_plan.transition(PlanStatus.CONFIRMED)
        settings = OutputSettings("portrait_1080p", 24)
        timeline = TimelineCompiler().compile(plan, self.context, settings)
        repository = TimelineRepository(self.projects)
        saved, created = repository.save(
            self.project.project_id,
            "request-build",
            plan.plan_id,
            plan.revision,
            settings,
            timeline,
        )
        replay, replay_created = repository.save(
            self.project.project_id,
            "request-build",
            plan.plan_id,
            plan.revision,
            settings,
            timeline,
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay, saved)
        self.assertEqual(TimelineRepository(self.projects).current(self.project.project_id), saved)

        with self.assertRaises(ProjectError) as conflict:
            repository.save(
                self.project.project_id,
                "request-build",
                plan.plan_id,
                plan.revision,
                OutputSettings("portrait_1080p", 30),
                TimelineCompiler().compile(plan, self.context, OutputSettings("portrait_1080p", 30)),
            )
        self.assertEqual(conflict.exception.code, "request_conflict")

    def test_service_requires_current_confirmed_plan_and_publishes_validation(self) -> None:
        plan = self.confirmed_plan()
        service = TimelineService(
            TimelineRepository(self.projects),
            self.planning,
            TimelineValidationRepository(self.projects),
            self.verified_profile(),
        )
        result = service.build(
            self.project.project_id,
            "request-service-build",
            plan.plan_id,
            plan.revision,
            "landscape_1080p",
            30,
        )
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["can_execute"])
        self.assertEqual(service.status(self.project.project_id), result)

        with self.assertRaises(ProjectError) as stale:
            service.build(
                self.project.project_id,
                "request-stale-build",
                plan.plan_id,
                plan.revision + 1,
                "landscape_1080p",
                30,
            )
        self.assertEqual(stale.exception.code, "plan_revision_conflict")

        self.planning.update_plan(
            self.project.project_id,
            "request-update-after-timeline",
            plan.plan_id,
            plan.revision,
            {
                "schema_version": 1,
                "title": plan.title,
                "summary": "改成更精簡的旅行順序。",
                "sections": [
                    {
                        "section_id": "section-001",
                        "title": "抵達",
                        "purpose": "建立旅程起點。",
                        "candidate_ids": ["candidate-video", "candidate-audio"],
                        "edit_notes": "保留現場感。",
                        "unknowns": [],
                    }
                ],
            },
        )
        self.assertEqual(service.status(self.project.project_id)["state"], "stale")

    def test_authenticated_api_builds_and_reads_timeline(self) -> None:
        plan = self.confirmed_plan()
        service = TimelineService(
            TimelineRepository(self.projects),
            self.planning,
            TimelineValidationRepository(self.projects),
            self.verified_profile(),
        )
        static = Path(self.temporary.name) / "dist"
        static.mkdir()
        (static / "index.html").write_text("Clipen", encoding="utf-8")
        authority = LocalSessionAuthority()
        app = create_app(
            ControllerConfig(static),
            ProviderStatus("Codex", ProviderState.READY, "AI 已連接"),
            authority,
            timeline_service=service,
        )

        def request(method, path, payload=None, headers=None):
            body = b"" if payload is None else json.dumps(payload).encode("utf-8")
            return asyncio.run(request_app(app, method, path, headers=headers, body=body))

        _, connect_headers, _ = request(
            "POST",
            "/api/session/connect",
            headers={"origin": "http://localhost:4200", "sec-fetch-site": "same-origin"},
        )
        cookie = connect_headers["set-cookie"].split(";", 1)[0]
        _, _, status_body = request("GET", "/api/status", headers={"cookie": cookie})
        csrf = json.loads(status_body)["session"]["csrf_token"]
        base = f"/api/projects/{self.project.project_id}/timeline"

        self.assertEqual(request("GET", base)[0], 401)
        initial = request("GET", base, headers={"cookie": cookie})
        self.assertEqual(json.loads(initial[2])["state"], "not_ready")
        built = request(
            "POST",
            base + "/commands/build",
            {
                "request_id": "request-api-build",
                "plan_id": plan.plan_id,
                "plan_revision": plan.revision,
                "output_preset": "landscape_1080p",
                "frame_rate": 30,
            },
            {
                "origin": "http://localhost:4200",
                "sec-fetch-site": "same-origin",
                "content-type": "application/json",
                "cookie": cookie,
                "x-clipen-csrf": csrf,
            },
        )
        self.assertEqual(built[0], 201)
        payload = json.loads(built[2])
        self.assertEqual(payload["state"], "ready")
        self.assertEqual(payload["timeline"]["duration_frames"], 300)


if __name__ == "__main__":
    unittest.main()
