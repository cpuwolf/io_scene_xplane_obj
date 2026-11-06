# Distributed as a Blender *extension*: the metadata lives in
# blender_manifest.toml, so no legacy bl_info dict anywhere in the package
# (its presence would make Blender ignore the manifest).

"""
X-Plane OBJ8 object importer for Blender.

    File > Import > X-Plane OBJ8 (.obj)

It imports; XPlane2Blender exports.  The import lands as that add-on's authoring
scene, so there is no separate convert step and no exporter of our own.

What is imported
----------------
One OBJ8 file contains ONE texture set (albedo + _LIT + normal) and an ordered
command stream: attributes, geometry, animations, manipulators, lights.  This
addon turns

    one OBJ file  ->  one Empty ("header") + one mesh per geometry command

into a normal Blender scene, ready for XPlane2Blender to write back out:

  * the TEXTURE / TEXTURE_LIT / TEXTURE_NORMAL maps become a Principled
    material (base colour, emission, normal map), with the original relative
    paths remembered so a `../../objects/x.png` name survives the trip.  The
    normal map is split instead of linked straight in: X-Plane packs normal XY,
    metalness and gloss into one texture (see `xpmesh.wire_combo_material`);
  * every VT row becomes a vertex with its authored normal attached as a
    custom split normal -- X-Plane stores lighting normals per vertex and they
    are NOT the averaged face normals Blender would invent;
  * the ground truth for everything else (ANIM_begin/end, ANIM_rotate/trans,
    ATTR_manip_*, LIGHT_PARAM, GLOBAL_*, ATTR_LOD, ...) is kept verbatim on the
    collection as text and is what the conversion reads, so editing a mesh does
    not silently delete the aircraft's animations.

Animation
---------
A dataref is a value the sim owns at run time, so "the animation" cannot be
copied into Blender; it can be *sampled* and *re-keyed*.  Every batch inside an
ANIM block gets a rig Empty holding the transform its datarefs select at the
import pose, plus an Action whose time line *is* the key table: key 1 sits on
frame 1, key N on frame N - the same frame-per-key layout XPlane2Blender uses,
so a scene authored here and one authored there agree about every frame.
Export can either write the command stream back untouched, or regenerate every
ANIM_* block from those Actions (`ANIM_rotate_key` / `ANIM_trans_key` tables).

Coordinate system
-----------------
X-Plane is right-handed with +Y up, +Z south; Blender is right-handed with +Z
up.  The mapping is a +90 degree turn about X, so nothing is mirrored.  What
does need flipping is the winding: X-Plane's front face is CLOCKWISE, Blender's
is counter-clockwise.

What is NOT supported
---------------------
Lines (VLINE + LINES) are imported read-only and carried through from their stored
indices; light primitives are preserved as raw commands.  Re-keying an animation
is limited to a rigid transform per batch (Euler XYZ about the origin), and
ANIM_show/hide are carried through rather than authored.  Manipulators survive
an export but cannot be created here.  Use XPlane2Blender when you need to
author manipulators, or animations that are more than a hinge.
"""

import os
import glob
import json

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, StringProperty)
from bpy_extras.io_utils import (ExportHelper, ImportHelper,
                                 poll_file_object_drop)

from . import acfcodec as AF
from . import objcodec as OC
from . import xpanim as XA
from . import xpmesh as XM

CLASSES = []


def _add(cls):
    CLASSES.append(cls)
    return cls


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def _import_objects(context, paths, report, coll=None):
    """Import every path into the scene; (files imported, triangles, headers).

    Shared by the .obj operator (one file, a dropped multi-selection or a whole
    folder) and the .acf operator (every object an aircraft attaches), so the
    two cannot drift apart in how a file becomes a header, or in what they count.
    """
    made = 0
    tri_count = 0
    headers = []
    for p in paths:
        print("xplane_obj: importing %s" % p, flush=True)
        try:
            header = XM.import_file(context, p, into_collection=coll,
                                    report=report, preview_rig=False)
        except (ValueError, OSError) as exc:
            report({'ERROR'}, "%s: %s" % (os.path.basename(p), exc))
            continue
        if header is None:
            continue
        made += 1
        headers.append(header)
        for part in XM.parts(header):
            if part.type == 'MESH' and part.get(XM.PROP_ROLE) == XM.ROLE_TRIS:
                tri_count += len(part.data.loop_triangles) or (
                    len(part.data.polygons))
        if coll is not None:
            header.hide_viewport = True
    return made, tri_count, headers


