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
PROP_ANIM_RANGE = "xp8_anim_range"          # legacy JSON, read as a fallback
PROP_ANIM_MIN = "xp8_value_min"
PROP_ANIM_MAX = "xp8_value_max"
PROP_ANIM_DATAREFS = "xp8_anim_datarefs"
PROP_ANIM_VIS = "xp8_anim_vis"
PROP_ANIM_ACTION = "xp8_anim_action"
PROP_ANIM_KEYS = "xp8_anim_keys"       # the command's key VALUES
PROP_ANIM_FRAMES = "xp8_anim_frames"   # the frame each of those keys sits on
PROP_ANIM_CMD = "xp8_anim_cmd"
PROP_BONES = "xp8_bones"               # the bone map of a nested-animation rig

ROLE_HEADER = "header"
ROLE_TRIS = "tris"
ROLE_LINES = "lines"
ROLE_RIG = "rig"
ROLE_ARMATURE = "armature"

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


#: roles that are preview only: they place the part where the datarefs put it,
#: but the vertices in the file are authored in the object frame
PREVIEW_ROLES = (ROLE_RIG, ROLE_ARMATURE)


def _chain_matrix(ob):
    """Local transform of `ob` in the frame of its nearest non-preview ancestor."""
    chain = []
    node = ob
    hops = 0
    while node is not None and hops < 64:
        chain.append(node)
        node = node.parent
        hops += 1
    m = Matrix.Identity(4)
    for node in reversed(chain):
        if node.get(PROP_ROLE) in PREVIEW_ROLES:
            continue                # preview only: never part of the file
        inverse = node.matrix_parent_inverse
        if node.parent is not None and node.parent.get(PROP_ROLE) == ROLE_ARMATURE:
            # bone parenting offsets the child by the bone's length along its
            # own Y axis.  The armature is skipped as a whole, so that offset
            # belongs to the preview too: what is left is what the user set on
            # the object itself.
            inverse = Matrix.Identity(4)
        m = m @ (inverse @ node.matrix_basis)
    return m


def rig_range(rig):
    """(value at the rig's first key, value at its last key).

    Stored as two plain float properties, so both are editable in the side panel
    (`layout.prop(rig, '["xp8_value_min"]')`) instead of a JSON blob.
    """
    lo = rig.get(PROP_ANIM_MIN)
    hi = rig.get(PROP_ANIM_MAX)
    if lo is not None and hi is not None:
        return float(lo), float(hi)
    try:
        v1, v2 = json.loads(rig.get(PROP_ANIM_RANGE, "[0, 1]"))
        return float(v1), float(v2)
    except (ValueError, TypeError):
        return 0.0, 1.0


def set_rig_range(rig, lo, hi):
    rig[PROP_ANIM_MIN] = float(lo)
    rig[PROP_ANIM_MAX] = float(hi)
    rig[PROP_ANIM_RANGE] = json.dumps([float(lo), float(hi)])
    return rig


def show_part(ob):
    """Make one batch visible no matter what hid it."""
    ob.hide_viewport = False
    ob.hide_render = False
    return ob


def rigs(header):
    """Every rig under a header, outermost first."""
    out = []

    def walk(node):
        for child in node.children:
            if is_rig(child):
                out.append(child)
            if is_rig(child) or is_batch(child):
                walk(child)

    walk(header)
    return out


def leaf_rigs(ob):
    """The rig chain of one batch, outermost first.

    A compound animation (pitch translate, then a yoke-hide, then a roll
    rotate) is one rig per transform command, nested, so each rig carries
    exactly one dataref and exactly as many keyframes as the file has keys.
    """
    out = []
    node = ob.parent if ob is not None else None
    hops = 0
    while node is not None and hops < 64:
        if not is_rig(node):
            break
        out.append(node)
        node = node.parent
        hops += 1
    return list(reversed(out))


def _is_identity(cmd, pose):
    """True when a command contributes nothing at this pose (and has no dataref)."""
    if cmd.get("dataref"):
        return False
    m = XA.cmd_matrix(cmd, pose)
    return all(abs(m[i][j] - (1.0 if i == j else 0.0)) < 1e-9
               for i in range(4) for j in range(4))


def command_range(cmd):
    """(vmin, vmax) of one animation command."""
    if cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_TRANS):
        return tuple(sorted((cmd["v1"], cmd["v2"])))
    keys = cmd.get("keys") or []
    if keys:
        return keys[0][0], keys[-1][0]
    return 0.0, 0.0


def command_values(cmd):
    """The dataref values a command has a key at, low to high."""
    if cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_TRANS):
        return sorted((cmd["v1"], cmd["v2"]))
    vals = sorted(k for k, _ in (cmd.get("keys") or []))
    return vals or [0.0, 1.0]


#: Blender interpolates a quaternion between two keys along the *short* arc, so
#: a 350 degree gauge sweep cannot be written with two keys; it needs enough keys
#: that no step exceeds this many degrees.
MAX_STEP_DEG = 170.0


def _angle_at(cmd, v):
    """The command's rotation angle at a dataref value, as the file means it."""
    if cmd["kind"] == XA.CMD_ROTATE:
        return XA._lerp(v, cmd["v1"], cmd["v2"], cmd["r1"], cmd["r2"])
    keys = sorted(cmd.get("keys") or [])
    if not keys:
        return 0.0
    return XA._key_lookup(keys, v)


