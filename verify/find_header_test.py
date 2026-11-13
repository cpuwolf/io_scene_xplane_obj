"""The four `_find_header` call sites, on a real import.

    Blender -b --factory-startup --python verify/find_header_test.py -- <fixture.obj>

Guards the regression the reviewer hit: the helper was deleted in 10586b6 while
the N-panel draw, the dataref picker, "Animate Part" and "Convert selection"
kept calling it, so every one of those raised NameError.  The import itself
converts to an XP2B scene at the end (`_build_xp2b`), so the call sites are
exercised on the state they are drawn in: the imported scene before the
conversion, then the conversion operator itself.
"""
import importlib.util
import os
import sys

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
FIXTURE = argv[0] if argv else os.path.join(REPO, "verify", "fixtures", "nested.obj")

fail = []


def check(label, ok, detail=""):
    print("%-46s %s %s" % (label, "PASS" if ok else "FAIL", detail), flush=True)
    if not ok:
        fail.append(label)


class Layout:
    """Enough of `bpy.types.UILayout` for a panel's draw() to run."""

    def __getattr__(self, _):
        def call(*a, **k):
            return self
        return call

    def __getitem__(self, _):
        return self


class Ctx:
    def __init__(self, active):
        self.active_object = active
        self.selected_objects = [active] if active else []
        self.collection = bpy.context.scene.collection


spec = importlib.util.spec_from_file_location(
    "xpobj_src", os.path.join(REPO, "__init__.py"),
    submodule_search_locations=[REPO])
M = importlib.util.module_from_spec(spec)
sys.modules["xpobj_src"] = M
spec.loader.exec_module(M)

check("_find_header is declared", callable(getattr(M, "_find_header", None)))

# the shipped package gets the exporter from its wheel; the source tree gets the
# same code from the submodule that wheel is built from
sys.path.insert(0, os.path.join(REPO, "vendor", "XPlane2Blender"))
bpy.ops.preferences.addon_enable(module="io_xplane2blender")
M.register()

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)

made, tris, headers = M._import_objects(
    bpy.context, [FIXTURE], lambda *a: print("report:", *a), None)
check("import keeps a header per file", bool(headers), "%d" % len(headers))

parts = [o for o in bpy.data.objects if M.XM.is_batch(o)]
check("the fixture has batch parts", bool(parts), "%d" % len(parts))
if not (headers and parts):
    print("RESULT: FAIL nothing to exercise the call sites on", flush=True)
    raise SystemExit(1)

header, part = headers[0], parts[0]

check("_find_header walks to the header", M._find_header(part) is not None,
      "%s -> %s" % (part.name, getattr(M._find_header(part), "name", None)))
check("_find_header(None) is None", M._find_header(None) is None)

# the dataref picker (an EnumProperty callback) used to die on NameError
try:
    items = M._pick_datarefs(None, Ctx(part))
    check("_pick_datarefs returns items", isinstance(items, list),
          "%d item(s)" % len(items))
except Exception as exc:                                    # noqa: BLE001
    check("_pick_datarefs returns items", False, repr(exc))

# "Animate Part" - an operator whose execute() starts with _find_header
bpy.context.view_layer.objects.active = part
for ob in bpy.data.objects:
    ob.select_set(False)
part.select_set(True)
try:
    res = bpy.ops.object.xp8_anim_rig()
    check("object.xp8_anim_rig runs", res == {'FINISHED'}, str(res))
except Exception as exc:                                    # noqa: BLE001
    check("object.xp8_anim_rig runs", False, repr(exc))

# the N-panel: a NameError here fires on every redraw of the sidebar
try:
    # the panel class itself cannot be instantiated from Python (bpy_struct);
    # draw() only reads self.layout
    class Panel:
        layout = Layout()
    M.VIEW3D_PT_xplane_obj.draw(Panel(), Ctx(part))
    check("the N-panel draws", True)
except Exception as exc:                                    # noqa: BLE001
    check("the N-panel draws", False, repr(exc))

# "Convert selection" - the operator that builds the XP2B scene
bpy.context.view_layer.objects.active = part
for ob in bpy.data.objects:
    ob.select_set(False)
part.select_set(True)
try:
    res = bpy.ops.object.xp8_to_xp2b()
    check("object.xp8_to_xp2b runs", res == {'FINISHED'}, str(res))
except Exception as exc:                                    # noqa: BLE001
    check("object.xp8_to_xp2b runs", False, repr(exc))

print("RESULT: %s" % ("PASS" if not fail else "FAIL " + ", ".join(fail)), flush=True)
