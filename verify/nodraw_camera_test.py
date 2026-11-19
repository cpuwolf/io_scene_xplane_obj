"""No-draw and camera-collision batches must be told apart in the viewport.

    <blender> -b --factory-startup --python verify/nodraw_camera_test.py

Both switches keep geometry out of X-Plane's picture, and both used to be
painted the same way (fully transparent), which makes a collision proxy
indistinguishable from a shadow caster.  They now carry a colour:

    ATTR_draw_disable            -> green, alpha 0.5, roughness 0.3
    ATTR_solid_camera            -> red, alpha 0.1, roughness 0.3
    both                         -> red (camera collision is the specific one)

Roughness and the colour are only *viewport* state; the export still writes the
two switches from ``material.xplane``.  A batch that draws keeps the material
the importer built, untouched (alpha 1, its texture still wired).
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

GREEN = (0.0, 1.0, 0.0)
RED = (1.0, 0.0, 0.0)


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg)
    return cond


def near(a, b, tol=1e-3):
    return abs(a - b) <= tol


def material_rows():
    rows = []
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        nt = mat.node_tree
        bsdf = next((n for n in nt.nodes
                     if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
        if bsdf is None:
            continue
        xp = getattr(mat, "xplane", None)
        rows.append({
            "name": mat.name,
            "colour": tuple(round(v, 3) for v in bsdf.inputs['Base Color'].default_value)[:3],
            "base_linked": any(l.to_node == bsdf
                               and l.to_socket.name == 'Base Color'
                               for l in nt.links),
            "alpha": round(bsdf.inputs['Alpha'].default_value, 4),
            "alpha_linked": any(l.to_node == bsdf and l.to_socket.name == 'Alpha'
                                for l in nt.links),
            "roughness": round(bsdf.inputs['Roughness'].default_value, 4),
            "render_method": getattr(mat, "surface_render_method", "?"),
            "diffuse": tuple(round(v, 3) for v in mat.diffuse_color),
            "draw": None if xp is None else xp.draw,
            "camera": None if xp is None else xp.solid_camera,
        })
    return rows


for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for me in list(bpy.data.meshes):
    bpy.data.meshes.remove(me)
for mat in list(bpy.data.materials):
    bpy.data.materials.remove(mat)

try:
    r = bpy.ops.import_scene.xplane_obj(
        filepath=os.path.join(FIX, "nodraw_camera.obj"))
    print("   import:", sorted(r))
except RuntimeError as exc:
    print("   import reported: %s" % str(exc)[:120])

rows = material_rows()
print("ROWS " + json.dumps(rows, indent=1))

groups = {}
for row in rows:
    groups.setdefault((row["draw"], row["camera"]), []).append(row)

check(sorted(groups) == [(False, False), (False, True), (True, False), (True, True)],
      "expected one material per (draw, camera) pair, got %s" % (sorted(groups),))
for key, members in sorted(groups.items(), key=lambda kv: str(kv[0])):
    check(len(members) == 1,
          "state %s: %d materials share it (%s)"
          % (key, len(members), [m["name"] for m in members]))


def state(draw, camera):
    members = groups.get((draw, camera)) or []
    return members[0] if members else None


def expect(row, colour, alpha, label):
    if not check(row is not None, "%s: no material carries that state" % label):
        return
    check(row["colour"] == colour,
          "%s (%s): base colour %s, expected %s"
          % (label, row["name"], row["colour"], colour))
    check(not row["base_linked"],
          "%s (%s): the texture still drives the base colour" % (label, row["name"]))
    check(near(row["alpha"], alpha),
          "%s (%s): alpha %s, expected %s" % (label, row["name"], row["alpha"], alpha))
    check(not row["alpha_linked"],
          "%s (%s): the texture still drives alpha" % (label, row["name"]))
    check(near(row["roughness"], 0.3),
          "%s (%s): roughness %s, expected 0.3"
          % (label, row["name"], row["roughness"]))
    check(row["render_method"] == 'BLENDED',
          "%s (%s): render method %s, expected BLENDED"
          % (label, row["name"], row["render_method"]))
    check(near(row["diffuse"][3], alpha) and row["diffuse"][:3] == colour,
          "%s (%s): Solid-mode colour %s, expected %s + alpha %s"
          % (label, row["name"], row["diffuse"], colour, alpha))


nodraw = state(False, False)
expect(nodraw, GREEN, 0.5, "no draw")
if nodraw is not None:
    check(nodraw["draw"] is False,
          "%s: xplane.draw is %s" % (nodraw["name"], nodraw["draw"]))

camera = state(True, True)
expect(camera, RED, 0.1, "camera collision")
if camera is not None:
    check(camera["camera"] is True,
          "%s: xplane.solid_camera is %s" % (camera["name"], camera["camera"]))

both = state(False, True)
expect(both, RED, 0.1, "no draw + camera collision")
if both is not None:
    check(both["draw"] is False and both["camera"] is True,
          "%s: draw=%s camera=%s" % (both["name"], both["draw"], both["camera"]))

drawing = state(True, False)
if check(drawing is not None, "the drawing batch lost its material"):
    check(near(drawing["alpha"], 1.0),
          "%s: drawing batch alpha %s, expected opaque"
          % (drawing["name"], drawing["alpha"]))
    check(drawing["colour"] not in (GREEN, RED),
          "%s: drawing batch was painted %s" % (drawing["name"], drawing["colour"]))

print("\nRESULT: %s" % ("PASS" if not fails else "FAIL (%d)" % len(fails)))
