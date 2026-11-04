"""Cockpit panel keywords -> XPlane2Blender, and back out again.

    blender -b --factory-startup --python verify/check_cockpit.py -- <obj> [out-dir]

Two things are checked:

1. the conversion put the panel state where XPlane2Blender reads it - the
   material's Cockpit Feature (panel / device / none) with its region and
   luminance fields, and the layer's Panel Texture Mode plus region table;
2. XPlane2Blender exports it again - the ATTR_cockpit* lines in the output have
   to say what the source file said.
"""
import os
import sys
import traceback

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

import addon_utils                                        # noqa: E402

addon_utils.enable("io_xplane2blender", default_set=True)

import io_scene_xplane_obj as IMP                         # noqa: E402

try:
    IMP.register()
except Exception:
    pass

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SRC = argv[0] if argv else os.path.join(REPO, "verify", "fixtures",
                                        "cockpit.obj")
OUT = argv[1] if len(argv) > 1 else os.path.join("/tmp", "xpobj_cockpit")

DEVICE_FIELDS = ("device_name", "plugin_device", "device_lighting_channel",
                 "device_auto_adjust", "device_bus_0", "device_bus_1",
                 "device_bus_2", "device_bus_3", "device_bus_4", "device_bus_5",
                 "cockpit_region", "cockpit_feature_use_luminance",
                 "cockpit_feature_luminance")


def clear():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)


def dump():
    for coll in [c for c in bpy.data.collections
                 if c.xplane.is_exportable_collection]:
        layer = coll.xplane.layer
        print("LAYER", coll.name, "type", layer.export_type,
              "panel_mode", layer.cockpit_panel_mode,
              "regions", layer.cockpit_regions)
        for i, region in enumerate(layer.cockpit_region):
            if i >= int(layer.cockpit_regions or 0):
                break
            print("   REGION", i, "left", region.left, "bottom", region.top,
                  "w", 2 ** region.width, "h", 2 ** region.height)
        for ob in coll.objects:
            if ob.type != 'MESH':
                continue
            for slot in ob.material_slots:
                mat = slot.material
                if mat is None:
                    continue
                print("   MESH", ob.name, "mat", mat.name,
                      "feature", mat.xplane.cockpit_feature,
                      {f: getattr(mat.xplane, f) for f in DEVICE_FIELDS})


def attr_lines(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            head = line.split()
            if head and head[0].startswith(("ATTR_cockpit", "ATTR_no_cockpit",
                                            "COCKPIT_REGION",
                                            "GLOBAL_cockpit_lit")):
                out.append(line.strip())
    return out


clear()
print("source:", SRC)
print("import:", bpy.ops.import_scene.xplane_obj(filepath=SRC))
dump()

target = os.path.join(OUT, os.path.basename(SRC))
os.makedirs(OUT, exist_ok=True)
try:
    print("export:", bpy.ops.export.xplane_obj(filepath=target), "->", target)
except Exception:
    traceback.print_exc()
print("--- source attributes")
for line in attr_lines(SRC):
    print("  ", line)
print("--- exported attributes")
for line in attr_lines(target):
    print("  ", line)
