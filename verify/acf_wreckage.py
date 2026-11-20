"""Wreckage objects are read from the .acf and kept out of the way.

    <blender> -b --factory-startup --python verify/acf_wreckage.py

Plane Maker's `wreckage` checkbox is `_obj_flags` bit 9 (its tooltip lives in the
Plane Maker binary: "only shown when the aircraft is destroyed ... MUST BE
ATTACHED TO WING 1 LEFT OR RIGHT, H-STAB LEFT OR RIGHT, VERT STAB 1 OR 2, OR BODY
1").  Nothing in the .obj says it, so without the .acf these objects are
indistinguishable from airframe parts - and they sit exactly where the airframe
parts sit, which is why they are worth hiding.

Checks:
  1. codec: every .acf yields the wreckage objects it actually declares
     (C172 family `broken_*`, MD-82 / ASK 21 / S-76 `wreck_*`, F-4 `F4_wrek/*`,
     SF50 `broken_*`), aircraft with none yield none, and no cockpit object is
     also wreckage
  2. import: one `Wreckage` collection under the aircraft, holding exactly those
     files' collections, orange, and excluded from the current view layer
  3. the exclusion is real: those objects leave the view layer and the XP2B
     export list (24 - 4 = 20 files on the ASK 21), while `Place by .acf` still
     moves them (the placement walk is recursive on purpose)
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


C172 = acf("Cessna 172 SP", "Cessna_172SP.acf")
C172G = acf("Cessna_172SP_G1000")
SEAPLANE = acf("Cessna_172SP_seaplane")
SF50 = acf("CirrusSF50")
F4 = acf("F-4 Phantom")
MD80 = acf("MD80.acf")
ASK21 = acf("ASK 21")
S76 = acf("S-76")
B738 = acf("737-800")

# 1) the codec -----------------------------------------------------------------
check("C172: 4 broken parts",
      names(AF.wreckage_objects(C172)) == ["broken_fuse.obj", "broken_nose.obj",
                                          "broken_wing_L.obj", "broken_wing_R.obj"],
      names(AF.wreckage_objects(C172)))
check("C172 G1000: 4 broken parts",
      len(AF.wreckage_objects(C172G)) == 4, len(AF.wreckage_objects(C172G)))
check("C172 seaplane: 4 broken + the floats",
      names(AF.wreckage_objects(SEAPLANE)) == ["broken_floats.obj",
                                               "broken_fuse.obj", "broken_nose.obj",
                                               "broken_wing_L.obj",
                                               "broken_wing_R.obj"],
      names(AF.wreckage_objects(SEAPLANE)))
check("SF50: 3", len(AF.wreckage_objects(SF50)) == 3,
      names(AF.wreckage_objects(SF50)))
check("F-4: 5 from F4_wrek/ (name says 'wrek', the bit does not)",
      len(AF.wreckage_objects(F4)) == 5, names(AF.wreckage_objects(F4)))
check("MD-82: 6", len(AF.wreckage_objects(MD80)) == 6,
      names(AF.wreckage_objects(MD80)))
check("S-76: 3", len(AF.wreckage_objects(S76)) == 3,
      names(AF.wreckage_objects(S76)))
check("b738: no wreckage at all",
      AF.wreckage_objects(B738) == set(), names(AF.wreckage_objects(B738)))
for path in (C172, SEAPLANE, F4, MD80, ASK21, S76, SF50):
    shared = AF.wreckage_objects(path) & AF.cockpit_objects(path)
    check("%s: no object is both" % os.path.basename(path), not shared,
          names(shared))

# 2) a real import -------------------------------------------------------------
bpy.ops.import_scene.xplane_acf(filepath=ASK21)
scene = bpy.context.scene
view_layer = bpy.context.view_layer
aircraft = bpy.data.collections[os.path.basename(ASK21)]
groups = [c for c in aircraft.children if c.name.startswith(addon.WRECKAGE_GROUP)]
check("one Wreckage collection under the aircraft", len(groups) == 1,
      [c.name for c in aircraft.children])
wreck = groups[0] if groups else None


def layer_coll(coll):
    return addon._layer_collection(view_layer, coll)


def parent_of(coll):
    for owner in bpy.data.collections:
        if owner.children.get(coll.name) is not None:
            return owner
    return None


if wreck is not None:
    want = names(AF.wreckage_objects(ASK21))
    check("it holds the 4 wreckage objects", names([c.name for c in wreck.children]) == want,
          sorted(c.name for c in wreck.children))
    check("all of them are orange",
          all(c.color_tag == addon.WRECKAGE_TAG for c in wreck.children),
          sorted({c.color_tag for c in wreck.children}))
    check("the intact parts were not moved into it",
          parent_of(bpy.data.collections["fuse.obj"]) is aircraft,
          parent_of(bpy.data.collections["fuse.obj"]).name)
    check("excluded from the view layer", layer_coll(wreck).exclude is True)
    gone = [ob.name for ob in bpy.data.objects
            if ob.name.startswith("wreck_") and ob.name in view_layer.objects]
    check("its objects left the view layer", not gone, gone)

    files = [f.filename for f in
             XF.createFilesFromBlenderRootObjects(scene, view_layer, False)]
    check("XP2B exports 24 - 4 = 20 files", len(files) == 20,
          "%d files: %s" % (len(files), "wreck" in str(files)))
    check("...and not one of them is the wreckage",
          not [f for f in files if "wreck" in f], files)

    roots = addon._collection_roots(aircraft)
    check("Place by .acf still walks into the wreckage",
          any(ob.name.startswith("wreck_") for ob in roots),
          "%d root object(s)" % len(roots))

    layer_coll(wreck).exclude = False
    files = [f.filename for f in
             XF.createFilesFromBlenderRootObjects(scene, view_layer, False)]
    check("re-enabled: 24 files again", len(files) == 24, len(files))

print()
if fails:
    print("RESULT: FAIL")
    for name in fails:
        print("  - " + name)
    sys.exit(1)
print("RESULT: PASS")
