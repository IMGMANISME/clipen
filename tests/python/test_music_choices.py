import tempfile
import unittest
from pathlib import Path

from clipen.editing.music_choices import music_choices
from clipen.editing.production_media import AgentAudioStore
from clipen.projects import ProjectRepository


class MusicChoicesTests(unittest.TestCase):
    def test_groups_identical_audio_and_rights_without_removing_stored_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            projects = ProjectRepository(Path(directory) / "projects")
            project = projects.create_project("music-choice-project", "配樂選單")
            store = AgentAudioStore(projects, project.project_id)
            base = {
                "asset_id": "a",
                "name": "同一首歌曲",
                "role": "music",
                "duration_seconds": 214,
                "fingerprint": "audio-hash",
                "relative_path": "private.wav",
                "provenance": {"license": "by", "source_url": "https://example.com/track", "retrieved_at": "yesterday"},
            }
            assets = [
                base,
                {**base, "asset_id": "b", "provenance": {**base["provenance"], "retrieved_at": "today"}},
                {**base, "asset_id": "different-audio", "fingerprint": "other-hash"},
                {
                    **base,
                    "asset_id": "different-rights",
                    "provenance": {**base["provenance"], "license": "user_supplied"},
                },
            ]
            store.add(assets, {})
            choices = music_choices(store)
            self.assertEqual(choices[0]["duplicate_group"], choices[1]["duplicate_group"])
            self.assertEqual(len({c["duplicate_group"] for c in choices}), 3)
            self.assertEqual(store.public_assets()[0]["asset_id"], "a")
            self.assertEqual(len(store.records()), 4)
            self.assertTrue(all("fingerprint" not in c and "relative_path" not in c for c in choices))
