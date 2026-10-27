"""Visual scale check: the imported aircraft with a wireframe cage the size of a
real King Air C90B (15.32 x 10.92 x 4.37 m) around it."""
import math
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
import bpy                                                    # noqa
from mathutils import Vector                                  # noqa

addon = __import__(os.path.basename(REPO))
addon.register()
XM = addon.XM

SAMPLES = ("/Users/rourou/X-Plane 12/Aircraft/King Air C90B EVO G1000 V1.3.1"
           "/objects/C90B_exterior")
OUT = "/tmp/xpobj_scale"
os.makedirs(OUT, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.xplane_obj(filepath=os.path.join(SAMPLES,
                                                     "C90B_fuselage.obj"),
                                import_folder=True)

meshes = [o for o in bpy.data.objects if o.type == 'MESH' and XM.is_batch(o)]
lo = Vector((1e9, 1e9, 1e9))
hi = Vector((-1e9, -1e9, -1e9))
for ob in meshes:
    for c in ob.bound_box:
        w = ob.matrix_world @ Vector(c)
        for i in range(3):
            lo[i] = min(lo[i], w[i])
            hi[i] = max(hi[i], w[i])
size = hi - lo
print("model  X %.3f  Y %.3f  Z %.3f m" % (size.x, size.y, size.z))

# reference cage: the real aircraft's span / length / height, sat on the model's
# lowest point so the vertical comparison is meaningful
REAL = (15.32, 10.92, 4.37)
centre = (lo + hi) / 2.0
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(centre.x, centre.y,
                                                   lo.z + REAL[2] / 2.0))
cage = bpy.context.active_object
cage.name = "REAL_C90B_15.32x10.92x4.37m"
cage.scale = REAL
cage.data.materials.append(bpy.data.materials.new("cage"))
cage.data.materials[0].diffuse_color = (1.0, 0.15, 0.1, 1.0)
mod = cage.modifiers.new("wire", 'WIREFRAME')
mod.thickness = 0.03
mod.use_replace = True

# a 1 m cube next to it, for eyeballing the unit
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(lo.x - 2.0, hi.y + 2.0,
                                                    lo.z + 0.5))
one = bpy.context.active_object
one.name = "ONE_METRE"
one.data.materials.append(bpy.data.materials.new("one"))
one.data.materials[0].diffuse_color = (0.2, 1.0, 0.2, 1.0)

scene = bpy.context.scene
scene.render.engine = 'BLENDER_WORKBENCH'
scene.display.shading.light = 'STUDIO'
scene.display.shading.color_type = 'TEXTURE'      # show the imported textures
scene.display.shading.show_object_outline = True
scene.render.resolution_x = 1400
scene.render.resolution_y = 900
scene.world = bpy.data.worlds.new("W")
scene.world.color = (0.08, 0.09, 0.11)

cam_data = bpy.data.cameras.new("cam")
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam

radius = max(size) * 1.8


def shoot(name, direction):
    """Place the camera along `direction` from the centre, looking back at it."""
    cam.location = centre + direction.normalized() * radius
    cam.rotation_euler = (-direction).to_track_quat('-Z', 'Y').to_euler()
    if cam_data.type == 'ORTHO':
        cam_data.ortho_scale = max(size) * 1.25
    scene.render.filepath = os.path.join(OUT, name + ".png")
    bpy.ops.render.render(write_still=True)
    print("wrote", scene.render.filepath)


cam_data.type = 'ORTHO'
shoot("top", Vector((0, 0, 1)))
cam_data.type = 'PERSP'
cam_data.lens = 60
radius = max(size) * 2.4
shoot("three_quarter", Vector((0.75, 0.55, 0.45)))
shoot("side", Vector((1, 0, 0.02)))
