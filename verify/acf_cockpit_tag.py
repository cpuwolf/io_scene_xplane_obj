"""The .acf import green-tags the cockpit object's collection.

    <blender> -b --python verify/acf_cockpit_tag.py

X-Plane honours manipulators in an aircraft's **cockpit objects** only (the pair
Plane-Maker calls INN / OUT), so "which collection is that" is the question every
author adding a clickspot has to answer - and a name is no guide: Laminar's own
cockpit objects are called `Cockpit_screens.obj`, `COCKPIT_PANEL.obj`,
`cockpit_panel_interactive.obj`, ... .  The .acf says it plainly in
`_obja/<slot>/_obj_flags` (bit 11 = interior view, bit 12 = exterior view), so the
import reads that and colours the collection instead of guessing.

Three things have to hold:
  1. the codec resolves the right files for every .acf on hand - two files for
     b738 / A330 (separate INN and OUT objects), one file for F-4 and C90B EVO
     (the same object attached twice), none for the AI copy A330_AI
  2. a real import tags exactly that one collection, and nothing else
  3. the tag is display-only: the exported file name is untouched
"""
import glob
import importlib
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
AF = importlib.import_module(NAME + ".acfcodec")
VR = importlib.import_module(NAME + ".xpvr")
GREEN = addon.COCKPIT_TAG
BLUE = VR.BLUE_TAG

fails = []


def check(label, ok, detail=""):
    print("%-46s %s  %s" % (label, "OK  " if ok else "FAIL", detail))
    if not ok:
        fails.append(label)


check("addon loaded from the repo, not a stale copy",
      os.path.abspath(addon.__file__) == os.path.join(REPO, "__init__.py"),
      addon.__file__)

XP = os.path.expanduser("~/X-Plane 12/Aircraft")
ACFS = {os.path.relpath(p, XP): p
        for p in glob.glob(os.path.join(XP, "**", "*.acf"), recursive=True)}
if not ACFS:
    print("no .acf files under %s" % XP)
    sys.exit(1)


def acf(*parts):
    for rel, path in ACFS.items():
        if all(p in rel for p in parts):
            return path
    raise SystemExit("no .acf matching %s" % (parts,))


def names(paths):
    return sorted(os.path.basename(p) for p in paths)


# 1) the codec, against every .acf on hand -------------------------------------
B738 = acf("737-800")
A330 = acf("Airbus A330-300", "A330.acf")
A330_AI = acf("A330_AI")
F4 = acf("F-4 Phantom")
EVO = acf("C90B EVO GNS")
ASK21 = acf("ASK 21")
C172 = acf("Cessna 172 SP", "Cessna_172SP.acf")

check("b738: INN and OUT are two files",
      names(AF.cockpit_objects(B738)) == ["b738_cockpit.obj",
                                          "b738_cockpit_OUT.obj"],
      names(AF.cockpit_objects(B738)))
check("A330: same",
      names(AF.cockpit_objects(A330)) == ["A330_cockpit.obj",
                                          "A330_cockpit_OUT.obj"],
      names(AF.cockpit_objects(A330)))
check("F-4: one file, attached INN and OUT",
      names(AF.cockpit_objects(F4)) == ["F-4_cockpit.obj"],
      names(AF.cockpit_objects(F4)))
check("C90B EVO: one file, attached INN and OUT",
      names(AF.cockpit_objects(EVO)) == ["C90B_cockpit.obj"],
      names(AF.cockpit_objects(EVO)))
check("A330_AI (an AI copy): no cockpit object",
      AF.cockpit_objects(A330_AI) == set(), names(AF.cockpit_objects(A330_AI)))
check("C172: one file for both views",
      names(AF.cockpit_objects(C172)) == ["Cessna_172SP_cockpit.obj"],
      names(AF.cockpit_objects(C172)))
every = {rel: len(AF.cockpit_objects(p)) for rel, p in ACFS.items()}
check("every other .acf has exactly one cockpit object",
      all(n == 1 for rel, n in every.items()
          if rel not in (os.path.relpath(B738, XP), os.path.relpath(A330, XP),
                         os.path.relpath(A330_AI, XP))),
      ", ".join("%s=%d" % (r, n) for r, n in sorted(every.items()) if n != 1))


# 2) a real import tags that collection, and only that one ---------------------
def import_acf(path):
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    bpy.ops.import_scene.xplane_acf(filepath=path)
    green = [c for c in bpy.data.collections if c.color_tag == GREEN]
    blue = [c for c in bpy.data.collections if c.color_tag == BLUE]
    return green, blue


for path in (ASK21, F4):
    green, blue = import_acf(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    want = names(AF.cockpit_objects(path))
    check("%s: exactly one green collection" % stem, len(green) == 1,
          "%d green: %s" % (len(green), [c.name for c in green]))
    if len(green) == 1:
        coll = green[0]
        check("%s: it is the cockpit object" % stem,
              os.path.basename(coll.name) in want, coll.name)
        check("%s: green is not the VR blue" % stem, green[0] not in blue,
              "%d blue (vrconfig)" % len(blue))
        if coll.xplane.is_exportable_collection:
            check("%s: file name for export untouched" % stem,
                  coll.xplane.layer.name ==
                  os.path.splitext(os.path.basename(coll.name))[0],
                  coll.xplane.layer.name)
        else:
            check("%s: no XP2B layer to rename" % stem, True, "(conversion off)")
    check("%s: the aircraft collection itself is not tagged" % stem,
          not any(c.color_tag == GREEN for c in bpy.data.collections
                  if c.get("acf_prt_ref_from")))

print()
if fails:
    print("RESULT: FAIL")
    for name in fails:
        print("  - " + name)
    sys.exit(1)
print("RESULT: PASS")