def _subdivide(cmd, values):
    """Add the keys a >180 degree rotation needs to be interpolable.

    `ANIM_rotate_key 0 0 / ANIM_rotate_key 1 349.99988` is a needle sweeping 350
    degrees.  The rotation *matrix* at 350 looks exactly like -10, so both the
    quaternion Blender stores and the axis/angle pair would silently replay it
    the short way round.  Splitting the span is the only way to keep the long
    way -- a few extra keys, same motion.
    """
    if cmd["kind"] not in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS) or len(values) < 2:
        return values
    out = [values[0]]
    for a, b in zip(values, values[1:]):
        step = abs(_angle_at(cmd, b) - _angle_at(cmd, a))
        n = max(1, int(math.ceil(step / MAX_STEP_DEG)))
        for i in range(1, n + 1):
            out.append(a + (b - a) * i / float(n))
    return out


def spread_frames(count):
    """Frames for `count` keys: frame 1 .. frame `count`, one key per frame.

    This is XPlane2Blender's convention and the reason it is not "1..101": XP2B
    samples the pose at the frames the dataref keys sit on, so key N belongs on
    frame N.  A scene authored here and a scene authored there then agree about
    which frame means which state - and a key table with 3 keys stays 3 keys
    instead of being smeared over 101 frames.

    Not by dataref value either: real gauges key on things like
    `-9999, 0, 0.0126, 0.0252, ... , 9999`, where a value-linear time line
    squeezes twenty keys into two adjacent frames and they collapse into one
    keyframe.
    """
    return [float(i + 1) for i in range(max(1, count))]


def chain_transforms(chain):
    """The leaf's transform commands, in the order the file multiplies them."""
    out = []
    for block in chain or []:
        for cmd in block.commands:
            if cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS,
                               XA.CMD_TRANS, XA.CMD_TRANS_KEYS):
                out.append(cmd)
    return out


def is_rig(obj):
    return obj is not None and obj.get(PROP_ROLE) == ROLE_RIG


def is_armature(obj):
    """True for a nested-animation rig: the armature, not the mesh."""
    return obj is not None and obj.get(PROP_ROLE) == ROLE_ARMATURE


def armature_of(ob):
    """The armature a batch is bone-parented to, or None."""
    parent = ob.parent if ob is not None else None
    return parent if is_armature(parent) else None


def bone_commands(arm):
    """The stored bone map of an armature rig: [{bone, kind, dataref, keys}]."""
    if arm is None:
        return []
    try:
        return json.loads(arm.get(PROP_BONES, "[]") or "[]")
    except ValueError:
        return []


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
    for dref, (lo, hi) in anim.pose_ranges().items():
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


# ---------------------------------------------------------------------------
# nested animation: the cascade as a bone chain
# ---------------------------------------------------------------------------
#: One transform command around a batch is a hinge: an Empty rig places it and
#: that is all the scene needs.  *Two or more* commands around one batch - a
#: yoke that translates in pitch and then rotates in roll, a gear leg that folds
#: and steers, a throttle that slides and swings - is a cascade, and a cascade is
#: what an armature is for: one bone per command, parented in the order the file
#: multiplies them, so the inner motion is carried by the outer one.
#:
#: OBJ8 has no skinning.  A bone only moves what is *parented to it*, so the
#: batch is bone-parented (`parent_type='BONE'`) and no Armature modifier is
#: added: a modifier would bake the pose into the vertices and the exporter would
#: write dead geometry.


def _bone_length(ob, fallback=0.2):
    """How long to draw a bone: sized to the batch, so the rig is visible."""
    try:
        size = max(ob.dimensions)
    except (AttributeError, TypeError):
        size = 0.0
    if not size or size != size:        # 0, or NaN from an empty bound box
        return fallback
    return max(0.02, min(2.0, 0.25 * size))


def _bone_name(index):
    return "anim%d" % index


def _bone_parent(ob, arm, bone_name):
    """Attach a batch to a bone without moving it.

    Blender's bone parenting offsets the child by the bone's length along the
    bone's own Y axis; `matrix_parent_inverse` cancels exactly that, so the
    object's local transform stays the identity and its world matrix is the
    bone's accumulated pose - which is what the file means.
    """
    ob.parent = arm
    ob.parent_type = 'BONE'
    ob.parent_bone = bone_name
    ob.matrix_parent_inverse = Matrix.Translation(
        (0.0, -arm.data.bones[bone_name].length, 0.0))
    return ob


def _unit(vector):
    """A direction, normalised - Blender wants a unit axis for AXIS_ANGLE."""
    length = math.sqrt(sum(value * value for value in vector))
    if length < 1e-12:
        return (0.0, 1.0, 0.0)
    return tuple(value / length for value in vector)


def _bone_name(block_index, order):
    return "b%03d_%d" % (block_index, order)


