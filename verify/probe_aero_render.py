"""Render the aero airframe over the imported model, to see that it sits right.

    <blender> -b --factory-startup --python verify/probe_aero_render.py -- <side|top|front> <out.png>

Workbench, object colours: the aero bodies and wings are purple, everything the
.acf attaches is grey, so a mis-scaled or mis-rotated grid shows up immediately.
"""
import math
import os
import sys

import addon_utils
import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
XM_PKG = "bl_ext.user_default.io_scene_xplane_obj"
if XM_PKG not in bpy.context.preferences.addons:
    addon_utils.enable(XM_PKG, default_set=False)
NAME = os.path.basename(REPO)
addon = __import__(NAME)
XB = __import__(NAME + ".acfaero", fromlist=["acfaero"])

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
VIEW = argv[0] if argv else "side"
OUT = argv[1] if len(argv) > 1 else "/tmp/aero_%s.png" % VIEW
ACF = os.path.expanduser(
    "~/X-Plane 12/Aircraft/Laminar Research/Cessna 172 SP/Cessna_172SP.acf")

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)

bpy.ops.import_scene.xplane_acf(filepath=ACF)

scene = bpy.context.scene
scene.render.engine = 'BLENDER_WORKBENCH'
scene.display.shading.light = 'STUDIO'
scene.display.shading.color_type = 'OBJECT'
scene.render.resolution_x = 1600
scene.render.resolution_y = 900

lo = [1e9] * 3
hi = [-1e9] * 3
for ob in bpy.data.objects:
    if ob.type != 'MESH' or ob.get(XB.PROP):
        continue
    for v in ob.data.vertices:
        p = ob.matrix_world @ v.co
        for i in range(3):
            lo[i] = min(lo[i], p[i])
            hi[i] = max(hi[i], p[i])
centre = [(lo[i] + hi[i]) / 2 for i in range(3)]
size = max(hi[i] - lo[i] for i in range(3))

cam_data = bpy.data.cameras.new("cam")
cam_data.type = 'ORTHO'
cam_data.ortho_scale = size * 1.15
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
dist = size * 3
if VIEW == "side":            # from +X: nose to the left
    cam.location = (centre[0] + dist, centre[1], centre[2])
    cam.rotation_euler = (math.radians(90), 0.0, math.radians(90))
elif VIEW == "top":           # from +Z, nose up the page
    cam.location = (centre[0], centre[1], centre[2] + dist)
    cam.rotation_euler = (0.0, 0.0, 0.0)
else:                         # front, from +Y (ahead of the nose)
    cam.location = (centre[0], centre[1] + dist, centre[2])
    cam.rotation_euler = (math.radians(90), 0.0, math.radians(180))
scene.camera = cam
scene.render.filepath = OUT
bpy.ops.render.render(write_still=True)
print("rendered", OUT, "view", VIEW)
