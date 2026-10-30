"""Does the export really write only the selected parts?

Drives the real operator, then parses the result back with the pure-python
codec: geometry is expected to shrink to the selection while the command
stream (ANIM_*, ATTR_*, LIGHT_PARAM, globals) stays intact, and POINT_COUNTS
matches the tables that were actually written.

    blender -b --factory-startup --python verify/check_selection.py
"""
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
import bpy                                   # noqa

addon = __import__(os.path.basename(REPO))
addon.register()
OC = addon.OC
XM = addon.XM

FIXTURE = os.path.join(REPO, "verify", "fixtures", "mini.obj")
TMP = tempfile.mkdtemp(prefix="xp8_sel_")
FAILED = []


def point_counts(obj8):
    for line in obj8.globals:
        w = line.split()
        if w and w[0] == "POINT_COUNTS":
            return w[1:]
    return None


def check(name, ok, detail=""):
    print("  %-52s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "   <- " + detail))
    if not ok:
        FAILED.append(name)


def fresh_import(path=FIXTURE):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.xplane_obj(filepath=path)
    header = [o for o in bpy.data.objects if XM.is_header(o)][0]
    parts = [c for c in header.children if c.get(XM.PROP_ROLE) == XM.ROLE_TRIS]
    return header, parts


def select(objs):
    bpy.ops.object.select_all(action='DESELECT')
    for ob in objs:
        ob.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[-1]


def export(path, mode='FILE'):
    """Run the operator; a reported ERROR comes back as RuntimeError."""
    try:
        return bpy.ops.export_scene.xplane_obj(filepath=path, mode=mode,
                                               patch_obj=False)
    except RuntimeError as exc:
        return {'CANCELLED', str(exc)}


def commands(obj8):
    """The command stream proper: ANIM blocks and raw lines, no batches."""
    out = []
    for kind, payload in obj8.events:
        if kind == OC.EV_ANIM_BEGIN:
            out.append("ANIM_begin")
        elif kind == OC.EV_ANIM_END:
            out.append("ANIM_end")
        elif kind == OC.EV_RAW:
            out.append(payload)
    return out


src = OC.parse(FIXTURE)
print("fixture: %d leaves, %d VT rows, %d indices"
      % (len(src.leaves), len(src.vt), len(src.idx)))
print("source command stream: %s" % commands(src))

# ---------------------------------------------------------------------------
print("\n[1] one part of two selected -> only that part's geometry")
header, parts = fresh_import()
print("  parts: %s" % [p.name for p in parts])
select([parts[0]])
out_a = os.path.join(TMP, "part0.obj")
res = export(out_a)
check("operator finished", 'FINISHED' in res, str(res))
a = OC.parse(out_a)
check("two batches in, one out", len(a.leaves) == 1, "%d leaves" % len(a.leaves))
check("geometry is the 2-triangle quad",
      [lf.count for lf in a.leaves] == [6], str([lf.count for lf in a.leaves]))
check("command stream kept verbatim",
      commands(a) == commands(src),
      "%s != %s" % (commands(a), commands(src)))
check("ANIM block still balanced",
      sum(1 for k, _ in a.events if k == OC.EV_ANIM_BEGIN)
      == sum(1 for k, _ in a.events if k == OC.EV_ANIM_END))
check("LIGHT_PARAM survived", any(str(p).startswith("LIGHT_PARAM")
                                 for k, p in a.events if k == OC.EV_RAW))
check("VT table shrank to the used rows", len(a.vt) == 4, "%d rows" % len(a.vt))
check("every index is inside the VT table",
      all(0 <= i < len(a.vt) for i in a.idx), str(a.idx))
check("every span is inside the index table",
      all(lf.offset + lf.count <= len(a.idx) and lf.count > 0
          for lf in a.leaves))
check("POINT_COUNTS matches the tables",
      point_counts(a) == [str(len(a.vt)), "0", "0", str(len(a.idx))],
      "%s vs vt=%d idx=%d" % (point_counts(a), len(a.vt), len(a.idx)))
check("textures / globals untouched",
      [g for g in a.globals if not g.startswith("POINT_COUNTS")]
      == [g for g in src.globals if not g.startswith("POINT_COUNTS")])

print("\n[2] the other part -> the animation's own triangle, ANIM kept")
header, parts = fresh_import()
select([parts[1]])
out_b = os.path.join(TMP, "part1.obj")
export(out_b)
b = OC.parse(out_b)
check("one batch", len(b.leaves) == 1, "%d" % len(b.leaves))
check("3 indices written", b.leaves[0].count == 3, str(b.leaves[0].count))
check("command stream kept verbatim",
      commands(b) == commands(src),
      "%s != %s" % (commands(b), commands(src)))
check("3 VT rows", len(b.vt) == 3, "%d" % len(b.vt))
check("every index is inside the VT table",
      all(0 <= i < len(b.vt) for i in b.idx), str(b.idx))

print("\n[3] the header Empty selected -> the whole object, byte for byte")
header, parts = fresh_import()
select([header])
out_c = os.path.join(TMP, "whole.obj")
export(out_c)
with open(FIXTURE, "rb") as f:
    want = f.read()
with open(out_c, "rb") as f:
    got = f.read()
check("identical to the source file", got == want)

print("\n[4] nothing selected -> the whole scene")
header, parts = fresh_import()
bpy.ops.object.select_all(action='DESELECT')
out_d = os.path.join(TMP, "none.obj")
res = export(out_d)
check("operator finished", 'FINISHED' in res, str(res))
with open(out_d, "rb") as f:
    got = f.read()
with open(FIXTURE, "rb") as f:
    want = f.read()
check("one object in the scene: written whole, byte for byte", got == want)

# two imported objects, nothing selected: one file cannot hold two command
# streams, so the geometry is flattened and the streams are dropped
shutil.copy2(FIXTURE, os.path.join(TMP, "mini2.obj"))
shutil.copy2(os.path.join(REPO, "verify", "fixtures", "mini.png"), TMP)
shutil.copy2(os.path.join(REPO, "verify", "fixtures", "mini_NRM.png"), TMP)
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.xplane_obj(filepath=FIXTURE)
bpy.ops.import_scene.xplane_obj(filepath=os.path.join(TMP, "mini2.obj"))
check("two imported objects in the scene",
      len([o for o in bpy.data.objects if XM.is_header(o)]) == 2)
bpy.ops.object.select_all(action='DESELECT')
out_g = os.path.join(TMP, "scene.obj")
res = export(out_g)
check("operator finished", 'FINISHED' in res, str(res))
s = OC.parse(out_g)
check("every mesh went in (2 objects x 2 batches)", len(s.leaves) == 4,
      "%d leaves" % len(s.leaves))
check("command streams dropped (one file, one header)",
      not any(k in (OC.EV_ANIM_BEGIN, OC.EV_ANIM_END) or
              (k == OC.EV_RAW and p and p.startswith(("ANIM_", "LIGHT_PARAM")))
              for k, p in s.events))

print("\n[5] back to source files, one part selected -> subset in place")
work = os.path.join(TMP, "wb")
os.makedirs(work)
for name in ("mini.obj", "mini.png", "mini_NRM.png"):
    shutil.copy2(os.path.join(REPO, "verify", "fixtures", name), work)
header, parts = fresh_import(os.path.join(work, "mini.obj"))
select([parts[0]])
res = export(os.path.join(work, "ignored.obj"), mode='WRITE_BACK')
check("operator finished", 'FINISHED' in res, str(res))
w = OC.parse(os.path.join(work, "mini.obj"))
check("source file now holds one batch", len(w.leaves) == 1,
      "%d" % len(w.leaves))
check("command stream kept verbatim", commands(w) == commands(src))

print("\n[5b] back to source files, nothing selected -> every object")
work2 = os.path.join(TMP, "wb_all")
os.makedirs(work2)
for name in ("mini.obj", "mini.png", "mini_NRM.png"):
    shutil.copy2(os.path.join(REPO, "verify", "fixtures", name), work2)
shutil.copy2(os.path.join(work2, "mini.obj"), os.path.join(work2, "mini2.obj"))
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.xplane_obj(filepath=os.path.join(work2, "mini.obj"))
bpy.ops.import_scene.xplane_obj(filepath=os.path.join(work2, "mini2.obj"))
stamps = [os.stat(os.path.join(work2, n)).st_mtime_ns
          for n in ("mini.obj", "mini2.obj")]
bpy.ops.object.select_all(action='DESELECT')
res = export(os.path.join(work2, "ignored.obj"), mode='WRITE_BACK')
check("operator finished", 'FINISHED' in res, str(res))
after = [os.stat(os.path.join(work2, n)).st_mtime_ns
         for n in ("mini.obj", "mini2.obj")]
check("both source files were written", after != stamps, "%s -> %s"
      % (stamps, after))
for name in ("mini.obj", "mini2.obj"):
    with open(os.path.join(work2, name), "rb") as f:
        check("%s untouched = byte identical" % name, f.read() == want)

print("\n[6] the subset file still imports")
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.xplane_obj(filepath=out_a)
h2 = [o for o in bpy.data.objects if XM.is_header(o)]
check("re-imported without error", len(h2) == 1)
check("one part rebuilt",
      len([c for c in h2[0].children if c.get(XM.PROP_ROLE) == XM.ROLE_TRIS]) == 1)

print("\n[7] a LINES primitive next to a TRIS part")
lines_src = os.path.join(TMP, "lines.obj")
with open(lines_src, "w") as f:
    f.write("I\n800\nOBJ\n\nTEXTURE\nPOINT_COUNTS\t4 2 0 8\n\n"
            "VT\t0 0 0 0 1 0 0 0\nVT\t1 0 0 0 1 0 1 0\n"
            "VT\t1 0 1 0 1 0 1 1\nVT\t0 0 1 0 1 0 0 1\n"
            "VLINE\t0 0 0\nVLINE\t1 0 0\n"
            "IDX10\t0 1 2 0 2 3 0 1\nTRIS\t0 6\nLINES\t6 2\n")
l_src = OC.parse(lines_src)
header, _ = fresh_import(lines_src)
kids = list(header.children)
lines_part = [c for c in kids if c.get(XM.PROP_ROLE) == XM.ROLE_LINES][0]
tris_part = [c for c in kids if c.get(XM.PROP_ROLE) == XM.ROLE_TRIS][0]
select([lines_part])
out_e = os.path.join(TMP, "lines_only.obj")
res = export(out_e)
check("lines part exports on its own", 'FINISHED' in res, str(res))
e = OC.parse(out_e)
check("only the LINES batch is written",
      [lf.kind for lf in e.leaves] == ["LINES"], str([lf.kind for lf in e.leaves]))
check("its indices still point into VLINE",
      e.leaves[0].offset + 2 <= len(e.idx)
      and e.leaves[0].count == 2, str([(e.leaves[0].offset, e.leaves[0].count)]))
check("VLINE rows kept", len(e.vline) == len(l_src.vline),
      "%d vs %d" % (len(e.vline), len(l_src.vline)))
select([tris_part])
out_f = os.path.join(TMP, "tris_only.obj")
export(out_f)
g = OC.parse(out_f)
check("only the TRIS batch is written",
      [lf.kind for lf in g.leaves] == ["TRIS"], str([lf.kind for lf in g.leaves]))
check("every index is inside the VT table",
      all(0 <= i < len(g.vt) for i in g.idx), str(g.idx))

shutil.rmtree(TMP, ignore_errors=True)
print("\n%s" % ("FAILED: " + ", ".join(FAILED) if FAILED else "all checks passed"))
sys.exit(1 if FAILED else 0)
