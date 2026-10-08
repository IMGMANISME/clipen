import json
from pathlib import Path
import tempfile
import unittest

from clipen.domain import (
    PlanRevision,
    PlanSection,
    PlanSourceRange,
    PlanStatus,
    Rational,
    SourceSpan,
)
from clipen.planning import (
    PlanningCandidate,
    PlanningContext,
    PlanningRepository,
    PlanningRunState,
    TranscriptReliability,
)
from clipen.projects import ProjectError, ProjectRepository
from clipen.providers import ProviderUsage


class PlanningRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.projects = ProjectRepository(Path(self.temporary.name) / "projects")
        self.project = self.projects.create_project(
            "request-planning-project", "東京 Vlog"
        )
        self.planning = PlanningRepository(self.projects)
        self.source = SourceSpan(
            "av_" + "a" * 32,
            0,
            "0",
            "900000",
            Rational(1, 90000),
        )
        self.context = PlanningContext(
            self.project.project_id,
            "剪成一支節奏自然的旅行紀錄。",
            "inv_" + "b" * 32,
            "analysis_" + "c" * 32,
            (
                PlanningCandidate(
                    "candidate-001",
                    self.source,
                    "毛片 001",
                    "video",
                    "車站外的移動畫面。",
                    TranscriptReliability.NEEDS_REVIEW,
                ),
            ),
            ("手持旅行紀錄。",),
            ("口語內容需要人工核對。",),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_run(self, request_id: str = "request-planning-run"):
        return self.planning.create_run(
            self.project.project_id,
            request_id,
            self.project.revision,
            "travel-natural",
            "skills-release-1",
            self.context,
        )

    def plan(self, status: PlanStatus = PlanStatus.READY_FOR_REVIEW) -> PlanRevision:
        return PlanRevision.create(
            plan_id="plan-" + "d" * 32,
            project_id=self.project.project_id,
            revision=1,
            material_snapshot_id=self.context.material_snapshot_id,
            analysis_snapshot_id=self.context.analysis_snapshot_id,
            system_skill_release="skills-release-1",
            title="東京旅行初剪",
            summary="依旅程順序保留現場感。",
            sections=(
                PlanSection(
                    "section-001",
                    "抵達",
                    "建立旅程起點。",
                    (PlanSourceRange("candidate-001", self.source),),
                    "保留環境聲。",
                    ("地點需確認",),
                ),
            ),
            status=status,
        )

    def test_create_is_idempotent_and_rejects_conflict_or_parallel_run(self) -> None:
        created, is_new = self.create_run()
        replay, replay_is_new = self.create_run()
        self.assertTrue(is_new)
        self.assertFalse(replay_is_new)
        self.assertEqual(replay.run_id, created.run_id)
        self.assertEqual(self.planning.context(self.project.project_id, created.run_id), self.context)

        with self.assertRaises(ProjectError) as conflict:
            self.planning.create_run(
                self.project.project_id,
                "request-planning-run",
                self.project.revision,
                "documentary",
                "skills-release-1",
                self.context,
            )
        self.assertEqual(conflict.exception.code, "request_conflict")

        with self.assertRaises(ProjectError) as busy:
            self.create_run("request-planning-parallel")
        self.assertEqual(busy.exception.code, "planning_busy")

    def test_context_hash_corruption_fails_closed_after_restart(self) -> None:
        run, _ = self.create_run()
        with self.projects.connect(self.project.project_id) as database:
            stored = json.loads(
                database.execute(
                    "SELECT context_json FROM planning_runs WHERE run_id=?",
                    (run.run_id,),
                ).fetchone()[0]
            )
            stored["brief"] = "遭到修改"
            database.execute(
                "UPDATE planning_runs SET context_json=? WHERE run_id=?",
                (json.dumps(stored, ensure_ascii=False), run.run_id),
            )

        restarted = PlanningRepository(self.projects)
        with self.assertRaises(ProjectError) as corrupt:
            restarted.context(self.project.project_id, run.run_id)
        self.assertEqual(corrupt.exception.code, "storage_corrupt")

    def test_publish_persists_plan_usage_and_current_pointer(self) -> None:
        run, _ = self.create_run()
        running = self.planning.mark_running(self.project.project_id, run.run_id)
        self.assertEqual(running.state, PlanningRunState.RUNNING)

        usage = ProviderUsage(800, 200, 120)
        completed = self.planning.publish(
            self.project.project_id, run.run_id, self.plan(), usage
        )
        self.assertEqual(completed.state, PlanningRunState.SUCCEEDED)
        self.assertEqual(completed.usage, usage)

        restarted = PlanningRepository(self.projects)
        self.assertEqual(restarted.current_plan(self.project.project_id), self.plan())
        self.assertEqual(
            restarted.latest_run(self.project.project_id).state,
            PlanningRunState.SUCCEEDED,
        )
        with self.assertRaises(ProjectError) as terminal:
            restarted.mark_terminal(
                self.project.project_id,
                run.run_id,
                PlanningRunState.FAILED,
                "late_failure",
            )
        self.assertEqual(terminal.exception.code, "planning_state_conflict")

    def test_publish_rejects_corrupt_or_mismatched_snapshots(self) -> None:
        run, _ = self.create_run()
        self.planning.mark_running(self.project.project_id, run.run_id)
        with self.projects.connect(self.project.project_id) as database:
            database.execute(
                "UPDATE planning_runs SET context_hash=? WHERE run_id=?",
                ("0" * 64, run.run_id),
            )
        with self.assertRaises(ProjectError) as corrupt:
            self.planning.publish(
                self.project.project_id, run.run_id, self.plan(), None
            )
        self.assertEqual(corrupt.exception.code, "storage_corrupt")

    def test_interrupt_only_changes_incomplete_runs(self) -> None:
        first, _ = self.create_run()
        self.planning.mark_running(self.project.project_id, first.run_id)
        self.assertEqual(self.planning.interrupt_incomplete(self.project.project_id), 1)
        self.assertEqual(
            self.planning.get_run(self.project.project_id, first.run_id).state,
            PlanningRunState.INTERRUPTED,
        )
        second, _ = self.create_run("request-planning-after-restart")
        self.planning.mark_terminal(
            self.project.project_id,
            second.run_id,
            PlanningRunState.CANCELLED,
            "user_cancelled",
        )
        self.assertEqual(self.planning.interrupt_incomplete(self.project.project_id), 0)


if __name__ == "__main__":
    unittest.main()