def bone_tree(anim, pose, all_blocks=False):
    """The whole file's animation as one bone tree: a bone per transform command.

    The ANIM blocks *are* a hierarchy - a translate, a hide, a show, a pivot plus
    a roll - and one armature is the only Blender thing that can say that in one
    place.  So the tree is built once for the file, mirroring the block tree, and
    every batch hangs off the bone of its innermost block.  Two batches inside
    the same block share the same bone, which is the point: the file shares the
    block too, and it is what made a per-batch rig wrong.

    Each bone carries only its own command, so an
    ``ANIM_rotate_key 1 53.690156`` is that bone's own 53.690156 degrees and
    nothing else - no product with its parents, no baked pose.

    ``all_blocks`` bones every block instead of only the ones on a cascade
    chain: a hinge gets one bone, and a block that only shows or hides gets a
    bare anchor bone (``cmd`` None) to hang the rule and the geometry on.  That
    is the shape XPlane2Blender wants - it writes ``ANIM_hide`` inside the
    bone's own ``ANIM_begin``, so the block scopes exactly as the file does.
    The importer leaves it off: its preview rig keeps a hinge as an Empty.

    Returns (specs, tips): specs are
    ``(bone name, head, command, parent bone name, block index)``, parents first;
    tips maps a block index to its innermost bone.
    """
    wanted = set()
    for block in anim.blocks:
        if all_blocks:
            if any(cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS,
                                   XA.CMD_TRANS, XA.CMD_TRANS_KEYS,
                                   XA.CMD_SHOW, XA.CMD_HIDE)
                   for cmd in block.commands):
                wanted.add(block.index)
    if not all_blocks:
        for leaf, chain in anim.leaves.items():
            if len(chain_transforms(chain)) > 1:
                wanted.update(block.index for block in chain)

    specs = []
    cumulative = {}     # block index -> matrix up to its last command
    tips = {}           # block index -> its innermost bone (or its parent's)
    for block in anim.blocks:
        if block.index not in wanted:
            continue
        parent_bone = None
        cum = Matrix.Identity(4)
        ancestor = block.parent
        while ancestor is not None:
            if ancestor.index in tips:
                parent_bone = tips[ancestor.index]
                cum = cumulative[ancestor.index]
                break
            ancestor = ancestor.parent
        transforms = [cmd for cmd in block.commands
                      if cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS,
                                         XA.CMD_TRANS, XA.CMD_TRANS_KEYS)]
        if not transforms:
            # show/hide only: nothing to move, but the rule needs a block of its
            # own to sit in, and that is a bone.  Only the XPlane2Blender path
            # asks for these - the importer's preview rig skips such a block,
            # it has no transformation to show.
            if not all_blocks:
                continue
            name = _bone_name(block.index, 0)
            specs.append((name, cum.translation.copy(), None, parent_bone,
                          block.index))
            cumulative[block.index] = cum
            tips[block.index] = name
            continue
        for order, cmd in enumerate(transforms):
            m = Matrix([list(row) for row in
                        XA.to_bl(XA.cmd_matrix(cmd, pose))])
            name = _bone_name(block.index, order)
            specs.append((name, cum.translation.copy(), cmd, parent_bone,
                          block.index))
            cum = cum @ m
            parent_bone = name
        cumulative[block.index] = cum
        tips[block.index] = parent_bone
    return specs, tips


def leaf_bones(anim, tips):
    """{leaf index: innermost bone name} for the batches a bone chain covers."""
    out = {}
    for leaf, chain in anim.leaves.items():
        if len(chain_transforms(chain)) <= 1:
            continue                # a hinge: the Empty rig path still carries it
        for block in reversed(chain):
            if tips.get(block.index):
                out[leaf] = tips[block.index]
                break
    return out


def build_bone_armature(context, coll, header, name, specs, ob=None):
    """Create the file's one armature, its bones parented like the ANIM blocks."""
    length = _bone_length(ob)
    data = bpy.data.armatures.new(name)
    arm = bpy.data.objects.new(name, data)
    coll.objects.link(arm)
    arm[PROP_ROLE] = ROLE_ARMATURE
    arm.parent = header
    arm.matrix_parent_inverse.identity()
    arm.show_in_front = False
    data.display_type = 'WIRE'

    previous = context.view_layer.objects.active
    for other in context.selected_objects:
        other.select_set(False)
    context.view_layer.objects.active = arm
    arm.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')
    for bone_name, head, cmd, parent_bone, _block in specs:
        bone = data.edit_bones.new(bone_name)
        bone.head = head
        bone.tail = head + Vector((0.0, length, 0.0))
        bone.roll = 0.0
        bone.use_connect = False        # a connected bone forces location = 0
        if parent_bone is not None:
            bone.parent = data.edit_bones[parent_bone]
    bpy.ops.object.mode_set(mode='OBJECT')
    context.view_layer.objects.active = previous
    for other in context.selected_objects:
        other.select_set(False)
    if previous is not None:
        previous.select_set(True)
    return arm