def _build_xp2b(context, headers, report):
    """The authoring scene XPlane2Blender's exporter reads.

    Its export operator only writes collections marked exportable, and this
    importer builds none - so without this step File > Export > X-Plane
    Object (.obj) stops with "Could not find any Exportable Collections".
    That is why it is not optional: the exported scene *is* this add-on's
    output.
    """
    from . import xp2b as XC

    if not headers:
        return
    if not XC.available():
        report({'ERROR'},
               "XPlane2Blender is not installed/enabled: the file was "
               "imported, but there is no exportable collection for "
               "its exporter to write. Install XPlane2Blender and "
               "import again")
        return
    try:
        colls = XC.convert(context, headers, report)
    except Exception as exc:                              # noqa: BLE001
        report({'ERROR'},
               "could not build the XPlane2Blender scene: %s" % exc)
        return
    report({'INFO'}, "ready to export with XPlane2Blender: %s"
           % ", ".join(c.name for c in colls))


@_add
class IMPORT_OT_xplane_obj(bpy.types.Operator, ImportHelper):
    """Import an X-Plane OBJ8 object (.obj) with its textures"""
    bl_idname = "import_scene.xplane_obj"
    bl_label = "X-Plane OBJ8 (.obj)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".obj"
    filter_glob: StringProperty(default="*.obj", options={'HIDDEN'})

    # SKIP_SAVE on filepath matters: without it the path from the previous
    # import stays on the operator and `invoke` (below) would re-import that
    # file instead of opening the file browser.
    filepath: StringProperty(
        name="File Path",
        description="Filepath used for importing the file",
        maxlen=1024, subtype='FILE_PATH', options={'SKIP_SAVE'})
    directory: StringProperty(
        subtype='DIR_PATH', options={'SKIP_SAVE', 'HIDDEN'})
    files: CollectionProperty(
        name="File Path", type=bpy.types.OperatorFileListElement,
        options={'SKIP_SAVE', 'HIDDEN'})

    import_folder: BoolProperty(
        name="Import whole folder",
        description="Import every .obj sitting next to the selected file",
        default=False)

    def invoke(self, context, event):
        """A dropped file arrives with `filepath` (or `files`) already set.

        Blender's file handler calls the operator with INVOKE_DEFAULT, and
        ImportHelper.invoke would open the file browser on top of the drop -
        so hand a pre-set path straight to execute().
        """
        if self.properties.is_property_set("filepath") or len(self.files):
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if len(self.files):
            # drag and drop of a multi-selection: import exactly what landed,
            # never "the whole folder" (that is the menu dialog's option).
            base = self.directory
            paths = [os.path.join(base, f.name) for f in self.files
                     if f.name.lower().endswith(".obj")]
            paths.sort()
            if not paths:
                self.report({'ERROR'}, "no .obj in the dropped files")
                return {'CANCELLED'}
        else:
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

        made, tri_count, headers = _import_objects(context, paths, self.report,
                                                   coll)
        if not made:
            self.report({'ERROR'}, "nothing imported")
            return {'CANCELLED'}
        self.report({'INFO'}, "imported %d file(s), %d triangles"
                    % (made, tri_count))
        _build_xp2b(context, headers, self.report)
        return {'FINISHED'}


