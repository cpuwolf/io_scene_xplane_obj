# Known limits

* `LINES` are imported read-only and exported from their stored indices.
* Particles are referenced, not authored: `PARTICLE_SYSTEM` and `EMITTER` become
  the layer's particle file and one Empty per emitter, but the `.pss` itself is
  neither read nor written — the particles' own shape, size and lifetime curves
  stay in that file and are edited with Laminar's particle editor.
* An `EMITTER` is only converted where XPlane2Blender can write one: the layer
  needs a `.pss`, and the scene's X-Plane version must be 1130 or later (Scene >
  X-Plane > Version). Otherwise the emitters are reported and left out rather
  than handed over and cancelling the export.
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
* The exporter writes from the object tree, not from the file's original text:
  it is XPlane2Blender's writer (bundled, see [CREDITS](../CREDITS.md)), so a
  batch's geometry comes out as the scene has it, not as the bytes were.
* **Disabling or uninstalling while an aircraft is open.** The animation in the
  scene is keyed through the exporter's own properties
  (`pose.bones["x"].xplane.datarefs[0].value`), so they cannot stay unregistered:
  Blender crashes on the next depsgraph build. The add-on unregisters them for
  real (so the next version can take over) and puts them back on the following
  event-loop tick if nothing did - which is what happens on a plain disable or an
  uninstall. Nothing to do about it from your side, but if you disable the add-on
  on purpose, restart Blender afterwards rather than trusting the session.
