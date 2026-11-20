"""The VR manipulator panel on the object: rows, callbacks, and a draw pass.

    <blender> -b --factory-startup --python verify/vrconfig_panel_test.py -- <file.acf>

A `BEGIN_MANIP` block is keyed by an `ATTR_manip_*` line *inside a mesh*, so its
parameters are edited where the block is: Properties > Object, next to
XPlane2Blender's own manipulator settings.  The rows therefore live on objects
(`obj.xp8_vr_manips`), a block that drives several meshes appears on each of
them, and every field change has to land in the pending table of the collection
that owns the file - or "Save VR Config" writes nothing.

`-b` never draws, so the panel's `draw()` is called by hand with a stub layout:
that is the only way to grade the UI path before a user clicks it.
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

XVR = IMP.xpvr

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
check(bpy.ops.import_scene.xplane_acf(filepath=ACF, place_by_acf=True) == {'FINISHED'},
      "the .acf import finished")
scene = bpy.context.scene
coll = next((c for c in bpy.data.collections if c.get(XVR.PROP_SOURCE)), None)
if not check(coll is not None, "a vrconfig collection came in"):
    print("\nRESULT: FAIL nothing to test", flush=True)
    raise SystemExit(1)

entries = json.loads(coll.get(XVR.PROP_MANIPS) or "[]")

# --- the rows are on the meshes, not on the scene -------------------------
rows = {}
for ob in bpy.data.objects:
    for item in ob.xp8_vr_manips:
        rows.setdefault(ob.name, []).append(item)
total_rows = sum(len(v) for v in rows.values())
expected = sum(len([n for n in (e.get("objects") or [])
                    if bpy.data.objects.get(n)]) for e in entries)
print("   %d block(s) -> %d row(s) on %d object(s)"
      % (len(entries), total_rows, len(rows)), flush=True)
check(total_rows == expected,
      "one row per block per mesh that carries it (%d vs %d)"
      % (total_rows, expected))
check(bool(rows), "at least one mesh carries a manipulator")

with_deg = [i for v in rows.values() for i in v if i.has_deg]
copied = [i for v in rows.values() for i in v if i.copied]
print("   rows with DEG_PER_ACTION: %d, on a duplicated key: %d"
      % (len(with_deg), len(copied)), flush=True)
check(bool(with_deg), "the blocks' parameters reached the rows")

# --- the field callbacks must land in the pending table ------------------
def rows_of(objects):
    out = []
    for name in objects:
        for item in bpy.data.objects[name].xp8_vr_manips:
            if item.collection == coll.name:
                out.append((name, item))
    return out


# a block that drives several meshes, if this aircraft has one
shared = None
for row, entry in enumerate(entries):
    names = [n for n in (entry.get("objects") or []) if bpy.data.objects.get(n)]
    if len(names) > 1:
        shared = (row, entry, names)
        break
if shared is not None:
    row, entry, names = shared
    print("   shared block %r drives %s" % (entry.get("a"), names), flush=True)
    holders = [pair for pair in rows_of(names) if pair[1].row == row]
    check(len(holders) == len(names), "each mesh shows a row for it (%d)"
          % len(holders))
    check(all(item.others for _n, item in holders),
          "and each row names the other meshes it drives")
else:
    entry = next(e for e in entries if e.get("objects"))
    names = [n for n in entry["objects"] if bpy.data.objects.get(n)]
    holders = [pair for pair in rows_of(names) if pair[1].row == entries.index(entry)]
    print("   no block drives two meshes in this file; using %r on %s"
          % (entry.get("a"), names), flush=True)
    check(bool(holders), "the block has a row on the mesh that carries it")

XVR.clear_manip_edits(coll)
_name, item = holders[0]
item.deg = 44.0
item.has_deg = True
pending = XVR.manip_edits(coll)
check(pending.get(str(item.row), {}).get("DEG_PER_ACTION") == [44.0],
      "editing the number records a pending edit on %r: %r"
      % (coll.name, pending))
others = [(n, i) for n, i in holders if i is not item]
if others:
    check(all(i.has_deg and abs(i.deg - 44.0) < 1e-6 for _n, i in others),
          "and the other meshes' rows show the same number: %r"
          % ([(n, i.deg) for n, i in others],))
item.hold = True
check(XVR.manip_edits(coll)[str(item.row)].get("HOLD_MANIP") is True,
      "so does ticking a flag")
if others:
    check(all(i.hold for _n, i in others),
          "and the other meshes' rows show the flag too")
had_deg = bool((entries[item.row].get("params") or {}).get("DEG_PER_ACTION"))
item.has_deg = False
pending = XVR.manip_edits(coll).get(str(item.row), {})
if had_deg:
    check(pending.get("DEG_PER_ACTION", "missing") is None,
          "unticking a directive the file has records a removal: %r" % (pending,))
else:
    check("DEG_PER_ACTION" not in pending,
          "unticking one the file never had leaves nothing pending: %r"
          % (pending,))
XVR.clear_manip_edits(coll)
check(XVR.manip_edits(coll) == {}, "and the table can be cleared")


# --- draw the panel by hand ----------------------------------------------
class Stub:
    """Everything `draw()` touches, recording what it was asked to show."""

    def __init__(self, name="", sink=None):
        self._name = name
        self._sink = sink if sink is not None else []
        self.active = True

    def label(self, text="", **_kwargs):
        self._sink.append(str(text)[:60])
        return Stub("%s.label" % self._name, self._sink)

    def prop(self, _data, name="", **_kwargs):
        self._sink.append(str(name))
        return Stub("%s.prop" % self._name, self._sink)

    def operator(self, _idname, **_kwargs):
        self._sink.append("operator")
        return Stub("%s.op" % self._name, self._sink)

    def __getattr__(self, name):
        def call(*_args, **_kwargs):
            return Stub("%s.%s" % (self._name, name), self._sink)
        return call


def draw_panel(cls, context):
    """Call a panel's draw() the way a redraw does, without a UI."""
    fake = type("FakePanel", (), {})()
    fake.layout = Stub("layout")
    cls.draw(fake, context)
    return fake.layout._sink


