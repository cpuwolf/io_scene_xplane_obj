"""What happens when a collection is freed while the import is still running.

    <blender> -b --factory-startup --python verify/acf_stale_test.py -- <file.acf>

The `.acf`/`.obj` imports are modal: they build datablocks over many timer
ticks.  An undo (Ctrl+Z, `editmode_toggle`) during that window frees whatever the
import had built, and every later touch of the old reference raises
`ReferenceError: StructRNA of type Collection has been removed`.  `_tick`
swallows that per object ("one object was skipped after an error"), but `_finish`
used to let it escape - and an exception unwinding through a modal callback takes
Blender down with it (SIGSEGV in `wm_operator_register`), losing the session.

Two rules come out of that, and this checks both: never hold a datablock across a
tick (`_parents` keeps names), and never let an exception out of `_finish`.
"""
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


def dead_collection(name="xp8_stale_probe"):
    """A real collection reference, freed - the shape an undo leaves behind."""
    coll = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(coll)
    coll["probe"] = 1
    bpy.context.scene.collection.children.unlink(coll)
    bpy.data.collections.remove(coll)
    return coll


stale = dead_collection()
try:
    stale.name
    alive_probe = True
except ReferenceError:
    alive_probe = False
check(not alive_probe, "the probe really is a removed collection")

# --- _alive: the question every holder of a collection must ask ------------
check(IMP._alive(stale) is False, "_alive() says no without raising")
check(IMP._alive(None) is False, "_alive(None) says no")
live = bpy.data.collections.new("xp8_live_probe")
bpy.context.scene.collection.children.link(live)
check(IMP._alive(live) is True, "_alive() says yes for a live collection")
bpy.context.scene.collection.children.unlink(live)
bpy.data.collections.remove(live)

# --- the placement helpers, handed a dead collection ----------------------
for fn in (IMP._apply_placements, IMP._clear_placements):
    try:
        moved = fn(stale)
        check(moved == 0, "%s(dead) returns 0" % fn.__name__)
    except Exception as exc:                                # noqa: BLE001
        check(False, "%s(dead) raised %s: %s" % (fn.__name__,
                                                 type(exc).__name__, exc))

# --- _aircraft_collection must rebuild instead of writing through it ------
class Fake:
    """The bits of the operator these methods touch."""

    def __init__(self):
        self._parents = {"probe.acf": "xp8_no_such_collection"}
        self._acf_files = {"probe.acf": ACF}
        self.reports = []
        self.place_by_acf = True
        self._timer = None
        self._queue = []
        self._made = 1
        self._total = 1
        self._tris = 0
        self._colls = 0
        self._lost = 0
        self._can_convert = True
        self._vr = {}
        self._done = 0

    def report(self, level, message):
        self.reports.append((tuple(level), message))

    def _status(self, context, text):
        pass


    # a real operator instance carries every method of its class; hand the
    # stand-in the one `_finish()` delegates to
    def _finish_run(self, context, stopped=False):
        return IMP.IMPORT_OT_xplane_acf._finish_run(self, context, stopped)


fake = Fake()
cls = IMP.IMPORT_OT_xplane_acf
try:
    fresh = cls._aircraft_collection(fake, bpy.context, "probe.acf")
    rebuilt = fresh is not stale and IMP._alive(fresh)
except Exception as exc:                                    # noqa: BLE001
    fresh, rebuilt = None, False
    print("      %s: %s" % (type(exc).__name__, exc), flush=True)
check(rebuilt, "_aircraft_collection() replaced the missing one with a live one")
check(fake._parents.get("probe.acf") == fresh.name,
      "and stored its name, so the next step finds it again: %r"
      % (fake._parents,))
check(fake._lost == 1, "and counted the loss (%d)" % fake._lost)
if fresh is not None and IMP._alive(fresh):
    bpy.context.scene.collection.children.unlink(fresh)
    bpy.data.collections.remove(fresh)

# --- _finish: the crash. A dead reference must end the run, not Blender ---
fake = Fake()
fake._parents = {"probe.acf": "xp8_stale_finish_name"}
fake._acf_files = {"probe.acf": ACF}
result = None
error = ""
try:
    result = cls._finish(fake, bpy.context)
except Exception as exc:                                    # noqa: BLE001
    error = "%s: %s" % (type(exc).__name__, exc)
check(not error, "_finish() with a dead collection does not raise%s"
      % (" (%s)" % error if error else ""))
print("      result=%r reports=%r" % (result, [m for _l, m in fake.reports]),
      flush=True)
check(result in ({'CANCELLED'}, {'FINISHED'}), "_finish() returns a status: %r"
      % (result,))
check(any("disappeared during the import" in m for _l, m in fake.reports),
      "and says the collection went away: %r" % ([m for _l, m in fake.reports],))

# --- and a healthy run still finishes normally ----------------------------
fake = Fake()
fake._parents = {}
result = cls._finish(fake, bpy.context)
check(result == {'FINISHED'}, "a normal finish still returns FINISHED (%r)"
      % (result,))
check(not any(level[0] == 'ERROR' for level, _m in fake.reports),
      "with no error reported")

print("\nRESULT: %s" % ("FAIL %d" % len(fails) if fails else "PASS"), flush=True)
sys.exit(1 if fails else 0)
