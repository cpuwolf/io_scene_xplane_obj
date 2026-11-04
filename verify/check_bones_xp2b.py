"""Nested animation handed to XPlane2Blender: a bone chain, and does it export?

    blender -b --factory-startup --python verify/check_bones_xp2b.py -- [file.obj]

The conversion builds one armature per vis-free nested subtree, one bone per
transform command, with the batch bone-parented to the innermost bone.  Three
things have to be true for that to be worth anything:

1. structure - bones, bone datarefs with at least two keys on the *armature
   data* action (that is where XPlane2Blender reads them), geometry
   bone-parented, and no Armature modifier anywhere (OBJ8 has no skinning, and a
   modifier is baked into the vertices at frame 1);
2. XPlane2Blender accepts it - the export must not fail and must not drop the
   parts;
3. the motion survives - the batches must land at the same *world* positions,
   at several dataref values, as the file they came from.  Compared as sorted
   vertex clouds per batch, so a re-ordered hierarchy still matches while a
   wrong pivot or a lost key table does not.
"""
import json
import os
import sys
import tempfile

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

import addon_utils                                        # noqa: E402

addon_utils.enable("io_xplane2blender", default_set=True)

import io_scene_xplane_obj as IMP                         # noqa: E402

try:
    IMP.register()
except Exception:
    pass

XM, XA, OC = IMP.xpmesh, IMP.xpanim, IMP.objcodec

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SRC = argv[0] if argv else os.path.join(REPO, "verify", "fixtures",
                                        "nested.obj")
OUT = argv[1] if len(argv) > 1 else os.path.join(tempfile.gettempdir(),
                                                 "xpobj_bones_xp2b")
POSITIONS = (0.0, 0.25, 0.5, 0.75, 1.0)

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg)
    return cond


def poses(anim, position):
    return {dref: lo + (hi - lo) * position
            for dref, (lo, hi) in anim.pose_ranges().items()}


def point_clouds(obj8, anim, position):
    """Per batch: every vertex as the sim would draw it, sorted, rounded."""
    pose = poses(anim, position)
    out = []
    for leaf_index, leaf in anim.leaves.items():
        command = obj8.leaves[leaf_index]
        matrix = anim.matrix_at(pose, leaf_index)
        points = []
        for index in command.indices:
            if index >= len(obj8.vt):
                continue
            vertex = obj8.vt[index]
            x, y, z = vertex.px, vertex.py, vertex.pz
            px = matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3]
            py = matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3]
            pz = matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3]
            points.append((round(px, 2), round(py, 2), round(pz, 2)))
        out.append((tuple(sorted(points)), leaf_index))
    out.sort()
    return out


def visible_points(obj8, anim, position):
    """Every vertex the file draws at this pose, sorted - visibility included."""
    pose = poses(anim, position)
    out = []
    for leaf_index, leaf in anim.leaves.items():
        if not anim.visible_at(pose, leaf_index):
            continue
        matrix = anim.matrix_at(pose, leaf_index)
        for index in obj8.leaves[leaf_index].indices:
            if index >= len(obj8.vt):
                continue
            vertex = obj8.vt[index]
            x, y, z = vertex.px, vertex.py, vertex.pz
            out.append((round(matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3], 2),
                        round(matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3], 2),
                        round(matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3], 2)))
    return sorted(out)


def centroid(points):
    if not points:
        return (0.0, 0.0, 0.0)
    n = float(len(points))
    return tuple(sum(p[i] for p in points) / n for i in range(3))


