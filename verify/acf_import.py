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

def is_exportable(coll):
    """`xplane` does not exist on a Collection while XP2B is disabled."""
    return getattr(getattr(coll, "xplane", None),
                   "is_exportable_collection", False)


exportable = [c for c in bpy.data.collections if is_exportable(c)]
meshes = [o for o in bpy.data.objects if o.type == 'MESH']
arms = [o for o in bpy.data.objects if o.type == 'ARMATURE']

# one parent collection per .acf, named after the file, holding the per-object
# exportable collections: that is the tree the user manages an aircraft in
parents = [c for c in bpy.data.collections if c.name.lower().endswith(".acf")]


def child_names(coll):
    return [c.name for c in coll.children]


def parent_of(coll, parents):
    for parent in parents:
        if coll.name in child_names(parent):
            return parent.name
    return None


print("\nresult: %s in %.1fs" % (result, elapsed), flush=True)
print("collections: %d (%d exportable, %d .acf parent)"
      % (len(bpy.data.collections), len(exportable), len(parents)), flush=True)
for coll in parents:
    print("  parent %-32s exportable=%s children=%d"
          % (coll.name, is_exportable(coll), len(coll.children)), flush=True)
for coll in exportable:
    owned = [o for o in coll.objects if o.type == 'MESH']
    layer = getattr(coll.xplane, "layer", None)
    print("  %-40s meshes=%-4d layer=%-24s parent=%s"
          % (coll.name, len(owned), getattr(layer, "name", "?"),
             parent_of(coll, parents)), flush=True)
print("meshes=%d armatures=%d" % (len(meshes), len(arms)), flush=True)

if error:
    print("error: %s" % error, flush=True)

# every imported .acf has its collection, and every exportable layer sits under
# exactly that collection (not at the scene root)
named = [c for c in parents
         if c.name in [os.path.basename(a) for a in ACFS]]
nested = [c for c in exportable if parent_of(c, named)]
print("acf parents matching the input: %d, exportable under one: %d/%d"
      % (len(named), len(nested), len(exportable)), flush=True)

# nesting must not hide a root from the exporter: XP2B walks collections
# recursively, so it has to find every layer inside the .acf collection
found_files = None
if WANT_XP2B and exportable:
    try:
        from io_xplane2blender.xplane_types import xplane_file as XF
        xplane_files = XF.createFilesFromBlenderRootObjects(
            bpy.context.scene, bpy.context.view_layer)
        found_files = [getattr(f, "filename", None) or f.layer_props.name
                       for f in xplane_files]
        print("xp2b finds %d root file(s): %s"
              % (len(found_files), found_files), flush=True)
    except Exception as exc:                              # noqa: BLE001
        print("xp2b root scan failed: %s" % exc, flush=True)

if WANT_XP2B:
    ok = (result == {'FINISHED'} and meshes and exportable
          and len(named) >= 1 and len(nested) == len(exportable)
          and (not exportable or len(found_files or []) == len(exportable)))
else:
    # without XP2B the import still has to land the geometry and say why there
    # is no exportable collection, instead of failing silently
    ok = bool(meshes) and "XPlane2Blender" in error and len(named) >= 1
print("\nRESULT: %s" % ("PASS" if ok else "FAIL"), flush=True)
