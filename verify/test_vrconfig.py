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
    check(VR.to_xp(VR.to_bl((1.0, 2.0, 3.0))) == (1.0, 2.0, 3.0), "and back")

    # --- angles round trip --------------------------------------------------
    for psi, the, phi in ((0.0, 0.0, 0.0), (90.0, 0.0, 0.0), (180.0, -10.0, 5.0),
                          (359.0, 30.0, -20.0), (45.0, -45.0, 90.0),
                          (3.0, 2.0, -1.0)):
        f, u = VR.view_vectors(psi, the, phi)
        back = VR.angles_from_vectors(f, u)
        check(all(abs(a - b) < 1e-6 for a, b in
                  zip(back, (psi if psi <= 180 else psi, the, phi))),
              "angles round trip %r -> %r" % ((psi, the, phi), tuple(round(v, 4) for v in back)))

    # --- the author's number shape -----------------------------------------
    check(VR.format_like("-0.3658,", -0.5) == "-0.5000,", "four decimals and the comma")
    check(VR.format_like("-3.0", 2.5) == "2.5", "one decimal stays one decimal")
    check(VR.format_like("30", 30.0) == "30", "an integer stays an integer")
    check(VR.format_like("30", 30.5) == "30.5", "and grows digits when it must")

    # --- rewriting one directive leaves the rest alone ----------------------
    block = [
        "BEGIN_TELEPORT_HOTSPOT SITTING Pilot's Seat",
        "   AABB -0.6843 0.2635 -0.9620 -0.0843 0.4635 -0.3620",
        "   PRESET_XYZ -0.3843, 0.9135, -0.6620",
        "   PRESET_PSI 0.0000",
        "   PRESET_THE 0.0000",
        "   PRESET_PHI 0.0000",
        "END_TELEPORT_HOTSPOT",
    ]
    out = VR.rewrite_lines(block, "PRESET_XYZ", [-0.5, 0.9, -0.7])
    check(len(out) == len(block), "rewriting keeps the block's line count")
    check(out[2] == "   PRESET_XYZ -0.5000, 0.9000, -0.7000",
          "only that line changed, with its commas and decimals: %r" % out[2])
    check(all(a == b for i, (a, b) in enumerate(zip(block, out)) if i != 2),
          "every other line is byte identical")
    out = VR.rewrite_lines(block, "PRESET_THE", [12.5])
    check(out[4] == "   PRESET_THE 12.5000", "a negative-zero line becomes the new value: %r" % out[4])

    # a directive the block does not have yet goes in before the END line
    short = ["BEGIN_TELEPORT_HOTSPOT SITTING X", "   PRESET_XYZ 0 0 0",
             "END_TELEPORT_HOTSPOT"]
    out = VR.rewrite_lines(short, "AABB", [-1, -1, -1, 1, 1, 1])
    check(out[-2].strip().startswith("AABB")
          and out[-1].strip().startswith("END_"),
          "a missing directive is inserted before END: %r" % out)

    # --- the whole-file write: nothing changed means nothing written --------
    check(VR.splice(vr, [(vr.hotspots[0], vr.hotspots[0].lines)]) == vr.text(),
          "an unedited block leaves the file byte identical")

    # --- manipulator parameters ---------------------------------------------
    axis, knob, switch = vr.manips
    check(axis.values("DEG_PER_ACTION") == [2.0],
          "DEG_PER_ACTION read from the block: %r" % (axis.values("DEG_PER_ACTION"),))
    check(axis.values("SWITCH_THRESHOLD") == [], "an absent directive reads empty")
    check(switch.has("HOLD_MANIP"), "HOLD_MANIP seen as a flag")
    check(switch.values("ACTION") == [0.0, 0.0, 0.2],
          "ACTION's own numbers are readable: %r" % (switch.values("ACTION"),))
    check(switch.line_of("ACTION").startswith("ACTION command_axis"),
          "ACTION kept as its own line: %r" % (switch.line_of("ACTION"),))

    # changing a number keeps the line's shape, nothing else moves
    out = VR.rewrite_lines(axis.lines, "DEG_PER_ACTION", [15.0])
    check(out[1] == "\tDEG_PER_ACTION 15", "the tab and the integer stay: %r" % out[1])
    check(len(out) == len(axis.lines) and out[0] == axis.lines[0], "only that line moved")

    # toggling a flag: add before END, remove the line, and no-op when it matches
    added = VR.rewrite_flag(axis.lines, "HOLD_MANIP", True)
    check(added[-2].strip() == "HOLD_MANIP" and added[-1] == axis.lines[-1],
          "a flag is inserted before END with the block's indent: %r" % added)
    check(VR.rewrite_flag(added, "HOLD_MANIP", True) is added,
          "asking for a flag that is already there changes nothing")
    removed = VR.rewrite_flag(added, "HOLD_MANIP", False)
    check(removed == axis.lines, "removing it gives the original block back")

    # a directive can be taken out of the block entirely
    dropped = VR.rewrite_drop(added, "HOLD_MANIP")
    check(dropped == axis.lines, "dropping the flag gives the block back")
    check(VR.rewrite_drop(axis.lines, "SWITCH_THRESHOLD") == axis.lines,
          "dropping a directive that is not there changes nothing")
    dropped = VR.rewrite_drop(axis.lines, "DEG_PER_ACTION")
    check(dropped == [axis.lines[0], axis.lines[-1]],
          "dropping the number leaves BEGIN and END: %r" % (dropped,))

    # ACTION can be retyped or dropped wholesale
    retyped = VR.rewrite_raw(switch.lines, "ACTION",
                             "ACTION command_knob sim/a_up sim/a_dn")
    check(sum(1 for line in retyped if "ACTION" in line) == 1,
          "one ACTION line, not two")
    check(retyped[-1] == switch.lines[-1], "and END stays last")
    dropped = VR.rewrite_raw(switch.lines, "ACTION", None)
    check(not any("ACTION" in line for line in dropped), "None removes it")

    # --- the real corpus, when there is one --------------------------------
    corpus = argv[0] if argv else _corpus()
    if corpus:
        files = sorted(glob.glob(os.path.join(corpus, "**", "*" + VR.SUFFIX),
                                 recursive=True))
        print("\ncorpus: %d file(s) under %s" % (len(files), corpus))
        total_h = total_m = total_missing = total_edited = 0
        for path in files:
            text = open(path, "rb").read().decode("utf-8", "replace")
            one = VR.parse(path)
            check(one.text() == text, "%s: byte identical round trip"
                  % os.path.basename(path))
            total_h += len(one.hotspots)
            total_m += len(one.manips)
            if not one.hotspots and not one.manips:
                check(False, "%s: parsed nothing" % os.path.basename(path))
            # move one hotspot and check that only its own line changed: this is
            # the promise the writer makes to an author who hand-edits the file
            if one.hotspots:
                hotspot = one.hotspots[0]
                moved = VR.rewrite_lines(hotspot.lines, "PRESET_XYZ",
                                         [0.125, 0.5, -0.75])
                changed_text = VR.splice(one, [(hotspot, moved)])
                before = text.replace("\r\n", "\n").split("\n")
                after = changed_text.replace("\r\n", "\n").split("\n")
                diff = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
                total_edited += 1
                check(len(before) == len(after) and len(diff) == 1,
                      "%s: moving a hotspot rewrites %d line(s), not the file "
                      "(%d line(s) differ)" % (os.path.basename(path), len(diff),
                                               len(diff)))
                back = VR.parse_text(changed_text)
                check(close(back.hotspots[0].xyz, (0.125, 0.5, -0.75)),
                      "%s: the written position reads back"
                      % os.path.basename(path))
            # the same promise for a manipulator block: retype one number and
            # add a flag, and nothing outside that block may move
            if one.manips:
                index, manip = 0, one.manips[0]
                for i, candidate in enumerate(one.manips):
                    if candidate.values("DEG_PER_ACTION"):
                        index, manip = i, candidate
                        break
                new = VR.rewrite_lines(manip.lines, "DEG_PER_ACTION", [12.5])
                new = VR.rewrite_flag(new, "HOLD_MANIP", True)
                changed_text = VR.splice(one, [(manip, new)])
                before = text.split(one.ending)
                after = changed_text.split(one.ending)
                low, high = manip.first_line, manip.first_line + len(manip.lines)
                added = len(new) - len(manip.lines)
                # the promise, stated exactly: every line except the block is
                # the line it was, in the same order (a naive zip would report
                # every line after an added one as "changed")
                expected = before[:low] + list(new) + before[high:]
                if len(expected) != len(after):
                    outside = [("length", len(expected), len(after))]
                else:
                    outside = [i for i, (a, b) in enumerate(zip(expected, after))
                               if a != b]
                check(not outside,
                      "%s: editing a manipulator touches only its block "
                      "(%d line(s) differ elsewhere: %r)"
                      % (os.path.basename(path), len(outside), outside[:3]))
                check(len(after) == len(before) + added,
                      "%s: %d line(s) added, as asked (%d -> %d)"
                      % (os.path.basename(path), added, len(before), len(after)))
                endings_ok = changed_text.count(one.ending) == \
                    text.count(one.ending) + added
                check(endings_ok,
                      "%s: line endings survived the edit (%r)"
                      % (os.path.basename(path), one.ending))
                back = VR.parse_text(changed_text, path)
                same = back.manips[index]
                check(close(same.values("DEG_PER_ACTION"), (12.5,))
                      and same.has("HOLD_MANIP") and len(back.manips) == len(one.manips),
                      "%s: the edited block reads back, block count unchanged"
                      % os.path.basename(path))

        print("  hotspots=%d manipulators=%d edited=%d"
              % (total_h, total_m, total_edited))
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
