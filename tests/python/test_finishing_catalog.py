import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from clipen.finishing.catalog import PublicMusicCatalog, checked_url, normalize_candidate
from clipen.projects import ProjectError


def track(identifier="d73e2696-d190-4bf6-a41e-ead3b4f2f674"):
    return {
        "id": identifier,
        "title": "Travel Theme",
        "creator": "Test Composer",
        "license": "by",
        "license_version": "3.0",
        "license_url": "https://creativecommons.org/licenses/by/3.0/",
        "duration": 135000,
        "url": "https://prod-1.storage.jamendo.com/?trackid=1687326&format=mp32",
        "foreign_landing_url": "https://www.jamendo.com/track/1687326",
        "tags": [{"name": "instrumental"}, {"name": "travel"}, {"name": "acoustic"}],
        "genres": ["folk"],
    }


class MusicCatalogTests(unittest.TestCase):
    def test_source_and_license_evidence_are_preserved(self):
        result = normalize_candidate(track())
        self.assertEqual(result["license"], "by")
        self.assertIn("Test Composer", result["attribution"])
        self.assertEqual(result["source_url"], "https://www.jamendo.com/track/1687326")
        self.assertIn("acoustic", result["tags"])

    def test_noncommercial_no_derivatives_unknown_and_incomplete_licenses_rejected(self):
        for change in (
            {"license": "by-nc"},
            {"license": "by-nd"},
            {"license": "unknown"},
            {"license_url": "https://example.org/free"},
            {"creator": None},
            {"tags": [{"name": "voice"}]},
            {"url": "file:///etc/passwd"},
            {"url": "https://127.0.0.1/music.mp3"},
            {"duration": 1000},
        ):
            with self.subTest(change=change):
                value = track()
                value.update(change)
                self.assertIsNone(normalize_candidate(value))

    def test_network_destinations_are_allowlisted_and_public(self):
        for url in (
            "http://api.openverse.org/v1/audio/",
            "https://127.0.0.1/audio",
            "https://api.openverse.org:8080/audio",
            "https://user:secret@api.openverse.org/audio",
        ):
            with self.assertRaises(ValueError):
                checked_url(url)
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ValueError):
                checked_url("https://api.openverse.org/v1/audio/")

    def test_queries_use_only_controlled_music_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ProjectError):
                PublicMusicCatalog().search_and_prepare(
                    {"music_tags": ["private-project-location"]}, Path(tmp) / "music", threading.Event(), lambda _: None
                )

    def test_partial_search_outage_uses_valid_candidates_and_failed_detail_does_not_delete_prior_file(self):
        first = track()
        second = track("b1be6e55-bbb8-4259-bd9d-0ee64ec0e255")
        queries = []

        class Catalog(PublicMusicCatalog):
            def fetch(self, url, cancel, *, limit=None, destination=None):
                if "/audio/?" in url:
                    queries.append(url)
                    if len(queries) == 2:
                        raise TimeoutError("search unavailable")
                    return json.dumps({"results": [first, second]}).encode()
                if second["id"] in url:
                    raise TimeoutError("detail unavailable")
                if "/audio/" in url:
                    return json.dumps(first).encode()
                destination.write_bytes(b"test music")
                return b""

        def inspected(role, reference, cancel, progress):
            files = list(Path(reference["canonical_path"]).glob("*.mp3"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"test music")
            return [
                {
                    "asset_id": "music-one",
                    "name": files[0].name,
                    "role": "music",
                    "duration_seconds": 135.0,
                    "beats_seconds": [1.0],
                }
            ], []

        with tempfile.TemporaryDirectory() as tmp:
            with patch("clipen.finishing.catalog.inspect_library", side_effect=inspected):
                assets, _ = Catalog().search_and_prepare(
                    {"music_tags": ["travel", "acoustic"]}, Path(tmp) / "music", threading.Event(), lambda _: None
                )
            self.assertEqual(assets[0]["name"], "Travel Theme")
            self.assertEqual(assets[0]["provenance"]["verification"], "catalog_metadata")
            self.assertTrue((Path(tmp) / "music" / "sources.json").is_file())
            self.assertNotIn("private", queries[0])


if __name__ == "__main__":
    unittest.main()
