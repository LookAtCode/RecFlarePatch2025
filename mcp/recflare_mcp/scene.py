"""Building edits for legacy/Rooms 1 PersistedRoomData.

Field numbers are documented in docs/format.md. No guessed prefab or shape enum
IDs: new geometry is made from a shape the target client has already saved.
Circuits and other unrelated room fields are never decoded or regenerated.
"""
from __future__ import annotations

import base64
import copy
import math
import uuid

from .wire import Field, FormatError, Message, read_vector, vector

MAX_OBJECTS = 20_000
MAX_PLAN_OBJECTS = 2_000


def vec(value, name: str, *, positive: bool = False) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{name} must contain exactly three numbers")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in value):
        raise ValueError(f"{name} must contain numbers")
    result = [float(x) for x in value]
    if any(not math.isfinite(x) or abs(x) > 100_000 for x in result):
        raise ValueError(f"{name} must be finite and within +/-100000")
    if positive and any(x <= 0 for x in result):
        raise ValueError(f"{name} must be positive")
    return result


def guid(value: bytes) -> str:
    return str(uuid.UUID(bytes_le=value)) if len(value) == 16 else value.hex()


def quaternion(euler: list[float]) -> Message:
    # Unity Quaternion.Euler applies Z, then X, then Y: q = qY * qX * qZ.
    x, y, z = [math.radians(v) / 2 for v in euler]
    sx, sy, sz = math.sin(x), math.sin(y), math.sin(z)
    cx, cy, cz = math.cos(x), math.cos(y), math.cos(z)
    w = cy * cx * cz + sy * sx * sz
    qx = cy * sx * cz + sy * cx * sz
    qy = sy * cx * cz - cy * sx * sz
    qz = cy * cx * sz - sy * sx * cz
    result = Message()
    # QuaternionData uses w=1, x=2, y=3, z=4 (not Vector4 order).
    for number, value in enumerate([w, qx, qy, qz], 1):
        result.set_float(number, value)
    return result


def strict_keys(value: dict, allowed: set[str]) -> None:
    extra = set(value) - allowed
    if extra:
        raise ValueError("Unknown fields: " + ", ".join(sorted(extra)))


