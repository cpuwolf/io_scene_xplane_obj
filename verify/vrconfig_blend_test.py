"""The .acf import also brings the aircraft's vrconfig in - and it lands in place.

    <blender> -b --factory-startup --python verify/vrconfig_blend_test.py -- <file.acf>

`vrconfig.txt` is aircraft-scoped: hotspots are metres in aircraft coordinates,
so they can only be placed by an import that knows the aircraft frame.  This
grades that import end to end:

1. one collection per vrconfig, with one camera and one AABB box per hotspot;
2. every camera/box position matches a fresh read of the file (the frame chain -
   `.acf` CG in feet, `REF_POINT_ACF`, metres, X-Plane axes -> Blender axes);
3. every `BEGIN_MANIP` block resolves to the geometry it overrides, and the ones
   that do not are reported (the CitationX ships three such blocks);
4. **the alignment proof**: a ray cast out of each seat's camera lands on cockpit
   geometry.  If the origin or the axis mapping were wrong the seat would be
   metres away from the panel and the ray would fly out into the sky - so this is
   the check that the numbers are right, not merely self-consistent.

Import with "Place by .acf" on: without it every .obj sits at its own file
origin and the hotspots (which are in the aircraft frame) legitimately do not
overlap the cockpit.
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

VR = IMP.xpvr
VC = IMP.vrconfig if hasattr(IMP, "vrconfig") else None
if VC is None:                                              # pragma: no cover
    import vrconfig as VC

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ACF = argv[0] if argv else os.path.join(
    os.path.expanduser("~/X-Plane 12/Aircraft"),
    "King Air C90B EVO G1000 V1.3.1", "C90B.acf")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg, flush=True)
    else:
        print("   ok   %s" % msg, flush=True)
    return cond


for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)

print("importing %s with placement on" % ACF, flush=True)
res = bpy.ops.import_scene.xplane_acf(filepath=ACF, place_by_acf=True)
check(res == {'FINISHED'}, "the .acf import finished: %r" % (res,))

# --- 1. the collection it built -------------------------------------------
path = VC.find(ACF)
check(path is not None, "the aircraft has a vrconfig")
vr = VC.parse(path)
cg = IMP.acfcodec.read_cg(ACF)
origin = vr.origin(cg)
print("   vrconfig %s: %d hotspot(s), %d manipulator(s), origin %s"
      % (os.path.basename(path), len(vr.hotspots), len(vr.manips), origin["note"]),
      flush=True)

coll = bpy.data.collections.get(os.path.basename(path))
check(coll is not None, "a collection per vrconfig: %r" % os.path.basename(path))
if coll is None:
    print("\nRESULT: FAIL no vrconfig collection", flush=True)
    raise SystemExit(1)

cams = [o for o in coll.objects if o.type == 'CAMERA']
boxes = [o for o in coll.objects if o.type == 'MESH']
check(len(cams) == len(vr.hotspots), "one camera per hotspot (%d)" % len(cams))
check(len(boxes) == sum(1 for h in vr.hotspots if h.aabb),
      "one box per AABB (%d)" % len(boxes))
check(bool(coll.get(VR.PROP_DUMP)), "the file's own text is stored on the collection")
check(bool(coll.get(VR.PROP_READONLY)), "the collection says it is read-only")
check(coll.color_tag == VR.BLUE_TAG,
      "the collection carries the blue outliner tag (%s)" % coll.color_tag)
check(all(all(abs(a - b) < 1e-6 for a, b in zip(tuple(ob.color), VR.BLUE))
          for ob in cams + boxes),
      "every camera and box is the same blue")

# nothing in here may reach the exporter
exportable = [c for c in bpy.data.collections if c.name != coll.name
              and getattr(getattr(c, "xplane", None), "is_exportable_collection", False)]
inside = [c.name for c in exportable if coll.name in [p.name for p in c.children]]
check(not inside, "the VR collection is not inside an exportable root (%r)" % inside)

# --- 2. positions, recomputed from the file -------------------------------
worst = 0.0
for hotspot in vr.hotspots:
    cam = bpy.data.objects.get(hotspot.name)
    if not check(cam is not None and cam.type == 'CAMERA',
                 "camera for %r" % hotspot.name):
        continue
    want = VC.to_bl(vr.hotspot_xyz(hotspot, origin))
    got = tuple(cam.matrix_world.translation)
    worst = max(worst, max(abs(a - b) for a, b in zip(want, got)))
    tag = cam.get(VR.PROP_HOTSPOT)
    if not check(tag == hotspot.name, "  %r carries its own name" % hotspot.name):
        continue
    if hotspot.aabb is not None:
        box = bpy.data.objects.get("%s [AABB]" % hotspot.name)
        if check(box is not None, "  %r has its AABB box" % hotspot.name):
            want_c = VC.to_bl(VR._with_origin(hotspot.centre, origin))
            got_c = tuple(box.matrix_world.translation)
            size = tuple(box.dimensions)
            worst = max(worst, max(abs(a - b) for a, b in zip(want_c, got_c)))
            check(all(abs(a - b) < 1e-3 for a, b in zip(size, hotspot.size)),
                  "  %r box is %r (wanted %r)"
                  % (hotspot.name, tuple(round(v, 3) for v in size),
                     tuple(round(v, 3) for v in hotspot.size)))
check(worst < 1e-4, "every hotspot sits where the file says (max %.2e m)" % worst)

# --- 3. the manipulator lookup -------------------------------------------
manips = json.loads(coll.get(VR.PROP_MANIPS) or "[]")
resolved = [m for m in manips if m["objects"]]
missing = [m for m in manips if not m["objects"]]
print("   manipulators: %d resolved, %d with no geometry"
      % (len(resolved), len(missing)), flush=True)
check(len(manips) == len(vr.manips), "every block is in the table (%d)" % len(manips))
check(bool(resolved), "at least one manipulator points at a mesh")
for m in resolved[:3]:
    print("      %-16s %-40s -> %s"
          % (m["type"], (m["a"] or "")[:40], m["objects"][:2]), flush=True)
for m in missing[:3]:
    print("      MISSING %-16s %s" % (m["type"], m["a"]), flush=True)

# --- 4. the alignment proof ----------------------------------------------
# the boxes are geometry too; take them out of the ray's way
coll.hide_viewport = True
depsgraph = bpy.context.evaluated_depsgraph_get()
from mathutils import Vector


def march(cam, steps=8):
    """Every hit along the camera's view, closest first."""
    origin = cam.matrix_world.translation.copy()
    forward = -(cam.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
    trail = []
    start = origin.copy()
    for _ in range(steps):
        hit, where, _normal, _index, ob, _matrix = bpy.context.scene.ray_cast(
            depsgraph, start, forward)
        if not hit:
            break
        trail.append((ob.name, (where - origin).length))
        start = where + forward * 0.01
    return trail


seats = []
for hotspot in vr.hotspots:
    cam = bpy.data.objects.get(hotspot.name)
    if cam is None:
        continue
    trail = march(cam)
    print("   %-18s %s" % (hotspot.name,
                           ", ".join("%s@%.2fm" % h for h in trail) or "nothing"),
          flush=True)
    # The crew figures are modelled around the eye point, so a seat's first hit
    # is the pilot's own head 2 cm away - skip them and ask what is behind.
    beyond = [h for h in trail if "pilot" not in h[0].lower()]
    seats.append((hotspot, trail, beyond))
coll.hide_viewport = False

check(all(beyond for _h, _t, beyond in seats),
      "every hotspot's view reaches cockpit geometry past the crew figure")
for hotspot, _trail, beyond in seats:
    if beyond:
        check(beyond[0][1] < 3.0,
              "%s faces geometry at %.2f m (%s)"
              % (hotspot.name, beyond[0][1], beyond[0][0]))
named = [s for s in seats if s[0].name in ("Pilot's Seat", "CoPilot's Seat",
                                           "Copilot's Seat")]
for hotspot, trail, beyond in named:
    check(bool(beyond) and beyond[0][1] < 1.5,
          "%s sees the panel within 1.5 m%s"
          % (hotspot.name,
             " (%s at %.2f m)" % beyond[0] if beyond else ""))
print("   %d of %d hotspot(s) look at something" % (sum(1 for s in seats if s[1]),
                                                    len(seats)), flush=True)

print("\nRESULT: %s" % ("FAIL %d" % len(fails) if fails else "PASS"), flush=True)
sys.exit(1 if fails else 0)
