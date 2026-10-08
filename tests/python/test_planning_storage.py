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

    def plan(
        self,
        status: PlanStatus = PlanStatus.READY_FOR_REVIEW,
        plan_id: str = "plan-" + "d" * 32,
    ) -> PlanRevision:
        return PlanRevision.create(
            plan_id=plan_id,
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

    @staticmethod
    def editable(summary: str = "改成較精簡的旅程順序。") -> dict[str, object]:
        return {
            "schema_version": 1,
            "title": "東京旅行初剪",
            "summary": summary,
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
        }

    def publish_plan(self) -> PlanRevision:
        run, _ = self.create_run()
        self.planning.mark_running(self.project.project_id, run.run_id)
        self.planning.publish(self.project.project_id, run.run_id, self.plan(), None)
        return self.plan()

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

    def test_regeneration_supersedes_the_previous_editable_plan(self) -> None:
        first = self.publish_plan()
        second_run, _ = self.create_run("request-planning-regenerate")
        self.planning.mark_running(self.project.project_id, second_run.run_id)
        second = self.plan(plan_id="plan-" + "e" * 32)
        self.planning.publish(
            self.project.project_id, second_run.run_id, second, None
        )
        self.assertEqual(self.planning.current_plan(self.project.project_id), second)
        with self.projects.connect(self.project.project_id) as database:
            old_payload = database.execute(
                "SELECT payload_json FROM plan_revisions "
                "WHERE plan_id=? AND revision=?",
                (first.plan_id, first.revision),
            ).fetchone()[0]
        self.assertEqual(
            PlanRevision.from_payload(json.loads(old_payload)).status,
            PlanStatus.SUPERSEDED,
        )

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

    def test_cancel_is_immediate_for_queued_and_idempotent_for_terminal(self) -> None:
        run, _ = self.create_run()
        cancelled = self.planning.request_cancel(self.project.project_id, run.run_id)
        self.assertEqual(cancelled.state, PlanningRunState.CANCELLED)
        replay = self.planning.request_cancel(self.project.project_id, run.run_id)
        self.assertEqual(replay, cancelled)

    def test_edit_creates_revision_supersedes_old_and_requires_reconfirmation(self) -> None:
        original = self.publish_plan()
        confirmed, _ = self.planning.confirm_plan(
            self.project.project_id,
            "request-confirm-original-plan",
            original.plan_id,
            original.revision,
        )
        self.assertEqual(confirmed.status, PlanStatus.CONFIRMED)

        updated, created = self.planning.update_plan(
            self.project.project_id,
            "request-update-confirmed-plan",
            confirmed.plan_id,
            confirmed.revision,
            self.editable(),
        )
        self.assertTrue(created)
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.status, PlanStatus.READY_FOR_REVIEW)
        self.assertFalse(updated.can_execute)
        with self.projects.connect(self.project.project_id) as database:
            old = database.execute(
                "SELECT status, payload_json FROM plan_revisions "
                "WHERE plan_id=? AND revision=1",
                (original.plan_id,),
            ).fetchone()
        self.assertEqual(old["status"], PlanStatus.SUPERSEDED.value)
        self.assertEqual(
            PlanRevision.from_payload(json.loads(old["payload_json"])).status,
            PlanStatus.SUPERSEDED,
        )

        replay, replay_created = self.planning.update_plan(
            self.project.project_id,
            "request-update-confirmed-plan",
            confirmed.plan_id,
            confirmed.revision,
            self.editable(),
        )
        self.assertFalse(replay_created)
        self.assertEqual(replay, updated)
        self.assertEqual(
            self.planning.current_plan(self.project.project_id), updated
        )

    def test_confirm_is_idempotent_and_rejects_stale_revision(self) -> None:
        original = self.publish_plan()
        confirmed, changed = self.planning.confirm_plan(
            self.project.project_id,
            "request-confirm-plan",
            original.plan_id,
            original.revision,
        )
        self.assertTrue(changed)
        self.assertTrue(confirmed.can_execute)
        replay, replay_changed = self.planning.confirm_plan(
            self.project.project_id,
            "request-confirm-plan",
            original.plan_id,
            original.revision,
        )
        self.assertFalse(replay_changed)
        self.assertEqual(replay, confirmed)
        with self.assertRaises(ProjectError) as stale:
            self.planning.confirm_plan(
                self.project.project_id,
                "request-confirm-stale-plan",
                original.plan_id,
                original.revision + 1,
            )
        self.assertEqual(stale.exception.code, "plan_revision_conflict")

    def test_invalid_or_unchanged_edit_leaves_current_revision_intact(self) -> None:
        original = self.publish_plan()
        invalid = self.editable()
        invalid["sections"][0]["candidate_ids"] = ["candidate-unknown"]
        with self.assertRaises(ProjectError) as rejected:
            self.planning.update_plan(
                self.project.project_id,
                "request-update-invalid-plan",
                original.plan_id,
                original.revision,
                invalid,
            )
        self.assertEqual(rejected.exception.code, "plan_content_invalid")
        self.assertEqual(self.planning.current_plan(self.project.project_id), original)

        unchanged = self.editable(original.summary)
        with self.assertRaises(ProjectError) as no_change:
            self.planning.update_plan(
                self.project.project_id,
                "request-update-unchanged-plan",
                original.plan_id,
                original.revision,
                unchanged,
            )
        self.assertEqual(no_change.exception.code, "plan_unchanged")
        self.assertEqual(self.planning.current_plan(self.project.project_id), original)


if __name__ == "__main__":
    unittest.main()
