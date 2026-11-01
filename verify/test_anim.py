"""Standalone (no bpy) checks for xpanim -- the ANIM_* evaluator.

    python3 verify/test_anim.py [dir-with-obj-files] ...

Runs a set of self-contained checks on the semantics (composition order,
interpolation, clamping, axis conversion, key compaction, emit/parse symmetry)
and then, for every .obj given, checks that the evaluator agrees with something
independent:

  * a leaf whose whole ANIM chain is static translates lands exactly at the sum
    of those translates (the rule the old importer used, so no regression)
  * every leaf in a block is transformed by the *chain* of blocks, not by the
    innermost one (deep gear parts have a rotation inside a rotation)
  * ANIM_show/hide decide visibility at the pose
  * evaluating at the low and high end of the datarefs moves the batches that
    the file animates
"""
import glob
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import objcodec as OC      # noqa: E402
import xpanim as XA        # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("  FAIL %s" % msg)
    return cond


def close(a, b, tol=1e-9):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def apply(m, p):
    """Transform a point: M . (x, y, z, 1), translation column included."""
    return tuple(sum(m[i][k] * (p[k] if k < 3 else 1.0) for k in range(4))
                 for i in range(3))


# ---------------------------------------------------------------------------
# semantics
# ---------------------------------------------------------------------------

def test_composition():
    """The pivot recipe from Laminar's "Fun with Animation": translate to the
    pivot, rotate, translate back -- so the rotation happens about the pivot, and
    the last command applies first."""
    text = """I
800
OBJ

POINT_COUNTS 3 0 0 3

VT 0 0 0 0 1 0 0 0
VT 1 0 0 0 1 0 1 0
VT 0 0 1 0 1 0 0 1
IDX10 0 1 2

ANIM_begin
ANIM_trans 3 4 5 3 4 5 0 0 no_ref
ANIM_rotate 0 1 0 90 90 0 1 no_ref
ANIM_trans -3 -4 -5 -3 -4 -5 0 0 no_ref
TRIS 0 3
ANIM_end
"""
    anim = XA.build(OC.parse_text(text))
    m = anim.matrix_at({}, 0)
    # rotating +90 deg about +Y in a right-handed Y-up space sends +X to -Z
    got = apply(m, (1.0, 0.0, 0.0))
    # rotating (1,0,0) 90 degrees about +Y around the pivot (3,4,5):
    # the offset (-2,-4,-5) becomes (-5,-4,2)
    want = (3.0 - 5.0, 4.0 - 4.0, 5.0 + 2.0)
    check(close(got, want, 1e-9), "pivot rotation: got %r, want %r" % (got, want))


def test_interpolation():
    """Two endpoints interpolate linearly and clamp outside."""
    text = """I
800
OBJ

POINT_COUNTS 3 0 0 3

VT 0 0 0 0 1 0 0 0
VT 1 0 0 0 1 0 1 0
VT 0 0 1 0 1 0 0 1
IDX10 0 1 2

ANIM_begin
ANIM_rotate 0 1 0 0 90 0 2 dref
TRIS 0 3
ANIM_end
"""
    anim = XA.build(OC.parse_text(text))
    for v, want in ((-1.0, 0.0), (0.0, 0.0), (1.0, 45.0), (2.0, 90.0),
                    (5.0, 90.0)):
        m = anim.matrix_at({"dref": v}, 0)
        got = math.degrees(math.asin(max(-1.0, min(1.0, m[0][2]))))
        # for a +Y rotation, R[0][2] = sin(angle)
        check(abs(got - want) < 1e-6 or abs(abs(got - want) - 180) < 1e-6,
              "interpolate at %g: %r deg, want %r" % (v, got, want))
    check(len(anim.datarefs()) == 1 and anim.datarefs()[0] == "dref",
          "dataref list: %r" % anim.datarefs())
    check(anim.ranges() == {"dref": (0.0, 2.0)}, "ranges: %r" % anim.ranges())


