"""A bounded, non-interactive Codex CLI adapter.

The adapter deliberately knows nothing about projects or source-media paths. A
Controller must first materialize the exact approved inputs into an isolated
attempt workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Mapping, Protocol
import uuid

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .recovery import (
    AttemptReceiptError,
    AttemptReceiptStore,
    ProviderAttemptReceipt,
    ProviderAttemptState,
    ProviderArtifactDecision,
    ProviderArtifactDisposition,
    ProviderArtifactRole,
    ProviderUsage,
    sha256_bytes,
    sha256_file,
    utc_now,
)


_MAX_PROMPT_BYTES = 1_000_000
_MAX_SCHEMA_BYTES = 1_000_000
_MAX_CAPTURE_BYTES = 2_000_000
_MAX_RESULT_BYTES = 2_000_000
_POLL_INTERVAL_SECONDS = 0.05
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PERMISSION_PROFILE = "clipen_attempt"
_CONFINEMENT_MARKER = b"clipen-confinement-ok\n"
_CONFINEMENT_SCRIPT = """
if ! /bin/cat "$1" >/dev/null 2>&1; then exit 10; fi
if /bin/cat "$2" >/dev/null 2>&1; then exit 20; fi
if /bin/cat "$3" >/dev/null 2>&1; then exit 30; fi
/bin/echo clipen-confinement-ok
""".strip()
_SECRET_PATTERNS = (
    re.compile(rb"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"),
    re.compile(rb"(?i)(api[_-]?key\s*[=:]\s*)[^\s\"']+"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{16,}\b"),
)
_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "ALL_PROXY",
        "CODEX_HOME",
        "HOME",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "SHELL",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
    }
)


class ProviderUnavailable(RuntimeError):
    """The selected provider cannot be launched on this machine."""


class ProviderProtocolError(RuntimeError):
    """The provider violated the expected one-shot result contract."""


class ProviderTimedOut(RuntimeError):
    """The provider exceeded the bounded attempt duration."""


class ProviderCancelled(RuntimeError):
    """The caller cancelled the provider attempt."""


class ProviderAuthenticationError(ProviderUnavailable):
    """The provider login is missing or no longer valid."""


class ProviderUsageLimitError(RuntimeError):
    """The provider rejected the attempt because of an account limit."""


class ProviderNetworkError(RuntimeError):
    """The provider could not reach its service."""


class ProviderPermissionError(RuntimeError):
    """The provider sandbox or permission contract was rejected."""


class CancellationToken(Protocol):
    """Minimal cancellation contract accepted by the adapter."""

    def is_set(self) -> bool: ...


def _bounded_identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a safe bounded identifier")


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _isolated_workspace(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("attempt workspace cannot be a symbolic link")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("attempt workspace must be a directory")
    return resolved


def _workspace_file(path: Path, workspace: Path, field: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{field} cannot be a symbolic link")
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not _inside(resolved, workspace):
        raise ValueError(f"{field} must be a regular file inside the attempt workspace")
    return resolved


def _provider_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {key: value for key, value in inherited.items() if key in _ENVIRONMENT_ALLOWLIST}
    environment["CLICOLOR"] = "0"
    environment["NO_COLOR"] = "1"
    return environment


def _decode_capture(value: bytes, field: str, limit: int = _MAX_CAPTURE_BYTES) -> str:
    if len(value) > limit:
        raise ProviderProtocolError(f"Codex {field} exceeded the capture limit")
    try:
        return value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ProviderProtocolError(f"Codex {field} was not valid UTF-8") from error


def _load_output_schema(path: Path) -> dict[str, object]:
    if path.stat().st_size > _MAX_SCHEMA_BYTES:
        raise ValueError("output_schema must be at most 1,000,000 bytes")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("output_schema must contain valid UTF-8 JSON") from error
    if not isinstance(parsed, dict):
        raise ValueError("output_schema must contain a JSON object")
    try:
        Draft202012Validator.check_schema(parsed)
    except SchemaError as error:
        raise ValueError("output_schema must be a valid JSON Schema 2020-12 document") from error
    return parsed


def _toml_string(value: str) -> str:
    """Encode a string using the TOML-compatible JSON basic-string subset."""

    return json.dumps(value, ensure_ascii=True)


def _permission_config(workspace: Path, temp_root: Path | None = None) -> tuple[str, ...]:
    workspace_key = _toml_string(str(workspace))
    denied_roots = {"/private/tmp/**", "/tmp/**", "/var/tmp/**"}
    if temp_root is not None:
        denied_roots.add(f"{temp_root}/**")
    filesystem_entries = [
        f'{_toml_string(":minimal")}="read"',
        f'{_toml_string(":workspace_roots")}="read"',
    ]
    filesystem_entries.extend(
        f'{_toml_string(path)}="deny"' for path in sorted(denied_roots)
    )
    return (
        "-c",
        f'default_permissions={_toml_string(_PERMISSION_PROFILE)}',
        "-c",
        f'permissions.{_PERMISSION_PROFILE}.description="Clipen isolated AI attempt"',
        "-c",
        f"permissions.{_PERMISSION_PROFILE}.workspace_roots={{{workspace_key}=true}}",
        "-c",
        f"permissions.{_PERMISSION_PROFILE}.filesystem={{{','.join(filesystem_entries)}}}",
        "-c",
        f"permissions.{_PERMISSION_PROFILE}.network.enabled=false",
        "-c",
        'shell_environment_policy.inherit="none"',
        "-c",
        'shell_environment_policy.set={PATH="/usr/bin:/bin",LANG="C"}',
    )


@dataclass(frozen=True)
class CodexCapabilities:
    """CLI features Clipen requires before offering Codex in the UI."""

    ephemeral_sessions: bool
    isolated_user_config: bool
    isolated_rules: bool
    granular_permissions: bool
    json_events: bool
    output_schema: bool
    output_file: bool

    @property
    def supports_bounded_attempts(self) -> bool:
        return all(
            (
                self.ephemeral_sessions,
                self.isolated_user_config,
                self.isolated_rules,
                self.granular_permissions,
                self.json_events,
                self.output_schema,
                self.output_file,
            )
        )


@dataclass(frozen=True)
class CodexProbeResult:
    """Read-only availability snapshot; probing never invokes a model."""

    version: str
    authenticated: bool
    capabilities: CodexCapabilities
    confinement_verified: bool

    @property
    def available(self) -> bool:
        return (
            self.authenticated
            and self.capabilities.supports_bounded_attempts
            and self.confinement_verified
        )


@dataclass(frozen=True)
class CodexAttempt:
    attempt_id: str
    workspace: Path
    output_schema: Path
    prompt: str
    timeout_seconds: float = 300.0
    images: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        _bounded_identifier(self.attempt_id, "attempt_id")
        workspace = _isolated_workspace(self.workspace)
        schema = _workspace_file(self.output_schema, workspace, "output_schema")
        _load_output_schema(schema)
        prompt_bytes = self.prompt.encode("utf-8")
        if not self.prompt.strip() or len(prompt_bytes) > _MAX_PROMPT_BYTES:
            raise ValueError("prompt must be non-empty and at most 1,000,000 UTF-8 bytes")
        if not 0 < self.timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be between zero and 3600")
        self.image_identity()

    def image_identity(self) -> list[dict[str, str]]:
        if type(self.images) is not tuple or len(self.images) > 8:
            raise ValueError("at most eight immutable image inputs are allowed")
        identity = []
        total = 0
        for image in self.images:
            path = _workspace_file(image, self.workspace.resolve(), "image")
            info = path.stat()
            total += info.st_size
            if info.st_nlink != 1 or info.st_size > 2 * 1024**2 or total > 8 * 1024**2:
                raise ValueError("image input exceeds bounds or is hardlinked")
            with path.open("rb") as source:
                if source.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise ValueError("image input must be a PNG snapshot")
            identity.append({"name": str(path.relative_to(self.workspace.resolve())),
                             "sha256": sha256_file(path, 2 * 1024**2)})
        return identity

    def input_hash(self) -> str:
        if not self.images:
            return sha256_bytes(self.prompt.encode("utf-8"))
        return sha256_bytes(json.dumps({"prompt": self.prompt, "images": self.image_identity()},
                                      sort_keys=True).encode("utf-8"))


@dataclass(frozen=True)
class CodexAttemptResult:
    attempt_id: str
    payload: dict[str, object]
    event_types: tuple[str, ...]
    elapsed_seconds: float | None
    usage: ProviderUsage | None = None


@dataclass(frozen=True)
class CodexReconciliation:
    attempt_id: str
    state: ProviderAttemptState
    result: CodexAttemptResult | None
    reason: str


class CodexExecAdapter:
    """Run one ephemeral Codex attempt without loading project configuration."""

    def __init__(
        self,
        attempt_root: Path,
        binary: str | Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._attempt_root = _isolated_workspace(attempt_root)
        selected = str(binary) if binary is not None else shutil.which("codex")
        if not selected:
            raise ProviderUnavailable("Codex CLI is not installed or not on PATH")
        candidate = Path(selected)
        if candidate.is_symlink():
            candidate = candidate.resolve(strict=True)
        else:
            candidate = candidate.resolve(strict=True)
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ProviderUnavailable("Codex CLI is not an executable regular file")
        self._binary = candidate
        self._environment = _provider_environment(environment)

    @property
    def binary(self) -> Path:
        return self._binary

    def version(self) -> str:
        completed = self._probe_command("--version")
        if completed.returncode != 0:
            raise ProviderUnavailable("Codex CLI version probe failed")
        version = _decode_capture(completed.stdout, "version output", 256).strip()
        if not version or len(version) > 256:
            raise ProviderProtocolError("Codex CLI returned an invalid version string")
        return version

    def capabilities(self) -> CodexCapabilities:
        exec_help = self._probe_command("exec", "--help")
        sandbox_help = self._probe_command("sandbox", "--help")
        if exec_help.returncode != 0 or sandbox_help.returncode != 0:
            raise ProviderUnavailable("Codex CLI capability probe failed")
        help_text = _decode_capture(exec_help.stdout, "exec help")
        sandbox_help_text = _decode_capture(sandbox_help.stdout, "sandbox help")
        return CodexCapabilities(
            ephemeral_sessions="--ephemeral" in help_text,
            isolated_user_config="--ignore-user-config" in help_text,
            isolated_rules="--ignore-rules" in help_text,
            granular_permissions=(
                "--config" in help_text
                and "--strict-config" in help_text
                and "--permission-profile" in sandbox_help_text
            ),
            json_events="--json" in help_text,
            output_schema="--output-schema" in help_text,
            output_file="--output-last-message" in help_text,
        )

    def probe(self) -> CodexProbeResult:
        """Check the local CLI, auth, and contract without contacting a model."""

        version = self.version()
        capabilities = self.capabilities()
        login = self._probe_command("login", "status")
        confinement_verified = False
        if login.returncode == 0 and capabilities.granular_permissions:
            confinement_verified = self._verify_confinement()
        return CodexProbeResult(
            version=version,
            authenticated=login.returncode == 0,
            capabilities=capabilities,
            confinement_verified=confinement_verified,
        )

    def _verify_confinement(self) -> bool:
        """Run a model-free canary under the exact filesystem permission profile."""

        try:
            with (
                tempfile.TemporaryDirectory(prefix=".permission-probe-", dir=self._attempt_root)
                as probe_directory,
                tempfile.TemporaryDirectory(prefix="clipen-denied-") as denied_directory,
            ):
                probe_root = Path(probe_directory)
                workspace = probe_root / "attempt"
                probe_home = probe_root / "codex-home"
                workspace.mkdir(mode=0o700)
                probe_home.mkdir(mode=0o700)
                inside = workspace / "inside.txt"
                outside = probe_root / "outside.txt"
                temp_outside = Path(denied_directory) / "outside.txt"
                inside.write_text("inside-canary", encoding="utf-8")
                outside.write_text("outside-canary", encoding="utf-8")
                temp_outside.write_text("temp-canary", encoding="utf-8")
                command = (
                    str(self._binary),
                    "sandbox",
                    *_permission_config(workspace, Path(tempfile.gettempdir()).resolve()),
                    "--permission-profile",
                    _PERMISSION_PROFILE,
                    "--cd",
                    str(workspace),
                    "--",
                    "/bin/sh",
                    "-c",
                    _CONFINEMENT_SCRIPT,
                    "clipen-canary",
                    str(inside),
                    str(outside),
                    str(temp_outside),
                )
                completed = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**self._environment, "CODEX_HOME": str(probe_home)},
                    check=False,
                    timeout=15,
                )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0 and completed.stdout == _CONFINEMENT_MARKER

    def _probe_command(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                [str(self._binary), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._environment,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProviderUnavailable("Codex CLI probe could not complete") from error
        if len(completed.stdout) > _MAX_CAPTURE_BYTES or len(completed.stderr) > _MAX_CAPTURE_BYTES:
            raise ProviderProtocolError("Codex CLI probe output exceeded the capture limit")
        return completed

    def command(self, attempt: CodexAttempt, output_path: Path) -> tuple[str, ...]:
        workspace = _isolated_workspace(attempt.workspace)
        if workspace == self._attempt_root or not _inside(workspace, self._attempt_root):
            raise ValueError("attempt workspace must be a child of the managed attempt root")
        schema = _workspace_file(attempt.output_schema, workspace, "output_schema")
        if output_path.is_symlink():
            raise ValueError("provider output cannot be a symbolic link")
        resolved_output = output_path.resolve(strict=False)
        if not _inside(resolved_output, workspace) or resolved_output.exists():
            raise ValueError("provider output must be a new file inside the attempt workspace")
        return (
            str(self._binary),
            "--ask-for-approval",
            "never",
            *_permission_config(workspace, Path(tempfile.gettempdir()).resolve()),
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(resolved_output),
            *(part for image in attempt.images for part in ("--image", str(image.resolve()))),
            "--cd",
            str(workspace),
            "-",
        )

    def execute(
        self,
        attempt: CodexAttempt,
        cancel_token: CancellationToken | None = None,
    ) -> CodexAttemptResult:
        if cancel_token is not None and cancel_token.is_set():
            raise ProviderCancelled(f"Codex attempt {attempt.attempt_id} was cancelled")
        workspace = _isolated_workspace(attempt.workspace)
        output_path = workspace / f"provider-output-{attempt.attempt_id}.json"
        events_path = workspace / f"provider-events-{attempt.attempt_id}.jsonl"
        diagnostics_path = workspace / f"provider-diagnostics-{attempt.attempt_id}.log"
        store = AttemptReceiptStore(workspace, attempt.attempt_id)
        if store.path.exists() or store.path.is_symlink():
            raise ProviderProtocolError(
                "Codex attempt already has a receipt; reconcile it instead of rerunning"
            )
        for artifact in (output_path, events_path, diagnostics_path):
            if artifact.exists() or artifact.is_symlink():
                raise ProviderProtocolError("Codex attempt artifact already exists")
        prompt_hash = attempt.input_hash()
        schema_hash = sha256_file(attempt.output_schema, _MAX_SCHEMA_BYTES)
        receipt = ProviderAttemptReceipt(
            attempt_id=attempt.attempt_id,
            state=ProviderAttemptState.PREPARED,
            prompt_sha256=prompt_hash,
            schema_sha256=schema_hash,
        )
        store.write(receipt)

        def record_started(pid: int) -> None:
            nonlocal receipt
            receipt = replace(
                receipt,
                state=ProviderAttemptState.RUNNING,
                started_at=utc_now(),
                pid=pid,
            )
            store.write(receipt)

        command = self.command(attempt, output_path)
        started = time.monotonic()
        returncode: int | None = None
        try:
            stdout, stderr, returncode = self._run_bounded(
                command,
                attempt,
                workspace,
                cancel_token,
                events_path,
                diagnostics_path,
                record_started,
            )
            self._sanitize_persisted_artifacts(
                events_path, diagnostics_path, attempt.prompt
            )
            stdout = self._read_artifact(events_path, _MAX_CAPTURE_BYTES)
            stderr = self._read_artifact(diagnostics_path, _MAX_CAPTURE_BYTES)
            if returncode != 0:
                self._raise_for_exit(returncode, stderr)
            result = self._validated_result(attempt, output_path, stdout)
            if attempt.input_hash() != prompt_hash:
                raise ProviderProtocolError("approved image snapshots changed during the attempt")
            receipt = replace(
                receipt,
                state=ProviderAttemptState.SUCCEEDED,
                finished_at=utc_now(),
                return_code=returncode,
                output_sha256=sha256_file(output_path, _MAX_RESULT_BYTES),
                events_sha256=sha256_file(events_path, _MAX_CAPTURE_BYTES),
                diagnostics_sha256=sha256_file(
                    diagnostics_path, _MAX_CAPTURE_BYTES
                ),
                usage=result.usage,
            )
            store.write(receipt)
            return replace(result, elapsed_seconds=time.monotonic() - started)
        except Exception as error:
            self._sanitize_persisted_artifacts(
                events_path, diagnostics_path, attempt.prompt
            )
            state, error_code = self._failure_receipt(error)
            receipt = replace(
                receipt,
                state=state,
                finished_at=utc_now(),
                return_code=returncode,
                output_sha256=self._optional_artifact_hash(
                    output_path, _MAX_RESULT_BYTES
                ),
                events_sha256=self._optional_artifact_hash(
                    events_path, _MAX_CAPTURE_BYTES
                ),
                diagnostics_sha256=self._optional_artifact_hash(
                    diagnostics_path, _MAX_CAPTURE_BYTES
                ),
                usage=self._optional_usage(events_path),
                error_code=error_code,
            )
            try:
                store.write(receipt)
            except AttemptReceiptError as receipt_error:
                raise ProviderProtocolError(
                    "Codex attempt failed and its terminal receipt could not be saved"
                ) from receipt_error
            raise

    def reconcile(self, attempt: CodexAttempt) -> CodexReconciliation:
        workspace = _isolated_workspace(attempt.workspace)
        store = AttemptReceiptStore(workspace, attempt.attempt_id)
        try:
            receipt = store.read()
        except AttemptReceiptError:
            return CodexReconciliation(
                attempt.attempt_id,
                ProviderAttemptState.UNKNOWN,
                None,
                "receipt_unavailable",
            )
        try:
            if (
                receipt.attempt_id != attempt.attempt_id
                or receipt.prompt_sha256
                != attempt.input_hash()
                or receipt.schema_sha256
                != sha256_file(attempt.output_schema, _MAX_SCHEMA_BYTES)
            ):
                raise AttemptReceiptError("attempt identity does not match its receipt")
            output_path = workspace / f"provider-output-{attempt.attempt_id}.json"
            events_path = workspace / f"provider-events-{attempt.attempt_id}.jsonl"
            if receipt.state in {
                ProviderAttemptState.SUCCEEDED,
                ProviderAttemptState.RUNNING,
            }:
                stdout = self._read_artifact(events_path, _MAX_CAPTURE_BYTES)
                result = self._validated_result(attempt, output_path, stdout)
                output_hash = sha256_file(output_path, _MAX_RESULT_BYTES)
                events_hash = sha256_file(events_path, _MAX_CAPTURE_BYTES)
                if receipt.state is ProviderAttemptState.SUCCEEDED:
                    if (
                        receipt.output_sha256 != output_hash
                        or receipt.events_sha256 != events_hash
                    ):
                        raise AttemptReceiptError("terminal provider artifacts changed")
                    self._verify_recorded_hash(
                        workspace
                        / f"provider-diagnostics-{attempt.attempt_id}.log",
                        receipt.diagnostics_sha256,
                        _MAX_CAPTURE_BYTES,
                    )
                else:
                    diagnostics_path = workspace / (
                        f"provider-diagnostics-{attempt.attempt_id}.log"
                    )
                    receipt = replace(
                        receipt,
                        state=ProviderAttemptState.SUCCEEDED,
                        finished_at=utc_now(),
                        return_code=0,
                        output_sha256=output_hash,
                        events_sha256=events_hash,
                        diagnostics_sha256=self._optional_artifact_hash(
                            diagnostics_path, _MAX_CAPTURE_BYTES
                        ),
                        usage=result.usage,
                        error_code=None,
                    )
                    store.write(receipt)
                return CodexReconciliation(
                    attempt.attempt_id,
                    ProviderAttemptState.SUCCEEDED,
                    result,
                    "terminal_artifacts_verified",
                )
            if receipt.state is ProviderAttemptState.PREPARED:
                interrupted = replace(
                    receipt,
                    state=ProviderAttemptState.INTERRUPTED,
                    finished_at=utc_now(),
                    error_code="launch_interrupted",
                )
                store.write(interrupted)
                return CodexReconciliation(
                    attempt.attempt_id,
                    ProviderAttemptState.INTERRUPTED,
                    None,
                    "launch_interrupted",
                )
            self._verify_recorded_hash(
                workspace / f"provider-output-{attempt.attempt_id}.json",
                receipt.output_sha256,
                _MAX_RESULT_BYTES,
            )
            self._verify_recorded_hash(
                workspace / f"provider-events-{attempt.attempt_id}.jsonl",
                receipt.events_sha256,
                _MAX_CAPTURE_BYTES,
            )
            self._verify_recorded_hash(
                workspace / f"provider-diagnostics-{attempt.attempt_id}.log",
                receipt.diagnostics_sha256,
                _MAX_CAPTURE_BYTES,
            )
            return CodexReconciliation(
                attempt.attempt_id,
                receipt.state,
                None,
                receipt.error_code or "terminal_receipt_verified",
            )
        except (AttemptReceiptError, OSError, ValueError, ProviderProtocolError):
            return CodexReconciliation(
                attempt.attempt_id,
                ProviderAttemptState.UNKNOWN,
                None,
                "terminal_state_unverified",
            )

    def artifact_inventory(
        self, attempt: CodexAttempt
    ) -> tuple[ProviderArtifactDecision, ...]:
        workspace = _isolated_workspace(attempt.workspace)
        store = AttemptReceiptStore(workspace, attempt.attempt_id)
        try:
            state = store.read().state
        except AttemptReceiptError:
            state = ProviderAttemptState.UNKNOWN

        terminal_failure = state in {
            ProviderAttemptState.FAILED,
            ProviderAttemptState.CANCELLED,
            ProviderAttemptState.TIMED_OUT,
            ProviderAttemptState.INTERRUPTED,
        }
        pending = state in {
            ProviderAttemptState.PREPARED,
            ProviderAttemptState.RUNNING,
            ProviderAttemptState.UNKNOWN,
        }
        if state is ProviderAttemptState.SUCCEEDED:
            schema_disposition = ProviderArtifactDisposition.RETAIN_RESULT
            output_disposition = ProviderArtifactDisposition.RETAIN_RESULT
            events_disposition = ProviderArtifactDisposition.RETAIN_RESULT
            diagnostics_disposition = ProviderArtifactDisposition.CLEANABLE
        elif pending:
            schema_disposition = ProviderArtifactDisposition.RETAIN_RECOVERY
            output_disposition = ProviderArtifactDisposition.RETAIN_RECOVERY
            events_disposition = ProviderArtifactDisposition.RETAIN_RECOVERY
            diagnostics_disposition = ProviderArtifactDisposition.RETAIN_RECOVERY
        elif terminal_failure:
            schema_disposition = ProviderArtifactDisposition.CLEANABLE
            output_disposition = ProviderArtifactDisposition.CLEANABLE
            events_disposition = ProviderArtifactDisposition.RETAIN_DIAGNOSTIC
            diagnostics_disposition = ProviderArtifactDisposition.RETAIN_DIAGNOSTIC
        else:
            raise AssertionError("unhandled provider attempt state")

        decisions = (
            ProviderArtifactDecision(
                ProviderArtifactRole.RECEIPT,
                store.path.name,
                ProviderArtifactDisposition.RETAIN_RECOVERY,
                "authoritative terminal or recovery receipt",
            ),
            ProviderArtifactDecision(
                ProviderArtifactRole.OUTPUT_SCHEMA,
                attempt.output_schema.name,
                schema_disposition,
                "schema required for validation and lineage",
            ),
            ProviderArtifactDecision(
                ProviderArtifactRole.OUTPUT,
                f"provider-output-{attempt.attempt_id}.json",
                output_disposition,
                "candidate result or incomplete output",
            ),
            ProviderArtifactDecision(
                ProviderArtifactRole.EVENTS,
                f"provider-events-{attempt.attempt_id}.jsonl",
                events_disposition,
                "terminal evidence and usage",
            ),
            ProviderArtifactDecision(
                ProviderArtifactRole.DIAGNOSTICS,
                f"provider-diagnostics-{attempt.attempt_id}.log",
                diagnostics_disposition,
                "redacted diagnostics",
            ),
        )
        return tuple(
            decision
            for decision in decisions
            if (workspace / decision.filename).is_file()
            and not (workspace / decision.filename).is_symlink()
        )

    def _validated_result(
        self, attempt: CodexAttempt, output_path: Path, stdout: bytes
    ) -> CodexAttemptResult:
        if not output_path.is_file() or output_path.is_symlink():
            raise ProviderProtocolError("Codex did not create the expected result file")
        if output_path.stat().st_size > _MAX_RESULT_BYTES:
            raise ProviderProtocolError("Codex result exceeded the size limit")
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ProviderProtocolError("Codex result was not valid UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise ProviderProtocolError("Codex result must be a JSON object")
        schema = _load_output_schema(
            _workspace_file(
                attempt.output_schema,
                _isolated_workspace(attempt.workspace),
                "output_schema",
            )
        )
        try:
            Draft202012Validator(schema).validate(payload)
        except ValidationError as error:
            raise ProviderProtocolError("Codex result did not match the output schema") from error
        event_types, usage = self._events_and_usage(stdout)
        if "turn.completed" not in event_types:
            raise ProviderProtocolError("Codex event stream did not report a completed turn")
        return CodexAttemptResult(
            attempt_id=attempt.attempt_id,
            payload=payload,
            event_types=event_types,
            elapsed_seconds=None,
            usage=usage,
        )

    @staticmethod
    def _raise_for_exit(returncode: int, stderr: bytes) -> None:
        diagnostic = _decode_capture(stderr, "stderr").lower()
        if any(marker in diagnostic for marker in ("not logged in", "unauthorized", "login required")):
            raise ProviderAuthenticationError("Codex login is missing or no longer valid")
        if any(marker in diagnostic for marker in ("usage limit", "rate limit", "quota", "credits")):
            raise ProviderUsageLimitError("Codex account usage limit prevented this attempt")
        if any(
            marker in diagnostic
            for marker in ("connection failed", "network is unreachable", "could not resolve host")
        ):
            raise ProviderNetworkError("Codex could not reach its service")
        if any(marker in diagnostic for marker in ("sandbox", "operation not permitted", "permission")):
            raise ProviderPermissionError("Codex permission confinement failed")
        raise ProviderProtocolError(f"Codex attempt exited with code {returncode}")

    def _run_bounded(
        self,
        command: tuple[str, ...],
        attempt: CodexAttempt,
        workspace: Path,
        cancel_token: CancellationToken | None,
        events_path: Path,
        diagnostics_path: Path,
        record_started,
    ) -> tuple[bytes, bytes, int]:
        deadline = time.monotonic() + attempt.timeout_seconds
        with tempfile.TemporaryFile() as prompt_stream:
            prompt_stream.write(attempt.prompt.encode("utf-8"))
            prompt_stream.seek(0)
            stdout_descriptor = os.open(
                events_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                stderr_descriptor = os.open(
                    diagnostics_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
            except Exception:
                os.close(stdout_descriptor)
                raise
            with (
                os.fdopen(stdout_descriptor, "wb") as stdout_stream,
                os.fdopen(stderr_descriptor, "wb") as stderr_stream,
            ):
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=workspace,
                        env=self._environment,
                        stdin=prompt_stream,
                        stdout=stdout_stream,
                        stderr=stderr_stream,
                        start_new_session=os.name == "posix",
                    )
                except OSError as error:
                    raise ProviderUnavailable("Codex CLI could not be launched") from error
                record_started(process.pid)

                while process.poll() is None:
                    if cancel_token is not None and cancel_token.is_set():
                        self._terminate(process)
                        raise ProviderCancelled(
                            f"Codex attempt {attempt.attempt_id} was cancelled"
                        )
                    if time.monotonic() >= deadline:
                        self._terminate(process)
                        raise ProviderTimedOut(
                            f"Codex attempt {attempt.attempt_id} exceeded its timeout"
                        )
                    if (
                        os.fstat(stdout_stream.fileno()).st_size > _MAX_CAPTURE_BYTES
                        or os.fstat(stderr_stream.fileno()).st_size > _MAX_CAPTURE_BYTES
                    ):
                        self._terminate(process)
                        raise ProviderProtocolError(
                            "Codex diagnostic output exceeded the capture limit"
                        )
                    time.sleep(_POLL_INTERVAL_SECONDS)

                stdout_stream.flush()
                stderr_stream.flush()
            stdout = self._read_artifact(events_path, _MAX_CAPTURE_BYTES)
            stderr = self._read_artifact(diagnostics_path, _MAX_CAPTURE_BYTES)
            return stdout, stderr, process.returncode

    @staticmethod
    def _events_and_usage(
        stdout: bytes,
    ) -> tuple[tuple[str, ...], ProviderUsage | None]:
        event_types: list[str] = []
        usage: ProviderUsage | None = None
        lines = _decode_capture(stdout, "JSONL stream").splitlines()
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise ProviderProtocolError("Codex JSONL stream contained invalid JSON") from error
            if not isinstance(event, dict):
                raise ProviderProtocolError("Codex JSONL events must be objects")
            event_type = event.get("type")
            if isinstance(event_type, str) and event_type:
                event_types.append(event_type)
            candidate_usage = event.get("usage")
            if event_type in {"turn.completed", "turn.failed"} and isinstance(
                candidate_usage, dict
            ):
                try:
                    usage = ProviderUsage(
                        input_tokens=candidate_usage["input_tokens"],
                        cached_input_tokens=candidate_usage.get(
                            "cached_input_tokens", 0
                        ),
                        output_tokens=candidate_usage["output_tokens"],
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise ProviderProtocolError(
                        "Codex usage data was malformed"
                    ) from error
        return tuple(event_types), usage

    @classmethod
    def _optional_usage(cls, path: Path) -> ProviderUsage | None:
        try:
            _, usage = cls._events_and_usage(
                cls._read_artifact(path, _MAX_CAPTURE_BYTES)
            )
            return usage
        except (OSError, ProviderProtocolError):
            return None

    @staticmethod
    def _read_artifact(path: Path, limit: int) -> bytes:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ProviderProtocolError("Codex attempt artifact is missing or oversized")
        value = path.read_bytes()
        if len(value) > limit:
            raise ProviderProtocolError("Codex attempt artifact exceeded its size limit")
        return value

    @staticmethod
    def _optional_artifact_hash(path: Path, limit: int) -> str | None:
        try:
            return sha256_file(path, limit)
        except (AttemptReceiptError, OSError):
            return None

    @staticmethod
    def _sanitize_persisted_artifacts(
        events_path: Path, diagnostics_path: Path, prompt: str
    ) -> None:
        prompt_bytes = prompt.encode("utf-8")
        for path in (events_path, diagnostics_path):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                value = path.read_bytes()
            except OSError:
                continue
            sanitized = value.replace(prompt_bytes, b"[REDACTED_PROMPT]")
            for pattern in _SECRET_PATTERNS:
                if pattern.groups:
                    sanitized = pattern.sub(rb"\1[REDACTED_SECRET]", sanitized)
                else:
                    sanitized = pattern.sub(b"[REDACTED_SECRET]", sanitized)
            if sanitized == value:
                continue
            temporary = path.with_name(f".{path.name}.redacted-{uuid.uuid4().hex}")
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(sanitized)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                os.chmod(path, 0o600)
            finally:
                if temporary.exists() and not temporary.is_symlink():
                    temporary.unlink()

    @staticmethod
    def _verify_recorded_hash(
        path: Path, expected: str | None, limit: int
    ) -> None:
        if expected is None:
            return
        if sha256_file(path, limit) != expected:
            raise AttemptReceiptError("provider artifact changed after receipt")

    @staticmethod
    def _failure_receipt(
        error: Exception,
    ) -> tuple[ProviderAttemptState, str]:
        if isinstance(error, ProviderCancelled):
            return ProviderAttemptState.CANCELLED, "cancelled"
        if isinstance(error, ProviderTimedOut):
            return ProviderAttemptState.TIMED_OUT, "timed_out"
        if isinstance(error, ProviderAuthenticationError):
            return ProviderAttemptState.FAILED, "authentication"
        if isinstance(error, ProviderUsageLimitError):
            return ProviderAttemptState.FAILED, "usage_limit"
        if isinstance(error, ProviderNetworkError):
            return ProviderAttemptState.FAILED, "network"
        if isinstance(error, ProviderPermissionError):
            return ProviderAttemptState.FAILED, "permission"
        if isinstance(error, ProviderUnavailable):
            return ProviderAttemptState.FAILED, "unavailable"
        return ProviderAttemptState.FAILED, "protocol"

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                process.wait(timeout=2)
                return
            except PermissionError:
                process.terminate()
        else:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    process.kill()
            else:
                process.kill()
            process.wait(timeout=2)