class Scene:
    def __init__(self, data: bytes):
        if not data:
            raise FormatError("An existing client-saved .room is required; an empty file is not a room")
        self.root = Message.parse(data)
        if not self.root.has(1) and not self.root.has(30):
            raise FormatError("This file has no room version fields; it is not a supported room save")
        self.views = [Message.parse(f.value) for f in self.root.all(2, 2)]
        if len(self.views) > MAX_OBJECTS:
            raise FormatError("Room contains too many root objects")
        ids = [v.blob(1) for v in self.views]
        if any(len(i) != 16 for i in ids) or len(ids) != len(set(ids)):
            raise FormatError("Root object IDs must be unique 16-byte GUIDs")
        self.original_views = {v.blob(1): f for v, f in zip(self.views, self.root.all(2, 2))}

    def require_editable(self) -> None:
        if self.root.blob(29):
            raise ValueError("Rooms 2/object-model scenes are read-only: their transforms require a different editor")

    def encode(self) -> bytes:
        current = {v.blob(1): v for v in self.views}
        fields = []
        for field in self.root.fields:
            if field.number != 2:
                fields.append(field)
                continue
            key = Message.parse(field.value).blob(1)
            if key in current:
                data = current[key].encode()
                fields.append(field if data == field.value else Field.make(2, 2, data))
        for view in self.views:
            if view.blob(1) not in self.original_views:
                fields.append(Field.make(2, 2, view.encode()))
        return Message(fields).encode()

    def find(self, object_id: str) -> Message:
        matches = [v for v in self.views if guid(v.blob(1)) == object_id]
        if not matches:
            raise ValueError(f"Object not found: {object_id}")
        return matches[0]

    def summary(self) -> dict:
        return {
            "room_version": self.root.integer(30),
            "legacy_version": self.root.integer(1),
            "object_model_present": bool(self.root.blob(29)),
            "editable": not bool(self.root.blob(29)),
            "root_objects": len(self.views),
            "shape_containers": sum(v.has(12) for v in self.views),
            "shapes": sum(len(v.message(12).message(1).all(1, 2)) for v in self.views if v.has(12)),
            "circuits_preserved": True,
        }

    @staticmethod
    def inspect(view: Message) -> dict:
        transform = view.message(10)
        q = transform.message(6)
        shapes = []
        for field in view.message(12).message(1).all(1, 2):
            s = Message.parse(field.value)
            shapes.append({
                "id": guid(s.blob(12)), "type_id": s.integer(1),
                "palette_color_id": s.integer(2), "material_id": s.integer(18),
                "local_position": read_vector(s.message(7)),
                "local_rotation_degrees": read_vector(s.message(8)),
                "scale": s.floating(13, 1), "is_curve": s.has(17),
            })
        return {
            "id": guid(view.blob(1)),
            "position": read_vector(transform.message(1)),
            "rotation_quaternion_wxyz": [q.floating(i, 1 if i == 1 else 0) for i in range(1, 5)],
            "uniform_scale": transform.floating(5, 1),
            "deformation": read_vector(view.message(53).message(1), [1, 1, 1]),
            "prefab_id": guid(view.message(11).blob(1)),
            "child_views": len(view.all(3, 2)),
            "shape_count": len(shapes), "shapes": shapes[:100],
        }

    def make_template(self, object_id: str, shape_id: str | None, dimensions: list[float]) -> dict:
        self.require_editable()
        dimensions = vec(dimensions, "dimensions", positive=True)
        source = self.find(object_id)
        if not source.has(12):
            raise ValueError("Choose a Maker Pen shape container, not a prop, circuit, or Studio object")
        if source.blob(9) or source.has(100) and source.integer(100) != (1 << 64) - 1:
            raise ValueError("Object-model indexed objects cannot be used as building templates")
        if source.message(14).blob(5):
            raise ValueError("Use your own plain shape, not a shape linked to an invention")
        shapes = [Message.parse(f.value) for f in source.message(12).message(1).all(1, 2)]
        if shape_id:
            shapes = [s for s in shapes if guid(s.blob(12)) == shape_id]
        if len(shapes) != 1:
            raise ValueError("Select exactly one shape using shape_id, or save a container holding a single shape")
        shape = shapes[0]
        if shape.has(17):
            raise ValueError("Curve templates are not supported; use an ordinary solid shape")
        if shape.blob(7) and any(abs(v) > 1e-5 for v in read_vector(shape.message(7))):
            raise ValueError("The seed shape must be centered in its container (local_position 0,0,0)")
        if any(abs(v) > 1e-5 for v in read_vector(shape.message(8))):
            raise ValueError("Use an unrotated seed shape; local_rotation must be 0,0,0")
        # A new independent object gets only known geometric components, not copied
        # circuit/child/group/invention references or an encoded entity index.
        # A serialized -1 entity index is an explicit "not an OM entity" sentinel,
        # not a link. Preserve it when the target client wrote one.
        allowed = {10, 11, 12, 14, 36, 53, 100}
        template = Message([f for f in source.fields if f.number in allowed])
        container = template.message(12)
        collection = container.message(1)
        collection.replace(1, [Field.make(1, 2, shape.encode())])
        container.set_message(1, collection)
        template.set_message(12, container)
        t = template.message(10)
        if abs(t.floating(5, 1)) < 1e-6:
            t.set_float(5, 1)
        t.set_message(1, vector([0, 0, 0]))
        t.set_message(6, quaternion([0, 0, 0]))
        t.replace(2, [])
        template.set_message(10, t)
        return {
            "data": base64.b64encode(template.encode()).decode("ascii"),
            "dimensions": dimensions, "source_object_id": object_id,
            "source_shape_id": guid(shape.blob(12)), "shape_type_id": shape.integer(1),
            "palette_color_id": shape.integer(2), "material_id": shape.integer(18),
        }

    def apply(self, operations: list[dict], templates: dict, labels: dict) -> tuple[dict, list[dict]]:
        self.require_editable()
        if not isinstance(operations, list) or not operations or len(operations) > 500:
            raise ValueError("operations must contain between 1 and 500 operations")
        labels = copy.deepcopy(labels)
        changes = []
        names = {info["name"]: key for key, info in labels.items()}

        def named(name) -> str:
            if not isinstance(name, str) or name not in names:
                raise ValueError(f"Unknown MCP-created object name: {name}")
            return names[name]

        def create(op: dict) -> None:
            strict_keys(op, {"op", "template", "name", "position", "rotation", "size", "color", "material"})
            name = op.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 100 or name in names:
                raise ValueError("Each created object needs a unique nonempty name of at most 100 characters")
            if op.get("template") not in templates:
                raise ValueError(f"Unknown template: {op.get('template')}")
            if len(self.views) >= MAX_OBJECTS or len(changes) >= MAX_PLAN_OBJECTS:
                raise ValueError("Building object limit exceeded")
            seed = templates[op["template"]]
            view = Message.parse(base64.b64decode(seed["data"], validate=True))
            object_id = uuid.uuid4().bytes_le
            view.set_blob(1, object_id)
            container = view.message(12)
            collection = container.message(1)
            updated = []
            for f in collection.all(1, 2):
                shape = Message.parse(f.value)
                shape.set_blob(12, uuid.uuid4().bytes_le)
                updated.append(Field.make(1, 2, shape.encode()))
            collection.replace(1, updated)
            container.set_message(1, collection)
            view.set_message(12, container)
            info = {
                "name": name, "template": op["template"],
                "position": vec(op.get("position", [0, 0, 0]), "position"),
                "rotation": vec(op.get("rotation", [0, 0, 0]), "rotation"),
                "size": vec(op.get("size", seed["dimensions"]), "size", positive=True),
                "color": seed["palette_color_id"], "material": seed["material_id"],
            }
            self._set_transform(view, seed, info)
            self._paint(view, info, op)
            self.views.append(view)
            key = guid(object_id)
            labels[key], names[name] = info, key
            changes.append({"op": "create", "name": name, "id": key})

        for op in operations:
            if not isinstance(op, dict):
                raise ValueError("Each operation must be an object")
            kind = op.get("op")
            if kind == "create":
                create(op)
            elif kind in ("repeat", "grid"):
                strict_keys(op, {"op", "template", "name", "position", "rotation", "size", "color", "material", "count", "step", "rotation_step", "counts", "spacing"})
                origin = vec(op.get("position", [0, 0, 0]), "position")
                rotation = vec(op.get("rotation", [0, 0, 0]), "rotation")
                if kind == "repeat":
                    count = op.get("count")
                    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_PLAN_OBJECTS:
                        raise ValueError("repeat count must be 1..2000")
                    step = vec(op.get("step", [1, 0, 0]), "step")
                    turn = vec(op.get("rotation_step", [0, 0, 0]), "rotation_step")
                    placements = [([origin[j] + i * step[j] for j in range(3)], [rotation[j] + i * turn[j] for j in range(3)]) for i in range(count)]
                else:
                    counts = op.get("counts")
                    if not isinstance(counts, list) or len(counts) != 3 or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in counts):
                        raise ValueError("grid counts must contain three positive integers")
                    if math.prod(counts) > MAX_PLAN_OBJECTS:
                        raise ValueError("grid exceeds 2000 objects")
                    spacing = vec(op.get("spacing", [1, 1, 1]), "spacing")
                    placements = [([origin[0] + x * spacing[0], origin[1] + y * spacing[1], origin[2] + z * spacing[2]], rotation) for x in range(counts[0]) for y in range(counts[1]) for z in range(counts[2])]
                for i, (position, turn) in enumerate(placements):
                    single = {k: v for k, v in op.items() if k not in {"count", "step", "rotation_step", "counts", "spacing"}}
                    single.update(op="create", name=f"{op.get('name', '')}_{i + 1:04d}", position=position, rotation=turn)
                    create(single)
            elif kind in ("transform", "paint", "delete"):
                allowed = {"op", "name"}
                if kind == "transform":
                    allowed |= {"position", "rotation", "size"}
                if kind == "paint":
                    allowed |= {"color", "material"}
                strict_keys(op, allowed)
                key = named(op.get("name"))
                view = self.find(key)
                if kind == "delete":
                    self.views.remove(view)
                    del names[labels[key]["name"]]
                    del labels[key]
                else:
                    info = labels[key]
                    if kind == "transform":
                        for field in ("position", "rotation", "size"):
                            if field in op:
                                info[field] = vec(op[field], field, positive=field == "size")
                        self._set_transform(view, templates[info["template"]], info)
                    self._paint(view, info, op)
                changes.append({"op": kind, "name": op["name"], "id": key})
            else:
                raise ValueError(f"Unknown operation: {kind}")
            if len(changes) > MAX_PLAN_OBJECTS:
                raise ValueError("A plan may change at most 2000 objects")
        return labels, changes

    @staticmethod
    def _set_transform(view: Message, seed: dict, info: dict) -> None:
        transform = view.message(10)
        transform.set_message(1, vector(info["position"], transform.message(1)))
        transform.set_message(6, quaternion(info["rotation"]))
        transform.replace(2, [])
        view.set_message(10, transform)
        original = Message.parse(base64.b64decode(seed["data"], validate=True))
        scale = read_vector(original.message(53).message(1), [1, 1, 1])
        values = [scale[i] * info["size"][i] / seed["dimensions"][i] for i in range(3)]
        deformation = view.message(53)
        deformation.set_message(1, vector(values, deformation.message(1)))
        view.set_message(53, deformation)

    @staticmethod
    def _paint(view: Message, info: dict, op: dict) -> None:
        if "color" not in op and "material" not in op:
            return
        hex_color = op.get("color")
        if isinstance(hex_color, str):
            import re
            if not re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", hex_color):
                raise ValueError("Color must be #RRGGBB, #RRGGBBAA, or a palette ID")
            rgba = bytes.fromhex(hex_color[1:])
            if len(rgba) == 3:
                rgba += b"\xff"
            override = Message()
            for i, channel in enumerate(rgba, 1):
                override.set_float(i, channel / 255)
            creation = view.message(14)
            creation.set_message(2, override)
            view.set_message(14, creation)
            info["color"] = hex_color
        for key in ("color", "material"):
            if key in op:
                value = op[key]
                if key == "color" and isinstance(value, str):
                    continue
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
                    raise ValueError(f"{key} must be a palette ID between 0 and 65535")
                info[key] = value
                if key == "color":
                    creation = view.message(14)
                    creation.replace(2, [])
                    view.set_message(14, creation)
        container = view.message(12)
        collection = container.message(1)
        fields = []
        for field in collection.all(1, 2):
            shape = Message.parse(field.value)
            if "color" in op and not isinstance(op["color"], str):
                shape.set_int(2, op["color"])
            if "material" in op:
                shape.set_int(18, op["material"])
            fields.append(Field.make(1, 2, shape.encode()))
        collection.replace(1, fields)
        container.set_message(1, collection)
        view.set_message(12, container)
