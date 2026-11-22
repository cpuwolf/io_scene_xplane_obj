"""The aero airframe comes in as meshes: Plane Maker's bodies and wings.

    <blender> -b --factory-startup --python verify/acf_aero.py

Both are point grids in the .acf (`_body/<n>/_geo_xyz/<i>,<j>,<k>` and
`_wing/<n>/...`, feet, x right / y up / z aft) of which only the first `_s_dim`
sections and `_r_dim` section points are live - the rest of the 20 x 18 array is
stale.  What has to hold:

  1. the readers find the parts, with the live sizes
  2. the body meshes have the surface area X-Plane's own Cycle Dump.txt prints
     for them (so the axis mapping, the welding and the body placement are right)
  3. a wing's grid is already the aircraft frame: no placement applied, dihedral
     and sweep baked in, left and right in separate slots
  4. the wing planform adds up to what the dump reports for the joined wing
  5. a vertical stabiliser comes out vertical, and the two sides mirror
  6. a real .acf import puts everything in an `Aero` collection that is not an
     exportable layer, `Place by .acf` does not move it, and XPlane2Blender
     never writes it
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
XB = __import__(NAME + ".acfaero", fromlist=["acfaero"])

FEET = XB.FEET
fails = []


def check(label, ok, detail=""):
    print("%-58s %s  %s" % (label, "OK  " if ok else "FAIL", detail))
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


def mesh_of(part):
    return XB._mesh(part["name"], part["points"], part["r"], part["s"], None)


# --- bodies ------------------------------------------------------------------
bodies = XB.bodies(LANCAIR)
check("Lancair: 3 bodies", len(bodies) == 3,
      [(b["slot"], b["name"], b["s"], b["r"]) for b in bodies])
check("Lancair Body 1 is 11 stations x 10 ring points",
      (bodies[0]["s"], bodies[0]["r"]) == (11, 10),
      (bodies[0]["s"], bodies[0]["r"]))
check("Lancair Body 1 keeps its drag coefficient",
      abs(bodies[0]["cd"] - 0.075) < 1e-6, bodies[0]["cd"])
c172 = XB.bodies(C172)
check("C172: 5 bodies (fuselage, spinner, 3 fairings)", len(c172) == 5,
      [(b["name"], b["s"], b["r"]) for b in c172])
check("C172 fuselage is 17 stations x 18 ring points",
      (c172[0]["s"], c172[0]["r"]) == (17, 18), (c172[0]["s"], c172[0]["r"]))

SIM_WETTED = {0: 23.9581, 1: 3.7115, 21: 1.3520}
for body in bodies:
    area = surface(mesh_of(body))
    want = SIM_WETTED[body["slot"]]
    check("Body %d surface = the sim's wetted area" % (body["slot"] + 1),
          abs(area - want) < 0.01, "%.4f vs %.4f m2" % (area, want))

me = mesh_of(bodies[0])
lo, hi = bbox(me)
span = [hi[i] - lo[i] for i in range(3)]
pts = [p for ring in bodies[0]["points"] for p in ring]
grid_z = max(p[2] for p in pts) - min(p[2] for p in pts)
grid_y = max(p[1] for p in pts) - min(p[1] for p in pts)
check("body length runs along Blender Y, in metres",
      abs(span[1] - grid_z * FEET) < 1e-3 and span[1] > span[0],
      "Y %.3f m (grid %.3f ft)" % (span[1], grid_z))
check("body height runs along Blender Z", abs(span[2] - grid_y * FEET) < 1e-3,
      "Z %.3f m (grid %.3f ft)" % (span[2], grid_y))

# the body placement: the Baron's fuselage is stored 2 degrees nose down and the
# import puts it where the sim draws it, so its tail ends up lower
baron = [b for b in XB.bodies(BARON) if b["slot"] == 0][0]
raw = XB._grids(XB._read_text(BARON), "_body")[0]
flat = dict(baron)
flat["points"] = raw["points"]
tail = min(mesh_of(baron).vertices, key=lambda v: v.co.y).co
tail_flat = min(mesh_of(flat).vertices, key=lambda v: v.co.y).co
check("Baron Body 1 is pitched 2 deg nose up (its tail drops)",
      tail.z < tail_flat.z,
      "tail z %.4f placed vs %.4f raw" % (tail.z, tail_flat.z))

aerolite = [b for b in XB.bodies(AEROLITE) if b["slot"] == 2][0]
alo, ahi = bbox(mesh_of(aerolite))
aflat = dict(aerolite)
aflat["points"] = XB._grids(XB._read_text(AEROLITE), "_body")[2]["points"]
flo, fhi = bbox(mesh_of(aflat))
check("Aerolite Body 3 is turned 90 deg (X takes the old Y extent)",
      abs((ahi[0] - alo[0]) - (fhi[1] - flo[1])) < 1e-4
      and abs((ahi[1] - alo[1]) - (fhi[0] - flo[0])) < 1e-4,
      "X %.3f/Y %.3f vs raw Y %.3f/X %.3f"
      % (ahi[0] - alo[0], ahi[1] - alo[1], fhi[1] - flo[1], fhi[0] - flo[0]))

# --- wings -------------------------------------------------------------------
wings = XB.wings(C172)
check("C172: 9 wing slots", len(wings) == 9,
      [(w["slot"], w["name"]) for w in wings])
check("...named the way Plane Maker names them",
      [w["name"] for w in wings] == ["Wing 1 L", "Wing 1 R", "Wing 2 L",
                                     "Wing 2 R", "Wing 3 L", "Wing 3 R",
                                     "H-Stab 1 L", "H-Stab 1 R", "V-Stab 1"],
      [w["name"] for w in wings])
wing1 = [w for w in wings if w["name"] == "Wing 1 R"][0]
check("wing 1 is 20 sections x 14 section points",
      (wing1["s"], wing1["r"]) == (20, 14), (wing1["s"], wing1["r"]))
wlo, whi = bbox(mesh_of(wing1))
check("its chord comes out at 5.20 ft (1.58 m)",
      abs((whi[1] - wlo[1]) - 5.2 * FEET) < 0.02,
      "chord %.3f m" % (whi[1] - wlo[1]))
check("it spans Blender X (lateral is X, not Y)",
      (whi[0] - wlo[0]) > (whi[1] - wlo[1]),
      "X %.2f m vs Y %.2f m" % (whi[0] - wlo[0], whi[1] - wlo[1]))
check("the grid is already the aircraft frame (no part offset added)",
      abs(whi[0] - 8.598 * FEET) < 0.01,
      "wing 1 R tip at X %.3f m (8.598 ft = %.3f m)"
      % (whi[0], 8.598 * FEET))
widest = max(bbox(mesh_of(w))[1][0] for w in wings if w["slot"] in (0, 1, 2, 3))
check("...which puts the wing tip at the real half-span (18.09 ft)",
      abs(widest - 18.09 * FEET) < 0.05, "%.3f m" % widest)

# wing planform against the sim's joined-wing area (Lancair: 6.46 m2)
lanc_wing = XB.wings(LANCAIR)
area = 0.0
for w in lanc_wing:
    if w["slot"] in (1, 3, 5, 7):
        m = mesh_of(w)
        for poly in m.polygons:
            vs = [m.vertices[i].co for i in poly.vertices]
            for k in range(1, len(vs) - 1):
                a, b, c = vs[0], vs[k], vs[k + 1]
                area += 0.5 * abs((b.x - a.x) * (c.y - a.y)
                                  - (c.x - a.x) * (b.y - a.y))
# the surface is closed (upper and lower skin), so half the projected sum is the
# planform - the same rule the sim uses for side/top area
area = area / 2.0
check("Lancair right wing planform ~= the sim's 6.46 m2",
      abs(area - 6.46) < 0.1, "%.3f m2" % area)

vstab = [w for w in wings if w["name"].startswith("V-Stab")][0]
slo, shi = bbox(mesh_of(vstab))
check("the vertical stabiliser comes out vertical",
      (shi[2] - slo[2]) > 3 * (shi[0] - slo[0]),
      "Z %.2f m, X %.2f m" % (shi[2] - slo[2], shi[0] - slo[0]))
left = [w for w in lanc_wing if w["slot"] == 0][0]
right = [w for w in lanc_wing if w["slot"] == 1][0]
llo, lhi = bbox(mesh_of(left))
rlo, rhi = bbox(mesh_of(right))
check("left and right are separate slots, mirrored about x=0",
      abs(abs(llo[0]) - rhi[0]) < 1e-3 and abs(lhi[0]) < 0.15,
      "L %.3f..%.3f  R %.3f..%.3f" % (llo[0], lhi[0], rlo[0], rhi[0]))

# --- fairings bolted to a gear leg -------------------------------------------
# Plane Maker does not rewrite `_part_*` when the gear changes, so a fairing's
# stored position can be far from the wheel it belongs to.  The gear defines the
# wheel: attach + leg * (sin latE, -cos latE cos lonE, -cos latE sin lonE).
SR22 = acf("Cirrus SR22.acf")
R22 = acf("Robinson R22 Beta II.acf")
wheels = XB.gear_wheels(XB._read_text(SR22))
check("the Cirrus' main legs put their wheels at x +-4.52 ft",
      abs(wheels[1][0] + 4.518) < 0.01 and abs(wheels[2][0] - 4.518) < 0.01,
      ["%.3f %.3f %.3f" % wheels[i] for i in (1, 2)])
sr22 = XB.bodies(SR22)
fixed = {b["name"]: b["gear_fix"] for b in sr22 if b.get("gear_fix")}
check("its two main fairings are re-seated on their wheels",
      len(fixed) == 2 and sorted(f["gear"] for f in fixed.values()) == [1, 2],
      sorted(fixed))
check("...from a stored position 1.44 ft too far outboard",
      all(abs(f["stored"][0] - f["wheel"][0]) > 1.4 for f in fixed.values()),
      ["%.2f -> %.2f" % (f["stored"][0], f["wheel"][0]) for f in fixed.values()])
check("...while the nose fairing keeps the position the file gives it",
      not [b for b in sr22 if b["slot"] == 29 and b.get("gear_fix")])
check("the Cessna's three fairings agree with their wheels and are left alone",
      not [b for b in XB.bodies(C172) if b.get("gear_fix")])
r22 = XB.bodies(R22)
check("the R22's skid fairings are stored at 0,0,0 and take the gear's wheel",
      all(b.get("gear_fix") for b in r22 if b["slot"] in (29, 30))
      and abs(r22[[b["slot"] for b in r22].index(29)]["gear_fix"]["wheel"][0]
              + 2.85) < 0.01,
      [(b["slot"], tuple(round(v, 2) for v in b["gear_fix"]["wheel"]))
       for b in r22 if b.get("gear_fix")])

# the leg the fairing hangs off: attach point -> wheel, which is what Plane Maker
# calls the strut ("lat arm / long arm / vert arm: attach point of the strut")
legs = XB.gear_legs(C172)
check("the Cessna's three legs are 2.20 ft (nose) and 3.00 ft (mains)",
      len(legs) == 3 and abs(legs[0]["leg"] - 2.2) < 1e-3
      and abs(legs[1]["leg"] - 3.0) < 1e-3,
      {k: round(v["leg"], 2) for k, v in legs.items()})
check("...ending on the tyres the model has",
      abs(legs[1]["wheel"][0] + 4.116) < 0.01
      and abs(legs[1]["wheel"][1] + 3.478) < 0.01,
      tuple(round(v, 3) for v in legs[1]["wheel"]))
check("...with the tyre the gear declares: radius, semi-width, pressure",
      abs(legs[1]["tire"] - 0.65) < 1e-6 and abs(legs[1]["width"] - 0.23) < 1e-6
      and abs(legs[1]["psi"] - 41.7) < 0.1,
      "r %.3f ft, semi-width %.3f ft (%.1f in wide), %.1f psi"
      % (legs[1]["tire"], legs[1]["width"], legs[1]["width"] * 24.0, legs[1]["psi"]))

# --- a real import -----------------------------------------------------------
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)
bpy.ops.import_scene.xplane_acf(filepath=ASK21)
aircraft = bpy.data.collections[os.path.basename(ASK21)]
groups = [c for c in aircraft.children if c.name.startswith(XB.GROUP)]
check("the import builds one 'Aero' collection", len(groups) == 1,
      [c.name for c in aircraft.children])
if groups:
    group = groups[0]
    objs = list(group.objects)
    want = len(XB.bodies(ASK21)) + len(XB.wings(ASK21))
    check("it holds every body and wing", len(objs) == want,
          "%d object(s), expected %d: %s"
          % (len(objs), want, sorted(o.name for o in objs)))
    check("each one is tagged with its kind",
          all(o.get(XB.PROP) and o.get(XB.PROP_KIND) in ("body", "wing")
              for o in objs))
    check("the collection is not an exportable XP2B layer",
          not group.xplane.is_exportable_collection)
    legs_ask = XB.gear_legs(ASK21)
    gear_colls = [c for c in group.children if c.name == XB.GEAR_GROUP]
    check("the legs come in a '%s' child collection" % XB.GEAR_GROUP,
          len(gear_colls) == (1 if legs_ask else 0),
          [(c.name, len(c.objects)) for c in gear_colls])
    if gear_colls:
        struts = [o for o in gear_colls[0].objects if "#gear" in str(o[XB.PROP])]
        off = []
        for ob in struts:
            slot = int(str(ob[XB.PROP]).rsplit("gear", 1)[1])
            leg = legs_ask[slot]
            mid = [(leg["attach"][i] + leg["wheel"][i]) / 2.0 for i in range(3)]
            vs = [ob.matrix_world @ v.co for v in ob.data.vertices]
            c = (sum(v.x for v in vs) / len(vs) / FEET,
                 sum(v.z for v in vs) / len(vs) / FEET,
                 -sum(v.y for v in vs) / len(vs) / FEET)
            off.append(math.dist(c, mid))
        check("one strut per leg, each running attach point -> wheel",
              len(struts) == len(legs_ask) and max(off) < 0.05,
              "%.3f ft worst of %d" % (max(off), len(struts)))
        check("...and XP2B never writes them",
              all(o.get(XB.PROP_KIND) == "gear" for o in struts),
              [o.name for o in struts])
        tires = [o for o in gear_colls[0].objects
                 if str(o[XB.PROP]).rsplit("#", 1)[-1].startswith("tire")]
        bad = []
        for t in tires:
            slot = int(str(t[XB.PROP]).rsplit("tire", 1)[1])
            leg = legs_ask[slot]
            lo_t, hi_t = bbox(t.data)
            if (abs(t[XB.PROP_TIRE_RADIUS] - leg["tire"]) > 1e-6
                    or abs(t[XB.PROP_TIRE_WIDTH] - 2.0 * leg["width"]) > 1e-6
                    or abs((hi_t[2] - lo_t[2]) - 2.0 * leg["tire"] * FEET) > 0.02
                    or abs((hi_t[0] - lo_t[0]) - 2.0 * leg["width"] * FEET) > 0.02):
                bad.append(t.name)
        check("one tyre per leg, as wide as 2 x the semi-width the file gives",
              len(tires) == len(legs_ask) and not bad,
              "%d tyre(s), bad %s" % (len(tires), bad))
    note = group.get(XB.PROP_SOURCE) or ""
    check("the collection says a wing grid is the stored shape, not the flown one",
          "not this mesh" in note and "_Ctip" in note,
          repr(note[:60]))
    roots = addon._collection_roots(aircraft)
    check("Place by .acf does not move them",
          not [o for o in roots if o.get(XB.PROP)],
          "%d placement root(s)" % len(roots))
    from io_xplane2blender.xplane_types import xplane_file as XF
    files = [f.filename for f in
             XF.createFilesFromBlenderRootObjects(bpy.context.scene,
                                                  bpy.context.view_layer, False)]
    check("XP2B still writes 20 files, none of them aero geometry",
          len(files) == 20
          and not [f for f in files if f.startswith(("Body", "Wing", "H-Stab",
                                                     "V-Stab"))],
          len(files))

print()
if fails:
    print("RESULT: FAIL")
    for name in fails:
        print("  - " + name)
    sys.exit(1)
print("RESULT: PASS")
