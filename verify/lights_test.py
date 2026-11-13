"""The imported light object has to *look* like the lamp the line describes.

    <blender> -b --factory-startup --python verify/lights_test.py

X-Plane2Blender writes `LIGHT_PARAM <name> <x> <y> <z> <params verbatim>`, so the
Blender light's colour, power, cone and aim are preview-only: nothing here may
change a byte of the export.  Two halves, both required:

  * the objects carry colour / energy / cone / aim that match the parameters
    (a landing spill aimed -Z, a billboard that emits nothing, a cockpit flood
    in its warm tint);
  * the export text still holds the same LIGHT_PARAM lines, at the same places,
    with no ANIM_rotate invented - XP2B adds a static ANIM_rotate when the
    artist's Blender aim disagrees with the DX/DY/DZ in the parameters, so this
    is exactly the check that catches an aim that leaked into the file.

lights.txt has to be parsed by hand: in a `-b` run XP2B's load_handler never
fires, every param name looks unknown, and the autocorrect path above never
runs - the test would pass while grading nothing.
"""
import math
import os
import sys

import bpy
import addon_utils
from mathutils import Vector

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

addon_utils.enable("io_xplane2blender", default_set=True)
import io_scene_xplane_obj as IMP                           # noqa: E402

try:
    IMP.register()
except Exception:                                           # noqa: BLE001
    pass

from io_xplane2blender.xplane_helpers import logger          # noqa: E402
from io_xplane2blender.xplane_types import xplane_file as XF  # noqa: E402
from io_xplane2blender.xplane_utils import xplane_lights_txt_parser as LP  # noqa: E402

LP.parse_lights_file()
logger.addTransport(lambda t, m, c=None: print("XP2B %s: %s" % (t.upper(), m)),
                    ["error", "warning"])

FIXTURE = os.path.join(REPO, "verify", "fixtures", "lights.obj")
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg)
    return cond


def near(a, b, tol=2e-3):
    return abs(a - b) <= tol


for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)
for mat in list(bpy.data.materials):
    bpy.data.materials.remove(mat)

bpy.ops.import_scene.xplane_obj(filepath=FIXTURE)

lights = {ob.name.split(".")[0]: ob for ob in bpy.data.objects if ob.type == 'LIGHT'}
print("lights: %s" % ", ".join(sorted(lights)))

# --- the pairs imported -------------------------------------------------------
# name, family, expected blender type, colour, cone (None = omni), aim
EXPECT = [
    ("airplane_landing_pm", 'SPOT', (1.0, 0.95, 0.9), 0.97629601,
     (-0.0868, -0.0872, -0.9924)),
    ("airplane_landing_bb", 'POINT', (1.0, 1.0, 1.0), None,
     (-0.0868, -0.0872, -0.9924)),
    ("airplane_nav_pm", 'POINT', (1.0, 0.0, 0.0), None, None),
    # a component billboard's three direction slots are def literals
    # (ZERO ZERO_ NEG_ONE), not the lamp's direction: it is not aimed
    ("airplane_taxi_core", 'POINT', (1.0, 1.0, 1.0), None, None),
    ("airplane_taxi_sp", 'SPOT', (1.0, 1.0, 1.0), 0.90630779,
     (0.0, 0.0, -1.0)),
    ("airplane_panel_sp", 'SPOT', (1.0, 0.88, 0.57), 0.17364822,
     (0.1736, -0.9848, 0.0)),
    ("airplane_beacon_rotate", 'POINT', (1.0, 0.08, 0.03), None, None),
    ("full_custom_halo", 'SPOT', (1.0, 0.91, 0.79), 0.81915205,
     (-0.0885, -0.9813, -0.171)),
]

