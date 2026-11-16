import json
import tempfile
import unittest
from pathlib import Path

from clipen.analysis import PreparationRepository
from clipen.annotations import AnnotationRepository
from clipen.projects import ProjectError, ProjectRepository


class AnnotationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.projects = ProjectRepository(Path(self.temporary.name) / "projects")
        self.project = self.projects.create_project("request-annotation-project", "東京")
        self.annotations = AnnotationRepository(self.projects)
        self.version_id = "av_" + "a" * 32
        self.revision_id = "tr_" + "b" * 32
        self._seed_transcript()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seed_transcript(self) -> None:
        project_id = self.project.project_id
        inventory_id = "inv_" + "c" * 32
        run_id = "prep_" + "d" * 32
        self.run_id = run_id
        asset_id = "asset_" + "e" * 32
        with self.projects.connect(project_id) as database:
            database.execute(
                "INSERT INTO inventory_runs "
                "(run_id,project_id,request_id,project_revision,input_fingerprint,roots_json,state,phase,created_at) "
                "VALUES(?,?,?,?,?,'[]','succeeded','completed','2026-10-14T00:00:00Z')",
                (inventory_id, project_id, "request-annotation-inventory", 1, "fingerprint"),
            )
            database.execute(
                "INSERT INTO assets VALUES(?,?,?,'clip.mov','2026-10-14T00:00:00Z')",
                (asset_id, project_id, "source"),
            )
            database.execute(
                "INSERT INTO asset_versions VALUES(?,?,1,1,?,'ready',?,'2026-10-14T00:00:00Z')",
                (
                    self.version_id,
                    asset_id,
                    "f" * 64,
                    json.dumps(
                        {
                            "capture_time": "2026-07-20T09:30:00+09:00",
                            "location": "GPS 35.676200, 139.650300",
                        }
                    ),
                ),
            )
            second_asset = "asset_" + "9" * 32
            second_version = "av_" + "8" * 32
            database.execute(
                "INSERT INTO assets VALUES(?,?,?,'clip-2.mov','2026-10-14T00:00:00Z')",
                (second_asset, project_id, "source"),
            )
            database.execute(
                "INSERT INTO asset_versions VALUES(?,?,1,1,?,'ready',?,'2026-10-14T00:00:00Z')",
                (
                    second_version,
                    second_asset,
                    "9" * 64,
                    json.dumps(
                        {
                            "capture_time": "2026-07-21T14:15:00+09:00",
                        }
                    ),
                ),
            )
            database.execute(
                "INSERT INTO inventory_items VALUES(?,?,?,?,?,?,NULL)",
                (inventory_id, asset_id, self.version_id, "clip.mov", "video", "usable"),
            )
            database.execute(
                "INSERT INTO inventory_items VALUES(?,?,?,?,?,?,NULL)",
                (inventory_id, second_asset, second_version, "clip-2.mov", "video", "usable"),
            )
            database.execute("INSERT INTO current_inventory VALUES(1,?)", (inventory_id,))
            database.execute(
                "INSERT INTO preparation_runs "
                "(run_id,project_id,request_id,project_revision,inventory_run_id,scope_id,operation_id,attempt_id,"
                "input_fingerprint,recipe_json,state,phase,total_count,transcribed_count,created_at,started_at,completed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'{}','succeeded','completed',1,1,?,?,?)",
                (
                    run_id,
                    project_id,
                    "request-annotation-preparation",
                    1,
                    inventory_id,
                    "scope_" + "1" * 32,
                    "op_" + "2" * 32,
                    "att_" + "3" * 32,
                    "4" * 64,
                    "2026-10-14T00:01:00Z",
                    "2026-10-14T00:01:00Z",
                    "2026-10-14T00:03:00Z",
                ),
            )
            source = {
                "version_id": self.version_id,
                "stream_index": 1,
                "start_pts": "0",
                "end_pts": "48000",
                "time_base": {"num": 1, "den": 48000},
            }
            database.execute(
                "INSERT INTO transcription_revisions "
                "(revision_id,project_id,run_id,version_id,stream_index,status,language,model_json,recipe_id,source_span_json,created_at) "
                "VALUES(?,?,?,?,1,'needs_review','zh','{}','recipe',?,'2026-10-14T00:02:00Z')",
                (self.revision_id, project_id, run_id, self.version_id, json.dumps(source)),
            )
            database.execute(
                "INSERT INTO transcript_segments "
                "(revision_id,segment_id,start_pts,end_pts,text,confidence,ordinal) "
                "VALUES(?,'seg-1','0','48000','泥模','0.2',0)",
                (self.revision_id,),
            )
            database.execute(
                "INSERT INTO preparation_items "
                "(run_id,version_id,stream_index,status,transcript_revision_id,review_required) "
                "VALUES(?,?,1,'transcribed',?,1)",
                (run_id, self.version_id, self.revision_id),
            )
            database.execute("INSERT INTO current_preparation VALUES(1,?)", (run_id,))

    def _payload(self):
        return {
            "people": ["陳均葦"],
            "location": "東京迪士尼海洋",
            "trip_day": 2,
            "day_part": "afternoon",
            "note": "第一次玩尼莫，保留反應。",
            "usage": "must_use",
        }

    def test_suggests_trip_order_day_part_and_embedded_location(self) -> None:
        first = self.annotations.get(self.project.project_id, self.version_id, 1)
        second = self.annotations.get(self.project.project_id, "av_" + "8" * 32, -1)
        self.assertEqual(first["suggestion"]["trip_day"], 1)
        self.assertEqual(first["suggestion"]["day_part"], "morning")
        self.assertEqual(first["suggestion"]["location"], "GPS 35.676200, 139.650300")
        self.assertEqual(second["suggestion"]["trip_day"], 2)
        self.assertEqual(second["suggestion"]["day_part"], "afternoon")

    def test_saves_material_and_confirmed_transcript_without_changing_source(self) -> None:
        saved = self.annotations.save(
            self.project.project_id,
            "request-annotation-save",
            self.version_id,
            1,
            0,
            0,
            self._payload(),
            {
                "source_revision_id": self.revision_id,
                "confirmed": True,
                "discarded": False,
                "corrections": [{"ordinal": 0, "text": "尼莫"}],
            },
        )
        self.assertEqual(saved["material"]["revision"], 1)
        self.assertEqual(saved["transcript"]["corrections"][0]["text"], "尼莫")
        self.assertTrue(saved["transcript"]["confirmed"])
        with self.projects.connect(self.project.project_id) as database:
            original = database.execute(
                "SELECT text FROM transcript_segments WHERE revision_id=?", (self.revision_id,)
            ).fetchone()["text"]
        self.assertEqual(original, "泥模")

    def test_saves_individually_removed_transcript_segment(self) -> None:
        saved = self.annotations.save(
            self.project.project_id,
            "request-annotation-remove-segment",
            self.version_id,
            1,
            0,
            0,
            self._payload(),
            {
                "source_revision_id": self.revision_id,
                "confirmed": False,
                "discarded": False,
                "corrections": [{"ordinal": 0, "text": ""}],
            },
        )
        self.assertEqual(saved["transcript"]["corrections"], [{"ordinal": 0, "text": ""}])
        with self.projects.connect(self.project.project_id) as database:
            original = database.execute(
                "SELECT text FROM transcript_segments WHERE revision_id=?", (self.revision_id,)
            ).fetchone()["text"]
        self.assertEqual(original, "泥模")

    def test_removes_transcript_from_ai_context_without_deleting_source(self) -> None:
        removed = self.annotations.save(
            self.project.project_id,
            "request-annotation-discard-transcript",
            self.version_id,
            1,
            0,
            0,
            self._payload(),
            {
                "source_revision_id": self.revision_id,
                "confirmed": True,
                "discarded": True,
                "corrections": [],
            },
        )
        self.assertTrue(removed["transcript"]["discarded"])
        self.assertFalse(removed["transcript"]["confirmed"])

        results = PreparationRepository(self.projects).result_items(self.project.project_id, self.run_id)
        self.assertTrue(results["items"][0]["transcript_discarded"])
        self.assertIsNone(results["items"][0]["preview_text"])
        with self.projects.connect(self.project.project_id) as database:
            source_text = database.execute(
                "SELECT text FROM transcript_segments WHERE revision_id=?", (self.revision_id,)
            ).fetchone()["text"]
        self.assertEqual(source_text, "泥模")

        replacement_revision = "tr_" + "7" * 32
        with self.projects.connect(self.project.project_id) as database:
            source = {
                "version_id": self.version_id,
                "stream_index": 1,
                "start_pts": "0",
                "end_pts": "48000",
                "time_base": {"num": 1, "den": 48000},
            }
            database.execute(
                "INSERT INTO transcription_revisions "
                "(revision_id,project_id,run_id,version_id,stream_index,status,language,model_json,recipe_id,source_span_json,created_at) "
                "VALUES(?,?,?,?,1,'transcribed','zh','{}','recipe',?,'2026-10-14T00:04:00Z')",
                (
                    replacement_revision,
                    self.project.project_id,
                    self.run_id,
                    self.version_id,
                    json.dumps(source),
                ),
            )
            database.execute(
                "INSERT INTO transcript_segments "
                "(revision_id,segment_id,start_pts,end_pts,text,confidence,ordinal) "
                "VALUES(?,'seg-2','0','48000','新的背景人聲','0.9',0)",
                (replacement_revision,),
            )
            database.execute(
                "UPDATE preparation_items SET transcript_revision_id=? "
                "WHERE run_id=? AND version_id=? AND stream_index=1",
                (replacement_revision, self.run_id, self.version_id),
            )
        after_reanalysis = self.annotations.get(self.project.project_id, self.version_id, 1)
        self.assertTrue(after_reanalysis["transcript"]["discarded"])
        self.assertTrue(after_reanalysis["transcript"]["stale"])

        restored = self.annotations.save(
            self.project.project_id,
            "request-annotation-restore-transcript",
            self.version_id,
            1,
            removed["material"]["revision"],
            removed["transcript"]["revision"],
            self._payload(),
            {
                "source_revision_id": replacement_revision,
                "confirmed": False,
                "discarded": False,
                "corrections": [],
            },
        )
        self.assertFalse(restored["transcript"]["discarded"])

    def test_analysis_results_expose_user_state_and_filter_by_effective_trip_day(self) -> None:
        analyses = PreparationRepository(self.projects)
        initial = analyses.result_items(self.project.project_id, self.run_id)
        self.assertEqual(initial["items"][0]["user_state"], "pending")
        self.assertEqual(initial["items"][0]["trip_day"], 1)
        self.assertEqual(initial["filters"]["trip_days"], [{"day": 1, "count": 1}])

        self.annotations.save(
            self.project.project_id,
            "request-annotation-filter",
            self.version_id,
            1,
            0,
            0,
            self._payload(),
            {
                "source_revision_id": self.revision_id,
                "confirmed": True,
                "discarded": False,
                "corrections": [],
            },
        )
        updated = analyses.result_items(self.project.project_id, self.run_id, trip_day=2, review_state="reviewed")
        self.assertEqual(updated["total"], 1)
        self.assertEqual(updated["items"][0]["user_state"], "priority")
        self.assertEqual(updated["items"][0]["trip_day"], 2)
        self.assertEqual(
            analyses.result_items(self.project.project_id, self.run_id, trip_day=1)["total"],
            0,
        )
        self.assertEqual(
            analyses.result_items(self.project.project_id, self.run_id, review_state="pending")["total"],
            0,
        )

    def test_exact_request_replays_and_stale_revision_is_rejected(self) -> None:
        arguments = (
            self.project.project_id,
            "request-annotation-replay",
            self.version_id,
            1,
            0,
            0,
            self._payload(),
            {"source_revision_id": self.revision_id, "confirmed": False, "discarded": False, "corrections": []},
        )
        first = self.annotations.save(*arguments)
        self.assertEqual(self.annotations.save(*arguments), first)
        with self.assertRaises(ProjectError) as conflict:
            self.annotations.save(
                self.project.project_id,
                "request-annotation-conflict",
                self.version_id,
                1,
                0,
                0,
                self._payload(),
                {"source_revision_id": self.revision_id, "confirmed": False, "discarded": False, "corrections": []},
            )
        self.assertEqual(conflict.exception.code, "annotation_revision_conflict")

    def test_rejects_unknown_segment_and_invalid_metadata(self) -> None:
        with self.assertRaises(ProjectError) as segment:
            self.annotations.save(
                self.project.project_id,
                "request-annotation-bad-segment",
                self.version_id,
                1,
                0,
                0,
                self._payload(),
                {
                    "source_revision_id": self.revision_id,
                    "confirmed": True,
                    "discarded": False,
                    "corrections": [{"ordinal": 99, "text": "不存在"}],
                },
            )
        self.assertEqual(segment.exception.code, "annotation_transcript_changed")
        invalid = self._payload()
        invalid["trip_day"] = 0
        with self.assertRaises(ProjectError) as metadata:
            self.annotations.save(
                self.project.project_id,
                "request-annotation-bad-metadata",
                self.version_id,
                1,
                0,
                0,
                invalid,
                None,
            )
        self.assertEqual(metadata.exception.code, "annotation_invalid")


if __name__ == "__main__":
    unittest.main()
