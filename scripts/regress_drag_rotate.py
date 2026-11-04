"""Regression: every aircraft .obj that uses ATTR_manip_drag_rotate must still
import and export, and keep all of its manipulators.

    blender -b --factory-startup --python scripts/regress_drag_rotate.py -- \
            <aircraft-root> <out-dir> [max-files]

Walks the aircraft folder for .obj files that carry the type (the add-on and
Laminar cockpits both do), imports each, exports with XPlane2Blender and prints
one line per file: import result, how many drag_rotate the conversion gave up
on, the export result and how many lines came back out.
"""
import glob
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
ROOT, OUTDIR = argv[0], argv[1]
LIMIT = int(argv[2]) if len(argv) > 2 else 0
os.makedirs(OUTDIR, exist_ok=True)

files = sorted(p for p in glob.glob(os.path.join(ROOT, "**", "*.obj"),
                                    recursive=True)
               if "ATTR_manip_drag_rotate" in open(p, "rb").read().decode(
                   "latin-1", "replace"))
if LIMIT:
    files = files[:LIMIT]
if len(argv) > 3:
    wanted = argv[3].split(",")
    files = [p for p in files if any(w in p for w in wanted)]
print("files:", len(files))


def count(path):
    return sum(1 for line in open(path, "rb")
               if b"ATTR_manip_drag_rotate" in line)


for path in files:
    name = os.path.basename(path)
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    try:
        res = bpy.ops.import_scene.xplane_obj(filepath=path)
    except Exception as exc:
        print("IMPORT-RAISED %-46s %s" % (name, exc))
        continue
    colls = [c for c in bpy.data.collections if c.xplane.is_exportable_collection]
    dropped = 0
    for coll in colls:
        try:
            import json
            for w in json.loads(coll.get("xp2b_convert_warnings", "[]")):
                if "drag_rotate" in w:
                    dropped += 1
        except ValueError:
            pass
    out = os.path.join(OUTDIR, name)
    try:
        exp = bpy.ops.export.xplane_obj(filepath=out)
    except Exception as exc:
        print("EXPORT-RAISED %-46s %s" % (name, exc))
        continue
    src_n = count(path)
    out_n = count(out) if os.path.exists(out) else 0
    print("%-46s import=%s drop=%d export=%s manips %d->%d %s"
          % (name, list(res), dropped, list(exp), src_n, out_n,
             "" if (src_n == out_n and os.path.exists(out)) else "  <-- CHECK"))
