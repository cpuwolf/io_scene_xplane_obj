"""Diagnose an export failure in an existing .blend.

    Blender -b <file.blend> --python verify/export_probe.py

Prints what the exporter is looking at (exportable roots, objects without a
material, armatures) and then runs the same collection + write step the export
operator does, with the full traceback instead of the one line the UI shows.
"""
import sys
import traceback

import bpy

print("file: %s" % bpy.data.filepath, flush=True)
print("blender: %s" % bpy.app.version_string, flush=True)
print("addons: %s" % [k for k in bpy.context.preferences.addons.keys()
                      if "xplane" in k], flush=True)

roots = [c for c in bpy.data.collections
         if getattr(getattr(c, "xplane", None), "is_exportable_collection",
                    False)]
print("collections: %d (%d exportable)" % (len(bpy.data.collections),
                                           len(roots)), flush=True)
for coll in roots:
    layer = getattr(coll.xplane, "layer", None)
    print("  root %-28s objects=%d layer=%s"
          % (coll.name, len(coll.all_objects),
             getattr(layer, "name", "?")), flush=True)

meshes = [o for o in bpy.data.objects if o.type == 'MESH']
no_mat = [o for o in meshes if not o.data.materials]
print("meshes=%d armatures=%d without-material=%d"
      % (len(meshes), len([o for o in bpy.data.objects if o.type == 'ARMATURE']),
         len(no_mat)), flush=True)
for ob in no_mat[:10]:
    print("  no material: %s (collection=%s)"
          % (ob.name, [c.name for c in ob.users_collection]), flush=True)

# the exporter ships as a wheel on sys.path, imported by its upstream name
from io_xplane2blender.xplane_types import xplane_file as XF  # noqa: E402

try:
    files = XF.createFilesFromBlenderRootObjects(bpy.context.scene,
                                                 bpy.context.view_layer)
except Exception:                                          # noqa: BLE001
    print("createFilesFromBlenderRootObjects raised:\n%s"
          % traceback.format_exc(), flush=True)
    files = []

print("files: %d" % len(files), flush=True)
for f in files:
    try:
        text = f.write()
    except Exception:                                      # noqa: BLE001
        print("write() raised for %s:\n%s"
              % (getattr(f, "filename", "?"), traceback.format_exc()), flush=True)
        continue
    print("  %s: %d chars, errors=%s"
          % (getattr(f, "filename", "?"), len(text or ""),
             [str(e) for e in (getattr(f, "errors", None) or [])[:8]]),
          flush=True)
    for w in (getattr(f, "warnings", None) or [])[:8]:
        print("    warning: %s" % str(w)[:140], flush=True)