@_add
class IMPORT_OT_xplane_acf(bpy.types.Operator, ImportHelper):
    """Import every OBJ8 object an X-Plane aircraft (.acf) attaches

    The .acf is only read: it lists the objects in `_obja/<slot>/_v10_att_file_stl`
    and this imports them the same way as dropping each .obj by hand.
    """
    bl_idname = "import_scene.xplane_acf"
    bl_label = "X-Plane Aircraft (.acf)"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".acf"
    filter_glob: StringProperty(default="*.acf", options={'HIDDEN'})

    # same reason as the .obj operator: a dropped file arrives with the path
    # already set, and a stale path must not be re-imported from the menu
    filepath: StringProperty(
        name="File Path",
        description="Filepath used for importing the file",
        maxlen=1024, subtype='FILE_PATH', options={'SKIP_SAVE'})
    directory: StringProperty(
        subtype='DIR_PATH', options={'SKIP_SAVE', 'HIDDEN'})
    files: CollectionProperty(
        name="File Path", type=bpy.types.OperatorFileListElement,
        options={'SKIP_SAVE', 'HIDDEN'})

    def invoke(self, context, event):
        if self.properties.is_property_set("filepath") or len(self.files):
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def _acfs(self):
        """The .acf files this call is about: a dropped selection, or one path."""
        if len(self.files):
            paths = [os.path.join(self.directory, f.name) for f in self.files
                     if f.name.lower().endswith(".acf")]
            paths.sort()
        elif self.filepath:
            paths = [self.filepath]
        else:
            paths = []
        return [p for p in paths if os.path.isfile(p)]

    def execute(self, context):
        acfs = self._acfs()
        if not acfs:
            self.report({'ERROR'}, "not a file: %s" % (self.filepath or
                                                       "no .acf selected"))
            return {'CANCELLED'}

        paths = []
        seen = set()
        missing = []
        for acf in acfs:
            try:
                found, gone = AF.objects(acf)
            except (OSError, ValueError) as exc:
                self.report({'ERROR'}, "%s: %s" % (os.path.basename(acf), exc))
                continue
            # two .acf of the same family (Cessna_172SP.acf and its _seaplane
            # variant) share objects; importing a file twice would build two
            # identical collections, one of which nothing refers to
            for found_path in found:
                key = os.path.normcase(found_path)
                if key in seen:
                    continue
                seen.add(key)
                paths.append(found_path)
            missing.extend(gone)

        if missing:
            shown = ", ".join(missing[:4])
            if len(missing) > 4:
                shown += ", ..."
            self.report({'WARNING'},
                        "%d object(s) listed in the .acf are not on disk "
                        "(moved, or a livery/extra that never shipped): %s"
                        % (len(missing), shown))
        if not paths:
            self.report({'ERROR'},
                        "%s attaches no .obj that could be found"
                        % os.path.basename(acfs[0]))
            return {'CANCELLED'}

        made, tri_count, headers = _import_objects(context, paths, self.report)
        if not made:
            self.report({'ERROR'}, "nothing imported")
            return {'CANCELLED'}
        self.report({'INFO'}, "imported %d of the %d object(s) an .acf "
                    "attaches, %d triangles" % (made, len(paths), tri_count))
        _build_xp2b(context, headers, self.report)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# drag and drop
# ---------------------------------------------------------------------------

@_add
class FILE_HANDLER_OT_xplane_obj(bpy.types.FileHandler):
    """Dropping an .obj into the 3D Viewport (or the View Layer outliner)
    imports it with this add-on, not with Blender's Wavefront importer.

    Without this class Blender 5.2 has no file handler for `.obj` at all, so a
    dropped X-Plane object did nothing; the OS drop falls through to whatever
    handler claims the extension.
    """
    bl_idname = "IO_FH_xplane_obj"
    bl_label = "X-Plane OBJ8 Object"
    bl_import_operator = "import_scene.xplane_obj"
    bl_file_extensions = ".obj"

    @classmethod
    def poll_drop(cls, context):
        return poll_file_object_drop(context)


