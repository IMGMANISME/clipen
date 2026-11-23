"""The workbench marks the texts and graphics of a draft the AI never looked at."""

import json
import sqlite3
import unittest
from types import SimpleNamespace

from clipen.domain import TextClip, TrackKind
from clipen.editing.editorial_review import text_fingerprints
from clipen.editing.review_coverage import ai_review_coverage


def timeline():
    cards = [TextClip(f"card-{n}", n * 30, 30, f"卡片 {n}", "look") for n in range(4)]
    return SimpleNamespace(
        frame_rate=SimpleNamespace(numerator=30, denominator=1),
        tracks=[SimpleNamespace(kind=TrackKind.TITLE, items=cards)],
    )


class ReviewCoverageTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE edit_agent_events (run_id TEXT, sequence INTEGER, event_type TEXT, payload_json TEXT)"
        )

    def event(self, kind, payload):
        count = self.db.execute("SELECT COUNT(*) FROM edit_agent_events").fetchone()[0]
        self.db.execute("INSERT INTO edit_agent_events VALUES ('run', ?, ?, ?)", (count, kind, json.dumps(payload)))

    def test_only_looks_at_this_revision_count(self):
        # A still at 0.5 s shows card 0; one at 2.5 s of an older revision says nothing about this one.
        self.event("production_review", {"revision_id": "rev-2", "offsets_seconds": [0.5]})
        self.event("production_review", {"revision_id": "rev-1", "offsets_seconds": [2.5]})
        result = ai_review_coverage(self.db, "run", timeline(), "rev-2")
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["unreviewed_item_ids"], ["card-1", "card-2", "card-3"])

    def test_an_editorial_review_counts_its_checks_and_carried_texts(self):
        carried = text_fingerprints(timeline())["card-3"]
        self.event(
            "editorial_review",
            {
                "revision_id": "rev-2",
                "text_checks": [{"item_id": "card-1", "matches": True}],
                "carried_texts": [carried],
            },
        )
        result = ai_review_coverage(self.db, "run", timeline(), "rev-2")
        self.assertEqual(result["unreviewed_item_ids"], ["card-0", "card-2"])


if __name__ == "__main__":
    unittest.main()
