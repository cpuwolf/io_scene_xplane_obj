"""Structural checks on the mini.obj fixture -- no Blender needed.

    python3 verify/test_fixture.py

This runs in CI before anything touches Blender: it pins the parser's exact
reading of the fixture, so a change in the codec that silently drops a line
fails here instead of being noticed months later in a customer's aircraft.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import objcodec as OC  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "mini.obj")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("  FAIL %s" % msg)


def main():
    o = OC.parse(FIXTURE)
    print("parsed %r" % (o,))

    check(o.header_flag == "I", "header flag: %r" % o.header_flag)
    check(o.version_line.startswith("800"), "version: %r" % o.version_line)
    check(o.line_ending == "\n", "line ending: %r" % o.line_ending)
    check(len(o.vt) == 7, "VT rows: %d != 7" % len(o.vt))
    check(len(o.idx) == 9, "index table: %d != 9" % len(o.idx))
    check(len(o.vline) == 0 and len(o.vlight) == 0, "unexpected line/light rows")

    # the comment sits before the globals and must stay in the globals region
    check(o.globals[0].startswith("#"), "comment lost from the globals: %r"
          % (o.globals[0] if o.globals else None))
    for kw in ("GLOBAL_specular", "TEXTURE", "TEXTURE_NORMAL",
               "NORMAL_METALNESS", "POINT_COUNTS"):
        check(o.global_value(kw) is not None, "missing global %s" % kw)
    check(o.global_value("TEXTURE") == ("mini.png",),
          "TEXTURE argument: %r" % (o.global_value("TEXTURE"),))
    check(o.global_value("POINT_COUNTS") == ("7", "0", "0", "9"),
          "POINT_COUNTS: %r" % (o.global_value("POINT_COUNTS"),))

    # two geometry commands, the second one inside an ANIM block
    check(len(o.leaves) == 2, "leaves: %d != 2" % len(o.leaves))
    check([lf.kind for lf in o.leaves] == ["TRIS", "TRIS"], "leaf kinds")
    check(o.leaves[0].indices == [0, 1, 2, 0, 2, 3],
          "leaf 0 indices: %r" % (o.leaves[0].indices,))
    check(o.leaves[1].indices == [4, 5, 6],
          "leaf 1 indices: %r" % (o.leaves[1].indices,))
    check((o.leaves[1].offset, o.leaves[1].count) == (6, 3),
          "leaf 1 span: %r" % ((o.leaves[1].offset, o.leaves[1].count),))

    # ATTR_poly_os is state: hoisted out of the command stream, snapshotted on
    # every leaf that draws under it
    for i, lf in enumerate(o.leaves):
        cats = [c for c, _ in lf.state]
        check("poly_os" in cats, "leaf %d lost its poly_os state: %r"
              % (i, lf.state))
    check("ATTR_poly_os" not in [p for k, p in o.events if k == OC.EV_RAW],
          "ATTR_poly_os leaked into the raw command stream")

    anims = [k for k, _ in o.events if k == OC.EV_ANIM_BEGIN]
    ends = [k for k, _ in o.events if k == OC.EV_ANIM_END]
    check(len(anims) == 1 and len(ends) == 1,
          "ANIM block count: %d/%d" % (len(anims), len(ends)))
    raw = [p for k, p in o.events if k == OC.EV_RAW]
    check(any(p.startswith("LIGHT_PARAM") for p in raw),
          "LIGHT_PARAM was not preserved: %r" % (raw,))
    check(not o.warnings, "unexpected parse warnings: %r" % (o.warnings,))

    # ---- round trip ----------------------------------------------------
    text = OC.emit(o)
    tmp = os.path.join(HERE, "_fixture_roundtrip.obj")
    with open(tmp, "wb") as f:
        f.write(text.encode("latin-1", "replace"))
    b = OC.parse(tmp)
    check(b.globals == o.globals, "globals changed: %r" % (b.globals,))
    check([r.as_tuple() for r in b.vt] == [r.as_tuple() for r in o.vt],
          "VT rows changed")
    check(b.idx == o.idx, "index table changed")
    check([(lf.kind, lf.indices, lf.offset, lf.count, lf.state)
           for lf in b.leaves] ==
          [(lf.kind, lf.indices, lf.offset, lf.count, lf.state)
           for lf in o.leaves], "leaves changed")
    check(b.events == o.events, "command stream changed")
    check(OC.emit(b) == text, "the writer is not idempotent")
    os.remove(tmp)

    # ---- triangulated geometry sanity ----------------------------------
    # X-Plane's front face is clockwise, so the winding normal of the quad
    # points down while the authored normals point up: the importer flips it.
    flipped = OC.flip_winding(o.leaves[0].indices)
    check(flipped == [0, 2, 1, 0, 3, 2], "flip_winding: %r" % (flipped,))

    print()
    if fails:
        print("%d CHECK(S) FAILED" % len(fails))
        return 1
    print("fixture OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
