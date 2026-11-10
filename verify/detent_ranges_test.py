"""The axis-detent pre-check must agree with XPlane2Blender's own validation.

    <blender> -b --factory-startup --python verify/detent_ranges_test.py

XP2B validates ATTR_axis_detent_range *during* export and one failure cancels
the whole file, so the conversion pre-checks every rule.  Being stricter than
XP2B is not the safe side: it silently drops manipulators the export would have
accepted.  XP2B compares the numbers as ``Decimal(f"{v:.3f}")``, which is why a
detent that tops out at 0.005 under a bone lifting 0.0046 is fine - the b738
flap handle and spoilers are exactly that case, and refusing them cost two
detented manipulators per cockpit.

The tables here are the real ones, not invented: the flap lever's 11 ranges and
the spoiler's 6, with the lifts their ANIM_trans tables give.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

import io_scene_xplane_obj.xp2b as X2                              # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg, flush=True)
    return cond


# b738_cockpit.obj, flap handle: 0..1 in 0.125 steps, stop pits at the marks
# (17 ATTR_axis_detent_range lines, verbatim)
FLAP = [(0.000, 0.000, 0.000), (0.000, 0.125, 0.005),
        (0.125, 0.125, 0.000), (0.125, 0.250, 0.005),
        (0.250, 0.250, 0.000), (0.250, 0.375, 0.005),
        (0.375, 0.375, 0.000), (0.375, 0.500, 0.005),
        (0.500, 0.500, 0.000), (0.500, 0.625, 0.005),
        (0.625, 0.625, 0.000), (0.625, 0.750, 0.005),
        (0.750, 0.750, 0.000), (0.750, 0.875, 0.005),
        (0.875, 0.875, 0.000), (0.875, 1.000, 0.005),
        (1.000, 1.000, 0.000)]
FLAP_LIFT = 0.0046          # laminar/B738/handles/flap_lever/stop_pos

SPOILER = [(0.000, 0.000, 0.000), (0.000, 0.089, 0.003),
           (0.089, 0.089, 0.002), (0.089, 0.667, 0.003),
           (0.667, 0.667, 0.002), (0.667, 1.000, 0.003)]
SPOILER_LIFT = 0.003

check(X2._q(0.0046) == X2._q(0.005),
      "0.0046 and 0.005 must quantise to the same number XP2B checks")
check(X2._q(0.0004) == X2._q(0.0), "0.0004 must quantise to 0.000")

check(X2._ranges_tile(FLAP, 0.0, 1.0, FLAP_LIFT),
      "the flap handle's 17 ranges must pass: a 0.005 detent under a 0.0046 "
      "lift is accepted by XP2B (both are 0.005 at three decimals)")
check(X2._ranges_tile(SPOILER, 0.0, 1.0, SPOILER_LIFT),
      "the spoiler's 6 ranges must pass")

# the rules it still has to refuse, or the export gets cancelled instead
HEIGHT = [(0.0, 0.5, 0.020), (0.5, 1.0, 0.0)]
check(not X2._ranges_tile(HEIGHT, 0.0, 1.0, 0.0046),
      "a height past the bone's lift must be refused")
GAP = [(0.0, 0.4, 0.0), (0.5, 1.0, 0.0)]
check(not X2._ranges_tile(GAP, 0.0, 1.0, 0.02),
      "a gap between two ranges must be refused")
SHORT = [(0.0, 0.5, 0.0)]
check(not X2._ranges_tile(SHORT, 0.0, 1.0, 0.02),
      "a list that does not reach v1_max must be refused")
PEG = [(0.0, 0.5, 0.010), (0.5, 0.5, 0.010), (0.5, 1.0, 0.0)]
check(not X2._ranges_tile(PEG, 0.0, 1.0, 0.02),
      "a stop peg (a pit as tall as its neighbour) must be refused")
BACKWARDS = [(0.5, 0.4, 0.0), (0.4, 1.0, 0.0)]
check(not X2._ranges_tile(BACKWARDS, 0.0, 1.0, 0.02),
      "a range whose start is past its end must be refused")
EDGE_PIT = [(0.0, 0.0, 0.0), (0.0, 1.0, 0.010)]
check(X2._ranges_tile(EDGE_PIT, 0.0, 1.0, 0.02),
      "an edge pit lower than its only neighbour is legal")
FLAT = [(0.0, 0.0, 0.010), (0.0, 1.0, 0.010)]
check(not X2._ranges_tile(FLAT, 0.0, 1.0, 0.02),
      "an edge pit as tall as its neighbour is a peg and must be refused")

print("RESULT: %s (%d failure(s))" % ("PASS" if not fails else "FAIL", len(fails)),
      flush=True)
