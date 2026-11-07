# X-Plane OBJ8 Object — import X-Plane `.obj` into XPlane2Blender's scene

Import an [X-Plane OBJ8](https://developer.x-plane.com/article/obj8-file-format-specification/)
`.obj` into a Blender scene that [XPlane2Blender](https://github.com/X-Plane/XPlane2Blender)
writes back out — geometry, materials, animations, manipulators and lights.

**File > Import > X-Plane OBJ8 (.obj)** (or just drag the file onto the
viewport), edit, **File > Export > X-Plane Object (.obj)**. There is no convert
step to remember and nothing to set up per file: the import builds the
XPlane2Blender collection itself.

Drop an aircraft's **`.acf`** instead and every object it attaches is imported in
one go — the whole aircraft, not one part of it.

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
XPlane2Blender, revised build
  File > Export > X-Plane Object (.obj)
        │
        │  ③  EXPORT — with the revised build, linked below
        ▼
X-Plane
```

The official XPlane2Blender release cannot do ③ on Blender 5.x: it writes no
animations at all and reports no error, so the round trip only closes with the
revised build —
**[cpuwolf/XPlane2Blender, tag `blender-5x-armature-v1`](https://github.com/cpuwolf/XPlane2Blender/tree/blender-5x-armature-v1)**
([download](https://github.com/cpuwolf/XPlane2Blender/archive/refs/tags/blender-5x-armature-v1.zip),
[install steps](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/install.md)) —
the same build this add-on is tested against.

![Importing an X-Plane OBJ8 object into Blender](blender_import_demo.gif)

```
C90B_cockpit.obj                 collection — the exportable XPlane2Blender layer
└ C90B_cockpit.obj.worldcenter   Empty — move this to move the whole object
  └ C90B_cockpit.obj.bones       armature — one bone per ANIM block
    └ C90B_cockpit_000 …         one mesh per geometry command
```

## Install

1. **Preferences > Get Extensions > search `X-Plane OBJ8` > Install.**
   (No platform account yet? Drag the release zip onto Blender instead.)
2. **Restart Blender.**

Blender 4.2 or newer.

Exporting is [XPlane2Blender](https://github.com/X-Plane/XPlane2Blender)'s job, so
install that too — and on Blender 5.x it must be the **armature build**, not the
official release: the official one still reads `Action.groups` /
`Action.fcurves`, which Blender 5 removed, and then exports **no animations at
all** with no error to show for it. That build, and the two commands to unpack
it, are in
[docs/install.md](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/install.md).

## What people ask first

| question | answer |
| --- | --- |
| Does it export too? | It is an **importer**: the export is XPlane2Blender's. This add-on gets the file *into* its authoring scene correctly — that is the hard part. |
| Do I lose the original file? | No. The file's own text is kept on the collection (custom properties), and everything the conversion cannot express is reported rather than dropped silently. |
| Will a lever still move? | Yes — one bone per `ANIM_*` block, one keyframe per key the file has, key N on frame N. Drag the time line to watch it. |
| Are X-Plane's normal maps handled? | Yes, as the **combo map** they are: `R,G` → normal (Z rebuilt), blue → Metallic (metalness mode), alpha → gloss inverted into Roughness. `TEXTURE_MAP` (12.1) included. |
| Cockpit panels, manipulators, light levels? | `ATTR_cockpit*`, `ATTR_manip_*`, `ATTR_light_level`, `ATTR_draw_disable`, `ATTR_solid_camera` all land on the XPlane2Blender material/object/layer fields they belong to. |
| Texture paths in the exported file? | The texture set is also written onto the XP2B **layer**, which is where its exporter reads them from. |
| Files the sim loads but exporters mangle? | A missing `POINT_COUNTS` and trailing `#` comments are both tolerated. |
| The whole aircraft at once? | Drag its **`.acf`** onto the viewport: every `.obj` the aircraft attaches is imported, one collection each. The `.acf` itself is only read. |

## Docs

| | |
| --- | --- |
| [install](https://github.com/cpuwolf/io_scene_xplane_obj/blob/main/docs/install.md) | both add-ons, requirements, checking it worked |
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
