"""Independent SDK/schema checks. Optional locally, required in the CI job."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from common import OBJECT_ID, fixture
from recflare_mcp.scene import Scene


def available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


HAS_SDK, HAS_PROTOBUF = available("mcp"), available("google.protobuf")
if os.environ.get("REQUIRE_EXTERNAL") == "1" and not (HAS_SDK and HAS_PROTOBUF):
    raise ImportError("CI requires the official MCP SDK and protobuf test dependencies")


@unittest.skipUnless(HAS_PROTOBUF, "protobuf is a CI-only independent test dependency")
class ProtobufReferenceTests(unittest.TestCase):
    def test_official_protobuf_decodes_generated_geometry(self):
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

        # A small independent typed descriptor using the published field map.
        # No implementation functions from wire.py are used by this decoder.
        file = descriptor_pb2.FileDescriptorProto(name="recflare_reference.proto", package="reference", syntax="proto3")
        FLOAT, BYTES, MESSAGE, UINT32, BOOL = 2, 12, 11, 13, 8
        specs = {
            "Vector": [("x", 1, FLOAT), ("y", 2, FLOAT), ("z", 3, FLOAT)],
            "Quaternion": [("w", 1, FLOAT), ("x", 2, FLOAT), ("y", 3, FLOAT), ("z", 4, FLOAT)],
            "Color": [("r", 1, FLOAT), ("g", 2, FLOAT), ("b", 3, FLOAT), ("a", 4, FLOAT)],
            "Creation": [("color_override", 2, MESSAGE, "Color"), ("is_frozen", 4, BOOL)],
            "Transform": [("position", 1, MESSAGE, "Vector"), ("scale", 5, FLOAT), ("quaternion_rotation", 6, MESSAGE, "Quaternion")],
            "Deformation": [("deformation", 1, MESSAGE, "Vector")],
            "Shape": [("shape_type", 1, UINT32), ("shape_color", 2, UINT32), ("current_local_position", 7, MESSAGE, "Vector"), ("current_local_rotation", 8, MESSAGE, "Vector"), ("id", 12, BYTES), ("scale", 13, FLOAT), ("shape_material", 18, UINT32)],
            "Collection": [("shapes", 1, MESSAGE, "Shape", True)],
            "Container": [("shape_collection", 1, MESSAGE, "Collection")],
            "View": [("id", 1, BYTES), ("transform", 10, MESSAGE, "Transform"), ("shape_container_data", 12, MESSAGE, "Container"), ("creation_object_data", 14, MESSAGE, "Creation"), ("sandbox_deformation_data", 53, MESSAGE, "Deformation")],
            "Room": [("legacy_version", 1, UINT32), ("persistence_views", 2, MESSAGE, "View", True), ("circuit_data", 18, BYTES), ("circuit_v2_data", 28, BYTES), ("version", 30, UINT32)],
        }
        for name, fields in specs.items():
            message = file.message_type.add(name=name)
            for spec in fields:
                field = message.field.add(name=spec[0], number=spec[1], type=spec[2], label=3 if len(spec) > 4 and spec[4] else 1)
                if spec[2] == MESSAGE:
                    field.type_name = ".reference." + spec[3]
        pool = descriptor_pool.DescriptorPool()
        pool.Add(file)
        Room = message_factory.GetMessageClass(pool.FindMessageTypeByName("reference.Room"))
        original = Room.FromString(fixture())
        self.assertEqual(original.persistence_views[0].transform.position.x, 10)
        scene = Scene(fixture())
        template = scene.make_template(OBJECT_ID, None, [1, 1, 1])
        labels, _ = scene.apply([{"op": "create", "template": "block", "name": "wall", "position": [2, 3, 4], "rotation": [0, 90, 0], "size": [10, .5, 7], "color": "#804020", "material": 9}], {"block": template}, {})
        decoded = Room.FromString(scene.encode())
        self.assertEqual(len(decoded.persistence_views), 2)
        view = decoded.persistence_views[1]
        self.assertEqual((view.transform.position.x, view.transform.position.y, view.transform.position.z), (2, 3, 4))
        deformation = view.sandbox_deformation_data.deformation
        self.assertEqual((deformation.x, deformation.y, deformation.z), (10, .5, 7))
        self.assertAlmostEqual(view.transform.quaternion_rotation.y, 2 ** -.5)
        self.assertEqual(view.shape_container_data.shape_collection.shapes[0].shape_material, 9)
        self.assertAlmostEqual(view.creation_object_data.color_override.r, 128 / 255)
        self.assertEqual(decoded.circuit_data, original.circuit_data)
        self.assertEqual(decoded.circuit_v2_data, original.circuit_v2_data)
        self.assertEqual(decoded.version, original.version)


@unittest.skipUnless(HAS_SDK, "official MCP SDK is a CI-only independent test dependency")
class McpSdkTests(unittest.TestCase):
    def test_official_sdk_builds_and_exports_the_example(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "imports").mkdir()
            (root / "imports" / "seed.room").write_bytes(fixture())
            source = Path(__file__).resolve().parents[1]
            params = StdioServerParameters(command=sys.executable, args=[str(source / "server.py"), "--workspace", str(root)])

            async def run():
                async with stdio_client(params) as (reader, writer):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()
                        tools = await client.list_tools()
                        self.assertGreaterEqual(len(tools.tools), 16)

                        async def call(name, arguments):
                            result = await client.call_tool(name, arguments=arguments)
                            self.assertFalse(result.isError, result.content)
                            return json.loads(result.content[0].text)

                        opened = await call("recflare_open_room", {"project": "example", "source_file": "seed.room"})
                        template = await call("recflare_register_template", {"project": "example", "expected_revision": opened["revision"], "template": "block", "object_id": OBJECT_ID, "dimensions": [1, 1, 1]})
                        plan = json.loads((source / "examples" / "courtyard.json").read_text())
                        built = await call("recflare_apply_plan", {"project": "example", "expected_revision": template["revision"], "operations": plan["operations"]})
                        self.assertEqual(built["created_objects"], 22)
                        exported = await call("recflare_export_room", {"project": "example", "expected_revision": built["revision"]})
                        self.assertEqual(len(Scene(Path(exported["path"]).read_bytes()).views), 23)

            asyncio.run(run())
