"""Blender side of the X-Plane OBJ8 reader/writer.

Coordinate systems
------------------
X-Plane: +X east, +Y up, +Z south, right-handed.
Blender: +X right, +Y away from the viewer, +Z up, right-handed.

A +90 degree turn about X carries one onto the other: (x, y, z) -> (x, -z, y).
That is a rotation, so handedness is preserved and only the *winding* needs
flipping -- X-Plane calls a clockwise triangle the front face, Blender/OpenGL
call a counter-clockwise one the front face.

Data model
----------
One OBJ file becomes one Empty (`xp8_role = "header"`) plus one mesh object per
geometry command (`xp8_role = "tris"` / `"lines"`).  The Empty keeps

    xp8_globals        the TEXTURE / GLOBAL_* / POINT_COUNTS lines, verbatim
    xp8_events         the ordered command stream, with geometry replaced by a
                       leaf index (see objcodec.EV_*)
    xp8_leaf_objs      leaf index -> object name
    xp8_dump           the original file text
    xp8_fingerprints   object name -> hash of the geometry the importer built

so an untouched import is written back byte for byte, and an edited one is
regenerated around the preserved command stream (animations, manipulators and
lights survive an edit because they were never thrown away).
"""

import hashlib
import json
import math
import os

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import objcodec as OC
from . import xpanim as XA

# ---------------------------------------------------------------------------
# constants and helpers
# ---------------------------------------------------------------------------

XP_TO_BL = (math.pi / 2.0, 'X')     # rotation that maps X-Plane axes to Blender

PROP_ROLE = "xp8_role"
PROP_SOURCE = "xp8_source"
PROP_GLOBALS = "xp8_globals"
PROP_EVENTS = "xp8_events"
PROP_LEAF_OBJS = "xp8_leaf_objs"
PROP_DUMP = "xp8_dump"
PROP_FINGERPRINTS = "xp8_fingerprints"
PROP_LEAF = "xp8_leaf"
PROP_TEXTURE = "xp8_texture"
PROP_TEXTURE_LIT = "xp8_texture_lit"
PROP_TEXTURE_NORMAL = "xp8_texture_normal"
PROP_TEXTURE_MAP = "xp8_texture_map"
PROP_VT_ATTR = "xp8_vt"
PROP_PLACEMENT = "xp8_placement"
PROP_LEAF_STATES = "xp8_leaf_states"
PROP_CMD_HASH = "xp8_cmd_hash"
# animation rig (one Empty per animated batch)
PROP_DATAREF = "xp8_dataref"
PROP_ANIM_RANGE = "xp8_anim_range"
PROP_ANIM_DATAREFS = "xp8_anim_datarefs"
PROP_ANIM_VIS = "xp8_anim_vis"
PROP_ANIM_ACTION = "xp8_anim_action"

ROLE_HEADER = "header"
ROLE_TRIS = "tris"
ROLE_LINES = "lines"
ROLE_RIG = "rig"

# quantisation used for VT de-duplication and for the "did the geometry change"
# fingerprint.  0.1 mm on position, 1e-4 on a normal, 1e-5 on a UV -- well below
# anything that shows up in the sim, and coarse enough that float32 storage in
# Blender cannot flip a value across a bucket boundary between two reads.
POS_Q = 1e4
NRM_Q = 1e4
UV_Q = 1e5


def xp_to_bl(co):
    """(x, y, z) in X-Plane metres -> Blender metres."""
    return (co[0], -co[2], co[1])


def bl_to_xp(co):
    return (co[0], co[2], -co[1])


def xp_to_bl_normal(n):
    return (n[0], -n[2], n[1])


def bl_to_xp_normal(n):
    return (n[0], n[2], -n[1])


def _q(values):
    """Quantise a (pos, normal, uv) triple to a hashable key."""
    return (int(round(values[0] * POS_Q)), int(round(values[1] * POS_Q)),
            int(round(values[2] * POS_Q)),
            int(round(values[3] * NRM_Q)), int(round(values[4] * NRM_Q)),
            int(round(values[5] * NRM_Q)),
            int(round(values[6] * UV_Q)), int(round(values[7] * UV_Q)))


def command_hash(header):
    """Hash of the stored command stream, globals and attribute states.

    The verbatim path writes the original file text back, which would silently
    throw away an edit made to these properties -- so they are part of the
    "was anything changed" test, not just the geometry fingerprints.
    """
    parts = []
    for prop in (PROP_GLOBALS, PROP_EVENTS, PROP_LEAF_STATES):
        parts.append(str(header.get(prop, "")).encode())
    return _hexdigest(parts)


def export_matrix(ob):
    """The transform geometry is written with: the file's own frame.

    Two things must not reach the file:

    * the **animation rigs**.  A part is displayed at the pose the dataref
      selects (or wherever its baked Action puts it), but the vertices in the
      file are authored in the object frame.  So the parent chain is walked and
      rig Empties are skipped: what is left is exactly what the user set on the
      object itself.  No inverse, no float noise -- an untouched round trip is
      byte identical.
    * the legacy `xp8_placement`.  Older imports stored a static ANIM_trans
      offset on the object and subtracted it here; keep honouring it so those
      scenes still export correctly.
    """
    m = _chain_matrix(ob)
    raw = ob.get(PROP_PLACEMENT)
    if not raw:
        return m
    try:
        p = json.loads(raw)
    except ValueError:
        return m
    if len(p) != 3:
        return m
    return Matrix.Translation(Vector((-p[0], -p[1], -p[2]))) @ m


def _chain_matrix(ob):
    """Local transform of `ob` in the frame of its nearest non-rig ancestor."""
    chain = []
    node = ob
    hops = 0
    while node is not None and hops < 64:
        chain.append(node)
        node = node.parent
        hops += 1
    m = Matrix.Identity(4)
    for node in reversed(chain):
        if node.get(PROP_ROLE) == ROLE_RIG:
            continue                # preview only: never part of the file
        m = m @ (node.matrix_parent_inverse @ node.matrix_basis)
    return m


def is_rig(obj):
    return obj is not None and obj.get(PROP_ROLE) == ROLE_RIG


def is_header(obj):
    return obj is not None and obj.get(PROP_ROLE) == ROLE_HEADER