def bake_bone_tree(context, arm, specs, overrides=None):
    """Key every bone with its own command, and keep the file's degrees readable.

    One Action for the armature, one pair of curves per bone: key N on frame N,
    the same frame-for-key mapping the rig Empties use.  Alongside the pose, each
    bone gets ``xp8_angle_deg`` keyed with the command's own angle, so a
    ``ANIM_rotate_key 1 53.690156`` can be read (and checked) as 53.690156 in the
    bone's custom properties instead of being reverse-engineered out of a
    quaternion.
    """
    data = arm.data
    action = bpy.data.actions.new(arm.name + "_anim")
    action.use_fake_user = True
    arm.animation_data_create()
    arm.animation_data.action = action
    report = []
    for bone_name, head, cmd, parent_bone, block_index in specs:
        bone = data.bones[bone_name]
        parent_rest = (data.bones[parent_bone].matrix_local
                       if parent_bone else Matrix.Identity(4))
        base = parent_rest.inverted() @ bone.matrix_local

        pose_bone = arm.pose.bones[bone_name]
        # AXIS_ANGLE, not QUATERNION: an OBJ8 rotation *is* an axis plus a keyed
        # angle - `ANIM_rotate_begin x y z <dataref>` / `ANIM_rotate_key v angle`
        # - so the bone's angle channel is the file's own angle and the axis
        # channel is the file's own axis, one key per file key, and a sweep past
        # 180 degrees needs no extra keys to keep its direction.
        pose_bone.rotation_mode = 'AXIS_ANGLE'
        if cmd is None:
            # an anchor bone for a block that only shows or hides: no curve
            continue
        dataref = cmd.get("dataref")
        rotates = cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS)
        if dataref:
            values = command_values(cmd)
            frames = spread_frames(len(values))
        else:
            values = [None]
            frames = [1.0]

        locations, angles, degrees = [], [], []
        for value in values:
            pose = dict(overrides or {})
            if dataref and value is not None:
                pose[dataref] = value
            m = Matrix([list(row) for row in
                        XA.to_bl(XA.cmd_matrix(cmd, pose))])
            loc, _quat, _scale = (base.inverted() @ m).decompose()
            locations.append(loc)
            if rotates:
                degrees.append(round(_angle_at(cmd, value
                                               if value is not None else 0.0),
                                     6))
                angles.append(math.radians(degrees[-1]))
            else:
                degrees.append(None)
                angles.append(0.0)

        axis_bl = _unit(xp_to_bl(cmd.get("axis") or (0.0, 1.0, 0.0)))
        path = 'pose.bones["%s"]' % bone_name
        for index in range(3):
            _write_curve(action, arm, path + ".location", index, frames,
                         [loc[index] for loc in locations])
        _write_curve(action, arm, path + ".rotation_axis_angle", 0, frames,
                     angles)
        for index in range(3):
            _write_curve(action, arm, path + ".rotation_axis_angle",
                         index + 1, frames, [axis_bl[index]] * len(frames))

        bone[PROP_DATAREF] = dataref or ""
        bone[PROP_ANIM_CMD] = cmd["kind"]
        try:
            set_rig_range(bone, *command_range(cmd))
        except (TypeError, ValueError):
            pass
        if cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS):
            axis = cmd.get("axis") or (0.0, 1.0, 0.0)
            bone["xp8_axis"] = "%s %s %s" % tuple(OC.fmt_num(v) for v in axis)
            bone["xp8_angle_deg"] = degrees[0]
            # A pose bone owns the keyed property (its f-curve lives on the
            # armature object), the data bone keeps the same value read-only so
            # the file's degree is visible without going into Pose Mode.
            pose_bone["xp8_angle_deg"] = degrees[0]
            _write_curve(action, arm, path + '["xp8_angle_deg"]', 0, frames,
                         degrees)
        report.append({"bone": bone_name, "block": block_index,
                       "kind": cmd["kind"], "dataref": dataref or "",
                       "axis": cmd.get("axis"),
                       "keys": [v for v in values if v is not None],
                       "degrees": degrees,
                       "frames": [round(f, 4) for f in frames]})

    arm[PROP_ANIM_ACTION] = action.name
    arm[PROP_BONES] = json.dumps(report)
    arm[PROP_ANIM_DATAREFS] = json.dumps(
        [entry["dataref"] for entry in report if entry["dataref"]])
    return action


#: legacy layout for rigs with no key map: frame 1 .. frame SPAN+1, that *is* every
#: dataref going from its lowest keyed value to its highest
SPAN = 100
SAMPLES = SPAN + 1

#: Minimum frame distance between two ANIM_show/hide boundaries (preview only).
#: Blender merges keyframes that are closer than about 1/24 of a frame.
VIS_MIN_STEP = 0.1


def _fcurve(action, ob, data_path, index):
    """An F-Curve on either the 4.2 API or the 5.x layered one."""
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return legacy.new(data_path, index=index)
    return action.fcurve_ensure_for_datablock(ob, data_path, index=index)


def _write_curve(action, ob, data_path, index, frames, values,
                 interp='LINEAR'):
    fc = _fcurve(action, ob, data_path, index)
    n = len(frames)
    fc.keyframe_points.add(n)
    co = []
    for f, v in zip(frames, values):
        co.append(float(f))
        co.append(float(v))
    fc.keyframe_points.foreach_set("co", co)
    # a dataref is linearly interpolated by the sim, so the preview must be too;
    # show/hide is a step, and a step is CONSTANT
    for kp in fc.keyframe_points:
        kp.interpolation = interp
    fc.update()


def frame_of(t, keys=None):
    """Time-line position (0..1) -> frame, for a table with `keys` keys.

    One key per frame, so `keys` keys occupy frames 1..keys.  Without `keys`
    the old uniform 1..101 layout is assumed, which is all a rig from an older
    import or a hand-made rig has.
    """
    n = max(2, int(keys)) if keys else SAMPLES
    return 1.0 + t * (n - 1.0)


def value_of(frame, keys=None):
    """Frame -> time-line position (0..1); the inverse of `frame_of`."""
    n = max(2, int(keys)) if keys else SAMPLES
    return (frame - 1.0) / (n - 1.0)


def timeline_keys(anim, leaf):
    """How many frames this batch's time line spans: its longest key table."""
    best = 0
    for cmd in chain_transforms(anim.leaves.get(leaf) or []):
        if cmd.get("dataref") and cmd.get("keys"):
            best = max(best, len(cmd["keys"]))
    return max(2, best)


