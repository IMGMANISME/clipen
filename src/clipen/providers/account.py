"""Read-only account details for the status bar: which model runs and how much quota is left.

Nothing here invokes a model or spends quota. Codex answers live through its
local app server; Claude Code has no such query, so its details come from the
last Clipen attempt and say so.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Mapping

_SNAPSHOT = "claude-account.json"
_WINDOWS = {300: "five_hour", 10080: "weekly"}


def _rpc(
    binary: str, environment: Mapping[str, str], requests: list[tuple[str, dict | None]], timeout: float
) -> dict[int, dict]:
    """Ask `codex app-server` a few JSON-RPC questions and return the answers by request id."""
    process = subprocess.Popen(
        [binary, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(environment),
        text=True,
        bufsize=1,
    )
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
    return {
        "kind": _WINDOWS.get(minutes, "other"),
        "minutes": minutes if isinstance(minutes, int) else None,
        "used_percent": max(0, min(100, round(used))),
        "resets_at": resets if isinstance(resets, int) else None,
    }


_MODELS: dict[str, tuple[float, list[dict]]] = {}
_MODELS_LOCK = threading.Lock()


def _models(data: object) -> list[dict]:
    if not isinstance(data, list):
        return []
    return [
        {
            "model": m.get("model") or m.get("id"),
            "name": m.get("displayName") or m.get("model"),
            "default": bool(m.get("isDefault")),
            "default_effort": m.get("defaultReasoningEffort"),
            "efforts": [
                e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts") or [] if isinstance(e, dict)
            ],
        }
        for m in data
        if isinstance(m, dict) and isinstance(m.get("model") or m.get("id"), str)
    ]


def available_models(binary: str | Path, environment: Mapping[str, str], ttl: float = 600.0) -> list[dict]:
    """Models this Codex account can run, cached for a few minutes."""
    key = str(binary)
    with _MODELS_LOCK:
        cached = _MODELS.get(key)
        if cached and time.monotonic() - cached[0] < ttl:
            return cached[1]
    models = _models((_rpc(key, environment, [("model/list", {})], 10.0).get(1) or {}).get("data"))
    with _MODELS_LOCK:
        # An empty answer is kept briefly too, so a slow app server delays one run, not each one.
        _MODELS[key] = (time.monotonic() - (ttl - 60 if not models else 0), models)
    return models


def resolve_codex_model(preferences: tuple[str | None, str | None], models: list[dict]) -> dict | None:
    """The model and effort a run will use: the user's choice when the account offers it, else the default."""
    if not models:
        return None
    wanted, effort = preferences
    default = next((m for m in models if m["default"]), models[0])
    chosen = next((m for m in models if m["model"] == wanted), None)
    model = chosen or default
    applied_effort = effort if effort and effort in model["efforts"] else None
    return {
        "id": model["model"],
        "name": model["name"],
        "effort": applied_effort or model["default_effort"],
        "apply_model": chosen is not None,
        "apply_effort": applied_effort is not None,
        "source": "codex_config" if chosen is not None or applied_effort else "default",
        "unavailable": wanted if wanted and chosen is None else None,
    }


def codex_account(binary: str | Path, environment: Mapping[str, str], timeout: float = 10.0) -> dict:
    """The model Clipen runs (see resolve_codex_model) and live usage limits."""
    from .codex import codex_preferences

    answers = _rpc(str(binary), environment, [("account/rateLimits/read", None), ("model/list", {})], timeout)
    limits = (answers.get(1) or {}).get("rateLimits")
    resolved = resolve_codex_model(codex_preferences(environment), _models((answers.get(2) or {}).get("data")))
    model = (
        None if resolved is None else {key: resolved[key] for key in ("id", "name", "effort", "source", "unavailable")}
    )
    quota = None
    if isinstance(limits, dict):
        windows = [w for w in (_window(limits.get("primary")), _window(limits.get("secondary"))) if w]
        quota = {
            "source": "live",
            "plan": limits.get("planType") if isinstance(limits.get("planType"), str) else None,
            "windows": windows,
            "limited": bool(limits.get("rateLimitReachedType")),
        }
    return {"model": model, "quota": quota}


def record_claude_account(attempt_root: Path, events: list[dict], requested: str | None = None) -> None:
    """Keep the model and limit status Claude reported during the last attempt."""
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None)
    info = next((e.get("rate_limit_info") for e in reversed(events) if e.get("type") == "rate_limit_event"), None)
    model = init.get("model") if isinstance(init, dict) and isinstance(init.get("model"), str) else None
    if model is None and not isinstance(info, dict):
        return
    snapshot = {"model": model, "requested": requested, "observed_at": int(time.time())}
    if isinstance(info, dict):
        snapshot["limit"] = {
            key: info.get(key)
            for key in ("status", "resetsAt", "rateLimitType")
            if isinstance(info.get(key), (str, int)) and not isinstance(info.get(key), bool)
        }
    path = attempt_root / _SNAPSHOT
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot), encoding="utf-8")
    os.replace(temporary, path)


def _claude_name(model: str) -> str:
    """claude-sonnet-5-20261001 reads as Claude Sonnet 5, and claude-sonnet-5-5 as Claude Sonnet 5.5."""
    words: list[str] = []
    for word in model.split("-"):
        if word.isdigit() and len(word) == 8:
            continue  # a release date
        if word.isdigit() and words and words[-1].replace(".", "").isdigit():
            words[-1] += "." + word  # the parts of one version number
        else:
            words.append(word.capitalize())
    return " ".join(words) or model


def claude_account(attempt_root: Path, environment: Mapping[str, str] | None = None) -> dict:
    """The last run's model and limit status, with the effort and model chosen in Claude Code settings."""
    from .claude import claude_preferences

    wanted, effort = claude_preferences(environment)
    source = "claude_config" if wanted or effort else "default"
    path = attempt_root / _SNAPSHOT
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snapshot, dict):
            raise ValueError
    except (OSError, ValueError):
        snapshot = {}
    model = snapshot.get("model")
    # A model chosen after the last run is what the next run uses.
    if wanted and (not isinstance(model, str) or snapshot.get("requested") != wanted):
        model = wanted
    limit = snapshot.get("limit") if isinstance(snapshot.get("limit"), dict) else {}
    observed = snapshot.get("observed_at") if isinstance(snapshot.get("observed_at"), int) else None
    quota = (
        None
        if not limit
        else {
            "source": "last_run",
            "observed_at": observed,
            "plan": None,
            "windows": [],
            "limited": limit.get("status") not in (None, "allowed", "allowed_warning"),
            "warning": limit.get("status") == "allowed_warning",
            "resets_at": limit.get("resetsAt") if isinstance(limit.get("resetsAt"), int) else None,
            "kind": limit.get("rateLimitType") if isinstance(limit.get("rateLimitType"), str) else None,
        }
    )
    return {
        "model": None
        if not isinstance(model, str)
        else {"id": model, "name": _claude_name(model), "effort": effort, "source": source, "unavailable": None},
        "quota": quota,
    }
