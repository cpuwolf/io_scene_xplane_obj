# Distributed as a Blender *extension*: the metadata lives in
# blender_manifest.toml, so no legacy bl_info dict anywhere in the package
# (its presence would make Blender ignore the manifest).

"""
X-Plane OBJ8 object importer/exporter for Blender.

    File > Import > X-Plane OBJ8 (.obj)
    File > Export > X-Plane OBJ8 (.obj)

What is imported
----------------
One OBJ8 file contains ONE texture set (albedo + _LIT + normal) and an ordered
command stream: attributes, geometry, animations, manipulators, lights.  This
addon turns

    one OBJ file  ->  one Empty ("header") + one mesh per geometry command

into a normal Blender scene, and puts it back together on export:

  * the TEXTURE / TEXTURE_LIT / TEXTURE_NORMAL maps become a Principled
    material (base colour, emission, normal map), with the original relative
    paths remembered so a `../../objects/x.png` name survives the trip;
  * every VT row becomes a vertex with its authored normal attached as a
    custom split normal -- X-Plane stores lighting normals per vertex and they
    are NOT the averaged face normals Blender would invent;
  * the ground truth for everything else (ANIM_begin/end, ANIM_rotate/trans,
    ATTR_manip_*, LIGHT_PARAM, GLOBAL_*, ATTR_LOD, ...) is kept verbatim on the
    header Empty and re-emitted around the geometry, so editing a mesh does not
    silently delete the aircraft's animations;
  * a file you did not touch is written back byte for byte.

Coordinate system
-----------------
X-Plane is right-handed with +Y up, +Z south; Blender is right-handed with +Z
up.  The mapping is a +90 degree turn about X, so nothing is mirrored.  What
does need flipping is the winding: X-Plane's front face is CLOCKWISE, Blender's
is counter-clockwise.

What is NOT supported
---------------------
Lines (VLINE + LINES) are imported read-only and written back from their stored
indices; light primitives are preserved as raw commands.  There is no
manipulator authoring UI: manipulator commands survive an export, you just
cannot create new ones here.  Use XPlane2Blender when you need to author
animations/manipulators from scratch; use this when you need to get an existing
object into Blender, change it, and get a valid object back out.
"""

import os
import glob

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import objcodec as OC
from . import xpmesh as XM

CLASSES = []


def _add(cls):
    CLASSES.append(cls)
    return cls


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

@_add
class IMPORT_OT_xplane_obj(bpy.types.Operator, ImportHelper):
    """Import an X-Plane OBJ8 object (.obj) with its textures"""
    bl_idname = "import_scene.xplane_obj"
    bl_label = "X-Plane OBJ8 (.obj)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".obj"
    filter_glob: StringProperty(default="*.obj", options={'HIDDEN'})

    import_folder: BoolProperty(
        name="Import whole folder",
        description="Import every .obj sitting next to the selected file",
        default=False)

    place_parts: BoolProperty(
        name="Place animated parts",
        description="Parts modelled at the file origin and moved into place by "
                    "a static ANIM_trans (landing gear legs, wipers) are moved "
                    "to their real position so they can be edited there. "
                    "Export subtracts it again, so the file stays unchanged",
        default=True)

    def execute(self, context):
        path = self.filepath
        if not os.path.isfile(path):
            self.report({'ERROR'}, "not a file: %s" % path)
            return {'CANCELLED'}

        paths = [path]
        if self.import_folder:
            folder = os.path.dirname(path)
            paths = sorted(glob.glob(os.path.join(folder, "*.obj")))
            if not paths:
                self.report({'ERROR'}, "no .obj in %s" % folder)
                return {'CANCELLED'}

        coll = None
        if len(paths) > 1:
            folder = os.path.dirname(paths[0])
            name = os.path.basename(folder.rstrip(os.sep)) or "X-Plane objects"
            coll = bpy.data.collections.new(name)
            context.scene.collection.children.link(coll)

        made = 0
        tri_count = 0
        for p in paths:
            try:
                header = XM.import_file(context, p, into_collection=coll,
                                        report=self.report,
                                        place_parts=self.place_parts)
            except (ValueError, OSError) as exc:
                self.report({'ERROR'}, "%s: %s" % (os.path.basename(p), exc))
                continue
            if header is None:
                continue
            made += 1
            for child in header.children:
                if child.type == 'MESH' and child.get(XM.PROP_ROLE) == XM.ROLE_TRIS:
                    tri_count += len(child.data.loop_triangles) or (
                        len(child.data.polygons))
            if coll is not None:
                header.hide_viewport = True

        if not made:
            self.report({'ERROR'}, "nothing imported")
            return {'CANCELLED'}
        self.report({'INFO'}, "imported %d file(s), %d triangles"
                    % (made, tri_count))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@_add
