"""Load the built zip the way Blender does: register, import a fixture, export it.

    Blender -b --factory-startup --python verify/bundle_smoke.py -- <zip> [fixture.obj]

`blender --command extension validate` only reads the package structure, and it
passes on a zip that cannot run: excluding `xplane2blender/tests/` from the
package is well-formed and then fails at registration with
`No module named '...xplane2blender.tests'`, because `xplane_file.py` and
`xplane_wiper_gradient.py` import `test_creation_helpers` at runtime.  So this
grades the artifact instead of the source tree: unzip it, import the add-on by
path, and make it import and export one fixture.

The last line is the only signal to grep for:

    RESULT: PASS   or   RESULT: FAIL <why>
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import zipfile

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if not argv:
    print("usage: -- <zip> [fixture.obj]")
    raise SystemExit(1)

ZIP = argv[0]
FIXTURE = argv[1] if len(argv) > 1 else os.path.join(
    REPO, "verify", "fixtures", "nested.obj")


def fail(reason):
    print("RESULT: FAIL %s" % reason, flush=True)
    raise SystemExit(1)


if not os.path.isfile(ZIP):
    fail("no zip at %s" % ZIP)

work = tempfile.mkdtemp(prefix="xp8_smoke_")
try:
    with zipfile.ZipFile(ZIP) as zf:
        zf.extractall(work)
    if not os.path.isfile(os.path.join(work, "__init__.py")):
        fail("the zip has no __init__.py at its root")
    print("zip: %s (%d files, %.0f KB)"
          % (os.path.basename(ZIP), len(zipfile.ZipFile(ZIP).namelist()),
             os.path.getsize(ZIP) / 1024.0), flush=True)

    spec = importlib.util.spec_from_file_location(
        "xp8_smoke", os.path.join(work, "__init__.py"),
        submodule_search_locations=[work])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["xp8_smoke"] = mod
    spec.loader.exec_module(mod)
    mod.register()

    from xp8_smoke import xp2b as XB
    if not XB.available():
        fail("the bundled exporter did not register (Collection has no .xplane)")
    if not XB.bone_support():
        fail("the bundled exporter is not the Blender 5.x armature build")
    if not hasattr(bpy.ops.export_scene, "xplane"):
        fail("no export_scene.xplane operator after registering")
    print("registered: export operator, bone datarefs, layer properties",
          flush=True)

    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)

    res = bpy.ops.import_scene.xplane_obj('INVOKE_DEFAULT', filepath=FIXTURE)
    colls = [c for c in bpy.data.collections
             if getattr(getattr(c, "xplane", None),
                        "is_exportable_collection", False)]
    if res != {'FINISHED'} or not colls:
        fail("import returned %s with %d exportable collection(s)"
             % (res, len(colls)))
    print("imported %s: %d collection(s)"
          % (os.path.basename(FIXTURE), len(colls)), flush=True)

    from xp8_smoke.xplane2blender.xplane_types import xplane_file as XF
    files = XF.createFilesFromBlenderRootObjects(bpy.context.scene,
                                                 bpy.context.view_layer)
    if not files:
        fail("the exporter found no root to write")
    anims = tris = 0
    for f in files:
        text = f.write()
        if not text:
            fail("%s wrote nothing (its own validation cancelled the export)"
                 % getattr(f, "filename", "?"))
        anims += sum(1 for line in text.splitlines()
                     if line.lstrip().startswith("ANIM_"))
        tris += sum(1 for line in text.splitlines() if "TRIS" in line)
    if tris == 0:
        fail("no TRIS line in the exported text")
    if anims == 0:
        fail("no ANIM_ line in the exported text - the export is silent about "
             "losing every animation, which is the failure this catches")
    print("exported %d file(s): %d ANIM_ line(s), %d TRIS line(s)"
          % (len(files), anims, tris), flush=True)

    # A clean fixture exports without logging anything, so a crash *in the
    # logging path* is invisible here - that is exactly how a build shipped
    # where the first message the exporter logged died with UnboundLocalError
    # (the counter was reachable only as a package attribute upstream). Exercise
    # both: the message itself, and the error the exporter raises for a mesh
    # with no material.
    from xp8_smoke.xplane2blender.xplane_helpers import XPlaneLogger
    try:
        XPlaneLogger.messageToString("info", "smoke", None)
        XPlaneLogger.messageToString("error", "smoke", None)
    except Exception as exc:                              # noqa: BLE001
        fail("logging a message raised %r - the export dies on the first line "
             "it has to report" % exc)
    print("logger: messageToString() survives a message", flush=True)

    me = bpy.data.meshes.new("smoke_nomat")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    me.update()
    ob = bpy.data.objects.new("smoke_nomat", me)
    colls[0].objects.link(ob)
    try:
        XF.createFilesFromBlenderRootObjects(bpy.context.scene,
                                             bpy.context.view_layer)
    except Exception as exc:                              # noqa: BLE001
        fail("a mesh with no material crashed the exporter: %r" % exc)
    print("a material-less mesh is reported, not crashed", flush=True)

    # Installing a version from disk is unregister + register in one process
    # (Blender does not unload the module, and XPlane2Blender's property groups
    # survive its own unregister), so the exporter has to come back without a
    # restart. It did not: the guard read the leftover properties, decided an
    # exporter was already there, and Export stayed missing until Blender quit.
    mod.unregister()
    mod.register()
    if not hasattr(bpy.types, "EXPORT_OT_xplane_obj"):
        fail("re-registering the add-on left no exporter - a version installed "
             "from disk would need a Blender restart before Export returns")
    print("re-registered: the exporter comes back without a restart", flush=True)

    mod.unregister()
    print("RESULT: PASS", flush=True)
finally:
    shutil.rmtree(work, ignore_errors=True)
