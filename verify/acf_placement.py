"""Placement grading: the .acf offset lives on the aircraft, not on objects.

    <blender> -b --python verify/acf_placement.py

Three things have to hold:
  1. `aircraft_placement()` reads the shared offset out of a real .acf, and
     says None for an aircraft that has none (C90B).
  2. Toggling an aircraft collection moves its top-level objects by exactly
     that offset, and toggling again restores every value.
  3. Importing an .obj never moves anything by itself - the geometry stays at
     the OBJ origins, which is the only state XPlane2Blender can export (its
     exporter bakes object transforms into the VT table).
"""
import importlib
import os
import sys

import addon_utils
import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
XM_PKG = "bl_ext.user_default.io_scene_xplane_obj"
if XM_PKG not in bpy.context.preferences.addons:
    addon_utils.enable(XM_PKG, default_set=False)

NAME = os.path.basename(REPO)
addon = __import__(NAME)
ACP = importlib.import_module(NAME + ".acfplace")

XP = os.path.expanduser("~/X-Plane 12/Aircraft/Laminar Research")
LANCAIR = os.path.join(XP, "Lancair Evolution/N844X.acf")
C90B = os.path.join(XP, "Beechcraft King Air C90B/C90B.acf")
OBJ = os.path.join(XP, "Lancair Evolution/objects/n844x_int1.obj")

fails = []


def check(label, ok, detail=""):
    print("%-38s %s  %s" % (label, "OK  " if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


def scene_bbox():
    lo, hi = [1e9] * 3, [-1e9] * 3
    for ob in bpy.data.objects:
        if ob.type != 'MESH':
            continue
        for v in ob.data.vertices:
            p = ob.matrix_world @ v.co
            for i in range(3):
                lo[i] = min(lo[i], p[i])
                hi[i] = max(hi[i], p[i])
    return tuple(round(v, 3) for v in lo), tuple(round(v, 3) for v in hi)


# 1) the file says one offset for the whole aircraft ---------------------------
spec = ACP.aircraft_placement(LANCAIR)
want = (0.0, -4.400000095, 14.600000381)
check("Lancair offset", spec is not None and
      all(abs(spec["xyz"][i] - want[i]) < 1e-3 for i in range(3)),
      "xyz=%s ft" % (None if spec is None
                     else tuple(round(v, 2) for v in spec["xyz"]),))
check("C90B has no offset", ACP.aircraft_placement(C90B) is None)

# 2) toggle an aircraft collection --------------------------------------------
coll = bpy.data.collections.new("VERIFY.acf")
bpy.context.scene.collection.children.link(coll)
mesh = bpy.data.meshes.new("verify")
mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)], [],
                 [(0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)])
ob = bpy.data.objects.new("verify", mesh)
coll.objects.link(ob)
child = bpy.data.objects.new("verify_child", mesh)
coll.objects.link(child)
child.parent = ob                            # a child must not be shifted twice

addon._remember_aircraft(coll, LANCAIR)
check("collection tagged", coll.get(ACP.PROP) is not None)
check("child of the root", child.parent is ob)

bpy.ops.object.xp8_acf_placement_toggle(coll_name=coll.name)
placed = tuple(round(v, 3) for v in ob.matrix_basis.translation)
child_placed = tuple(round(v, 3) for v in child.matrix_basis.translation)
check("toggled on", placed == (0.0, -4.45, -1.341), "root at %s" % (placed,))
check("child not shifted twice", child_placed == (0.0, 0.0, 0.0),
      "child at %s" % (child_placed,))
check("collection remembers state", bool(coll.get(ACP.PROP_ON)))

bpy.ops.object.xp8_acf_placement_toggle(coll_name=coll.name)
back = tuple(round(v, 3) for v in ob.matrix_basis.translation)
check("toggled off restores", back == (0.0, 0.0, 0.0), "root at %s" % (back,))
check("state cleared", not coll.get(ACP.PROP_ON))

# 3) a plain import never shifts by itself -------------------------------------
for stale in list(bpy.data.objects):
    bpy.data.objects.remove(stale, do_unlink=True)
for stale in list(bpy.data.collections):
    bpy.data.collections.remove(stale)
bpy.ops.import_scene.xplane_obj(filepath=OBJ)
lo, hi = scene_bbox()
tagged = [c for c in bpy.data.collections if c.get(ACP.PROP_SOURCE)]
check("import leaves geometry alone", not tagged and lo[1] < -1.0,
      "bbox y %.2f..%.2f, %d aircraft collection(s)"
      % (lo[1], hi[1], len(tagged)))

print()
if fails:
    print("RESULT: FAIL")
    for name in fails:
        print("  - " + name)
    sys.exit(1)
print("RESULT: PASS")
