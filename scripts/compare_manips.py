"""Does the manipulator survive the round trip through Blender + XP2B?

    blender -b --factory-startup --python scripts/compare_manips.py -- \
            <source.obj> <out-dir>

Imports the file (which converts it), exports it again with XPlane2Blender,
then compares every ATTR_manip_drag_rotate against the source: the pivot, the
axis, both angle ends, the detent lift, both value ranges, both datarefs and
the tooltip.  Kinds are compared as sets per line because XP2B sorts the key
table ascending, which swaps the two ends of a pair legitimately.

Also reports ATTR_axis_detent_range as a multiset per batch (a duplicated table
is the failure this catches: it used to be snapshotted twice).
"""
import json
import os
import sys

import bpy
import addon_utils

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

addon_utils.enable("io_xplane2blender", default_set=True)
import io_scene_xplane_obj as IMP                            # noqa: E402

try:
    IMP.register()
except Exception:
    pass

OC = IMP.objcodec
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SRC, OUTDIR = argv[0], argv[1]
os.makedirs(OUTDIR, exist_ok=True)
base = os.path.splitext(os.path.basename(SRC))[0]

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
print("import:", bpy.ops.import_scene.xplane_obj(filepath=SRC))
res = bpy.ops.export.xplane_obj(filepath=os.path.join(OUTDIR, base + ".obj"))
print("export:", res)
written = os.path.join(OUTDIR, base + ".obj")
if not os.path.exists(written):
    print("NOTHING WRITTEN")
    raise SystemExit(0)
print("wrote:", written, os.path.getsize(written), "bytes")


def manips(path_or_text):
    obj = (OC.parse(path_or_text) if os.path.exists(path_or_text)
           else OC.parse_text(path_or_text))
    out = []
    for leaf in obj.leaves:
        state = dict((c, list(r)) for c, r in leaf.state)
        line = None
        for raw in state.get("manip", []):
            if "drag_rotate" in raw:
                line = raw.split()
        if line is None:
            continue
        nums = [round(float(w), 2) for w in line[2:15]]
        out.append({
            "pivot": tuple(nums[0:3]),
            "axis": tuple(nums[3:6]),
            "angles": tuple(sorted(nums[6:8])),
            "lift": nums[8],
            "v1": tuple(sorted(nums[9:11])),
            "v2": tuple(sorted(nums[11:13])),
            "datarefs": (line[15], line[16]),
            "tooltip": " ".join(line[17:]),
            "ranges": sorted(tuple(round(float(w), 3) for w in r.split()[1:4])
                             for r in state.get("detent_range", [])),
        })
    return out


src = sorted(manips(SRC), key=lambda m: (m["datarefs"], m["tooltip"],
                                         m["pivot"]))
dst = sorted(manips(written), key=lambda m: (m["datarefs"], m["tooltip"],
                                             m["pivot"]))
print("drag_rotate in source:", len(src), " exported:", len(dst))
bad = 0
for want, got in zip(src, dst):
    if want["datarefs"] != got["datarefs"] or want["tooltip"] != got["tooltip"]:
        print("MISALIGNED", want["datarefs"], want["tooltip"])
        bad += 1
        continue
    for key in ("pivot", "axis", "angles", "lift", "v1", "v2", "ranges"):
        if got[key] != want[key]:
            print("DIFF", want["tooltip"], key, "source", want[key],
                  "exported", got[key])
            bad += 1
print("mismatched:", bad)
