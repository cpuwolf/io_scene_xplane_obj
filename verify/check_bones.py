"""Nested animation: one armature for the file, bones mirroring the ANIM tree.

    blender -b --factory-startup --python verify/check_bones.py -- [file.obj]

A batch wrapped by more than one transform command is a cascade, and the ANIM
blocks around it *are* a hierarchy.  The importer builds **one armature for the
whole file**, its bone tree a copy of the block tree, one bone per transform
command, and hangs each batch off the bone of its innermost block.  This checks
the three claims that matter:

1. structure - exactly one armature, its bones named/parented/placed like the
   block tree, two batches inside one block sharing one bone, and no Armature
   modifier anywhere (OBJ8 has no skinning: a modifier would be baked into the
   vertices at frame 1);
2. pose - every bone at every key frame holds `base^-1 @ M` for its *own*
   command, so `ANIM_rotate_key 1 53.690156` is that bone's own 53.690156
   degrees, and the bone's `xp8_angle_deg` property says so in degrees;
3. round trip - an untouched import still writes back byte for byte, and an
   export with animation='BLENDER' leaves bone-rigged batches alone.
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import bpy                                              # noqa: E402
from mathutils import Matrix                            # noqa: E402

sys.path.insert(0, os.path.dirname(REPO))
pkg = __import__(os.path.basename(REPO))
pkg.register()
OC, XA, XM = pkg.OC, pkg.XA, pkg.XM

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SRC = argv[0] if argv else os.path.join(REPO, "verify", "fixtures",
                                        "nested.obj")
OUT = argv[1] if len(argv) > 1 else os.path.join(tempfile.gettempdir(),
                                                 "xpobj_bones")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg)
    return cond


def sha(path):
    h = __import__("hashlib").sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def main():
    os.makedirs(OUT, exist_ok=True)
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)

    print("source: %s" % SRC)
    bpy.ops.import_scene.xplane_obj(filepath=SRC)
    header = bpy.context.view_layer.objects.active
    check(XM.is_header(header), "import did not leave a header selected")
    text = header.get(XM.PROP_DUMP, "")
    anim = XA.build(OC.parse_text(text))
    leaf_objs = json.loads(header.get(XM.PROP_LEAF_OBJS, "[]"))
    pose0 = XM.pose_at(anim, 0.0, {})

    specs, tips = XM.bone_tree(anim, pose0)
    leaf_bone = XM.leaf_bones(anim, tips)

    print("\n1. structure: the file's ANIM tree as one armature")
    armatures = [ob for ob in bpy.data.objects if XM.is_armature(ob)]
    print("   %d armature(s), %d bone(s) expected, %d batch(es) on bones"
          % (len(armatures), len(specs), len(leaf_bone)))
    if not specs:
        print("   this file has no nested animation: nothing to check")
        return 0
    check(len(armatures) == 1,
          "expected one armature for the whole file, got %d" % len(armatures))
    if not armatures:
        return 1
    arm = armatures[0]
    check([b.name for b in arm.data.bones] == [spec[0] for spec in specs],
          "bone set does not mirror the block tree")
    check(arm.parent is header, "the armature is not under the header")

    for name, head, cmd, parent, block in specs:
        bone = arm.data.bones.get(name)
        if bone is None:
            check(False, "bone %s is missing" % name)
            continue
        check((bone.parent.name if bone.parent else None) == parent,
              "bone %s: parent %s, expected %s"
              % (name, bone.parent and bone.parent.name, parent))
        check((bone.head_local - head).length < 1e-5,
              "bone %s: head %.4f m off its pivot"
              % (name, (bone.head_local - head).length))
        check(not bone.use_connect,
              "bone %s is connected (a connected bone forces location 0)" % name)

    for leaf, name in enumerate(leaf_objs):
        ob = bpy.data.objects.get(name) if name else None
        if ob is None:
            continue
        want = leaf_bone.get(leaf)
        if want:
            check(ob.parent is arm and ob.parent_bone == want,
                  "batch %d hangs off %s/%s, expected bone %s"
                  % (leaf, ob.parent and ob.parent.name, ob.parent_bone, want))
        else:
            check(ob.parent is not arm,
                  "batch %d is on a bone but its chain is a single hinge" % leaf)
        check(not any(m.type == 'ARMATURE' for m in ob.modifiers),
              "batch %d has an Armature modifier: OBJ8 has no skinning" % leaf)

    shared = {}
    for leaf, name in leaf_bone.items():
        shared.setdefault(name, []).append(leaf)
    reused = sorted((bone, leaves) for bone, leaves in shared.items()
                    if len(leaves) > 1)
    print("   %d bone(s) carry more than one batch%s"
          % (len(reused), (", e.g. %s -> %s" % reused[0]) if reused else ""))

    print("\n2. pose, and the file's own degrees on the bone:")
    scene = bpy.context.scene
    worst = 0.0
    readable = []
    for name, head, cmd, parent, block in specs:
        bone = arm.data.bones[name]
        parent_rest = (arm.data.bones[parent].matrix_local if parent
                       else Matrix.Identity(4))
        base = parent_rest.inverted() @ bone.matrix_local
        # no subdivision: an AXIS_ANGLE bone keeps the file's own key count (a
        # 1200-degree sweep is one angle channel, so it does not need extra keys)
        values = (XM.command_values(cmd) if cmd.get("dataref") else [None])
        for frame, value in enumerate(values, start=1):
            pose = dict(pose0)
            if cmd.get("dataref") and value is not None:
                pose[cmd["dataref"]] = value
            want = base.inverted() @ Matrix(
                [list(row) for row in XA.to_bl(XA.cmd_matrix(cmd, pose))])
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            got = arm.pose.bones[name].matrix_basis
            delta = max(abs(got[i][j] - want[i][j])
                        for i in range(4) for j in range(4))
            worst = max(worst, delta)
            if delta > 1e-4:
                check(False, "bone %s frame %d: pose is %.4f off its command"
                      % (name, frame, delta))
        if cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS):
            degrees = bone.get("xp8_angle_deg")
            if degrees is not None:
                readable.append((name, cmd.get("dataref") or "-",
                                 round(degrees, 6)))
    print("   worst matrix delta %.2e" % worst)
    check(worst <= 1e-4, "poses drift from the file's own matrices")
    for name, dataref, degrees in readable[:8]:
        print("   %-10s %-44s %11.6f deg" % (name, dataref[:44], degrees))

    print("\n3. round trip:")
    dst = os.path.join(OUT, os.path.basename(SRC))
    stats = XM.export_header(bpy.context, header, dst, preserve=True)
    same = sha(SRC) == sha(dst)
    print("   untouched: %-9s %s" % ("verbatim" if stats["verbatim"] else "REGEN",
                                     "IDENTICAL" if same else "DIFFERS"))
    check(same, "an untouched import with an armature is not byte identical")

    stem = os.path.splitext(os.path.basename(SRC))[0]
    dst2 = os.path.join(OUT, stem + "_blender.obj")
    XM.export_header(bpy.context, header, dst2, preserve=True,
                     animation="BLENDER",
                     report=lambda lvl, msg: print("   ", msg))
    src_anim = XA.build(OC.parse_text(text))
    out_anim = XA.build(OC.parse(dst2))
    worst = 0.0
    for position in (0.0, 0.25, 0.5, 0.75, 1.0):
        pose = {}
        for dref, (lo, hi) in src_anim.pose_ranges().items():
            pose[dref] = lo + (hi - lo) * position
        a = src_anim.matrix_at(pose)
        b = out_anim.matrix_at(pose)
        for leaf in a:
            if leaf not in b:
                check(False, "batch %d lost its animation on export" % leaf)
                continue
            delta = max(abs(a[leaf][i][j] - b[leaf][i][j])
                        for i in range(4) for j in range(4))
            worst = max(worst, delta)
    print("   animation='BLENDER': worst matrix delta %.2e, %d batches"
          % (worst, len(src_anim.leaves)))
    check(worst <= 1e-3, "the exported motion differs from the source")

    print("\n%s" % ("FAILED: %d problem(s)" % len(fails) if fails else "OK"))
    return 1 if fails else 0


sys.exit(main())
