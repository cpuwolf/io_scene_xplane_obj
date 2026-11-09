"""ATTR_draw_disable must reach Blender as a transparent material.

    <blender> -b --factory-startup --python verify/nodraw_test.py

X-Plane's "Draw objects with this material" is OBJ8's ATTR_draw_disable.  The
conversion already copies the material and clears xplane.draw for it; this
checks the copy also ends up at alpha 0 (with the texture's alpha link cut, or
the socket value would be ignored), while the sibling that draws normally keeps
alpha 1.
"""
import json
import os
import sys

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

FIX = os.path.join(REPO, "verify", "fixtures")
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg)
    return cond


def material_rows():
    rows = []
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        bsdf = next((n for n in mat.node_tree.nodes
                     if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
        if bsdf is None:
            continue
        rows.append({
            "name": mat.name,
            "alpha": round(bsdf.inputs['Alpha'].default_value, 4),
            "alpha_linked": any(l.to_node == bsdf and l.to_socket.name == 'Alpha'
                                for l in mat.node_tree.links),
            "render_method": getattr(mat, "surface_render_method", "?"),
            "diffuse_alpha": round(mat.diffuse_color[3], 4),
            "xp2b_draw": getattr(getattr(mat, "xplane", None), "draw", None),
        })
    return rows


def run(fname, expect_nodraw):
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for me in list(bpy.data.meshes):
        bpy.data.meshes.remove(me)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)

    path = os.path.join(FIX, fname)
    try:
        r = bpy.ops.import_scene.xplane_obj(filepath=path)
        print("   import:", sorted(r))
    except RuntimeError as exc:
        print("   import reported: %s" % str(exc)[:100])

    rows = material_rows()
    print("CASE " + json.dumps({"file": fname, "materials": rows}))

    nodraw = [m for m in rows if m["name"].endswith("_nodraw")]
    normal = [m for m in rows if not m["name"].endswith("_nodraw")]

    if expect_nodraw:
        check(len(nodraw) == 1,
              "%s: expected one *_nodraw material, got %d"
              % (fname, len(nodraw)))
        for m in nodraw:
            check(m["alpha"] == 0.0,
                  "%s: nodraw alpha is %s, expected 0" % (m["name"], m["alpha"]))
            check(not m["alpha_linked"],
                  "%s: alpha still driven by the texture" % m["name"])
            check(m["render_method"] == 'BLENDED',
                  "%s: render method %s" % (m["name"], m["render_method"]))
            check(m["diffuse_alpha"] == 0.0,
                  "%s: viewport colour alpha %s" % (m["name"], m["diffuse_alpha"]))
            check(m["xp2b_draw"] is False,
                  "%s: xplane.draw is %s, expected False"
                  % (m["name"], m["xp2b_draw"]))
        check(len(normal) >= 1,
              "%s: the drawing quad lost its own material" % fname)
        for m in normal:
            check(m["alpha"] > 0.5,
                  "%s: normal sibling alpha %s, expected opaque"
                  % (m["name"], m["alpha"]))
            check(m["xp2b_draw"] is not False,
                  "%s: normal sibling has draw=False" % m["name"])
    else:
        for m in rows:
            check(m["alpha"] > 0.5,
                  "%s: control alpha %s" % (m["name"], m["alpha"]))


run("nodraw_attr.obj", expect_nodraw=True)
run("mini.obj", expect_nodraw=False)

print("\nRESULT: %s" % ("PASS" if not fails else "FAIL (%d)" % len(fails)))
