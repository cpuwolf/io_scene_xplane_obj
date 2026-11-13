# XPlane2Blender bridge

The import ends by converting the scene in place (`object.xp8_to_xp2b`, also on
the N-panel as *author with XPlane2Blender*), so the exportable collection exists
the moment the file lands. From then on the scene belongs to XPlane2Blender and
**File > Export > X-Plane Object (.obj)** is the export path.

This is a one-way bridge and it is the whole export path: XP2B rebuilds every
`ANIM_*` block from the object tree. Authoring (manipulators, new animations,
lights, LODs) is what it is good at — nothing here writes a `.obj` itself.

## What maps to what

| in the file | in the XPlane2Blender scene |
| --- | --- |
| `ANIM_begin/end` nesting | a bone chain (one armature per file); a block with only show/hide gets a bare anchor bone |
| `ANIM_trans_begin` + keys | bone/Empty with a `transform` dataref and one location key per file key |
| `ANIM_rotate_begin` + keys | `rotation_mode = AXIS_ANGLE`, the file's axis verbatim |
| static `ANIM_trans` / `ANIM_rotate` | plain transforms |
| `ANIM_show` / `ANIM_hide` | `show` / `hide` datarefs on the **innermost bone of the block**, so the rule scopes to that subtree |
| `ATTR_draw_disable` | a material clone with *Draw Objects With This Material* off |
| `ATTR_blend` / `ATTR_no_blend [ratio]` / `ATTR_shadow_blend ratio` | the material's *Blend Mode* and *Alpha Cutoff Ratio* |
| `ATTR_shadow` / `ATTR_no_shadow` | the material's *Shadow Local* |
| `ATTR_poly_os n` / `ATTR_no_poly_os` | the material's *Polygon Offset* |
| `ATTR_solid_camera` | the material's *Camera Collision* |
| `ATTR_cockpit*` | the material's *Cockpit Feature* (`Panel Texture` / `Cockpit Device`) plus the layer's *Panel Texture Mode*; `COCKPIT_REGION` becomes the layer's region table |
| `ATTR_light_level` | the **object's** *X-Plane > Light Levels > Override Light Level* (value1 / value2 / dataref) |
| `ATTR_manip_*` | `object.xplane.manip` (command family, toggle, push, radio, delta/wrap, drag axis, drag rotate) |
| `TEXTURE` / `TEXTURE_LIT` / `TEXTURE_NORMAL` | the **layer's** Default / Night / Normal-Specular fields |
| `TEXTURE_MAP normal` / `material_gloss` / `gloss` | the layer's 12.0+ Normal / Material-Gloss / Gloss fields |
| `NORMAL_METALNESS`, `BLEND_GLASS` | the layer's *Normal Metalness* and *Blend Glass* |
| `ATTR_shiny_rat` | the material's specularity |
| `LIGHT_PARAM` / `LIGHT_NAMED` | a light object (position from the object, the parameter text verbatim) *plus* a preview-only colour / power / cone / aim read from that text; the aim is the one field XP2B compares against, so it is set to the line's own direction |
| `ATTR_LOD` | the layer's LOD levels, plus the `lod[i]` flag on each batch |
| `PARTICLE_SYSTEM` | the **layer's** *Particle System File* |
| `EMITTER` | an Empty with XP2B's *Particle Emitter* type (name, array index, position and Euler angles) |

Two of those are worth the extra sentence they get:

* **Textures live on the layer, not the material.** XP2B writes
  `TEXTURE`/`TEXTURE_LIT`/`TEXTURE_NORMAL`/`TEXTURE_MAP` from
  Collection > X-Plane > Textures, and the material graph only affects the
  viewport. Paths are stored the way the file spells them (`livery_NML.png`), so
  a file kept next to its textures exports with the same one-word name; XP2B
  relativises whatever it is given against the `.obj` output directory.
* **Light levels live on the object, not the material.** The line is per geometry
  command and an object *is* one command, so no material is cloned for it. The
  material path is not merely redundant, it loses batches: XP2B emits
  `ATTR_light_level` when the material changes and a reset when leaving it, but
  does not re-emit the level for a later batch that shares the earlier value
  (C90B_cockpit.obj: 21 of 33 batches silently lost
  `laminar/c90/warning/gear_handle`). Per object it survives any batch order —
  measured 0 differing batches on 358 (C90B_cockpit.obj), 164 (C90B_cockpit2.obj)
  and 59 (Sikorsky switches.obj) — and a cockpit-panel batch keeps its level too,
  because XP2B only refuses a *panel material* that overrides the level.

Material clones are shared: batches that ask for the same state combination get
one clone, not one each.

* **Blending is the one state whose "on" side writes nothing.** X-Plane's
  default state is blending on (OBJ8 spec: *this is the default* next to
  `ATTR_blend`), and XP2B's command printer starts out considering `ATTR_blend`
  already in effect, so a batch with `ATTR_blend` exports with no blend line at
  all — exactly what a material authored in XP2B from scratch does. The line is
  written again where it is needed: after a batch that turned blending off or
  switched it to shadow-only. `ATTR_no_blend 0.3` and `ATTR_shadow_blend 0.75`
  keep their cutoff ratio (`blendRatio`); leaving it alone would quietly hand
  X-Plane XP2B's 0.5 default.

* **A manipulator XPlane2Blender would have to rewrite goes out verbatim.** Its
  drag-rotate settings are derived from the bones above the mesh — pivot, axis,
  both angle ends, the value range and the detent lift — so where the file's line
  disagrees with the bone (a throttle lever keyed `-1..1` whose reverse gate maps
  only `-1..0`, a rotation that lives in a sibling block, a detent taller than the
  bone can lift) the native fields would export *different* numbers. The line is
  then carried through the object's custom attributes instead, together with its
  detent table, intermediate keyframes and wheel rate. X-Plane reads the
  manipulator and the `ANIM` table independently, so the export is exact; what it
  costs is the N-panel — that batch's manipulator is a raw attribute rather than
  editable fields. Every other line still takes the native route.

## Particles

A particle file is two records, and both have to land or neither exports:

* `PARTICLE_SYSTEM <file.pss>` becomes the layer's *Particle System File*, the
  path spelled exactly as the file spells it (`particles/aircraft_system.pss` is
  relative to the `.obj`, which is where the export writes);
* every `EMITTER` becomes an Empty whose *Particle Emitter* settings hold the
  emitter's name and array index, and whose transform holds the position and the
  three Euler angles. Emitters inside an `ANIM` block hang off that block's bone
  exactly like the geometry, so the line comes back out inside the same block and
  keeps following the animation that drives it.

XPlane2Blender refuses an emitter while no `.pss` is set (it cancels the whole
export), so a file with `EMITTER` but no `PARTICLE_SYSTEM` is reported instead of
half-converted. The `.pss` itself is not read or edited here — only referenced.

## What cannot be carried

`IF`/`ENDIF` blocks have no XP2B command form, so they are dropped with a warning
per file (also stored as `xp2b_convert_warnings` on the collection), as is any
`ATTR_*` category no field of XPlane2Blender's covers. A file that mixes panel
modes cannot be reproduced exactly either: the layer holds one mode, so the first
wins and the mixture is reported.

## Accuracy

Measured through XPlane2Blender on four aircraft objects and the test fixture:
every batch lands within 0.07 m of the file at five dataref positions, most
exactly. On Laminar's C90B exterior the `ANIM_*`/`ATTR_*` text comes out
line-for-line identical apart from 1e-4 float rounding, triangle counts are exact
(fuselage 19697, lights 1558).

Two known gaps are in [known limits](known-limits.md).
