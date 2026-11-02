# X-Plane OBJ8 Object — Blender importer/exporter

Import and export [X-Plane OBJ8](https://developer.x-plane.com/article/obj8-file-format-specification/)
`.obj` objects with their textures, edit them in Blender, and get a `.obj` back
that X-Plane still loads — animations, manipulators and lights included.

## Install

From [extensions.blender.org](https://extensions.blender.org/add-ons/io_scene_xplane_obj/)
(Preferences > Get Extensions > search "X-Plane OBJ8"), or drag the release
`io_scene_xplane_obj-<version>.zip` into Blender. Requires Blender 4.2 or newer.

Menu: **File > Import > X-Plane OBJ8 (.obj)**, **File > Export > X-Plane OBJ8 (.obj)**.
Tick "Import whole folder" to bring in an aircraft's `objects/` directory at once.

## What it does

* one `.obj` becomes a header Empty plus one mesh per geometry command, so a
  21-part fuselage is 21 objects you can hide, edit and join;
* `TEXTURE` / `TEXTURE_LIT` / `TEXTURE_NORMAL` become a Principled material
  (base colour, emission, normal map), and the original *relative* texture paths
  (`../../objects/foo/bar.png`) are written back out unchanged;
* every `VT` row's authored normal is kept as a custom split normal — X-Plane
  stores lighting normals per vertex and they are not the averaged face normals
  Blender would otherwise compute;
* the command stream (`ANIM_*`, `ATTR_*`, `LIGHT_*`, `ATTR_LOD`, `IF`/`ENDIF`,
  …) is kept verbatim on the header Empty and replayed around the regenerated
  geometry, so moving a vertex does **not** delete the aircraft's door animation;
* a file you did not touch is written back **byte for byte**;
* tri / line / light counts and `POINT_COUNTS` are recomputed from what was
  actually written, and `TRIS`/`LINES` offsets are rebuilt with the index table.

Triangle winding is flipped on both sides: X-Plane calls a *clockwise* triangle
the front face, Blender a counter-clockwise one. Forgetting this makes every
model render inside-out — no error, just invisible surfaces.

## Animation

A `.obj` animation is driven by a **dataref** — a value the sim owns at run time
(`sim/flightmodel2/gear/deploy_ratio[0]`), not by a clock. Blender has no such
input, so the time line *is* the dataref:

* every batch inside an `ANIM_begin`/`ANIM_end` block gets a **chain of rig
  Empties** (`header -> rig -> rig -> mesh`), one per transform command, holding
  the transform its dataref selects. The part therefore sits where the sim would
  draw it — gear legs down, doors open — instead of piled up at the file origin,
  and you can edit it there. A compound animation (a pitch translate, then a
  yoke-hide, then a roll rotate) is a chain, so each dataref keeps its own
  values and its own keys instead of being squashed into one;
* each rig also gets an **Action** with **one keyframe per key the file has**,
  spread over frames 1..101 *by key index* — a two-endpoint `ANIM_rotate`
  becomes two keyframes, a 15-key gauge table fifteen. Index, not dataref value:
  real gauges key on things like `-9999, 0, 0.0126, 0.0252, …, 9999`, and a
  value-linear time line squeezes all twenty keys into two adjacent frames where
  they collapse into one. Linear keys, because that is how the file
  interpolates; a rotation of more than 180° between two keys gets the one
  intermediate key Blender needs to replay the long way round;
* `ANIM_show`/`ANIM_hide` become **step keyframes on `hide_viewport` /
  `hide_render`** (`CONSTANT` interpolation, one key per visibility boundary),
  so scrubbing the time line makes the parts appear and disappear exactly as the
  file says — icing shapes at their level, the prop disc at 0 rpm. A batch the
  file does not draw at the current value is hidden at import, and the import
  report says how many; **Show All Parts** in the X-Plane panel reveals
  everything for editing and **Restore Visibility** puts the schedule back.
  The `ANIM_show`/`ANIM_hide` lines themselves stay in `xp8_anim_vis` and are
  what gets written back — the keyframes are a preview, not the source.

**Import** options: *Animation at* (0..1, where to look — the pose you edit in),
*Pin datarefs* (`dref=value, …` to hold one still while the rest sweeps, e.g. a
prop at 0°, and *Bake Actions* off for rigs with a static pose only).

**Export** has two animation modes:

| mode | what happens |
| --- | --- |
| **From the file** (default) | the `ANIM_*` command stream is written exactly as imported. Edit it as text in `xp8_events` |
| **From Blender (rigs)** | every animated batch's block is **regenerated from its rig's Action**: the keyframes become `ANIM_rotate_begin`/`ANIM_rotate_key` tables (and `ANIM_trans_*`), and the file's own animation commands are dropped |

Rigs never reach the file — the exporter walks the parent chain, skips them, and
uses what you set on the mesh itself — so an untouched import is still byte
identical, and a part you *move* in Blender exports at the new position.

Drag the time line to watch the gear retract, the altimeter needle sweep, the
yoke move in pitch and roll at once.

To *author* a new animation, select a batch and press **Animate Part** in the
N-panel (category *X-Plane*): it creates the rig. Keyframe the rig's
location/rotation, export with **From Blender (rigs)**.

### The dataref is a field, not hidden data

Select a rig and the panel shows the animation in three editable controls:

| control | what it does |
| --- | --- |
| **Dataref** | the sim variable that drives the part — type any name, or use *Pick a dataref from this object* to choose one the file already uses. Changing it renames what the exported `ANIM_rotate_key` table is keyed on (a `show`/`hide` that watched the old name follows) |
| **Value@1 / @101** | the dataref values at the two ends of the time line, i.e. what the keys' x values mean. `0..1` for a ratio, `-18..89` for prop pitch |
| **Drop the show/hide rules** | the `ANIM_show`/`ANIM_hide` ranges this part carries; dropping them makes the part drawn at every value |

All three are plain custom properties (`xp8_dataref`, `xp8_value_min`,
`xp8_value_max`, `xp8_anim_vis`), so they are also visible in the object's
*Custom Properties* list and survive a save. Nothing about the animation is
hidden in a file you cannot reach — the only thing that comes from elsewhere is
the keyframe *count*, which follows the file's own keys at import.

A rotation is written as one `ANIM_rotate_begin` axis/angle table — exactly the
shape a hinge has — preceded by an `ANIM_rotate` for the rest orientation when
the part is not built axis-aligned. **A batch is only re-keyed when it really is
one hinge driven by one dataref**; OBJ8's key table has a single x axis, so
anything else (a gear leg that folds *and* steers, a wiper arm and blade on two
datarefs) would silently lose one of its inputs. Those batches keep the
animation they came in with, and the export says so, part by part, in the
console — an edit made to them in Blender is not written.

## Export writes the selection

**File > Export > X-Plane OBJ8** writes what is selected, nothing else:

| selection | what the file gets |
| --- | --- |
| the header Empty — the object itself | every part; byte for byte when nothing was edited |
| one or more mesh parts | only those parts' geometry |
| nothing | the whole scene: one imported object → that object, byte for byte; several → all meshes flattened into one file (command streams dropped, with a warning) |
| objects of several files at once | one file (the active object's); the others are skipped with a warning |

A partial export keeps the file's **command stream** — `ANIM_*`, `ATTR_manip_*`,
`ATTR_LOD`, `LIGHT_*`, `TEXTURE`/`GLOBAL_*`, the `VLINE`/`VLIGHT` sections — so
animations and manipulators survive; the dropped parts simply contribute no
batch, the `VT` and index tables are rebuilt from the parts that were written,
and `POINT_COUNTS` is recomputed to match. An `ATTR_LOD` level whose only
geometry was dropped stays behind as an empty level; if X-Plane complains,
remove the leftover lines from `xp8_events` on the header Empty.

A subset is never the original text, so the byte-identical shortcut is off
while parts are left out, and the dialog proposes `<name>_selected.obj` rather
than the source path — a partial write must not overwrite the object it came
from. **Back to source files** and the `.patch.obj` sibling follow the same
selection.

## Ortho4XP mesh patches

Tick **Write .patch.obj** on the export dialog and a sibling file is written next to the
`.obj`: `<name>.patch.obj`, the form Ortho4XP reads as a *terrain patch*
(`O4_Vector_Map.keep_obj8`, step 1). One `.obj` in, two files out.

| | `<name>.obj` | `<name>.patch.obj` |
|---|---|---|
| header block `I / 800 / OBJ` | yes | no |
| `ANCHOR` | no | line 1 |
| contents | globals, commands, animation | `VT` + `IDX10`/`IDX` + `TRIS` only |
| read by | X-Plane | Ortho4XP |

`ANCHOR` is not part of the X-Plane OBJ8 format (the spec puts an object's 0,0,0 wherever the
scenery places it), and Ortho4XP needs it on line 1 — so the two forms cannot be one file.

* The **longitude/latitude** is taken from the Ortho4XP tile in the scene: an import with the
  *X-Plane DSF and Ortho4XP Mesh* add-on leaves `mesh_georef` on its object, and the anchor is
  that frame's origin (the tile centre). Both are written as the frame's `lon0`/`lat0`, so the
  patch's `VT` columns are the object's own metres, unchanged.
* **Altitude and heading are 0.** Height therefore comes straight from the `VT` heights
  (absolute metres), and the axes are not rotated.
* No tile in the scene: the export still writes the normal `.obj` and reports a warning, the
  `.patch.obj` is skipped.

Drop the patch into `Patches/<lat tens><lon tens>/<+LL+LLL>/<any folder>/` and rebuild steps
1–2 of the tile. It contributes geometry and height only: no texture, no UV, and vertical faces
collapse, because the mesh is a height field. A patch belongs on a *scenery* object — an
aircraft's geometry exported this way would put the whole aircraft into the terrain.

## Where the X-Plane data lives

One `.obj` becomes one Empty plus one mesh per geometry command. The animation
exists twice on purpose: as **text** in custom properties on the Empty (the
ground truth that is written back), and as an **Action on a rig Empty** per
animated batch (so you can see and edit it). Nothing you do to the meshes can
damage the text.

| property | on | contents |
| --- | --- | --- |
| `xp8_dump` | Empty | the whole original file, for the byte-identical write-back |
| `xp8_events` | Empty | ordered command stream: `["A"]` ANIM_begin, `["Z"]` ANIM_end, `["R", line]` any command line verbatim, `["L", n]` geometry batch `n` |
| `xp8_globals` | Empty | `TEXTURE` / `POINT_COUNTS` / `GLOBAL_*` lines |
| `xp8_leaf_states` | Empty | the `ATTR_*` snapshot each batch draws under |
| `xp8_leaf_objs` | Empty | batch index → object name |
| `xp8_fingerprints`, `xp8_cmd_hash` | Empty | what the "was anything changed?" test compares |
| `xp8_leaf` | mesh | which batch it is |
| `xp8_leaf`, `xp8_dataref`, `xp8_anim_range`, `xp8_anim_datarefs`, `xp8_anim_vis`, `xp8_anim_action`, `xp8_anim_keys` | rig Empty | the batch it drives, its driving dataref and value range, the visibility commands, the baked Action and the key positions it was built from |
| `xp8_anim_vis_action` | mesh | the Action holding the step keys that show/hide the batch on the time line |
| `xp8_placement` | mesh | legacy: a static `ANIM_trans` offset, still honoured on export |
| `xp8_vt` | mesh attribute | source `VT` row index per vertex, so untouched rows keep their exact text |

Editing `xp8_events`, `xp8_globals` or `xp8_leaf_states` and exporting does reach
the file: the untouched-file shortcut switches itself off as soon as any of them,
or any geometry, differs from what was imported.

## Known limits

* `LINES` are imported read-only and exported from their stored indices; light
  primitives (`LIGHT_PARAM`, `LIGHT_NAMED`) are preserved as raw commands.
* Animation can be *re-keyed* here (rigs + Actions → `ANIM_rotate_key` tables),
  but only as a rigid transform per batch: rotation is written as Euler XYZ
  around the origin, and `ANIM_show`/`ANIM_hide` are carried through as-is
  rather than authored. Manipulators (`ATTR_manip_*`) survive an export but
  cannot be created or edited here — use
  [XPlane2Blender](https://github.com/X-Plane/XPlane2Blender) for that.
* One `.obj` has one texture set, so an object with several materials exports
  with the first material's textures.
* Blender stores custom normals as int16 per axis, so regenerated normals can
  differ from the source by ~1e-4. Positions and UVs round-trip exactly.

## Development

```sh
./dev-link.sh            # symlink this repo into Blender's user extension folder
                         # then enable "X-Plane OBJ8 Object" in Preferences

python3 verify/test_codec.py verify/fixtures       # codec only, no Blender
python3 verify/test_fixture.py                     # pinned expectations
python3 verify/test_anim.py                        # ANIM_* evaluator, no Blender
blender -b --factory-startup --python verify/roundtrip_blender.py -- verify/fixtures
blender -b --factory-startup --python verify/anim_roundtrip.py -- verify/fixtures
blender -b --factory-startup --python verify/check_selection.py   # export = selection

# stronger: run the same tests against a real aircraft
blender -b --factory-startup --python verify/roundtrip_blender.py -- \
    "/path/to/X-Plane 12/Aircraft/<plane>/objects/<folder>"
```

`objcodec.py` is pure Python (no `bpy`), so the format work can be tested and
debugged without starting Blender.

## Releasing

1. bump `version` in `blender_manifest.toml`
2. `git tag -a v<same version>` (annotated: its message becomes the release
   notes) and `git push origin main v<same version>`

The `v*` tag runs `.github/workflows/blender-extension.yml`, which builds
`io_scene_xplane_obj-<version>.zip` with `blender --command extension build`,
validates it, keeps it as the `blender-extension` build artifact and attaches it
to a **draft** GitHub release. Nothing is published automatically: review the
draft and publish it if you want it on GitHub, then send the same zip to
<https://extensions.blender.org/submit/>, where it appears once the moderation
team approves it.


What ends up in the zip is decided by `[build] paths_exclude_pattern` in the
manifest: the add-on modules, `LICENSE` and this README. The test fixtures, the
dev symlink helper and the CI config stay in the repository.

Tests are not part of the release build — run them yourself:

```sh
python3 verify/test_codec.py verify/fixtures
python3 verify/test_fixture.py
python3 verify/test_anim.py
blender -b --factory-startup --python verify/roundtrip_blender.py -- verify/fixtures
blender -b --factory-startup --python verify/anim_roundtrip.py -- verify/fixtures
```


## License

GPL-3.0-or-later, matching the [extension platform requirements](https://docs.blender.org/manual/en/latest/advanced/extensions/licenses.html).
