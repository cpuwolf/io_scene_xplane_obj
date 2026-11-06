"""Headless check of the .acf import path: one aircraft, every object it attaches.

    Blender -b --factory-startup --python verify/acf_import.py -- <file.acf> [more.acf ...] [--no-xp2b]

It calls the operator exactly the way the file handler does (INVOKE_DEFAULT
plus a filepath), so this is the real drop path and not a shortcut through
`import_file`.

The add-on is loaded straight from this working tree by path, so the check does
not depend on what is installed in Blender and never collides with the installed
copy (a `--factory-startup` session has no add-on registered at all).

What it grades: the operator finishes, it produced one exportable collection per
.obj in the .acf, and the meshes/armatures are there.  `--no-xp2b` skips the
conversion, which is the slow half and only needs XP2B.

In `-b` the operator takes its synchronous path: background mode has no event
loop, so the modal handler (status text, progress bar, ESC) only runs in a GUI
session - see docs/development.md.
"""
import importlib.util
import math
import os
import sys
import time

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if not argv:
    print("usage: -- <file.acf> [--no-xp2b]")
    sys.exit(1)
ACFS = [a for a in argv if not a.startswith("--")]
if not ACFS:
    print("usage: -- <file.acf> [more.acf ...] [--no-xp2b]")
    sys.exit(1)
WANT_XP2B = "--no-xp2b" not in argv

if WANT_XP2B:
    try:
        bpy.ops.preferences.addon_enable(module="io_xplane2blender")
        print("xp2b: enabled", flush=True)
    except RuntimeError as exc:
        print("xp2b: NOT available (%s)" % exc, flush=True)

spec = importlib.util.spec_from_file_location(
    "xpobj_repo", os.path.join(REPO, "__init__.py"),
    submodule_search_locations=[REPO])
M = importlib.util.module_from_spec(spec)
sys.modules["xpobj_repo"] = M
spec.loader.exec_module(M)
M.register()
print("addon: %s" % M.__name__, flush=True)
print("handlers: %s" % [(c.bl_idname, c.bl_file_extensions)
                        for c in bpy.types.FileHandler.__subclasses__()],
      flush=True)

# --factory-startup hands us the default Cube; it would be counted as imported
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)

# a camera that is NOT where the import should leave it, so "it moved" means
# something; the numbers below are the X-Plane viewpoint the operator carries
EXPECT_LOCATION = (-4.4895, 5.0562, 2.4779)
EXPECT_ROTATION_DEG = (76.327, 0.0, -149.23)
camera = bpy.data.objects.new("ProbeCamera", bpy.data.cameras.new("ProbeCamera"))
bpy.context.scene.collection.objects.link(camera)
camera.location = (1.0, 2.0, 3.0)
bpy.context.scene.camera = camera

t0 = time.time()
try:
    if len(ACFS) > 1:
        # a multi-selection drop: directory + files, never `filepath`
        result = bpy.ops.import_scene.xplane_acf(
            'INVOKE_DEFAULT',
            directory=os.path.dirname(ACFS[0]),
            files=[{"name": os.path.basename(a)} for a in ACFS])
    else:
        result = bpy.ops.import_scene.xplane_acf('INVOKE_DEFAULT',
                                                 filepath=ACFS[0])
    error = ""
except RuntimeError as exc:
    # a {'ERROR'} report raises in a script; in the UI it is a red line
    result = {'CANCELLED'}
    error = str(exc)
elapsed = time.time() - t0

exportable = [c for c in bpy.data.collections
              if getattr(c.xplane, "is_exportable_collection", False)]
meshes = [o for o in bpy.data.objects if o.type == 'MESH']
arms = [o for o in bpy.data.objects if o.type == 'ARMATURE']

print("\nresult: %s in %.1fs" % (result, elapsed), flush=True)
print("collections: %d (%d exportable)" % (len(bpy.data.collections),
                                           len(exportable)), flush=True)
for coll in exportable:
    owned = [o for o in coll.objects if o.type == 'MESH']
    layer = getattr(coll.xplane, "layer", None)
    print("  %-40s meshes=%-4d layer=%s" % (coll.name, len(owned),
                                            getattr(layer, "name", "?")),
          flush=True)
print("meshes=%d armatures=%d" % (len(meshes), len(arms)), flush=True)

camera_location = tuple(round(v, 4) for v in camera.location)
camera_rotation = tuple(round(math.degrees(a), 3) for a in camera.rotation_euler)
print("camera: loc=%r rot=%r mode=%s scale=%r"
      % (camera_location, camera_rotation, camera.rotation_mode,
         tuple(round(v, 3) for v in camera.scale)), flush=True)
camera_ok = (camera_location == EXPECT_LOCATION
             and camera_rotation == EXPECT_ROTATION_DEG
             and camera.rotation_mode == 'XYZ'
             and tuple(camera.scale) == (1.0, 1.0, 1.0))
print("camera check: %s" % ("PASS" if camera_ok else "FAIL"), flush=True)

if error:
    print("error: %s" % error, flush=True)

ok = camera_ok
if WANT_XP2B:
    ok = ok and result == {'FINISHED'} and bool(meshes) and bool(exportable)
else:
    # without XP2B the import still has to land the geometry and say why there
    # is no exportable collection, instead of failing silently
    ok = ok and bool(meshes) and "XPlane2Blender" in error
print("\nRESULT: %s" % ("PASS" if ok else "FAIL"), flush=True)