def bake_command_action(rig, cmd, overrides=None):
    """Bake one animation command into an Action on its own rig.

    One command = one dataref = one key table, so the keyframes are exactly the
    file's keys: a two-endpoint `ANIM_trans` gives two keys, a three-key
    `ANIM_rotate_key` table gives three.  Blender cannot read a dataref, so the
    time line *is* the key table: key 1 sits on frame 1, key N on frame N, the
    same frame-for-key mapping XPlane2Blender uses.
    """
    dataref = cmd.get("dataref")
    if dataref is None:
        return None                     # static: no keyframes to write
    values = _subdivide(cmd, command_values(cmd))
    frames = spread_frames(len(values))
    locs, quats = [], []
    prev = None
    for v in values:
        pose = dict(overrides or {})
        pose[dataref] = v
        m = Matrix([list(row) for row in XA.to_bl(XA.cmd_matrix(cmd, pose))])
        loc, quat, _scale = m.decompose()
        if prev is not None and quat.dot(prev) < 0.0:
            quat.negate()               # keep the shortest arc between keys
        prev = quat
        locs.append(loc)
        quats.append(quat)

    action = bpy.data.actions.new(rig.name + "_anim")
    action.use_fake_user = True
    rig.animation_data_create()
    rig.animation_data.action = action
    for axis in range(3):
        _write_curve(action, rig, "location", axis, frames,
                     [v[axis] for v in locs])
    for axis in range(4):
        _write_curve(action, rig, "rotation_quaternion", axis, frames,
                     [q[axis] for q in quats])
    rig[PROP_ANIM_ACTION] = action.name
    rig[PROP_ANIM_KEYS] = json.dumps(values)
    rig[PROP_ANIM_FRAMES] = json.dumps([round(f, 4) for f in frames])
    return action


def visibility_steps(anim, leaf, ranges):
    """[(timeline position, visible)] -- the ANIM_show/hide step function.

    `visible_at` is piecewise constant in the dataref, so mapping every
    show/hide boundary onto the time line gives the exact schedule: it says
    nothing about keys, and nothing about transforms, just "from here on, drawn
    or not drawn".
    """
    breaks = set([0.0, 1.0])
    for block in anim.leaves.get(leaf) or []:
        for cmd in block.commands:
            if cmd["kind"] not in (XA.CMD_SHOW, XA.CMD_HIDE):
                continue
            lo, hi = ranges.get(cmd.get("dataref"), (0.0, 1.0))
            if hi == lo:
                continue
            for v in (cmd["v1"], cmd["v2"]):
                t = (v - lo) / (hi - lo)
                if not (-1e-9 <= t <= 1.0 + 1e-9):
                    continue
                t = min(1.0, max(0.0, t))
                breaks.add(round(t, 6))
                # a zero-width range (ANIM_show 2 2) is visible at that one
                # value and not one step later, so the state right after the
                # boundary has to be sampled too
                breaks.add(round(min(1.0, t + 1e-3), 6))
    out = []
    for t in sorted(breaks):
        pose = {d: lo + (hi - lo) * t for d, (lo, hi) in ranges.items()}
        visible = anim.visible_at(pose, leaf)
        if out and out[-1][1] == visible:
            continue                   # a step that changes nothing
        out.append((t, visible))
    return out


def _chain_datarefs(chain):
    """Datarefs that move the batch.  Visibility-only ones are not included:
    the time line is defined by the transforms (see PropertyAnimRange)."""
    out = []
    for block in chain:
        for cmd in block.commands:
            if cmd["kind"] not in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS,
                                   XA.CMD_TRANS, XA.CMD_TRANS_KEYS):
                continue
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
    rigs_made = []

    # one armature for the whole file, its bones mirroring the ANIM block tree:
    # a batch hangs off the bone of its innermost block, so parts that share a
    # block share a bone - which is what the file does
    bone_specs, bone_tips = (bone_tree(anim, pose) if anim is not None
                             else ([], {}))
    bone_arm = None
    leaf_bone = {}
    if bone_specs:
        bone_arm = build_bone_armature(context, coll, header, base + "_bones",
                                       bone_specs)
        leaf_bone = leaf_bones(anim, bone_tips)

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
        commands = [cmd for cmd in chain_transforms(chain)
                    if cmd.get("dataref") or not _is_identity(cmd, pose)]
        bone = leaf_bone.get(leaf_index)
        if bone:
            # a cascade: the batch rides the file's own block hierarchy
            _bone_parent(ob, bone_arm, bone)
        else:
            parent = header
            for n, cmd in enumerate(commands):
                m = XA.to_bl(XA.cmd_matrix(cmd, pose))
                rig = _rig_for(context, coll, parent,
                               "%s_%03d_anim%d" % (base, leaf_index, n), m)
                rig[PROP_LEAF] = leaf_index
                dref = cmd.get("dataref") or ""
                rig[PROP_DATAREF] = dref
                rig[PROP_ANIM_DATAREFS] = json.dumps([dref] if dref else [])
                rig[PROP_ANIM_CMD] = cmd["kind"]
                set_rig_range(rig, *command_range(cmd))
                parent = rig
                rigs_made.append((rig, cmd))
            if parent is not header:
                ob.parent = parent
        if anim is not None and chain:
            # the ANIM_show/hide rules travel as text (and as XP2B datarefs on
            # the bridge); nothing is hidden in the scene. A `hide_viewport`
            # keyframe used to fake the schedule, but a dataref is not a clock:
            # it hid parts the user then could not find, and XP2B reads that flag
            # as "export the animation only" and drops the geometry.
            ob[PROP_ANIM_VIS] = json.dumps(_chain_visibility(chain))
        leaf_objs[leaf_index] = ob.name
    if rigs_made and bake_actions:
        for rig, cmd in rigs_made:
            bake_command_action(rig, cmd, overrides)
    if bone_arm is not None and bake_actions:
        bake_bone_tree(context, bone_arm, bone_specs, overrides)
    if (rigs_made or bone_arm is not None) and bake_actions:
        # the animated properties are now owned by the Actions
        if anim.leaves:
            # the time line is the key table: one key per frame, so the scene
            # ends where the longest table does (XPlane2Blender's convention)
            span = max(timeline_keys(anim, i) for i in anim.leaves)
            context.scene.frame_set(int(round(frame_of(anim_position, span))))
            context.scene.frame_start = 1
            context.scene.frame_end = max(2, span)

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

    if rigs_made:
        header["xp8_anim_parts"] = len(rigs_made)
    if bone_arm is not None:
        header["xp8_anim_armatures"] = 1
        header["xp8_anim_bones"] = len(bone_specs)
        header["xp8_anim_parts"] = len(rigs_made) + len(leaf_bone)
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
def _action_fcurves(action):
    """The F-Curves of an Action on the 4.2 API or the 5.x layered one."""
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        return list(legacy)
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                out.extend(bag.fcurves)
    return out


