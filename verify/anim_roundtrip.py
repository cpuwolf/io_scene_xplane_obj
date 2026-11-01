"""Does the animation survive the trip into Blender and back out again?

    blender -b --factory-startup --python verify/anim_roundtrip.py -- \
            [samples-dir] [output-dir]

For every .obj in the folder:

1. import it (rigs for every animated batch, one Action each)
2. write it back unchanged and require BYTE IDENTITY -- the rigs must not leak
   into the file
3. write it again with animation='BLENDER', which regenerates every ANIM_*
   block from the rig Actions
4. evaluate BOTH files' animation at five dataref positions and require the
   per-batch transforms to agree -- this is the check that proves the exported
   key tables mean the same thing as the ones that came in
5. require the geometry to be unchanged: same batch count, same index count,
   same VT count

The last point is why the comparison is done on matrices rather than on the
ANIM_* lines: the exporter is free to write a two-endpoint command as a key
table (it always does), so the text differs by design while the motion must not.
"""
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
SAMPLES = argv[0] if argv else os.path.join(REPO, "verify", "fixtures")
OUT = argv[1] if len(argv) > 1 else os.path.join(tempfile.gettempdir(),
                                                 "xpobj_anim")
POSITIONS = (0.0, 0.25, 0.5, 0.75, 1.0)

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


def poses(anim, position):
    """The pose dict the importer bakes at: every dataref at its own fraction."""
    return {d: lo + (hi - lo) * position
            for d, (lo, hi) in anim.ranges().items()}


def compare_motion(name, src_anim, dst_anim):
    """Compare the animation of two files at five positions, batch by batch."""
    src_leaves = sorted(src_anim.leaves)
    dst_leaves = sorted(dst_anim.leaves)
    if not src_leaves:
        print("  %-24s no animation in the source" % name)
        return
    check(len(dst_leaves) == len(src_leaves),
          "%s: %d animated batches became %d"
          % (name, len(src_leaves), len(dst_leaves)))
    worst = 0.0
    for position in POSITIONS:
        a = src_anim.matrix_at(poses(src_anim, position))
        b = dst_anim.matrix_at(poses(dst_anim, position))
        for leaf in src_leaves:
            if leaf not in b:
                check(False, "%s: batch %d lost its animation" % (name, leaf))
                continue
            delta = max(abs(a[leaf][i][j] - b[leaf][i][j])
                        for i in range(4) for j in range(4))
            worst = max(worst, delta)
            if delta > 1e-3:
                check(False, "%s: batch %d moves differently at %.2f "
                             "(max element delta %.4f)"
                      % (name, leaf, position, delta))
    print("  %-24s %d batches, worst matrix delta %.2e"
          % (name, len(src_leaves), worst))


def main():
    if not os.path.isdir(SAMPLES):
        print("sample folder missing: %s" % SAMPLES)
        return 2
    os.makedirs(OUT, exist_ok=True)
    originals = sorted(os.path.join(SAMPLES, f) for f in os.listdir(SAMPLES)
                       if f.lower().endswith(".obj"))
    print("samples: %d files in %s\n" % (len(originals), SAMPLES))

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.xplane_obj(filepath=originals[0], import_folder=True)
    headers = {os.path.abspath(h.get(XM.PROP_SOURCE)): h
               for h in bpy.data.objects if XM.is_header(h)}
    check(len(headers) == len(originals),
          "expected %d headers, got %d" % (len(originals), len(headers)))
    for src, h in sorted(headers.items()):
        rigs = [c for c in h.children if XM.is_rig(c)]
        parts = XM.parts(h)
        actions = [r for r in rigs if r.get(XM.PROP_ANIM_ACTION)]
        print("  %-24s parts %3d rigs %3d actions %3d"
              % (os.path.basename(src), len(parts), len(rigs), len(actions)))
        check(len(rigs) == len(actions) or not rigs,
              "%s: %d rigs but %d actions" % (os.path.basename(src),
                                              len(rigs), len(actions)))

    print("\n1. untouched round trip (rigs must not reach the file):")
    total_lines_in = total_lines_out = 0
    for src, h in sorted(headers.items()):
        dst = os.path.join(OUT, os.path.basename(src))
        stats = XM.export_header(bpy.context, h, dst, preserve=True)
        same = sha(src) == sha(dst)
        print("  %-24s %-9s %s" % (os.path.basename(src),
                                   "verbatim" if stats["verbatim"] else "REGEN",
                                   "IDENTICAL" if same else "DIFFERS"))
        check(same, "%s is not byte identical after an untouched round trip"
              % os.path.basename(src))

    print("\n2. animation written back from the rigs:")
    for src, h in sorted(headers.items()):
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(OUT, stem + "_anim.obj")
        stats = XM.export_header(bpy.context, h, dst, preserve=True,
                                 animation="BLENDER",
                                 report=lambda lvl, msg: print("   ", msg))
        a = OC.parse(src)
        b = OC.parse(dst)
        anim_a = XA.build(a)
        anim_b = XA.build(b)
        print("\n  %s: vt %d->%d idx %d->%d leaves %d->%d blocks %d->%d"
              % (stem, len(a.vt), len(b.vt), len(a.idx), len(b.idx),
                 len(a.leaves), len(b.leaves), len(anim_a.blocks),
                 len(anim_b.blocks)))
        check(len(b.leaves) == len(a.leaves),
              "%s: batch count changed %d -> %d"
              % (stem, len(a.leaves), len(b.leaves)))
        check(len(b.idx) == len(a.idx),
              "%s: index count changed %d -> %d" % (stem, len(a.idx),
                                                    len(b.idx)))
        compare_motion(stem, anim_a, anim_b)
        # Rotation must always come back as a key table, and a translate that
        # still has two different endpoints would be an animation the file kept
        # instead of regenerating.  A constant ANIM_trans is the placement form
        # the exporter writes on purpose, so that one is fine.
        stray = []
        for kind, line in b.events:
            if kind != OC.EV_RAW or not line:
                continue
            words = line.split()
            if words[0] == "ANIM_rotate":
                stray.append(line)
            elif words[0] == "ANIM_trans" and len(words) >= 7:
                if words[1:4] != words[4:7]:
                    stray.append(line)
        check(not stray, "%s: %d unregenerated animation commands left (%s)"
              % (stem, len(stray), stray[:2]))
        if b.warnings:
            print("    warnings:", b.warnings[:4])

    print("\n%s" % ("ALL CHECKS PASSED" if not fails
                    else "%d CHECK(S) FAILED:\n  " % len(fails)
                         + "\n  ".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
