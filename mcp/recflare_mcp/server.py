"""Building-focused MCP server over newline-delimited JSON-RPC on stdio."""
from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
from pathlib import Path
import re
import sys

from . import __version__
from .api import Api, RemoteError
from .preview import render
from .scene import Scene
from .workspace import Workspace, atomic_write

PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MAX_LINE = 2 * 1024 * 1024


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or list(properties), "additionalProperties": False}


STRING = {"type": "string", "minLength": 1, "maxLength": 2000}
PROJECT = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"}
REVISION = {"type": "string", "pattern": r"^[0-9a-f]{32}$"}
INTEGER = {"type": "integer", "minimum": 1}
VECTOR = {"type": "array", "items": {"type": "number", "minimum": -100000, "maximum": 100000}, "minItems": 3, "maxItems": 3}
SIZE = {"type": "array", "items": {"type": "number", "exclusiveMinimum": 0, "maximum": 100000}, "minItems": 3, "maxItems": 3}
COLOR = {"oneOf": [{"type": "integer", "minimum": 0, "maximum": 65535}, {"type": "string", "pattern": r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$"}]}
OPERATION = obj({
    "op": {"type": "string", "enum": ["create", "repeat", "grid", "transform", "paint", "delete"]},
    "template": STRING, "name": {"type": "string", "minLength": 1, "maxLength": 100},
    "position": VECTOR, "rotation": VECTOR, "size": SIZE, "color": COLOR,
    "material": {"type": "integer", "minimum": 0, "maximum": 65535},
    "count": {"type": "integer", "minimum": 1, "maximum": 2000},
    "step": VECTOR, "rotation_step": VECTOR,
    "counts": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 2000}, "minItems": 3, "maxItems": 3},
    "spacing": VECTOR,
}, ["op", "name"])


def definition(name: str, description: str, schema: dict, *, read: bool = False, remote: bool = False, destructive: bool = False) -> dict:
    return {"name": name, "description": description, "inputSchema": schema,
            "annotations": {"readOnlyHint": read, "destructiveHint": destructive, "openWorldHint": remote}}


TOOLS = [
    definition("recflare_status", "Describe this builder, its workspace and supported workflow. No game connection is implied. Read this first.", obj({}, []), read=True),
    definition("recflare_list_projects", "List local building projects and room files copied into workspace/imports.", obj({}, []), read=True),
    definition("recflare_open_room", "Import an existing client-saved .room from workspace/imports into a new backed-up project. No server access. Does not create a valid room from an empty file.", obj({"project": PROJECT, "source_file": STRING})),
    definition("recflare_get_project", "Read revision, templates, scene summary and upload status. Use the returned revision for every local edit.", obj({"project": PROJECT}), read=True),
    definition("recflare_list_objects", "Inspect root objects and seed shapes. Nested/Rooms 2 objects cannot be edited. Returns raw palette/material IDs already used by the game.", obj({"project": PROJECT, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "shapes_only": {"type": "boolean"}}, ["project"]), read=True),
    definition("recflare_register_template", "Register one centered, unrotated, plain Maker Pen shape as a reusable building piece. dimensions are the shape's measured local width, height and depth in meters; do not guess shape/prefab IDs. A one-meter cube is a useful first seed.", obj({"project": PROJECT, "expected_revision": REVISION, "template": PROJECT, "object_id": STRING, "shape_id": STRING, "dimensions": SIZE}, ["project", "expected_revision", "template", "object_id", "dimensions"])),
    definition("recflare_apply_plan", "Atomically build or edit up to 2000 pieces using registered templates. Positions are room-space meters; rotation is Unity Euler degrees; size is width/height/depth relative to template measurements. create, repeat and grid make independent objects; transform/paint/delete address names created by this MCP. Circuits and original objects remain untouched. No upload occurs.", obj({"project": PROJECT, "expected_revision": REVISION, "operations": {"type": "array", "items": OPERATION, "minItems": 1, "maxItems": 500}, "label": {"type": "string", "maxLength": 300}}, ["project", "expected_revision", "operations"]), destructive=True),
    definition("recflare_history", "List local immutable backups, newest first.", obj({"project": PROJECT}), read=True),
    definition("recflare_restore_revision", "Restore an earlier local snapshot as a new revision without erasing history. Does not restore the online room.", obj({"project": PROJECT, "expected_revision": REVISION, "restore_revision": REVISION}), destructive=True),
    definition("recflare_export_room", "Export the current complete scene as a .room file for manual RecFlare website upload. The original import and all backups remain available.", obj({"project": PROJECT, "expected_revision": REVISION})),
    definition("recflare_preview", "Write a local interactive HTML bounding-box layout preview of MCP-created pieces. This is not a game screenshot or proof of game loading.", obj({"project": PROJECT})),
    definition("recflare_list_rooms", "List rooms the authenticated RecFlare account owns or co-owns. Requires --server and a local login.", obj({}, []), read=True, remote=True),
    definition("recflare_download_room", "Download a published or staged room save into a NEW local building project, backing it up and binding later uploads to this room. Use an ordinary test room with a plain Maker Pen seed shape.", obj({"project": PROJECT, "room_id": INTEGER, "subroom_id": INTEGER, "source": {"type": "string", "enum": ["published", "staged"]}}, ["project", "room_id", "subroom_id"]), remote=True),
    definition("recflare_stage_room", "Upload the exact local revision as a NEW STAGED save through existing RecFlare APIs. Requires an online-origin project and unchanged server save IDs. Refuses dorms because they auto-publish. Does not update a running game instance or publish the room.", obj({"project": PROJECT, "expected_revision": REVISION, "description": {"type": "string", "maxLength": 300}}, ["project", "expected_revision"]), remote=True),
    definition("recflare_check_upload", "Reconcile an uncertain upload against server save history without retrying the save. Updates the local journal only after verification.", obj({"project": PROJECT}), remote=True),
    definition("recflare_publish_room", "Publish this project's exact verified staged save using the existing creator-only API. Changes what NEW room instances load. Rejoining a fresh instance is required. Use only when the user wants this revision to go live.", obj({"project": PROJECT, "expected_revision": REVISION, "expected_save_id": INTEGER}), remote=True, destructive=True),
]


def validate(value, schema: dict, path: str = "arguments") -> None:
    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            try:
                validate(value, option, path)
                matches += 1
            except ValueError:
                pass
        if matches != 1:
            raise ValueError(f"{path} does not match the permitted types")
        return
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": isinstance(value, bool),
             "integer": isinstance(value, int) and not isinstance(value, bool),
             "number": isinstance(value, (int, float)) and not isinstance(value, bool)}
    if kind and not valid.get(kind, False):
        raise ValueError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"Missing {path}.{key}")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key not in properties:
                if schema.get("additionalProperties") is False:
                    raise ValueError(f"Unexpected {path}.{key}")
            else:
                validate(item, properties[key], path + "." + key)
    elif kind == "array":
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1_000_000):
            raise ValueError(f"{path} has the wrong number of items")
        for i, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{path}[{i}]")
    elif kind == "string":
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 1_000_000):
            raise ValueError(f"{path} has the wrong length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"{path} has an invalid format")
    elif kind in ("integer", "number"):
        import math
        if not math.isfinite(value):
            raise ValueError(f"{path} must be finite")
        if value < schema.get("minimum", float("-inf")) or value > schema.get("maximum", float("inf")) or value <= schema.get("exclusiveMinimum", float("-inf")):
            raise ValueError(f"{path} is outside the permitted range")


class Builder:
    def __init__(self, workspace: Workspace, api: Api):
        self.workspace, self.api = workspace, api

    def call(self, name: str, args: dict) -> dict:
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            raise ValueError("Unknown tool")
        validate(args, tool["inputSchema"])
        w = self.workspace
        project = args.get("project")
        if name == "recflare_status":
            return {"version": __version__, "transport": "stdio", "workspace": str(w.root), "server": self.api.server,
                    "login_available": (w.root / "auth.json").is_file() or bool(os.environ.get("RECFLARE_ACCESS_TOKEN")),
                    "workflow": "Edit client-saved room files, stage, then explicitly publish and rejoin a fresh instance",
                    "live_game_connection": False, "game_load_verified": False, "circuits": "preserved, not edited", "instructions": "Start with recflare_open_room or recflare_download_room, inspect objects, register a measured seed shape, then apply a plan. Keep uploads in a test room until game loading is verified"}
        if name == "recflare_list_projects":
            return {"projects": sorted(p.name for p in (w.root / "projects").iterdir() if p.is_dir() and (p / "project.json").is_file())[:200], "imports": sorted(p.name for p in (w.root / "imports").iterdir() if p.is_file() and p.suffix.lower() in (".room", ".binpb"))[:200]}
        if name == "recflare_open_room":
            source = w.import_path(args["source_file"])
            return w.create(project, source.read_bytes(), source=source.name)
        if name == "recflare_get_project":
            return w.info(project)
        if name == "recflare_list_objects":
            manifest, snapshot, scene = w.load(project)
            views = [v for v in scene.views if not args.get("shapes_only", True) or v.has(12)]
            offset, limit = args.get("offset", 0), args.get("limit", 25)
            rows = [Scene.inspect(v) for v in views[offset:offset + limit]]
            for row in rows:
                row["mcp_object"] = snapshot["objects"].get(row["id"])
            return {"project": project, "revision": manifest["revision"], "total": len(views), "objects": rows}
        if name in ("recflare_register_template", "recflare_apply_plan", "recflare_restore_revision"):
            with w.lock(project):
                manifest, snapshot, scene = w.load(project)
                w.expect(manifest, args["expected_revision"])
                if name == "recflare_register_template":
                    template = args["template"]
                    if template in snapshot["templates"]:
                        raise ValueError("Template already exists; register it under a new name")
                    snapshot["templates"][template] = scene.make_template(args["object_id"], args.get("shape_id"), args["dimensions"])
                    label, changes = f"Registered template {template}", []
                elif name == "recflare_apply_plan":
                    snapshot["objects"], changes = scene.apply(args["operations"], snapshot["templates"], snapshot["objects"])
                    label = args.get("label", "Applied building plan")
                else:
                    # Only revisions in this project's immutable history are valid.
                    old = w.read_snapshot(project, args["restore_revision"])
                    digest = old["scene_sha256"]
                    if not re.fullmatch(r"[0-9a-f]{64}", digest):
                        raise ValueError("Invalid snapshot hash")
                    scene = Scene((w.project_path(project) / "scenes" / f"{digest}.room").read_bytes())
                    snapshot = copy.deepcopy(old)
                    label, changes = f"Restored {args['restore_revision']}", []
                revision = w.commit(project, manifest, snapshot, scene.encode(), label)
            return {"project": project, "revision": revision, "changes": changes[:100], "total_changes": len(changes), "created_objects": len(snapshot["objects"]), "uploaded": False}
        if name == "recflare_history":
            return {"history": w.history(project)}
        if name in ("recflare_export_room", "recflare_preview"):
            manifest, snapshot, scene = w.load(project)
            if name == "recflare_export_room":
                w.expect(manifest, args["expected_revision"])
                scene.require_editable()
                suffix, data = "room", scene.encode()
            else:
                suffix, data = "html", render(snapshot["objects"])
            path = w.root / "exports" / f"{project}-{manifest['revision']}.{suffix}"
            atomic_write(path, data)
            return {"path": str(path), "revision": manifest["revision"], "game_load_verified": False, "approximate_geometry": suffix == "html"}
        if name == "recflare_list_rooms":
            rooms = self.api.call("Rooms", "GET", "/rooms/ownedby/me")
            if not isinstance(rooms, list):
                raise RemoteError("Unexpected room list response")
            return {"rooms": [{"room_id": r.get("RoomId"), "name": r.get("Name"), "subrooms": [{"subroom_id": s.get("SubRoomId"), "name": s.get("Name")} for s in r.get("SubRooms", [])]} for r in rooms[:200]]}
        if name == "recflare_download_room":
            return self.api.download(project, args["room_id"], args["subroom_id"], args.get("source", "published"))
        if name == "recflare_stage_room":
            return self.api.stage(project, args["expected_revision"], args.get("description", "Built with RecFlare MCP"))
        if name == "recflare_check_upload":
            return self.api.check_upload(project)
        if name == "recflare_publish_room":
            return self.api.publish(project, args["expected_revision"], args["expected_save_id"])
        raise ValueError("Tool is not implemented")


def error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def dispatch(builder: Builder, request) -> dict | None:
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
        return error(None, -32600, "Invalid JSON-RPC request")
    request_id = request.get("id")
    if "id" in request and (request_id is None or isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
        return error(None, -32600, "Invalid request ID")
    # Notifications must never execute mutations, even if they name tools/call.
    if "id" not in request:
        return None
    method, params = request["method"], request.get("params", {})
    if not isinstance(params, dict):
        return error(request_id, -32602, "params must be an object")
    if method == "initialize":
        version = params.get("protocolVersion")
        result = {"protocolVersion": version if version in PROTOCOLS else PROTOCOLS[-1], "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "recflare-builder", "version": __version__}, "instructions": "Building-focused saved-room editor. Read recflare_status first. Changes are local until explicitly staged/published. Circuits are preserved. A game reload and real-client validation are required"}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        if not isinstance(params.get("name"), str) or not isinstance(params.get("arguments", {}), dict):
            return error(request_id, -32602, "Tool name and argument object are required")
        if not any(t["name"] == params["name"] for t in TOOLS):
            return error(request_id, -32602, "Unknown tool")
        try:
            value = builder.call(params["name"], params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(value, indent=2, allow_nan=False)}], "structuredContent": value, "isError": False}
        except (ValueError, RuntimeError, OSError, KeyError) as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception:
            print("Unexpected builder exception; no automatic retry", file=sys.stderr)
            result = {"content": [{"type": "text", "text": "Unexpected internal error. Inspect project history and upload status before retrying"}], "isError": True}
    else:
        return error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(builder: Builder, source, target) -> None:
    while True:
        line = source.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            response = error(None, -32700, "Message exceeds 2 MiB; connection closed")
            target.write((json.dumps(response) + "\n").encode())
            target.flush()
            break
        try:
            def reject_constant(value):
                raise ValueError("Non-finite JSON number")
            request = json.loads(line, parse_constant=reject_constant)
            response = dispatch(builder, request)
        except (ValueError, UnicodeError, RecursionError):
            response = error(None, -32700, "Invalid JSON")
        if response is not None:
            target.write((json.dumps(response, allow_nan=False) + "\n").encode("utf-8"))
            target.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="RecFlare building MCP: client-side saved-room editing, no backend changes")
    parser.add_argument("mode", nargs="?", choices=["serve", "login", "doctor"], default="serve")
    parser.add_argument("--workspace", required=True, help="Directory for imports, projects, backups and exports")
    parser.add_argument("--server", help="Name-server URL, e.g. https://ns.recflare.net; omit for offline building")
    parser.add_argument("--username", help="Used only by the local interactive login command")
    options = parser.parse_args()
    workspace = Workspace(options.workspace)
    api = Api(options.server, workspace)
    if options.mode == "login":
        if not options.server:
            parser.error("login requires --server")
        username = options.username or input("RecFlare username: ").strip()
        password = getpass.getpass("RecFlare password (not shown or sent to AI): ")
        try:
            api.login(username, password)
        except (RemoteError, ValueError) as exc:
            parser.exit(1, f"Sign-in failed: {exc}\n")
        finally:
            password = ""
        print("Signed in. Credentials were saved locally; no room was changed.")
    elif options.mode == "doctor":
        print(json.dumps(Builder(workspace, api).call("recflare_status", {}), indent=2))
    else:
        serve(Builder(workspace, api), sys.stdin.buffer, sys.stdout.buffer)
