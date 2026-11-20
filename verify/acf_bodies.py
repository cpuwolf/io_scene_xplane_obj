"""The aero bodies come in as meshes, and they are the shape the sim measures.

    <blender> -b --factory-startup --python verify/acf_bodies.py

Plane Maker's Bodies screen is stored as a 20 x 18 point grid in the .acf
(`_body/<n>/_geo_xyz/<i>,<j>,<k>`, feet, x right / y up / z aft), of which only
the first `_s_dim` stations and `_r_dim` ring points are live.  X-Plane's own
Cycle Dump.txt prints the areas it computes from that grid, so the check is:

  1. the reader finds the bodies and the live sizes
  2. the mesh it builds has the same surface area as the grid the sim measures
     (rotation-invariant, so this also proves the placement / axis mapping did
     not stretch anything) - Lancair Body 1 must come out at 23.9591 m2
  3. the axis mapping is the OBJ8 importer's: length along Blender Y, up along Z,
     in metres
  4. a body with a pitch offset is actually pitched: Baron Body 1 has 2 degrees
     nose up, so its front tip must sit higher than its tail
  5. a real .acf import puts them in a `Bodies` collection under the aircraft,
     `Place by .acf` does not move them, and XPlane2Blender does not export them
"""
import math
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
XB = __import__(NAME + ".acfbodies", fromlist=["acfbodies"])

fails = []


