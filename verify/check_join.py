"""What breaks when two imported objects are joined before export?"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
import bpy                                   # noqa

addon = __import__(os.path.basename(REPO))
addon.register()
OC = addon.OC
XM = addon.XM

SRC = ("/Users/rourou/X-Plane 12/Aircraft/King Air C90B EVO G1000 V1.3.1"
       "/objects/C90B_exterior/C90B_fuselage.obj")
OUT = "/tmp/join_test.obj"

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.xplane_obj(filepath=SRC)
header = [o for o in bpy.data.objects if XM.is_header(o)][0]
parts = [c for c in header.children if c.get(XM.PROP_ROLE) == XM.ROLE_TRIS]
print("imported parts: %d" % len(parts))
for p in parts[:4]:
    print("  %-22s leaf %-3s polys %5d placement %s"
          % (p.name, p.get(XM.PROP_LEAF), len(p.data.polygons),
             p.get(XM.PROP_PLACEMENT)))

# join part 2 into part 0 the way a user does it: select both, ctrl+J
a, b = parts[0], parts[1]
a_name, b_name = a.name, b.name          # the reference dies with the join
print("\njoining %s (leaf %s) into %s (leaf %s)"
      % (b_name, b.get(XM.PROP_LEAF), a_name, a.get(XM.PROP_LEAF)))
polys_before = len(a.data.polygons) + len(b.data.polygons)
bpy.ops.object.select_all(action='DESELECT')
b.select_set(True)
a.select_set(True)
bpy.context.view_layer.objects.active = a
bpy.ops.object.join()
print("joined: %d polys, survivor keeps leaf=%s placement=%s"
      % (len(a.data.polygons), a.get(XM.PROP_LEAF), a.get(XM.PROP_PLACEMENT)))
print("object %r still exists: %s" % (b_name, b_name in bpy.data.objects))

# export and inspect the result
stats = XM.export_header(bpy.context, header, OUT,
                         report=lambda lvl, msg: print("   report:", msg))
print("\nexported: verbatim=%s vt=%d idx=%d" % (stats["verbatim"],
                                                stats["vt"], stats["indices"]))
src = OC.parse(SRC)
out = OC.parse(OUT)
print("leaves: source %d -> exported %d" % (len(src.leaves), len(out.leaves)))
bad = [(i, lf.offset, lf.count) for i, lf in enumerate(out.leaves)
       if lf.offset + lf.count > len(out.idx) or lf.count == 0]
print("index table length: %d" % len(out.idx))
print("leaf spans that point outside the index table:")
for i, off, cnt in bad:
    print("   leaf %-2d TRIS %d %d   (table is %d long)" % (i, off, cnt,
                                                            len(out.idx)))
if not bad:
    print("   none")
ids = [i for i in out.idx]
print("indices out of VT range: %d" % sum(1 for i in ids if not 0 <= i < len(out.vt)))
print("total triangles declared: %d   (before join: %d)"
      % (sum(lf.count for lf in out.leaves) // 3, polys_before))
