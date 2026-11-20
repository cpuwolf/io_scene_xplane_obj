"""Is the *installed* add-on the working tree, and does it have the VR bits?

    blender -b --python verify/installed_probe.py

No `--factory-startup`: this reads the real preferences, so it answers "what will
the user actually get when they open Blender", not "what does a clean session
load".  Nothing is written.
"""
import os
import sys

import bpy

MODULE = "bl_ext.user_default.io_scene_xplane_obj"
REPO = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("enabled in prefs:", MODULE in bpy.context.preferences.addons)
module = sys.modules.get(MODULE)
if module is None:
    import addon_utils
    addon_utils.enable(MODULE, default_set=True)
    module = sys.modules.get(MODULE)
where = os.path.realpath(getattr(module, "__file__", "") or "")
print("module path     :", where)
print("points at the repo:", where.startswith(REPO))
manifest = os.path.join(where.rsplit("/", 1)[0] if where else "", "blender_manifest.toml")
version = ""
if os.path.isfile(manifest):
    for line in open(manifest):
        if line.startswith("version"):
            version = line.split("=", 1)[1].strip().strip('"')
            break
print("version         :", version)

for name in ("IMPORT_SCENE_OT_xplane_obj", "IMPORT_SCENE_OT_xplane_acf",
             "OBJECT_OT_xp8_vrconfig_save", "VIEW3D_PT_xplane_vr",
             "EXPORT_OT_xplane_obj"):
    print("%-32s %s" % (name, hasattr(bpy.types, name)))

rna = bpy.ops.import_scene.xplane_obj.get_rna_type()
props = [p.identifier for p in rna.properties]
print("weld_vertices on the import operator:", "weld_vertices" in props)
print("xpvr module:", bool(getattr(module, "xpvr", None)),
      "| vrconfig module:", bool(getattr(module, "vrconfig", None)) or
      "vrconfig" in sys.modules)
