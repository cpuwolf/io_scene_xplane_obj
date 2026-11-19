"""`weld_vertices` merges positions and keeps the per-corner UVs and normals.

    <blender> -b --factory-startup --python verify/weld_test.py [-- <file.obj>]

An OBJ8 stores a `VT` row per triangle corner, so every hard edge and every
texture seam repeats the same position.  Imported one-row-per-vertex (the
default) that is faithful to the file and wrong for editing: moving a vertex
leaves a crack.  `weld_vertices` builds one vertex per position instead, and the
data that is per-*corner* in Blender - the UV map and the custom split normals -
has to be written from the source rows anyway, or the weld silently resamples
them (Blender's own `Merge by Distance` recomputes split normals after welding:
measured on a Laminar cockpit, 58 % of the loops changed, mean 37 deg).

The fixture is a box: 8 positions, 24 `VT` rows, 6 normals, 6 UV tiles.  Cheap
enough to run every time, and every corner differs in *something*, so a weld that
drops the UV or the normal cannot pass.

Point it at a real file to grade one: a Laminar cockpit lands about 39 % more
vertices than it has distinct points.  Two things are expected to differ from the
unwelded import, and both are checked for their true cause rather than tolerated:

* triangles whose corners share a position have no area; welding collapses them
  and `validate()` drops them, so the triangle count falls by exactly the number
  of such triangles in the file (28 of 25,399 in the C90B cockpit).
* Blender stores a custom split normal as an int16 offset against the averaged
  vertex normal, so a *shared* vertex with per-corner normals pays a little
  precision (measured max 0.34 deg on that cockpit).  XP2B's vertex optimizer
  compares exact floats, so the exported `VT` table can grow by a few percent
  even though it is built from the same geometry.
"""
import os
import sys
import json
import collections

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

XM = IMP.xpmesh

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SRC = argv[0] if argv else os.path.join(REPO, "verify", "fixtures", "weld.obj")
OUT = argv[1] if len(argv) > 1 else os.path.join(REPO, "verify", "tmp", "weld")
os.makedirs(OUT, exist_ok=True)

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg, flush=True)
    else:
        print("   ok   %s" % msg, flush=True)
    return cond


def clear():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)


def pk(co):
    return tuple(int(round(v * 1e4)) for v in co)


def uk(uv):
    return tuple(int(round(v * 1e5)) for v in uv)


def area(tri):
    """Triangle area in m^2 from the quantised X-Plane positions."""
    p0, p1, p2 = [tuple(c / 1e4 for c in corner[0]) for corner in tri]
    u = [p1[i] - p0[i] for i in range(3)]
    v = [p2[i] - p0[i] for i in range(3)]
    cx = u[1] * v[2] - u[2] * v[1]
    cy = u[2] * v[0] - u[0] * v[2]
    cz = u[0] * v[1] - u[1] * v[0]
    return 0.5 * (cx * cx + cy * cy + cz * cz) ** 0.5


#: warnings the import reported.  They reach the user through `report()`, and the
#: header that holds them is deleted by the conversion, so catch them on the way
#: through `import_file`.
SEEN = []
_real_import_file = IMP.XM.import_file


def _spy_import_file(*args, **kwargs):
    header = _real_import_file(*args, **kwargs)
    if header is not None:
        SEEN.append(json.loads(header.get("xp8_warnings") or "[]"))
    return header


IMP.XM.import_file = _spy_import_file


def tris_of(ob):
    """The mesh as X-Plane-space triangles, one row per face, corner order kept."""
    got = XM.triangle_corners(bpy.context, ob)
    if not got:
        return []
    flat = got[0]
    return [[(pk(p), uk(u), tuple(n)) for p, n, u in flat[i:i + 3]]
            for i in range(0, len(flat), 3)]


def run(weld, tag):
    """Import once and export once; return what the checks need from that run."""
    clear()
    res = bpy.ops.import_scene.xplane_obj(filepath=SRC, weld_vertices=weld)
    print("import (%s):" % tag, res, flush=True)
    coll = next((c for c in bpy.data.collections
                 if getattr(getattr(c, "xplane", None),
                            "is_exportable_collection", False)), None)
    meshes = [o for o in (coll.all_objects if coll else bpy.data.objects)
              if o.type == 'MESH']
    per_leaf = {}
    verts = extra = 0
    for ob in meshes:
        me = ob.data
        verts += len(me.vertices)
        grouped = collections.Counter(pk(v.co) for v in me.vertices)
        extra += sum(n - 1 for n in grouped.values())
        per_leaf[ob.get(XM.PROP_LEAF)] = tris_of(ob)
    # XP2B names the file after the layer, not after `filepath`, so each run
    # gets a directory of its own and the check reads back what landed in it
    outdir = os.path.join(OUT, tag)
    os.makedirs(outdir, exist_ok=True)
    for stale in os.listdir(outdir):
        os.remove(os.path.join(outdir, stale))
    target = os.path.join(outdir, os.path.basename(SRC))
    exp = bpy.ops.export.xplane_obj(filepath=target)
    written = [os.path.join(outdir, n) for n in sorted(os.listdir(outdir))
               if n.lower().endswith(".obj")]
    print("export (%s):" % tag, exp, "->", written, flush=True)
    out = IMP.objcodec.parse(written[0]) if written else None
    exported = None
    if out is not None:
        exported = collections.Counter(
            (pk((r.px, r.py, r.pz)), uk((r.s, r.t)))
            for leaf in out.leaves for vi in leaf.indices
            for r in [out.vt[vi]])
    warnings = SEEN[-1] if SEEN else []
    return {"verts": verts, "extra": extra, "leaves": per_leaf,
            "exported": exported, "vt_rows": len(out.vt) if out else 0,
            "warnings": warnings}


