"""Atomic local projects, immutable snapshots, backups, and optimistic revisions."""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from .scene import Scene
from .wire import MAX_BYTES


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, (json.dumps(value, indent=2, allow_nan=False) + "\n").encode())


class Workspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        for folder in ("imports", "projects", "exports"):
            if not (self.root / folder).resolve().is_relative_to(self.root):
                raise ValueError("Workspace subdirectories must stay inside the workspace")
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    def project_path(self, name: str) -> Path:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise ValueError("Project name must be 1..64 letters, numbers, underscores, or hyphens")
        path = (self.root / "projects" / name).resolve()
        if not path.is_relative_to((self.root / "projects").resolve()) or not path.is_relative_to(self.root):
            raise ValueError("Project path escapes the workspace")
        return path

    def import_path(self, filename: str) -> Path:
        if not isinstance(filename, str):
            raise ValueError("source_file must be a filename under the workspace imports directory")
        path = (self.root / "imports" / filename).resolve()
        if not path.is_relative_to((self.root / "imports").resolve()) or not path.is_relative_to(self.root):
            raise ValueError("Copy the room into the workspace imports directory first")
        if path.suffix.lower() not in (".room", ".binpb") or not path.is_file():
            raise ValueError("source_file must name an existing .room or .binpb")
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("Room exceeds 64 MiB")
        return path

    @contextmanager
    def lock(self, name: str):
        path = self.project_path(name)
        if not path.is_dir():
            raise ValueError(f"Project does not exist: {name}")
        lockfile = path / ".lock"
        try:
            fd = os.open(lockfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise ValueError("Project is in use by another call. If a process crashed, close all builders before removing its .lock file") from None
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            lockfile.unlink(missing_ok=True)

    @staticmethod
    def _snapshot_path(path: Path, revision: str) -> Path:
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{32}", revision):
            raise ValueError("Invalid revision ID")
        return path / "revisions" / f"{revision}.json"

    def load(self, name: str) -> tuple[dict, dict, Scene]:
        path = self.project_path(name)
        try:
            manifest = json.loads((path / "project.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError(f"Project does not exist: {name}") from None
        snapshot = self.read_snapshot(name, manifest["revision"])
        digest = snapshot["scene_sha256"]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Corrupt scene hash in project")
        data = (path / "scenes" / f"{digest}.room").read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Project scene checksum mismatch; restore from an earlier snapshot")
        return manifest, snapshot, Scene(data)

    def read_snapshot(self, name: str, revision: str) -> dict:
        return json.loads(self._snapshot_path(self.project_path(name), revision).read_text(encoding="utf-8"))

    @staticmethod
    def expect(manifest: dict, expected_revision: str) -> None:
        if expected_revision != manifest["revision"]:
            raise ValueError(f"Revision conflict: expected {expected_revision}, current is {manifest['revision']}. Read the project again")

    def commit(self, name: str, manifest: dict, snapshot: dict, data: bytes, label: str) -> str:
        # Caller holds the project lock. Each file is immutable and complete before
        # project.json switches to it; an interrupted write leaves the old head valid.
        Scene(data)
        path = self.project_path(name)
        digest = hashlib.sha256(data).hexdigest()
        scene_file = path / "scenes" / f"{digest}.room"
        if not scene_file.exists():
            atomic_write(scene_file, data)
        revision = uuid.uuid4().hex
        new = copy.deepcopy(snapshot)
        new.update(revision=revision, parent=manifest.get("revision"), scene_sha256=digest,
                   label=label[:300], created_at=datetime.now(timezone.utc).isoformat())
        write_json(self._snapshot_path(path, revision), new)
        manifest["revision"] = revision
        write_json(path / "project.json", manifest)
        return revision

    def save_manifest(self, name: str, manifest: dict) -> None:
        write_json(self.project_path(name) / "project.json", manifest)

    def create(self, name: str, data: bytes, *, remote: dict | None = None, source: str = "local import") -> dict:
        Scene(data)
        path = self.project_path(name)
        try:
            path.mkdir(exist_ok=False)
        except FileExistsError:
            raise ValueError(f"Project already exists: {name}; choose a new name") from None
        try:
            atomic_write(path / "original.room", data)
            manifest = {"format": 1, "name": name, "remote": remote, "source": source, "pending_upload": None, "last_staged_save_id": None}
            self.commit(name, manifest, {"templates": {}, "objects": {}}, data, "Original client save")
        except BaseException:
            shutil.rmtree(path)
            raise
        return self.info(name)

    def info(self, name: str) -> dict:
        manifest, snapshot, scene = self.load(name)
        templates = {k: {field: value for field, value in t.items() if field != "data"} for k, t in snapshot["templates"].items()}
        return {
            "project": name, "revision": manifest["revision"], "source": manifest["source"],
            "scene": scene.summary(), "templates": templates,
            "created_objects": len(snapshot["objects"]), "remote": manifest["remote"],
            "pending_upload": manifest.get("pending_upload"),
            "last_staged_save_id": manifest.get("last_staged_save_id"),
            "game_load_verified": False,
            "workflow": "saved-room editing; reload the room in-game after publication",
        }

    def history(self, name: str, limit: int = 30) -> list[dict]:
        manifest, _, _ = self.load(name)
        result, revision = [], manifest["revision"]
        while revision and len(result) < limit:
            snapshot = self.read_snapshot(name, revision)
            result.append({k: snapshot[k] for k in ("revision", "parent", "label", "created_at", "scene_sha256")})
            revision = snapshot.get("parent")
        return result
