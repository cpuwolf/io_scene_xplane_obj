"""Does a panel (ATTR_cockpit) batch inherit a blend state from an earlier batch?

    Blender -b --factory-startup --python verify/blend_panel_leak.py

Three cases, each a fresh scene built by hand, exported with the real operator:

  P1  panel batch first                      -> blender starts blending ON
  P2  ATTR_no_blend batch, then the panel    -> what state does the panel see?
  P3  P2 + an ATTR_blend custom attribute on the panel object (the workaround)
"""
import os
import sys

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


def quad(name, mat, x):
    me = bpy.data.meshes.new(name)
    me.from_pydata([(x, 0, 0), (x + 1, 0, 0), (x + 1, 0, 1), (x, 0, 1)], [],
                   [(0, 1, 2, 3)])
    me.update()
    ob = bpy.data.objects.new(name, me)
    ob.data.materials.append(mat)
    return ob


def build(tag, batches):
    """batches: list of (material fields dict, custom_attributes list)"""
    clean()
    coll = bpy.data.collections.new(tag)
    bpy.context.scene.collection.children.link(coll)
    coll.xplane.is_exportable_collection = True
    coll.xplane.layer.name = tag
    coll.xplane.layer.export_type = "cockpit"
    coll.xplane.layer.cockpit_panel_mode = "cockpit"    # ATTR_cockpit
    print("   layer panel mode:", coll.xplane.layer.cockpit_panel_mode,
          "export_type:", coll.xplane.layer.export_type, flush=True)
    for i, (fields, customs) in enumerate(batches):
        mat = bpy.data.materials.new("%s_mat%d" % (tag, i))
        mat.use_nodes = True
        for k, v in fields.items():
            setattr(mat.xplane, k, v)
        for cname in customs:
            attr = mat.xplane.customAttributes.add()
            attr.name = cname
            attr.value = ""
        ob = quad("%s_ob%d" % (tag, i), mat, i * 2)
        coll.objects.link(ob)
        print("   mat%d %s custom=%s" % (i, fields, customs), flush=True)


def export(tag):
    try:
        print("   operator:", bpy.ops.export.xplane_obj(
            filepath=os.path.join(OUT, tag + ".obj"), export_is_relative=False),
            flush=True)
    except Exception as exc:                                  # noqa: BLE001
        print("   operator raised:", exc, flush=True)
    path = os.path.join(OUT, tag + ".obj")
    if not os.path.exists(path):
        print("   !! no file", flush=True)
        return
    text = open(path, encoding="utf-8", errors="replace").read()
    keep = ("ATTR_cockpit", "ATTR_no_cockpit", "ATTR_blend", "ATTR_no_blend",
            "ATTR_shadow_blend", "TRIS", "GLOBAL_cockpit")
    print("   --- %s" % os.path.basename(path), flush=True)
    for line in text.splitlines():
        s = line.strip()
        if s.split()[:1] and s.split()[0] in keep:
            print("     |" + s, flush=True)


panelm = {"cockpit_feature": "panel"}
cutout = {"blend_v1000": "off", "blendRatio": 0.5}

print("=== P1: panel batch is first")
build("P1", [(panelm, [])])
export("P1")

print("\n=== P2: a no_blend cutout batch, then the panel batch")
build("P2", [(cutout, []), (panelm, [])])
export("P2")

print("\n=== P3: P2 + ATTR_blend as a custom attribute on the panel material")
build("P3", [(cutout, []), (panelm, ["ATTR_blend"])])
export("P3")
