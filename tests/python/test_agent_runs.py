"""Edit runs, their tool surface and the coordinator; every run is an AI production session."""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from clipen.editing import EditAgentCoordinator, EditAgentTools
from clipen.editing.agent_models import ToolResult
from clipen.projects import ProjectError
from clipen.providers import ProviderCancelled
from production_fixture import PRODUCTION, ProductionFixture


class SuccessfulAgent:
    def __init__(self, projects_root, attempt_root, repository):
        self.repository = repository

    def execute(self, project_id, run_id, *, cancel):
        self.repository.activate(project_id, run_id)
        self.repository.finish(project_id, run_id, "fake provider finished")
        self.repository.record_provider_outcome(
            project_id, run_id, "agent-fake-success",
            usage={"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30},
        )


class BlockingAgent:
    def __init__(self, projects_root, attempt_root, repository):
        self.repository = repository

    def execute(self, project_id, run_id, *, cancel):
        self.repository.activate(project_id, run_id)
        cancel.wait(2)
        raise ProviderCancelled("controller stopped")


class AgentRunTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ProductionFixture(); self.fixture.setUp()
        self.projects = self.fixture.projects
        self.project = self.fixture.project
        self.repository = self.fixture.repository

    def tearDown(self):
        self.fixture.tearDown()

    def call(self, tools, name, **arguments):
        return self.fixture.call(tools, name, arguments)

    def test_run_is_idempotent_and_starts_with_an_empty_draft(self):
        def start():
            return self.repository.create_run(self.project.project_id, "request-edit-run", production=PRODUCTION,
                                              max_tool_calls=12, max_preview_renders=2)
        run, created = start()
        replay, replay_created = start()
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay, run)
        revision = self.repository.current_revision(self.project.project_id, run.run_id)
        self.assertEqual(revision.ordinal, 0)
        self.assertEqual(revision.timeline.duration_frames, 0)
        self.assertIsNone(revision.timeline.plan_id)
        with self.projects.connect(self.project.project_id) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 19)

    def test_runs_require_a_production_session(self):
        with self.assertRaises(TypeError):
            self.repository.create_run(self.project.project_id, "request-no-session")
        with self.assertRaises(ProjectError) as invalid:
            self.repository.create_run(self.project.project_id, "request-no-session", production=None)
        self.assertEqual(invalid.exception.code, "production_input_invalid")

    def test_tools_create_revisions_undo_and_finish_without_paths(self):
        run, tools = self.fixture.create_tools()
        candidate = self.fixture.candidate
        status = self.call(tools, "get_edit_status")
        self.assertEqual(status.payload["mode"], "skill_production")
        self.assertEqual(status.payload["intent"]["brief"], PRODUCTION["intent"])
        self.assertNotIn("confirmed_direction", status.payload)

        searched = self.call(tools, "search_media", query="東京", limit=10)
        self.assertEqual(searched.payload["returned"], 1)
        self.assertNotIn(str(self.projects.root), json.dumps(searched.payload))

        replaced = self.call(tools, "replace_sequence", clips=[
            {"candidate_id": candidate, "start_offset_seconds": offset, "duration_seconds": 4}
            for offset in (0, 0.5, 1, 1.5, 2, 2.5, 3, 4)], summary="以精確入出點建立短樣片")
        self.assertEqual(replaced.payload["revision"]["ordinal"], 1)
        sequence = self.repository.current_revision(self.project.project_id, run.run_id).timeline
        self.assertEqual(sequence.duration_frames, 960)
        self.assertTrue(all(item.duration_frames == 120 for item in sequence.tracks[0].items))
        effected = self.call(tools, "set_clip_effect", item_id="agent-video-0001", effect_id="video.slow_push",
                             parameters={"scale_from": 1.0, "scale_to": 1.08}, summary="加入緩慢推進")
        self.assertEqual(effected.payload["revision"]["ordinal"], 2)
        undone = self.call(tools, "undo_revision")
        self.assertEqual(undone.payload["revision"]["ordinal"], 1)
        restored = self.call(tools, "set_clip_effect", item_id="agent-video-0001", effect_id="video.slow_push",
                             parameters={}, summary="使用安全預設加入緩慢推進")
        self.assertEqual(restored.payload["revision"]["ordinal"], 2)
        restored_timeline = self.repository.current_revision(self.project.project_id, run.run_id).timeline
        self.assertEqual(restored_timeline.tracks[0].items[0].effects[0].parameters,
                         {"scale_from": 1.0, "scale_to": 1.08})

        reviewed = {"reviewed_revision": True}
        valid = ToolResult({"issues": []})
        with patch("clipen.editing.production_preview.finish_review_status", return_value=reviewed), \
                patch.object(EditAgentTools, "_production_validate_sequence", return_value=valid):
            # Every production run must say what it changed before it can finish.
            with self.assertRaises(ProjectError) as unreported:
                self.call(tools, "finish_edit", summary="完成第一版旅行初剪")
            self.assertEqual(unreported.exception.code, "change_report_invalid")
            report = dict(summary="完成第一版旅行初剪", changes=[{"what": "剪出八個街景鏡頭", "why": "交代早上的東京"}])
            left = [{"what": "片尾店名", "reason": "needs_you", "detail": "請告訴我拉麵店的名字"}]
            # Work left undone is sent back once, to be finished with the tools first...
            with self.assertRaises(ProjectError) as challenged:
                self.call(tools, "finish_edit", **report, not_done=left)
            self.assertEqual(challenged.exception.code, "change_report_unfinished")
            # ...and kept only when the AI confirms it really needs the person.
            final = self.call(tools, "finish_edit", **report, not_done=left)
        self.assertEqual(final.payload["state"], "succeeded")
        self.assertEqual(final.payload["final_revision_id"], restored.payload["revision"]["revision_id"])
        with self.projects.connect(self.project.project_id) as database:
            finished = database.execute("SELECT payload_json FROM edit_agent_events WHERE run_id=? AND event_type='run_finished'",
                                        (run.run_id,)).fetchone()
        self.assertEqual(json.loads(finished["payload_json"])["report"]["changes"][0]["what"], "剪出八個街景鏡頭")
        # What was left for the person travels with the next request.
        self.repository.request_refinement(self.project.project_id, run.run_id, "request-refine-left",
                                           final.payload["final_revision_id"], "店名是一蘭")
        with self.projects.connect(self.project.project_id) as database:
            intent = json.loads(database.execute("SELECT intent_json FROM production_sessions WHERE run_id=?", (run.run_id,)).fetchone()["intent_json"])
        self.assertEqual(intent["latest_revision_request"]["left_last_time"], left)

        recorded = self.repository.record_provider_outcome(
            self.project.project_id, run.run_id, "agent-attempt-0001",
            usage={"input_tokens": 1200, "cached_input_tokens": 200, "output_tokens": 300})
        self.assertEqual(recorded.usage["input_tokens"], 1200)
        self.assertEqual(recorded.to_public()["usage"]["total_tokens"], 1500)

    def test_mutations_need_the_revision_guard(self):
        _, tools = self.fixture.create_tools()
        with self.assertRaises(ProjectError) as unguarded:
            tools.call("replace_sequence", {"clips": [{"candidate_id": self.fixture.candidate,
                                                       "start_offset_seconds": 0, "duration_seconds": 4}],
                                            "summary": "沒有版本保護"})
        self.assertEqual(unguarded.exception.code, "mutation_guard_required")

    def test_sequence_rejects_repeated_ranges_and_overlong_shots(self):
        _, tools = self.fixture.create_tools()
        repeated = [{"candidate_id": self.fixture.candidate, "start_offset_seconds": 0, "duration_seconds": 4}] * 2
        with self.assertRaises(ProjectError) as duplicate:
            self.call(tools, "replace_sequence", clips=repeated, summary="重複")
        self.assertEqual(duplicate.exception.code, "agent_input_invalid")
        with self.assertRaises(ProjectError) as long_shot:
            self.call(tools, "replace_sequence", clips=[{"candidate_id": self.fixture.candidate,
                                                        "start_offset_seconds": 0, "duration_seconds": 31}],
                      summary="太長")
        self.assertEqual(long_shot.exception.code, "agent_input_invalid")

    def test_candidate_inspection_uses_the_explicit_run_budget(self):
        _, tools = self.fixture.create_tools(calls=20)
        for _ in range(20):
            inspected = self.call(tools, "inspect_clip", candidate_id=self.fixture.candidate)
            self.assertEqual(inspected.payload["image_count"], 0)
        with self.assertRaises(ProjectError) as exhausted:
            self.call(tools, "inspect_clip", candidate_id=self.fixture.candidate)
        self.assertEqual(exhausted.exception.code, "tool_budget_exhausted")

    def test_tool_budget_interrupts_run(self):
        run, tools = self.fixture.create_tools(calls=1)
        self.call(tools, "get_edit_status")
        with self.assertRaises(ProjectError) as context:
            self.call(tools, "inspect_sequence")
        self.assertEqual(context.exception.code, "tool_budget_exhausted")
        self.assertEqual(self.repository.get(self.project.project_id, run.run_id).state, "interrupted")

    def test_rejects_unknown_tool_and_unknown_arguments(self):
        _, tools = self.fixture.create_tools()
        with self.assertRaises(ProjectError) as unknown:
            self.call(tools, "run_shell", command="cat /etc/passwd")
        self.assertEqual(unknown.exception.code, "tool_not_allowed")
        with self.assertRaises(ProjectError) as fields:
            self.call(tools, "search_media", path="/Volumes/private")
        self.assertEqual(fields.exception.code, "agent_input_invalid")
        self.call(tools, "replace_sequence", clips=[{"candidate_id": self.fixture.candidate,
                                                     "start_offset_seconds": 0, "duration_seconds": 4}],
                  summary="一個鏡頭")
        with self.assertRaises(ProjectError) as effect:
            self.call(tools, "set_clip_effect", item_id="agent-video-0001", effect_id="video.punch_in",
                      parameters={"scale": 99}, summary="invalid scale")
        self.assertEqual(effect.exception.code, "agent_input_invalid")

    def test_coordinator_runs_one_shot_provider_and_reports_usage(self):
        coordinator = EditAgentCoordinator(self.repository, Path(self.fixture.temporary.name),
                                           adapter_factory=SuccessfulAgent)
        try:
            run = coordinator.start(self.project.project_id, "request-agent-coordinator", True,
                                    max_tool_calls=10, max_preview_renders=1, production=PRODUCTION)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                latest = coordinator.status(self.project.project_id)["latest_run"]
                if latest and latest["state"] == "succeeded" and latest["usage"] is not None:
                    break
                time.sleep(0.01)
            else:
                self.fail("agent coordinator did not finish")
        finally:
            coordinator.close()
        self.assertEqual(latest["run_id"], run.run_id)
        self.assertEqual(latest["usage"]["input_tokens"], 100)
        self.assertIsNotNone(latest["final_revision_id"])

    def test_controller_shutdown_marks_active_agent_interrupted(self):
        coordinator = EditAgentCoordinator(self.repository, Path(self.fixture.temporary.name),
                                           adapter_factory=BlockingAgent)
        run = coordinator.start(self.project.project_id, "request-agent-shutdown", True, production=PRODUCTION)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.repository.get(self.project.project_id, run.run_id).state == "running":
                break
            time.sleep(0.01)
        coordinator.close()
        stopped = self.repository.get(self.project.project_id, run.run_id)
        self.assertEqual(stopped.state, "interrupted")
        self.assertEqual(stopped.error_code, "controller_stopped")

    def test_recovery_seals_successful_run_missing_provider_usage(self):
        run, _ = self.repository.create_run(self.project.project_id, "request-agent-finished-before-usage",
                                            production=PRODUCTION)
        self.repository.activate(self.project.project_id, run.run_id)
        self.repository.finish(self.project.project_id, run.run_id, "已完成剪輯")
        coordinator = EditAgentCoordinator(self.repository, Path(self.fixture.temporary.name),
                                           adapter_factory=SuccessfulAgent)
        self.assertEqual(coordinator.recover(), 1)
        recovered = self.repository.get(self.project.project_id, run.run_id)
        self.assertEqual(recovered.state, "succeeded")
        self.assertIsNone(recovered.usage)
        self.assertEqual(recovered.provider_attempt_id, "recovered-" + run.run_id.removeprefix("editrun_"))


if __name__ == "__main__":
    unittest.main()
