"""Synthetic protocol fixture. It is NOT captured from or validated in the game."""
from __future__ import annotations

import struct
import uuid


def v(n: int) -> bytes:
    result = bytearray()
    while n >= 128:
        result.append((n & 127) | 128)
        n >>= 7
    return bytes(result) + bytes([n])


def integer(field: int, value: int) -> bytes:
    return v(field * 8) + v(value)


def blob(field: int, value: bytes) -> bytes:
    return v(field * 8 + 2) + v(len(value)) + value


def number(field: int, value: float) -> bytes:
    return v(field * 8 + 5) + struct.pack("<f", value)


def vector(values) -> bytes:
    return b"".join(number(i + 1, value) for i, value in enumerate(values))


OBJECT_ID = "11111111-2222-3333-4444-555555555555"
SHAPE_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PREFAB_ID = "00000000-1111-2222-3333-444444444444"
CIRCUITS = b"\xff\x00opaque circuit bytes\x80\x80\x00"
UNKNOWN = blob(10000, b"future version payload") + b"\xa0\x9c\x04\x81\x00"


def fixture(*, object_model: bool = False, local_position=(0, 0, 0), extra_shapes: bool = False) -> bytes:
    mesh = blob(1, vector([1, 1, 1])) + blob(2, vector([1, 1, 1]))
    shape = integer(1, 4) + integer(2, 7) + integer(18, 5) + blob(7, vector(local_position)) + blob(8, vector([0, 0, 0])) + blob(12, uuid.UUID(SHAPE_ID).bytes_le) + number(13, 1) + blob(16, mesh) + blob(200, b"unknown shape property")
    collection = blob(1, shape)
    if extra_shapes:
        collection += blob(1, shape.replace(uuid.UUID(SHAPE_ID).bytes_le, uuid.uuid4().bytes_le))
    transform = blob(1, vector([10, 2, -8])) + number(5, 1) + blob(6, number(1, 1))
    view = blob(1, uuid.UUID(OBJECT_ID).bytes_le) + blob(10, transform) + blob(11, blob(1, uuid.UUID(PREFAB_ID).bytes_le)) + blob(12, blob(1, collection)) + blob(14, integer(4, 1)) + blob(777, b"unknown original component")
    room = integer(1, 2) + UNKNOWN + blob(2, view) + blob(18, CIRCUITS) + integer(30, 52) + blob(28, CIRCUITS) + integer(6, 20)
    if object_model:
        room += blob(29, b"Rooms 2 data requiring a separate editor")
    return room
