# X-Plane aircraft, opened in Blender — and handed back

One install. Import a whole aircraft or a single part, edit anything, export it
back to X-Plane. **Including the animations** — the ones X-Plane ships already
compiled away.

<img src="blender_import_demo.gif" width="640" alt="Importing an X-Plane aircraft into Blender">

## Four things people use it for

### 1. Open an aircraft you could only fly

A shipped aircraft is an `.acf` plus a pile of OBJ8 files: text, but baked — no
scene, no rig, no source to edit. You have the aircraft, not its project.

Drop the `.acf` and the **entire aircraft** lands in Blender: every part, its
materials, its cockpit, its animations, in one collection named after the aircraft.

### 2. Get the animations back

This is the part that is hard to find anywhere else. Every `ANIM_` block becomes a
**bone with keyframes on the time line**, keyed to the same frames the file uses:

* yokes and throttles turn, gear retracts, flaps travel, doors open — scrub the
  time line and watch it happen
* from there it is a normal armature: re-time it, fix a part that comes apart,
  mirror a motion, drive it from a different dataref, or add a control that was
  never in the file

X-Plane's own OBJ8 keeps nothing you could rebuild that from. This writes it back
as an editable rig.

<img src="aircraft_demo.gif" width="640" alt="Laminar's King Air C90B imported from the .obj X-Plane ships">

### 3. Fix the aircraft that broke

* an update changed the datarefs and now a lever moves the wrong way
* a hinge was always 2 cm off, or a panel texture lost its gloss
* forty-year-old freeware you refuse to let die, and X-Plane 12 refuses to light
  properly
* your own aircraft, whose original project is on a drive you no longer have

Import, fix the thing, export. It is the same aircraft, one part better.

### 4. Keep a project maintainable

Importing a folder of OBJ8 into one scene is how a legacy aircraft becomes
something you can work on again: real collections, one mesh per part, materials
as materials, animations as armatures — and an exporter on the other side.

## What survives the trip

| In the file | In Blender |
| --- | --- |
| geometry, UVs, per-vertex normals | one mesh per part, normals kept as authored (not averaged) |
| `ANIM_*` blocks | bones + keyframes, drag the time line to pose it |
| `ANIM_show` / `ANIM_hide` | attached to the bone that owns that part |
| mixed normal-metalness **combo maps** | split into Normal / Metallic / Roughness, ready to paint |
| cockpit panels, devices, lit regions | X-Plane layer and material settings — editable, and written back |
| manipulators (drag, rotate, push, toggle, axis, detents) | the object's manipulator settings, same numbers |
| light levels, `ATTR_draw_disable`, culling, LODs, `LIGHT_PARAM` | the fields X-Plane reads, plus real light objects |
| the file's own text | kept on the collection, so nothing is guessed twice |

And the export side is in the same package — no second add-on, no version to match.

## Quick start

1. **Preferences > Get Extensions**, search **X-Plane OBJ8**, install, restart
   Blender once.
2. Drag an aircraft's **`.acf`** (whole aircraft) or a **`.obj`** onto the viewport.
3. Edit. **File > Export > X-Plane Object (.obj)**.

Blender 5.0 or newer. Every part is one collection, so you can hide what you are
not touching and work in a 400-part cockpit without scrolling.

## Tested on

Laminar's Cessna 172 (42 objects from its `.acf`), Boeing 737-800, Airbus A330
cockpits, plus add-on aircraft such as the King Air C90B — whose animations are
the kind of thing you only get back with an importer that reads the armature
structure properly. The numbers behind that claim are in
[known limits](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/known-limits.md).

## What it does not do

* **It never writes the `.acf`.** That file is read only, as Laminar asks:
  anything you change goes into the `.obj` files.
* **Per-object attach offsets are not applied.** Every part arrives at its own
  origin, exactly as if you had opened each `.obj` by hand.
* **A few things cannot be expressed in Blender** — an aircraft that mixes cockpit
  panel modes, for instance — and those are reported, not silently dropped.

## Docs

| | |
| --- | --- |
| [install](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/install.md) | requirements, checking it worked, uninstall |
| [import](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/import.md) | what the import builds, combo maps, tolerant parsing |
| [animation](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/animation.md) | the armature, keys, `ANIM_show/hide`, the dataref panel |
| [export bridge](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/xp2b-bridge.md) | what maps to what, and the exceptions worth knowing |
| [known limits](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/known-limits.md) | what is not carried, and how accurate the bridge is |
| [credits](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/CREDITS.md) | the bundled exporter: authors, licence, what was changed |
| [development](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/development.md) | dev link, tests, releasing |

## License

GPL-3.0-or-later, matching the
[extension platform requirements](https://docs.blender.org/manual/en/latest/advanced/extensions/licenses.html).
The exporter inside this package is XPlane2Blender's armature build; its authors
and licence are credited in [CREDITS.md](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/CREDITS.md).

<sub>Not affiliated with Laminar Research. Use it on aircraft you have the right
to modify.</sub>
