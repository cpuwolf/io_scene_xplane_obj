"""Where does the animation data actually live after an import?"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
import bpy                                    # noqa

addon = __import__(os.path.basename(REPO))
addon.register()
XM = addon.XM
OC = addon.OC

SRC = ("/Users/rourou/X-Plane 12/Aircraft/King Air C90B EVO G1000 V1.3.1"
       "/objects/C90B_exterior/C90B_fuselage.obj")

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.xplane_obj(filepath=SRC)
header = [o for o in bpy.data.objects if XM.is_header(o)][0]

print("=== header object %r ===" % header.name)
print("%-22s %-8s %s" % ("custom property", "bytes", "what"))
kinds = {
    "xp8_dump": "the whole original file, verbatim",
    "xp8_events": "the ordered command stream (JSON)",
    "xp8_globals": "TEXTURE / POINT_COUNTS / GLOBAL_* lines",
    "xp8_leaf_objs": "leaf index -> object name",
    "xp8_fingerprints": "object name -> geometry hash",
    "xp8_source": "absolute path of the .obj",
    "xp8_line_ending": "lf / crlf / cr",
    "xp8_header_flag": "A or I",
    "xp8_version": "800",
    "xp8_warnings": "import warnings",
    "xp8_placed_parts": "how many parts a static ANIM_trans moved",
}
total = 0
for k in sorted(header.keys()):
    v = header[k]
    n = len(v) if isinstance(v, str) else 0
    total += n
    print("%-22s %-8d %s" % (k, n, kinds.get(k, "")))
print("%-22s %-8d (file on disk: %d bytes)"
      % ("TOTAL", total, os.path.getsize(SRC)))

print("\n=== xp8_events: the command stream ===")
events = json.loads(header["xp8_events"])
print("entries: %d" % len(events))
print("first 6:", events[:6])
i = next(n for n, e in enumerate(events) if e[0] == "A")
print("\naround the first ANIM_begin (index %d):" % i)
for n, e in enumerate(events[i - 2:i + 9], start=i - 2):
    print("  %3d  %-3s %s" % (n, e[0], (e[1] or "")[:78]))

anims = sum(1 for e in events if e[0] == "A")
raw = [e[1] for e in events if e[0] == "R"]
kw = {}
for line in raw:
    kw[line.split()[0]] = kw.get(line.split()[0], 0) + 1
print("\nA (ANIM_begin) = %d   Z (ANIM_end) = %d   R (raw line) = %d   L (geometry) = %d"
      % (anims, sum(1 for e in events if e[0] == "Z"), len(raw),
         sum(1 for e in events if e[0] == "L")))
print("raw keywords:", kw)

print("\n=== one geometry object ===")
part = [c for c in header.children if c.get(XM.PROP_ROLE) == XM.ROLE_TRIS][0]
for k in sorted(part.keys()):
    v = part[k]
    print("  %-18s %s" % (k, (str(v)[:70])))
print("  attributes:", [a.name for a in part.data.attributes])
print("  vertices: %d  -> the xp8_vt int attribute holds the source VT row index"
      % len(part.data.vertices))

print("\n=== is there ANY blender animation data? ===")
print("  actions:", len(bpy.data.actions))
print("  objects with animation_data:", sum(1 for o in bpy.data.objects
                                            if o.animation_data))
