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
    header = ob.parent
    while header is not None and not XM.is_header(header):
        header = header.parent
    rig = ob.parent if XM.is_rig(ob.parent) else None
    placement = list(rig.location) if rig is not None else None
    rows.append((os.path.basename(header.get(XM.PROP_SOURCE, "?")) if header
                 else "?", ob.name, placement, (lo + hi) / 2, hi - lo,
                 rig.get(XM.PROP_DATAREF) if rig is not None else None))

placed = [r for r in rows if r[2] and any(abs(v) > 1e-9 for v in r[2])]
print("meshes %d, placed parts %d" % (len(rows), len(placed)))
print("%-26s %-24s %-26s %s" % ("part", "rig offset (Blender m)",
                               "world centre", "size"))
for src, name, pl, ctr, sz, _ in sorted(placed,
                                        key=lambda r: -max(abs(r[3].x),
                                                           abs(r[3].y))):
    p = pl
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
