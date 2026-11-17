"""People return to complete edits, not to each AI tool call."""

import json
import sqlite3
import unittest

from clipen.editing.versions import film_usage, versions


def database(events):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE edit_agent_events (run_id TEXT, sequence INTEGER, event_type TEXT, payload_json TEXT, created_at TEXT)"
    )
    for n, (kind, payload) in enumerate(events):
        db.execute(
            "INSERT INTO edit_agent_events VALUES (?,?,?,?,?)",
            ("run", n, kind, json.dumps(payload), f"2026-10-27T10:{n:02}:00+00:00"),
        )
    return db


class VersionTests(unittest.TestCase):
    def test_each_request_manual_edit_and_restore_is_one_version(self):
        db = database(
            [
                ("run_created", {}),
                *[("revision_created", {"revision_id": f"r{n}"}) for n in range(5)],
                ("run_finished", {}),
                ("refinement_requested", {"instruction": "換配樂", "base_revision_id": "r4"}),
                ("provider_switched", {"from": "codex", "to": "claude"}),
                ("revision_created", {"revision_id": "r5"}),
                ("revision_created", {"revision_id": "r6"}),
                ("provider_failed", {"error_code": "provider_usage_limit"}),
                ("run_resumed", {}),
                ("revision_created", {"revision_id": "r7"}),
                ("run_finished", {}),
                ("revision_created", {"revision_id": "r8"}),
                ("tool_completed", {"tool": "edit_text", "authority": "user"}),
                ("revision_created", {"revision_id": "r9"}),
                ("tool_completed", {"tool": "restore_revision", "authority": "user"}),
                ("refinement_requested", {"instruction": "再精修", "base_revision_id": "r9"}),
                ("provider_failed", {"error_code": "provider_timed_out"}),
            ]
        )
        listed = versions(db, "run", "claude")
        self.assertEqual(
            [(v["number"], v["revision_id"], v["label"]) for v in listed],
            [
                (5, "r9", "還原版本"),
                (4, "r8", "手動修改：文字與時間"),
                (3, "r7", "Claude 打磨完成"),
                (2, "r6", "Claude 未完成（額度不足時停下）"),
                (1, "r4", "Codex 製作完成"),
            ],
        )
        self.assertEqual(listed[2]["detail"], "換配樂")
        # A stop that changed nothing (the last refinement) adds no version.
        self.assertNotIn("超過時間", " ".join(v["label"] for v in listed))

    def test_each_ai_version_carries_the_tokens_it_used(self):
        use = lambda n: {"input_tokens": n * 10, "cached_input_tokens": n * 8, "output_tokens": n}
        db = database(
            [
                ("run_created", {}),
                ("revision_created", {"revision_id": "r0"}),
                ("revision_created", {"revision_id": "r1"}),
                # A stopped attempt reports its tokens before its version is listed...
                ("provider_failed", {"error_code": "provider_usage_limit", "usage": use(5)}),
                ("run_resumed", {}),
                ("revision_created", {"revision_id": "r2"}),
                ("run_finished", {}),
                # ...a finished one just after.
                ("provider_finished", {"usage": use(3)}),
                ("refinement_requested", {"instruction": "沒改到東西", "base_revision_id": "r2"}),
                ("provider_failed", {"error_code": "provider_timed_out", "usage": use(7)}),
                ("refinement_requested", {"instruction": "換配樂", "base_revision_id": "r2"}),
                ("revision_created", {"revision_id": "r3"}),
                ("run_finished", {}),
                ("provider_finished", {"usage": use(2)}),
                ("revision_created", {"revision_id": "r4"}),
                ("tool_completed", {"tool": "edit_component", "authority": "user"}),
            ]
        )
        listed = {v["revision_id"]: v.get("usage") for v in versions(db, "run", "claude")}
        self.assertEqual(listed, {"r1": use(5), "r2": use(3), "r3": use(2), "r4": None})
        # The film's total also counts the attempt that changed nothing.
        self.assertEqual(film_usage(db, "run"), use(17))


if __name__ == "__main__":
    unittest.main()
