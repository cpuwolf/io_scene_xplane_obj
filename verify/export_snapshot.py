"""Snapshot what XP2B writes for a file, so a change can be diffed byte for byte.

    <blender> -b --factory-startup --python verify/export_snapshot.py -- <in.obj> <out.txt>

Reads the file through the add-on's own import (no probe transforms), then runs
XP2B's write() and dumps the text.  Diff the two dumps with `diff`, not by
eyeballing: the light enrichment is only acceptable if this text never changes.

lights.txt is parsed explicitly: in a `-b` run XP2B's load_handler never fires,
so every param light name would look "unknown" and the autocorrect path this
snapshot is supposed to grade would never execute.
"""
import os
import sys

import bpy
import addon_utils

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

addon_utils.enable("io_xplane2blender", default_set=True)
import io_scene_xplane_obj as IMP                           # noqa: E402

try:
    IMP.register()
except Exception:                                           # noqa: BLE001
    pass

from io_xplane2blender.xplane_helpers import logger          # noqa: E402
from io_xplane2blender.xplane_types import xplane_file as XF  # noqa: E402
from io_xplane2blender.xplane_utils import xplane_lights_txt_parser as LP  # noqa: E402

LP.parse_lights_file()
logger.addTransport(lambda t, m, c=None: print("XP2B %s: %s" % (t.upper(), m)),
                    ["error", "warning"])

argv = sys.argv[sys.argv.index("--") + 1:]
src, dst = argv[0], argv[1]

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
for coll in list(bpy.data.collections):
    bpy.data.collections.remove(coll)
for mat in list(bpy.data.materials):
    bpy.data.materials.remove(mat)

bpy.ops.import_scene.xplane_obj(filepath=src)

logger.clearMessages()
files = XF.createFilesFromBlenderRootObjects(bpy.context.scene, bpy.context.view_layer)
text = files[0].write() if files else ""
for m in logger.messages:
    print("MSG[%s] %s" % (m["type"], m["message"]))

with open(dst, "w") as f:
    f.write(text)
print("WROTE %s chars=%d lines=%d" % (dst, len(text), text.count("\n")))
