# Standalone exporter (legacy) and Ortho4XP patches

> **Legacy path.** The supported export is XPlane2Blender's, see the
> [bridge](xp2b-bridge.md). The add-on's own exporter is kept for two jobs:
> writing a scene back **byte for byte** when it was imported *without* XP2B, and
> writing the Ortho4XP `.patch.obj`.
>
> The UI import always converts the scene for XP2B, and after that conversion the
> byte-identical shortcut no longer applies (the meshes were re-parented onto the
> bridge's armature). So in practice: import with XP2B disabled if you want the
> verbatim write-back, or use XP2B to author.

## Writing the file back

**File > Export > X-Plane OBJ8** writes what is selected, nothing else:

| selection | what the file gets |
| --- | --- |
| the imported object | every part; byte for byte when nothing was edited |
| one or more mesh parts | only those parts' geometry |
| nothing | the whole scene: one imported object → that object, byte for byte; several → all meshes flattened into one file (command streams dropped, with a warning) |
| objects of several files at once | one file (the active object's); the others are skipped with a warning |

A partial export keeps the file's **command stream** (`ANIM_*`,
`ATTR_manip_*`, `ATTR_LOD`, `LIGHT_*`, `TEXTURE`/`GLOBAL_*`, the `VLINE`/`VLIGHT`
sections), so animations and manipulators survive; the dropped parts simply
contribute no batch, the `VT` and index tables are rebuilt from the parts that
were written, and `POINT_COUNTS` is recomputed to match. An `ATTR_LOD` level
whose only geometry was dropped stays behind as an empty level; if X-Plane
complains, remove the leftover lines from `xp8_events` on the collection.

A subset is never the original text, so the byte-identical shortcut is off while
parts are left out, and the dialog proposes `<name>_selected.obj` rather than the
source path — a partial write must not overwrite the object it came from.

## Ortho4XP mesh patches

Tick **Write .patch.obj** and a sibling file is written next to the `.obj`:
`<name>.patch.obj`, the form Ortho4XP reads as a *terrain patch*
(`O4_Vector_Map.keep_obj8`, step 1). One `.obj` in, two files out.

| | `<name>.obj` | `<name>.patch.obj` |
| --- | --- | --- |
| header block `I / 800 / OBJ` | yes | no |
| `ANCHOR` | no | line 1 |
| contents | globals, commands, animation | `VT` + `IDX10`/`IDX` + `TRIS` only |
| read by | X-Plane | Ortho4XP |

`ANCHOR` is not part of the X-Plane OBJ8 format (the spec puts an object's 0,0,0
wherever the scenery places it) and Ortho4XP needs it on line 1, so the two forms
cannot be one file.

* **Longitude/latitude** come from the Ortho4XP tile in the scene: an import with
  the *X-Plane DSF and Ortho4XP Mesh* add-on leaves `mesh_georef` on its object,
  and the anchor is that frame's origin (the tile centre). Both are written as
  the frame's `lon0`/`lat0`, so the patch's `VT` columns are the object's own
  metres, unchanged.
* **Altitude and heading are 0.** Height therefore comes straight from the `VT`
  heights (absolute metres), and the axes are not rotated.
* No tile in the scene: the export still writes the normal `.obj` and reports a
  warning, the `.patch.obj` is skipped.

Drop the patch into `Patches/<lat tens><lon tens>/<+LL+LLL>/<any folder>/` and
rebuild steps 1–2 of the tile. It contributes geometry and height only: no
texture, no UV, and vertical faces collapse because the mesh is a height field. A
patch belongs on a *scenery* object — an aircraft's geometry exported this way
would put the whole aircraft into the terrain.
