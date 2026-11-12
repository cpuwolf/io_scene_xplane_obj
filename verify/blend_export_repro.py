"""Reproduce: UI says "Blend Mode: Off", exported .obj has no ATTR_no_blend?

Builds plain Blender scenes (nothing imported from OBJ8), sets material.xplane
exactly like the N-panel does, runs the real export operator, and prints the
blend lines that came out.

    Blender -b --factory-startup --python verify/blend_export_repro.py
"""
import os
import sys
import traceback

import bpy
import addon_utils

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

addon_utils.enable("io_xplane2blender", default_set=True)
import io_scene_xplane_obj as IMP                            # noqa: E402

try:
    IMP.register()
except Exception:                                            # noqa: BLE001
    pass

from io_xplane2blender.xplane_helpers import logger as xlog   # noqa: E402
xlog.addTransport(lambda t, m, c=None: print("XP2B %s: %s" % (t.upper(), m), flush=True),
                  ["error", "warning"])

import io_xplane2blender as _XP                              # noqa: E402
print("XP2B module:", os.path.dirname(_XP.__file__), flush=True)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp")
os.makedirs(OUT, exist_ok=True)


def clean():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for me in list(bpy.data.meshes):
        bpy.data.meshes.remove(me)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)


def make_quad(name, mat):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)], [], [(0, 1, 2, 3)])
    me.update()
    ob = bpy.data.objects.new(name, me)
    ob.data.materials.append(mat)
    return ob


def build(name, mats, version="1220"):
    """mats: list of dicts of material.xplane field -> value; one mesh per mat"""
    clean()
    scn = bpy.context.scene
    scn.xplane.version = version

    coll = bpy.data.collections.new(name)
    scn.collection.children.link(coll)
    coll.xplane.is_exportable_collection = True
    coll.xplane.layer.name = name
    coll.xplane.layer.export_type = "aircraft"

    for i, fields in enumerate(mats):
        mat = bpy.data.materials.new("%s_mat%d" % (name, i))
        mat.use_nodes = True
        for k, v in fields.items():
            setattr(mat.xplane, k, v)
        ob = make_quad("%s_ob%d" % (name, i), mat)
        coll.objects.link(ob)
        print("   %-18s %s" % (mat.name,
                               {k: getattr(mat.xplane, k) for k in fields}),
              flush=True)
    return coll


def export(tag):
    path = os.path.join(OUT, tag + ".obj")
    print("--- export %s (scene version %s)" % (tag, bpy.context.scene.xplane.version),
          flush=True)
    try:
        r = bpy.ops.export.xplane_obj(filepath=path, export_is_relative=False)
        print("   operator:", r, flush=True)
    except Exception:                                        # noqa: BLE001
        print("   operator raised:\n%s" % traceback.format_exc(), flush=True)
    log = bpy.data.texts.get("XPlane2Blender.log")
    if log is not None and len(log.as_string().strip()):
        print("   LOG:\n" + "".join("     " + l + "\n"
                                    for l in log.as_string().strip().splitlines()[-8:]),
              flush=True)
    if not os.path.exists(path):
        print("   !! no file written", flush=True)
        return
    text = open(path, encoding="utf-8", errors="replace").read()
    lines = [l.strip() for l in text.splitlines()]
    blend = [l for l in lines if l.split()[:1] and "blend" in l.split()[0]]
    print("   %d bytes, blend lines: %s" % (len(text), blend or "NONE"), flush=True)


print("=== A: default scene version, Blend Mode = Off")
build("A", [{"blend_v1000": "off"}])
export("A_off")

print("\n=== B: Blend Mode = Off, ratio 0.25")
build("B", [{"blend_v1000": "off", "blendRatio": 0.25}])
export("B_off_ratio")

print("\n=== C: Blend Mode = Shadow, ratio 0.75")
build("C", [{"blend_v1000": "shadow", "blendRatio": 0.75}])
export("C_shadow")

print("\n=== D: Blend Mode = On (control)")
build("D", [{"blend_v1000": "on"}])
export("D_on")

print("\n=== E: one object, slot0 = default material, slot1 = Blend Mode Off")
build("E", [{"blend_v1000": "on"}])
mat = bpy.data.materials.new("E_mat_off")
mat.use_nodes = True
mat.xplane.blend_v1000 = "off"
ob = bpy.context.scene.collection.children["E"].objects["E_ob0"]
# replace the object's material with two slots
ob.data.materials.clear()
ob.data.materials.append(bpy.data.materials["E_mat0"])
ob.data.materials.append(mat)
# put half the faces on slot 1
ob.data.polygons[0].material_index = 1
print("   slots:", [(s.material.name, s.material.xplane.blend_v1000)
                    for s in ob.material_slots], flush=True)
export("E_second_slot")

print("\n=== F: legacy scene (X-Plane 9.x) with 'Use Alpha Cutoff' = True")
build("F", [{"blend": True, "blendRatio": 0.4}], version="900")
export("F_legacy900")

print("\n=== G: material with a cockpit feature set (Panel Texture)")
build("G", [{"blend_v1000": "off", "cockpit_feature": "panel"}])
export("G_cockpit_panel")

print("\n=== H: draw off + blend off")
build("H", [{"blend_v1000": "off", "draw": False}])
export("H_nodraw")

print("\n=== I: two quads on one object, slot0 on / slot1 off")
clean()
scn = bpy.context.scene
coll = bpy.data.collections.new("I")
scn.collection.children.link(coll)
coll.xplane.is_exportable_collection = True
coll.xplane.layer.name = "I"
coll.xplane.layer.export_type = "aircraft"
mat0 = bpy.data.materials.new("I_on")
mat0.use_nodes = True
mat0.xplane.blend_v1000 = "on"
mat1 = bpy.data.materials.new("I_off")
mat1.use_nodes = True
mat1.xplane.blend_v1000 = "off"
me = bpy.data.meshes.new("I_mesh")
me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1),
                (3, 0, 0), (4, 0, 0), (4, 0, 1), (3, 0, 1)], [],
               [(0, 1, 2, 3), (4, 5, 6, 7)])
me.update()
ob = bpy.data.objects.new("I_ob", me)
ob.data.materials.append(mat0)
ob.data.materials.append(mat1)
ob.data.polygons[1].material_index = 1
coll.objects.link(ob)
print("   slots:", [(s.material.name, s.material.xplane.blend_v1000)
                    for s in ob.material_slots], flush=True)
export("I_two_slots")
