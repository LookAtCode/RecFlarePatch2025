# RecFlare building MCP — experimental

Let Codex or another MCP-capable AI help build the geometry of a RecFlare room.
Requires **Python 3.11+**, with no third-party runtime packages or AI API key.

**This version edits saved Maker Pen rooms. It does not hook the running game.**
It creates, resizes, rotates, recolors, repeats, and removes its own building
pieces, exports a complete `.room`, and can stage/publish through existing
RecFlare APIs. Join a **fresh room instance** to see published changes.
Existing instances do not receive live edits from this helper.

**Actual loading in the 20250718.01 game client remains unverified.** The protocol,
geometry edits, backups, and HTTP workflow have automated tests, but no real
game binary or client-generated room save was available during implementation.
Synthetic fixtures and successful uploads are not game-load evidence. Start with
a separate test room. This is not Roblox Studio parity. Circuits are preserved,
not edited. See [validation status](docs/validation.md).

## Quick start: offline files

1. Make a regular test room in your supported RecFlare client. Place a standalone,
   unrotated, centered **one-meter cube** with the Maker Pen. Save and publish it.
2. Download that subroom's scene save from your RecFlare website room page. Keep
   the original. Use a save from the client that will load the result, without
   downgrading newer exports or stripping their circuits.
3. Choose a workspace, run the doctor command below, then copy the file into
   `C:\RecFlareBuilds\imports\seed.room`.
4. Add the MCP server to your AI client. Substitute your own paths:

```powershell
py -3.11 "C:\Code\RecFlarePatch2025\mcp\server.py" doctor --workspace "C:\RecFlareBuilds"
codex mcp add recflare -- py -3.11 "C:\Code\RecFlarePatch2025\mcp\server.py" --workspace "C:\RecFlareBuilds"
codex mcp list
```

Restart your AI client. Codex's `/mcp` command lists active connections. Running
the server without `doctor` waits quietly for MCP requests; it is not an
interactive prompt. Another MCP client can launch `python /absolute/path/mcp/server.py`
with the same workspace arguments. No TCP port is opened.

Ask the AI:

> Use the RecFlare tools. Import seed.room as project courtyard, inspect its
> shape containers, and register my one-meter cube as template block. Build a
> 20-meter courtyard with walls, a doorway, pillars, and stairs. Leave original
> objects alone. Make a layout preview and export the room. Do not publish it.

The AI inspects the file to find real object and shape IDs. The helper does not
invent prefab IDs or treat an empty file as a room. If the seed is off-center or
rotated inside its container, create a clean standalone seed and save again.
Upload the exported `.room` through your room page to check it in-game. Local
originals and revision history stay in the workspace.

## Optional online workflow

The helper uses your own account and normal room permissions, without changing
or deploying a backend. Sign in in **your own terminal**; the password is hidden
and is not written to disk or passed to the AI:

```powershell
py -3.11 "C:\Code\RecFlarePatch2025\mcp\server.py" login --workspace "C:\RecFlareBuilds" --server "https://ns.recflare.net" --username YOUR_USERNAME
```

The workspace's `auth.json` holds the resulting access/refresh tokens. Keep it
private and out of Git. Alternatively supply `RECFLARE_ACCESS_TOKEN` through the
environment; the AI does not need to see the token. No account is created.

Add `--server https://ns.recflare.net` to your MCP launch arguments, using your
own deployment's name server if different. The same workspace and server URL
must be used for login and the MCP process. See [codex.example.toml](codex.example.toml).
If you already configured an entry, edit that entry rather than adding duplicates.

The AI can then list your rooms, download a published or staged save into a new
project, build locally, and stage the exact local revision. Staging appends a
save and leaves the published revision alone. Explicit publication uses the
existing creator-only API; co-owners may save but cannot publish. Join a fresh
instance afterward. The live session is not automatically reloaded.

Dorm uploads are refused because dorm saves auto-publish. Online uploads require
a project created by `recflare_download_room`, bound to the same server and room.
Local imports can be exported for manual website upload.

Avoid simultaneous game and MCP saves to the same subroom. Published/staged save
IDs are checked before upload and before saving, but the existing server has no
atomic compare-and-save endpoint. A conflict requires a fresh download. A lost
save response is journaled: `recflare_check_upload` finds that exact blob/hash in
history without blindly retrying. Retain an unresolved journal and inspect server
history before attempting manual recovery.

## Building plans

Positions are room-space meters. Rotations are Unity Euler degrees `[x,y,z]`.
Sizes are `[width,height,depth]` in the seed's local axes. Correct dimensions
depend on correctly measuring the seed; with a one-meter cube, `[10,0.5,4]`
produces a ten-by-half-by-four-meter piece.

- `create`: one independent piece from a registered template.
- `repeat`: a line/staircase using `count`, `step`, and optional `rotation_step`.
- `grid`: a 3D array using `counts` and `spacing`.
- `transform`: move/rotate/resize an MCP-created piece by name.
- `paint`: a known palette color ID or `#RRGGBB`/`#RRGGBBAA`; materials use IDs
  copied from inspected shapes. Hex colors use the object's color-override field.
- `delete`: remove an MCP-created piece by name.

Original objects can be inspected and supply templates; this version does not
move/delete them. Templates support one ordinary solid shape. Props, curves,
linked inventions, nested groups, Rooms 2/object-model scenes, scripts, and
circuit editing are outside this version's editing scope.

Send [examples/courtyard.json](examples/courtyard.json)'s `operations` to
`recflare_apply_plan` with the project name and current `expected_revision`.
Every edit returns a new revision. An invalid operation rolls back the entire
plan; stale revisions are rejected. Local snapshots are immutable and restorable.

The HTML preview shows bounding boxes of MCP-created pieces, excluding existing
room geometry. It does not reproduce detailed shapes or exact palette colors.
It is a layout aid, not a game screenshot or proof of loading.

## Tools

| Tool | Purpose |
| --- | --- |
| `recflare_status` | Workflow, paths, login availability, limits |
| `recflare_list_projects` | Local projects and import files |
| `recflare_open_room` | Back up/import a local scene |
| `recflare_get_project` | Current revision, templates, upload state |
| `recflare_list_objects` | Inspect shapes, transforms, palette IDs |
| `recflare_register_template` | Register a measured shape seed |
| `recflare_apply_plan` | Create, repeat, grid, transform, paint, delete |
| `recflare_history` | Local revision history |
| `recflare_restore_revision` | Restore locally without erasing history |
| `recflare_export_room` | Export the complete edited scene |
| `recflare_preview` | Interactive bounding-box preview |
| `recflare_list_rooms` | Your online rooms/subrooms |
| `recflare_download_room` | Download/bind an online save |
| `recflare_stage_room` | Append a staged save |
| `recflare_check_upload` | Reconcile an uncertain save |
| `recflare_publish_room` | Publish the exact verified staged save |

## Verification

```powershell
cd mcp
py -3.11 -m unittest discover -s tests -v
```

Tests exercise a real stdio subprocess and loopback HTTP fixture, including a
1000-piece build, unknown/circuit-byte preservation, undo, malformed input, hash
checks, permissions/refusals, conflicts, and lost replies. CI adds the official
MCP Python SDK and Google protobuf as **test-only** independent checks. Consult
the branch's CI results for their actual status.

The native DLL/injector are unchanged; rebuilding them is unnecessary for this
helper. Their existing Windows build is checked separately on this branch.
See [format mappings](docs/format.md) and the [real-client acceptance test](docs/validation.md).
Codex setup follows the [official OpenAI documentation](https://developers.openai.com/codex/mcp).
