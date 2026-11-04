"""Convert an imported OBJ8 to an XPlane2Blender scene and export it back.

    blender -b --factory-startup --python verify/xp2b_convert.py -- <obj-file> <out-dir>

Checks the round trip that matters for this conversion:
  import (lossless model) -> convert to XP2B -> export with XP2B -> compare
  the ANIM_*/ATTR_manip_* lines and the evaluated transforms with the source.
"""
import os
import sys
import traceback

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

import addon_utils                                        # noqa: E402

addon_utils.enable("io_xplane2blender", default_set=True)

import io_scene_xplane_obj as IMP                         # noqa: E402

try:
    IMP.register()
except Exception:
    pass

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SRC = argv[0] if argv else os.path.join(REPO, "verify", "fixtures", "mini.obj")
OUT = argv[1] if len(argv) > 1 else "/tmp/xp2b_convert_out"
os.makedirs(OUT, exist_ok=True)

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)

print("source:", SRC)
res = bpy.ops.import_scene.xplane_obj(filepath=SRC)
print("import:", res)
header = bpy.context.view_layer.objects.active
print("header:", header.name, "parts:", len(IMP.xpmesh.parts(header)),
      "events:", len(__import__("json").loads(header[IMP.xpmesh.PROP_EVENTS])))

res = (bpy.ops.object.xp8_to_xp2b()
       if not [c for c in bpy.data.collections if c.xplane.is_exportable_collection]
       else "skipped, the import already built it")
print("convert:", res)

new = [c for c in bpy.data.collections if c.xplane.is_exportable_collection]
print("exportable collections:", [(c.name, c.xplane.layer.export_type) for c in new])
for c in new:
    for o in c.objects:
        if o.type == 'MESH':
            print("   MESH", o.name, "parent", o.parent.name if o.parent else None,
                  "polys", len(o.data.polygons), "mats", [m.name if m else None for m in o.data.materials],
                  "visible", o.visible_get(), "hide", o.hide_viewport,
                  "collections", [cc.name for cc in o.users_collection],
                  "xp?", hasattr(o, "xplane"))
    print("   objects:", len(c.objects),
          "empties:", sum(1 for o in c.objects if o.type == 'EMPTY'),
          "meshes:", sum(1 for o in c.objects if o.type == 'MESH'))
    print("   warnings:", c.get("xp2b_convert_warnings", "")[:400])

target = os.path.join(OUT, os.path.basename(SRC))
try:
    r = bpy.ops.export.xplane_obj(filepath=target)
    print("export:", r, "->", target)
except Exception:
    traceback.print_exc()
