"""Drive a real .acf import one step at a time, and undo it in the middle.

    <blender> -b --factory-startup --python verify/acf_undo_test.py -- <file.acf>

`_step` is one object per timer tick.  Here they are called by hand so a "user
undo" can be injected between two of them: the collections the import has built
so far are freed, exactly as Ctrl+Z does.  Before the fix every later step died
with `ReferenceError: StructRNA of type Collection has been removed` (reported
ten times as "one object was skipped after an error"), and `_finish` then raised
inside the modal callback, which takes the whole Blender process down.
"""
import os
import sys
import traceback

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


for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)

print("importing %s" % ACF, flush=True)
check(bpy.ops.import_scene.xplane_acf(filepath=ACF, place_by_acf=True,
                                      weld_vertices=True) == {'FINISHED'},
      "the .acf import finished")
before = len(bpy.data.collections)
check(before > 1, "collections were built (%d)" % before)

# --- the same import again, with the scene rolled back after two objects ---
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)

cls = IMP.IMPORT_OT_xplane_acf


class Fake:
    """The operator, minus the modal plumbing: filled the way `execute` does."""

    def __init__(self):
        self.reports = []
        self._parents = {}
        self._acf_files = {}
        self._queue = []
        self._total = 0
        self._done = 0
        self._made = 0
        self._tris = 0
        self._colls = 0
        self._lost = 0
        self._timer = None
        self._can_convert = True
        self._vr = {}
        self.place_by_acf = True
        self.weld_vertices = True

    def report(self, level, message):
        self.reports.append((tuple(level), message))
        print("      %s %s" % ("/".join(level), message), flush=True)

    def _status(self, context, text):
        pass

    def _finish_run(self, context, stopped=False):
        return cls._finish_run(self, context, stopped)

    def _step(self, context):
        return cls._step(self, context)

    def _aircraft_collection(self, context, name):
        return cls._aircraft_collection(self, context, name)


# the queue is what `execute` builds: (path, the .acf's basename) per object
group = os.path.basename(ACF)
found, gone = IMP.AF.objects(ACF)
paths = [(path, group) for path in found]
check(len(paths) > 3, "the .acf attaches %d file(s)" % len(paths))

fake = Fake()
fake._queue = list(paths)
fake._total = len(paths)
fake._acf_files[group] = ACF

context = bpy.context
errors = []

# two objects in, the user presses Ctrl+Z: everything the import built is freed
for _ in range(2):
    try:
        cls._step(fake, context)
    except Exception as exc:                                # noqa: BLE001
        errors.append("%s: %s" % (type(exc).__name__, exc))
check(not errors, "the first two steps import (%r)" % (errors,))
check(fake._parents, "the aircraft collection exists (%r)"
      % (list(fake._parents),))

print("   ... undoing the import (freeing what it has built)", flush=True)
for coll in list(bpy.data.collections):
    for ob in list(coll.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.data.collections.remove(coll)

# the rest of the queue runs against a scene that no longer has any of it
skipped, crashes = 0, []
while fake._queue:
    try:
        cls._step(fake, context)
    except Exception as exc:                                # noqa: BLE001
        crashes.append("%s: %s" % (type(exc).__name__, exc))
        skipped += 1
check(not crashes, "no step raises after the undo (%d, first: %s)"
      % (skipped, crashes[0] if crashes else ""))
check(fake._lost >= 1, "the loss is counted (%d)" % fake._lost)

result, error = None, ""
try:
    result = cls._finish(fake, context)
except Exception as exc:                                    # noqa: BLE001
    error = "%s: %s" % (type(exc).__name__, exc)
    traceback.print_exc()
check(not error, "_finish() after the undo does not raise (%s)" % error)
check(result == {'FINISHED'}, "the import still finishes (%r)" % (result,))
check(any("disappeared during the import" in message
          for _level, message in fake.reports),
      "with a warning that says what was lost: %r"
      % ([m for _l, m in fake.reports],))
check(len([c for c in bpy.data.collections if c.name.endswith(".acf")]) == 1,
      "and the objects after the undo landed in a fresh aircraft collection")

print("\nRESULT: %s" % ("FAIL %d" % len(fails) if fails else "PASS"), flush=True)
sys.exit(1 if fails else 0)
