"""Read-only account details for the status bar: which model runs and how much quota is left.

Nothing here invokes a model or spends quota. Codex answers live through its
local app server; Claude Code has no such query, so its details come from the
last Clipen attempt and say so.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Mapping

_SNAPSHOT = "claude-account.json"
_WINDOWS = {300: "five_hour", 10080: "weekly"}


def _rpc(binary: str, environment: Mapping[str, str], requests: list[tuple[str, dict | None]], timeout: float) -> dict[int, dict]:
    """Ask `codex app-server` a few JSON-RPC questions and return the answers by request id."""
    process = subprocess.Popen([binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=dict(environment), text=True, bufsize=1)
    lines: queue.Queue[str | None] = queue.Queue()

    def read() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    answers: dict[int, dict] = {}
    deadline = time.monotonic() + timeout
    try:
        def send(message: dict) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
            process.stdin.flush()

        def wait(identifier: int) -> dict | None:
            while (left := deadline - time.monotonic()) > 0:
                try:
                    line = lines.get(timeout=left)
                except queue.Empty:
                    return None
                if line is None:
                    return None
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") == identifier:
                    return message
            return None

        send({"id": 0, "method": "initialize", "params": {"clientInfo": {"name": "clipen", "version": "1"}}})
        if wait(0) is None:
            return answers
        send({"method": "initialized"})
        for identifier, (method, params) in enumerate(requests, 1):
            send({"id": identifier, "method": method, **({} if params is None else {"params": params})})
            answer = wait(identifier)
            if answer is not None and isinstance(answer.get("result"), dict):
                answers[identifier] = answer["result"]
    except (OSError, ValueError):
        pass
    finally:
        process.kill()
        process.wait(timeout=5)
    return answers


def _window(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    used, minutes, resets = value.get("usedPercent"), value.get("windowDurationMins"), value.get("resetsAt")
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    return {"kind": _WINDOWS.get(minutes, "other"), "minutes": minutes if isinstance(minutes, int) else None,
            "used_percent": max(0, min(100, round(used))), "resets_at": resets if isinstance(resets, int) else None}


def codex_account(binary: str | Path, environment: Mapping[str, str], timeout: float = 10.) -> dict:
    """The default Codex model (Clipen ignores the user's Codex config) and live usage limits."""
    answers = _rpc(str(binary), environment, [("account/rateLimits/read", None), ("model/list", {})], timeout)
    limits = (answers.get(1) or {}).get("rateLimits")
    models = (answers.get(2) or {}).get("data")
    default = next((m for m in models if isinstance(m, dict) and m.get("isDefault")), None) if isinstance(models, list) else None
    model = None if default is None else {"id": default.get("model") or default.get("id"),
                                          "name": default.get("displayName") or default.get("model"),
                                          "effort": default.get("defaultReasoningEffort")}
    quota = None
    if isinstance(limits, dict):
        windows = [w for w in (_window(limits.get("primary")), _window(limits.get("secondary"))) if w]
        quota = {"source": "live", "plan": limits.get("planType") if isinstance(limits.get("planType"), str) else None,
                 "windows": windows, "limited": bool(limits.get("rateLimitReachedType"))}
    return {"model": model, "quota": quota}


def record_claude_account(attempt_root: Path, events: list[dict]) -> None:
    """Keep the model and limit status Claude reported during the last attempt."""
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    info = next((e.get("rate_limit_info") for e in reversed(events) if e.get("type") == "rate_limit_event"), None)
    model = init.get("model") if isinstance(init, dict) and isinstance(init.get("model"), str) else None
    if model is None and not isinstance(info, dict):
        return
    snapshot = {"model": model, "observed_at": int(time.time())}
    if isinstance(info, dict):
        snapshot["limit"] = {key: info.get(key) for key in ("status", "resetsAt", "rateLimitType")
                             if isinstance(info.get(key), (str, int)) and not isinstance(info.get(key), bool)}
    path = attempt_root / _SNAPSHOT
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot), encoding="utf-8")
    os.replace(temporary, path)


def _claude_name(model: str) -> str:
    """claude-sonnet-5-20261001 reads as Claude Sonnet 5."""
    words = [w for w in model.split("-") if not (w.isdigit() and len(w) == 8)]
    return " ".join(w.capitalize() for w in words) or model


def claude_account(attempt_root: Path) -> dict:
    path = attempt_root / _SNAPSHOT
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return {"model": None, "quota": None}
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"model": None, "quota": None}
    model = snapshot.get("model")
    limit = snapshot.get("limit") if isinstance(snapshot.get("limit"), dict) else {}
    observed = snapshot.get("observed_at") if isinstance(snapshot.get("observed_at"), int) else None
    quota = None if not limit else {
        "source": "last_run", "observed_at": observed, "plan": None, "windows": [],
        "limited": limit.get("status") not in (None, "allowed", "allowed_warning"),
        "warning": limit.get("status") == "allowed_warning",
        "resets_at": limit.get("resetsAt") if isinstance(limit.get("resetsAt"), int) else None,
        "kind": limit.get("rateLimitType") if isinstance(limit.get("rateLimitType"), str) else None}
    return {"model": None if not isinstance(model, str) else {"id": model, "name": _claude_name(model), "effort": None,
                                                               "source": "last_run"},
            "quota": quota}
