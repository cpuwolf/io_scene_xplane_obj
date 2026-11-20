"""Moving a hotspot in Blender writes back only the lines that changed.

    <blender> -b --factory-startup --python verify/vrconfig_save_test.py -- <file.acf>

The vrconfig is hand-edited: author `####` banners, block order, decimal places
and comma-separated coordinates are all content, and an aircraft's own updater
may rewrite the file behind Blender's back.  So the writer must

1. write **nothing** when nothing moved (the import alone must not touch the file),
2. move one hotspot and rewrite exactly that one directive, in the author's own
   style - not the block, not the file,
3. be idempotent (a second save finds nothing to do),
4. refuse to write when the file changed on disk since the import.

Everything happens on a copy of the aircraft's vrconfig; the real file is never
opened for writing.
"""
import json
import os
import shutil
import sys
import tempfile

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

XVR = IMP.xpvr
VC = getattr(IMP, "vrconfig", None)
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

print("importing %s" % ACF, flush=True)
res = bpy.ops.import_scene.xplane_acf(filepath=ACF, place_by_acf=True)
check(res == {'FINISHED'}, "the .acf import finished: %r" % (res,))

source = VC.find(ACF)
coll = next((c for c in bpy.data.collections if c.get(XVR.PROP_SOURCE)), None)
if not check(coll is not None, "an aircraft with a vrconfig was imported"):
    print("\nRESULT: FAIL nothing to save", flush=True)
    raise SystemExit(1)

# --- work on a copy, never on the aircraft's own file ----------------------
work = tempfile.mkdtemp(prefix="vrconfig_")
copy = os.path.join(work, os.path.basename(source))
shutil.copyfile(source, copy)
original = open(copy, "rb").read()
coll[XVR.PROP_SOURCE] = copy
coll[XVR.PROP_HASH] = XVR._digest(original)
print("   working on a copy: %s" % copy, flush=True)

# --- 1. an untouched scene writes nothing ---------------------------------
check(XVR.changes(coll) == [], "nothing is reported as moved right after import")
info = XVR.save(bpy.context, coll)
check(info["written"] == 0, "saving an untouched scene writes nothing (%d)" % info["written"])
check(open(copy, "rb").read() == original, "and the file is byte identical")

# --- 2. move one hotspot ---------------------------------------------------
stored = json.loads(coll.get(XVR.PROP_HOTSPOTS))
entry = next(e for e in stored if e.get("camera"))
cam = bpy.data.objects[entry["camera"]]
box = bpy.data.objects.get(entry.get("box") or "")
from mathutils import Vector
shift = Vector((0.05, -0.03, 0.02))
cam.matrix_world = cam.matrix_world.copy()
cam.location = cam.matrix_world.translation + shift
if box is not None:
    box.scale = (1.2, 1.5, 1.2)

bpy.context.view_layer.update()      # matrix_world / dimensions follow the edit

moved = XVR.changes(coll)
check(len(moved) == 1, "exactly one hotspot reports a change (%d)" % len(moved))
if moved:
    name, diff = moved[0]
    check(name == entry["name"], "it is the one that moved (%r)" % name)
    check("PRESET_XYZ" in diff, "the position is part of it")
    if box is not None:
        check("AABB" in diff, "and the box")

info = XVR.save(bpy.context, coll)
check(info["written"] == 1, "one block written (%d)" % info["written"])

after = open(copy, "rb").read()
before_lines = original.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
after_lines = after.decode("utf-8", "replace").replace("\r\n", "\n").split("\n")
check(len(before_lines) == len(after_lines),
      "the line count is unchanged (%d -> %d)" % (len(before_lines), len(after_lines)))
diff_lines = [(i, a, b) for i, (a, b) in enumerate(zip(before_lines, after_lines))
              if a != b]
print("   changed line(s): %s"
      % ", ".join("%d: %r -> %r" % (i, a.strip(), b.strip()) for i, a, b in diff_lines),
      flush=True)
check(1 <= len(diff_lines) <= 3, "only the moved directives were rewritten (%d)"
      % len(diff_lines))
for _i, _a, b in diff_lines:
    check(b.strip().split()[0] in ("PRESET_XYZ", "AABB"),
          "a rewritten line is a directive we moved: %r" % b.strip().split()[0])

# the new numbers are the scene's numbers
again = VC.parse(copy)
hotspot = next(h for h in again.hotspots if h.name == entry["name"])
origin = XVR.origin_of(coll)
want = VC.to_xp(tuple(cam.matrix_world.translation))
want = (want[0] - origin["offset"][0], want[1] - origin["offset"][1],
        want[2] - origin["offset"][2])
check(all(abs(a - b) < 2e-3 for a, b in zip(hotspot.xyz, want)),
      "the file carries the moved position %r (wanted %r)"
      % (tuple(round(v, 4) for v in hotspot.xyz), tuple(round(v, 4) for v in want)))
if box is not None:
    size = tuple(box.dimensions)                    # Blender (x, y, z)
    want_size = (size[0], size[2], size[1])         # -> X-Plane (x, y, z)
    check(all(abs(a - b) < 5e-3 for a, b in zip(hotspot.size, want_size)),
          "the file carries the moved box %r (box is %r in Blender axes)"
          % (tuple(round(v, 3) for v in hotspot.size),
             tuple(round(v, 3) for v in size)))

# --- 3. idempotent ---------------------------------------------------------
info = XVR.save(bpy.context, coll)
check(info["written"] == 0, "a second save finds nothing to do (%d)" % info["written"])
check(open(copy, "rb").read() == after, "and leaves the file alone")

