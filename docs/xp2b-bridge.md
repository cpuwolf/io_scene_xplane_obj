# XPlane2Blender bridge

The import ends by converting the scene in place (`object.xp8_to_xp2b`, also on
the N-panel as *author with XPlane2Blender*), so the exportable collection exists
the moment the file lands. From then on the scene belongs to XPlane2Blender and
**File > Export > X-Plane Object (.obj)** is the export path.

This is a one-way bridge: XP2B rebuilds every `ANIM_*` block from the object
tree, so it cannot echo raw commands. Authoring (manipulators, new animations,
lights, LODs) is what it is good at; echoing a file byte for byte is what the
standalone exporter was for.

## What maps to what

| in the file | in the XPlane2Blender scene |
| --- | --- |
| `ANIM_begin/end` nesting | a bone chain (one armature per file); a block with only show/hide gets a bare anchor bone |
| `ANIM_trans_begin` + keys | bone/Empty with a `transform` dataref and one location key per file key |
| `ANIM_rotate_begin` + keys | `rotation_mode = AXIS_ANGLE`, the file's axis verbatim |
| static `ANIM_trans` / `ANIM_rotate` | plain transforms |
| `ANIM_show` / `ANIM_hide` | `show` / `hide` datarefs on the **innermost bone of the block**, so the rule scopes to that subtree |
| `ATTR_draw_disable` | a material clone with *Draw Objects With This Material* off |
| `ATTR_solid_camera` | the material's *Camera Collision* |
| `ATTR_cockpit*` | the material's *Cockpit Feature* (`Panel Texture` / `Cockpit Device`) plus the layer's *Panel Texture Mode*; `COCKPIT_REGION` becomes the layer's region table |
| `ATTR_light_level` | the **object's** *X-Plane > Light Levels > Override Light Level* (value1 / value2 / dataref) |
| `ATTR_manip_*` | `object.xplane.manip` (command family, toggle, push, radio, delta/wrap, drag axis, drag rotate) |
| `TEXTURE` / `TEXTURE_LIT` / `TEXTURE_NORMAL` | the **layer's** Default / Night / Normal-Specular fields |
| `TEXTURE_MAP normal` / `material_gloss` / `gloss` | the layer's 12.0+ Normal / Material-Gloss / Gloss fields |
| `NORMAL_METALNESS`, `BLEND_GLASS` | the layer's *Normal Metalness* and *Blend Glass* |
| `ATTR_shiny_rat` | the material's specularity |

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

## What cannot be carried

`LIGHT_*` primitives, `ATTR_LOD`, `IF`/`ENDIF` and `MAGNET` have no XP2B command
form, so they are dropped with a warning per file (also stored as
`xp2b_convert_warnings` on the collection). A file that mixes panel modes cannot
be reproduced exactly either: the layer holds one mode, so the first wins and the
mixture is reported.

## Accuracy

Measured through XPlane2Blender on four aircraft objects and the test fixture:
every batch lands within 0.07 m of the file at five dataref positions, most
exactly. On Laminar's C90B exterior the `ANIM_*`/`ATTR_*` text comes out
line-for-line identical apart from 1e-4 float rounding, triangle counts are exact
(fuselage 19697, lights 1558).

Two known gaps are in [known limits](known-limits.md).
