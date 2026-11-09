"""X-Plane surfaces are single-sided; ATTR_no_cull is the exception.

    <blender> -b --factory-startup --python verify/cull_test.py

Checks the material flag Blender actually uses for this
(Settings > Surface > Backface Culling > Camera): every material the importer
builds starts with it on, and only the batch behind ATTR_no_cull gets a copy
with it off.  The Solid-viewport switch lives on the viewport, not the material,
so it is exercised by the operator rather than asserted here.
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


def clean():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for me in list(bpy.data.meshes):
        bpy.data.meshes.remove(me)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)


def rows():
    out = []
    for mat in bpy.data.materials:
        if not mat.use_nodes:
            continue
        out.append({"name": mat.name,
                    "backface_culling": bool(mat.use_backface_culling)})
    return out


# 1. a plain object: single-sided by default
clean()
try:
    bpy.ops.import_scene.xplane_obj(filepath=os.path.join(FIX, "mini.obj"))
except RuntimeError as exc:
    print("   import reported: %s" % str(exc)[:100])
plain = rows()
print("CASE " + json.dumps({"file": "mini.obj", "materials": plain}))
check(len(plain) > 0, "mini.obj: no material built")
for m in plain:
    check(m["backface_culling"],
          "%s: backface culling is off, but X-Plane is single-sided by default"
          % m["name"])

# 2. ATTR_no_cull: the exception
clean()
try:
    bpy.ops.import_scene.xplane_obj(filepath=os.path.join(FIX, "nocull.obj"))
except RuntimeError as exc:
    print("   import reported: %s" % str(exc)[:100])
cull_rows = rows()
print("CASE " + json.dumps({"file": "nocull.obj", "materials": cull_rows}))

nocull = [m for m in cull_rows if "no_cull" in m["name"].lower()]
normal = [m for m in cull_rows if "no_cull" not in m["name"].lower()]
check(len(nocull) == 1,
      "nocull.obj: expected one ATTR_no_cull material copy, got %d"
      % len(nocull))
check(len(normal) >= 1, "nocull.obj: the culled quad lost its material")
for m in nocull:
    check(m["backface_culling"] is False,
          "%s: ATTR_no_cull material still has culling on" % m["name"])
for m in normal:
    check(m["backface_culling"] is True,
          "%s: normal material lost its culling" % m["name"])

# 3. the viewport helper must not explode without a window (headless)
try:
    IMP._enable_viewport_backface_culling()
    print("VIEWPORT helper ran headless without raising")
except Exception as exc:                                    # noqa: BLE001
    check(False, "viewport helper raised headless: %r" % exc)

print("\nRESULT: %s" % ("PASS" if not fails else "FAIL (%d)" % len(fails)))