# --- 3b. a manipulator parameter, through the same pending-edit table ------
before_manip = open(copy, "rb").read()
manips = json.loads(coll.get(XVR.PROP_MANIPS))
target = None
for row, entry in enumerate(manips):
    if (entry.get("params") or {}).get("DEG_PER_ACTION"):
        target = (row, entry)
        break
if check(target is not None, "a manipulator block with DEG_PER_ACTION was imported"):
    row, entry = target
    old = entry["params"]["DEG_PER_ACTION"][0]
    new = 15.0 if abs(old - 15.0) > 1e-6 else 7.0
    # through the panel's own rows: that is the path a user takes
    holders = [item for ob in bpy.data.objects for item in ob.xp8_vr_manips
               if item.collection == coll.name and item.row == row]
    if check(bool(holders), "the block has a row on the mesh that carries it"):
        holders[0].has_deg = True
        holders[0].deg = new
        holders[0].hold = True
    info = XVR.save(bpy.context, coll)
    check(info["written"] == 1, "the block was written (%d)" % info["written"])
    after_manip = open(copy, "rb").read()
    lines_before = before_manip.decode("utf-8", "replace").split("\n")
    lines_after = after_manip.decode("utf-8", "replace").split("\n")
    import difflib
    diff = list(difflib.unified_diff(lines_before, lines_after, "before", "after",
                                     n=2, lineterm=""))
    print("   manip diff:\n%s" % "\n".join("      " + d for d in diff[:40]), flush=True)
    changed = len([d for d in diff if d[:1] in "+-" and not d.startswith(("+++", "---"))])
    check(changed == 3, "one line rewritten and one added (%d changed lines)" % changed)
    check(len(lines_after) == len(lines_before) + 1,
          "HOLD_MANIP added a line (%d -> %d)" % (len(lines_before), len(lines_after)))
    check(any(line.strip() == "DEG_PER_ACTION %g" % new
              or line.strip().startswith("DEG_PER_ACTION %g" % new)
              for line in lines_after),
          "the new DEG_PER_ACTION is in the file")
    check(any(line.strip() == "HOLD_MANIP" for line in lines_after),
          "and so is the flag")
    fresh = VC.parse(copy)
    block = next((m for m in fresh.manips
                  if m.type == entry["type"] and m.a == entry["a"]), None)
    check(block is not None and block.values("DEG_PER_ACTION") == [new]
          and block.has("HOLD_MANIP"),
          "and it reads back as %r + HOLD_MANIP" % (new,))
    # and the pending table is empty again
    check(XVR.manip_edits(coll) == {}, "the pending edits were cleared")
    info = XVR.save(bpy.context, coll)
    check(info["written"] == 0, "saving again writes nothing (%d)" % info["written"])

    # --- 3c. an ACTION line that is not one is refused, not written --------
    before_bad = open(copy, "rb").read()
    reports = []
    holders = [item for ob in bpy.data.objects for item in ob.xp8_vr_manips
               if item.collection == coll.name and item.row == row]
    holders[0].action = "this is not a directive"
    holders[0].has_deg = True
    holders[0].deg = new + 1
    info = XVR.save(bpy.context, coll, lambda level, text: reports.append(
        (tuple(level), text)))
    check(info["written"] == 1, "the rest of the edit still went in (%d)"
          % info["written"])
    check(any("is not an ACTION line" in text for _level, text in reports),
          "a warning says why the ACTION was left alone: %r"
          % ([t for _l, t in reports],))
    after_bad = open(copy, "rb").read().decode("utf-8", "replace")
    check("this is not a directive" not in after_bad,
          "the bad ACTION text is not in the file")
    check(after_bad.count("\n") == before_bad.decode("utf-8", "replace").count("\n"),
          "and it did not add a line either (%d -> %d)"
          % (before_bad.count(b"\n"), after_bad.count("\n")))

    # --- 3d. unticking a box takes the directive out of the file ----------
    holders = [item for ob in bpy.data.objects for item in ob.xp8_vr_manips
               if item.collection == coll.name and item.row == row]
    if check(bool(holders), "the row is still there to untick"):
        holders[0].has_deg = False
        holders[0].hold = False
        pending = XVR.manip_edits(coll)
        check(pending.get(str(row), {}).get("DEG_PER_ACTION", "missing") is None,
              "unticking records a removal, not a no-op: %r" % (pending,))
        info = XVR.save(bpy.context, coll)
        check(info["written"] == 1, "and the block is written (%d)" % info["written"])
        back = VC.parse(copy)
        same = next((m for m in back.manips if m.type == entry["type"]
                     and m.a == entry["a"]), None)
        check(same is not None, "the block is still in the file")
        if same is not None:
            check(not same.values("DEG_PER_ACTION"),
                  "its DEG_PER_ACTION is gone: %r" % (same.values("DEG_PER_ACTION"),))
            check(not same.has("HOLD_MANIP"), "and so is its HOLD_MANIP")
        check(len(back.manips) >= 1, "the file still parses (%d block(s))"
              % len(back.manips))

# --- 4. a file that moved on disk is not overwritten ----------------------
with open(copy, "ab") as fh:
    fh.write(b"\n# someone else edited this file\n")
touched = open(copy, "rb").read()
cam.location = cam.matrix_world.translation + Vector((0.01, 0.0, 0.0))
info = XVR.save(bpy.context, coll)
check(info["written"] == 0, "a stale scene refuses to write (%d)" % info["written"])
check(open(copy, "rb").read() == touched,
      "and the other edit survives untouched")

shutil.rmtree(work, ignore_errors=True)
print("\nRESULT: %s" % ("FAIL %d" % len(fails) if fails else "PASS"), flush=True)
sys.exit(1 if fails else 0)
