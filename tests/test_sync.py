"""Tests for the Docs & Files syncer."""

from __future__ import annotations

import os
from pathlib import Path

from bcworker.config import Config
from bcworker.db import Database
from bcworker.sync import KIND_DOCUMENT, KIND_SKILL, Syncer

PROJECT = 55


class FakeSyncClient:
    """In-memory Docs & Files: root folders + per-folder file entries."""

    def __init__(self) -> None:
        # folder name -> (folder_id, list of file entries)
        self.folders: dict[str, tuple[int, list[dict]]] = {}
        # file_id -> (filename, content)
        self.blobs: dict[int, tuple[str, str]] = {}
        self.download_count = 0

    def set_folder(self, name: str, folder_id: int, entries: list[dict]) -> None:
        self.folders[name] = (folder_id, entries)

    async def list_files(self, project_id: int, vault_id: int | None = None) -> list[dict]:
        if vault_id is None:
            return [
                {"id": fid, "name": name, "type": "folder"}
                for name, (fid, _entries) in self.folders.items()
            ]
        for _name, (fid, entries) in self.folders.items():
            if fid == vault_id:
                return list(entries)
        return []

    async def download_file(self, file_id: int, project_id: int, out_dir: Path) -> None:
        self.download_count += 1
        name, content = self.blobs[file_id]
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:  # noqa: ASYNC230
            fh.write(content)


def _config(tmp_path: Path) -> Config:
    return Config(claude_workspace_dir=tmp_path / "workspace", basecamp_project_id=PROJECT)


async def test_sync_adds_skill_and_document(db: Database, tmp_path: Path) -> None:
    client = FakeSyncClient()
    client.set_folder("skills", 100, [{"id": 1, "name": "greet.md", "updated_at": "v1"}])
    client.set_folder("documents", 200, [{"id": 2, "name": "tables.md", "updated_at": "v1"}])
    client.blobs = {1: ("greet.md", "skill body"), 2: ("tables.md", "doc body")}
    syncer = Syncer(client, db, _config(tmp_path))

    await syncer.sync_project(PROJECT)

    ws = tmp_path / "workspace"
    assert (ws / ".claude" / "skills" / "greet" / "SKILL.md").read_text() == "skill body"
    assert (ws / "documents" / "tables.md").read_text() == "doc body"
    assert set(await db.synced_files_for(PROJECT, KIND_SKILL)) == {1}
    assert set(await db.synced_files_for(PROJECT, KIND_DOCUMENT)) == {2}


async def test_unchanged_file_is_not_redownloaded(db: Database, tmp_path: Path) -> None:
    client = FakeSyncClient()
    client.set_folder("skills", 100, [{"id": 1, "name": "greet.md", "updated_at": "v1"}])
    client.set_folder("documents", 200, [])
    client.blobs = {1: ("greet.md", "body")}
    syncer = Syncer(client, db, _config(tmp_path))

    await syncer.sync_project(PROJECT)
    await syncer.sync_project(PROJECT)  # same checksum

    assert client.download_count == 1


async def test_changed_file_is_redownloaded(db: Database, tmp_path: Path) -> None:
    client = FakeSyncClient()
    client.set_folder("skills", 100, [{"id": 1, "name": "greet.md", "updated_at": "v1"}])
    client.set_folder("documents", 200, [])
    client.blobs = {1: ("greet.md", "body")}
    syncer = Syncer(client, db, _config(tmp_path))
    await syncer.sync_project(PROJECT)

    client.set_folder("skills", 100, [{"id": 1, "name": "greet.md", "updated_at": "v2"}])
    client.blobs = {1: ("greet.md", "new body")}
    await syncer.sync_project(PROJECT)

    assert client.download_count == 2
    ws = tmp_path / "workspace"
    assert (ws / ".claude" / "skills" / "greet" / "SKILL.md").read_text() == "new body"


async def test_deleted_file_is_removed(db: Database, tmp_path: Path) -> None:
    client = FakeSyncClient()
    client.set_folder("skills", 100, [{"id": 1, "name": "greet.md", "updated_at": "v1"}])
    client.set_folder("documents", 200, [{"id": 2, "name": "tables.md", "updated_at": "v1"}])
    client.blobs = {1: ("greet.md", "s"), 2: ("tables.md", "d")}
    syncer = Syncer(client, db, _config(tmp_path))
    await syncer.sync_project(PROJECT)

    # Both folders now empty.
    client.set_folder("skills", 100, [])
    client.set_folder("documents", 200, [])
    await syncer.sync_project(PROJECT)

    ws = tmp_path / "workspace"
    assert not (ws / ".claude" / "skills" / "greet").exists()
    assert not (ws / "documents" / "tables.md").exists()
    assert await db.synced_files_for(PROJECT, KIND_SKILL) == {}
    assert await db.synced_files_for(PROJECT, KIND_DOCUMENT) == {}


async def test_non_markdown_documents_are_ignored(db: Database, tmp_path: Path) -> None:
    client = FakeSyncClient()
    client.set_folder("skills", 100, [])
    client.set_folder("documents", 200, [{"id": 9, "name": "notes.txt", "updated_at": "v1"}])
    client.blobs = {9: ("notes.txt", "x")}
    syncer = Syncer(client, db, _config(tmp_path))

    await syncer.sync_project(PROJECT)

    assert await db.synced_files_for(PROJECT, KIND_DOCUMENT) == {}


async def test_sync_claude_md_overwrites_workspace_file(db: Database, tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("baked", encoding="utf-8")

    client = FakeSyncClient()
    client.set_folder("skills", 100, [])
    client.set_folder("documents", 200, [{"id": 3, "name": "CLAUDE.md", "updated_at": "v1"}])
    client.blobs = {3: ("CLAUDE.md", "from basecamp")}
    syncer = Syncer(client, db, _config(tmp_path))

    await syncer.sync_claude_md(PROJECT)

    assert (ws / "CLAUDE.md").read_text() == "from basecamp"
    # A CLAUDE.md in documents is not also mirrored as a plain document.
    await syncer.sync_project(PROJECT)
    assert not (ws / "documents" / "CLAUDE.md").exists()


async def test_missing_folder_does_not_wipe_mirror(db: Database, tmp_path: Path) -> None:
    client = FakeSyncClient()
    client.set_folder("skills", 100, [{"id": 1, "name": "greet.md", "updated_at": "v1"}])
    client.set_folder("documents", 200, [])
    client.blobs = {1: ("greet.md", "body")}
    syncer = Syncer(client, db, _config(tmp_path))
    await syncer.sync_project(PROJECT)

    # The whole skills folder disappears from the listing (e.g. a flaky response).
    client.folders.pop("skills")
    await syncer.sync_project(PROJECT)

    ws = tmp_path / "workspace"
    assert (ws / ".claude" / "skills" / "greet" / "SKILL.md").exists()
    assert set(await db.synced_files_for(PROJECT, KIND_SKILL)) == {1}


async def test_sync_claude_md_absent_leaves_file(db: Database, tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("baked", encoding="utf-8")

    client = FakeSyncClient()
    client.set_folder("documents", 200, [])
    syncer = Syncer(client, db, _config(tmp_path))

    await syncer.sync_claude_md(PROJECT)

    assert (ws / "CLAUDE.md").read_text() == "baked"