@_add
class FILE_HANDLER_OT_xplane_acf(bpy.types.FileHandler):
    """Dropping an aircraft's .acf imports every object it attaches.

    .acf is X-Plane's own aircraft format, so no other add-on claims it; the
    drop is the shortest path from "which aircraft is this" to a scene with all
    of its objects in it.
    """
    bl_idname = "IO_FH_xplane_acf"
    bl_label = "X-Plane Aircraft"
    bl_import_operator = "import_scene.xplane_acf"
    bl_file_extensions = ".acf"

    @classmethod
    def poll_drop(cls, context):
        return poll_file_object_drop(context)


# ---------------------------------------------------------------------------
# export
@_add
class OBJECT_OT_xp8_anim_rig(bpy.types.Operator):
    """Give the selected part an animation rig, so it can be keyframed here"""
    bl_idname = "object.xp8_anim_rig"
    bl_label = "Animate Part"
    bl_options = {'REGISTER', 'UNDO'}

    dataref: StringProperty(
        name="Dataref",
        description="The sim variable that drives this part; it becomes the "
                    "x axis of the exported ANIM_rotate_key / ANIM_trans_key "
                    "table (key 1 on frame 1, key 2 on frame 2)",
        default="sim/flightmodel2/misc/door_open_ratio")

    v1: FloatProperty(name="Value at key 1 (frame 1)", default=0.0)
    v2: FloatProperty(name="Value at key 2 (frame 2)", default=1.0)

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and XM.is_batch(ob)

    def execute(self, context):
        ob = context.active_object
        header = _find_header(ob)
        rig = ob.parent if XM.is_rig(ob.parent) else None
        if rig is None:
            rig = bpy.data.objects.new(ob.name + "_anim", None)
            rig.empty_display_type = 'PLAIN_AXES'
            rig.empty_display_size = 0.15
            rig[XM.PROP_ROLE] = XM.ROLE_RIG
            rig.rotation_mode = 'QUATERNION'
            (context.collection or context.scene.collection).objects.link(rig)
            rig.parent = header
            rig.matrix_parent_inverse.identity()
            ob.parent = rig
            ob.matrix_parent_inverse.identity()
        rig[XM.PROP_LEAF] = ob.get(XM.PROP_LEAF, -1)
        rig[XM.PROP_DATAREF] = self.dataref
        XM.set_rig_range(rig, self.v1, self.v2)
        if not rig.get(XM.PROP_ANIM_VIS):
            rig[XM.PROP_ANIM_VIS] = "[]"
        self.report({'INFO'}, "%s: rig %s driven by %s (%g..%g)"
                    % (ob.name, rig.name, self.dataref, self.v1, self.v2))
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# animation dataref: pick one of the datarefs this object already uses
# ---------------------------------------------------------------------------

def dataref_candidates(header):
    """Datarefs this object mentions: the rigs first, then the command stream."""
    out = []
    for rig in [c for c in header.children if XM.is_rig(c)]:
        dref = (rig.get(XM.PROP_DATAREF) or "").strip()
        if dref and dref not in out:
            out.append(dref)
    for arm in [c for c in header.children if XM.is_armature(c)]:
        for entry in XM.bone_commands(arm):
            dref = (entry.get("dataref") or "").strip()
            if dref and dref not in out:
                out.append(dref)
    for kind, line in _json_list(header.get(XM.PROP_EVENTS, "[]")):
        if kind != "R" or not line:
            continue
        for word in line.split():
            if "/" not in word or word.startswith("#"):
                continue
            if word not in out:
                out.append(word)
    return out


def _pick_datarefs(self, context):
    """EnumProperty items: what can be picked without typing."""
    obj = context.active_object
    header = _find_header(obj)
    names = dataref_candidates(header) if header is not None else []
    if not names:
        return [("", "(none in this object)", "no dataref found")]
    return [(n, n, "") for n in names[:200]]


