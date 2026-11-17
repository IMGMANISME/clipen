"""Bounded public music discovery with retained source and license evidence."""

from __future__ import annotations

import ipaddress
import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from clipen.music_vocabulary import MUSIC_TAGS
from clipen.projects import DirectoryReference, DirectoryRole, ProjectError
from clipen.projects.storage import utc_now

from .audio import inspect_library
from .models import digest, encoded

_HOSTS = {
    "api.openverse.org",
    "prod-1.storage.jamendo.com",
    "prod-2.storage.jamendo.com",
    "cdn.freesound.org",
    "freesound.org",
    "www.jamendo.com",
    "ccmixter.org",
    "dig.ccmixter.org",
    "upload.wikimedia.org",
    "commons.wikimedia.org",
}
_USER_AGENT = "Clipen/0.0 (+https://github.com/IMGMANISME/clipen)"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def checked_url(url):
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("invalid catalog URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("unapproved catalog destination")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("non-public catalog address")
    return url


class PublicMusicCatalog:
    """Only musical vocabulary is searched; private project text is never queried."""

    def fetch(self, url, cancel, *, limit=2 * 1024 * 1024, destination=None):
        opener = build_opener(NoRedirect)
        deadline = time.monotonic() + 60
        for _ in range(4):
            if cancel.is_set():
                raise ProjectError("user_cancelled", "已取消自動配樂。", status_code=409)
            checked_url(url)
            try:
                response = opener.open(
                    Request(
                        url,
                        headers={
                            "User-Agent": _USER_AGENT,
                            "Accept": "application/json, audio/*;q=0.9, text/html;q=0.5",
                        },
                    ),
                    timeout=15,
                )
            except HTTPError as error:
                if error.code in {301, 302, 303, 307, 308} and error.headers.get("Location"):
                    url = urljoin(url, error.headers["Location"])
                    error.close()
                    continue
                raise
            with response:
                length = response.headers.get("Content-Length")
                if length and int(length) > limit:
                    raise ValueError("catalog response too large")
                data = bytearray()
                output = None if destination is None else destination.open("xb")
                size = 0
                try:
                    while True:
                        if cancel.is_set():
                            raise ProjectError("user_cancelled", "已取消自動配樂。", status_code=409)
                        if time.monotonic() > deadline:
                            raise TimeoutError("music transfer exceeded its time bound")
                        block = response.read(65536)
                        if not block:
                            break
                        size += len(block)
                        if size > limit:
                            raise ValueError("catalog response too large")
                        if output is None:
                            data.extend(block)
                        else:
                            output.write(block)
                finally:
                    if output is not None:
                        output.close()
                return bytes(data)
        raise ValueError("too many catalog redirects")

    def search_and_prepare(self, brief, directory, cancel, progress):
        tags = brief["music_tags"]
        if not tags or any(t not in MUSIC_TAGS for t in tags):
            raise ProjectError("finishing_music_query_invalid", "AI 配樂方向無效。", status_code=422)
        directory.mkdir(mode=0o700, exist_ok=False)
        metadata = {}
        issues = []
        # Two bounded searches; choose music tagged instrumental, never field recordings.
        queries = [tags[0] + " instrumental"]
        if len(tags) > 1:
            queries.append(tags[1] + " instrumental")
        for query in dict.fromkeys(queries):
            try:
                url = "https://api.openverse.org/v1/audio/?" + urlencode(
                    {"q": query, "license": "cc0,by", "page_size": 8}
                )
                result = json.loads(self.fetch(url, cancel))
                for raw in result.get("results", [])[:8]:
                    candidate = normalize_candidate(raw)
                    if candidate is not None:
                        metadata.setdefault(candidate["catalog_id"], candidate)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                issues.append({"code": "search_unavailable", "query": query})
        if not metadata:
            raise ProjectError("finishing_catalog_unavailable", "目前搜尋不到可用配樂，請稍後重試。", status_code=409)
        # Metadata tags are catalog evidence, not a claim that Codex heard the music.
        ranked = sorted(metadata.values(), key=lambda c: sum(t in c["tags"] for t in tags), reverse=True)
        downloaded = {}
        for candidate in ranked[:8]:
            if len(downloaded) >= 3:
                break
            path = None
            try:
                # Refresh the exact candidate immediately before acquisition.
                raw = json.loads(
                    self.fetch("https://api.openverse.org/v1/audio/" + candidate["catalog_id"] + "/", cancel)
                )
                current = normalize_candidate(raw)
                if current is None:
                    continue
                name = "music-" + digest(current["catalog_id"])[:24] + ".mp3"
                path = directory / name
                self.fetch(current["download_url"], cancel, limit=24 * 1024 * 1024, destination=path)
                downloaded[name] = current
                progress(0.25 + len(downloaded) * 0.1)
            except Exception:
                if cancel.is_set():
                    raise
                if path is not None:
                    path.unlink(missing_ok=True)
                issues.append({"code": "candidate_unavailable", "title": candidate["title"]})
        if not downloaded:
            raise ProjectError(
                "finishing_music_unavailable",
                "找不到可下載且有完整授權資料的配樂，請調整影片方向或稍後重試。",
                status_code=409,
            )
        info = directory.stat()
        reference = DirectoryReference(
            DirectoryRole.SOURCE,
            str(directory.resolve()),
            directory.name,
            directory.name,
            info.st_dev,
            info.st_ino,
            utc_now(),
        ).to_storage()
        assets, audio_issues = inspect_library("music", reference, cancel, lambda value: progress(0.55 + value * 0.4))
        prepared = []
        for asset in assets:
            item = downloaded.get(asset["name"])
            if item is None or asset["duration_seconds"] < 30:
                continue
            asset["name"] = item["title"]
            asset["tags"] = item["tags"]
            asset["genres"] = item["genres"]
            asset["provenance"] = {k: v for k, v in item.items() if k != "download_url"} | {
                "retrieved_at": utc_now(),
                "verification": "catalog_metadata",
                "changes": "Clipen 將本曲截取、調整音量並混入影片。",
            }
            prepared.append(asset)
        if not prepared:
            raise ProjectError("finishing_music_unusable", "候選配樂無法解碼或長度不足，請重新搜尋。", status_code=409)
        # Keep the exact metadata consulted; future index updates do not rewrite this record.
        (directory / "sources.json").write_text(
            encoded(
                {"queries": queries, "sources": [a["provenance"] for a in prepared], "issues": issues + audio_issues}
            )
        )
        return prepared, reference


def normalize_candidate(raw):
    """Exclude restrictive/unknown license metadata rather than inventing rights."""
    if not isinstance(raw, dict):
        return None
    license_id = raw.get("license")
    version = str(raw.get("license_version") or "")
    expected = (
        "https://creativecommons.org/publicdomain/zero/1.0/"
        if license_id == "cc0"
        else f"https://creativecommons.org/licenses/by/{version}/"
    )
    license_url = str(raw.get("license_url") or "").replace("http://", "https://")
    if (
        license_id not in {"cc0", "by"}
        or license_url != expected
        or (license_id == "by" and version not in {"1.0", "2.0", "2.5", "3.0", "4.0"})
    ):
        return None
    from uuid import UUID

    try:
        identifier = str(UUID(raw["id"]))
        duration = float(raw.get("duration", 0)) / 1000
        download = urlsplit(raw["url"])
        landing = urlsplit(raw["foreign_landing_url"])
        tags = [
            t["name"].lower() for t in (raw.get("tags") or []) if isinstance(t, dict) and isinstance(t.get("name"), str)
        ][:80]
        if not 30 <= duration <= 3600 or "instrumental" not in tags:
            return None
        if (
            download.scheme != "https"
            or download.hostname not in _HOSTS
            or landing.scheme not in {"http", "https"}
            or landing.hostname not in _HOSTS
        ):
            return None
        if any(part.username or part.password or part.port not in (None, 443) for part in (download, landing)):
            return None
        title, creator = raw.get("title"), raw.get("creator")
        if not isinstance(title, str) or not title.strip() or not isinstance(creator, str) or not creator.strip():
            return None
        return {
            "catalog_id": identifier,
            "title": title[:255],
            "creator": creator[:255],
            "source_url": raw["foreign_landing_url"].replace("http://", "https://"),
            "download_url": raw["url"],
            "license": license_id,
            "license_version": version,
            "license_url": expected,
            "attribution": f"{title[:255]} — {creator[:255]}. {expected} {raw['foreign_landing_url']}",
            "tags": tags,
            "genres": [str(g)[:80] for g in (raw.get("genres") or [])[:12]],
            "catalog_url": "https://api.openverse.org/v1/audio/" + identifier + "/",
        }
    except (KeyError, TypeError, ValueError):
        return None
