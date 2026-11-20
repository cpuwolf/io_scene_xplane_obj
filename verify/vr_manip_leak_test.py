"""Does the `vr_manips` tag on a mesh leak into the exported .obj?

    <blender> -b --factory-startup --python verify/vr_manip_leak_test.py -- <file.obj>

The VR import tags every mesh that a vrconfig manipulator block points at with a
`vr_manips` custom property.  XPlane2Blender writes `ATTR_manip_*` from its own
RNA plus `customAttributes`, so a plain custom property should be none of its
business - this proves it by exporting the same file twice and diffing the text.
"""
import os
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

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SRC = argv[0] if argv else os.path.join(
    os.path.expanduser("~/X-Plane 12/Aircraft"),
    "King Air C90B EVO G1000 V1.3.1", "objects", "C90B_cockpit", "C90B_pilot.obj")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg, flush=True)
    else:
        print("   ok   %s" % msg, flush=True)
    return cond


def run(tag, tag_it):
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    bpy.ops.import_scene.xplane_obj(filepath=SRC)
    if tag_it:
        names = []
        for ob in bpy.data.objects:
            if ob.type == 'MESH':
                ob[XVR.PROP_OBJ_MANIPS] = ["command_knob sim/test"]
                names.append(ob.name)
        print("   tagged %d mesh(es)" % len(names), flush=True)
    out = os.path.join(tempfile.mkdtemp(prefix="vrfx_"), tag)
    bpy.ops.export.xplane_obj(filepath=out + ".obj")
    written = [os.path.join(os.path.dirname(out), n)
               for n in sorted(os.listdir(os.path.dirname(out)))
               if n.lower().endswith(".obj")]
    check(bool(written), "the %s run exported" % tag)
    return open(written[0], "rb").read() if written else b""


plain = run("plain", False)
tagged = run("tagged", True)
check(plain == tagged, "a vr_manips tag does not change the exported .obj "
      "(%d vs %d bytes)" % (len(plain), len(tagged)))

print("\nRESULT: %s" % ("FAIL %d" % len(fails) if fails else "PASS"), flush=True)
sys.exit(1 if fails else 0)