def _key_frames(rig):
    """(sorted key frames, all-linear?) of a rig's Action, or None."""
    ad = rig.animation_data
    if ad is None or ad.action is None:
        return None
    frames = set()
    linear = True
    for fc in _action_fcurves(ad.action):
        for kp in fc.keyframe_points:
            frames.add(round(kp.co[0], 4))
            if kp.interpolation != 'LINEAR':
                linear = False
    if not frames:
        return None
    return sorted(frames), linear


def _sample_rigs(context, rigs, report=None):
    """Read each rig back at the frames its own Action keys.

    Keys are read where the Action puts them: a two-endpoint animation was baked
    as two keyframes and comes back as two keys, a three-key table as three.
    """
    scene = context.scene
    plans = {}
    every = set([1.0])
    for rig in rigs:
        got = _key_frames(rig)
        if got is None:
            plans[rig.name] = [1.0]           # static rig: one sample is enough
            continue
        frames, _linear = got
        plans[rig.name] = frames if len(frames) >= 2 else [1.0]
        every.update(plans[rig.name])

    mats = dict((rig.name, {}) for rig in rigs)
    for frame in sorted(every):
        whole = int(frame)
        scene.frame_set(whole, subframe=frame - whole)
        depsgraph = context.evaluated_depsgraph_get()
        for rig in rigs:
            if frame not in plans[rig.name]:
                continue
            mats[rig.name][frame] = (rig.evaluated_get(depsgraph)
                                     .matrix_basis.copy())
    return plans, mats


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


def key_map(rig):
    """[(frame, value)] of a rig's keyframes, as baked from the file."""
    try:
        values = json.loads(rig.get(PROP_ANIM_KEYS, "[]") or "[]")
        frames = json.loads(rig.get(PROP_ANIM_FRAMES, "[]") or "[]")
    except (ValueError, TypeError):
        return []
    if len(values) != len(frames) or not values:
        return []
    return list(zip(frames, values))


def _value_at(rig, frame):
    """The dataref value at a frame, interpolating between the baked keys."""
    pairs = key_map(rig)
    if not pairs:
        v1, v2 = rig_range(rig)
        return v1 + (v2 - v1) * value_of(frame)
    if len(pairs) == 1 or frame <= pairs[0][0]:
        return pairs[0][1]
    if frame >= pairs[-1][0]:
        return pairs[-1][1]
    for (f1, a), (f2, b) in zip(pairs, pairs[1:]):
        if frame <= f2:
            if f2 == f1:
                return b
            t = (frame - f1) / (f2 - f1)
            return a + (b - a) * t
    return pairs[-1][1]


def _single_axis(quats):
    """(axis, angles in degrees) when every rotation is about one axis.

    A hinge, a prop, a gear leg -- almost every OBJ8 animation is one axis, and
    one `ANIM_rotate_begin` block reproduces it *exactly* between keys.  Three
    Euler tables would only agree at the keys: the interpolated path in between
    is a different rotation (measured: 0.55 of matrix error on a 102 degree
    gear swing), so the Euler form is a last resort, not the default.

    Returns None when the samples do not share an axis.
    """
    ref = max(quats, key=lambda q: abs(q.angle))
    if abs(ref.angle) < 1e-9:
        return None
    axis = ref.axis.copy()
    if axis.length < 1e-9:
        return None
    axis.normalize()
    angles = []
    for q in quats:
        qq = q.copy()
        if qq.w < 0.0:
            qq.negate()
        s = qq.x * axis.x + qq.y * axis.y + qq.z * axis.z
        theta = 2.0 * math.atan2(s, qq.w)          # radians, (-pi, pi]
        test = Matrix.Rotation(theta, 3, axis)
        got = qq.to_matrix()
        if max(abs(test[i][j] - got[i][j])
               for i in range(3) for j in range(3)) > 1e-4:
            return None
        angles.append(math.degrees(theta))
    return axis, _unwrap(angles)


