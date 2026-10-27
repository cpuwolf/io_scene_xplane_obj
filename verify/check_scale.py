"""Is the imported geometry the right size?

Compares three things:
 1. the raw .obj VT data, measured in X-Plane axes
 2. the same geometry imported into Blender, measured in world space
 3. the real King Air C90B dimensions (wingspan 15.32 m, length 10.92 m,
    height 4.36 m) -- and the scene's unit settings
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
import bpy  # noqa

addon = __import__(os.path.basename(REPO))
addon.register()
OC = addon.OC
XM = addon.XM

SAMPLES = ("/Users/rourou/X-Plane 12/Aircraft/King Air C90B EVO G1000 V1.3.1"
           "/objects/C90B_exterior")

# ---- 1. raw file data ------------------------------------------------------
files = sorted(f for f in os.listdir(SAMPLES) if f.lower().endswith(".obj"))
lo = [1e9] * 3
hi = [-1e9] * 3
per_file = {}
for f in files:
    o = OC.parse(os.path.join(SAMPLES, f))
    flo = [1e9] * 3
    fhi = [-1e9] * 3
    for row in o.vt:
        p = (row.px, row.py, row.pz)
        for i in range(3):
            lo[i] = min(lo[i], p[i])
            hi[i] = max(hi[i], p[i])
            flo[i] = min(flo[i], p[i])
            fhi[i] = max(fhi[i], p[i])
    per_file[f] = (flo, fhi)

print("=== raw .obj vertex data, X-Plane axes (metres) ===")
for f in files:
    flo, fhi = per_file[f]
    print("  %-26s x %7.3f..%7.3f  y %7.3f..%7.3f  z %7.3f..%7.3f"
          % (f, flo[0], fhi[0], flo[1], fhi[1], flo[2], fhi[2]))
print("  combined:")
for i, axis in enumerate("xyz"):
    print("    %s %8.3f .. %8.3f   span %7.3f" % (axis, lo[i], hi[i],
                                                  hi[i] - lo[i]))

# ---- 2. the same data through the importer ---------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
us = sc.unit_settings
print("\n=== scene units ===")
print("  unit_system=%s  scale_length=%g  length_unit=%s"
      % (us.system, us.scale_length, us.length_unit))
print("  1 Blender unit = %.4f m" % us.scale_length)

bpy.ops.import_scene.xplane_obj(filepath=os.path.join(SAMPLES, files[0]),
                                import_folder=True)
meshes = [o for o in bpy.data.objects
          if o.type == 'MESH' and XM.is_batch(o)]
blo = [1e9] * 3
bhi = [-1e9] * 3
scales = set()
for ob in meshes:
    scales.add(tuple(round(v, 6) for v in ob.scale))
    for corner in ob.bound_box:
        w = ob.matrix_world @ __import__("mathutils").Vector(corner)
        for i in range(3):
            blo[i] = min(blo[i], w[i])
            bhi[i] = max(bhi[i], w[i])
print("\n=== imported, Blender world space ===")
print("  objects: %d   object scales seen: %s" % (len(meshes), sorted(scales)))
for i, axis in enumerate("XYZ"):
    print("    %s %8.3f .. %8.3f   span %7.3f" % (axis, blo[i], bhi[i],
                                                  bhi[i] - blo[i]))

# ---- 3. compare ------------------------------------------------------------
print("\n=== comparison (Blender X = wingspan, Y = length, Z = height) ===")
print("  raw file   x %6.3f  y %6.3f  z %6.3f" % (hi[0] - lo[0], hi[2] - lo[2],
                                                  hi[1] - lo[1]))
print("  imported   X %6.3f  Y %6.3f  Z %6.3f" % (bhi[0] - blo[0],
                                                  bhi[1] - blo[1],
                                                  bhi[2] - blo[2]))

REAL = {"wingspan": 15.32, "length": 10.92, "height": 4.36}
print("\n  real King Air C90B: wingspan 15.32 m, length 10.92 m, height 4.36 m")
print("  imported/reported ratio:"
      "  X %.3f   Y %.3f   Z %.3f"
      % ((bhi[0] - blo[0]) / REAL["wingspan"], (bhi[1] - blo[1]) / REAL["length"],
         (bhi[2] - blo[2]) / REAL["height"]))

# a unit sanity check that does not depend on the aircraft: the decal strip is
# 5.1467 m from the centreline in the file, so it must be 5.1467 m in Blender
print("\n=== per-object spot check (first 3 meshes) ===")
for ob in meshes[:3]:
    pts = [ob.matrix_world @ v.co for v in ob.data.vertices]
    if not pts:
        continue
    sx = max(p.x for p in pts) - min(p.x for p in pts)
    sy = max(p.y for p in pts) - min(p.y for p in pts)
    sz = max(p.z for p in pts) - min(p.z for p in pts)
    print("  %-22s X %7.3f  Y %7.3f  Z %7.3f  verts %d"
          % (ob.name, sx, sy, sz, len(pts)))
