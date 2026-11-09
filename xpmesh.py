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

so the file's own command stream (animations, manipulators, lights) survives an
edit: the conversion to XPlane2Blender re-parses that text instead of guessing
from the object tree.
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


def texture_map_usages(obj8):
    """``TEXTURE_MAP`` as {usage: filename} -- normal / material_gloss / gloss.

    X-Plane 12.1 spells a material out over two files instead of one, and the
    blue channel of the ``normal`` file is then *not* a vector component: a
    blue of 0 must not reach Blender as z = -1, or every surface lights
    inside-out.
    """
    out = {}
    for words in obj8.global_values("TEXTURE_MAP"):
        if len(words) >= 2 and words[0] not in out:
            out[words[0]] = words[1]
    return out


def _material_image_node(nt, img, loc, label):
    n = nt.nodes.new('ShaderNodeTexImage')
    n.image = img
    n.location = loc
    n.name = n.label = label
    return n


def wire_combo_material(nt, bsdf, obj8, obj_dir, raw_nrm, x, y=-360):
    """Wire X-Plane's combo map into the Principled BSDF; return its image.

    What X-Plane calls a normal map is the whole material standing in one
    texture, so linking it straight into Blender is wrong as soon as there is
    more than a normal in it:

        R,G   tangent-space normal X,Y.  Nothing else: the file does not store
              a Z when the channels are taken by material data, so rebuild it
              as 1.0 (the Normal Map node renormalises, which is the standard
              tangent-space reconstruction).
        B     base reflectance / metalness -- **only** in metalness mode
        A     gloss, which is Blender's roughness *inverted*

    Which of those is live depends on directives, not on the file name:

        TEXTURE_NORMAL f + NORMAL_METALNESS    R,G normal, B metalness, A gloss
        TEXTURE_MAP normal f                   f is R,G only, B and A unused
        TEXTURE_MAP material_gloss g           f R,G normal, g: R metal, G gloss
        TEXTURE_MAP gloss g                    g: R is the gloss, G ignored

    A ``TEXTURE_NORMAL`` with no ``NORMAL_METALNESS`` is an XP10/11 file: RGB
    is the complete vector and the alpha is *specular strength*, not gloss, so
    that one keeps the plain image -> Normal Map link and no channel split.

    91% of the Laminar OBJs (390 of 427 that have a normal map) are metalness
    mode, so this is the normal case, not an exotic one.  Non-color is not
    optional: an sRGB decode on a vector texture distorts the math.
    """
    maps = texture_map_usages(obj8)
    metalness = obj8.global_value("NORMAL_METALNESS") is not None

    nrm_name = maps.get("normal") or raw_nrm
    nrm = resolve_texture(obj_dir, nrm_name)
    if not nrm:
        return None
    img = _load_image(nrm, non_color=True)
    if img is None:
        return None

    tex = _material_image_node(nt, img, (x, y), "XP normal (R,G)")
    nm = nt.nodes.new('ShaderNodeNormalMap')
    metal = gloss = None
    if metalness or "normal" in maps:
        sep = nt.nodes.new('ShaderNodeSeparateColor')
        sep.mode = 'RGB'
        sep.location = (x + 220, y)
        sep.name = sep.label = "XP normal split"
        nt.links.new(tex.outputs['Color'], sep.inputs['Color'])

        comb = nt.nodes.new('ShaderNodeCombineColor')
        comb.mode = 'RGB'
        comb.location = (x + 400, y + 60)
        comb.name = comb.label = "XP normal Z=1"
        nt.links.new(sep.outputs['Red'], comb.inputs['Red'])
        nt.links.new(sep.outputs['Green'], comb.inputs['Green'])
        comb.inputs['Blue'].default_value = 1.0

        nm.location = (x + 580, y + 60)
        nt.links.new(comb.outputs['Color'], nm.inputs['Color'])
        if metalness and "material_gloss" not in maps:
            metal = sep.outputs['Blue']
            gloss = tex.outputs['Alpha']      # Separate Color has no alpha
    else:
        nm.location = (x + 280, y)
        nt.links.new(tex.outputs['Color'], nm.inputs['Color'])
    nt.links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])

    # The separated material file (XP12.1), when the object carries one.
    mat_name = maps.get("material_gloss") or maps.get("gloss")
    mat_path = resolve_texture(obj_dir, mat_name)
    if mat_path:
        mimg = _load_image(mat_path, non_color=True)
        if mimg is not None:
            mtex = _material_image_node(nt, mimg, (x, y - 280),
                                        "XP material (R metal, G gloss)")
            if "material_gloss" in maps:
                msep = nt.nodes.new('ShaderNodeSeparateColor')
                msep.mode = 'RGB'
                msep.location = (x + 220, y - 280)
                msep.name = msep.label = "XP material split"
                nt.links.new(mtex.outputs['Color'], msep.inputs['Color'])
                metal = msep.outputs['Red']
                gloss = msep.outputs['Green']
            else:
                metal = None                       # `gloss` usage has no metal
                gloss = mtex.outputs['Red']

    if metal is not None and 'Metallic' in bsdf.inputs:
        nt.links.new(metal, bsdf.inputs['Metallic'])
    if gloss is not None and 'Roughness' in bsdf.inputs:
        inv = nt.nodes.new('ShaderNodeInvert')
        inv.location = (x + 400, y - 280)
        inv.name = inv.label = "XP gloss -> roughness"
        inv.inputs['Fac'].default_value = 1.0
        nt.links.new(gloss, inv.inputs['Color'])
        nt.links.new(inv.outputs['Color'], bsdf.inputs['Roughness'])
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

    wire_combo_material(nt, bsdf, obj8, obj_dir, raw_nrm, x)

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
    tip_head = {}       # block index -> where that innermost bone sits
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
            # An anchor bone sits on its parent's head, NOT on the chain's
            # accumulated translation.  A bone's rest offset is a rigid part of
            # the pose stack, and an animated parent cannot cancel it because
            # this bone has no keyed pose of its own: laying it at the parent's
            # posed position applied that offset twice (King Air C90B yoke:
            # ANIM_trans to the pivot, then hide, then show, then the roll
            # rotation - the yoke sat 2.35 m behind where the file draws it).
            head = Vector((0.0, 0.0, 0.0))
            ancestor = block.parent
            while ancestor is not None:
                if ancestor.index in tip_head:
                    head = tip_head[ancestor.index].copy()
                    break
                ancestor = ancestor.parent
            name = _bone_name(block.index, 0)
            specs.append((name, head, None, parent_bone, block.index))
            cumulative[block.index] = cum
            tips[block.index] = name
            tip_head[block.index] = head
            continue
        for order, cmd in enumerate(transforms):
            m = Matrix([list(row) for row in
                        XA.to_bl(XA.cmd_matrix(cmd, pose))])
            name = _bone_name(block.index, order)
            head = cum.translation.copy()
            specs.append((name, head, cmd, parent_bone, block.index))
            cum = cum @ m
            parent_bone = name
            tip_head[block.index] = head
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
                bake_actions=True, preview_rig=True):
    """Import one .obj file.  Returns the header Empty (or None on failure).

    With `place_parts` every batch inside an ANIM_begin/ANIM_end block gets a
    rig Empty holding the transform its datarefs select at `anim_position`
    (0..1 of each dataref's keyed range), so the part sits where the sim would
    draw it instead of piled up at the file origin.  Unless `bake_actions` is
    off, that rig also gets an Action sweeping the whole range over the time
    line, which is the only way a dataref-driven animation can be *seen* in
    Blender.

    The rigs are preview only: the conversion to XPlane2Blender builds its own
    armature and ignores them.  `preview_rig=False` builds
    none of them - the caller is about to convert the file to an XPlane2Blender
    scene, which builds its own armature, and two rigs of the same animation in
    one .blend is one rig too many.
    """
    path = os.path.abspath(path)
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("latin-1")
    obj8 = OC.parse_text(text)
    # A file with no POINT_COUNTS would be handed back without one on the
    # verbatim path, and X-Plane refuses to load that; the record is mandatory
    # and the numbers are the tables we just read, so insert it into the stored
    # text (one line, every other byte untouched).
    text = OC.with_point_counts(text, obj8)
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
    bone_specs, bone_tips = (bone_tree(anim, pose)
                             if anim is not None and preview_rig
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
        elif not preview_rig:
            pass                    # flat on the header; the conversion re-hangs it
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
    if rigs_made and bake_actions and preview_rig:
        for rig, cmd in rigs_made:
            bake_command_action(rig, cmd, overrides)
    if bone_arm is not None and bake_actions and preview_rig:
        bake_bone_tree(context, bone_arm, bone_specs, overrides)
    if (rigs_made or bone_arm is not None) and bake_actions and preview_rig:
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
    # the file's own text is what the conversion reads
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

    # Per-loop UVs and normals, addressed through each loop's OWN vertex.
    # Looking them up as tris[k] is wrong: validate() drops near-degenerate
    # slivers (this file has triangles down to 1e-9 m^2), and from that face
    # onwards every loop index has shifted by three - the UV stream would land
    # on the wrong vertices for the rest of the mesh.  remap is injective
    # (one Blender vertex per VT index), so inverting it gives the VT row that
    # each surviving loop actually belongs to.
    inv = {bv: vi for vi, bv in remap.items()}
    uv = me.uv_layers.new(name="UVMap")
    flat_uv = [0.0] * (len(me.loops) * 2)
    normals = []
    have_normals = True
    for k, loop in enumerate(me.loops):
        vi = inv.get(loop.vertex_index)
        row = obj8.vt[vi] if vi is not None and 0 <= vi < len(obj8.vt) else None
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


