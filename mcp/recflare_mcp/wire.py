"""Small lossless protobuf wire editor, not a schema converter.

Unchanged fields retain their original bytes, including unknown fields, ordering,
and noncanonical varints. Only explicitly edited fields are re-encoded.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import struct

MAX_BYTES = 64 * 1024 * 1024
MAX_FIELDS = 250_000


class FormatError(ValueError):
    pass


def varint(value: int) -> bytes:
    if not isinstance(value, int) or not 0 <= value < 1 << 64:
        raise FormatError("Varint outside uint64 range")
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise FormatError("Truncated protobuf varint")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise FormatError("Protobuf varint exceeds uint64")
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise FormatError("Unterminated protobuf varint")


@dataclass(frozen=True)
class Field:
    number: int
    wire: int
    value: bytes | int
    raw: bytes

    @staticmethod
    def make(number: int, wire: int, value: bytes | int) -> Field:
        tag = varint((number << 3) | wire)
        if wire == 0:
            raw = tag + varint(value)
        elif wire == 2:
            raw = tag + varint(len(value)) + value
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if not isinstance(value, bytes) or len(value) != size:
                raise FormatError("Wrong fixed-width field size")
            raw = tag + value
        else:
            raise FormatError("Unsupported protobuf wire type")
        return Field(number, wire, value, raw)


class Message:
    def __init__(self, fields: list[Field] | None = None):
        self.fields = fields or []

    @classmethod
    def parse(cls, data: bytes) -> Message:
        if len(data) > MAX_BYTES:
            raise FormatError("Room data exceeds 64 MiB")
        fields = []
        pos = 0
        while pos < len(data):
            if len(fields) >= MAX_FIELDS:
                raise FormatError("Too many protobuf fields")
            start = pos
            tag, pos = read_varint(data, pos)
            number, wire = tag >> 3, tag & 7
            if not 1 <= number < 1 << 29:
                raise FormatError("Invalid protobuf field number")
            if wire == 0:
                value, pos = read_varint(data, pos)
            elif wire in (1, 5):
                end = pos + (8 if wire == 1 else 4)
                if end > len(data):
                    raise FormatError("Truncated fixed-width protobuf field")
                value, pos = data[pos:end], end
            elif wire == 2:
                length, pos = read_varint(data, pos)
                end = pos + length
                if end > len(data):
                    raise FormatError("Truncated protobuf payload")
                value, pos = data[pos:end], end
            else:
                raise FormatError("Legacy protobuf groups are unsupported; no data was changed")
            fields.append(Field(number, wire, value, data[start:pos]))
        return cls(fields)

    def encode(self) -> bytes:
        data = b"".join(f.raw for f in self.fields)
        if len(data) > MAX_BYTES:
            raise FormatError("Edited room would exceed 64 MiB")
        return data

    def all(self, number: int, wire: int | None = None) -> list[Field]:
        result = [f for f in self.fields if f.number == number]
        if wire is not None and any(f.wire != wire for f in result):
            raise FormatError(f"Unexpected wire type for field {number}")
        return result

    def has(self, number: int) -> bool:
        return bool(self.all(number))

    def integer(self, number: int, default: int = 0) -> int:
        fields = self.all(number, 0)
        return fields[-1].value if fields else default

    def blob(self, number: int, default: bytes = b"") -> bytes:
        fields = self.all(number, 2)
        return fields[-1].value if fields else default

    def message(self, number: int) -> Message:
        # Protobuf merges repeated occurrences of a singular message.
        return Message.parse(b"".join(f.value for f in self.all(number, 2)))

    def floating(self, number: int, default: float = 0) -> float:
        fields = self.all(number, 5)
        value = struct.unpack("<f", fields[-1].value)[0] if fields else default
        if not math.isfinite(value):
            raise FormatError("Non-finite float in room data")
        return value

    def replace(self, number: int, replacements: list[Field]) -> None:
        out, inserted = [], False
        for field in self.fields:
            if field.number == number:
                if not inserted:
                    out.extend(replacements)
                    inserted = True
            else:
                out.append(field)
        if not inserted:
            out.extend(replacements)
        self.fields = out

    def set_int(self, number: int, value: int) -> None:
        self.replace(number, [Field.make(number, 0, value)])

    def set_blob(self, number: int, value: bytes) -> None:
        self.replace(number, [Field.make(number, 2, value)])

    def set_message(self, number: int, value: Message) -> None:
        self.set_blob(number, value.encode())

    def set_float(self, number: int, value: float) -> None:
        if not math.isfinite(value) or abs(value) > 1e30:
            raise FormatError("Invalid float")
        self.replace(number, [Field.make(number, 5, struct.pack("<f", value))])


def vector(values: list[float], base: Message | None = None) -> Message:
    result = base or Message()
    for number, value in enumerate(values, 1):
        result.set_float(number, value)
    return result


def read_vector(msg: Message, default: list[float] | None = None) -> list[float]:
    default = default or [0.0, 0.0, 0.0]
    return [msg.floating(i + 1, default[i]) for i in range(3)]
