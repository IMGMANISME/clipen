import json
from pathlib import Path
import tempfile
import unittest

from clipen.domain import Rational, SourceSpan
from clipen.planning import PlanningCandidate, PlanningContextBuilder, TranscriptReliability
from clipen.projects import ProjectError, ProjectRepository


class PlanningContextBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.projects = ProjectRepository(Path(self.temporary.name) / "projects")
        project = self.projects.create_project("request-plan-context-create", "東京")
        self.project = self.projects.update_details(
            project.project_id,
            "request-plan-context-brief",
            project.revision,
            project.name,
            "依旅程順序剪成自然的敘事 Vlog。",
        )
        self.builder = PlanningContextBuilder(self.projects)
        self.inventory_id = "inv_" + "1" * 32
        self.evidence_id = "ev_" + "2" * 32
        self._seed_inventory_and_evidence()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def encoded(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def span(version: str, stream: int = 0) -> SourceSpan:
        return SourceSpan(version, stream, "0", "2700000", Rational(1, 90000))

    def _seed_inventory_and_evidence(self) -> None:
        with self.projects.connect(self.project.project_id) as database:
            database.execute(
                "INSERT INTO inventory_runs "
                "(run_id, project_id, request_id, project_revision, input_fingerprint, "
                "roots_json, state, phase, created_at) "
                "VALUES (?, ?, 'request-plan-context-inventory', ?, 'fingerprint', '[]', "
                "'succeeded', 'completed', '2026-10-08T00:00:00Z')",
                (self.inventory_id, self.project.project_id, self.project.revision),
            )
            database.execute(
                "INSERT INTO current_inventory(slot, run_id) VALUES(1, ?)",
                (self.inventory_id,),
            )
            database.execute(
                "INSERT INTO evidence_runs "
                "(run_id, request_id, scope_id, inventory_run_id, project_revision, "
                "state, scope_json, total_count, progress_json, created_at) "
                "VALUES (?, 'request-plan-context-evidence', 'scope-evidence', ?, ?, "
                "'succeeded', '{}', 2, '{}', '2026-10-08T00:01:00Z')",
                (self.evidence_id, self.inventory_id, self.project.revision),
            )
            source_version = "av_" + "a" * 32
            reference_version = "av_" + "b" * 32
            source = {
                "key": f"{source_version}:0",
                "display_name": "private-source-name.MP4",
                "kind": "video",
                "role": "source",
                "source": self.span(source_version).to_storage(),
                "error_code": None,
            }
            reference = {
                "key": f"{reference_version}:0",
                "display_name": "private-reference-name.MP4",
                "kind": "video",
                "role": "reference",
                "source": self.span(reference_version).to_storage(),
                "error_code": None,
            }
            for index, (version_id, role) in enumerate(
                ((source_version, "source"), (reference_version, "reference"))
            ):
                asset_id = "asset_" + str(index + 1) * 32
                database.execute(
                    "INSERT INTO assets VALUES(?,?,?,?,?)",
                    (asset_id, self.project.project_id, role, f"clip-{index}.mov", "2026-10-08T00:00:00Z"),
                )
                database.execute(
                    "INSERT INTO asset_versions VALUES(?,?,1,1,?,'ready','{}','2026-10-08T00:00:00Z')",
                    (version_id, asset_id, str(index + 1) * 64),
                )
                database.execute(
                    "INSERT INTO inventory_items VALUES(?,?,?,?,?,?,NULL)",
                    (
                        self.inventory_id, asset_id, version_id, f"clip-{index}.mov",
                        "video", "usable",
                    ),
                )
            for ordinal, result in enumerate((source, reference)):
                database.execute(
                    "INSERT INTO evidence_results "
                    "(run_id, ordinal, item_key, result_json, has_issue) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (
                        self.evidence_id,
                        ordinal,
                        result["key"],
                        self.encoded(result),
                    ),
                )
            observation = {
                "observations": [
                    {"index": 0, "description": "車站外的移動畫面", "uncertainty": "地點未知"}
                ],
                "style_summary": "手持旅行紀錄，節奏自然。",
                "frame_evidence": [{"pts": "900000", "artifact_id": "art_" + "c" * 32}],
            }
            for index, item_key in enumerate((source["key"], reference["key"])):
                database.execute(
                    "INSERT INTO evidence_observations "
                    "(observation_id, run_id, item_key, request_id, state, result_json) "
                    "VALUES (?, ?, ?, ?, 'succeeded', ?)",
                    (
                        "obs_" + str(index + 1) * 32,
                        self.evidence_id,
                        item_key,
                        f"request-plan-context-observe-{index}",
                        self.encoded(observation),
                    ),
                )

    def test_builds_path_free_source_candidates_and_reference_notes(self) -> None:
        context = self.builder.build(self.project.project_id)
        self.assertEqual(context.material_snapshot_id, self.inventory_id)
        self.assertEqual(len(context.candidates), 1)
        candidate = context.candidates[0]
        self.assertEqual(candidate.display_name, "毛片 001")
        self.assertEqual(candidate.transcript_reliability, TranscriptReliability.UNAVAILABLE)
        self.assertIn("車站外", candidate.evidence_summary)
        self.assertEqual(context.reference_notes, ("手持旅行紀錄，節奏自然。",))
        provider = repr(context.to_provider())
        self.assertNotIn("private-source-name", provider)
        self.assertNotIn("private-reference-name", provider)
        self.assertNotIn("av_", provider)

    def test_requires_current_evidence_and_nonempty_brief(self) -> None:
        with self.projects.connect(self.project.project_id) as database:
            database.execute("UPDATE evidence_runs SET state = 'failed'")
        with self.assertRaises(ProjectError) as context:
            self.builder.build(self.project.project_id)
        self.assertEqual(context.exception.code, "planning_evidence_required")

        with self.projects.connect(self.project.project_id) as database:
            database.execute("UPDATE project SET brief = ''")
        with self.assertRaises(ProjectError) as context:
            self.builder.build(self.project.project_id)
        self.assertEqual(context.exception.code, "planning_brief_required")

    def test_user_metadata_enters_context_and_excluded_material_is_removed(self) -> None:
        source_version = "av_" + "a" * 32
        with self.projects.connect(self.project.project_id) as database:
            database.execute(
                "INSERT INTO material_annotations "
                "(version_id,revision,people_json,location,trip_day,day_part,note,usage,updated_at) "
                "VALUES(?,1,?,'東京車站',1,'morning','保留第一次抵達的反應','must_use','2026-10-08T00:03:00Z')",
                (source_version, self.encoded(["小明"])),
            )
        context = self.builder.build(self.project.project_id)
        summary = context.candidates[0].evidence_summary
        self.assertIn("旅行第 1 天", summary)
        self.assertIn("東京車站", summary)
        self.assertIn("人物：小明", summary)
        self.assertIn("優先使用", summary)
        self.assertIn("保留第一次抵達", summary)

        with self.projects.connect(self.project.project_id) as database:
            database.execute(
                "UPDATE material_annotations SET revision=2,usage='exclude' WHERE version_id=?",
                (source_version,),
            )
        with self.assertRaises(ProjectError) as context:
            self.builder.build(self.project.project_id)
        self.assertEqual(context.exception.code, "planning_candidates_empty")

    def test_confirmed_correction_replaces_untrusted_text_for_the_same_material(self) -> None:
        version_id = "av_" + "a" * 32
        run_id = "prep_" + "3" * 32
        revision_id = "tr_" + "4" * 32
        with self.projects.connect(self.project.project_id) as database:
            database.execute(
                "INSERT INTO preparation_runs "
                "(run_id,project_id,request_id,project_revision,inventory_run_id,scope_id,operation_id,attempt_id,"
                "input_fingerprint,recipe_json,state,phase,total_count,transcribed_count,created_at) "
                "VALUES(?,?,?,?,?,'scope-test','op-test','att-test','fingerprint','{}','succeeded','completed',1,1,'2026-10-08T00:04:00Z')",
                (run_id, self.project.project_id, "request-plan-context-preparation", self.project.revision, self.inventory_id),
            )
            source_span = SourceSpan(version_id, 1, "0", "48000", Rational(1, 48000))
            database.execute(
                "INSERT INTO transcription_revisions "
                "(revision_id,project_id,run_id,version_id,stream_index,status,language,model_json,recipe_id,source_span_json,created_at) "
                "VALUES(?,?,?,?,1,'needs_review','zh','{}','recipe',?,'2026-10-08T00:05:00Z')",
                (revision_id, self.project.project_id, run_id, version_id, self.encoded(source_span.to_storage())),
            )
            database.execute(
                "INSERT INTO transcript_segments "
                "(revision_id,segment_id,start_pts,end_pts,text,confidence,ordinal) "
                "VALUES(?,'seg-1','0','48000','泥模','0.2',0)",
                (revision_id,),
            )
            database.execute(
                "INSERT INTO preparation_items "
                "(run_id,version_id,stream_index,status,transcript_revision_id,review_required) "
                "VALUES(?,?,1,'transcribed',?,1)",
                (run_id, version_id, revision_id),
            )
            database.execute("INSERT INTO current_preparation VALUES(1,?)", (run_id,))
            database.execute(
                "INSERT INTO transcript_annotations "
                "(version_id,stream_index,source_revision_id,revision,confirmed,discarded,corrections_json,updated_at) "
                "VALUES(?,?,?,1,1,0,?,'2026-10-08T00:06:00Z')",
                (version_id, 1, revision_id, self.encoded([{"ordinal": 0, "text": "尼莫"}])),
            )
        context = self.builder.build(self.project.project_id)
        corrected = [item for item in context.candidates if item.transcript_excerpt == "尼莫"]
        self.assertEqual(len(corrected), 1)
        self.assertEqual(corrected[0].transcript_reliability, TranscriptReliability.TRUSTED)
        self.assertEqual(corrected[0].source.stream_index, 0)
        self.assertEqual(corrected[0].source.time_base, Rational(1, 90000))
        self.assertEqual(corrected[0].source.duration_pts, 90000)
        self.assertNotIn("泥模", repr(context.to_provider()))

        with self.projects.connect(self.project.project_id) as database:
            database.execute(
                "UPDATE transcript_annotations SET corrections_json=?,revision=2 "
                "WHERE version_id=? AND stream_index=1",
                (self.encoded([{"ordinal": 0, "text": ""}]), version_id),
            )
        without_segment = self.builder.build(self.project.project_id)
        self.assertFalse(
            any(candidate.transcript_excerpt for candidate in without_segment.candidates)
        )

        with self.projects.connect(self.project.project_id) as database:
            database.execute(
                "UPDATE transcript_annotations SET discarded=1,confirmed=0,revision=3 "
                "WHERE version_id=? AND stream_index=1",
                (version_id,),
            )
        without_transcript = self.builder.build(self.project.project_id)
        self.assertFalse(
            any(candidate.transcript_excerpt for candidate in without_transcript.candidates)
        )
        self.assertNotIn("尼莫", repr(without_transcript.to_provider()))

    def test_capture_metadata_guides_plan_when_user_has_not_annotated(self) -> None:
        source_version = "av_" + "a" * 32
        with self.projects.connect(self.project.project_id) as database:
            database.execute(
                "UPDATE asset_versions SET probe_json=? WHERE version_id=?",
                (self.encoded({
                    "capture_time": "2026-07-20T07:30:00+09:00",
                    "location": "GPS 35.676200, 139.650300",
                }), source_version),
            )
        context = self.builder.build(self.project.project_id)
        summary = context.candidates[0].evidence_summary
        self.assertIn("旅行第 1 天", summary)
        self.assertIn("早上", summary)
        self.assertIn("GPS 35.676200", summary)
        self.assertIn("使用者尚未核對", summary)

    def test_candidate_limit_samples_across_materials_before_taking_depth(self) -> None:
        groups = []
        for group_index in range(3):
            groups.append(
                [
                    PlanningCandidate(
                        f"candidate-{group_index}-{depth}",
                        self.span("av_" + f"{group_index + 1:032x}"),
                        f"毛片 {group_index + 1:03d}",
                        "video",
                        "本機範圍",
                        TranscriptReliability.UNAVAILABLE,
                    )
                    for depth in range(3)
                ]
            )
        selected = PlanningContextBuilder._round_robin(groups, 5)
        self.assertEqual(
            [candidate.candidate_id for candidate in selected],
            [
                "candidate-0-0",
                "candidate-1-0",
                "candidate-2-0",
                "candidate-0-1",
                "candidate-1-1",
            ],
        )


if __name__ == "__main__":
    unittest.main()