def _sample_one(context, rig, frames):
    """Evaluate one rig's matrix_basis at the given frames."""
    scene = context.scene
    out = []
    for frame in frames:
        whole = int(frame)
        scene.frame_set(whole, subframe=frame - whole)
        out.append(rig.evaluated_get(context.evaluated_depsgraph_get())
                   .matrix_basis.copy())
    return out


def _xp_matrices(mats):
    return [Matrix([list(row) for row in
                    XA.to_xp([[m[i][j] for j in range(4)] for i in range(4)])])
            for m in mats]


def _anim_lines(rig, frames, mats, report=None):
    """The ANIM_* commands of one rig: at most one translate and one rotate.

    One rig carries one command, so the sampled motion is a rotation about a
    fixed axis (or none) plus a translation -- exactly what OBJ8 can say.  A rig
    with no dataref is a placement: a fixed offset and/or a fixed turn.
    """
    dataref = (rig.get(PROP_DATAREF) or "").strip()
    v1, v2 = rig_range(rig)
    n = len(mats)
    vs = [_value_at(rig, frame) for frame in frames]
    lines = []

    trans = [m.translation.copy() for m in mats]
    spread = max(max(abs(t[i] - trans[0][i]) for t in trans) for i in range(3))
    quats = [m.to_quaternion() for m in mats]
    rest = quats[0].copy()
    rel = [rest.inverted() @ q for q in quats]
    moves = max(abs(q.angle) for q in rel)

    if not dataref:
        # a placement: nothing to key, just where the command puts the part
        if max(abs(v) for v in trans[0]) > 1e-6:
            lines.append(XA.static_trans(tuple(trans[0])))
        if abs(rest.angle) > 1e-6:
            lines.append(XA.static_rotate(rest.axis, math.degrees(rest.angle)))
        return lines

    if spread > 1e-6:
        keys = [(vs[i], tuple(trans[i])) for i in range(n)]
        lines.extend(XA.trans_block(keys, dataref))
    elif max(abs(v) for v in trans[0]) > 1e-6:
        lines.append(XA.static_trans(tuple(trans[0])))

    if moves < 1e-9:
        # translation only (icing boots, sliding doors): nothing to rotate
        return lines or None
    single = _single_axis(rel)
    if single is not None:
        if abs(rest.angle) > 1e-6:
            lines.append(XA.static_rotate(rest.axis, math.degrees(rest.angle)))
        axis, degrees = single
        if max(abs(v) for v in degrees) > 1e-6:
            keys = [(vs[i], degrees[i]) for i in range(n)]
            lines.extend(XA.rotate_block(axis, keys, dataref))
        return lines or None
    return None


def _eval_lines(lines, dataref, v):
    """Evaluate generated ANIM_* lines at a dataref value.

    Uses the parser itself, so what is checked is exactly what will be written.
    """
    text = ("I\n800\nOBJ\n\nPOINT_COUNTS 3 0 0 3\n\n"
            "VT 0 0 0 0 1 0 0 0\nVT 1 0 0 0 1 0 1 0\n"
            "VT 0 0 1 0 1 0 0 1\nIDX10 0 1 2\n\nANIM_begin\n"
            + "\n".join(lines) +
            "\nTRIS 0 3\nANIM_end\n")
    anim = XA.build(OC.parse_text(text))
    return anim.matrix_at({dataref: v}, 0)


def _probe_positions(frames):
    """Midpoints between the keys: where a wrong curve shows up.

    `frames` is the rig's own key layout (key N on frame N), so the value
    position of each key is its index along the table, not frame/101.
    """
    if len(frames) < 2:
        return []
    lo, hi = frames[0], frames[-1]
    if hi == lo:
        return []
    ts = [(f - lo) / (hi - lo) for f in frames]
    return [(a + b) / 2.0 for a, b in zip(ts, ts[1:])]


def _verify_generated(context, rig, frames, lines, report=None):
    """Do the generated lines reproduce the motion *between* the keys?

    Keys alone prove nothing: two datarefs composed (gear deploy plus tire
    steer) look exactly like one hinge at both ends, and bow away in the middle.
    Sampling the midpoints catches that, which is the difference between a
    faithful export and a plausible one.
    """
    dataref = rig.get(PROP_DATAREF, "") or ""
    v1, v2 = rig_range(rig)
    probes = _probe_positions(frames)
    if not probes:
        return True
    probe_frames = [(a + b) / 2.0 for a, b in zip(frames, frames[1:])]
    actual = _xp_matrices(_sample_one(context, rig, probe_frames))
    for t, want in zip(probes, actual):
        got = Matrix([list(row) for row in
                      _eval_lines(lines, dataref, v1 + (v2 - v1) * t)])
        delta = max(abs(got[i][j] - want[i][j])
                    for i in range(4) for j in range(4))
        if delta > 1e-3:
            if report:
                report({'INFO'}, "%s: written animation would stray %.3f at "
                                 "%.0f%% of the range -- kept from the file"
                       % (rig.name, delta, t * 100.0))
            return False
    return True