def is_batch(obj):
    return obj is not None and obj.get(PROP_ROLE) in (ROLE_TRIS, ROLE_LINES)


def _hexdigest(parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# textures and materials
# ---------------------------------------------------------------------------

def resolve_texture(obj_dir, name):
    """Find a texture named in an OBJ file.  Returns an absolute path or None."""
    if not name:
        return None
    name = name.replace("\\", "/").strip()
    if not name:
        return None
    candidates = []
    if os.path.isabs(name):
        candidates.append(name)
    else:
        candidates.append(os.path.normpath(os.path.join(obj_dir, name)))
        candidates.append(os.path.normpath(os.path.join(obj_dir, os.path.basename(name))))
    for c in candidates:
        if os.path.isfile(c):
            return c
    # case-insensitive fallback (files authored on Windows)
    for c in candidates:
        d, base = os.path.split(c)
        if os.path.isdir(d):
            for entry in os.listdir(d):
                if entry.lower() == base.lower():
                    return os.path.join(d, entry)
    return None


def _load_image(path, non_color=False):
    if not path:
        return None
    try:
        img = bpy.data.images.load(path, check_existing=True)
    except RuntimeError:
        return None
    if non_color:
        try:
            img.colorspace_settings.name = 'Non-Color'
        except TypeError:
            pass
    return img


def build_material(obj8, obj_dir, name):
    """A Principled material wired from the file's TEXTURE / _LIT / _NORMAL."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = None
    for node in nt.nodes:
        if node.bl_idname == 'ShaderNodeBsdfPrincipled':
            bsdf = node
            break
    if bsdf is None:
        return mat

    tex = obj8.global_value("TEXTURE")
    tex_lit = obj8.global_value("TEXTURE_LIT")
    tex_nrm = obj8.global_value("TEXTURE_NORMAL")
    tex_map = obj8.global_value("TEXTURE_MAP")

    raw_tex = tex[0] if tex else ""
    raw_lit = tex_lit[0] if tex_lit else ""
    raw_nrm = tex_nrm[0] if tex_nrm else ""
    used = resolve_texture(obj_dir, raw_tex)
    if raw_tex and not used:
        obj8.warnings.append("texture not found: %s" % raw_tex)

    x = -420
    if used:
        img = _load_image(used)
        if img:
            n = nt.nodes.new('ShaderNodeTexImage')
            n.image = img
            n.location = (x, 240)
            nt.links.new(n.outputs['Color'], bsdf.inputs['Base Color'])
            if img.depth == 32 or img.alpha_mode in ('STRAIGHT', 'PREMUL'):
                nt.links.new(n.outputs['Alpha'], bsdf.inputs['Alpha'])

    lit = resolve_texture(obj_dir, raw_lit)
    if lit:
        img = _load_image(lit)
        if img:
            n = nt.nodes.new('ShaderNodeTexImage')
            n.image = img
            n.location = (x, -60)
            nt.links.new(n.outputs['Color'], bsdf.inputs['Emission Color'])
            try:
                bsdf.inputs['Emission Strength'].default_value = 1.0
            except KeyError:
                pass

    nrm = resolve_texture(obj_dir, raw_nrm)
    if nrm:
        img = _load_image(nrm, non_color=True)
        if img:
            n = nt.nodes.new('ShaderNodeTexImage')
            n.image = img
            n.location = (x, -360)
            nm = nt.nodes.new('ShaderNodeNormalMap')
            nm.location = (x + 280, -360)
            nt.links.new(n.outputs['Color'], nm.inputs['Color'])
            nt.links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])

    # Blend mode: BLEND_GLASS is real transparency (windows), everything else
    # keeps dithering so alpha cut-outs behave like X-Plane's alpha test.
    if obj8.global_value("BLEND_GLASS") is not None:
        try:
            mat.surface_render_method = 'BLENDED'
        except (AttributeError, TypeError):
            pass
        bsdf.inputs['Roughness'].default_value = 0.05
    if "_LIT" in raw_tex.upper() or raw_lit:
        pass

    mat[PROP_TEXTURE] = raw_tex
    mat[PROP_TEXTURE_LIT] = raw_lit
    mat[PROP_TEXTURE_NORMAL] = raw_nrm
    if tex_map and len(tex_map) >= 2:
        mat[PROP_TEXTURE_MAP] = " ".join(tex_map)
    return mat


def material_textures(mat):
    """(albedo, lit, normal) texture names to write, preferring what the OBJ said.

    A node-graph edit should win: if the image plugged into Base Color is no
    longer the one named in the material's stored path, the node graph is taken
    as the truth.  If it still is, the original relative path is kept so that
    `../../objects/foo.png` does not degrade to `foo.png`.
    """
    out = {"TEXTURE": mat.get(PROP_TEXTURE, ""),
           "TEXTURE_LIT": mat.get(PROP_TEXTURE_LIT, ""),
           "TEXTURE_NORMAL": mat.get(PROP_TEXTURE_NORMAL, "")}
    nt = mat.node_tree
    if nt is None:
        return out
    bsdf = None
    for node in nt.nodes:
        if node.bl_idname == 'ShaderNodeBsdfPrincipled':
            bsdf = node
            break
    if bsdf is None:
        return out

    def image_into(socket):
        for link in nt.links:
            if link.to_socket == socket:
                n = link.from_node
                if n.bl_idname == 'ShaderNodeTexImage' and n.image:
                    return n.image
                if n.bl_idname == 'ShaderNodeNormalMap':
                    for l2 in nt.links:
                        if l2.to_socket == n.inputs['Color']:
                            n2 = l2.from_node
                            if n2.bl_idname == 'ShaderNodeTexImage' and n2.image:
                                return n2.image
        return None

    for key, socket in (("TEXTURE", bsdf.inputs.get('Base Color')),
                        ("TEXTURE_LIT", bsdf.inputs.get('Emission Color')),
                        ("TEXTURE_NORMAL", bsdf.inputs.get('Normal'))):
        if socket is None:
            continue
        img = image_into(socket)
        if img is None:
            continue
        stored = out[key]
        if stored and os.path.basename(stored.replace("\\", "/")) == img.name:
            continue                       # node still uses the imported file
        out[key] = img.name
    return out


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def _parse_line_rows(rows):
    out = []
    for row in rows:
        w = row.split()
        try:
            out.append((float(w[1]), float(w[2]), float(w[3])))
        except (IndexError, ValueError):
            out.append((0.0, 0.0, 0.0))
    return out


def parse_overrides(text):
    """'dref=value, dref=value' -> {dref: float}."""
    out = {}
    for piece in (text or "").replace(";", ",").split(","):
        piece = piece.strip()
        if not piece or "=" not in piece:
            continue
        name, _, value = piece.rpartition("=")
        try:
            out[name.strip()] = float(value)
        except ValueError:
            continue
    return out


def pose_at(anim, position, overrides=None):
    """{dataref: value} at a timeline position.

    Each dataref sweeps its own keyed range (0..180 for a flap, 0..1 for a
    gear), so position 1.0 means "every animation at its far end", which is
    what "extended" looks like for most aircraft parts.
    """
    pose = {}
    for dref, (lo, hi) in anim.ranges().items():
        pose[dref] = lo + (hi - lo) * position
    if overrides:
        pose.update(overrides)
    return pose


def _rig_for(context, coll, header, name, m_bl):
    """An Empty that carries one batch's animation (preview only)."""
    rig = bpy.data.objects.new(name, None)
    rig.empty_display_type = 'PLAIN_AXES'
    rig.empty_display_size = 0.15
    rig[PROP_ROLE] = ROLE_RIG
    rig.rotation_mode = 'QUATERNION'
    coll.objects.link(rig)
    rig.parent = header
    rig.matrix_parent_inverse.identity()
    rig.matrix_basis = Matrix([list(row) for row in m_bl])
    return rig


#: samples along the time line: frame 1 .. frame SAMPLES is dataref min .. max
SAMPLES = 101


def _fcurve(action, ob, data_path, index):
    """An F-Curve on either the 4.2 API or the 5.x layered one."""
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return legacy.new(data_path, index=index)
    return action.fcurve_ensure_for_datablock(ob, data_path, index=index)


def _write_curve(action, ob, data_path, index, frames, values):
    fc = _fcurve(action, ob, data_path, index)
    n = len(frames)
    fc.keyframe_points.add(n)
    co = []
    for f, v in zip(frames, values):
        co.append(float(f))
        co.append(float(v))
    fc.keyframe_points.foreach_set("co", co)
    # a dataref is linearly interpolated by the sim, so the preview must be too
    for kp in fc.keyframe_points:
        kp.interpolation = 'LINEAR'
    fc.update()


def bake_rig_action(rig, anim, leaf, overrides=None):
    """Bake one batch's animation into an Action on its rig.

    Blender cannot read a dataref, so the time line *is* the dataref: frame 1
    is every dataref at its lowest keyed value, frame SAMPLES at its highest.
    Dragging the time line therefore plays the animation the way the sim would.
    """
    rng = anim.ranges()
    frames = list(range(1, SAMPLES + 1))
    locs, quats = [], []
    prev = None
    for i, frame in enumerate(frames):
        t = i / (SAMPLES - 1.0)
        pose = {d: lo + (hi - lo) * t for d, (lo, hi) in rng.items()}
        if overrides:
            pose.update(overrides)
        m = Matrix([list(r) for r in XA.to_bl(anim.matrix_at(pose, leaf))])
        loc, quat, _scale = m.decompose()
        if prev is not None and quat.dot(prev) < 0.0:
            quat.negate()          # keep the shortest arc between keys
        prev = quat
        locs.append(loc)
        quats.append(quat)

    action = bpy.data.actions.new(rig.name + "_anim")
    action.use_fake_user = True
    rig.animation_data_create()
    rig.animation_data.action = action    # 5.x needs the slot before curves
    for axis in range(3):
        _write_curve(action, rig, "location", axis, frames,
                     [v[axis] for v in locs])
    for axis in range(4):
        _write_curve(action, rig, "rotation_quaternion", axis, frames,
                     [q[axis] for q in quats])
    rig[PROP_ANIM_ACTION] = action.name
    return action


def _chain_datarefs(chain):
    out = []
    for block in chain:
        for cmd in block.commands:
            dref = cmd.get("dataref")
            if dref and dref not in out:
                out.append(dref)
    return out


def _chain_visibility(chain):
    out = []
    for block in chain:
        for cmd in block.commands:
            if cmd["kind"] in (XA.CMD_SHOW, XA.CMD_HIDE):
                out.append([cmd["kind"], cmd["v1"], cmd["v2"],
                            cmd.get("dataref") or ""])
    return out


def import_file(context, path, into_collection=None, report=None,
                place_parts=True, anim_position=0.0, anim_overrides="",
                bake_actions=True):
    """Import one .obj file.  Returns the header Empty (or None on failure).

    With `place_parts` every batch inside an ANIM_begin/ANIM_end block gets a
    rig Empty holding the transform its datarefs select at `anim_position`
    (0..1 of each dataref's keyed range), so the part sits where the sim would
    draw it instead of piled up at the file origin.  Unless `bake_actions` is
    off, that rig also gets an Action sweeping the whole range over the time
    line, which is the only way a dataref-driven animation can be *seen* in
    Blender.

    The rigs are preview only: `export_matrix` skips them, so an untouched
    import is still written back byte for byte.
    """
    path = os.path.abspath(path)
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("latin-1")
    obj8 = OC.parse_text(text)
    warnings = list(obj8.warnings)
    obj_dir = os.path.dirname(path)
    base = os.path.splitext(os.path.basename(path))[0]

    coll = into_collection
    if coll is None:
        coll = context.collection or context.scene.collection

    header = bpy.data.objects.new(base, None)
    header.empty_display_type = 'PLAIN_AXES'
    header.empty_display_size = 0.5
    coll.objects.link(header)
    header[PROP_ROLE] = ROLE_HEADER
    header[PROP_SOURCE] = path

    mat = build_material(obj8, obj_dir, base)

    anim = None
    ranges = {}
    pose = {}
    overrides = {}
    if place_parts:
        anim = XA.build(obj8)
        warnings.extend(anim.warnings)
        ranges = anim.ranges()
        overrides = parse_overrides(anim_overrides)
        pose = pose_at(anim, anim_position, overrides)

    leaf_objs = [None] * len(obj8.leaves)
    vline_pts = _parse_line_rows(obj8.vline)
    rigs = []

    for leaf_index, leaf in enumerate(obj8.leaves):
        if leaf.kind == "TRIS":
            ob = _build_tris_object(context, obj8, leaf, base, leaf_index,
                                    mat, coll, warnings)
        else:
            ob = _build_lines_object(obj8, leaf, base, leaf_index, vline_pts,
                                     coll, warnings)
        if ob is None:
            warnings.append("geometry command %d (%s) built no mesh"
                            % (leaf_index, leaf.kind))
            continue
        ob.parent = header
        ob.matrix_parent_inverse.identity()
        ob[PROP_LEAF] = leaf_index
        chain = (anim.leaves.get(leaf_index) or []) if anim is not None else []
        if any(block.commands for block in chain):
            drefs = _chain_datarefs(chain)
            rig = _rig_for(context, coll, header,
                           "%s_%03d_anim" % (base, leaf_index),
                           XA.to_bl(anim.matrix_at(pose, leaf_index)))
            rig[PROP_LEAF] = leaf_index
            rig[PROP_ANIM_DATAREFS] = json.dumps(drefs)
            rig[PROP_DATAREF] = drefs[0] if drefs else ""
            rig[PROP_ANIM_RANGE] = json.dumps(
                list(ranges.get(drefs[0], (0.0, 1.0))) if drefs else (0.0, 1.0))
            rig[PROP_ANIM_VIS] = json.dumps(_chain_visibility(chain))
            ob.parent = rig
            if not anim.visible_at(pose, leaf_index):
                ob.hide_viewport = True
                ob.hide_render = True
            rigs.append((rig, leaf_index))
        leaf_objs[leaf_index] = ob.name

    if rigs and bake_actions:
        for rig, leaf_index in rigs:
            bake_rig_action(rig, anim, leaf_index, overrides)
        # the animated properties are now owned by the Actions
        context.scene.frame_set(int(1 + round(anim_position * (SAMPLES - 1))))

    header[PROP_GLOBALS] = json.dumps(obj8.globals)
    header[PROP_EVENTS] = json.dumps([[k, p] for k, p in obj8.events])
    header[PROP_LEAF_OBJS] = json.dumps(leaf_objs)
    header[PROP_DUMP] = text
    header["xp8_warnings"] = json.dumps(warnings)
    header["xp8_header_flag"] = obj8.header_flag
    header["xp8_version"] = obj8.version_line
    header["xp8_line_ending"] = {"\r\n": "crlf", "\r": "cr", "\n": "lf"}[
        obj8.line_ending]

    # the rigs are parented and the matrices set: make sure the depsgraph has
    # evaluated them before anything reads a matrix back
    context.view_layer.update()

    # fingerprint the geometry we just built: export uses it to decide whether
    # the file can be written back verbatim
    fps = {}
    for name in leaf_objs:
        ob = bpy.data.objects.get(name) if name else None
        if ob is not None:
            fps[name] = object_fingerprint(context, ob)
    header[PROP_FINGERPRINTS] = json.dumps(fps)
    # the per-leaf ATTR_* snapshots: a property, so they can be read and edited
    # without digging the raw command text out of xp8_dump
    header[PROP_LEAF_STATES] = json.dumps([leaf.state for leaf in obj8.leaves])
    header[PROP_CMD_HASH] = command_hash(header)

    # make the header the active object: the exporter and the side panel both
    # start from it, and in background mode nothing is selected otherwise
    for other in context.selected_objects:
        other.select_set(False)
    header.select_set(True)
    context.view_layer.objects.active = header

    if rigs:
        header["xp8_anim_parts"] = len(rigs)
    for w in warnings:
        if report:
            report({'WARNING'}, w)
    return header


def _new_mesh(name):
    me = bpy.data.meshes.new(name)
    return me


def _set_custom_normals(mesh, normals):
    try:
        mesh.normals_split_custom_set(normals)
    except Exception:
        try:
            mesh.normals_split_custom_set_from_vertices(normals[::3])
        except Exception:
            pass


def _build_tris_object(context, obj8, leaf, base, leaf_index, mat, coll, warnings):
    idx = leaf.indices
    usable = len(idx) - (len(idx) % 3)
    if usable != len(idx):
        warnings.append("TRIS %d %d: dropped %d trailing index/indices"
                        % (leaf.offset, leaf.count, len(idx) - usable))
    if usable == 0:
        return None
    tris = OC.flip_winding(idx[:usable])   # X-Plane CW front -> Blender CCW front

    remap = {}
    verts = []
    vrows = []
    for vi in tris:
        if vi in remap:
            continue
        if not (0 <= vi < len(obj8.vt)):
            warnings.append("TRIS %d %d references VT %d of %d"
                            % (leaf.offset, leaf.count, vi, len(obj8.vt)))
            remap[vi] = 0 if not verts else len(verts) - 1
            continue
        row = obj8.vt[vi]
        remap[vi] = len(verts)
        verts.append(xp_to_bl((row.px, row.py, row.pz)))
        vrows.append(row)
    if not verts:
        return None

    local = [remap[v] for v in tris]
    faces = [tuple(local[i:i + 3]) for i in range(0, len(local), 3)]

    name = "%s_%03d" % (base, leaf_index)
    me = _new_mesh(name)
    me.from_pydata(verts, [], faces)
    me.validate(verbose=False)
    me.update()

    # per-loop UVs and normals, straight out of the VT table
    uv = me.uv_layers.new(name="UVMap")
    flat_uv = [0.0] * (len(tris) * 2)
    normals = []
    have_normals = True
    for k, vi in enumerate(tris):
        row = obj8.vt[vi] if 0 <= vi < len(obj8.vt) else None
        if row is not None:
            flat_uv[k * 2] = row.s
            flat_uv[k * 2 + 1] = row.t
            if row.has_normal:
                normals.append(xp_to_bl_normal((row.nx, row.ny, row.nz)))
            else:
                have_normals = False
                normals.append((0.0, 0.0, 1.0))
        else:
            normals.append((0.0, 0.0, 1.0))
    uv.data.foreach_set("uv", flat_uv)

    me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
    if have_normals:
        _set_custom_normals(me, normals)

    # where each Blender vertex came from, so an untouched row can be written
    # back with its original text
    try:
        attr = me.attributes.new(PROP_VT_ATTR, 'INT', 'POINT')
        attr.data.foreach_set("value", [vi for vi in remap])
    except Exception:
        pass

    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    ob[PROP_ROLE] = ROLE_TRIS
    return ob


def _build_lines_object(obj8, leaf, base, leaf_index, vline_pts, coll, warnings):
    pts = []
    edges = []
    remap = {}
    for i, vi in enumerate(leaf.indices):
        if not (0 <= vi < len(vline_pts)):
            warnings.append("LINES %d %d references VLINE %d of %d"
                            % (leaf.offset, leaf.count, vi, len(vline_pts)))
            return None
        if vi not in remap:
            remap[vi] = len(pts)
            pts.append(xp_to_bl(vline_pts[vi]))
        if i % 2 == 1:
            edges.append((remap[leaf.indices[i - 1]], remap[vi]))
    if not edges:
        return None
    name = "%s_%03d_lines" % (base, leaf_index)
    me = _new_mesh(name)
    me.from_pydata(pts, edges, [])
    me.update()
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    ob[PROP_ROLE] = ROLE_LINES
    return ob


# ---------------------------------------------------------------------------
# geometry extraction: one code path for the import fingerprint, the export
# tables and the "did anything change" test
# ---------------------------------------------------------------------------

def triangle_corners(context, ob, uv_name=None):
    """All triangles of `ob` as X-Plane-space (pos, normal, uv) corner triples.

    Returns (corners, loop_vertex_index) or None.  Winding is already flipped
    back to X-Plane's clockwise-front convention.
    """
    depsgraph = context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(depsgraph)
    try:
        me = ev.to_mesh()
    except RuntimeError:
        return None
    if me is None:
        return None
    try:
        me.calc_loop_triangles()
        n_loops = len(me.loops)
        if n_loops == 0:
            return []
        verts = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", verts)
        verts = verts.reshape(-1, 3)

        normals = np.array([tuple(n.vector) for n in me.corner_normals],
                           dtype=np.float64).reshape(-1, 3)

        uv = None
        if me.uv_layers:
            uv = me.uv_layers.get(uv_name) if uv_name else None
            if uv is None:
                uv = me.uv_layers.active or me.uv_layers[0]
        if uv is not None:
            uvs = np.empty(n_loops * 2, dtype=np.float64)
            uv.data.foreach_get("uv", uvs)
            uvs = uvs.reshape(-1, 2)
        else:
            uvs = np.zeros((n_loops, 2), dtype=np.float64)

        m = np.array(export_matrix(ob), dtype=np.float64)
        rot = m[:3, :3]
        trans = m[:3, 3]
        world = verts @ rot.T + trans
        # Blender (x, y, z) -> X-Plane (x, z, -y); same rotation for normals
        xp = np.empty_like(world)
        xp[:, 0] = world[:, 0]
        xp[:, 1] = world[:, 2]
        xp[:, 2] = -world[:, 1]
        nrm = normals @ rot.T
        lengths = np.linalg.norm(nrm, axis=1)
        lengths[lengths == 0.0] = 1.0
        nrm = nrm / lengths[:, None]
        nx = np.empty_like(nrm)
        nx[:, 0] = nrm[:, 0]
        nx[:, 1] = nrm[:, 2]
        nx[:, 2] = -nrm[:, 1]

        loop_vert = np.empty(n_loops, dtype=np.int64)
        me.loops.foreach_get("vertex_index", loop_vert)

        corners = []
        order = []
        for lt in me.loop_triangles:
            a, b, c = lt.loops
            # Blender front face is CCW; X-Plane's is CW
            for li in (a, c, b):
                vi = loop_vert[li]
                corners.append((tuple(xp[vi]), tuple(nx[li]), tuple(uvs[li])))
                order.append(int(vi))
        return corners, order
    finally:
        ev.to_mesh_clear()


def corner_key(pos, nrm, uv):
    return (int(round(pos[0] * POS_Q)), int(round(pos[1] * POS_Q)),
            int(round(pos[2] * POS_Q)),
            int(round(nrm[0] * NRM_Q)), int(round(nrm[1] * NRM_Q)),
            int(round(nrm[2] * NRM_Q)),
            int(round(uv[0] * UV_Q)), int(round(uv[1] * UV_Q)))


def object_fingerprint(context, ob, uv_name=None):
    """Hash of the geometry exactly as the exporter would read it."""
    got = triangle_corners(context, ob, uv_name)
    if not got:                     # unreadable, or a mesh with no faces (LINES)
        return "unreadable"
    corners = got[0]
    m = np.array(export_matrix(ob), dtype=np.float64).ravel()
    parts = [("%.5f|" % v).encode() for v in m]
    for pos, nrm, uv in corners:
        parts.append(("%d,%d,%d,%d,%d,%d,%d,%d|"
                      % corner_key(pos, nrm, uv)).encode())
    return _hexdigest(parts)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _header_template(header):
    """Rebuild the Obj8 skeleton (globals, events, leaves, VT text) of a file."""
    dump = header.get(PROP_DUMP)
    template = None
    if dump:
        try:
            template = OC.parse_text(dump)
        except ValueError:
            template = None
    if template is None:
        template = OC.Obj8()
    if PROP_GLOBALS in header:
        try:
            template.globals = list(json.loads(header[PROP_GLOBALS]))
        except ValueError:
            pass
    if PROP_EVENTS in header:
        try:
            template.events = [(k, p) for k, p in json.loads(header[PROP_EVENTS])]
        except ValueError:
            pass
    states = header.get(PROP_LEAF_STATES)
    if states:
        try:
            parsed = json.loads(states)
        except ValueError:
            parsed = None
        if parsed is not None:
            for leaf, state in zip(template.leaves, parsed):
                leaf.state = [[cat, list(raws)] for cat, raws in state]
    return template


def _leaf_object_names(header):
    try:
        return list(json.loads(header.get(PROP_LEAF_OBJS, "[]")))
    except ValueError:
        return []


def build_tables(context, template, leaf_objects, uv_name=None, report=None,
                 skip=None):
    """Meshes -> (vt row texts, index table, spans per leaf index, fingerprints).

    VT rows a vertex still matches are reused with their original text, so a
    file that was only touched here and there keeps its exact numbers.  The
    match cannot be exact: Blender stores custom normals as int16 per axis, so
    every normal comes back ~3e-5 off (positions and UVs are exact).  Hence a
    coarse bucket lookup plus a tolerance check.

    ``skip`` is a set of leaf indices to leave out entirely: their object is not
    read and their indices never enter the table (a partial export).
    """
    orig_rows = template.vt
    orig_by_key = {}
    orig_by_pos = {}
    for i, row in enumerate(orig_rows):
        if not row.has_normal:
            continue
        orig_by_key.setdefault(
            corner_key((row.px, row.py, row.pz),
                       (row.nx, row.ny, row.nz),
                       (row.s, row.t)), i)
        # bucket on POSITION ONLY.  Position and UV survive the Blender round
        # trip exactly, but a float32 value times a coefficient still lands on
        # the other side of a rounding boundary now and then (UV 0.5885 reads
        # back as 0.588500022 -> *1000 gives 588.4999 vs 588.50002), and the
        # normals are int16-quantised.  So: cheap bucket, exact tolerance check.
        bucket = (int(round(row.px * 100.0)), int(round(row.py * 100.0)),
                  int(round(row.pz * 100.0)))
        orig_by_pos.setdefault(bucket, []).append(i)

    def find_original(pos, nrm, uv):
        """Row index of an equal source row, within Blender's normal noise."""
        bucket = (int(round(pos[0] * 100.0)), int(round(pos[1] * 100.0)),
                  int(round(pos[2] * 100.0)))
        for i in orig_by_pos.get(bucket, ()):
            row = orig_rows[i]
            if abs(row.px - pos[0]) > 1e-4 or abs(row.py - pos[1]) > 1e-4 \
                    or abs(row.pz - pos[2]) > 1e-4:
                continue
            if abs(row.s - uv[0]) > 1e-4 or abs(row.t - uv[1]) > 1e-4:
                continue
            if abs(row.nx - nrm[0]) > 2e-3 or abs(row.ny - nrm[1]) > 2e-3 \
                    or abs(row.nz - nrm[2]) > 2e-3:
                continue
            return i
        return None

    texts = [row.text for row in orig_rows]
    used = [False] * len(texts)
    key_to_index = {}
    idx_values = []
    idx_is_vt = []        # parallel to idx_values: does the value index the VT table?
    spans = {}
    fps = {}

    for leaf_index, name in enumerate(leaf_objects):
        if skip and leaf_index in skip:
            continue
        ob = bpy.data.objects.get(name) if name else None
        if ob is None:
            if report:
                report({'WARNING'},
                       "geometry command %d has no object (%r); skipped"
                       % (leaf_index, name))
            continue
        leaf = template.leaves[leaf_index] if leaf_index < len(template.leaves) else None
        if leaf is not None and leaf.kind == "LINES":
            # line primitives are not edited through the mesh: reuse the indices
            # (their values index the VLINE table, not VT: no remap below)
            offset = len(idx_values)
            idx_values.extend(leaf.indices)
            idx_is_vt.extend([False] * len(leaf.indices))
            spans[leaf_index] = (offset, len(leaf.indices))
            continue
        if ob.type != 'MESH':
            if report:
                report({'WARNING'}, "%s is not a mesh; skipped" % name)
            continue

        got = triangle_corners(context, ob, uv_name)
        if not got or not got[0]:
            continue
        corners = got[0]
        offset = len(idx_values)
        parts = []
        for pos, nrm, uv in corners:
            key = corner_key(pos, nrm, uv)
            slot = key_to_index.get(key)
            if slot is None:
                slot = orig_by_key.get(key)
            if slot is None:
                slot = find_original(pos, nrm, uv)
            if slot is None:
                body = "\t".join(OC.fmt_num(v) for v in
                                 (pos[0], pos[1], pos[2], nrm[0], nrm[1],
                                  nrm[2], uv[0], uv[1]))
                slot = len(texts)
                texts.append(body)
                used.append(True)
            else:
                used[slot] = True
            key_to_index[key] = slot
            idx_values.append(slot)
            idx_is_vt.append(True)
            parts.append(("%d,%d,%d,%d,%d,%d,%d,%d|" % key).encode())
        spans[leaf_index] = (offset, len(corners))
        m = np.array(export_matrix(ob), dtype=np.float64).ravel()
        fps[name] = _hexdigest([("%.5f|" % v).encode() for v in m] + parts)

    # drop the rows nothing points at any more, keeping the original order
    keep = [i for i, u in enumerate(used) if u]
    remap = dict((old, new) for new, old in enumerate(keep))
    vt_text = [texts[i] for i in keep]
    # note: span offsets point into the index table, which compaction of the
    # vertex table does not move -- only the values change.  LINES values index
    # the VLINE table, which is never compacted, so those stay as they are.
    idx_values = [remap[v] if is_vt else v
                  for v, is_vt in zip(idx_values, idx_is_vt)]
    return vt_text, idx_values, spans, fps


def _unmodified(context, header, uv_name):
    if header.get(PROP_CMD_HASH) != command_hash(header):
        return False            # the command stream or a state snapshot was edited
    stored = header.get(PROP_FINGERPRINTS)
    if not stored:
        return False
    try:
        stored = json.loads(stored)
    except ValueError:
        return False
    if not stored:
        return False
    for name, want in stored.items():
        ob = bpy.data.objects.get(name)
        if ob is None:
            return False
        if object_fingerprint(context, ob, uv_name) != want:
            return False
    return True


def patch_anchor(context, origin_obj=None):
    """(lon, lat, alt, heading) for an Ortho4XP ANCHOR, or None without a tile.

    VT rows are written in the object frame of the imported tile -- metres,
    X east / Y up / Z south, origin at the tile centre -- so the anchor is that
    frame's origin, not the object's own position: (lon0, lat0, 0, 0).  With alt
    0 the VT heights stay absolute MSL; heading 0 leaves the axes alone.
    """
    g = _georef_of(origin_obj) if origin_obj is not None else None
    if g is None:
        for ob in context.scene.objects:
            g = _georef_of(ob)
            if g is not None:
                break
    if g is None:
        return None
    return (g["lon0"], g["lat0"], 0.0, 0.0)


def _georef_of(obj):
    """The `mesh_georef` JSON an Ortho4XP tile import leaves on its object."""
    if obj is None:
        return None
    raw = obj.get("mesh_georef")
    if not raw:
        return None
    try:
        g = json.loads(raw)
    except ValueError:
        return None
    for key in ("lon0", "lat0", "m_per_deg_lon", "m_per_deg_lat"):
        if key not in g:
            return None
    return g


def _write_patch(path, vt_text, idx_values, anchor, report=None):
    """Write the Ortho4XP patch sibling; returns (verts, tris) or None."""
    if anchor is None:
        if report:
            report({'WARNING'},
                   "%s skipped: no Ortho4XP tile in the scene. Import the tile "
                   "with the Ortho4XP Mesh add-on (it stores 'mesh_georef') so "
                   "the ANCHOR can be derived." % os.path.basename(path))
        return None
    try:
        text = OC.emit_patch(vt_text, idx_values, anchor)
    except (ValueError, IndexError, KeyError) as exc:
        if report:
            report({'WARNING'}, "%s: %s" % (os.path.basename(path), exc))
        return None
    with open(path, "wb") as f:
        f.write(text.encode("latin-1", "replace"))
    n_t = int(text.rsplit("TRIS", 1)[1].split()[1]) // 3
    n_v = text.count("\nVT\t") + text.startswith("VT\t")
    return int(n_v), n_t


# ---------------------------------------------------------------------------
# export: writing the animation back out of Blender
# ---------------------------------------------------------------------------

#: one sample per time-line frame, so a baked Action is read back at the very
#: frames it was written on
def _sample_rigs(context, rigs):
    """{rig name: [matrix_basis per frame]} with the Actions evaluated."""
    scene = context.scene
    frames = list(range(1, SAMPLES + 1))
    out = dict((rig.name, []) for rig in rigs)
    for frame in frames:
        scene.frame_set(frame)
        depsgraph = context.evaluated_depsgraph_get()
        for rig in rigs:
            ev = rig.evaluated_get(depsgraph)
            out[rig.name].append(ev.matrix_basis.copy())
    return out


def _unwrap(values):
    """Remove 360 degree jumps so a linear key table plays smoothly."""
    out = []
    for v in values:
        if out:
            while v - out[-1] > 180.0:
                v -= 360.0
            while v - out[-1] < -180.0:
                v += 360.0
        out.append(v)
    return out


def _anim_lines(rig, mats, report=None):
    """ANIM_* lines for one batch, from the sampled rig transforms.

    The file has no matrix animation: a rotation is a sequence of axis/angle
    pairs, so the sampled rotation is decomposed into Euler XYZ and written as
    up to three rotate blocks.  Read back in file order the blocks multiply as
    Rz . Ry . Rx, which is exactly what `Euler('XYZ').to_matrix()` builds, so
    the geometry lands where Blender shows it.
    """
    dataref = rig.get(PROP_DATAREF, "") or ""
    if not dataref:
        if report:
            report({'WARNING'}, "%s: no dataref, animation not written"
                   % rig.name)
        return None
    try:
        v1, v2 = json.loads(rig.get(PROP_ANIM_RANGE, "[0, 1]"))
    except (ValueError, TypeError):
        v1, v2 = 0.0, 1.0
    n = len(mats)
    vs = [v1 + (v2 - v1) * (i / (n - 1.0)) for i in range(n)]

    lines = []
    for entry in json.loads(rig.get(PROP_ANIM_VIS, "[]") or "[]"):
        kind, a, b, dref = entry
        lines.append(XA.visibility_block(kind == XA.CMD_SHOW, a, b,
                                        dref or dataref))

    trans = [m.translation.copy() for m in mats]
    spread = max(max(abs(t[i] - trans[0][i]) for t in trans) for i in range(3))
    if spread > 1e-6:
        keys = XA.simplify([(vs[i], tuple(trans[i])) for i in range(n)], 1e-4)
        lines.extend(XA.trans_block(keys, dataref))
    elif max(abs(v) for v in trans[0]) > 1e-6:
        lines.append(XA.static_trans(tuple(trans[0])))

    angles = {"X": [], "Y": [], "Z": []}
    for m in mats:
        euler = m.to_euler('XYZ')
        for axis, value in zip("XYZ", euler):
            angles[axis].append(math.degrees(value))
    # outermost first: Rz @ Ry @ Rx
    for axis in ("Z", "Y", "X"):
        values = _unwrap(angles[axis])
        if max(abs(v) for v in values) < 1e-6:
            continue
        keys = XA.simplify([(vs[i], values[i]) for i in range(n)], 0.005)
        unit = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0),
                "Z": (0.0, 0.0, 1.0)}[axis]
        lines.extend(XA.rotate_block(unit, keys, dataref))
    return lines or None