carrier = bpy.data.objects[sorted(rows)[0]]
context = type("C", (), {"object": carrier, "scene": scene})()
try:
    shown = draw_panel(IMP.OBJECT_PT_xplane_vr, context)
    drew, error = True, ""
except Exception as exc:                                    # noqa: BLE001
    shown, drew, error = [], False, "%s: %s" % (type(exc).__name__, exc)
check(drew, "OBJECT_PT_xplane_vr.draw() does not raise%s"
      % ("" if drew else " (%s)" % error))
if drew:
    print("   the panel asked the layout for %d widget(s): %r"
          % (len(shown), shown[:8]), flush=True)
    check(any("DEG_PER_ACTION" in text for text in shown),
          "and it shows the parameter labels")
check(IMP.OBJECT_PT_xplane_vr.poll(context) is True,
      "poll() says yes for a mesh that carries a block")

empty = bpy.data.objects.new("xp8_empty_probe", None)
scene.collection.objects.link(empty)
check(IMP.OBJECT_PT_xplane_vr.poll(
    type("C", (), {"object": empty, "scene": scene})()) is False,
    "and no for an object that carries none")
bpy.data.objects.remove(empty, do_unlink=True)

# --- importing the same aircraft again must not double anything ----------
collections_before = len([c for c in bpy.data.collections if c.get(XVR.PROP_SOURCE)])
rows_before = sum(len(ob.xp8_vr_manips) for ob in bpy.data.objects)
bpy.ops.import_scene.xplane_acf(filepath=ACF, place_by_acf=True)
collections_after = len([c for c in bpy.data.collections if c.get(XVR.PROP_SOURCE)])
rows_after = sum(len(ob.xp8_vr_manips) for ob in bpy.data.objects)
print("   re-import: %d vrconfig collection(s) / %d row(s) -> %d / %d"
      % (collections_before, rows_before, collections_after, rows_after),
      flush=True)
check(collections_after == 1,
      "re-importing replaces the vrconfig collection instead of stacking one (%d)"
      % collections_after)
check(rows_after == rows_before,
      "and the rows are not doubled (%d -> %d)" % (rows_before, rows_after))
stale_rows = [item.collection for ob in bpy.data.objects for item in ob.xp8_vr_manips
              if bpy.data.collections.get(item.collection) is None]
check(not stale_rows, "no row points at a collection that no longer exists: %r"
      % (stale_rows[:4],))

# the hotspot panel is still there and still draws
try:
    draw_panel(IMP.VIEW3D_PT_xplane_vr, type("C", (), {"scene": scene,
                                                       "object": carrier})())
    check(True, "the VR hotspot panel still draws")
except Exception as exc:                                    # noqa: BLE001
    check(False, "the VR hotspot panel raised %s: %s"
          % (type(exc).__name__, exc))

print("\nRESULT: %s" % ("FAIL %d" % len(fails) if fails else "PASS"), flush=True)
sys.exit(1 if fails else 0)
