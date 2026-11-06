# Known limits

* `LINES` are imported read-only and exported from their stored indices; light
  primitives (`LIGHT_PARAM`, `LIGHT_NAMED`) are preserved as raw commands in the
  file text and cannot be handed to XPlane2Blender.
* One `.obj` has one texture set, so an object with several materials exports
  with the first material's textures.
* Blender stores custom normals as int16 per axis, so regenerated normals can
  differ from the source by ~1e-4. Positions and UVs round-trip exactly.
* The bridge is accurate but not exact on every file: King Air C90B
  `C90B_cockpit.obj` lands up to 0.48 m off on 7 of 358 batches and MD-82
  `gear_main.obj` up to 23.8 m. Both numbers are **identical** on the code from
  before the bone work, so this is a pre-existing gap in the `ANIM_*` →
  XPlane2Blender conversion, not the bone tree.
* A batch that is not one hinge on one dataref keeps the animation it came in
  with instead of being re-keyed from Blender (see
  [animation](animation.md)); editing its geometry still exports, editing its
  motion needs the `ANIM_*` text in `xp8_events`.
* A file that mixes cockpit panel modes cannot be reproduced exactly: XP2B's
  layer holds one *Panel Texture Mode*, so the first mode wins and the mixture is
  reported.
* `ATTR_manip_device` (TDS GTN750Xi and friends) is a third-party extension with
  no XP2B counterpart and is reported, not converted.
* With XPlane2Blender enabled, the add-on's own exporter no longer reproduces a
  file byte for byte after the import has converted the scene — that is the
  bridge's trade, see [export](export.md).
