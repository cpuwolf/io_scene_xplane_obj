"""Screenshot the VR manipulator panel in Properties > Object, next to XP2B's.

    <blender> --factory-startup --python verify/gui_vr_panel_shot.py -- <file.acf> <out.png>

A UI change can only be checked in a window: `-b` never draws a panel, so this
imports an aircraft, selects a mesh that carries a VR manipulator, switches the
Properties editor to the Object tab, opens the panel and saves a screenshot.
"""
import os
import sys
import time

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ACF = argv[0] if argv else os.path.join(
    os.path.expanduser("~/X-Plane 12/Aircraft"),
    "King Air C90B EVO G1000 V1.3.1", "C90B.acf")
OUT = argv[1] if len(argv) > 1 else "/tmp/xp8_vr_panel.png"


def log(message):
    print("%s %s" % (time.strftime("%H:%M:%S"), message), flush=True)


try:
    # the splash is a full-window overlay: a screenshot taken with it up shows
    # the splash, not the panel
    bpy.context.preferences.view.show_splash = False
except Exception as exc:                                    # noqa: BLE001
    print("splash: %s" % exc, flush=True)

for module in ("bl_ext.user_default.io_scene_xplane_obj",):
    try:
        bpy.ops.preferences.addon_enable(module=module)
        log("enabled %s" % module)
    except Exception as exc:                                # noqa: BLE001
        log("could not enable: %s" % exc)

IMP = sys.modules.get("bl_ext.user_default.io_scene_xplane_obj")
if IMP is None:
    log("module not loaded")
    bpy.ops.wm.quit_blender()
    raise SystemExit(1)

state = {"start": 0.0, "seen": -1, "same": 0}


def set_folded(cls, folded):
    """Re-register a panel so it draws folded (or open) - a shot-time trick."""
    try:
        bpy.utils.unregister_class(cls)
    except Exception as exc:                                # noqa: BLE001
        log("unregister %s: %s" % (cls.__name__, exc))
        return
    cls.bl_options = {'DEFAULT_CLOSED'} if folded else set()
    try:
        bpy.utils.register_class(cls)
    except Exception as exc:                                # noqa: BLE001
        log("re-register %s: %s" % (cls.__name__, exc))


def open_the_panel():
    """Re-register ours open; everything else stays exactly as it is."""
    set_folded(IMP.OBJECT_PT_xplane_vr, False)


def shoot():
    carrier = None
    for ob in bpy.data.objects:
        if ob.type == 'MESH' and len(ob.xp8_vr_manips):
            carrier = ob
            break
    if carrier is None:
        log("no mesh carries a manipulator - nothing to show")
        bpy.ops.wm.quit_blender()
        return
    log("carrier: %s (%d row(s))" % (carrier.name, len(carrier.xp8_vr_manips)))
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    carrier.select_set(True)
    bpy.context.view_layer.objects.active = carrier

    open_the_panel()

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'PROPERTIES':
                continue
            for space in area.spaces:
                if space.type == 'PROPERTIES':
                    space.context = 'OBJECT'
    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)

    # the X-Plane panel above ours is long: scroll the sidebar to its end so the
    # shot shows ours with the end of X-Plane's body as its neighbour
    scrolled = 0
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'PROPERTIES':
                continue
            for region in area.regions:
                if region.type != 'WINDOW':
                    continue
                try:
                    with bpy.context.temp_override(window=window, area=area,
                                                   region=region):
                        for _ in range(60):
                            bpy.ops.view2d.scroll_down()
                            scrolled += 1
                except Exception as exc:                    # noqa: BLE001
                    log("scroll: %s" % exc)
    log("scrolled %d step(s)" % scrolled)
    bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=2)
    bpy.ops.screen.screenshot(filepath=OUT)
    log("screenshot: %s" % OUT)
    bpy.ops.wm.quit_blender()


def watch():
    count = len(bpy.data.collections)
    if count == state["seen"]:
        state["same"] += 1
    else:
        state["same"] = 0
        state["seen"] = count
    elapsed = time.time() - state["start"]
    if state["same"] >= 3 and elapsed > 3:
        log("settled: %d collections, %d objects" % (count, len(bpy.data.objects)))
        shoot()
        return None
    if elapsed > 240:
        log("gave up waiting")
        bpy.ops.wm.quit_blender()
        return None
    return 1.0


def start():
    state["start"] = time.time()
    log("importing %s" % ACF)
    bpy.ops.import_scene.xplane_acf(filepath=ACF, place_by_acf=True,
                                    weld_vertices=False)
    bpy.app.timers.register(watch, first_interval=1.0)
    return None


bpy.app.timers.register(start, first_interval=1.0)
