"""Sync skills, documents and CLAUDE.md from a project's Docs & Files.

A person manages the worker's knowledge from Basecamp: files dropped into the
"skills" / "documents" folders of the project's Docs & Files are mirrored into
the claude workspace, and files removed there are forgotten locally. The
workspace ``CLAUDE.md`` is likewise refreshed from Basecamp on its own cadence,
so project guidance can change without rebuilding the image.

The sync is a diff against the ``synced_files`` table (keyed by remote file id +
checksum), so unchanged files are skipped and removed files are cleaned up.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .basecamp import BasecampClient
from .config import Config
from .db import Database

logger = logging.getLogger(__name__)

KIND_SKILL = "skill"
KIND_DOCUMENT = "document"
KIND_CLAUDE_MD = "claude_md"

_FOLDER_TYPES = {"folder", "vault"}
# Handled specially (workspace/CLAUDE.md), never mirrored as a plain document.
_CLAUDE_MD_NAME = "claude.md"


def _entry_id(entry: dict) -> int:
    value = entry.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _entry_name(entry: dict) -> str:
    return str(entry.get("name") or entry.get("title") or entry.get("filename") or "").strip()


def _is_folder(entry: dict) -> bool:
    kind = str(entry.get("type") or entry.get("kind") or "").strip().lower()
    return kind in _FOLDER_TYPES or bool(entry.get("is_folder"))


def _entry_checksum(entry: dict) -> str:
    for key in ("updated_at", "version", "content_length", "byte_size"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def _find_folder(entries: list[dict], name: str) -> int | None:
    """Return the id of the folder matching ``name`` (case-insensitive)."""
    target = name.strip().lower()
    for entry in entries:
        if _is_folder(entry) and _entry_name(entry).lower() == target:
            file_id = _entry_id(entry)
            if file_id:
                return file_id
    return None


class Syncer:
    """Mirrors a project's skills/documents/CLAUDE.md into the workspace."""

    def __init__(self, client: BasecampClient, db: Database, config: Config):
        self._client = client
        self._db = db
        self._config = config
        self._workspace = config.claude_workspace_dir
        self._skills_dir = self._workspace / ".claude" / "skills"
        self._documents_dir = self._workspace / "documents"
        self._staging_dir = self._workspace / ".sync-tmp"

    async def sync_project(self, project_id: int) -> None:
        """Sync both the skills and documents folders of a project."""
        await self._sync_folder(
            project_id, self._config.skills_folder_name, KIND_SKILL, self._skills_dir
        )
        await self._sync_folder(
            project_id, self._config.documents_folder_name, KIND_DOCUMENT, self._documents_dir
        )

    async def sync_claude_md(self, project_id: int) -> None:
        """Refresh the workspace CLAUDE.md from a CLAUDE.md in the documents folder.

        When no CLAUDE.md is present in Basecamp the existing (baked) file is left
        untouched.
        """
        entries = await self._client.list_files(project_id)
        folder_id = _find_folder(entries, self._config.documents_folder_name)
        items = (
            await self._client.list_files(project_id, vault_id=folder_id)
            if folder_id is not None
            else []
        )
        target = next(
            (e for e in items if not _is_folder(e) and _entry_name(e).lower() == _CLAUDE_MD_NAME),
            None,
        )
        if target is None:
            return
        file_id = _entry_id(target)
        checksum = _entry_checksum(target)
        existing = await self._db.synced_files_for(project_id, KIND_CLAUDE_MD)
        if existing.get(file_id, (None, None, None))[1] == checksum and checksum:
            return  # unchanged
        dest = self._workspace / "CLAUDE.md"
        await self._download_to(project_id, file_id, dest)
        await self._db.upsert_synced_file(
            project_id, KIND_CLAUDE_MD, file_id, "CLAUDE.md", checksum, str(dest)
        )
        logger.info("Refreshed workspace CLAUDE.md from Basecamp")

    async def _sync_folder(
        self, project_id: int, folder_name: str, kind: str, dest_root: Path
    ) -> None:
        entries = await self._client.list_files(project_id)
        folder_id = _find_folder(entries, folder_name)
        if folder_id is None:
            # Folder not found. Treat this as "no information" and skip, rather
            # than "empty" — a transient/flaky listing must not wipe the whole
            # local mirror. Real deletions (folder present, file gone) still sync.
            logger.debug("Folder %r not found in project %s; skipping", folder_name, project_id)
            return

        items = await self._client.list_files(project_id, vault_id=folder_id)
        desired = {
            _entry_id(e): (_entry_name(e), _entry_checksum(e))
            for e in items
            if not _is_folder(e) and _entry_id(e) and _is_syncable(kind, _entry_name(e))
        }

        existing = await self._db.synced_files_for(project_id, kind)

        # Additions and changes.
        for file_id, (name, checksum) in desired.items():
            old = existing.get(file_id)
            if old is not None and old[1] == checksum and checksum:
                continue  # unchanged
            dest = _dest_path(kind, dest_root, name)
            await self._download_to(project_id, file_id, dest)
            await self._db.upsert_synced_file(
                project_id, kind, file_id, name, checksum, str(dest)
            )
            logger.info("Synced %s %r", kind, name)

        # Deletions: files that vanished from Basecamp.
        for file_id, (name, _checksum, local_path) in existing.items():
            if file_id not in desired:
                _remove_local(kind, Path(local_path))
                await self._db.delete_synced_file(project_id, kind, file_id)
                logger.info("Removed %s %r (deleted in Basecamp)", kind, name)

    async def _download_to(self, project_id: int, file_id: int, dest: Path) -> None:
        """Download a file and move it to ``dest`` (whatever its remote name)."""
        staging = self._staging_dir
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        await self._client.download_file(file_id, project_id, staging)
        downloaded = next((p for p in staging.iterdir() if p.is_file()), None)
        if downloaded is None:
            raise FileNotFoundError(f"Download produced no file for id {file_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded), str(dest))
        shutil.rmtree(staging, ignore_errors=True)


def _is_syncable(kind: str, name: str) -> bool:
    """Filter which files a folder contributes.

    Documents are markdown only, and the special CLAUDE.md is handled separately.
    Skills accept any file.
    """
    lower = name.lower()
    if lower == _CLAUDE_MD_NAME:
        return False
    if kind == KIND_DOCUMENT:
        return lower.endswith(".md")
    return True


def _slug(name: str) -> str:
    """Directory-safe stem for a skill file name."""
    stem = Path(name).stem.strip() or "skill"
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in stem)


def _dest_path(kind: str, dest_root: Path, name: str) -> Path:
    """Where a synced file of ``kind`` lands in the workspace.

    Skills become a Claude Code skill directory (``<slug>/SKILL.md``); documents
    keep their filename.
    """
    if kind == KIND_SKILL:
        return dest_root / _slug(name) / "SKILL.md"
    return dest_root / name


def _remove_local(kind: str, local_path: Path) -> None:
    """Remove a synced file (and, for a skill, its containing directory)."""
    if kind == KIND_SKILL:
        shutil.rmtree(local_path.parent, ignore_errors=True)
        return
    local_path.unlink(missing_ok=True)
