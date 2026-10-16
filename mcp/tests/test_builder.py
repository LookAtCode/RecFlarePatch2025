from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

from recflare_mcp.api import Api
from recflare_mcp.scene import Scene, quaternion
from recflare_mcp.server import Builder, TOOLS, dispatch, serve
from recflare_mcp.wire import FormatError, Message, read_vector
from recflare_mcp.workspace import Workspace
from common import CIRCUITS, OBJECT_ID, UNKNOWN, blob, fixture, integer


class WireTests(unittest.TestCase):
    def test_golden_wire_roundtrip_and_unknown_noncanonical_varint(self):
        # Protobuf wire-format examples: int32 field 1=150; string field 2='testing'.
        data = bytes.fromhex("089601120774657374696e67") + UNKNOWN
        message = Message.parse(data)
        self.assertEqual(message.integer(1), 150)
        self.assertEqual(message.blob(2), b"testing")
        self.assertEqual(message.encode(), data)
        message.set_int(1, 151)
        self.assertTrue(message.encode().endswith(UNKNOWN))

    def test_malformed_wire_is_rejected(self):
        for data in [b"\x00", b"\x80", b"\x0a\x02x", b"\x0d\x00", b"\x08" + b"\xff" * 10, b"\x0b", b"\x0f"]:
            with self.subTest(data=data), self.assertRaises(FormatError):
                Message.parse(data)

    def test_duplicate_singular_messages_merge(self):
        message = Message.parse(blob(1, integer(1, 3)) + blob(1, integer(2, 7)))
        self.assertEqual(message.message(1).integer(1), 3)
        self.assertEqual(message.message(1).integer(2), 7)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Workspace(self.temporary.name)
        self.builder = Builder(self.workspace, Api(None, self.workspace))
        self.original = fixture()
        (self.workspace.root / "imports" / "seed.room").write_bytes(self.original)
        self.builder.call("recflare_open_room", {"project": "arena", "source_file": "seed.room"})

    def revision(self):
        return self.workspace.info("arena")["revision"]

    def template(self):
        return self.builder.call("recflare_register_template", {"project": "arena", "expected_revision": self.revision(), "template": "block", "object_id": OBJECT_ID, "dimensions": [1, 1, 1]})

    def apply(self, operations, revision=None):
        return self.builder.call("recflare_apply_plan", {"project": "arena", "expected_revision": revision or self.revision(), "operations": operations})

    def test_import_is_byte_exact_and_original_is_backed_up(self):
        _, _, scene = self.workspace.load("arena")
        self.assertEqual(scene.encode(), self.original)
        self.assertEqual((self.workspace.project_path("arena") / "original.room").read_bytes(), self.original)

    def test_build_transform_paint_export_and_restore(self):
        first = self.revision()
        self.template()
        self.apply([{"op": "create", "template": "block", "name": "floor", "position": [0, -.5, 0], "size": [12, 1, 12], "color": "#204080"}])
        self.apply([{"op": "transform", "name": "floor", "position": [5, 2, -3], "rotation": [0, 90, 0], "size": [10, 2, 8]}, {"op": "paint", "name": "floor", "material": 9}])
        _, snapshot, scene = self.workspace.load("arena")
        key = next(iter(snapshot["objects"]))
        view = scene.find(key)
        self.assertEqual(read_vector(view.message(10).message(1)), [5, 2, -3])
        self.assertEqual(read_vector(view.message(53).message(1)), [10, 2, 8])
        color = view.message(14).message(2)
        self.assertAlmostEqual(color.floating(1), 32 / 255)
        self.assertEqual(scene.root.blob(18), CIRCUITS)
        self.assertEqual(scene.root.blob(28), CIRCUITS)
        self.assertEqual(scene.find(OBJECT_ID).encode(), Scene(self.original).find(OBJECT_ID).encode())
        self.assertIn(UNKNOWN, scene.encode())
        exported = self.builder.call("recflare_export_room", {"project": "arena", "expected_revision": self.revision()})
        self.assertEqual(Path(exported["path"]).read_bytes(), scene.encode())
        self.builder.call("recflare_restore_revision", {"project": "arena", "expected_revision": self.revision(), "restore_revision": first})
        self.assertEqual(self.workspace.load("arena")[2].encode(), self.original)
        self.assertGreater(len(self.workspace.history("arena")), 3)

    def test_1000_piece_build_has_unique_ids_and_preserves_original(self):
        self.template()
        self.apply([{"op": "grid", "template": "block", "name": "tile", "counts": [10, 10, 10], "spacing": [2, 2, 2]}])
        _, snapshot, scene = self.workspace.load("arena")
        self.assertEqual(len(snapshot["objects"]), 1000)
        self.assertEqual(len(scene.views), 1001)
        ids = [v.blob(1) for v in scene.views]
        shape_ids = [Message.parse(f.value).blob(12) for view in scene.views for f in view.message(12).message(1).all(1, 2)]
        self.assertEqual(len(set(ids)), 1001)
        self.assertEqual(len(set(shape_ids)), 1001)
        self.assertEqual(scene.find(OBJECT_ID).encode(), Scene(self.original).find(OBJECT_ID).encode())

    def test_repeat_staircase_positions(self):
        self.template()
        self.apply([{"op": "repeat", "template": "block", "name": "step", "count": 12, "position": [0, 0, 0], "step": [1, .5, 0], "size": [1, .5, 2]}])
        objects = self.workspace.load("arena")[1]["objects"].values()
        self.assertEqual(list(objects)[-1]["position"], [11, 5.5, 0])

    def test_bad_operation_rolls_back_entire_plan(self):
        self.template()
        revision = self.revision()
        with self.assertRaises(ValueError):
            self.apply([{"op": "create", "template": "block", "name": "valid"}, {"op": "create", "template": "missing", "name": "invalid"}])
        self.assertEqual(self.revision(), revision)
        self.assertEqual(self.workspace.info("arena")["created_objects"], 0)

    def test_stale_revision_cannot_edit_and_originals_cannot_be_deleted(self):
        old = self.revision()
        self.template()
        with self.assertRaisesRegex(ValueError, "Revision conflict"):
            self.apply([{"op": "create", "template": "block", "name": "x"}], revision=old)
        with self.assertRaisesRegex(ValueError, "Unknown MCP-created"):
            self.apply([{"op": "delete", "name": OBJECT_ID}])

    def test_rooms_2_cannot_be_edited(self):
        scene = Scene(fixture(object_model=True))
        with self.assertRaisesRegex(ValueError, "Rooms 2"):
            scene.make_template(OBJECT_ID, None, [1, 1, 1])

    def test_bad_templates_require_specific_shape_or_centered_seed(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            Scene(fixture(extra_shapes=True)).make_template(OBJECT_ID, None, [1, 1, 1])
        with self.assertRaisesRegex(ValueError, "centered"):
            Scene(fixture(local_position=[2, 0, 0])).make_template(OBJECT_ID, None, [1, 1, 1])

    def test_unset_entity_sentinel_is_preserved_but_real_indices_are_rejected(self):
        scene = Scene(fixture())
        scene.find(OBJECT_ID).set_int(100, (1 << 64) - 1)
        template = scene.make_template(OBJECT_ID, None, [1, 1, 1])
        self.assertEqual(Message.parse(base64.b64decode(template["data"])).integer(100), (1 << 64) - 1)
        scene.find(OBJECT_ID).set_int(100, 12)
        with self.assertRaisesRegex(ValueError, "indexed"):
            scene.make_template(OBJECT_ID, None, [1, 1, 1])

    def test_path_escape_rejected(self):
        for path in ["../seed.room", str(self.workspace.root.parent / "seed.room")]:
            with self.assertRaises(ValueError):
                self.workspace.import_path(path)
        for name in ["../escape", "bad/name", "MCP Test", ""]:
            with self.assertRaises(ValueError):
                self.workspace.project_path(name)

    def test_different_shape_and_material_ids_are_reused_not_invented(self):
        self.template()
        self.apply([{"op": "create", "template": "block", "name": "test"}])
        _, snapshot, scene = self.workspace.load("arena")
        shape = scene.find(next(iter(snapshot["objects"]))).message(12).message(1).message(1)
        self.assertEqual(shape.integer(1), 4)
        self.assertEqual(shape.integer(18), 5)
        self.assertEqual(shape.integer(2), 7)

    def test_palette_paint_clears_rgb_override(self):
        self.template()
        self.apply([{"op": "create", "template": "block", "name": "test", "color": "#aabbcc"}, {"op": "paint", "name": "test", "color": 19}])
        _, snapshot, scene = self.workspace.load("arena")
        view = scene.find(next(iter(snapshot["objects"])))
        self.assertFalse(view.message(14).has(2))
        self.assertEqual(view.message(12).message(1).message(1).integer(2), 19)

    def test_quaternion_uses_wxyz_and_unity_rotation_order(self):
        q = quaternion([0, 90, 0])
        self.assertAlmostEqual(q.floating(1), 2 ** -.5)
        self.assertAlmostEqual(q.floating(3), 2 ** -.5)
        self.assertAlmostEqual(q.floating(2), 0)
        self.assertAlmostEqual(q.floating(4), 0)

    def test_preview_does_not_embed_unescaped_markup(self):
        self.template()
        self.apply([{"op": "create", "template": "block", "name": "</script><img src=x onerror=alert(1)>"}])
        result = self.builder.call("recflare_preview", {"project": "arena"})
        text = Path(result["path"]).read_text()
        self.assertNotIn("<img", text)
        self.assertIn("Not a game screenshot", text)

    def test_argument_validation_prevents_bool_ids_nonfinite_and_unknown_fields(self):
        for args in [{"project": "arena", "expected_revision": self.revision(), "operations": [{"op": "create", "name": "bad", "template": "x", "position": [float("nan"), 0, 0]}]}, {"project": "arena", "extra": "unexpected"}]:
            with self.assertRaises(ValueError):
                self.builder.call("recflare_apply_plan" if "operations" in args else "recflare_get_project", args)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        workspace = Workspace(self.tmp.name)
        self.builder = Builder(workspace, Api(None, workspace))

    def test_handshake_tools_and_notifications(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        self.assertEqual(dispatch(self.builder, request)["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(dispatch(self.builder, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = dispatch(self.builder, {"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
        self.assertEqual(len(tools["result"]["tools"]), len(TOOLS))
        self.assertIsNone(dispatch(self.builder, {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "recflare_status"}}))

    def test_malformed_messages_and_null_ids(self):
        for request in [[], {}, {"jsonrpc": "2.0", "id": None, "method": "tools/list"}, {"jsonrpc": "2.0", "id": True, "method": "tools/list"}]:
            self.assertEqual(dispatch(self.builder, request)["error"]["code"], -32600)
        self.assertEqual(dispatch(self.builder, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "nonexistent"}})["error"]["code"], -32602)

    def test_real_stdio_subprocess_has_only_protocol_output(self):
        script = Path(__file__).resolve().parents[1] / "server.py"
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "recflare_status", "arguments": {}}}]
        process = subprocess.run([sys.executable, str(script), "--workspace", self.tmp.name], input="".join(json.dumps(r) + "\n" for r in requests), text=True, capture_output=True, timeout=15, check=True)
        rows = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[1]["result"]["isError"])
        self.assertEqual(process.stderr, "")

    def test_parse_error_and_tool_error_are_distinct(self):
        source = io.BytesIO(b"not json\n" + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "recflare_get_project", "arguments": {"project": "missing"}}}).encode() + b"\n")
        target = io.BytesIO()
        serve(self.builder, source, target)
        rows = [json.loads(line) for line in target.getvalue().splitlines()]
        self.assertEqual(rows[0]["error"]["code"], -32700)
        self.assertTrue(rows[1]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
