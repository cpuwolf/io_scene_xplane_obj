"""Import one OBJ8 headlessly and print every convert warning.

    blender -b --factory-startup --python scripts/import_warnings.py -- <file.obj>

The conversion stores its warnings on the exportable collection
(`xp2b_convert_warnings`), so this reads the same list the UI reports.
Blender also prints each report to stdout as `Warning: ...`.
"""
import json
import os
import sys

import bpy
import addon_utils

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

addon_utils.enable("io_xplane2blender", default_set=True)
import io_scene_xplane_obj as IMP                            # noqa: E402

try:
    IMP.register()
except Exception:
    pass

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SRC = argv[0]

print("source:", SRC)
print("import:", bpy.ops.import_scene.xplane_obj(filepath=SRC))

for coll in bpy.data.collections:
    if coll.xplane.is_exportable_collection:
        print("collection:", coll.name, "meshes:",
              len([o for o in coll.objects if o.type == 'MESH']))
        raw = coll.get("xp2b_convert_warnings")
        if raw:
            ws = json.loads(raw)
            print("CONVERT WARNINGS: %d" % len(ws))
            for i, m in enumerate(ws, 1):
                print("%3d. %s" % (i, m))
        else:
            print("CONVERT WARNINGS: none")
