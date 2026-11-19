"""The vrconfig reader -- no Blender needed.

    python3 verify/test_vrconfig.py [-- <corpus dir>]

Pins what reading a ``_vrconfig.txt`` must do, on the fixture and (when a corpus
directory is given, or X-Plane is installed) on every real file:

* **nothing is lost**: re-emitting the stored lines reproduces the file byte for
  byte, so a later write-back can replace only the blocks that changed and leave
  the author's comments alone;
* the numbers are read even when the author typed commas;
* the three ``REF_POINT_ACF`` forms pick the right origin - getting this wrong
  moves every hotspot in the aircraft and says nothing;
* ``BEGIN_MANIP`` is keyed by the OBJ's own manipulator type, not by ``ACTION``
  (55 of 519 blocks in the corpus retype, and keying on ACTION loses them);
* the hotspot orientation vectors follow the spec: PSI 90 looks at the right
  wing, THE 90 looks up, PHI 90 tilts the right ear to the shoulder.
"""
import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

import vrconfig as VR  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "vrconfig.txt")

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("  FAIL %s" % msg)
    return cond


def close(a, b, tol=1e-6):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def main(argv):
    raw = open(FIXTURE, "rb").read().decode("utf-8")
    vr = VR.parse(FIXTURE)
    print("parsed %r" % (vr,))

    # --- the file's own text ------------------------------------------------
    check(vr.text() == raw, "round trip is byte identical")
    check(vr.header == ["A", "1100", "VRCONFIG"], "header: %r" % vr.header)
    check(vr.ending == "\n", "line ending: %r" % vr.ending)

    # --- hotspots -----------------------------------------------------------
    check(len(vr.hotspots) == 2, "hotspot count: %d != 2" % len(vr.hotspots))
    if len(vr.hotspots) == 2:
        pilot, observer = vr.hotspots
        check(pilot.name == "Pilot's Seat", "hotspot name: %r" % pilot.name)
        check(pilot.kind == "SITTING", "hotspot kind: %r" % pilot.kind)
        check(close(pilot.xyz, (-0.3843, 0.9135, -0.6620)),
              "comma separated PRESET_XYZ: %r" % (pilot.xyz,))
        check(close(pilot.aabb, (-0.6843, 0.2635, -0.9620,
                                -0.0843, 0.4635, -0.3620)), "AABB: %r" % (pilot.aabb,))
        check(close(pilot.size, (0.6, 0.2, 0.6)), "AABB extents: %r" % (pilot.size,))
        check(close(pilot.centre, (-0.3843, 0.3635, -0.6620)),
              "AABB centre: %r" % (pilot.centre,))
        check(observer.kind == "STANDING", "standing kind: %r" % observer.kind)
        check(close(observer.eye(), (0.3, 0.4 + VR.EYE_HEIGHT, -0.1)),
              "standing hotspot previews at eye height: %r" % (observer.eye(),))
        check(observer.psi == 180.0 and observer.the == -10.0 and observer.phi == 5.0,
              "orientation: %r" % ((observer.psi, observer.the, observer.phi),))
        check(pilot.lines[0].strip().startswith("BEGIN_TELEPORT_HOTSPOT"),
              "block text starts at BEGIN")
        check(pilot.lines[-1].strip() == "END_TELEPORT_HOTSPOT",
              "block text ends at END")

    # --- manipulators -------------------------------------------------------
    check(len(vr.manips) == 3, "manip count: %d != 3" % len(vr.manips))
    if len(vr.manips) == 3:
        axis, knob, switch = vr.manips
        check(axis.key() == ("axis_knob", "laminar/c90/airCon/dial/cabin_temp", None),
              "one-argument key: %r" % (axis.key(),))
        check(knob.key() == ("command_knob", "sim/systems/wipers_up",
                             "sim/systems/wipers_dn"), "two-argument key: %r" % (knob.key(),))
        check(switch.tooltip == "Autopilot Speed Knob",
              "tooltip: %r" % (switch.tooltip,))
        check(switch.action is not None and switch.action.startswith("ACTION command_axis"),
              "ACTION kept as text: %r" % (switch.action,))
        check(switch.lines[-1].strip() == "END_MANIP", "manip block ends at END")

    # --- the origin ---------------------------------------------------------
    check(vr.ref_point_raw is not None, "REF_POINT_ACF seen")
    check(vr.origin_kind() == "acf_old_cg", "origin kind: %r" % vr.origin_kind())
    origin = vr.origin((0.0, 0.0, 12.0))
    check(close(origin["offset"], (0.0, 0.3048, 10.2108)),
          "REF_POINT_ACF origin: %r" % (origin["offset"],))

    bare = VR.parse_text("A\n1100\nVRCONFIG\nREF_POINT_ACF\n")
    check(bare.origin_kind() == "acf", "bare REF_POINT_ACF kind: %r" % bare.origin_kind())
    check(close(bare.origin()["offset"], (0.0, 0.0, 0.0)),
          "bare REF_POINT_ACF is the authoring point")

    cg_only = VR.parse_text("A\n1100\nVRCONFIG\n"
                            "BEGIN_TELEPORT_HOTSPOT SITTING X\n"
                            "AABB -1 -1 -1 1 1 1\nPRESET_XYZ 0 0 -1\n"
                            "END_TELEPORT_HOTSPOT\n")
    check(cg_only.origin_kind() == "cg", "no directive means CG: %r" % cg_only.origin_kind())
    # the C90B: 12 ft aft of the authoring point, 0 up
    origin = cg_only.origin((0.0, 0.0, 12.0))
    check(close(origin["offset"], (0.0, 0.0, 12.0 * 0.3048), tol=1e-9),
          "CG origin converted from feet: %r" % (origin["offset"],))
    check(close(cg_only.hotspot_xyz(cg_only.hotspots[0], origin),
                (0.0, 0.0, -1.0 + 12.0 * 0.3048), tol=1e-9),
          "hotspot position applies the origin")

    # --- the OBJ side of the lookup ----------------------------------------
    check(VR.manip_key("ATTR_manip_command_knob\thand\tsim/a\tsim/b\tA Knob")
          == ("command_knob", "sim/a", "sim/b"), "OBJ key for command_knob")
    check(VR.manip_key("ATTR_manip_axis_knob\thand\t0.000\t1.000\t0.050\t0.250\t"
                       "laminar/c90/x\tCabin Dial")
          == ("axis_knob", "laminar/c90/x", None),
          "OBJ key skips the numeric ranges and the tooltip")
    index = VR.manip_index([
        ("C90B_cockpit_012", "ATTR_manip_command_knob\thand\tsim/systems/wipers_up\tsim/systems/wipers_dn\tWipers"),
        ("C90B_cockpit_012", "ATTR_manip_axis_knob\thand\t0.0\t1.0\t0.1\t0.5\tlaminar/c90/airCon/dial/cabin_temp\tCabin"),
    ])
    missing = VR.resolve(vr, index)
    check(len(missing) == 1, "one manipulator resolves to nothing: %d" % len(missing))
    if len(missing) == 1:
        check(missing[0].type == "command_switch_up_down",
              "the unresolved one is the switch: %r" % missing[0].type)
    check(vr.manips[0].sources == ["C90B_cockpit_012"],
          "resolved sources: %r" % (vr.manips[0].sources,))

    # --- orientation vectors ------------------------------------------------
    f, u = VR.view_vectors(0.0, 0.0, 0.0)
    check(close(f, (0.0, 0.0, -1.0)), "PSI 0 looks forward (-Z): %r" % (f,))
    check(close(u, (0.0, 1.0, 0.0)), "PHI 0 is upright: %r" % (u,))
    f, _ = VR.view_vectors(90.0, 0.0, 0.0)
    check(close(f, (1.0, 0.0, 0.0)), "PSI 90 looks at the right wing (+X): %r" % (f,))
    f, _ = VR.view_vectors(0.0, 90.0, 0.0)
    check(close(f, (0.0, 1.0, 0.0)), "THE 90 looks straight up: %r" % (f,))
    _, u = VR.view_vectors(0.0, 0.0, 90.0)
    check(close(u, (1.0, 0.0, 0.0), tol=1e-9),
          "PHI 90 tilts the right ear to the shoulder (+X up): %r" % (u,))

    # --- the frame change ---------------------------------------------------
    check(VR.to_bl((1.0, 2.0, 3.0)) == (1.0, -3.0, 2.0), "xp -> blender axes")

    # --- the real corpus, when there is one --------------------------------
    corpus = argv[0] if argv else _corpus()
    if corpus:
        files = sorted(glob.glob(os.path.join(corpus, "**", "*" + VR.SUFFIX),
                                 recursive=True))
        print("\ncorpus: %d file(s) under %s" % (len(files), corpus))
        total_h = total_m = total_missing = 0
        for path in files:
            text = open(path, "rb").read().decode("utf-8", "replace")
            one = VR.parse(path)
            check(one.text() == text, "%s: byte identical round trip"
                  % os.path.basename(path))
            total_h += len(one.hotspots)
            total_m += len(one.manips)
            if not one.hotspots and not one.manips:
                check(False, "%s: parsed nothing" % os.path.basename(path))
        print("  hotspots=%d manipulators=%d" % (total_h, total_m))
        if total_missing:
            print("  %d manipulator(s) with no geometry" % total_missing)
    else:
        print("\n(no corpus found - fixture only)")

    print()
    if fails:
        print("%d CHECK(S) FAILED" % len(fails))
        return 1
    print("vrconfig OK")
    return 0


def _corpus():
    guess = os.path.expanduser("~/X-Plane 12/Aircraft")
    return guess if os.path.isdir(guess) else None


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    sys.exit(main(args))
