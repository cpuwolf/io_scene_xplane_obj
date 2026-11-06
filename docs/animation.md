# Animation

A `.obj` animation is driven by a **dataref** — a value the sim owns at run time
(`sim/flightmodel2/gear/deploy_ratio[0]`), not by a clock. Blender has no such
input, so the time line *is* the dataref.

## How the import represents it

* **One armature per file, bone for bone.** `ANIM_begin`/`ANIM_end` blocks are a
  hierarchy (a translate, a hide, a pivot plus a roll around one yoke), so the
  bone tree is a copy of the block tree: one bone per transform command, named
  after its block (`b181_1`). A batch hangs off the bone of its innermost block,
  so parts that share a block share a bone.
* OBJ8 has no skinning, so **no Armature modifier is added**: the bones move the
  batch, the vertices stay the file's own.
* **Every bone is `AXIS_ANGLE`.** `ANIM_rotate_begin <x> <y> <z> <dataref>` is an
  axis plus a keyed angle, and that is what the bone stores: the angle channel
  carries the file's own degrees and the axis channel the file's own axis
  (`xp8_axis`). A 1200° trim wheel keeps turning instead of being folded to −120°
  by a quaternion's short arc. The degree is also keyed into `xp8_angle_deg`.
* **One keyframe per key the file has, key N on frame N.** A two-endpoint
  `ANIM_rotate` is two keyframes, a 15-key gauge table fifteen. That is
  XPlane2Blender's convention too, so a scene authored here and one authored
  there agree about which frame means which state. Index, not dataref value:
  gauges key on `-9999 … 9999` and a value-linear time line would squeeze twenty
  keys into two adjacent frames.
* **`ANIM_show`/`ANIM_hide` stay text** (`xp8_anim_vis` on the batch) and are
  written back as they came in. Nothing in the scene is hidden and nothing is
  keyframed: a dataref is not a clock, so a `hide_viewport` keyframe only
  invented a schedule — it hid parts the user then could not find again, and
  XPlane2Blender reads that flag as "export the animation only" and drops the
  geometry without a word.

Drag the time line to watch the gear retract, the altimeter needle sweep, the
yoke move in pitch and roll at once.

## Editing it

Select a bone-managed batch and the panel shows what drives it:

| control | what it does |
| --- | --- |
| **Dataref** | the sim variable, editable; *Pick a dataref from this object* lists the ones the file already uses |
| **Value@1 / @last key** | the dataref values at the first and last key, i.e. what the keys' x values mean (`0..1` for a ratio, `-18..89` for prop pitch) |
| **Drop the show/hide rules** | the `ANIM_show`/`ANIM_hide` ranges this part carries |

All three are plain custom properties (`xp8_dataref`, `xp8_value_min`,
`xp8_value_max`, `xp8_anim_vis`), visible in *Custom Properties* and saved with
the file. Nothing about the animation is hidden data.

**A batch is only re-keyed when it really is one hinge driven by one dataref.**
OBJ8's key table has a single x axis, so anything else (a gear leg that folds
*and* steers, a wiper arm and blade on two datarefs) would silently lose one of
its inputs. Those batches keep the animation they came in with, and the export
said so, part by part.

## Import options

| option | for |
| --- | --- |
| **Animation at** (0..1) | where in the dataref range to look — the pose you edit in (gear down, doors open) |
| **Pin datarefs** (`dref=value, …`) | hold one dataref still while the rest sweeps, e.g. a prop at 0° |
| **Bake Actions** off | rigs get a static pose only, no Action sweeping the range |

## Exporting animation

The supported path is XPlane2Blender: it samples the pose at the frames the
dataref keys sit on, which is exactly the mapping the import used. See
[XPlane2Blender bridge](xp2b-bridge.md).

The add-on's own exporter (see [export](export.md)) additionally offers:

| mode | what happens |
| --- | --- |
| **From the file** (default) | the `ANIM_*` command stream is written exactly as imported; edit it as text in `xp8_events` |
| **From Blender (rigs)** | every animated batch's block is regenerated from its rig's Action: keyframes become `ANIM_rotate_begin`/`ANIM_rotate_key` tables (and `ANIM_trans_*`), and the file's own animation commands are dropped |
