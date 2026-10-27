"""Where do the static-ANIM_trans parts land? Catches double-offsets."""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
import bpy                                          # noqa
from mathutils import Vector                        # noqa

addon = __import__(os.path.basename(REPO))
addon.register()
XM = addon.XM

SAMPLES = ("/Users/rourou/X-Plane 12/Aircraft/King Air C90B EVO G1000 V1.3.1"
           "/objects/C90B_exterior")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.xplane_obj(filepath=os.path.join(SAMPLES,
                                                      "C90B_fuselage.obj"),
                                import_folder=True)

rows = []
for ob in bpy.data.objects:
    if ob.type != 'MESH' or not XM.is_batch(ob):
        continue
    lo = Vector((1e9,) * 3)
    hi = Vector((-1e9,) * 3)
    for c in ob.bound_box:
        w = ob.matrix_world @ Vector(c)
        for i in range(3):
            lo[i] = min(lo[i], w[i])
            hi[i] = max(hi[i], w[i])
    rows.append((os.path.basename(ob.parent.get(XM.PROP_SOURCE, "?")), ob.name,
                 ob.get(XM.PROP_PLACEMENT), (lo + hi) / 2, hi - lo,
                 ob.get("xp8_dummy")))

placed = [r for r in rows if r[2]]
print("meshes %d, placed parts %d" % (len(rows), len(placed)))
print("%-26s %-24s %-26s %s" % ("part", "placement (Blender m)",
                               "world centre", "size"))
for src, name, pl, ctr, sz, _ in sorted(placed,
                                        key=lambda r: -max(abs(r[3].x),
                                                           abs(r[3].y))):
    p = json.loads(pl)
    bad = "  <== outside the airframe" if (abs(ctr.x) > 8.5 or
                                           abs(ctr.y) > 12) else ""
    print("%-26s (%6.2f %6.2f %6.2f)  (%6.2f %6.2f %6.2f)  (%5.2f %5.2f %5.2f)%s"
          % (name[-26:], p[0], p[1], p[2], ctr.x, ctr.y, ctr.z,
             sz.x, sz.y, sz.z, bad))

lo = Vector((1e9,) * 3)
hi = Vector((-1e9,) * 3)
for ob in bpy.data.objects:
    if ob.type != 'MESH' or not XM.is_batch(ob):
        continue
    for c in ob.bound_box:
        w = ob.matrix_world @ Vector(c)
        for i in range(3):
            lo[i] = min(lo[i], w[i])
            hi[i] = max(hi[i], w[i])
print("\nassembled bbox X %.2f..%.2f  Y %.2f..%.2f  Z %.2f..%.2f"
      % (lo.x, hi.x, lo.y, hi.y, lo.z, hi.z))
