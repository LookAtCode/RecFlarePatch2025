"""Existing RecFlare HTTP contracts. This module never changes server code."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .wire import MAX_BYTES
from .workspace import Workspace, write_json


class RemoteError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_url(value: str, *, allow_query: bool = False) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(value)
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query and not allow_query:
        raise ValueError("Server URL must have a host, without credentials, query, or fragment")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and _loopback(parsed.hostname)):
        raise ValueError("Use HTTPS for a remote server; HTTP is allowed only on loopback")
    return parsed


def save_id(save: dict | None) -> int | None:
    if not isinstance(save, dict):
        return None
    value = save.get("SubRoomDataSaveId", save.get("subRoomDataSaveId"))
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def room_state(sub: dict) -> dict:
    return {"published": save_id(sub.get("CurrentSave")), "staged": sub.get("StagedSubRoomDataSaveId")}


class Api:
    def __init__(self, server: str | None, workspace: Workspace, *, timeout: float = 30):
        self.server = server.rstrip("/") if server else None
        self.workspace = workspace
        self.timeout = timeout
        self.services: dict[str, str] | None = None
        self.opener = urllib.request.build_opener(NoRedirects())
        self.host = validate_url(self.server).hostname if self.server else None
        self.domain = self.host[3:] if self.host and self.host.startswith("ns.") else self.host

    def _trust(self, url: str) -> None:
        parsed = validate_url(url, allow_query=True)
        hostname = parsed.hostname
        if not self.domain or not (hostname == self.domain or hostname.endswith("." + self.domain)):
            raise ValueError("Discovery returned a service outside the configured server domain")
        if self.server.startswith("https:") and parsed.scheme != "https":
            raise ValueError("Discovery attempted to downgrade HTTPS")

    def _request(self, method: str, url: str, *, body: bytes | None = None,
                 content_type: str | None = None, authed: bool = False,
                 binary: bool = False):
        self._trust(url)
        headers = {"Accept": "application/octet-stream" if binary else "application/json", "User-Agent": "RecFlare-MCP/0.1.0"}
        if content_type:
            headers["Content-Type"] = content_type
        if authed:
            headers["Authorization"] = "Bearer " + self._token()
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                limit = MAX_BYTES if binary else 4 * 1024 * 1024
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise RemoteError("Response exceeds size limit")
        except urllib.error.HTTPError as error:
            # Do not echo raw response bodies or request headers: tokens can occur
            # there. In particular never forward a bearer through a redirect.
            raise RemoteError(f"RecFlare returned HTTP {error.code} for {method} {urllib.parse.urlsplit(url).path}", error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RemoteError("RecFlare connection failed or timed out; mutating requests are never retried automatically") from None
        if binary:
            return data
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise RemoteError("RecFlare returned an invalid JSON response") from None

    def discover(self) -> dict[str, str]:
        if not self.server:
            raise ValueError("Online tools require --server; offline building and export work without it")
        if self.services is None:
            document = self._request("GET", self.server + "/")
            if not isinstance(document, dict):
                raise RemoteError("Invalid name-server document")
            services = {}
            for name in ("Auth", "Rooms", "Storage", "CDN"):
                value = document.get(name)
                if not isinstance(value, str):
                    raise RemoteError(f"Name server did not advertise {name}")
                validate_url(value)
                self._trust(value)
                services[name] = value.rstrip("/")
            self.services = services
        return self.services

    def call(self, service: str, method: str, path: str, *, value=None, form: dict | None = None, binary: bool = False):
        base = self.discover()[service]
        body, content_type = None, None
        if value is not None:
            body, content_type = json.dumps(value, allow_nan=False).encode(), "application/json"
        if form is not None:
            body, content_type = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        return self._request(method, base + path, body=body, content_type=content_type, authed=service != "CDN", binary=binary)

    def _token(self) -> str:
        from_environment = os.environ.get("RECFLARE_ACCESS_TOKEN")
        if from_environment:
            if "\n" in from_environment or "\r" in from_environment:
                raise ValueError("Invalid access token")
            return from_environment
        path = self.workspace.root / "auth.json"
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError("Sign in locally with server.py login, or provide RECFLARE_ACCESS_TOKEN") from None
        if cached.get("server") != self.server:
            raise ValueError("Cached login belongs to another server; sign in again for this server")
        if cached.get("expires_at", 0) < time.time() + 60:
            refresh = cached.get("refresh_token")
            if not refresh:
                raise ValueError("Login expired; sign in again locally")
            cached = self._grant({"grant_type": "refresh_token", "refresh_token": refresh, "ver": "20250718.01"})
        token = cached.get("access_token")
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ValueError("Invalid login cache; sign in again locally")
        return token

    def _grant(self, form: dict) -> dict:
        auth = self.discover()["Auth"]
        result = self._request("POST", auth + "/connect/token", body=urllib.parse.urlencode(form).encode(), content_type="application/x-www-form-urlencoded")
        if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
            raise RemoteError("Sign-in did not return an access token")
        cached = {"server": self.server, "access_token": result["access_token"], "refresh_token": result.get("refresh_token"), "expires_at": time.time() + int(result.get("expires_in", 3600))}
        write_json(self.workspace.root / "auth.json", cached)
        return cached

    def login(self, username: str, password: str) -> None:
        self._grant({"grant_type": "password", "username": username, "password": password, "ver": "20250718.01"})

    def room(self, room_id: int, subroom_id: int) -> tuple[dict, dict]:
        room = self.call("Rooms", "GET", f"/rooms/{room_id}")
        if not isinstance(room, dict) or room.get("RoomId") != room_id:
            raise RemoteError("Room response does not match the requested room")
        subs = room.get("SubRooms") or []
        sub = next((s for s in subs if s.get("SubRoomId") == subroom_id), None)
        if not sub:
            raise ValueError("Subroom is not part of this room")
        return room, sub

    def download(self, project: str, room_id: int, subroom_id: int, source: str) -> dict:
        room, sub = self.room(room_id, subroom_id)
        if source == "staged":
            staged_id = sub.get("StagedSubRoomDataSaveId")
            if not staged_id:
                raise ValueError("This subroom has no staged save")
            save = self.call("Rooms", "GET", f"/rooms/{room_id}/subrooms/{subroom_id}/saves/{staged_id}")
        elif source == "published":
            save = sub.get("CurrentSave")
        else:
            raise ValueError("source must be published or staged")
        if not isinstance(save, dict) or save_id(save) is None or not save.get("DataBlob"):
            raise ValueError("Save the room with a plain Maker Pen seed shape first")
        filename = save["DataBlob"]
        if not isinstance(filename, str) or ".." in filename.split("/"):
            raise RemoteError("Invalid room blob filename")
        data = self.call("CDN", "GET", "/room/" + urllib.parse.quote(filename, safe="/"), binary=True)
        expected_hash = save.get("DataBlobHash")
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        if expected_hash and digest != expected_hash:
            raise RemoteError("Downloaded room does not match its SHA-256 hash")
        remote = {"server": self.server, "room_id": room_id, "subroom_id": subroom_id, "source_save_id": save_id(save), "state": room_state(sub), "is_dorm": room.get("IsDorm") is True,
                  "unity_asset_id": save.get("UnityAssetId"), "persistence_version": save.get("PersistenceVersion", sub.get("PersistenceVersion", 0)), "invention_usage": sub.get("InventionUsage")}
        return self.workspace.create(project, data, remote=remote, source=f"RecFlare {source} save {save_id(save)}")

    def _bound(self, manifest: dict) -> dict:
        remote = manifest.get("remote")
        if not isinstance(remote, dict) or remote.get("server") != self.server:
            raise ValueError("Project is not bound to this server. Start with recflare_download_room; local imports can be exported for manual upload")
        if remote.get("is_dorm"):
            raise ValueError("Dorms auto-publish on save. Use a regular test room for staged MCP uploads, or export a file for manual import")
        return remote

    def _unchanged(self, remote: dict) -> dict:
        _, sub = self.room(remote["room_id"], remote["subroom_id"])
        if room_state(sub) != remote["state"]:
            raise ValueError("Server save changed since this project was downloaded or staged. Download a new project before uploading")
        return sub

    def stage(self, project: str, expected_revision: str, description: str) -> dict:
        with self.workspace.lock(project):
            manifest, snapshot, scene = self.workspace.load(project)
            self.workspace.expect(manifest, expected_revision)
            remote = self._bound(manifest)
            if manifest.get("pending_upload"):
                raise ValueError("A previous upload has an uncertain outcome. Call recflare_check_upload before doing anything else")
            self._unchanged(remote)
            if manifest.get("last_staged_revision") == expected_revision:
                return {"save_id": manifest["last_staged_save_id"], "already_staged": True, "published": False}
            scene.require_editable()
            data = scene.encode()
            boundary = "RecFlareMCP" + uuid.uuid4().hex
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"FileType\"\r\n\r\n1\r\n"
                    f"--{boundary}\r\nContent-Disposition: form-data; name=\"File\"; filename=\"mcp.room\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
            uploaded = self._request("POST", self.discover()["Storage"] + "/upload", body=body, content_type=f"multipart/form-data; boundary={boundary}", authed=True)
            filename = uploaded.get("filename") if isinstance(uploaded, dict) else None
            if not isinstance(filename, str) or not filename:
                raise RemoteError("Storage did not return an uploaded filename")
            self._unchanged(remote)
            digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
            pending = {"filename": filename, "hash": digest, "revision": expected_revision, "description": description}
            manifest["pending_upload"] = pending
            self.workspace.save_manifest(project, manifest)
            value = {"SubRoomData": {"Filename": filename, "Hash": digest}, "Description": description, "AutoPublish": False, "PersistenceVersion": remote["persistence_version"]}
            if remote.get("unity_asset_id"):
                value["UnityAssetId"] = remote["unity_asset_id"]
            if isinstance(remote.get("invention_usage"), str):
                value["InventionUsage"] = remote["invention_usage"]
            path = f"/rooms/{remote['room_id']}/subrooms/{remote['subroom_id']}/data"
            try:
                response = self.call("Rooms", "POST", path, value=value)
            except RemoteError as error:
                if error.status is not None and 400 <= error.status < 500:
                    manifest["pending_upload"] = None
                    self.workspace.save_manifest(project, manifest)
                raise
            if not isinstance(response, dict) or response.get("success") is not True:
                if isinstance(response, dict) and response.get("success") is False:
                    manifest["pending_upload"] = None
                    self.workspace.save_manifest(project, manifest)
                    raise RemoteError("Room save was refused by RecFlare")
                raise RemoteError("Save response was unexpected; its outcome is unknown. Use recflare_check_upload")
            new_id = save_id((response.get("value") or {}).get("subRoomDataSave"))
            if new_id is None:
                raise RemoteError("Save may have succeeded but its ID is unknown; use recflare_check_upload")
            return self._finish_upload(project, manifest, new_id)

    def _finish_upload(self, project: str, manifest: dict, new_id: int) -> dict:
        remote = self._bound(manifest)
        pending = manifest["pending_upload"]
        _, sub = self.room(remote["room_id"], remote["subroom_id"])
        actual = room_state(sub)
        if actual["staged"] != new_id or actual["published"] != remote["state"]["published"]:
            raise RemoteError("Save was recorded but its staging state could not be verified; use recflare_check_upload")
        remote["state"] = actual
        manifest.update(pending_upload=None, last_staged_save_id=new_id, last_staged_revision=pending["revision"])
        self.workspace.save_manifest(project, manifest)
        return {"save_id": new_id, "published": False, "staged": True, "revision": pending["revision"]}

    def check_upload(self, project: str) -> dict:
        with self.workspace.lock(project):
            manifest, _, _ = self.workspace.load(project)
            remote = self._bound(manifest)
            pending = manifest.get("pending_upload")
            if not pending:
                return {"pending": False, "last_staged_save_id": manifest.get("last_staged_save_id")}
            for skip in range(0, 500, 50):
                page = self.call("Rooms", "GET", f"/rooms/{remote['room_id']}/subrooms/{remote['subroom_id']}/saves/no_unity_assets?skip={skip}&take=50")
                rows = page.get("Results", []) if isinstance(page, dict) else []
                match = next((row for row in rows if row.get("DataBlob") == pending["filename"] and row.get("DataBlobHash") == pending["hash"]), None)
                if match and save_id(match):
                    return self._finish_upload(project, manifest, save_id(match))
                if len(rows) < 50:
                    break
            return {"pending": True, "outcome": "unknown", "retry_allowed": False, "message": "No matching save was found. Keep the project journal and inspect server history before any manual retry"}

    def publish(self, project: str, expected_revision: str, expected_save_id: int) -> dict:
        with self.workspace.lock(project):
            manifest, _, _ = self.workspace.load(project)
            self.workspace.expect(manifest, expected_revision)
            remote = self._bound(manifest)
            if manifest.get("pending_upload") or manifest.get("last_staged_revision") != expected_revision:
                raise ValueError("Stage this exact local revision successfully before publishing")
            _, sub = self.room(remote["room_id"], remote["subroom_id"])
            if expected_save_id == manifest.get("last_staged_save_id") and save_id(sub.get("CurrentSave")) == expected_save_id:
                remote["state"] = room_state(sub)
                self.workspace.save_manifest(project, manifest)
                return {"published": True, "save_id": expected_save_id, "already_published": True, "reload_required": True}
            if room_state(sub) != remote["state"]:
                raise ValueError("Server save changed since staging; inspect it before publishing")
            if expected_save_id != manifest.get("last_staged_save_id") or expected_save_id != sub.get("StagedSubRoomDataSaveId"):
                raise ValueError("The requested save is not this project's latest verified staged save")
            response = self.call("Rooms", "POST", f"/rooms/{remote['room_id']}/subrooms/{remote['subroom_id']}/publish_save", form={"subRoomDataSaveId": expected_save_id})
            if not isinstance(response, dict) or response.get("success") is not True:
                raise RemoteError("Publication was refused; only the room creator may publish")
            _, sub = self.room(remote["room_id"], remote["subroom_id"])
            if save_id(sub.get("CurrentSave")) != expected_save_id:
                raise RemoteError("Publication outcome could not be verified; inspect the room before retrying")
            remote["state"] = room_state(sub)
            self.workspace.save_manifest(project, manifest)
            return {"published": True, "save_id": expected_save_id, "reload_required": True, "message": "Rejoin a fresh room instance to load this published save. An already-running instance is not updated by this tool"}
