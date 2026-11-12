"""ATTR_blend / ATTR_no_blend / ATTR_shadow_blend survive the round trip.

    <blender> -b --factory-startup --python verify/blend_test.py

The three lines are one state category, and X-Plane's default state is *blending
on* (OBJ8 spec, "This is the default" next to ATTR_blend) — so an explicit
ATTR_blend in the source legitimately exports as no line at all, and XP2B's
command printer starts out considering ATTR_blend already in effect.  What must
not be lost is the mode itself (it has to come back as ATTR_blend after a batch
that turned blending off) and the alpha cutoff ratio that ATTR_no_blend and
ATTR_shadow_blend carry: a ratio that lands on XP2B's 0.5 default instead of the
file's own number changes what the sim cuts away.

The fixture is five batches: blend, no_blend 0.3 (with a trailing comment),
blend, shadow_blend 0.75, no_blend with no ratio (0.5, the spec default).
"""
import json
import os
import sys

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

import addon_utils                                          # noqa: E402

addon_utils.enable("io_xplane2blender", default_set=True)

import io_scene_xplane_obj as IMP                           # noqa: E402

try:
    IMP.register()
except Exception:                                           # noqa: BLE001
    pass

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg, flush=True)
    else:
        print("   ok   %s" % msg, flush=True)
    return cond


for ob in list(bpy.data.objects):          # the startup cube, camera, light
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)
for mat in list(bpy.data.materials):
    bpy.data.materials.remove(mat)

SRC = os.path.join(REPO, "verify", "fixtures", "blend.obj")
print("import:", sorted(bpy.ops.import_scene.xplane_obj(filepath=SRC)), flush=True)

# ---------------------------------------------------------------------------
# the material side: mode and ratio per cloned material
# ---------------------------------------------------------------------------
modes = {}
for mat in bpy.data.materials:
    xp = getattr(mat, "xplane", None)
    if xp is None:
        continue
    modes[mat.name] = (getattr(xp, "blend_v1000", None),
                       round(getattr(xp, "blendRatio", -1), 4))

print("materials: %s" % modes, flush=True)

by_mode = {}
for name, (mode, ratio) in modes.items():
    by_mode.setdefault(mode, []).append((name, ratio))

check("on" in by_mode, "no material took ATTR_blend -> blend_v1000 'on'")
check("off" in by_mode, "no material took ATTR_no_blend -> blend_v1000 'off'")
check("shadow" in by_mode, "no material took ATTR_shadow_blend -> 'shadow'")

off_ratios = sorted({r for _, r in by_mode.get("off", [])})
check(off_ratios == [0.3, 0.5],
      "ATTR_no_blend ratios are %s, expected [0.3, 0.5] (0.3 spelled in the "
      "file, 0.5 where the line has no number)" % off_ratios)
shadow_ratios = [r for _, r in by_mode.get("shadow", [])]
check(shadow_ratios == [0.75],
      "ATTR_shadow_blend 0.75 -> blendRatio %s" % shadow_ratios)

# a trailing comment is not a cutoff: it must not draw a "not a number" warning
warnings = []
for coll in bpy.data.collections:
    raw = coll.get("xp2b_convert_warnings")
    if raw:
        warnings += json.loads(raw)
blend_warnings = [w for w in warnings if "cutoff" in w or "blend" in w]
check(not blend_warnings,
      "the conversion complained about the blend lines: %s" % blend_warnings)

# ---------------------------------------------------------------------------
# the export side: what XPlane2Blender writes back
# ---------------------------------------------------------------------------
from io_xplane2blender.xplane_types import xplane_file as XF   # noqa: E402

files = XF.createFilesFromBlenderRootObjects(bpy.context.scene,
                                            bpy.context.view_layer)
text = files[0].write() if files else ""
lines = [l.strip() for l in (text or "").splitlines()]
blend_lines = [l for l in lines if l.split()[:1] and "blend" in l.split()[0]]
print("export blend lines: %s" % blend_lines, flush=True)

check(lines, "the export produced no text (XPlane2Blender refused the file)")


def number_after(line):
    """The number on a line, as a float (None if the line carries none).

    Read as a number, not as text: XP2B formats floats from a float32, so
    0.3 comes back out as 0.30000001.
    """
    words = line.split()
    if len(words) < 2:
        return None
    try:
        return float(words[1])
    except ValueError:
        return None


def near(a, b, tol=1e-5):
    return a is not None and abs(a - b) <= tol


# the export has to walk the same states the file does, in order: the 0.3
# cutout, blending back on, the shadow cutout, and the unnumbered 0.5 default
want = [("ATTR_no_blend", 0.3), ("ATTR_blend", None),
        ("ATTR_shadow_blend", 0.75), ("ATTR_no_blend", 0.5)]
got = [(l.split()[0], number_after(l)) for l in blend_lines]
same = len(got) == len(want) and all(
    g[0] == w[0] and (w[1] is None or near(g[1], w[1]))
    for g, w in zip(got, want))
check(same,
      "export blend lines are %s, expected %s (0.3 cutout, blend back on, "
      "0.75 shadow cutout, 0.5 default)" % (got, want))

# the first batch is blending-on, which is X-Plane's default state: nothing to
# declare, and declaring it anyway would only add draw calls
first_tris = next((i for i, l in enumerate(lines) if l.startswith("TRIS")), None)
head = lines[:first_tris] if first_tris is not None else lines
check(not [l for l in head if l.split()[:1] == ["ATTR_blend"]],
      "a leading batch that is blending-on must not write ATTR_blend: %s" % head)

print("\nRESULT: %s" % ("PASS" if not fails else "FAIL (%d)" % len(fails)), flush=True)
