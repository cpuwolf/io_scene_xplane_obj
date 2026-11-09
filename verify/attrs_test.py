"""The four ATTR categories that used to be dropped now have a home.

    <blender> -b --factory-startup --python verify/attrs_test.py

ATTR_hard -> Material Surface Type, ATTR_hud_glass -> Object HUD Glass,
ATTR_depth -> an XPlane2Blender custom attribute (written back verbatim), and
none of them should still show up as "is not converted".
"""
import json
import os
import sys

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

import addon_utils                                          # noqa: E402

addon_utils.enable("io_xplane2blender", default_set=True)

import io_scene_xplane_obj as IMP                           # noqa: E402

try:
    IMP.register()
except Exception:                                           # noqa: BLE001
    pass

FIX = os.path.join(REPO, "verify", "fixtures")
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg)
    return cond


for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for me in list(bpy.data.meshes):
    bpy.data.meshes.remove(me)
for mat in list(bpy.data.materials):
    bpy.data.materials.remove(mat)

try:
    r = bpy.ops.import_scene.xplane_obj(filepath=os.path.join(FIX, "attrs.obj"))
    print("import:", sorted(r))
except RuntimeError as exc:
    print("import reported: %s" % str(exc)[:120])

mats = []
for mat in bpy.data.materials:
    if not mat.use_nodes:
        continue
    mats.append({
        "name": mat.name,
        "surface": getattr(mat.xplane, "surfaceType", None),
        "deck": getattr(mat.xplane, "deck", None),
        "draped": getattr(mat.xplane, "draped", None),
        "custom": [a.name for a in getattr(mat.xplane, "customAttributes", [])],
    })

objs = []
for ob in bpy.data.objects:
    if ob.type != 'MESH':
        continue
    objs.append({
        "name": ob.name,
        "hud_glass": getattr(ob.xplane, "hud_glass", None),
        "custom": [a.name for a in getattr(ob.xplane, "customAttributes", [])],
    })

print("CASE " + json.dumps({"materials": mats, "objects": objs}, indent=1))

# ATTR_hard 3 == Asphalt (index into the surface list)
check(any(m["surface"] == "asphalt" for m in mats),
      "no material took ATTR_hard 3 -> asphalt (got %r)"
      % [m["surface"] for m in mats])

# ATTR_hud_glass is an object property
check(any(o["hud_glass"] is True for o in objs),
      "no object took ATTR_hud_glass")

# ATTR_depth has no field at all - it must survive as a custom attribute
check(any("ATTR_depth" in o["custom"] for o in objs),
      "ATTR_depth did not land in any object's custom attributes")

# and nothing should have been reported as unconvertible for these categories
unconverted = [w for w in IMP.xp2b._LAST_WARNINGS if "is not converted" in w] \
    if hasattr(IMP.xp2b, "_LAST_WARNINGS") else []
check(not unconverted,
      "these were still reported as unconverted: %r" % unconverted)

print("\nRESULT: %s" % ("PASS" if not fails else "FAIL (%d)" % len(fails)))
