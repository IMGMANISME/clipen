"""Recoverable jobs that extract bounded evidence entirely on the local machine."""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import queue
import re
import shutil
import threading
import uuid

from clipen.media import InventoryRoot
from clipen.projects.models import ProjectError, validate_request_id, validate_revision
from clipen.projects.storage import utc_now
from .artifacts import ArtifactStore
from .audio import AudioPreparationInput, AudioPreparationError, project_samples_to_source
from .evidence import CoverageInterval, EvidenceCoverage, EvidenceModality
from .models import ArtifactKind, SourceSpan
from .storage import _source_span
from .visual import LocalEvidenceExtractor, verified_source

RECIPE = "evidence.local.rgb8-512.v1"
MAX_STREAMS = 2000
PAGE_SIZE = 25


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fail(code, message="分析狀態已改變，請重新整理後再試。"):
    return ProjectError(code, message, status_code=409)


def source_input(item):
    return AudioPreparationInput(InventoryRoot.from_storage(item["root"]), item["relative_path"],
                                 item["version_id"], item["stream_index"], item["size_bytes"],
                                 item["mtime_ns"], item["fingerprint"], SourceSpan.from_storage(item["source"]))


class EvidenceService:
    def __init__(self, projects, *, extractor_factory=LocalEvidenceExtractor):
        self.projects = projects
        self.artifacts = ArtifactStore(projects)
        self.extractor_factory = extractor_factory
        self._lock = threading.Lock()
        self._queue = queue.Queue(maxsize=16)
        self._cancel = {}
        self._closed = False
        self._worker = None

    def recover(self):
        for project in self.projects.list_projects():
            with self.projects.connect(project.project_id) as db:
                db.execute("UPDATE evidence_runs SET state='interrupted', error_code='controller_stopped' WHERE state='running'")

    def scope(self, project_id):
        with self.projects.connect(project_id) as db:
            project = db.execute("SELECT revision FROM project").fetchone()
            inventory = db.execute("SELECT r.* FROM current_inventory c JOIN inventory_runs r ON c.run_id=r.run_id WHERE c.slot=1").fetchone()
            if inventory is None:
                raise fail("inventory_required", "請先完成素材檢查。")
            rows = db.execute("SELECT a.role,a.relative_path,i.display_name,i.version_id,v.size_bytes,v.mtime_ns,v.fingerprint,s.stream_index,s.kind,s.details_json FROM inventory_items i JOIN assets a ON a.asset_id=i.asset_id JOIN asset_versions v ON v.version_id=i.version_id JOIN media_streams s ON s.version_id=i.version_id WHERE i.run_id=? AND i.status='usable' AND i.kind IN ('video','audio') ORDER BY a.role,a.relative_path,s.stream_index LIMIT ?", (inventory["run_id"], MAX_STREAMS * 2 + 1)).fetchall()
        if not rows:
            raise fail("evidence_empty", "目前沒有可擷取證據的影音串流。")
        roots = {r["role"]: r for r in json.loads(inventory["roots_json"])}
        items = []
        for row in rows:
            item = dict(row)
            details = json.loads(item.pop("details_json"))
            if details.get("attached_pic") or details.get("timed_thumbnails"):
                continue
            item["root"] = roots[item["role"]]
            item["key"] = f'{item["version_id"]}:{item["stream_index"]}'
            try:
                span = _source_span(item["version_id"], item["stream_index"], details)
                if span.time_base.numerator <= 0:
                    raise ValueError("invalid time base")
                item["source"] = span.to_storage()
                item["error_code"] = None
            except (ValueError, TypeError, KeyError):
                item["source"] = None
                item["error_code"] = "time_metadata_missing"
            items.append(item)
        if not items:
            raise fail("evidence_empty", "目前沒有可擷取證據的影音串流。")
        if len(items) > MAX_STREAMS or len(rows) > MAX_STREAMS * 2:
            raise fail("evidence_scope_too_large", "這批超過 2,000 個影音串流，請分開建立作品。")
        result = {"inventory_run_id": inventory["run_id"], "project_revision": project["revision"],
                  "recipe": RECIPE, "items": items}
        result["scope_id"] = hashlib.sha256(encoded(result).encode()).hexdigest()
        return result

    def _read_run(self, project_id, run_id):
        if not isinstance(run_id, str) or re.fullmatch(r"ev_[0-9a-f]{32}", run_id) is None:
            raise fail("evidence_not_found")
        with self.projects.connect(project_id) as db:
            row = db.execute("SELECT * FROM evidence_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise fail("evidence_not_found")
        result = dict(row)
        with self.projects.connect(project_id) as db:
            result["results_json"] = "[" + ",".join(r[0] for r in db.execute("SELECT result_json FROM evidence_results WHERE run_id=? ORDER BY ordinal", (run_id,))) + "]"
        return result

    def _current(self, project_id, scope):
        if self.scope(project_id)["scope_id"] != scope["scope_id"]:
            raise fail("evidence_scope_changed", "素材範圍已改變，請重新擷取；舊成果仍保留。")

    def status(self, project_id, offset=0):
        if type(offset) is not int or not 0 <= offset <= MAX_STREAMS:
            raise fail("invalid_page")
        try:
            scope = self.scope(project_id)
            blocked = None
        except ProjectError as error:
            if error.code not in {"inventory_required", "evidence_empty", "evidence_scope_too_large"}:
                raise
            scope, blocked = None, error.message
        with self.projects.connect(project_id) as db:
            row = db.execute("SELECT * FROM evidence_runs ORDER BY created_at DESC,rowid DESC LIMIT 1").fetchone()
        latest = None if row is None else {k: row[k] for k in ("run_id", "state", "error_code")}
        if latest is not None:
            with self.projects.connect(project_id) as db:
                counts = db.execute("SELECT COUNT(*),COALESCE(SUM(has_issue),0) FROM evidence_results WHERE run_id=?", (row["run_id"],)).fetchone()
                page = [json.loads(r[0]) for r in db.execute("SELECT result_json FROM evidence_results WHERE run_id=? ORDER BY ordinal LIMIT ? OFFSET ?", (row["run_id"], PAGE_SIZE, offset))]
            latest.update(completed_count=counts[0], issue_count=counts[1], offset=offset, page_size=PAGE_SIZE,
                          progress=json.loads(row["progress_json"]), results=page, total=row["total_count"],
                          stale=scope is None or scope["scope_id"] != row["scope_id"])
        return {"scope": None if scope is None else {"scope_id": scope["scope_id"], "project_revision": scope["project_revision"], "total": len(scope["items"]),
                                                   "video_streams": sum(i["kind"] == "video" for i in scope["items"])},
                "blocked_reason": blocked, "latest": latest,
                "limits": {"frames_per_stream": 8, "local_window_seconds": 120}}

    def start(self, project_id, request_id, revision, scope_id):
        validate_request_id(request_id)
        validate_revision(revision)
        if not isinstance(scope_id, str) or re.fullmatch(r"[0-9a-f]{64}", scope_id) is None:
            raise fail("evidence_scope_changed")
        with self._lock:
            if self._closed or self._queue.full():
                raise fail("evidence_busy", "分析工作佇列已滿，請稍後再試。")
            with self.projects.connect(project_id) as db:
                existing = db.execute("SELECT * FROM evidence_runs WHERE request_id=?", (request_id,)).fetchone()
                if existing:
                    if existing["scope_id"] != scope_id or existing["project_revision"] != revision:
                        raise fail("request_conflict")
                    return self.status(project_id)
            scope = self.scope(project_id)
            if scope["scope_id"] != scope_id or scope["project_revision"] != revision:
                raise fail("evidence_scope_changed")
            if shutil.disk_usage(self.projects.root / project_id).free < 32 * 1024**2:
                raise fail("evidence_disk_full", "Clipen 儲存磁碟可用空間不足；請先釋放空間，毛片不會被刪除。")
            run_id = "ev_" + uuid.uuid4().hex
            with self.projects.connect(project_id) as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM evidence_runs WHERE state='running'").fetchone():
                    raise fail("evidence_busy", "目前已有本機證據擷取工作。")
                db.execute("INSERT INTO evidence_runs(run_id,request_id,scope_id,inventory_run_id,project_revision,state,scope_json,total_count,created_at) VALUES(?,?,?,?,?,'running',?,?,?)",
                           (run_id, request_id, scope_id, scope["inventory_run_id"], revision, encoded(scope), len(scope["items"]), utc_now()))
                db.commit()
            self._enqueue(project_id, run_id)
        return self.status(project_id)

    def resume(self, project_id, run_id):
        with self._lock:
            if self._closed or self._queue.full():
                raise fail("evidence_busy")
            run = self._read_run(project_id, run_id)
            self._current(project_id, json.loads(run["scope_json"]))
            if run["state"] not in {"interrupted", "cancelled"}:
                raise fail("evidence_not_resumable")
            with self.projects.connect(project_id) as db:
                if db.execute("SELECT 1 FROM evidence_runs WHERE state='running'").fetchone():
                    raise fail("evidence_busy")
                db.execute("UPDATE evidence_runs SET state='running',error_code=NULL WHERE run_id=?", (run_id,))
            self._enqueue(project_id, run_id)
        return self.status(project_id)

    def cancel(self, project_id, run_id):
        with self._lock:
            self._read_run(project_id, run_id)
            for (pid, rid), token in self._cancel.items():
                if (pid, rid) == (project_id, run_id):
                    token.set()
        return self.status(project_id)

    def _enqueue(self, project_id, run_id):
        key = (project_id, run_id)
        self._cancel[key] = threading.Event()
        self._queue.put_nowait(key)
        if self._worker is None:
            self._worker = threading.Thread(target=self._loop, name="clipen-evidence", daemon=True)
            self._worker.start()

    def _loop(self):
        while True:
            key = self._queue.get()
            if key is None:
                return
            with self._lock:
                token = self._cancel[key]
            try:
                self._extract(*key, token)
            except Exception:
                # A corrupt project must not kill the worker for all other projects.
                try:
                    with self.projects.connect(key[0]) as db:
                        db.execute("UPDATE evidence_runs SET state='failed',error_code='evidence_storage_failed' WHERE run_id=?", (key[1],))
                except ProjectError:
                    pass
            finally:
                with self._lock:
                    self._cancel.pop(key, None)
                self._queue.task_done()

    def close(self):
        with self._lock:
            self._closed = True
            for token in self._cancel.values():
                token.set()
            worker = self._worker
        if worker:
            self._queue.put(None)
            worker.join(15)

    def _publish(self, project_id, run_id, path, span, recipe, lineage):
        return self.artifacts.publish(project_id, path, kind=ArtifactKind.CHECKPOINT,
            producer_operation_id="op_" + run_id[3:], producer_attempt_id="att_" + run_id[3:],
            recipe_id=recipe, lineage=lineage, source_span=span)

    def _checkpoint(self, project_id, run_id, ordinal, result):
        with self.projects.connect(project_id) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO evidence_results(run_id,ordinal,item_key,result_json,has_issue) VALUES(?,?,?,?,?)",
                       (run_id, ordinal, result["key"], encoded(result), int(bool(result.get("error_code") or result.get("scene_error_code")))))
            for frame in result.get("frames", []):
                db.execute("INSERT OR IGNORE INTO evidence_images(run_id,artifact_id) VALUES(?,?)", (run_id, frame["artifact_id"]))
            db.commit()

    def _progress(self, project_id, run_id, name, phase, frames):
        with self.projects.connect(project_id) as db:
            db.execute("UPDATE evidence_runs SET progress_json=? WHERE run_id=?", (encoded({"display_name": name, "phase": phase, "frames": frames}), run_id))

    def _extract(self, project_id, run_id, cancel):
        run = self._read_run(project_id, run_id)
        scope = json.loads(run["scope_json"])
        results = json.loads(run["results_json"])
        try:
            extractor = self.extractor_factory()
            version = extractor.version()
            for item in scope["items"]:
                if cancel.is_set():
                    raise AudioPreparationError("cancelled")
                self._current(project_id, scope)
                previous = next((r for r in results if r["key"] == item["key"]), None)
                if previous is not None:
                    if not previous.get("error_code"):
                        with verified_source(source_input(item)):
                            pass
                        self.artifacts.path(self.artifacts.get(project_id, previous["artifact_id"]))
                        for frame in previous.get("frames", []):
                            self.artifacts.path(self.artifacts.get(project_id, frame["artifact_id"]))
                    continue
                self._progress(project_id, run_id, item["display_name"], "frames" if item["kind"] == "video" else "audio", 0)
                result = {k: item[k] for k in ("key", "display_name", "kind", "role", "source", "error_code")}
                result.update(frames=[], recipe=RECIPE, ffmpeg=version, preview_color="rgb8_not_color_managed")
                paths = []
                def staging(suffix):
                    path = self.artifacts.create_staging_file(project_id, suffix=suffix)
                    paths.append(path)
                    return path
                try:
                    if shutil.disk_usage(self.projects.root / project_id).free < 16 * 1024**2:
                        raise fail("evidence_disk_full", "Clipen 的分析儲存空間不足。")
                    if item["error_code"]:
                        raise AudioPreparationError(item["error_code"])
                    source = source_input(item)
                    with verified_source(source):
                        pass
                    with self.projects.connect(project_id) as db:
                        cached = db.execute("SELECT result_json FROM evidence_cache WHERE item_key=? AND recipe=? AND ffmpeg=?", (item["key"], RECIPE, version)).fetchone()
                    if cached:
                        candidate = json.loads(cached["result_json"])
                        try:
                            self.artifacts.path(self.artifacts.get(project_id, candidate["artifact_id"]))
                            for frame in candidate.get("frames", []):
                                self.artifacts.path(self.artifacts.get(project_id, frame["artifact_id"]))
                        except (ProjectError, OSError):
                            pass
                        else:
                            result = candidate
                            results.append(result)
                            self._checkpoint(project_id, run_id, len(results)-1, result)
                            continue
                    span = source.source_span
                    if item["kind"] == "video":
                        duration = Fraction(int(span.end_pts) - int(span.start_pts)) * Fraction(span.time_base.numerator, span.time_base.denominator)
                        for ordinal in range(8):
                            if cancel.is_set():
                                raise AudioPreparationError("cancelled")
                            output = staging(".png")
                            pts = extractor.frame(source, duration * ordinal / 8, output, cancel)
                            if any(frame["pts"] == str(pts) for frame in result["frames"]):
                                continue
                            record = self._publish(project_id, run_id, output, span, "evidence.frame.v1", {"pts": str(pts), "inventory_run_id": scope["inventory_run_id"]})
                            result["frames"].append({"pts": str(pts), "artifact_id": record.artifact_id})
                            self._progress(project_id, run_id, item["display_name"], "frames", ordinal + 1)
                        self._progress(project_id, run_id, item["display_name"], "scenes", 8)
                        try:
                            result["scenes"] = extractor.scenes(source, cancel)
                        except AudioPreparationError as error:
                            if error.code == "cancelled":
                                raise
                            result["scene_error_code"] = error.code
                        result["coverage"] = EvidenceCoverage(span, EvidenceModality.VISUAL,
                            sampled_pts=tuple(sorted(int(f["pts"]) for f in result["frames"]))).to_storage()
                    else:
                        features = extractor.audio_features(source, staging(".wav"), cancel)
                        last = features["windows"][-1]["end_sample"]
                        inspected = project_samples_to_source(span, 0, last, 16000)
                        result["audio_features"] = features
                        result["coverage"] = EvidenceCoverage(span, EvidenceModality.AUDIO,
                            (CoverageInterval(int(inspected.start_pts), int(inspected.end_pts)),)).to_storage()
                    with verified_source(source):
                        pass
                    self._current(project_id, scope)
                    manifest = staging(".json")
                    manifest.write_text(encoded(result), encoding="utf-8")
                    record = self._publish(project_id, run_id, manifest, span, RECIPE, {"inventory_run_id": scope["inventory_run_id"]})
                    result["artifact_id"] = record.artifact_id
                    with self.projects.connect(project_id) as db:
                        db.execute("INSERT OR REPLACE INTO evidence_cache(item_key,recipe,ffmpeg,result_json) VALUES(?,?,?,?)", (item["key"], RECIPE, version, encoded(result)))
                except Exception as error:
                    if cancel.is_set() or getattr(error, "code", None) == "cancelled":
                        raise AudioPreparationError("cancelled") from error
                    if isinstance(error, ProjectError) and error.code in {"evidence_scope_changed", "evidence_disk_full"}:
                        raise
                    result.update(error_code=getattr(error, "code", "evidence_decode_failed"), frames=[])
                finally:
                    for path in paths:
                        path.unlink(missing_ok=True)
                results.append(result)
                self._checkpoint(project_id, run_id, len(results)-1, result)
            if cancel.is_set():
                raise AudioPreparationError("cancelled")
            self._current(project_id, scope)
            state, error_code = "succeeded", None
        except Exception as error:
            state = "interrupted" if self._closed else "cancelled" if cancel.is_set() else "failed"
            error_code = getattr(error, "code", "evidence_failed")
        with self.projects.connect(project_id) as db:
            db.execute("UPDATE evidence_runs SET state=?,error_code=? WHERE run_id=?", (state, error_code, run_id))

    def image(self, project_id, run_id, artifact_id):
        with self.projects.connect(project_id) as db:
            if not db.execute("SELECT 1 FROM evidence_images WHERE run_id=? AND artifact_id=?", (run_id, artifact_id)).fetchone():
                raise fail("evidence_image_not_found")
        record = self.artifacts.get(project_id, artifact_id)
        if record.recipe_id != "evidence.frame.v1" or record.size_bytes > 2 * 1024**2:
            raise fail("evidence_image_invalid")
        return self.artifacts.path(record)