for name, kind, colour, width, aim in EXPECT:
    ob = lights.get(name)
    if not check(ob is not None, "no light object named %s" % name):
        continue
    data = ob.data
    check(data.type == kind, "%s: type is %s, want %s" % (name, data.type, kind))
    for channel, want in zip("RGB", colour):
        got = getattr(data, "color")[("RGB".index(channel))]
        check(near(got, want, 0.02),
              "%s: colour %s is %.3f, want %.3f" % (name, channel, got, want))
    if width is not None:
        want = 2.0 * math.acos(width)          # radians, as Blender stores it
        got = getattr(data, "spot_size", None)
        check(got is not None and near(got, want, 0.5),
              "%s: cone is %s rad, want %.3f" % (name, got, want))
    if aim is not None:
        v = Vector(aim).normalized()
        want = Vector((v.x, -v.z, v.y))          # vec_x_to_b
        got = (ob.matrix_world.to_3x3() @ Vector((0.0, 0.0, -1.0))).normalized()
        check(near(got.dot(want), 1.0, 1e-3),
              "%s: aim is %s, want %s" % (name, tuple(round(c, 3) for c in got),
                                          tuple(round(c, 3) for c in want)))

# billboards emit nothing (they are glow quads) and real spills do
for name in ("airplane_landing_bb", "airplane_taxi_core",
             "airplane_beacon_rotate"):
    ob = lights.get(name)
    if check(ob is not None, "no light object named %s" % name):
        check(ob.data.energy == 0.0, "%s: billboard emits %.3f W"
              % (name, ob.data.energy))
for name in ("airplane_landing_pm", "airplane_taxi_sp", "airplane_panel_sp"):
    ob = lights.get(name)
    if check(ob is not None, "no light object named %s" % name):
        check(ob.data.energy > 0.0, "%s: spill emits %.3f W"
              % (name, ob.data.energy))

# names with no table keep Blender's defaults and are reported, not guessed
odd = lights.get("airplane_made_up_lamp")
if check(odd is not None, "unknown name was dropped instead of imported"):
    check(odd.data.energy == 10.0, "unknown name got energy %.3f, want the "
          "Blender default 10.0" % odd.data.energy)
    check(odd.data.type == 'POINT', "unknown name got type %s" % odd.data.type)
named = lights.get("ship_mast_powered")
check(named is not None, "LIGHT_NAMED was dropped")

# --- the export must not learn anything from this ----------------------------
logger.clearMessages()
files = XF.createFilesFromBlenderRootObjects(bpy.context.scene, bpy.context.view_layer)
text = files[0].write() if files else ""
for m in logger.messages:
    print("XP2B %s: %s" % (m["type"].upper(), m["message"]))
check(bool(text), "the export came back empty (a validation error cancelled it)")

source = [l.split() for l in open(FIXTURE).read().splitlines()
          if l.strip().startswith(("LIGHT_PARAM", "LIGHT_NAMED"))]
written = [l.split() for l in text.splitlines()
           if l.strip().startswith(("LIGHT_PARAM", "LIGHT_NAMED"))]
check(len(written) == len(source),
      "export has %d light lines, the file has %d" % (len(written), len(source)))
check(not [l for l in text.splitlines() if "ANIM_rotate" in l],
      "the export invented an ANIM_rotate: the aim disagrees with DX/DY/DZ")

by_name = {}
for words in written:
    by_name.setdefault(words[1], []).append(words)
for words in source:
    want = by_name.get(words[1], [])
    if not check(want, "%s missing from the export" % words[1]):
        continue
    got = want.pop(0)
    for i, (a, b) in enumerate(zip(words[2:5], got[2:5])):
        check(near(float(a), float(b), 1e-4),
              "%s: position %d moved %s -> %s" % (words[1], i, a, b))
    if words[0] == "LIGHT_PARAM":
        check(len(got) == len(words),
              "%s: %d parameters written, %d in the file"
              % (words[1], len(got), len(words)))

print("\nRESULT: %s" % ("PASS" if not fails else "FAIL (%d)" % len(fails)))
for f in fails:
    print(" - %s" % f)