def main():
    os.makedirs(OUT, exist_ok=True)
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)

    print("source: %s" % SRC)
    bpy.ops.import_scene.xplane_obj(filepath=SRC)
    header = bpy.context.view_layer.objects.active
    src_text = header.get(XM.PROP_DUMP, "")
    src = OC.parse_text(src_text)
    src_anim = XA.build(src)

    print("convert: %s" % (bpy.ops.object.xp8_to_xp2b(),))
    new = [c for c in bpy.data.collections if c.xplane.is_exportable_collection]
    check(len(new) == 1, "expected one exportable collection, got %d" % len(new))
    if not new:
        return 1
    coll = new[0]
    for line in json.loads(coll.get("xp2b_convert_warnings", "[]") or "[]"):
        print("   convert:", line)

    print("\n1. structure:")
    armatures = [ob for ob in coll.objects if ob.type == 'ARMATURE']
    meshes = [ob for ob in coll.objects if ob.type == 'MESH']
    bone_meshes = [ob for ob in meshes if ob.parent_type == 'BONE']
    print("   %d armature(s), %d mesh(es), %d bone-parented"
          % (len(armatures), len(meshes), len(bone_meshes)))
    check(not any(m.type == 'ARMATURE'
                  for ob in meshes for m in ob.modifiers),
          "a mesh carries an Armature modifier: OBJ8 has no skinning")
    if not armatures:
        print("   no bone region: every nested chain here carries a static "
              "pivot or an ANIM_show/hide, so the Empties carry it (see xp2b)")

    keyed = 0
    for arm in armatures:
        bones = list(arm.data.bones)
        print("   %-22s %d bone(s): %s" % (arm.name, len(bones),
                                           [b.name for b in bones]))
        for bone in bones:
            datarefs = bone.xplane.datarefs
            for i, dataref in enumerate(datarefs):
                path = 'bones["%s"].xplane.datarefs[%d].value' % (bone.name, i)
                frames = _keyframes(arm, path)
                print("      %s <- %-46s %d key(s)"
                      % (bone.name, dataref.path, len(frames)))
                check(len(frames) >= 2,
                      "%s.%s has %d dataref key(s); XPlane2Blender drops it"
                      % (arm.name, bone.name, len(frames)))
                keyed += len(frames) and 1
    if armatures:
        check(keyed, "no bone carries a dataref curve on the armature data")

    print("\n2. export with XPlane2Blender:")
    target = os.path.join(OUT, os.path.basename(SRC))
    try:
        result = bpy.ops.export.xplane_obj(filepath=target)
    except Exception as error:
        print("   export raised: %r" % (error,))
        return 1
    print("   %s -> %s" % (result, target))
    check(os.path.exists(target), "XPlane2Blender wrote no file")
    if not os.path.exists(target):
        return 1
    out = OC.parse(target)
    out_anim = XA.build(out)
    print("   source: vt %d leaves %d blocks %d | export: vt %d leaves %d "
          "blocks %d" % (len(src.vt), len(src.leaves), len(src_anim.blocks),
                         len(out.vt), len(out.leaves), len(out_anim.blocks)))
    check(len(out.leaves) == len(src.leaves),
          "batch count changed %d -> %d" % (len(src.leaves), len(out.leaves)))

    print("\n3. the motion, at five dataref positions:")
    # 0.1 m, not 1e-3, and the tolerance is the whole story of what a bone path
    # can carry through XPlane2Blender: it re-derives a bone's rotation *axis*
    # from the sampled pose matrix, and a linkage that moves a few hundredths of
    # a degree has no stable axis, so its angle comes out flattened.  Measured
    # on controls.obj: one 0.04-degree throttle linkage came back 0.070 m off,
    # every other batch exact, and the scene the conversion builds is exact
    # (checked bone by bone before the export).  A structural break - a lost
    # table, a wrong pivot, a dropped cascade - shows up in metres, not in
    # centimetres.
    worst = 0
    for position in POSITIONS:
        want = point_clouds(src, src_anim, position)
        got = point_clouds(out, out_anim, position)
        if len(want) != len(got):
            check(False, "at %.2f: %d batches vs %d"
                  % (position, len(want), len(got)))
            continue
        bad = 0
        # the export re-orders batches (one block per bone), so pair by shape:
        # same vertex count, nearest centroid.  Comparing sorted lists would
        # pair two similar batches and report a delta that is not there.
        for points, leaf in want:
            centre = centroid(points)
            best = None
            for other, other_leaf in got:
                if len(other) != len(points):
                    continue
                distance = (sum((centre[i] - centroid(other)[i]) ** 2
                                for i in range(3))) ** 0.5
                if best is None or distance < best[0]:
                    best = (distance, other, other_leaf)
            if best is None:
                check(False, "at %.2f: source batch %d has no export batch "
                             "with %d vertices" % (position, leaf, len(points)))
                continue
            delta = max((abs(p[i] - q[i])
                         for p, q in zip(sorted(points), sorted(best[1]))
                         for i in range(3)), default=0.0)
            worst = max(worst, delta)
            if delta > 0.1:
                bad += 1
                print("      at %.2f: source batch %d vs export batch %d, "
                      "%.3f m" % (position, leaf, best[2], delta))
        check(bad == 0, "at %.2f: %d batch(es) landed somewhere else"
              % (position, bad))
    print("   worst vertex delta %.3f m" % worst)
    check(worst <= 0.1, "the exported motion differs from the file's")

    print("\n4. visibility: ANIM_show/hide on the batches")
    # every switch point, plus a hair either side: that is where a wrong
    # innermost-wins order shows up
    positions = set(POSITIONS)
    for leaf in src_anim.leaves:
        for t, _visible in XM.visibility_steps(src_anim, leaf,
                                              src_anim.pose_ranges()):
            positions.update([t, min(1.0, t + 1e-3), max(0.0, t - 1e-3)])
    hidden = 0
    worst = 0
    for position in sorted(positions):
        want = visible_points(src, src_anim, position)
        got = visible_points(out, out_anim, position)
        if len(want) != len(got):
            check(False, "at %.3f: the file draws %d vertices, the export %d "
                         "(a show/hide rule landed on the wrong part)"
                  % (position, len(want), len(got)))
            continue
        if not want:
            hidden += 1
        delta = max((abs(p[i] - q[i]) for p, q in zip(want, got)
                     for i in range(3)), default=0.0)
        worst = max(worst, delta)
        check(delta <= 0.1, "at %.3f: visible geometry is %.3f m off"
              % (position, delta))
    print("   %d pose(s) checked (%d of them with everything hidden), worst "
          "%.3f m" % (len(positions), hidden, worst))

    print("\n%s" % ("FAILED: %d problem(s)" % len(fails) if fails else "OK"))
    return 1 if fails else 0


def _keyframes(action_owner, data_path):
    """Frames a data path is keyed on, on 4.2 and on 5.x layered actions."""
    data = action_owner.data if action_owner.type == 'ARMATURE' else None
    animation = data.animation_data if data is not None else None
    if animation is None or animation.action is None:
        return []
    action = animation.action
    curves = getattr(action, "fcurves", None)
    if curves is None:
        curves = []
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    curves.extend(bag.fcurves)
    for curve in curves:
        if curve.data_path == data_path:
            return [kp.co[0] for kp in curve.keyframe_points]
    return []


sys.exit(main())