def build_anim_blocks(context, header, report=None):
    """{leaf index: [ANIM_* lines]} rebuilt from each batch's rig chain.

    One rig = one command = one key table, so a compound animation comes out as
    the same nest of commands it went in as: `ANIM_trans_begin ... ANIM_rotate_begin ...`
    inside one block (composition is matrix-multiplication in file order, and
    the block already wraps only this batch, so nesting is the same thing).
    """
    out = {}
    bone_rigged = 0
    for ob in parts(header):
        leaf = ob.get(PROP_LEAF)
        if leaf is None:
            continue
        if armature_of(ob) is not None:
            # a nested animation lives on a bone chain: its motion is the bone
            # poses, which the exporter does not sample (one key table per rig
            # Empty).  Keep the animation the file came in with rather than
            # write a flat table that would send the part somewhere else.
            bone_rigged += 1
            continue
        lines = []
        for entry in json.loads(ob.get(PROP_ANIM_VIS, "[]") or "[]"):
            kind, a, b, dref = entry
            lines.append(XA.visibility_block(kind == XA.CMD_SHOW, a, b, dref))
        ok = True
        for rig in leaf_rigs(ob):
            sub = rig_lines(context, rig, report)
            if sub is None:
                ok = False
                break
            lines.extend(sub)
        if ok and lines:
            out[int(leaf)] = lines
    if bone_rigged and report:
        report({'INFO'}, "%s: %d batch(es) are rigged with an armature (nested "
                         "animation); their animation is kept from the file"
               % (header.name, bone_rigged))
    return out


def rig_lines(context, rig, report=None):
    """The ANIM_* command(s) of one rig, or None when it cannot be expressed."""
    frames = _key_frames(rig)
    if frames is None:
        frames = [1.0]
    else:
        frames = frames[0]
    mats = _xp_matrices(_sample_one(context, rig, frames))
    return _anim_lines(rig, frames, mats, report)


def _swap_animation(template, regenerate, header, report=None):
    """Event stream with only the regenerable batches' animation replaced.

    `regenerate` is {leaf index: generated ANIM_* lines}.  A batch can only be
    regenerated if every ANIM block around it holds nothing but regenerable
    batches -- otherwise removing the block's own commands would strip the
    animation off a batch that is being kept.  Batches that cannot be
    regenerated keep the animation they came in with, which is the only honest
    option: their motion is not a hinge, and a flat key table would send them
    somewhere else.  Those are reported, because an edit made to them in Blender
    is NOT written.
    """
    if not template.events:
        return template.events, None
    anim = XA.build(template)
    if not anim.blocks:
        if regenerate and report:
            report({'WARNING'}, "%s: no ANIM_begin block to put the animation "
                                 "in" % header.name)
        return template.events, None

    known = set(regenerate)
    under = dict((block.index, set()) for block in anim.blocks)
    for leaf, chain in anim.leaves.items():
        for block in chain:
            under[block.index].add(leaf)

    # A block may only be stripped when every batch inside it is regenerated --
    # otherwise removing the block's own transform would strip the animation off
    # a batch that is being kept.  And a batch is only regenerated when *all* of
    # its blocks are stripped.  Those two rules feed each other (an outer block
    # shared by everything blocks its children), so settle the fixpoint instead
    # of guessing: getting this wrong produced a half-animated part -- the outer
    # translation kept, the inner rotations gone.
    usable = set(known)
    while True:
        strip = set(block.index for block in anim.blocks
                    if under[block.index] <= usable)
        dropped = set(leaf for leaf in usable
                      if not all(block.index in strip
                                 for block in anim.leaves.get(leaf) or []))
        if not dropped:
            break
        usable -= dropped

    leaf_blocks = {}
    for leaf in usable:
        leaf_blocks[leaf] = regenerate[leaf]
    for leaf in sorted(known - usable):
        if report:
            report({'WARNING'},
                   "%s: batch %d shares an ANIM block with a batch that cannot "
                   "be regenerated; its animation is kept from the file and "
                   "changes made in Blender to it are NOT written"
                   % (header.name, leaf))
    if not usable:
        if report:
            report({'WARNING'}, "%s: nothing could be regenerated, the "
                                 "animation is written as it came in"
                   % header.name)
        return template.events, None

    drop_events = set()
    for block in anim.blocks:
        if block.index not in strip:
            continue
        for cmd in block.commands:
            if "ev" in cmd:
                drop_events.add(cmd["ev"])
            drop_events.update(cmd.get("key_ev", ()))
            if "end_ev" in cmd:
                drop_events.add(cmd["end_ev"])

    # an emptied ANIM_begin/ANIM_end pair is legal but pointless: drop it when
    # the block holds nothing but the batches that now carry their own block
    events = template.events
    for block in anim.blocks:
        if block.index not in strip or block.end_ev is None:
            continue
        material = False
        for index in range(block.begin_ev + 1, block.end_ev):
            if index in drop_events:
                continue
            kind, payload = events[index]
            if kind == OC.EV_RAW:
                material = True
                break
            if kind == OC.EV_LEAF and payload not in usable:
                material = True
                break
        if not material:
            drop_events.add(block.begin_ev)
            drop_events.add(block.end_ev)

    kept = []
    for index, (kind, payload) in enumerate(events):
        if index in drop_events:
            continue
        kept.append((kind, payload))
    header["xp8_anim_written"] = json.dumps(sorted(usable))
    if report:
        report({'INFO'}, "%s: %d of %d animated batches written from Blender"
               % (os.path.basename(header.get(PROP_SOURCE, header.name)),
                  len(usable), len(anim.leaves)))
    return kept, leaf_blocks


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
        preserve = False
        regenerate = build_anim_blocks(context, header, report)
        template.events, leaf_blocks = _swap_animation(
            template, regenerate, header, report)

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
            if is_batch(child) or is_rig(child) or is_armature(child):
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