@_add
class OBJECT_OT_xp8_pick_dataref(bpy.types.Operator):
    """Set the driving dataref on the selected animation rigs"""
    bl_idname = "object.xp8_pick_dataref"
    bl_label = "Pick Dataref"
    bl_options = {'REGISTER', 'UNDO'}

    dataref: EnumProperty(name="Dataref", items=_pick_datarefs,
                          description="A dataref this object already uses; "
                                      "any other name can be typed into the "
                                      "Dataref field of the panel")

    @classmethod
    def poll(cls, context):
        return any(XM.is_rig(ob) for ob in context.selected_objects)

    def execute(self, context):
        n = 0
        for ob in context.selected_objects:
            if XM.is_rig(ob):
                ob[XM.PROP_DATAREF] = self.dataref
                n += 1
        self.report({'INFO'}, "%d rig(s) driven by %s" % (n, self.dataref))
        return {'FINISHED'}


@_add
class OBJECT_OT_xp8_clear_visibility(bpy.types.Operator):
    """Stop hiding this part; the exported file keeps it drawn forever"""
    bl_idname = "object.xp8_clear_visibility"
    bl_label = "Clear Visibility Rules"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return any(XM.is_rig(ob) for ob in context.selected_objects)

    def execute(self, context):
        n = 0
        for ob in context.selected_objects:
            if not XM.is_rig(ob):
                continue
            ob[XM.PROP_ANIM_VIS] = "[]"
            for child in ob.children:
                XM.show_part(child)
                # a scene imported before the preview keyframes were dropped
                # still carries them: take the Action off, it is not the truth
                if child.get("xp8_anim_vis_action"):
                    child.animation_data_clear()
                    child.pop("xp8_anim_vis_action", None)
            n += 1
        self.report({'INFO'}, "%d part(s) are now drawn at every dataref value"
                    % n)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# convert to an XPlane2Blender authoring scene
# ---------------------------------------------------------------------------

