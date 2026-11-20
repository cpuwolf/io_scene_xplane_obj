"""Render a side view: do the aero bodies sit where the imported model sits?

    <blender> -b --factory-startup --python verify/probe_bodies_render.py -- out.png
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

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else "/tmp/bodies_side.png"
ACF = os.path.expanduser(
    "~/X-Plane 12/Aircraft/Laminar Research/Schleicher ASK 21/ASK21.acf")

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)

bpy.ops.import_scene.xplane_acf(filepath=ACF)

scene = bpy.context.scene
scene.render.engine = 'BLENDER_WORKBENCH'
scene.display.shading.light = 'STUDIO'
scene.display.shading.color_type = 'OBJECT'
scene.display.shading.show_xray = False
scene.render.resolution_x = 1600
scene.render.resolution_y = 900
scene.render.film_transparent = False
scene.world = scene.world or bpy.data.worlds.new("W")

lo = [1e9] * 3
hi = [-1e9] * 3
for ob in bpy.data.objects:
    if ob.type != 'MESH':
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
# from +X (starboard), looking at the aircraft: nose to the left, up is +Z
cam.location = (centre[0] + size * 3, centre[1], centre[2])
cam.rotation_euler = (math.radians(90), 0.0, math.radians(90))
scene.camera = cam

scene.render.filepath = OUT
bpy.ops.render.render(write_still=True)
print("rendered", OUT)