def build_anim_blocks(context, header, report=None):
    """{leaf index: [ANIM_* lines]} regenerated from the rigs' Actions."""
    rigs = [ob for ob in header.children if is_rig(ob)]
    if not rigs:
        return {}
    samples = _sample_rigs(context, rigs)
    out = {}
    for rig in rigs:
        leaf = rig.get(PROP_LEAF)
        if leaf is None:
            continue
        mats = [XA.to_xp([[m[i][j] for j in range(4)] for i in range(4)])
                for m in samples[rig.name]]
        mats = [Matrix([list(row) for row in m]) for m in mats]
        lines = _anim_lines(rig, mats, report)
        if lines:
            out[int(leaf)] = lines
    return out


def export_header(context, header, filepath, uv_name=None, preserve=True,
                  report=None, patch_path=None, only=None, animation="FILE"):
    """Write one OBJ8 file from a header Empty and its meshes.

    ``patch_path`` additionally writes the Ortho4XP patch variant: the ANCHOR
    line first and nothing but the geometry, since that is all Ortho4XP reads.

    ``only`` restricts the *geometry* to these object names -- the parts that
    are selected.  The command stream is still written as it always is
    (ANIM_*, ATTR_manip_*, LIGHT_*, ATTR_LOD, the VLINE/VLIGHT sections), so the
    animation blocks survive; a dropped part just contributes no batch and
    POINT_COUNTS is recomputed.  ``None`` writes every part.

    ``animation`` decides where the ANIM_* blocks come from:

        "FILE"     the command stream exactly as imported (default)
        "BLENDER"  regenerated from each rig's Action, so keyframes edited in
                   Blender reach the file.  The file's own animation commands
                   are dropped, and every animated batch gets a fresh
                   ANIM_begin block built from its rig.
    """
    template = _header_template(header)
    leaf_objs = _leaf_object_names(header)
    if not template.events:
        template.events = [(OC.EV_LEAF, i) for i in range(len(leaf_objs))]

    leaf_blocks = None
    if animation == "BLENDER":
        template.events = XA.strip_animation(template.events)
        leaf_blocks = build_anim_blocks(context, header, report)
        preserve = False
        if not leaf_blocks and report:
            report({'WARNING'}, "%s: no animated parts in Blender, writing "
                                 "the geometry without animation" % header.name)

    skip = set()
    if only is not None:
        keep = set(only)
        skip = set(i for i, n in enumerate(leaf_objs) if n not in keep)
        if skip:
            if len(skip) == len(leaf_objs):
                raise ValueError("none of the selected objects is a geometry "
                                 "part of %s" % header.name)
            # a dropped part must not be emitted either: its batch has no rows
            # in the new index table any more
            template.events = [(k, p) for k, p in template.events
                               if not (k == OC.EV_LEAF and p in skip)]
            if report:
                report({'INFO'}, "%s: %d of %d parts selected"
                       % (os.path.basename(filepath), len(leaf_objs) - len(skip),
                          len(leaf_objs)))
            # a subset is never the original text, so no verbatim shortcut
            preserve = False

    if preserve and header.get(PROP_DUMP) and _unmodified(context, header, uv_name):
        with open(filepath, "wb") as f:
            f.write(header[PROP_DUMP].encode("latin-1", "replace"))
        if patch_path:
            _write_patch(patch_path, [r.text for r in template.vt],
                         template.idx, patch_anchor(context, header), report)
        return {"verbatim": True, "objects": len(leaf_objs),
                "vt": len(template.vt), "indices": len(template.idx)}

    vt_text, idx_values, spans, fps = build_tables(
        context, template, leaf_objs, uv_name, report=report, skip=skip)
    text = OC.emit(template, vt_text=vt_text, idx_values=idx_values,
                   leaf_spans=spans, leaf_blocks=leaf_blocks)
    with open(filepath, "wb") as f:
        f.write(text.encode("latin-1", "replace"))
    if patch_path:
        _write_patch(patch_path, vt_text, idx_values,
                     patch_anchor(context, header), report)
    return {"verbatim": False, "objects": len(fps), "vt": len(vt_text),
            "indices": len(idx_values)}


