import sqlite3
from pathlib import Path
import tempfile
import unittest

from clipen.domain import (
    ValidationIssue,
    ValidationReport,
    ValidationSeverity,
)
from clipen.projects import (
    ProjectError,
    ProjectRepository,
    TimelineValidationRepository,
)


class TimelineValidationWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "projects"
        self.projects = ProjectRepository(self.root)
        self.project = self.projects.create_project(
            "request-create-validation", "剪輯驗證"
        )
        self.workflow = TimelineValidationRepository(self.projects)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def report(
        report_id: str,
        *issues: ValidationIssue,
        timeline_id: str = "timeline_primary",
    ) -> ValidationReport:
        return ValidationReport(
            report_id,
            timeline_id,
            "capability_local_resolve",
            "a" * 64,
            issues,
        )

    @staticmethod
    def warning() -> ValidationIssue:
        return ValidationIssue(
            "effect.preview_only",
            ValidationSeverity.WARNING,
            "/tracks/0/items/0",
            "這個效果在輸出前仍需確認。",
            "先播放預覽並確認畫面。",
            "clip_title",
        )

    @staticmethod
    def blocking() -> ValidationIssue:
        return ValidationIssue(
            "media.missing",
            ValidationSeverity.BLOCKING,
            "/tracks/0/items/0",
            "找不到這個素材。",
            "重新連接硬碟或替換素材。",
            "clip_missing",
        )

    def test_new_project_is_honestly_not_ready(self) -> None:
        state = self.workflow.get_state(self.project.project_id)
        self.assertEqual(state.state, "not_ready")
        self.assertFalse(state.can_execute)
        self.assertIsNone(state.report)

    def test_pass_report_is_persisted_and_survives_restart(self) -> None:
        state = self.workflow.publish(
            self.project.project_id, self.report("report_pass")
        )
        self.assertEqual(state.state, "ready")
        self.assertTrue(state.can_execute)

        restarted = TimelineValidationRepository(ProjectRepository(self.root))
        recovered = restarted.get_state(self.project.project_id)
        self.assertEqual(recovered.report["report_id"], "report_pass")
        self.assertTrue(recovered.can_execute)

    def test_warning_requires_exact_explicit_acceptance_and_is_idempotent(self) -> None:
        report = self.report("report_warning", self.warning())
        initial = self.workflow.publish(self.project.project_id, report)
        self.assertEqual(initial.state, "needs_confirmation")
        self.assertFalse(initial.can_execute)

        accepted = self.workflow.accept_current_warnings(
            self.project.project_id,
            "request-accept-warning-001",
            report.report_id,
        )
        replay = self.workflow.accept_current_warnings(
            self.project.project_id,
            "request-accept-warning-001",
            report.report_id,
        )
        self.assertEqual(accepted, replay)
        self.assertEqual(accepted.state, "ready")
        self.assertTrue(accepted.warnings_accepted)
        self.assertTrue(accepted.can_execute)

    def test_new_report_does_not_inherit_old_acceptance(self) -> None:
        first = self.report("report_warning_old", self.warning())
        self.workflow.publish(self.project.project_id, first)
        self.workflow.accept_current_warnings(
            self.project.project_id,
            "request-accept-warning-old",
            first.report_id,
        )

        second = self.report(
            "report_warning_new", self.warning(), timeline_id="timeline_revision_2"
        )
        state = self.workflow.publish(self.project.project_id, second)
        self.assertEqual(state.state, "needs_confirmation")
        self.assertFalse(state.warnings_accepted)
        with self.assertRaises(ProjectError) as context:
            self.workflow.accept_current_warnings(
                self.project.project_id,
                "request-accept-warning-stale",
                first.report_id,
            )
        self.assertEqual(context.exception.code, "validation_report_stale")
        with self.assertRaises(ProjectError) as replay_context:
            self.workflow.accept_current_warnings(
                self.project.project_id,
                "request-accept-warning-old",
                first.report_id,
            )
        self.assertEqual(replay_context.exception.code, "validation_report_stale")

    def test_blocking_report_cannot_be_overridden(self) -> None:
        report = self.report("report_blocking", self.blocking())
        state = self.workflow.publish(self.project.project_id, report)
        self.assertEqual(state.state, "blocked")
        self.assertFalse(state.can_execute)
        with self.assertRaises(ProjectError) as context:
            self.workflow.accept_current_warnings(
                self.project.project_id,
                "request-accept-blocking-001",
                report.report_id,
            )
        self.assertEqual(context.exception.code, "validation_blocked")

    def test_report_id_is_immutable(self) -> None:
        self.workflow.publish(
            self.project.project_id, self.report("report_immutable")
        )
        with self.assertRaises(ProjectError) as context:
            self.workflow.publish(
                self.project.project_id,
                self.report("report_immutable", self.warning()),
            )
        self.assertEqual(context.exception.code, "validation_report_conflict")

    def test_corrupt_report_payload_fails_closed(self) -> None:
        report = self.report("report_corrupt", self.warning())
        self.workflow.publish(self.project.project_id, report)
        database = self.root / self.project.project_id / "project.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE timeline_validation_reports SET report_json = '{}' "
                "WHERE report_id = ?",
                (report.report_id,),
            )
        with self.assertRaises(ProjectError) as context:
            self.workflow.get_state(self.project.project_id)
        self.assertEqual(context.exception.code, "storage_corrupt")

    def test_v7_project_migrates_without_losing_existing_data(self) -> None:
        database = self.root / self.project.project_id / "project.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TABLE timeline_warning_acceptances")
            connection.execute("DROP TABLE current_timeline_validation")
            connection.execute("DROP TABLE timeline_validation_reports")
            connection.execute("PRAGMA user_version = 7")

        restarted = ProjectRepository(self.root)
        self.assertEqual(
            restarted.get_project(self.project.project_id).name, "剪輯驗證"
        )
        with restarted.connect(self.project.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 12)
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("timeline_validation_reports", tables)


if __name__ == "__main__":
    unittest.main()
