import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

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
            (self.projects / created.project_id / "project.sqlite3").stat().st_mode & 0o777,
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

        first = threading.Thread(target=update, args=("request-update-concurrent-a", "並行版本 A"))
        second = threading.Thread(target=update, args=("request-update-concurrent-b", "並行版本 B"))
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
        request_hash = command_digest({"expected_revision": created.revision, "name": "不應保存"})

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

    def test_production_activity_sorts_home_without_changing_project_revision(self):
        first = self.repository.create_project("request-activity-first", "持續剪輯的作品")
        second = self.repository.create_project("request-activity-second", "較新建立的作品")
        with self.repository.connect(first.project_id) as db:
            db.execute(
                "INSERT INTO operation_events(event_id,project_id,operation_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    "activity-event",
                    first.project_id,
                    "activity-operation",
                    "production_prompt_saved",
                    "{}",
                    "2099-01-01T00:00:00+00:00",
                ),
            )
        listed = self.repository.list_projects()
        self.assertEqual([p.project_id for p in listed], [first.project_id, second.project_id])
        self.assertEqual(listed[0].to_public()["last_activity_at"], "2099-01-01T00:00:00+00:00")
        self.assertEqual((listed[0].revision, listed[0].updated_at), (first.revision, first.updated_at))
        self.assertEqual(self.repository.get_project(first.project_id), first)

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
        created = self.repository.create_project("request-create-partial-migration", "接續 migration")
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
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 19)
            observation = connection.execute(
                "SELECT state FROM evidence_observations WHERE observation_id = 'obs_test'"
            ).fetchone()
        self.assertEqual(observation["state"], "running")

    def test_v10_project_adds_annotation_schema_without_changing_project(self) -> None:
        created = self.repository.create_project("request-create-v10-migration", "舊作品")
        database = self.projects / created.project_id / "project.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TABLE annotation_command_receipts")
            connection.execute("DROP TABLE transcript_annotations")
            connection.execute("DROP TABLE material_annotations")
            connection.execute("PRAGMA user_version = 10")

        restarted = ProjectRepository(self.projects)
        self.assertEqual(restarted.get_project(created.project_id), created)
        with restarted.connect(created.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 19)
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%annotation%'"
                )
            }
        self.assertEqual(
            tables,
            {"material_annotations", "transcript_annotations", "annotation_command_receipts"},
        )

    def test_v11_project_adds_reversible_transcript_removal(self) -> None:
        created = self.repository.create_project("request-create-v11-migration", "既有作品")
        database = self.projects / created.project_id / "project.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("ALTER TABLE transcript_annotations DROP COLUMN discarded")
            connection.execute("PRAGMA user_version = 11")

        restarted = ProjectRepository(self.projects)
        self.assertEqual(restarted.get_project(created.project_id), created)
        with restarted.connect(created.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 19)
            columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(transcript_annotations)")}
        self.assertIn("discarded", columns)
        self.assertEqual(columns["discarded"]["dflt_value"], "0")

    # The version 9-18 schema of the retired confirmed-plan flow, as real projects still hold it.
    _RETIRED_PLANNING_SCHEMA = """
        CREATE TABLE planning_runs (run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES project(project_id),
            request_id TEXT NOT NULL UNIQUE, context_json TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE plan_revisions (plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
            project_id TEXT NOT NULL REFERENCES project(project_id), status TEXT NOT NULL, payload_json TEXT NOT NULL,
            source_run_id TEXT REFERENCES planning_runs(run_id), created_at TEXT NOT NULL, PRIMARY KEY(plan_id, revision));
        CREATE TABLE current_plan (slot INTEGER PRIMARY KEY CHECK (slot = 1), plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
            FOREIGN KEY(plan_id, revision) REFERENCES plan_revisions(plan_id, revision));
        CREATE TABLE plan_command_receipts (request_id TEXT PRIMARY KEY, command_type TEXT NOT NULL,
            request_hash TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX planning_runs_project_created ON planning_runs(project_id, created_at DESC);
        CREATE INDEX plan_revisions_project_created ON plan_revisions(project_id, created_at DESC);
    """

    def _retired_tables(self, connection) -> set[str]:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('planning_runs','plan_revisions','current_plan','plan_command_receipts',"
                "'planning_runs_project_created','plan_revisions_project_created')"
            )
        }

    def test_v18_project_archives_and_drops_retired_planning_tables(self) -> None:
        import json

        created = self.repository.create_project("request-create-v18", "外接硬碟上的作品")
        directory = self.projects / created.project_id
        with sqlite3.connect(directory / "project.sqlite3") as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(self._RETIRED_PLANNING_SCHEMA)
            connection.execute(
                "INSERT INTO planning_runs VALUES ('plan-run', ?, 'plan-request', '{\"brief\":\"東京\"}', 'succeeded', '2026-01-01')",
                (created.project_id,),
            )
            connection.execute(
                "INSERT INTO plan_revisions VALUES ('plan-one', 1, ?, 'confirmed', '{\"title\":\"東京第一天\"}', 'plan-run', '2026-01-01')",
                (created.project_id,),
            )
            connection.execute("INSERT INTO current_plan VALUES (1, 'plan-one', 1)")
            connection.execute("PRAGMA user_version = 18")

        restarted = ProjectRepository(self.projects)
        self.assertEqual(restarted.get_project(created.project_id), created)
        with restarted.connect(created.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 19)
            self.assertEqual(self._retired_tables(connection), set())
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        archive = directory / "retired-planning.json"
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        tables = json.loads(archive.read_text(encoding="utf-8"))["tables"]
        self.assertEqual(tables["plan_revisions"][0]["payload_json"], '{"title":"東京第一天"}')
        self.assertEqual(tables["planning_runs"][0]["context_json"], '{"brief":"東京"}')
        self.assertEqual(tables["current_plan"], [{"slot": 1, "plan_id": "plan-one", "revision": 1}])
        self.assertEqual(tables["plan_command_receipts"], [])
        self.assertFalse((directory / "retired-planning.json.tmp").exists())

    def test_v18_project_without_planning_rows_writes_no_archive(self) -> None:
        created = self.repository.create_project("request-create-v18-empty", "沒用過舊規劃的作品")
        directory = self.projects / created.project_id
        with sqlite3.connect(directory / "project.sqlite3") as connection:
            connection.executescript(self._RETIRED_PLANNING_SCHEMA)
            connection.execute("PRAGMA user_version = 18")
        with ProjectRepository(self.projects).connect(created.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 19)
            self.assertEqual(self._retired_tables(connection), set())
        self.assertFalse((directory / "retired-planning.json").exists())

    def test_new_project_has_no_planning_tables(self) -> None:
        created = self.repository.create_project("request-create-v19", "新作品")
        with self.repository.connect(created.project_id) as connection:
            self.assertEqual(self._retired_tables(connection), set())

    def test_v13_project_adds_resumable_edit_agent_schema(self) -> None:
        created = self.repository.create_project("request-create-v13", "舊剪輯作品")
        database = self.projects / created.project_id / "project.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TABLE edit_agent_command_receipts")
            connection.execute("DROP TABLE edit_agent_events")
            connection.execute("DROP TABLE edit_agent_revisions")
            connection.execute("DROP TABLE edit_agent_runs")
            connection.execute("PRAGMA user_version = 13")

        restarted = ProjectRepository(self.projects)
        self.assertEqual(restarted.get_project(created.project_id), created)
        with restarted.connect(created.project_id) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 19)
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'edit_agent_%'"
                )
            }
        self.assertEqual(
            tables,
            {
                "edit_agent_runs",
                "edit_agent_revisions",
                "edit_agent_events",
                "edit_agent_command_receipts",
            },
        )

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

    def test_delete_project_removes_only_managed_data_and_is_idempotent(self) -> None:
        created = self.repository.create_project("request-create-delete-1", "東京作品")
        source = self.root / "external-source"
        source.mkdir()
        media = source / "original.mov"
        media.write_bytes(b"raw-media")
        managed_artifact = self.projects / created.project_id / "artifacts" / "thumb.jpg"
        managed_artifact.parent.mkdir()
        managed_artifact.write_bytes(b"derived")

        deleted = self.repository.delete_project(
            created.project_id,
            "request-delete-project-1",
            created.revision,
            created.name,
        )
        replay = self.repository.delete_project(
            created.project_id,
            "request-delete-project-1",
            created.revision,
            created.name,
        )

        self.assertEqual(deleted, created.project_id)
        self.assertEqual(replay, created.project_id)
        self.assertFalse((self.projects / created.project_id).exists())
        self.assertEqual(media.read_bytes(), b"raw-media")

    def test_delete_project_requires_exact_name_and_no_active_work(self) -> None:
        created = self.repository.create_project("request-create-delete-2", "不能誤刪")
        with self.assertRaises(ProjectError) as mismatch:
            self.repository.delete_project(
                created.project_id,
                "request-delete-project-2",
                created.revision,
                "名稱錯誤",
            )
        self.assertEqual(mismatch.exception.code, "confirmation_mismatch")

        with self.repository.connect(created.project_id) as connection:
            connection.execute(
                "INSERT INTO inventory_runs "
                "(run_id, project_id, request_id, project_revision, input_fingerprint, "
                "roots_json, state, phase, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "inv_delete_busy",
                    created.project_id,
                    "request-delete-busy-run",
                    created.revision,
                    "fingerprint",
                    "[]",
                    "running",
                    "probing",
                    "2026-10-13T00:00:00+00:00",
                ),
            )
        with self.assertRaises(ProjectError) as busy:
            self.repository.delete_project(
                created.project_id,
                "request-delete-project-3",
                created.revision,
                created.name,
            )
        self.assertEqual(busy.exception.code, "project_busy")
        self.assertTrue((self.projects / created.project_id).is_dir())

    def test_delete_project_rejects_symlink_and_preserves_external_file(self) -> None:
        created = self.repository.create_project("request-create-delete-3", "安全刪除")
        external = self.root / "external-original.mov"
        external.write_bytes(b"original")
        link = self.projects / created.project_id / "unsafe-link"
        link.symlink_to(external)

        with self.assertRaises(ProjectError) as unsafe:
            self.repository.delete_project(
                created.project_id,
                "request-delete-project-4",
                created.revision,
                created.name,
            )

        self.assertEqual(unsafe.exception.code, "storage_unsafe")
        self.assertEqual(external.read_bytes(), b"original")
        self.assertTrue((self.projects / created.project_id).is_dir())


if __name__ == "__main__":
    unittest.main()
