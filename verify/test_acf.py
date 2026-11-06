"""Structural checks on acfcodec -- no Blender needed.

    python3 verify/test_acf.py

The parser is small but the thing it has to get right is not obvious: an
attached object's path is resolved against BOTH the aircraft folder and its
``objects/`` subfolder, in that order, and Laminar's own files mix the two
(``../X.obj`` reaches out of ``objects/``, ``X.obj`` reaches into it).  Every
case below is one of the shapes found in a real X-Plane 12 aircraft.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import acfcodec as A  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("  FAIL %s" % msg)


ACF = """I
1200 Version
ACF

PROPERTIES_BEGIN
P _acf/_name Test Aircraft
P _obja/0/_v10_att_file_stl ../cockpit.obj
P _obja/0/_v10_is_internal 0
P _obja/1/_v10_att_file_stl fuselage.obj
P _obja/10/_v10_att_file_stl Cabin/seats.obj
P _obja/2/_v10_att_file_stl Vans RV-10_exterior.obj
P _obja/3/_v10_att_file_stl wheels.OBJ
P _obja/4/_v10_att_file_stl ../cockpit.obj
P _obja/5/_v10_att_file_stl spaced.obj   
P _obja/6/_v10_att_file_stl missing.obj
P _obja/7/_v10_att_file_stl missing.obj
P _obja/count 11
P _wpna/0/_v10_att_file_stl AIM9.wpn
P _prop/0/_num_blades 2.000000000
PROPERTIES_END
"""


def main():
    root = tempfile.mkdtemp(prefix="xpobj_acf_")
    try:
        aircraft = os.path.join(root, "Test Aircraft")
        objects = os.path.join(aircraft, "objects")
        os.makedirs(os.path.join(objects, "Cabin"))
        for path in ["cockpit.obj", "Vans RV-10_exterior.obj"]:
            open(os.path.join(aircraft, path), "w").close()
        for path in ["fuselage.obj", "wheels.OBJ", "spaced.obj",
                     "Cabin/seats.obj"]:
            open(os.path.join(objects, path), "w").close()
        acf = os.path.join(aircraft, "TestAircraft.acf")
        with open(acf, "w") as f:
            f.write(ACF)

        found = A.slots(A.read_text(acf))
        print("slots: %r" % (found,))
        check([s for s, _ in found] == [0, 1, 2, 3, 4, 5, 6, 7, 10],
              "slot order is numeric: %r" % ([s for s, _ in found],))
        check(found[4][1] == "../cockpit.obj", "duplicate slot kept: %r"
              % (found[4],))
        check(found[5][1] == "spaced.obj", "trailing spaces trimmed: %r"
              % (found[5],))
        check(all("wpn" not in raw for _s, raw in found),
              "_wpna (weapons) must not be read as attached objects")

        paths, missing = A.objects(acf)
        names = [os.path.relpath(p, root) for p in paths]
        print("paths: %r" % (names,))
        print("missing: %r" % (missing,))
        # `../cockpit.obj` is resolved against objects/, i.e. the aircraft
        # folder; `fuselage.obj` against objects/ after the aircraft folder
        # missed it. One path, once, even though slot 4 attaches it again.
        check(names == ["Test Aircraft/cockpit.obj",
                        "Test Aircraft/objects/fuselage.obj",
                        "Test Aircraft/Vans RV-10_exterior.obj",
                        "Test Aircraft/objects/wheels.OBJ",
                        "Test Aircraft/objects/spaced.obj",
                        "Test Aircraft/objects/Cabin/seats.obj"],
              "resolution order or dedupe is wrong: %r" % (names,))
        # an extension is matched without regard to case, a missing file is
        # reported once even when two slots attach it
        check(missing == ["missing.obj"], "missing: %r" % (missing,))

        # X-Plane 9 wrote a binary .acf; say so instead of returning nothing
        binary = os.path.join(aircraft, "Old.acf")
        with open(binary, "wb") as f:
            f.write(b"ACF\x00\x10\x00\x00binary")
        try:
            A.objects(binary)
        except ValueError as exc:
            check("binary" in str(exc), "binary error text: %s" % exc)
        else:
            check(False, "binary .acf must raise ValueError")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    if fails:
        print("\nRESULT: FAIL (%d)" % len(fails))
        return 1
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
