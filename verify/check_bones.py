"""Nested animation: does the bone chain reproduce the file's cascade?

    blender -b --factory-startup --python verify/check_bones.py -- [file.obj]

An OBJ8 batch wrapped by more than one transform command (a static pivot plus a
keyed rotate, or two nested ANIM_begin blocks with one dataref each) is imported
as an armature with one bone per command.  This checks the three claims that
matter:

1. structure - a nested batch is bone-parented to an armature with as many
   bones as the file has transform commands, a single-command batch is still an
   Empty rig, and nothing gets an Armature modifier (OBJ8 has no skinning: a
   modifier would be baked into the vertices);
2. pose - at every key frame the batch's world matrix equals the matrix
   `xpanim` evaluates from the file, so the bones mean the same thing the
   ANIM_* lines do.  Without this the rig is just numbers that look plausible;
3. round trip - an untouched import still writes back byte for byte, and an
   export with animation='BLENDER' leaves the bone-rigged batches' animation
   from the file alone instead of writing a flat key table over it.
"""
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import bpy                                              # noqa: E402

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


def matrices_for(cmds, values_at_frame):
    """The chain's accumulated matrix at one frame, straight from the file."""
    pose = {}
    total = XA.IDENT
    for cmd in cmds:
        dataref = cmd.get("dataref")
        if dataref:
            pose[dataref] = values_at_frame[dataref]
        total = XA.mat_mul(total, XA.cmd_matrix(cmd, pose))
    return total


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

    armatures = []
    rig_empties = []
    plain = []
    for leaf, name in enumerate(leaf_objs):
        if not name:
            continue
        ob = bpy.data.objects.get(name)
        chain = anim.leaves.get(leaf) or []
        cmds = [cmd for cmd in XM.chain_transforms(chain)
                if cmd.get("dataref") or not XM._is_identity(cmd, pose0)]
        arm = XM.armature_of(ob)
        print("  batch %-2d %-22s commands %d  parent %s"
              % (leaf, name, len(cmds),
                 "bone %s" % ob.parent_bone if arm else
                 ("empty %s" % ob.parent.name if ob.parent else "header")))

        check(not any(m.type == 'ARMATURE' for m in ob.modifiers),
              "batch %d has an Armature modifier: OBJ8 has no skinning" % leaf)

        if len(cmds) > 1:
            check(arm is not None,
                  "batch %d has %d transform commands but no armature"
                  % (leaf, len(cmds)))
            if arm is None:
                continue
            armatures.append((leaf, ob, arm, cmds))
            check(len(arm.data.bones) == len(cmds),
                  "batch %d: %d bones for %d commands"
                  % (leaf, len(arm.data.bones), len(cmds)))
            check(ob.parent_type == 'BONE',
                  "batch %d is not bone-parented" % leaf)
            check(ob.parent_bone == arm.data.bones[-1].name,
                  "batch %d is not on the innermost bone" % leaf)
            names = [bone.name for bone in arm.data.bones]
            stored = [entry["bone"] for entry in XM.bone_commands(arm)]
            check(names == stored,
                  "batch %d: bone order %s does not match the stored map %s"
                  % (leaf, names, stored))
            check(arm.animation_data is not None
                  and arm.animation_data.action is not None,
                  "batch %d: the armature has no Action" % leaf)
        elif len(cmds) == 1:
            rig_empties.append(leaf)
            check(arm is None, "batch %d got an armature for one command" % leaf)
            check(XM.is_rig(ob.parent),
                  "batch %d is not on a rig Empty" % leaf)
        else:
            plain.append(leaf)
            check(arm is None and ob.parent is header,
                  "batch %d has no animation but hangs off something else" % leaf)

    print("\n1. structure: %d armature(s), %d Empty rig(s), %d static batch(es)"
          % (len(armatures), len(rig_empties), len(plain)))
    check(armatures, "the fixture produced no armature at all")

    print("\n2. pose at every key frame:")
    # Per bone, not per chain: one bone = one command = one key table, and each
    # table runs over frames 1..its own key count.  Two commands on the same
    # dataref with different key counts therefore sit at different values on the
    # same frame - that is the convention this scene shares with XPlane2Blender
    # (one key per frame, per command), so the check has to be per command too.
    scene = bpy.context.scene
    worst = 0.0
    for leaf, ob, arm, cmds in armatures:
        bones = arm.data.bones
        for index, cmd in enumerate(cmds):
            bone = bones["anim%d" % index]
            parent_rest = (bones["anim%d" % (index - 1)].matrix_local
                           if index else XM.Matrix.Identity(4))
            base = parent_rest.inverted() @ bone.matrix_local
            values = (XM._subdivide(cmd, XM.command_values(cmd))
                      if cmd.get("dataref") else [None])
            for frame, value in enumerate(values, start=1):
                pose = dict(pose0)
                if cmd.get("dataref"):
                    pose[cmd["dataref"]] = value
                want = base.inverted() @ XM.Matrix(
                    [list(row) for row in XA.to_bl(XA.cmd_matrix(cmd, pose))])
                scene.frame_set(frame)
                bpy.context.view_layer.update()
                got = arm.pose.bones[bone.name].matrix_basis
                delta = max(abs(got[i][j] - want[i][j])
                            for i in range(4) for j in range(4))
                worst = max(worst, delta)
                if delta > 1e-4:
                    check(False, "batch %d bone %d frame %d: pose is %.4f off "
                                 "the file's matrix"
                          % (leaf, index, frame, delta))
        checked = sum(len(cmd.get("keys") or [cmd]) for cmd in cmds)
        print("  batch %-3d %d bone(s), %d key(s) checked"
              % (leaf, len(cmds), checked))
    print("  worst matrix delta %.2e" % worst)
    check(worst <= 1e-4, "bone poses drift from the file's own matrices")

    print("\n3. round trip:")
    dst = os.path.join(OUT, os.path.basename(SRC))
    stats = XM.export_header(bpy.context, header, dst, preserve=True)
    same = sha(SRC) == sha(dst)
    print("  untouched: %-9s %s" % ("verbatim" if stats["verbatim"] else "REGEN",
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
    print("  animation='BLENDER': worst matrix delta %.2e, %d batches"
          % (worst, len(src_anim.leaves)))
    check(worst <= 1e-3, "the exported motion differs from the source")

    print("\n%s" % ("FAILED: %d problem(s)" % len(fails) if fails else "OK"))
    return 1 if fails else 0


sys.exit(main())
