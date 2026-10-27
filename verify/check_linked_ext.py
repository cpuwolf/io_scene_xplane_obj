"""Smoke test through the *installed* (symlinked) extension, not a sys.path import.

    blender -b --factory-startup --python verify/check_linked_ext.py -- [file.obj]
"""
import hashlib
import os
import sys

import bpy

MOD = "bl_ext.user_default.io_scene_xplane_obj"
bpy.ops.preferences.addon_enable(module=MOD)
if MOD not in bpy.context.preferences.addons:
    print("FAIL: %s did not enable" % MOD)
    sys.exit(1)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SRC = argv[0] if argv else (
    "/Users/rourou/X-Plane 12/Aircraft/King Air C90B EVO G1000 V1.3.1"
    "/objects/C90B_exterior/C90B_ext_glass.obj")
DST = os.path.expanduser("~/.hermes/cache/scratch/linked_smoke.obj")

print("extension enabled:", MOD in bpy.context.preferences.addons)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module=MOD)      # re-enable after the wipe

r = bpy.ops.import_scene.xplane_obj(filepath=SRC)
print("import:", r)
header = [o for o in bpy.data.objects if o.get("xp8_role") == "header"]
print("header:", [h.name for h in header])
print("parts:", [(c.name, len(c.data.polygons)) for c in header[0].children][:4])
print("material:", [m.name for m in header[0].children[0].data.materials])

r = bpy.ops.export_scene.xplane_obj(filepath=DST, mode="FILE",
                                    preserve_unmodified=True)
print("export:", r)


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


print("source  ", sha(SRC))
print("exported", sha(DST))
print("BYTE IDENTICAL:", sha(SRC) == sha(DST))
