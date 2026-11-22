"""Prefill objects are read from the .acf and kept out of the way.

    <blender> -b --factory-startup --python verify/acf_prefill.py

Plane Maker's prefill popup (`No Prefill / Prefill / Prefill Only`) is
`_obj_flags` bit 8.  A prefill object is drawn before the world so the clouds
behind the 3-d cockpit are never filled in - it is a cloud mask, not airframe,
and Laminar's own are untextured proxies sitting *inside* the aircraft (MD-82:
one tube, 4 197 triangles in one batch, against 227 577 in 877 batches for the
real interior).  Nothing in the .obj says it, so without the .acf these objects
are indistinguishable from a white broken cockpit shell.

Checks:
  1. the bit: 256 alone, and 256 among other flags (PA-18 stores 297), while
     cockpit (2048) and wreckage (512) flags do not read as prefill
  2. codec: each .acf yields the prefill object it declares (MD-82 / S-76 /
     PA-18 `prefill.obj`, b738 `737_prefill.obj`, C90B `C90B_prefill.obj`),
     aircraft with none yield none; and across the fleet the bit is set on
     exactly the `*prefill*` objects - never on a cockpit or wreckage object
  3. import: one `Prefill` collection under the aircraft, holding exactly that
     file's collection, yellow, excluded from the current view layer
  4. the exclusion is real: the object leaves the view layer and the XP2B
     export list (25 - 1 = 24 files on the PA-18), and re-enabling puts it back
"""

import os
import sys

import addon_utils
import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
XM_PKG = "bl_ext.user_default.io_scene_xplane_obj"
if XM_PKG not in bpy.context.preferences.addons:
    addon_utils.enable(XM_PKG, default_set=False)

NAME = os.path.basename(REPO)
addon = __import__(NAME)
import importlib
AF = importlib.import_module(NAME + ".acfcodec")

from io_xplane2blender.xplane_types import xplane_file as XF

fails = []


def check(label, ok, detail=""):
    print("%-52s %s  %s" % (label, "OK  " if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


XP = os.path.expanduser("~/X-Plane 12/Aircraft")


def acf(*parts):
    for root, _dirs, files in os.walk(XP):
        for name in files:
            if not name.lower().endswith(".acf"):
                continue
            path = os.path.join(root, name)
            if all(p in path for p in parts):
                return path
    raise SystemExit("no .acf matching %s" % (parts,))


def names(paths):
    return sorted(os.path.basename(p) for p in paths)


MD80 = acf("MD80.acf")
S76 = acf("S-76")
B738 = acf("737-800")
C90B = acf("Beechcraft King Air C90B", "C90B.acf")
PA18 = acf("PA-18")
C172 = acf("Cessna 172 SP", "Cessna_172SP.acf")

# 1) the bit -------------------------------------------------------------------
check("bit 8: is_prefill(256)", AF.is_prefill(1 << 8) is True)
check("PA-18's 297 (256+32+8+1) is prefill", AF.is_prefill(297) is True)
check("MD-82's 289 (256+32+1) is prefill", AF.is_prefill(289) is True)
check("141 (interior shell) is not prefill", AF.is_prefill(141) is False)
check("512 (wreckage) is not prefill", AF.is_prefill(AF.WRECKAGE_BIT) is False)
check("2048 (cockpit INN) is not prefill",
      AF.is_prefill(1 << 11) is False)

# 2) the codec -----------------------------------------------------------------
check("MD-82: prefill.obj", names(AF.prefill_objects(MD80)) == ["prefill.obj"],
      names(AF.prefill_objects(MD80)))
check("S-76: prefill.obj", names(AF.prefill_objects(S76)) == ["prefill.obj"],
      names(AF.prefill_objects(S76)))
check("b738: 737_prefill.obj",
      names(AF.prefill_objects(B738)) == ["737_prefill.obj"],
      names(AF.prefill_objects(B738)))
check("C90B: C90B_prefill.obj",
      names(AF.prefill_objects(C90B)) == ["C90B_prefill.obj"],
      names(AF.prefill_objects(C90B)))
check("PA-18: prefill.obj", names(AF.prefill_objects(PA18)) == ["prefill.obj"],
      names(AF.prefill_objects(PA18)))
check("C172: none at all", AF.prefill_objects(C172) == set(),
      names(AF.prefill_objects(C172)))

# the fleet-wide claim the bit rests on: every object that carries bit 8 is a
# `*prefill*` file, and none of them is also a cockpit or wreckage object
acfs = 0
hits = 0
odd = []
shared = []
for root, _dirs, files in os.walk(XP):
    for name in files:
        if not name.lower().endswith(".acf"):
            continue
        path = os.path.join(root, name)
        found = AF.prefill_objects(path)
        if not found:
            continue
        acfs += 1
        hits += len(found)
        for p in found:
            if "prefill" not in os.path.basename(p).lower():
                odd.append("%s: %s" % (name, p))
        if found & AF.wreckage_objects(path):
            shared.append(name)
check("fleet: 14 prefill objects in 14 aircraft", (acfs, hits) == (14, 14),
      "%d object(s) in %d aircraft" % (hits, acfs))
check("all of them are *prefill*.obj", not odd, odd)
check("no prefill object is also wreckage", not shared, shared)

# 3) a real import -------------------------------------------------------------
bpy.ops.import_scene.xplane_acf(filepath=PA18)
scene = bpy.context.scene
view_layer = bpy.context.view_layer
aircraft = bpy.data.collections[os.path.basename(PA18)]
groups = [c for c in aircraft.children if c.name.startswith(addon.PREFILL_GROUP)]
check("one Prefill collection under the aircraft", len(groups) == 1,
      [c.name for c in aircraft.children])
prefill = groups[0] if groups else None


def layer_coll(coll):
    return addon._layer_collection(view_layer, coll)


if prefill is not None:
    check("it holds the prefill object's collection",
          names([c.name for c in prefill.children]) == ["prefill.obj"],
          sorted(c.name for c in prefill.children))
    check("it is yellow",
          all(c.color_tag == addon.PREFILL_TAG for c in prefill.children),
          sorted({c.color_tag for c in prefill.children}))
    check("it is not orange (that is wreckage)",
          addon.PREFILL_TAG != addon.WRECKAGE_TAG)
    check("excluded from the view layer", layer_coll(prefill).exclude is True)
    gone = [ob.name for ob in bpy.data.objects
            if ob.name.startswith("prefill") and ob.name in view_layer.objects]
    check("its objects left the view layer", not gone, gone)

    files = [f.filename for f in
             XF.createFilesFromBlenderRootObjects(scene, view_layer, False)]
    check("XP2B exports 25 - 1 = 24 files", len(files) == 24,
          "%d files" % len(files))
    check("...and not one of them is the prefill",
          not [f for f in files if "prefill" in f.lower()], files)

    roots = addon._collection_roots(aircraft)
    check("Place by .acf still walks into the prefill",
          any(ob.name.startswith("prefill") for ob in roots),
          "%d root object(s)" % len(roots))

    layer_coll(prefill).exclude = False
    files = [f.filename for f in
             XF.createFilesFromBlenderRootObjects(scene, view_layer, False)]
    check("re-enabled: 25 files again", len(files) == 25, len(files))

print()
if fails:
    print("RESULT: FAIL")
    for name in fails:
        print("  - " + name)
    sys.exit(1)
print("RESULT: PASS")