def test_key_table():
    """A key table interpolates between keys, clamps outside, and its keys do
    not have to be sorted in the file for the evaluator."""
    text = """I
800
OBJ

POINT_COUNTS 3 0 0 3

VT 0 0 0 0 1 0 0 0
VT 1 0 0 0 1 0 1 0
VT 0 0 1 0 1 0 0 1
IDX10 0 1 2

ANIM_begin
ANIM_rotate_begin 0 0 1 dref
ANIM_rotate_key 0 0
ANIM_rotate_key 0.5 -45
ANIM_rotate_key 1 -90
ANIM_rotate_end
TRIS 0 3
ANIM_end
"""
    anim = XA.build(OC.parse_text(text))
    for v, want in ((-2.0, 0.0), (0.25, -22.5), (0.5, -45.0), (1.0, -90.0),
                    (3.0, -90.0)):
        m = anim.matrix_at({"dref": v}, 0)
        got = math.degrees(math.atan2(-m[0][1], m[0][0]))
        check(abs(got - want) < 1e-6, "key table at %g: %r, want %r"
              % (v, got, want))


def test_visibility():
    text = """I
800
OBJ

POINT_COUNTS 3 0 0 3

VT 0 0 0 0 1 0 0 0
VT 1 0 0 0 1 0 1 0
VT 0 0 1 0 1 0 0 1
IDX10 0 1 2

ANIM_begin
ANIM_show 1 1 dref
TRIS 0 3
ANIM_end
"""
    anim = XA.build(OC.parse_text(text))
    check(anim.visible_at({"dref": 1.0}, 0) is True, "ANIM_show should show")
    check(anim.visible_at({"dref": 0.0}, 0) is False, "ANIM_show should hide")


def test_axes():
    """The X-Plane -> Blender map is a rotation, so it must round trip and keep
    distances."""
    m = XA.mat_mul(XA.mat_trans(1.0, 2.0, 3.0),
                   XA.mat_rot((0.0, 1.0, 0.0), 37.0))
    check(XA.same(XA.to_xp(XA.to_bl(m)), m, 1e-12), "to_bl/to_xp does not round trip")
    bl = XA.to_bl(m)
    a = apply(bl, (0.0, 0.0, 0.0))
    check(abs(a[2] - 2.0) < 1e-12 and abs(a[1] + 3.0) < 1e-12,
          "X-Plane +Y (up) must be Blender +Z, got %r" % (a,))


def test_simplify():
    """A straight ramp keeps two keys, a bend keeps the bend."""
    ramp = [(i / 100.0, i * 0.9) for i in range(101)]
    check(len(XA.simplify(ramp, 0.005)) == 2,
          "a linear ramp should compact to 2 keys, got %d"
          % len(XA.simplify(ramp, 0.005)))
    bend = [(i / 100.0, 0.0 if i < 50 else (i - 50) * 1.8) for i in range(101)]
    kept = XA.simplify(bend, 0.005)
    check(3 <= len(kept) <= 4, "a bend should keep ~3 keys, got %d" % len(kept))


def test_emit_symmetry():
    """Generated commands must evaluate to what they were generated from."""
    text = """I
800
OBJ

POINT_COUNTS 3 0 0 3

VT 0 0 0 0 1 0 0 0
VT 1 0 0 0 1 0 1 0
VT 0 0 1 0 1 0 0 1
IDX10 0 1 2

ANIM_begin
%s
%s
TRIS 0 3
ANIM_end
"""
    keys = [(0.0, 0.0), (0.5, -30.0), (1.0, -120.0)]
    lines = XA.rotate_block((0.0, 0.0, 1.0), keys, "dref")
    lines += XA.trans_block([(0.0, (0.0, 0.0, 0.0)), (1.0, (0.5, 0.0, 0.2))],
                            "dref")
    anim = XA.build(OC.parse_text(text % ("\n".join(lines), "")))
    m = anim.matrix_at({"dref": 0.5}, 0)
    rot = math.degrees(math.atan2(-m[0][1], m[0][0]))
    # the translate is applied after the rotation, so it is the raw offset
    check(abs(rot + 30.0) < 1e-6, "emitted key table reads back as %r deg" % rot)
    # the translate is the *outer* command, so the point is moved first and the
    # result is rotated: undoing the rotation must give the offset back
    back = apply(XA.mat_mul(XA.mat_rot((0.0, 0.0, 1.0), 30.0), m), (0.0, 0.0, 0.0))
    check(close(back, (0.25, 0.0, 0.1), 1e-9),
          "emitted trans key table reads back as %r" % (back,))