class EXPORT_OT_xplane_obj(bpy.types.Operator, ExportHelper):
    """Export an X-Plane OBJ8 object (.obj)"""
    bl_idname = "export_scene.xplane_obj"
    bl_label = "X-Plane OBJ8 (.obj)"
    bl_options = {'REGISTER'}

    filename_ext = ".obj"
    filter_glob: StringProperty(default="*.obj", options={'HIDDEN'})
    check_existing: BoolProperty(default=False)

    mode: EnumProperty(
        name="Write",
        items=[
            ('FILE', "One file",
             "Write the selected X-Plane object, or the selected parts / "
             "meshes, to the file name below"),
            ('WRITE_BACK', "Back to source files",
             "Write every selected X-Plane object back to the path it came "
             "from (or into the folder below)"),
        ],
        default='FILE')

    preserve_unmodified: BoolProperty(
        name="Copy untouched files verbatim",
        description="If no geometry changed, write the original file text "
                    "instead of regenerating it",
        default=True)

    uv_layer: StringProperty(
        name="UV map", default="",
        description="UV layer to export (empty = active layer)")

    patch_obj: BoolProperty(
        name="Write .patch.obj",
        description="Also write <name>.patch.obj next to the file: the variant "
                    "Ortho4XP reads as a mesh patch (ANCHOR on line 1, geometry "
                    "only). The ANCHOR longitude/latitude is taken from the "
                    "Ortho4XP tile in the scene, its altitude and heading are 0. "
                    "This file is not a valid X-Plane object",
        default=False)

    def invoke(self, context, event):
        obj = context.active_object
        header = _find_header(obj)
        if header is not None:
            src = header.get(XM.PROP_SOURCE)
            if src:
                parts = self._selection(context).get(header)
                if parts is not None:
                    # a subset must not default to overwriting the whole file
                    self.filepath = os.path.splitext(src)[0] + "_selected.obj"
                else:
                    self.filepath = src
        self.mode = 'FILE'
        return ExportHelper.invoke(self, context, event)

    def execute(self, context):
        if self.mode == 'WRITE_BACK':
            return self._write_back(context)
        return self._write_one(context)

    def _patch_path(self, dst):
        if not self.patch_obj:
            return None
        return os.path.splitext(dst)[0] + ".patch.obj"

    def _selection(self, context):
        """What is selected right now: {header: None | [part, ...]}.

        ``None`` means the header Empty itself is selected, i.e. the whole
        object; a list holds the parts selected under it.  Objects outside an
        imported X-Plane object (a loose mesh) never appear here.
        """
        out = {}
        for ob in context.selected_objects:
            header = _find_header(ob)
            if header is None:
                continue
            if ob is header:
                out[header] = None          # the object itself: every part
            elif header in out:
                if out[header] is not None:
                    out[header].append(ob)
            else:
                out[header] = [ob]
        return out

    def _scene_headers(self, context):
        return [ob for ob in context.scene.objects if XM.is_header(ob)]

    def _write_back(self, context):
        picked = self._selection(context)
        if not picked:
            if context.selected_objects:
                self.report({'ERROR'}, "the selection holds no imported "
                                       "X-Plane object")
                return {'CANCELLED'}
            # nothing selected: every imported object in the scene
            picked = dict((h, None) for h in self._scene_headers(context))
        if not picked:
            self.report({'ERROR'}, "no imported X-Plane objects in this scene")
            return {'CANCELLED'}
        target_dir = os.path.dirname(self.filepath)
        ok = verbatim = 0
        for header, parts in picked.items():
            src = header.get(XM.PROP_SOURCE) or ""
            if src and os.path.isdir(os.path.dirname(src)):
                dst = src
            elif target_dir:
                dst = os.path.join(target_dir,
                                   os.path.basename(src or header.name + ".obj"))
            else:
                continue
            try:
                stats = XM.export_header(
                    context, header, dst,
                    uv_name=self.uv_layer or None,
                    preserve=self.preserve_unmodified,
                    report=self.report,
                    patch_path=self._patch_path(dst),
                    only=None if parts is None else [ob.name for ob in parts])
            except (OSError, ValueError) as exc:
                self.report({'ERROR'}, "%s: %s" % (os.path.basename(dst), exc))
                continue
            if stats:
                ok += 1
                verbatim += 1 if stats["verbatim"] else 0
        self.report({'INFO'}, "wrote %d file(s), %d unchanged" % (ok, verbatim))
        return {'FINISHED'}

    def _write_one(self, context):
        picked = self._selection(context)
        selected = list(context.selected_objects)
        if not picked and not selected:
            # nothing selected: the whole scene.  One object can be written
            # with its command stream; several can only be flattened into one
            # file (an .obj is one header + its batches).
            headers = self._scene_headers(context)
            if len(headers) == 1:
                picked = {headers[0]: None}
            elif headers:
                self.report({'WARNING'},
                            "nothing selected: %d objects in the scene go into "
                            "one file, their command streams (animation, "
                            "manipulators) are dropped. Select one object to "
                            "keep them" % len(headers))
        header = None
        if picked:
            header = _find_header(context.active_object)
            if header not in picked:
                header = next(iter(picked))
            assert header is not None           # picked is not empty here
            if len(picked) > 1:
                self.report({'WARNING'},
                            "selection spans %d objects; one file per export, "
                            "writing %s only" % (len(picked), header.name))
            parts = picked[header]
            try:
                stats = XM.export_header(
                    context, header, self.filepath,
                    uv_name=self.uv_layer or None,
                    preserve=self.preserve_unmodified,
                    report=self.report,
                    patch_path=self._patch_path(self.filepath),
                    only=None if parts is None else [ob.name for ob in parts])
            except (OSError, ValueError) as exc:
                self.report({'ERROR'}, str(exc))
                return {'CANCELLED'}
            self.report({'INFO'}, "%s: %d VT rows, %d indices%s"
                        % (os.path.basename(self.filepath), stats["vt"],
                           stats["indices"],
                           " (verbatim)" if stats["verbatim"] else ""))
            return {'FINISHED'}

        meshes = [ob for ob in selected if ob.type == 'MESH']
        if not meshes and not selected:
            # nothing selected at all: every mesh in the scene
            meshes = [ob for ob in context.scene.objects if ob.type == 'MESH']
        if not meshes:
            self.report({'ERROR'}, "no mesh objects to export")
            return {'CANCELLED'}
        try:
            stats = XM.export_unparented_objects(
                context, self.filepath, meshes,
                uv_name=self.uv_layer or None, report=self.report,
                patch_path=self._patch_path(self.filepath))
        except (OSError, ValueError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        if stats is None:
            return {'CANCELLED'}
        self.report({'INFO'}, "%s: %d meshes, %d VT rows"
                    % (os.path.basename(self.filepath), stats["objects"],
                       stats["vt"]))
        return {'FINISHED'}


def _find_header(obj):
    """Walk up to the header Empty that owns `obj`."""
    hops = 0
    while obj is not None and hops < 32:
        if XM.is_header(obj):
            return obj
        obj = obj.parent
        hops += 1
    return None


# ---------------------------------------------------------------------------
# side panel: what am I looking at
# ---------------------------------------------------------------------------

@_add
class VIEW3D_PT_xplane_obj(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "X-Plane"
    bl_label = "OBJ8 Object"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        header = _find_header(obj)
        if obj is None or header is None:
            layout.label(text="Select an imported object", icon='INFO')
            return

        box = layout.box()
        box.label(text=os.path.basename(header.get(XM.PROP_SOURCE, "?")) or
                  header.name, icon='FILE')
        col = box.column(align=True)
        col.label(text="parts: %d" % len(header.children))
        globals_lines = _json_list(header.get(XM.PROP_GLOBALS, "[]"))
        for line in globals_lines:
            words = line.split()
            if words and words[0] in ("TEXTURE", "TEXTURE_LIT",
                                      "TEXTURE_NORMAL", "POINT_COUNTS",
                                      "BLEND_GLASS", "NORMAL_METALNESS"):
                col.label(text=line[:48], icon='TEXTURE'
                          if words[0].startswith("TEXTURE") else 'DOT')

        if XM.is_batch(obj):
            part = layout.box()
            part.label(text="%s  (command %s)"
                       % (obj.name, obj.get(XM.PROP_LEAF, "?")))
            info = part.column(align=True)
            info.label(text="triangles: %d" % len(obj.data.polygons))
            info.label(text="vertices: %d" % len(obj.data.vertices))
            if obj.data.materials:
                info.label(text="material: %s" % obj.data.materials[0].name)

        warnings = _json_list(header.get("xp8_warnings", "[]"))
        if warnings:
            wbox = layout.box()
            wbox.label(text="import warnings", icon='ERROR')
            for w in warnings[:8]:
                wbox.label(text=w[:56])


def _json_list(raw):
    import json
    try:
        return list(json.loads(raw))
    except (ValueError, TypeError):
        return []


# ---------------------------------------------------------------------------
# menus / registration
# ---------------------------------------------------------------------------

def _menu_import(self, context):
    self.layout.operator(IMPORT_OT_xplane_obj.bl_idname, text="X-Plane OBJ8 (.obj)")


def _menu_export(self, context):
    self.layout.operator(EXPORT_OT_xplane_obj.bl_idname, text="X-Plane OBJ8 (.obj)")


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
