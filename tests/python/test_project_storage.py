from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from clipen.projects import ProjectError, ProjectRepository
from clipen.projects.storage import command_digest


class ProjectStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.projects = self.root / "projects"
        self.repository = ProjectRepository(self.projects)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_create_is_durable_and_idempotent(self) -> None:
        created = self.repository.create_project("request-create-0001", "第一支作品")
        restarted = ProjectRepository(self.projects)

        loaded = restarted.get_project(created.project_id)
        replay = restarted.create_project("request-create-0001", "第一支作品")

        self.assertEqual(loaded, created)
        self.assertEqual(replay, created)
        self.assertEqual(restarted.list_projects(), (created,))
        self.assertEqual(
            (self.projects / created.project_id).stat().st_mode & 0o777,
            0o700,
        )
        self.assertEqual(
            (self.projects / created.project_id / "project.sqlite3").stat().st_mode
            & 0o777,
            0o600,
        )

    def test_reusing_create_request_for_other_content_is_rejected(self) -> None:
        self.repository.create_project("request-create-0002", "原作品")
        with self.assertRaises(ProjectError) as context:
            self.repository.create_project("request-create-0002", "另一個作品")
        self.assertEqual(context.exception.code, "request_reused")

    def test_update_details_enforces_revision_and_replays_exact_request(self) -> None:
        created = self.repository.create_project("request-create-0003", "未命名作品")
        updated = self.repository.update_details(
            created.project_id,
            "request-update-0001",
            created.revision,
            "咖啡教學",
            "剪成清楚的三十秒步驟。",
        )
        replay = self.repository.update_details(
            created.project_id,
            "request-update-0001",
            created.revision,
            "咖啡教學",
            "剪成清楚的三十秒步驟。",
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(replay, updated)
        with self.assertRaises(ProjectError) as context:
            self.repository.update_details(
                created.project_id,
                "request-update-0002",
                created.revision,
                "過期修改",
                "",
            )
        self.assertEqual(context.exception.code, "revision_conflict")

    def test_concurrent_updates_serialize_and_only_one_revision_wins(self) -> None:
        created = self.repository.create_project("request-create-concurrent", "並行測試")
        barrier = threading.Barrier(3)
        outcomes: list[tuple[str, object]] = []
        outcome_lock = threading.Lock()

        def update(request_id: str, name: str) -> None:
            barrier.wait()
            try:
                result = self.repository.update_details(
                    created.project_id,
                    request_id,
                    created.revision,
                    name,
                    "",
                )
                outcome = ("saved", result)
            except ProjectError as error:
                outcome = (error.code, error)
            with outcome_lock:
                outcomes.append(outcome)

        first = threading.Thread(
            target=update, args=("request-update-concurrent-a", "並行版本 A")
        )
        second = threading.Thread(
            target=update, args=("request-update-concurrent-b", "並行版本 B")
        )
        first.start()
        second.start()
        barrier.wait()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertCountEqual([status for status, _ in outcomes], ["saved", "revision_conflict"])
        final = self.repository.get_project(created.project_id)
        self.assertEqual(final.revision, 2)
        self.assertIn(final.name, {"並行版本 A", "並行版本 B"})

    def test_failed_mutation_rolls_back_data_and_receipt(self) -> None:
        created = self.repository.create_project("request-create-rollback", "回滾前")
        request_id = "request-update-rollback"
        request_hash = command_digest(
            {"expected_revision": created.revision, "name": "不應保存"}
        )

        def fail_after_write(connection, now) -> None:
            connection.execute(
                "UPDATE project SET name = ?, updated_at = ? WHERE project_id = ?",
                ("不應保存", now, created.project_id),
            )
            raise sqlite3.IntegrityError("synthetic failure after write")

        with self.assertRaises(ProjectError) as context:
            self.repository._mutate(
                created.project_id,
                request_id,
                "synthetic_failure",
                request_hash,
                created.revision,
                fail_after_write,
            )
        self.assertEqual(context.exception.code, "storage_failed")

        reloaded = ProjectRepository(self.projects)
        self.assertEqual(reloaded.get_project(created.project_id), created)
        self.assertIsNone(
            reloaded.replay_receipt(
                created.project_id,
                request_id,
                "synthetic_failure",
                request_hash,
            )
        )

    def test_projects_are_sorted_by_last_update(self) -> None:
        first = self.repository.create_project("request-create-0004", "第一個")
        second = self.repository.create_project("request-create-0005", "第二個")
        updated_first = self.repository.update_details(
            first.project_id,
            "request-update-0003",
            first.revision,
            "第一個，已更新",
            "",
        )
        listed = self.repository.list_projects()
        self.assertEqual([project.project_id for project in listed], [updated_first.project_id, second.project_id])

    def test_rejects_invalid_project_id_and_symlinked_project(self) -> None:
        with self.assertRaises(ProjectError):
            self.repository.get_project("../../raw-footage")

        created = self.repository.create_project("request-create-0006", "安全測試")
        project_dir = self.projects / created.project_id
        real_dir = self.root / "moved-project"
        project_dir.rename(real_dir)
        project_dir.symlink_to(real_dir, target_is_directory=True)
        with self.assertRaises(ProjectError) as context:
            self.repository.get_project(created.project_id)
        self.assertEqual(context.exception.code, "project_not_found")

    def test_rejects_symlinked_projects_root(self) -> None:
        real_root = self.root / "real-projects"
        real_root.mkdir()
        linked_root = self.root / "linked-projects"
        linked_root.symlink_to(real_root, target_is_directory=True)
        with self.assertRaises(ProjectError) as context:
            ProjectRepository(linked_root)
        self.assertEqual(context.exception.code, "storage_unsafe")

    def test_rejects_database_from_newer_schema(self) -> None:
        created = self.repository.create_project("request-create-0007", "未來版本")
        database = self.projects / created.project_id / "project.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA user_version = 999")
        connection.close()

        with self.assertRaises(ProjectError) as context:
            self.repository.get_project(created.project_id)
        self.assertEqual(context.exception.code, "migration_required")

    def test_v6_with_existing_observation_table_finishes_migration(self) -> None:
        created = self.repository.create_project(
            "request-create-partial-migration", "接續 migration"
        )
        database = self.projects / created.project_id / "project.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO evidence_runs "
                "(run_id, request_id, scope_id, inventory_run_id, project_revision, "
                "state, scope_json, total_count, progress_json, created_at) "
                "VALUES ('evd_test', 'evidence-request', 'scope_test', "
                "'inventory_test', 1, 'running', '{}', 1, '{}', '2026-09-19')"
            )
            connection.execute(
                "INSERT INTO evidence_observations "
                "(observation_id, run_id, item_key, request_id, state) "
                "VALUES ('obs_test', 'evd_test', 'item_test', "
                "'observation-request', 'running')"
            )
            connection.execute("PRAGMA user_version = 6")

        restarted = ProjectRepository(self.projects)
        self.assertEqual(restarted.get_project(created.project_id), created)
        with restarted.connect(created.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 7)
            observation = connection.execute(
                "SELECT state FROM evidence_observations "
                "WHERE observation_id = 'obs_test'"
            ).fetchone()
        self.assertEqual(observation["state"], "running")

    def test_validation_rejects_control_characters_and_oversized_brief(self) -> None:
        with self.assertRaises(ProjectError):
            self.repository.create_project("request-create-0008", "bad\nname")
        created = self.repository.create_project("request-create-0009", "正常作品")
        with self.assertRaises(ProjectError):
            self.repository.update_details(
                created.project_id,
                "request-update-0004",
                created.revision,
                "正常作品",
                "x" * 8001,
            )


if __name__ == "__main__":
    unittest.main()
