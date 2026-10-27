"""Standalone (no bpy) checks for objcodec against the files in this repo's docs.

    python3 verify/test_codec.py <dir-with-obj-files> ...

For every .obj it parses, re-emits with no changes, parses the result again and
compares the two structures field by field.  Then it diffs the emitted text
against the original to show exactly what the writer normalises.
"""
import difflib
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import objcodec as OC  # noqa: E402


def compare(a, b, name):
    bad = []

    def chk(label, x, y):
        if x != y:
            bad.append("%s: %r != %r" % (label, x, y))

    chk("header_flag", a.header_flag, b.header_flag)
    chk("version_line", OC.parse_text.__name__ and a.version_line.split()[0],
        b.version_line.split()[0])
    chk("globals", a.globals, b.globals)
    chk("vt count", len(a.vt), len(b.vt))
    for i, (x, y) in enumerate(zip(a.vt, b.vt)):
        chk("vt[%d]" % i, x.as_tuple(), y.as_tuple())
        if bad:
            break
    chk("vline", a.vline, b.vline)
    chk("vlight", a.vlight, b.vlight)
    chk("idx", a.idx, b.idx)
    chk("leaf count", len(a.leaves), len(b.leaves))
    for i, (x, y) in enumerate(zip(a.leaves, b.leaves)):
        chk("leaf[%d].kind" % i, x.kind, y.kind)
        chk("leaf[%d].indices" % i, x.indices, y.indices)
        chk("leaf[%d].span" % i, (x.offset, x.count), (y.offset, y.count))
        chk("leaf[%d].state" % i, x.state, y.state)
        if bad:
            break
    chk("events", a.events, b.events)
    return bad


def main(dirs):
    files = []
    for d in dirs:
        files.extend(sorted(glob.glob(os.path.join(d, "*.obj"))))
    if not files:
        print("no .obj files found")
        return 2
    failures = 0
    for path in files:
        t = time.time()
        src = OC.parse(path)
        text = OC.emit(src)
        tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "_roundtrip.obj")
        with open(tmp, "wb") as f:
            f.write(text.encode("latin-1", "replace"))
        again = OC.parse(tmp)
        bad = compare(src, again, path)
        with open(path, "r", encoding="latin-1") as f:
            original_text = f.read()
        diff = list(difflib.unified_diff(
            original_text.splitlines(), text.splitlines(),
            "original", "emitted", lineterm="", n=0))
        changed = sum(1 for d in diff if d[:1] in "+-" and d[:3] not in ("+++", "---"))
        print("%-34s %6.2fs  VT %5d IDX %6d leaves %3d  changed lines %4d  %s"
              % (os.path.basename(path), time.time() - t, len(src.vt),
                 len(src.idx), len(src.leaves), changed,
                 "OK" if not bad else "FAIL"))
        for b in bad[:6]:
            print("    !", b)
        if src.warnings:
            for w in src.warnings[:4]:
                print("    warn:", w)
        if bad:
            failures += 1
    print("\n%d/%d files identical after round trip" % (len(files) - failures, len(files)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