@_add
class OBJECT_OT_xp8_to_xp2b(bpy.types.Operator):
    bl_idname = "object.xp8_to_xp2b"
    bl_label = "Convert to XPlane2Blender scene"
    bl_description = ("Rebuild this object as XPlane2Blender Empties (datarefs "
                      "and keyframes) and meshes. One way: the imported command "
                      "stream and this scene stay untouched, but the result is "
                      "only as complete as an export from XPlane2Blender can be")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from . import xp2b as XC
        headers = []
        for ob in context.selected_objects:
            header = _find_header(ob)
            if header is not None and header not in headers:
                headers.append(header)
        if not headers:
            self.report({'ERROR'}, "select an imported OBJ8 object first")
            return {'CANCELLED'}
        try:
            colls = XC.convert(context, headers, self.report)
        except RuntimeError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, "%d file(s) converted: %s"
                    % (len(colls), ", ".join(c.name for c in colls)))
        return {'FINISHED'}


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
        col.label(text="parts: %d" % len(XM.parts(header)))
        globals_lines = _json_list(header.get(XM.PROP_GLOBALS, "[]"))
        for line in globals_lines:
            words = line.split()
            if words and words[0] in ("TEXTURE", "TEXTURE_LIT",
                                      "TEXTURE_NORMAL", "POINT_COUNTS",
                                      "BLEND_GLASS", "NORMAL_METALNESS"):
                col.label(text=line[:48], icon='TEXTURE'
                          if words[0].startswith("TEXTURE") else 'DOT')

        xbox = layout.box()
        xbox.label(text="author with XPlane2Blender", icon='MODIFIER')
        done = header.get("xp2b_collection")
        if done and bpy.data.collections.get(done) is not None:
            # import already did it; the button is only for a file that arrived
            # before XPlane2Blender was enabled, or whose collection was deleted
            xbox.label(text="converted: %s" % done, icon='CHECKMARK')
        else:
            xbox.operator("object.xp8_to_xp2b", icon='DUPLICATE')
        if header.get("xp2b_convert_warnings"):
            xbox.label(text="last conversion had warnings (hover the button)",
                       icon='ERROR')

        if XM.is_rig(obj):
            abox = layout.box()
            abox.label(text="animation rig", icon='ARMATURE_DATA')
            if XM.PROP_DATAREF not in obj:
                XM.set_rig_range(obj, 0.0, 1.0)
                obj[XM.PROP_DATAREF] = ""
            # plain custom properties, so these are ordinary editable fields
            abox.prop(obj, '["%s"]' % XM.PROP_DATAREF, text="Dataref")
            if not (obj.get(XM.PROP_DATAREF) or "").strip():
                abox.label(text="no dataref: nothing will be written",
                           icon='ERROR')
            keys = _json_list(obj.get(XM.PROP_ANIM_KEYS, "[]"))
            frames = _json_list(obj.get(XM.PROP_ANIM_FRAMES, "[]"))
            if len(keys) > 1 and len(keys) == len(frames):
                # imported: the values are the file's own keys, one per frame
                abox.label(text="%d key(s): %g … %g over frames 1..%d"
                           % (len(keys), keys[0], keys[-1], round(frames[-1])))
            else:
                row = abox.row(align=True)
                row.prop(obj, '["%s"]' % XM.PROP_ANIM_MIN, text="Value@1")
                row.prop(obj, '["%s"]' % XM.PROP_ANIM_MAX, text="@last key")
            abox.operator("object.xp8_pick_dataref", icon='PRESET',
                          text="Pick a dataref from this object")
            vis = _json_list(obj.get(XM.PROP_ANIM_VIS, "[]"))
            if vis:
                box = abox.box()
                box.label(text="ANIM_show/hide", icon='HIDE_ON')
                for kind, a, b, dref in vis:
                    box.label(text="%s %g..%g  %s" % (kind, a, b, dref))
                box.operator("object.xp8_clear_visibility", icon='X',
                             text="Drop the show/hide rules")
            info = abox.column(align=True)
            if obj.get(XM.PROP_ANIM_ACTION):
                info.label(text="action: %s" % obj[XM.PROP_ANIM_ACTION])
            else:
                info.label(text="static (no keyframes)")

        if XM.is_armature(obj):
            bbox = layout.box()
            bbox.label(text="nested animation rig", icon='ARMATURE_DATA')
            entries = XM.bone_commands(obj)
            bbox.label(text="%d bone(s), one per transform command"
                       % len(obj.data.bones))
            if not entries:
                bbox.label(text="no keyframes: static pose", icon='INFO')
            for entry in entries[:12]:
                dref = entry.get("dataref") or "-"
                bbox.label(text="%s  %s  %s"
                           % (entry.get("bone", "?"), entry.get("kind", ""),
                              dref[:34]))
            if len(entries) > 12:
                bbox.label(text="... %d more" % (len(entries) - 12))
            if obj.get(XM.PROP_ANIM_ACTION):
                bbox.label(text="action: %s" % obj[XM.PROP_ANIM_ACTION])
            bbox.label(text="preview only: the file's animation is in xp8_events",
                       icon='INFO')

        if XM.is_batch(obj):
            part = layout.box()
            part.label(text="%s  (command %s)"
                       % (obj.name, obj.get(XM.PROP_LEAF, "?")))
            info = part.column(align=True)
            info.label(text="triangles: %d" % len(obj.data.polygons))
            info.label(text="vertices: %d" % len(obj.data.vertices))
            if obj.data.materials:
                info.label(text="material: %s" % obj.data.materials[0].name)
            arm = XM.armature_of(obj)
            if XM.is_rig(obj.parent):
                info.label(text="rig: %s" % obj.parent.name, icon='ARMATURE_DATA')
            elif arm is not None:
                info.label(text="bone: %s / %d"
                           % (obj.parent_bone, len(arm.data.bones)),
                           icon='BONE_DATA')
                info.label(text="rig: %s" % arm.name)
            else:
                info.operator("object.xp8_anim_rig", icon='ADD')

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
    self.layout.operator(IMPORT_OT_xplane_acf.bl_idname,
                         text="X-Plane Aircraft (.acf, all objects)")


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
