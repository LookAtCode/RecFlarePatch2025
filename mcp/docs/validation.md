# Validation status

Implemented and covered by automated tests:

- A real stdio subprocess serving MCP discovery and building tool calls.
- Measured shape templates; bulk creation, transforms, color, deletion, export.
- Original-object, unknown-field, and circuit-payload preservation.
- Whole-plan rollback, immutable history, local conflicts, and restore.
- Loopback HTTP discovery, download, hash check, multipart upload, stage, publish,
  refusals, save-ID conflicts, and lost-response recovery without duplicate saves.
- CI adds independent official SDK and protobuf checks; see actual run results.

These checks do **not** prove game rendering, collision, Maker Pen editability,
multiplayer visibility, or load/save compatibility. The synthetic test fixture
uses a made-up prefab GUID and is not captured from the game.

## First real-client acceptance test

1. Use 20250718.01 and a disposable regular test room.
2. Save/publish one centered, unrotated one-meter Maker Pen cube.
3. Download it as a new project and register that exact cube as a template.
4. Create one additional cube two meters away, preserving the original.
5. Export/import, or stage and explicitly publish using the existing API.
6. Join a fresh instance. Confirm both cubes render, collide, and can be selected
   with the Maker Pen. Keep Player.log if loading fails.
7. Save from the game, leave, and rejoin a fresh instance to verify persistence.
8. Have another player verify visibility and collision.
9. Repeat with a nonuniform size, 90-degree rotation, hex color, known material,
   and then a larger layout.

On failure, retain original/generated room files and revision history. Verify
the target build's fields, required prefab components, dimensions, and GUID
handling. Upload success or a preview is not proof of a successful game load.

Live editing of an existing instance needs a separate verified native bridge.
This implementation installs no guessed native function addresses.
