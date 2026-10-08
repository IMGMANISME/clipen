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