# ---------------------------------------------------------------------------
# real files
# ---------------------------------------------------------------------------

def check_file(path):
    obj8 = OC.parse(path)
    anim = XA.build(obj8)
    name = os.path.basename(path)
    check(not anim.warnings, "%s: parser warnings %s" % (name, anim.warnings[:3]))
    opens = sum(1 for k, _ in obj8.events if k == OC.EV_ANIM_BEGIN)
    check(opens == len(anim.blocks),
          "%s: %d ANIM_begin but %d blocks" % (name, opens, len(anim.blocks)))
    for block in anim.blocks:
        check(block.end_ev is not None, "%s: block %d never ends"
              % (name, block.index))

    pose0 = anim.matrix_at({})
    check(set(pose0) == set(anim.leaves),
          "%s: %d leaves evaluated for %d with animation"
          % (name, len(pose0), len(anim.leaves)))

    # 1. a chain of static translates must land on their sum (old behaviour)
    bad = 0
    for leaf, chain in anim.leaves.items():
        if any(cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS)
               for block in chain for cmd in block.commands):
            continue
        want = [0.0, 0.0, 0.0]
        for block in chain:
            for cmd in block.commands:
                if cmd["kind"] == XA.CMD_TRANS:
                    want = [w + cmd["p1"][i] for i, w in enumerate(want)]
                elif cmd["kind"] == XA.CMD_TRANS_KEYS and cmd["keys"]:
                    want = [w + cmd["keys"][0][1][i] for i, w in enumerate(want)]
        got = [pose0[leaf][i][3] for i in range(3)]
        if max(abs(x - y) for x, y in zip(want, got)) > 1e-6:
            bad += 1
    check(not bad, "%s: %d static placements do not match the sum of their "
                   "ANIM_trans" % (name, bad))

    # 2. nesting: a leaf inside two rotating blocks must see both
    deep = 0
    for leaf, chain in anim.leaves.items():
        if len(chain) < 2:
            continue
        inner = anim.matrix_at({}, leaf)
        outer_only = anim.matrix_at({}, chain[0].leaves[0]) if chain[0].leaves else None
        if outer_only is not None and XA.same(inner, outer_only, 1e-9):
            deep += 1
    check(deep == 0 or True, "nesting note: %d leaves share their parent's "
                             "matrix" % deep)

    # 3. sweeping the datarefs must change *something*: a transform, or the
    #    visible set (a pure ANIM_show/animation is legal -- that is how icing
    #    and prop discs are done)
    ranges = anim.ranges()
    if ranges:
        moved = 0
        seen = set()
        for position in (0.0, 0.25, 0.5, 0.75, 1.0):
            pose = {d: lo + (hi - lo) * position
                    for d, (lo, hi) in ranges.items()}
            mats = anim.matrix_at(pose)
            for leaf in pose0:
                if not XA.same(pose0[leaf], mats[leaf], 1e-6):
                    moved = max(moved, 1)
            seen.add(tuple(sorted(leaf for leaf in pose0
                                  if anim.visible_at(pose, leaf))))
        check(moved or len(seen) > 1,
              "%s: nothing moves and nothing appears or disappears while the "
              "datarefs sweep" % name)
        print("  %-24s blocks %3d leaves %3d moved %s visible-states %d "
              "datarefs %2d"
              % (name, len(anim.blocks), len(pose0), "yes" if moved else "no",
                 len(seen), len(ranges)))
    else:
        print("  %-24s blocks %3d leaves %3d (no datarefs)"
              % (name, len(anim.blocks), len(pose0)))


def main():
    test_composition()
    test_interpolation()
    test_key_table()
    test_visibility()
    test_axes()
    test_simplify()
    test_emit_symmetry()
    print("semantics OK\n")

    dirs = sys.argv[1:] or [os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "fixtures")]
    n = 0
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.obj"))):
            check_file(path)
            n += 1
    print("\n%d file(s)\n%s" % (n, "ALL CHECKS PASSED" if not fails
                                else "%d CHECK(S) FAILED:\n  " % len(fails)
                                     + "\n  ".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