def check(label, ok, detail=""):
    print("%-56s %s  %s" % (label, "OK  " if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


XP = os.path.expanduser("~/X-Plane 12/Aircraft")


def acf(*parts):
    for root, _dirs, files in os.walk(XP):
        for name in files:
            if name.lower().endswith(".acf"):
                path = os.path.join(root, name)
                if all(p in path for p in parts):
                    return path
    raise SystemExit("no .acf matching %s" % (parts,))


LANCAIR = acf("N844X.acf")
C172 = acf("Cessna_172SP.acf")
BARON = acf("Baron_58.acf")
AEROLITE = acf("Aerolite_103.acf")
ASK21 = acf("ASK 21")

# 1) the reader ----------------------------------------------------------------
bodies = XB._read(LANCAIR)
check("Lancair: 3 bodies", len(bodies) == 3,
      [(b["slot"], b["descrip"], b["s"], b["r"]) for b in bodies])
check("Lancair Body 1 is 11 stations x 10 ring points",
      (bodies[0]["s"], bodies[0]["r"]) == (11, 10),
      (bodies[0]["s"], bodies[0]["r"]))
check("Lancair Body 1 carries its drag coefficient",
      abs(bodies[0]["cd"] - 0.075) < 1e-6, bodies[0]["cd"])
c172 = XB._read(C172)
check("C172: 5 bodies (fuselage, spinner, 3 fairings)", len(c172) == 5,
      [(b["slot"], b["s"], b["r"]) for b in c172])
check("C172 fuselage is 17 stations x 18 ring points",
      (c172[0]["s"], c172[0]["r"]) == (17, 18), (c172[0]["s"], c172[0]["r"]))


# 2/3) geometry ---------------------------------------------------------------
def surface(me):
    total = 0.0
    for poly in me.polygons:
        vs = [me.vertices[i].co for i in poly.vertices]
        for k in range(1, len(vs) - 1):
            a, b, c = vs[0], vs[k], vs[k + 1]
            u = b - a
            v = c - a
            total += 0.5 * math.sqrt((u.y * v.z - u.z * v.y) ** 2
                                     + (u.z * v.x - u.x * v.z) ** 2
                                     + (u.x * v.y - u.y * v.x) ** 2)
    return total


def bbox(me):
    lo = [1e9] * 3
    hi = [-1e9] * 3
    for v in me.vertices:
        for i in range(3):
            lo[i] = min(lo[i], v.co[i])
            hi[i] = max(hi[i], v.co[i])
    return lo, hi


SIM_WETTED = {0: 23.9581, 1: 3.7115, 21: 1.3520}
for body in bodies:
    name, me = XB._mesh(body, None)
    area = surface(me)
    want = SIM_WETTED[body["slot"]]
    check("Body %d surface = the sim's wetted area" % (body["slot"] + 1),
          abs(area - want) < 0.01, "%.4f vs %.4f m2" % (area, want))

name, me = XB._mesh(bodies[0], None)
lo, hi = bbox(me)
span = [hi[i] - lo[i] for i in range(3)]
pts = [p for ring in bodies[0]["points"] for p in ring]
grid_z = max(p[2] for p in pts) - min(p[2] for p in pts)
grid_y = max(p[1] for p in pts) - min(p[1] for p in pts)
check("length runs along Blender Y, in metres",
      abs(span[1] - grid_z * XB.FEET) < 1e-3 and span[1] > span[0],
      "Y %.3f m (grid %.3f ft), X %.3f m" % (span[1], grid_z, span[0]))
check("up runs along Blender Z",
      abs(span[2] - grid_y * XB.FEET) < 1e-3,
      "Z %.3f m (grid %.3f ft)" % (span[2], grid_y))

# 4) a pitched body is pitched -------------------------------------------------
baron = XB._read(BARON)
body = [b for b in baron if b["slot"] == 0][0]
check("Baron Body 1 has a 2 degree pitch offset in the file",
      abs(body["rot"][0] - 2.0) < 1e-6, body["rot"])
name, me = XB._mesh(body, None)
tail = min(me.vertices, key=lambda v: v.co.y).co        # tail is -Y (nose is +Y)
flat = dict(body)
flat["rot"] = (0.0, 0.0, 0.0)
_name, flat_me = XB._mesh(flat, None)
flat_tail = min(flat_me.vertices, key=lambda v: v.co.y).co
check("...and the mesh comes out nose up (the tail drops)",
      tail.z < flat_tail.z,
      "tail z %.4f (pitched) vs %.4f (flat)" % (tail.z, flat_tail.z))

aerolite = XB._read(AEROLITE)
body = [b for b in aerolite if b["slot"] == 2][0]
check("Aerolite Body 3 has a 90 degree heading offset",
      abs(body["rot"][1] - 90.0) < 1e-6, body["rot"])
name, me = XB._mesh(body, None)
lo, hi = bbox(me)
flat = dict(body)
flat["rot"] = (0.0, 0.0, 0.0)
_name, flat_me = XB._mesh(flat, None)
flo, fhi = bbox(flat_me)
check("...and the mesh is turned with it (X takes the old Y extent)",
      abs((hi[0] - lo[0]) - (fhi[1] - flo[1])) < 1e-4
      and abs((hi[1] - lo[1]) - (fhi[0] - flo[0])) < 1e-4,
      "X %.3f/Y %.3f vs flat Y %.3f/X %.3f"
      % (hi[0] - lo[0], hi[1] - lo[1], fhi[1] - flo[1], fhi[0] - flo[0]))

# 5) a real import -------------------------------------------------------------
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)
bpy.ops.import_scene.xplane_acf(filepath=ASK21)
aircraft = bpy.data.collections[os.path.basename(ASK21)]
groups = [c for c in aircraft.children if c.name.startswith(XB.GROUP)]
check("the import builds one 'Bodies' collection", len(groups) == 1,
      [c.name for c in aircraft.children])
if groups:
    group = groups[0]
    objs = list(group.objects)
    check("it holds the ASK 21's 2 bodies (fuselage + wheel fairing)",
          len(objs) == 2, sorted(o.name for o in objs))
    check("they are tagged as aero bodies",
          all(o.get(XB.PROP) for o in objs))
    check("the collection is not an exportable XP2B layer",
          not group.xplane.is_exportable_collection)
    roots = addon._collection_roots(aircraft)
    check("Place by .acf does not move them",
          not [o for o in roots if o.get(XB.PROP)],
          "%d placement root(s)" % len(roots))
    from io_xplane2blender.xplane_types import xplane_file as XF
    files = [f.filename for f in
             XF.createFilesFromBlenderRootObjects(bpy.context.scene,
                                                  bpy.context.view_layer, False)]
    check("XP2B still writes 20 files, none of them a body",
          len(files) == 20 and not [f for f in files if f.startswith("Body")],
          len(files))

print()
if fails:
    print("RESULT: FAIL")
    for name in fails:
        print("  - " + name)
    sys.exit(1)
print("RESULT: PASS")
