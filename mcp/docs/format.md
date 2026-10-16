# Format mappings and boundaries

Independent minimal protobuf wire editor; no original game code, schema binary,
asset files, or prefab registry is bundled. The interoperability fields used are:

| Message | Fields |
| --- | --- |
| `PersistedRoomData` | legacy version 1, repeated views 2, object model 29, version 30 |
| `PersistenceViewData` | GUID 1, children 3, tool entity 9, transform 10, prefab 11, shapes 12, creation 14, colorable 36, deformation 53, entity index 100 |
| `TransformData` | position 1, legacy Euler 2, uniform scale 5, quaternion 6 |
| `QuaternionData` | w 1, x 2, y 3, z 4 |
| `Vector3Data` | x 1, y 2, z 3 |
| `ShapeContainerData` | shape collection 1 |
| `ShapeDataCollectionData` | repeated shape 1 |
| `ShapeData` | type 1, palette color 2, local position 7, local Euler 8, GUID 12, scale 13, curve 17, material 18 |
| `SandboxDeformationData` | deformation vector 1 |
| `CreationObjectData` | RGBA override 2, linked invention metadata 5 |
| `ColorData` | r 1, g 2, b 3, a 4 |

Published definitions inspected on 2026-10-16:

- [Room](https://github.com/obfuscatedgenerated/RecRoomDataMining/blob/main/proto/rec_room/persistence/persisted_room_data.proto)
- [View](https://github.com/obfuscatedgenerated/RecRoomDataMining/blob/main/proto/rec_room/persistence/persistence_view_data.proto)
- [Shapes](https://github.com/obfuscatedgenerated/RecRoomDataMining/blob/main/proto/rec_room/persistence/shape_data.proto)
- [Transform](https://github.com/obfuscatedgenerated/RecRoomDataMining/blob/main/proto/rec_room/transform_data.proto)
- [Quaternion](https://github.com/obfuscatedgenerated/RecRoomDataMining/blob/main/proto/core/quaternion_data.proto)
- [Creation settings](https://github.com/obfuscatedgenerated/RecRoomDataMining/blob/main/proto/rec_room/persistence/creation_object_data.proto)

These descriptions come from a later export, so they are not proof of archived
client compatibility. The view/version fields agree with the existing RecFlare
converter, but generated-object loading still needs a real 20250718.01 test.

The helper requires a target-client save and an actual seed shape. It preserves
version fields, shape type, prefab ID, and geometric component data. New objects
and shapes get .NET-layout GUID bytes (`uuid.bytes_le`). A template omits copied
child/group/circuit/object-model references. Caller-measured dimensions control
nonuniform deformation. Rotation uses Unity's Z-then-X-then-Y Euler convention.
Roots must have distinct 16-byte IDs; indexed objects and nonempty object-model
data are not editable. Only centered, unrotated, standalone solid seeds are used.

Untouched raw fields and original object payloads retain their bytes, including
unknown fields and noncanonical varints. Circuit fields remain opaque and are
never stripped or regenerated. Malformed files are refused. Every project keeps
`original.room`; immutable snapshots and scene blobs are written before an atomic
manifest switch. Locks and expected revisions reject concurrent local edits.
After a crash, close all helpers before manually removing a stale `.lock` file.

## Existing backend contracts

Read from RecFlareServer commit `1f4c3e5fc069bab0a65baf2fd192d282ccc33203`:

- Name-server document advertises Auth, Rooms, Storage, CDN.
- Auth `/connect/token` supplies ordinary password/refresh grants.
- Storage `/upload` takes multipart FileType 1 plus a binary File.
- Rooms `POST /rooms/{id}/subrooms/{sid}/data` records the uploaded key and base64
  SHA-256. The source persistence version, invention usage, and Studio bundle ID
  are carried forward. Room-level metadata is not overwritten.
- `AutoPublish:false` stages a save, except dorms, which this helper refuses.
- Form `subRoomDataSaveId` to `.../publish_save` explicitly publishes.

No backend endpoint, migration, or deployment is added. The existing API lacks
atomic save preconditions, so concurrent online changes remain a documented race.

## MCP

Newline-delimited UTF-8 JSON-RPC on stdio. Initialization supports 2024-11-05,
2025-03-26, 2025-06-18, and 2025-11-25. Methods: initialize, ping, tools/list,
tools/call. Business errors use MCP isError; protocol errors use JSON-RPC errors.
Notifications cannot execute edits. No runtime SDK is needed. CI uses the official
MCP Python SDK independently. Newer revisions/every third-party host are not
independently certified.