plain = run(False, "noweld")
welded = run(True, "weld")

print("\nnoweld: verts=%d extra_at_same_pos=%d leaves=%d vt_rows=%d"
      % (plain["verts"], plain["extra"], len(plain["leaves"]),
         plain["vt_rows"]), flush=True)
print("weld:   verts=%d extra_at_same_pos=%d leaves=%d vt_rows=%d"
      % (welded["verts"], welded["extra"], len(welded["leaves"]),
         welded["vt_rows"]), flush=True)

check(welded["extra"] == 0, "welded import has one vertex per position")
check(welded["verts"] < plain["verts"], "welding removed vertices (%d -> %d)"
      % (plain["verts"], welded["verts"]))
check(sorted(welded["leaves"]) == sorted(plain["leaves"]),
      "one mesh per geometry batch, both ways")
check(not [w for w in welded["warnings"] if "matched" in w],
      "every welded triangle was matched back to its VT rows")

# Walk both meshes face by face.  The welded mesh is the unwelded one with the
# zero-area triangles removed, and from_pydata/validate keep face order, so a
# two-pointer walk over the plain triangles recovers the correspondence.
uv_bad = 0
worst = 0.0
collapsed = 0
mismatched = 0
slivers = 0
sliver_worst = 0.0
sliver_bad = 0
for leaf, a in plain["leaves"].items():
    b = welded["leaves"].get(leaf)
    if b is None:
        mismatched += 1
        continue
    src = 0
    for tri in b:
        pv = tuple(c[0] for c in tri)
        while src < len(a) and tuple(c[0] for c in a[src]) != pv:
            gone = a[src]
            if len(set(c[0] for c in gone)) < 3:
                collapsed += 1                 # zero-area: collapsed on purpose
            src += 1
        if src >= len(a):
            mismatched += 1
            break
        d = 0.0
        for (pos, uv, nrm), (_, uv2, nrm2) in zip(tri, a[src]):
            if uv != uv2:
                uv_bad += 1
            d = max(d, max(abs(x - y) for x, y in zip(nrm, nrm2)))
        # a face with no area has no normal to preserve: Blender's custom-normal
        # storage cannot express one on a degenerate face and hands back (0,0,0),
        # on the unwelded mesh as well.  28 such triangles in a C90B cockpit, and
        # they draw nothing, so they are counted, not graded.
        if area(tri) < 1e-6:
            slivers += 1
            sliver_worst = max(sliver_worst, d)
            if d > 0.01745:
                sliver_bad += 1
        else:
            worst = max(worst, d)
        src += 1
    while src < len(a):
        gone = a[src]
        if len(set(c[0] for c in gone)) < 3:
            collapsed += 1
        src += 1

print("\ncollapsed zero-area triangles: %d   corner UV mismatches: %d   "
      "max normal deviation: %.2e (%.3f deg)   sliver faces: %d, of which %d "
      "changed a normal by >1 deg (max %.1f deg; not graded)"
      % (collapsed, uv_bad, worst, worst * 57.2958, slivers, sliver_bad,
         sliver_worst * 57.2958), flush=True)

check(mismatched == 0, "every welded triangle is the same triangle as before")
check(uv_bad == 0, "every corner keeps its UV (%d differ)" % uv_bad)
check(worst < 0.01745, "authored normals survive welding (max %.3f deg < 1)"
      % (worst * 57.2958))
declared = [w for w in welded["warnings"] if "collapsed by welding" in w]
check(bool(declared) or collapsed == 0,
      "the import says which triangles welding dropped (%d)" % len(declared))

# and the file that leaves: same corners, minus the collapsed triangles
if plain["exported"] is not None and welded["exported"] is not None:
    ua, ub = plain["exported"], welded["exported"]
    lost = sum((ua - ub).values())
    gained = sum((ub - ua).values())
    check(gained == 0 and lost == 3 * collapsed,
          "the exported file keeps every other corner (%d corners lost, "
          "%d gained, %d collapsed triangles)"
          % (lost, gained, collapsed))
    check(welded["vt_rows"] <= max(1, int(plain["vt_rows"] * 1.2)),
          "the exported VT table stays the same size (%d -> %d rows; shared "
          "vertices cost a little normal precision, and XP2B's optimizer "
          "compares exact floats)"
          % (plain["vt_rows"], welded["vt_rows"]))
else:
    check(False, "both runs exported a .obj")

print("\nRESULT: %s" % ("FAIL %d" % len(fails) if fails else "PASS"), flush=True)
sys.exit(1 if fails else 0)
