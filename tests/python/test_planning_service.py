from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from clipen.domain import Rational, SourceSpan
from clipen.planning import (
    PlanningCandidate,
    PlanningContext,
    PlanningRepository,
    PlanningRunState,
    PlanningService,
    TranscriptReliability,
)
from clipen.projects import ProjectError, ProjectRepository
from clipen.providers import ProviderCancelled, ProviderTimedOut, ProviderUsage
from clipen.system_skills import SystemSkillVerificationError


class FakeContextBuilder:
    def __init__(self, context: PlanningContext) -> None:
        self.context = context

    def build(self, project_id: str) -> PlanningContext:
        if self.context.project_id != project_id:
            raise AssertionError("unexpected project")
        return self.context


class FakeSkills:
    release_id = "skills-release-1"
    summaries = (SimpleNamespace(skill_id="clipen-story-vlog"),)

    def attempt_snapshot(self, editing_skill_id: str) -> tuple[str, str]:
        if editing_skill_id != "clipen-story-vlog":
            raise KeyError(editing_skill_id)
        return (
            "Internal core instructions that must remain hidden from every user output.",
            "Internal Vlog instructions that must remain hidden from every user output.",
        )


class FakeProvider:
    calls = []

    def __init__(self, root: Path) -> None:
        self.root = root

    def probe(self):
        return SimpleNamespace(available=True)

    def execute(self, attempt, cancel):
        self.calls.append(attempt)
        return SimpleNamespace(
            usage=ProviderUsage(600, 100, 80),
            payload={
                "schema_version": 1,
                "title": "東京旅行初剪",
                "summary": "依事件順序保留旅行現場感。",
                "sections": [
                    {
                        "section_id": "section-001",
                        "title": "抵達",
                        "purpose": "建立旅程起點。",
                        "candidate_ids": ["candidate-001"],
                        "edit_notes": "保留環境聲。",
                        "unknowns": ["地點需確認"],
                    }
                ],
            },
        )


class PlanningServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = ProjectRepository(self.root / "projects")
        self.project = self.projects.create_project(
            "request-planning-service-project", "東京 Vlog"
        )
        source = SourceSpan(
            "av_" + "a" * 32,
            0,
            "0",
            "900000",
            Rational(1, 90000),
        )
        self.context = PlanningContext(
            self.project.project_id,
            "剪成一支自然的旅行紀錄。",
            "inv_" + "b" * 32,
            "analysis_" + "c" * 32,
            (
                PlanningCandidate(
                    "candidate-001",
                    source,
                    "毛片 001",
                    "video",
                    "車站外的移動畫面。",
                    TranscriptReliability.NEEDS_REVIEW,
                ),
            ),
            limitations=("口語內容需要人工核對。",),
        )
        self.attempts = self.root / "attempts"
        self.attempts.mkdir()
        FakeProvider.calls = []
        self.repository = PlanningRepository(self.projects)
        self.service = PlanningService(
            self.repository,
            self.attempts,
            Path("schemas/v1/plan-proposal.schema.json"),
            FakeSkills(),
            adapter_factory=FakeProvider,
        )
        self.service.context_builder = FakeContextBuilder(self.context)

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def start(self, request_id: str = "request-planning-service-run"):
        return self.service.start(
            self.project.project_id,
            request_id,
            self.project.revision,
            "clipen-story-vlog",
            True,
            self.context.content_hash,
        )

    def wait(self, run_id: str):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = self.repository.get_run(self.project.project_id, run_id)
            if run.state not in {PlanningRunState.QUEUED, PlanningRunState.RUNNING}:
                return run
            time.sleep(0.01)
        self.fail("planning service did not settle")

    def test_success_is_bounded_private_and_durable(self) -> None:
        preflight = self.service.preflight(self.project.project_id)
        self.assertEqual(preflight["candidate_count"], 1)
        self.assertGreater(preflight["estimated_input_tokens"], 0)
        run = self.start()
        self.assertGreater(run.estimated_input_tokens, self.context.estimated_input_tokens)
        completed = self.wait(run.run_id)
        self.assertEqual(completed.state, PlanningRunState.SUCCEEDED)
        self.assertEqual(completed.usage, ProviderUsage(600, 100, 80))
        self.assertEqual(len(FakeProvider.calls), 1)
        prompt = FakeProvider.calls[0].prompt
        self.assertIn("Internal core instructions", prompt)
        self.assertNotIn("av_", prompt)
        self.assertNotIn(str(self.root), prompt)

        status = self.service.preflight(self.project.project_id)
        public_plan = status["current_plan"]
        self.assertEqual(len(status["recent_runs"]), 1)
        self.assertEqual(status["recent_runs"][0]["run_id"], run.run_id)
        self.assertEqual(
            status["recent_runs"][0]["usage"],
            {"input_tokens": 600, "cached_input_tokens": 100, "output_tokens": 80},
        )
        self.assertEqual(public_plan["sections"][0]["candidate_ids"], ["candidate-001"])
        self.assertNotIn("source", repr(public_plan))
        self.assertNotIn("av_", repr(public_plan))

        replay = self.start()
        self.assertEqual(replay.run_id, run.run_id)
        self.assertEqual(len(FakeProvider.calls), 1)

    def test_requires_explicit_consent_and_current_preflight(self) -> None:
        with self.assertRaises(ProjectError) as consent:
            self.service.start(
                self.project.project_id,
                "request-planning-no-consent",
                self.project.revision,
                "clipen-story-vlog",
                False,
                self.context.content_hash,
            )
        self.assertEqual(consent.exception.code, "planning_consent_required")
        with self.assertRaises(ProjectError) as changed:
            self.service.start(
                self.project.project_id,
                "request-planning-stale-context",
                self.project.revision,
                "clipen-story-vlog",
                True,
                "0" * 64,
            )
        self.assertEqual(changed.exception.code, "planning_context_changed")
        self.assertEqual(FakeProvider.calls, [])

    def test_cancel_stops_running_provider(self) -> None:
        entered = threading.Event()

        class BlockingProvider(FakeProvider):
            def execute(adapter, attempt, cancel):
                entered.set()
                cancel.wait(3)
                raise ProviderCancelled("cancelled")

        self.service.adapter_factory = BlockingProvider
        run = self.start()
        self.assertTrue(entered.wait(2))
        cancelling = self.service.cancel(self.project.project_id, run.run_id)
        self.assertEqual(cancelling.phase, "cancelling")
        self.assertEqual(self.wait(run.run_id).state, PlanningRunState.CANCELLED)

    def test_provider_failure_has_stable_error_and_no_plan(self) -> None:
        class TimeoutProvider(FakeProvider):
            def execute(adapter, attempt, cancel):
                raise ProviderTimedOut("timeout")

        self.service.adapter_factory = TimeoutProvider
        run = self.start()
        failed = self.wait(run.run_id)
        self.assertEqual(failed.state, PlanningRunState.FAILED)
        self.assertEqual(failed.error_code, "provider_timed_out")
        self.assertIsNone(self.repository.current_plan(self.project.project_id))

    def test_skill_reverification_failure_never_calls_provider(self) -> None:
        class ChangedSkills(FakeSkills):
            def attempt_snapshot(self, editing_skill_id):
                raise SystemSkillVerificationError("changed")

        self.service.system_skills = ChangedSkills()
        with self.assertRaises(ProjectError) as changed:
            self.start()
        self.assertEqual(changed.exception.code, "system_skill_changed")
        self.assertIsNone(self.repository.latest_run(self.project.project_id))
        self.assertEqual(FakeProvider.calls, [])

    def test_recovery_interrupts_without_replaying_provider(self) -> None:
        run, _ = self.repository.create_run(
            self.project.project_id,
            "request-planning-recovery",
            self.project.revision,
            "clipen-story-vlog",
            FakeSkills.release_id,
            self.context,
        )
        self.assertEqual(self.service.recover(), 1)
        recovered = self.repository.get_run(self.project.project_id, run.run_id)
        self.assertEqual(recovered.state, PlanningRunState.INTERRUPTED)
        self.assertEqual(FakeProvider.calls, [])

    def test_internal_skill_copy_in_output_is_rejected(self) -> None:
        class LeakingProvider(FakeProvider):
            def execute(adapter, attempt, cancel):
                response = super().execute(attempt, cancel)
                response.payload["summary"] = (
                    "Internal core instructions that must remain hidden from every user output."
                )
                return response

        self.service.adapter_factory = LeakingProvider
        run = self.start()
        failed = self.wait(run.run_id)
        self.assertEqual(failed.error_code, "provider_protocol")
        self.assertIsNone(self.repository.current_plan(self.project.project_id))

    def test_schema_valid_shape_is_rechecked_before_persistence(self) -> None:
        class ExtraFieldProvider(FakeProvider):
            def execute(adapter, attempt, cancel):
                response = super().execute(attempt, cancel)
                response.payload["unexpected"] = "must not persist"
                return response

        self.service.adapter_factory = ExtraFieldProvider
        run = self.start()
        failed = self.wait(run.run_id)
        self.assertEqual(failed.error_code, "provider_protocol")
        self.assertIsNone(self.repository.current_plan(self.project.project_id))


if __name__ == "__main__":
    unittest.main()