def parts(header):
    """Every geometry batch under a header Empty, in object order.

    A batch that carries animation hangs off a rig Empty (`header -> rig ->
    mesh`), so a plain `header.children` loop finds nothing for those files.
    Always go through this.
    """
    out = []

    def walk(node):
        for child in node.children:
            if is_batch(child):
                out.append(child)
            if is_batch(child) or is_rig(child):
                walk(child)

    walk(header)
    return out


def collect_scene_meshes(header, only_used=False):
    """Meshes under a header Empty, in object order."""
    return [ob for ob in parts(header) if ob.type == 'MESH']


def export_unparented_objects(context, filepath, objects, uv_name=None,
                              report=None, patch_path=None):
    """Write a brand new OBJ8 from loose mesh objects (no import needed)."""
    meshes = [ob for ob in objects if ob.type == 'MESH']
    if not meshes:
        if report:
            report({'ERROR'}, "nothing to export: no mesh objects selected")
        return None
    obj8 = OC.Obj8()
    obj8.header_flag = "I"
    obj8.line_ending = "\n"

    tex = {"TEXTURE": "", "TEXTURE_LIT": "", "TEXTURE_NORMAL": ""}
    for ob in meshes:
        hit = False
        for mat in ob.data.materials:
            if mat is None or mat.node_tree is None:
                continue
            got = material_textures(mat)
            for k in tex:
                if not tex[k] and got.get(k):
                    tex[k] = os.path.basename(got[k].replace("\\", "/"))
            if any(got.values()):
                hit = True
                break
        if hit:
            break
    obj8.globals = ["GLOBAL_specular 1"]
    obj8.globals.append("TEXTURE\t%s" % tex["TEXTURE"] if tex["TEXTURE"]
                        else "TEXTURE")
    if tex["TEXTURE_LIT"]:
        obj8.globals.append("TEXTURE_LIT\t%s" % tex["TEXTURE_LIT"])
    if tex["TEXTURE_NORMAL"]:
        obj8.globals.append("TEXTURE_NORMAL\t%s" % tex["TEXTURE_NORMAL"])
    obj8.globals.append("POINT_COUNTS\t0 0 0 0")

    for i, ob in enumerate(meshes):
        obj8.leaves.append(OC.Leaf("TRIS", [], [], 0, 0))
        obj8.events.append((OC.EV_LEAF, i))

    leaf_objs = [ob.name for ob in meshes]
    vt_text, idx_values, spans, fps = build_tables(
        context, obj8, leaf_objs, uv_name, report=report)
    text = OC.emit(obj8, vt_text=vt_text, idx_values=idx_values,
                   leaf_spans=spans)
    with open(filepath, "wb") as f:
        f.write(text.encode("latin-1", "replace"))
    if patch_path:
        _write_patch(patch_path, vt_text, idx_values,
                     patch_anchor(context, meshes[0]), report)
    return {"verbatim": False, "objects": len(meshes), "vt": len(vt_text),
            "indices": len(idx_values)}
