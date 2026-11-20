"""Reproduce the weld import in a real window (modal path, event loop, HUD).

    /Applications/Blender.app/Contents/MacOS/Blender --factory-startup \
        --python verify/gui_weld_probe.py -- <file.acf>

`-b` drains the whole import synchronously in `execute`, so it never exercises
the timer ticks, the depsgraph, undo snapping or the "Adjust Last Operation" HUD
that a real session has.  Those are where the crash is, so this runs with a
window: it starts the import from a timer, watches the collections, and records
what it saw before Blender either finishes or dies.
"""
import os
import sys
import time
import traceback

import bpy

LOG = os.environ.get("XP8_GUI_LOG", "/tmp/gui_weld_probe.log")

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ACF = argv[0] if argv else os.path.join(
    os.path.expanduser("~/X-Plane 12/Aircraft"),
    "King Air C90B EVO G1000 V1.3.1", "C90B.acf")
WELD = (os.environ.get("XP8_WELD", "1") != "0")
WAIT = float(os.environ.get("XP8_WAIT", "120"))


def log(message):
    line = "%s %s" % (time.strftime("%H:%M:%S"), message)
    with open(LOG, "a") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


open(LOG, "w").close()
log("start: %s weld=%s background=%s" % (ACF, WELD, bpy.app.background))

# --factory-startup loads factory preferences, which has the extension switched
# off - turn it on the way a user's session has it
for module in ("bl_ext.user_default.io_scene_xplane_obj",
               "bl_ext.user_default.io_xplane2blender"):
    try:
        bpy.ops.preferences.addon_enable(module=module)
        log("enabled %s" % module)
    except Exception as exc:                                # noqa: BLE001
        log("could not enable %s: %s" % (module, exc))

state = {"started": 0.0, "seen": 0}


def watch():
    """Sample the scene so a crash leaves a trail of what was happening."""
    collections = len(bpy.data.collections)
    objects = len(bpy.data.objects)
    grew = collections != state["seen"]
    state["seen"] = collections
    elapsed = time.time() - state["started"]
    if grew or int(elapsed) % 10 < 2:
        log("t=%4.1fs collections=%d objects=%d" % (elapsed, collections, objects))
    if elapsed > WAIT:
        log("gave up waiting (no crash?)")
        bpy.ops.wm.quit_blender()
        return None
    if elapsed > 5 and not grew and objects:
        log("settled: collections=%d objects=%d" % (collections, objects))
        bpy.ops.wm.save_as_mainfile(filepath="/tmp/xp8_gui_probe.blend")
        log("saved the result, quitting")
        bpy.ops.wm.quit_blender()
        return None
    return 1.0


def start():
    state["started"] = time.time()
    try:
        result = bpy.ops.import_scene.xplane_acf(filepath=ACF, place_by_acf=True,
                                                 weld_vertices=WELD)
        log("operator returned %r" % (result,))
    except Exception:                                       # noqa: BLE001
        log("operator raised:\n%s" % traceback.format_exc())
    bpy.app.timers.register(watch, first_interval=1.0)
    return None


bpy.app.timers.register(start, first_interval=1.0)
