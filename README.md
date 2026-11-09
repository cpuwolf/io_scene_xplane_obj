# X-Plane OBJ8 Import & Export — `.obj` and `.acf`, in and out of Blender

Import an [X-Plane OBJ8](https://developer.x-plane.com/article/obj8-file-format-specification/)
`.obj` into a Blender scene you can edit — geometry, materials, animations,
manipulators and lights — and export it back out. Reading and writing are one
package: the exporter that writes X-Plane's `.obj`
([XPlane2Blender](https://github.com/X-Plane/XPlane2Blender)) ships inside this
add-on, so there is nothing else to install and no version to match up.

**File > Import > X-Plane OBJ8 (.obj)** (or just drag the file onto the
viewport), edit, **File > Export > X-Plane Object (.obj)**. There is no convert
step to remember and nothing to set up per file.

Drop an aircraft's **`.acf`** instead and every object it attaches is imported in
one go — the whole aircraft, not one part of it — under one parent collection
named after the `.acf`:

```
Cessna_172SP.acf                  collection — one per .acf, an organiser only
├ Cessna_172SP_cockpit.obj        collection — the exportable XPlane2Blender layer
├ fuselage.obj                    collection
└ wings.obj                       collection  (…one per .obj the aircraft attaches)
```

## The pipeline

```
X-Plane aircraft
  .acf  →  every .obj it attaches
  .obj  →  one object
  folder of .obj files
        │
        │  ①  IMPORT — this add-on
        │      File > Import, or drop the file on the viewport
        ▼
Blender
  one exportable collection per .obj: geometry, materials, animations,
  manipulators, cockpit panels — editable and previewable
        │
        │  ②  EDIT — Blender
        ▼
this add-on's bundled exporter
  File > Export > X-Plane Object (.obj)
        │
        │  ③  EXPORT — same package, nothing else installed
        ▼
X-Plane
```

The exporter inside this package is XPlane2Blender's **armature build** — 4.5.0
plus the Blender 5.x fix — and which build it is matters: every official release
up to 4.5.0 still reads `Action.groups` / `Action.fcurves`, which Blender 5
removed, and then writes **no animations at all** while the export reports
*finished*. That is why the exporter is bundled at that exact commit rather than
left for you to install: upstream authors, licence and the two changes made when
vendoring it are in [CREDITS.md](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/CREDITS.md).

![Importing an X-Plane OBJ8 object into Blender](blender_import_demo.gif)

```
C90B_cockpit.obj                 collection — the exportable XPlane2Blender layer
└ C90B_cockpit.obj.worldcenter   Empty — move this to move the whole object
  └ C90B_cockpit.obj.bones       armature — one bone per ANIM block
    └ C90B_cockpit_000 …         one mesh per geometry command
```

## Install

One add-on, one step.

1. **Preferences > Get Extensions > search `X-Plane OBJ8` > Install.**
   (No platform account yet? Drag the release zip onto Blender instead.)
2. **Restart Blender.** Add-ons load their modules at startup, so the restart is
   what makes it take effect.

Blender 5.0 or newer. The exporter is inside this package, so nothing else has to
be installed and nothing can go out of sync.

If you already run a standalone XPlane2Blender, this add-on steps aside and uses
yours — it never registers its bundled copy on top of one that is enabled.

## What people ask first

| question | answer |
| --- | --- |
| Does it export too? | Yes. The exporter (XPlane2Blender's armature build) is bundled, so `File > Export > X-Plane Object (.obj)` works out of the same install. |
| Do I lose the original file? | No. The file's own text is kept on the collection (custom properties), and everything the conversion cannot express is reported rather than dropped silently. |
| Will a lever still move? | Yes — one bone per `ANIM_*` block, one keyframe per key the file has, key N on frame N. Drag the time line to watch it. |
| Are X-Plane's normal maps handled? | Yes, as the **combo map** they are: `R,G` → normal (Z rebuilt), blue → Metallic (metalness mode), alpha → gloss inverted into Roughness. `TEXTURE_MAP` (12.1) included. |
| Cockpit panels, manipulators, light levels? | `ATTR_cockpit*`, `ATTR_manip_*`, `ATTR_light_level`, `ATTR_draw_disable`, `ATTR_solid_camera` all land on the XPlane2Blender material/object/layer fields they belong to. |
| Texture paths in the exported file? | The texture set is also written onto the XP2B **layer**, which is where its exporter reads them from. |
| Files the sim loads but exporters mangle? | A missing `POINT_COUNTS` and trailing `#` comments are both tolerated. |
| The whole aircraft at once? | Drag its **`.acf`** onto the viewport: every `.obj` the aircraft attaches is imported, one collection each, all of them children of one collection named after the `.acf`. The `.acf` itself is only read. |

## Docs

| | |
| --- | --- |
| [install](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/install.md) | requirements, checking it worked, uninstall |
| [credits](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/CREDITS.md) | the bundled exporter: authors, licence, what was changed |
| [import](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/import.md) | the tree, meshes, materials, combo maps, tolerant parsing, where the data lives |
| [animation](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/animation.md) | armature, keys, `ANIM_show/hide`, the dataref panel, import options |
| [XPlane2Blender bridge](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/xp2b-bridge.md) | what maps to what, and the two exceptions worth knowing |
| [known limits](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/known-limits.md) | what is not carried, and how accurate the bridge is |
| [development](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/development.md) | dev link, tests, releasing |

![Laminar's King Air C90B imported from its own .obj](aircraft_demo.gif)

Laminar's King Air C90B, imported straight from the `.obj` X-Plane ships.

## License

GPL-3.0-or-later, matching the
[extension platform requirements](https://docs.blender.org/manual/en/latest/advanced/extensions/licenses.html).
