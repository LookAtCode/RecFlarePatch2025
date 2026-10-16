from __future__ import annotations

import base64
import copy
from email.parser import BytesParser
from email import policy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.parse

from recflare_mcp.api import Api, RemoteError, validate_url
from recflare_mcp.server import Builder
from recflare_mcp.scene import Scene
from recflare_mcp.workspace import Workspace
from common import OBJECT_ID, fixture


class State:
    def __init__(self):
        self.seed = fixture()
        digest = base64.b64encode(hashlib.sha256(self.seed).digest()).decode()
        self.room = {"RoomId": 10, "Name": "TestRoom", "IsDorm": False, "DataBlob": "room-metadata-unchanged", "SubRooms": [{"SubRoomId": 20, "Name": "Test", "CurrentSave": {"SubRoomDataSaveId": 100, "DataBlob": "seed.room", "DataBlobHash": digest, "UnityAssetId": "studio-asset-to-preserve", "PersistenceVersion": 52}, "StagedSubRoomDataSaveId": None, "InventionUsage": "original-invention-usage"}]}
        self.uploads = {"seed.room": self.seed}
        self.saves = [copy.deepcopy(self.room["SubRooms"][0]["CurrentSave"])]
        self.requests = []
        self.save_calls, self.publish_calls = 0, 0
        self.refuse_save, self.refuse_publish, self.lost_save_reply, self.lost_publish_reply = False, False, False, False
        self.bad_hash = False
        self.discovery_override = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def state(self):
        return self.server.state

    def reply(self, value, status=200, binary=False):
        data = value if binary else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream" if binary else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        self.state.requests.append(("GET", self.path, self.headers.get("Authorization")))
        if path == "/":
            root = f"http://127.0.0.1:{self.server.server_port}"
            return self.reply(self.state.discovery_override or {name: root + "/svc/" + name for name in ["Auth", "Rooms", "Storage", "CDN"]})
        if path.startswith("/svc/CDN/room/"):
            name = urllib.parse.unquote(path.removeprefix("/svc/CDN/room/"))
            data = self.state.uploads.get(name)
            if data is None:
                return self.reply({}, 404)
            return self.reply(data + b"bad" if self.state.bad_hash else data, binary=True)
        if self.headers.get("Authorization") != "Bearer test-token":
            return self.reply({}, 401)
        if path == "/svc/Rooms/rooms/ownedby/me":
            return self.reply([self.state.room])
        if path == "/svc/Rooms/rooms/10":
            return self.reply(self.state.room)
        if path.endswith("/saves/no_unity_assets"):
            return self.reply({"Results": list(reversed(self.state.saves)), "TotalResults": len(self.state.saves), "TotalCount": len(self.state.saves)})
        if "/saves/" in path:
            wanted = int(path.rsplit("/", 1)[1])
            return self.reply(next(row for row in self.state.saves if row["SubRoomDataSaveId"] == wanted))
        return self.reply({}, 404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.state.requests.append(("POST", self.path, self.headers.get("Authorization")))
        if self.path == "/svc/Auth/connect/token":
            form = urllib.parse.parse_qs(body.decode())
            if form.get("password") == ["test-password"] or form.get("refresh_token") == ["refresh-test"]:
                return self.reply({"access_token": "test-token", "refresh_token": "refresh-test", "expires_in": 3600})
            return self.reply({}, 400)
        if self.headers.get("Authorization") != "Bearer test-token":
            return self.reply({}, 401)
        if self.path == "/svc/Storage/upload":
            message = BytesParser(policy=policy.default).parsebytes(("Content-Type: " + self.headers["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
            parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in message.iter_parts()}
            if parts.get("FileType") != b"1" or "File" not in parts:
                return self.reply({}, 400)
            name = f"2026-10-16/upload-{len(self.state.uploads)}"
            self.state.uploads[name] = parts["File"]
            return self.reply({"filename": name})
        sub = self.state.room["SubRooms"][0]
        if self.path == "/svc/Rooms/rooms/10/subrooms/20/data":
            self.state.save_calls += 1
            if self.state.refuse_save:
                return self.reply({"success": False, "error": "refused", "value": None})
            value = json.loads(body)
            self.state.last_save_body = value
            if value["AutoPublish"] is not False:
                return self.reply({}, 400)
            blob = value["SubRoomData"]
            data = self.state.uploads[blob["Filename"]]
            if base64.b64encode(hashlib.sha256(data).digest()).decode() != blob["Hash"]:
                return self.reply({}, 400)
            save_id = 100 + len(self.state.saves)
            row = {"SubRoomDataSaveId": save_id, "DataBlob": blob["Filename"], "DataBlobHash": blob["Hash"], "PersistenceVersion": value["PersistenceVersion"]}
            self.state.saves.append(row)
            sub["StagedSubRoomDataSaveId"] = save_id
            if self.state.lost_save_reply:
                self.close_connection = True
                return
            return self.reply({"success": True, "error": None, "value": {"room": self.state.room, "subRoomDataSave": {"subRoomDataSaveId": save_id}}})
        if self.path == "/svc/Rooms/rooms/10/subrooms/20/publish_save":
            self.state.publish_calls += 1
            if self.state.refuse_publish:
                return self.reply({"success": False, "error": "owner only", "value": None})
            wanted = int(urllib.parse.parse_qs(body.decode())["subRoomDataSaveId"][0])
            sub["CurrentSave"] = next(row for row in self.state.saves if row["SubRoomDataSaveId"] == wanted)
            sub["StagedSubRoomDataSaveId"] = None
            if self.state.lost_publish_reply:
                self.close_connection = True
                return
            return self.reply({"success": True, "error": "", "value": self.state.room})
        return self.reply({}, 404)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Workspace(self.tmp.name)
        self.state = State()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.state = self.state
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api = Api(f"http://127.0.0.1:{self.server.server_port}", self.workspace, timeout=3)
        self.env = patch.dict(os.environ, {"RECFLARE_ACCESS_TOKEN": "test-token"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def download_build(self):
        result = self.api.download("online", 10, 20, "published")
        builder = Builder(self.workspace, self.api)
        result = builder.call("recflare_register_template", {"project": "online", "expected_revision": result["revision"], "template": "cube", "object_id": OBJECT_ID, "dimensions": [1, 1, 1]})
        return builder.call("recflare_apply_plan", {"project": "online", "expected_revision": result["revision"], "operations": [{"op": "repeat", "template": "cube", "name": "wall", "count": 10, "step": [1, 0, 0]}]})["revision"]

    def test_complete_download_build_stage_publish_contract(self):
        revision = self.download_build()
        staged = self.api.stage("online", revision, "Built a wall")
        self.assertFalse(staged["published"])
        self.assertEqual(self.state.room["SubRooms"][0]["CurrentSave"]["SubRoomDataSaveId"], 100)
        body = self.state.last_save_body
        self.assertEqual(body["UnityAssetId"], "studio-asset-to-preserve")
        self.assertEqual(body["InventionUsage"], "original-invention-usage")
        self.assertEqual(body["PersistenceVersion"], 52)
        self.assertNotIn("RoomData", body)
        self.assertEqual(self.state.room["DataBlob"], "room-metadata-unchanged")
        data = self.state.uploads[body["SubRoomData"]["Filename"]]
        self.assertEqual(len(Scene(data).views), 11)
        published = self.api.publish("online", revision, staged["save_id"])
        self.assertTrue(published["reload_required"])
        self.assertEqual(self.state.room["SubRooms"][0]["CurrentSave"]["SubRoomDataSaveId"], staged["save_id"])

    def test_stage_is_idempotent_for_the_same_revision(self):
        revision = self.download_build()
        first = self.api.stage("online", revision, "wall")
        second = self.api.stage("online", revision, "wall")
        self.assertEqual(first["save_id"], second["save_id"])
        self.assertEqual(self.state.save_calls, 1)

    def test_save_conflict_and_dorm_rejected_before_upload(self):
        revision = self.download_build()
        self.state.room["SubRooms"][0]["StagedSubRoomDataSaveId"] = 999
        with self.assertRaisesRegex(ValueError, "changed"):
            self.api.stage("online", revision, "conflict")
        self.assertEqual(len(self.state.uploads), 1)
        self.state.room["SubRooms"][0]["StagedSubRoomDataSaveId"] = None
        self.state.room["IsDorm"] = True
        self.api.download("dorm", 10, 20, "published")
        with self.assertRaisesRegex(ValueError, "Dorms auto-publish"):
            self.api.stage("dorm", self.workspace.info("dorm")["revision"], "dorm")

    def test_lost_save_response_is_reconciled_without_retry(self):
        revision = self.download_build()
        self.state.lost_save_reply = True
        with self.assertRaises(RemoteError):
            self.api.stage("online", revision, "wall")
        with self.assertRaisesRegex(ValueError, "uncertain"):
            self.api.stage("online", revision, "wall")
        result = self.api.check_upload("online")
        self.assertTrue(result["staged"])
        self.assertEqual(self.state.save_calls, 1)
        self.assertIsNone(self.workspace.info("online")["pending_upload"])

    def test_lost_publish_response_can_be_verified_without_second_post(self):
        revision = self.download_build()
        staged = self.api.stage("online", revision, "wall")
        self.state.lost_publish_reply = True
        with self.assertRaises(RemoteError):
            self.api.publish("online", revision, staged["save_id"])
        result = self.api.publish("online", revision, staged["save_id"])
        self.assertTrue(result["already_published"])
        self.assertEqual(self.state.publish_calls, 1)

    def test_http_200_save_refusal_is_not_success(self):
        revision = self.download_build()
        self.state.refuse_save = True
        with self.assertRaisesRegex(RemoteError, "refused"):
            self.api.stage("online", revision, "wall")
        self.assertIsNone(self.workspace.info("online")["pending_upload"])

    def test_wrong_save_or_local_revision_cannot_publish(self):
        revision = self.download_build()
        staged = self.api.stage("online", revision, "wall")
        with self.assertRaisesRegex(ValueError, "latest verified"):
            self.api.publish("online", revision, staged["save_id"] + 1)
        self.state.refuse_publish = True
        with self.assertRaisesRegex(RemoteError, "creator"):
            self.api.publish("online", revision, staged["save_id"])

    def test_credential_is_not_sent_to_cdn_or_discovery(self):
        self.api.download("read", 10, 20, "published")
        for method, path, authorization in self.state.requests:
            if path == "/" or path.startswith("/svc/CDN"):
                self.assertIsNone(authorization)

    def test_corrupted_download_is_rejected(self):
        self.state.bad_hash = True
        with self.assertRaisesRegex(RemoteError, "SHA-256"):
            self.api.download("bad", 10, 20, "published")
        self.assertFalse(self.workspace.project_path("bad").exists())

    def test_local_login_keeps_credentials_out_of_tool_output(self):
        self.api.login("test-user", "test-password")
        status = Builder(self.workspace, self.api).call("recflare_status", {})
        self.assertTrue(status["login_available"])
        self.assertNotIn("test-token", json.dumps(status))
        self.assertNotIn("test-password", (self.workspace.root / "auth.json").read_text())

    def test_service_origin_and_transport_validation(self):
        for value in ["http://recflare.net", "https://user:password@recflare.net", "https://recflare.net/#fragment"]:
            with self.assertRaises(ValueError):
                validate_url(value)
        root = f"http://127.0.0.1:{self.server.server_port}"
        self.state.discovery_override = {name: root + "/svc/" + name for name in ["Auth", "Rooms", "Storage", "CDN"]}
        self.state.discovery_override["Rooms"] = "https://unrelated.invalid"
        with self.assertRaisesRegex(ValueError, "outside"):
            self.api.discover()

    def test_offline_import_cannot_be_accidentally_uploaded(self):
        self.workspace.create("offline", fixture())
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.api.stage("offline", self.workspace.info("offline")["revision"], "wall")


if __name__ == "__main__":
    unittest.main()
