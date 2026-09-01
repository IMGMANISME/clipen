"""Project use cases coordinating storage with explicitly selected directories."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time

from .models import (
    DirectoryReference,
    DirectoryRole,
    ProjectError,
    ProjectSnapshot,
    validate_request_id,
    validate_revision,
)
from .picker import (
    MacOSDirectoryPicker,
    NativeDirectoryPicker,
    SelectionAuthority,
    refresh_availability,
)
from .storage import ProjectRepository, command_digest


class ProjectWorkspace:
    def __init__(
        self,
        projects_root: Path,
        *,
        picker: NativeDirectoryPicker | None = None,
        selections: SelectionAuthority | None = None,
    ) -> None:
        self.repository = ProjectRepository(projects_root)
        self.picker = picker or MacOSDirectoryPicker()
        self.selections = selections or SelectionAuthority(self.repository.root)
        self._picker_lock = threading.Lock()
        self._picker_receipts: dict[str, tuple[DirectoryRole, object]] = {}

    def list_projects(self) -> tuple[ProjectSnapshot, ...]:
        return tuple(self._refresh(project) for project in self.repository.list_projects())

    def get_project(self, project_id: str) -> ProjectSnapshot:
        return self._refresh(self.repository.get_project(project_id))

    def create_project(self, request_id: object, name: object) -> ProjectSnapshot:
        return self._refresh(self.repository.create_project(request_id, name))

    def update_details(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        name: object,
        brief: object,
    ) -> ProjectSnapshot:
        return self._refresh(
            self.repository.update_details(
                project_id, request_id, expected_revision, name, brief
            )
        )

    def select_directory(
        self, request_id: object, role: DirectoryRole
    ) -> dict[str, object] | None:
        normalized_request = validate_request_id(request_id)
        with self._picker_lock:
            cached = self._picker_receipts.get(normalized_request)
            if cached is not None:
                cached_role, cached_selection = cached
                if cached_role is not role:
                    raise ProjectError(
                        "request_reused",
                        "這次操作的識別資訊已用於不同內容。",
                        status_code=409,
                    )
                if cached_selection.expires_at >= time.monotonic():
                    return cached_selection.to_public()
                self._picker_receipts.pop(normalized_request, None)
            selected = self.picker.choose(role)
            if selected is None:
                return None
            selection = self.selections.register(selected, role)
            self._picker_receipts[normalized_request] = (role, selection)
            return selection.to_public()

    def set_directory(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        role: DirectoryRole,
        selection_token: object,
    ) -> ProjectSnapshot:
        normalized_request = validate_request_id(request_id)
        revision = validate_revision(expected_revision)
        command_hash = command_digest(
            {
                "expected_revision": revision,
                "role": role.value,
                "selection_token": selection_token,
            }
        )
        replay = self.repository.replay_receipt(
            project_id, normalized_request, "set_directory", command_hash
        )
        if replay is not None:
            return self._refresh(replay)
        reference = self.selections.resolve(selection_token, role)
        current = self.repository.get_project(project_id)
        other = next(
            (item for item in current.directories if item.role is not role), None
        )
        if other is not None and self._overlaps(reference, other):
            raise ProjectError(
                "directory_overlap",
                "毛片與參考作品請選擇不同且不重疊的資料夾。",
                status_code=422,
            )
        return self._refresh(
            self.repository.set_directory(
                project_id,
                normalized_request,
                command_hash,
                revision,
                reference,
            )
        )

    def remove_directory(
        self,
        project_id: str,
        request_id: object,
        expected_revision: object,
        role: DirectoryRole,
    ) -> ProjectSnapshot:
        return self._refresh(
            self.repository.remove_directory(
                project_id, request_id, expected_revision, role
            )
        )

    @staticmethod
    def _overlaps(first: DirectoryReference, second: DirectoryReference) -> bool:
        first_path = Path(first.canonical_path)
        second_path = Path(second.canonical_path)
        return (
            first_path == second_path
            or first_path.is_relative_to(second_path)
            or second_path.is_relative_to(first_path)
        )

    @staticmethod
    def _refresh(project: ProjectSnapshot) -> ProjectSnapshot:
        return replace(
            project,
            directories=tuple(
                refresh_availability(reference) for reference in project.directories
            ),
        )
