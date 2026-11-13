"""Turn an imported OBJ8 header into an XPlane2Blender *authoring* scene.

The importer in this addon keeps an OBJ8 file's command stream verbatim, so an
file's own text is kept verbatim.  That model is deliberately NOT a
one-to-one mapping to XPlane2Blender's: XP2B regenerates every ``ANIM_*`` block
from the object tree on export, and it has no way to echo raw command text.

This module does the one-way conversion that gap needs:

    imported scene  ->  collection of Empties (datarefs + keyframes) and meshes
                        that XPlane2Blender exports

What survives the conversion
----------------------------
* the ``ANIM`` nesting, as an Empty chain (static ``ANIM_trans`` / ``ANIM_rotate``
  become plain transforms, keyed tables become dataref + keyframes)
* ``ANIM_hide`` / ``ANIM_show`` as XP2B ``hide`` / ``show`` datarefs
* ``ATTR_draw_disable`` as a material copy with ``draw = False``
* ``ATTR_light_level`` as the material's light-level fields
* ``ATTR_cockpit`` / ``_lit_only`` / ``_region`` / ``_device`` /
  ``ATTR_no_cockpit`` as the material's *Cockpit Feature* plus the layer's
  *Panel Texture Mode*, ``COCKPIT_REGION`` as the layer's region table
* manipulators (the common signatures: command family, toggle, push, radio,
  delta/wrap, drag axis) as ``object.xplane.manip``
* ``LIGHT_PARAM`` / ``LIGHT_NAMED`` as light objects, ``ATTR_LOD`` as the
  collection's LOD levels
* ``PARTICLE_SYSTEM`` as the layer's *Particle System File*, and every
  ``EMITTER`` as an Empty of XPlane2Blender's *Particle Emitter* type
* the geometry, with its materials and UVs, as the importer already built it

What does NOT: ``IF``/``ENDIF`` blocks, ``ATTR_*`` categories no field of
XPlane2Blender's covers, and the manipulators it refuses (a drag-rotate whose
pivot bone is missing).  XPlane2Blender cannot be handed those as commands, so
they are dropped and counted in a warning per file - the conversion is one-way
and lossy by design, which is exactly why the source scene is left untouched.

One structural note: a block that carries both a translation table and a
rotation table becomes two nested Empty levels, because each dataref gets its
own object.  ``ANIM_begin`` therefore appears twice where the file had one;
the composition order is unchanged.
"""

import json
import math
import os
from decimal import Decimal

import bpy
from mathutils import Matrix, Vector

from . import xpanim as XA
from . import objcodec as OC
from . import xplights as XL
from . import xpmesh as XM
from .xpanim import CMD_ROTATE, CMD_ROTATE_KEYS, CMD_SHOW, CMD_HIDE, \
    CMD_TRANS, CMD_TRANS_KEYS

# the XPlane2Blender property group on objects / collections / materials
XP2B_PROP = "xplane"

_UNSUPPORTED_MANIP = {
    # XP2B derives a drag-rotate's pivot, axis and angle ends from a *rotation*
    # bone that must sit above the mesh, and a detent one additionally from a
    # translation bone.  In the files here (C90B cockpit throttles) the rotation
    # lives in a sibling block, so XP2B rejects the object outright ("Requirements
    # for Drag Rotate manipulator are not met") and writes no file at all - worse
    # than leaving the manipulator reported.
    "ATTR_manip_drag_rotate", "ATTR_manip_drag_rotate_detent",
    "ATTR_manip_drag_axis_pix",
}

#: The three panel spellings, and the XPlane2Blender *Panel Texture Mode* each
#: one is.  In OBJ8 they are a per-material switch; XP2B splits the "what" (the
#: material's Cockpit Feature) from the "how" (one mode per file).
PANEL_MODE = {
    "ATTR_cockpit": "cockpit",
    "ATTR_cockpit_lit_only": "cockpit_lit_only",
    "ATTR_cockpit_region": "cockpit_region",
}

PLUGIN_DEVICE = "Plugin Device"
MAX_COCKPIT_REGIONS = 4


def available():
    """True when XPlane2Blender is installed and enabled."""
    try:
        bpy.types.Object.bl_rna.properties[XP2B_PROP]
        bpy.types.Collection.bl_rna.properties[XP2B_PROP]
    except KeyError:
        return False
    return True


def bone_support():
    """True when the enabled XPlane2Blender is the armature build.

    Bone datarefs (`bone.xplane.datarefs`) are what that build adds, and it is
    the only XPlane2Blender that works on Blender 5.x: every release up to 4.5.0
    still reads `Action.groups` and `Action.fcurves`, which Blender 5 deleted,
    and that failure is silent - the export finishes and writes no ANIM block at
    all.  A scene converted here and exported with the old build looks perfect in
    Blender and ships an aircraft with no moving parts.
    """
    return "xplane" in bpy.types.Bone.bl_rna.properties


# ---------------------------------------------------------------------------
# small builders
# ---------------------------------------------------------------------------

def _make_transparent(mat):
    """Drive a material to alpha 0, in the viewport, Solid mode and renders.

    The alpha link has to be cut first: a texture's alpha channel wired into
    the BSDF wins over the socket value, so setting 0 alone would leave the
    object exactly as visible as it was.
    """
    if mat is None or not mat.use_nodes or not mat.node_tree:
        return
    nt = mat.node_tree
    bsdf = next((n for n in nt.nodes
                 if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
    if bsdf is None:
        return
    for link in list(nt.links):
        # compare with == and not `is`: Blender hands out a fresh Python wrapper
        # for a node on every access, so an identity check silently misses the
        # link and the texture's alpha keeps winning over the socket value
        if link.to_node == bsdf and link.to_socket.name == 'Alpha':
            nt.links.remove(link)
    bsdf.inputs['Alpha'].default_value = 0.0
    try:
        mat.surface_render_method = 'BLENDED'
    except (AttributeError, TypeError):
        try:
            mat.blend_method = 'BLEND'
        except (AttributeError, TypeError):
            pass
    try:
        mat.diffuse_color = (0.0, 0.0, 0.0, 0.0)
    except (AttributeError, TypeError):
        pass


#: XPlane2Blender's Material > X-Plane > Surface Type, in the order X-Plane
#: numbers the surfaces, so ATTR_hard's argument is an index into this.
SURFACE_TYPES = ("none", "water", "concrete", "asphalt", "grass", "dirt",
                 "gravel", "lakebed", "snow", "shoulder", "blastpad", "smooth")

#: XPlane2Blender's MAX_LODS is 5, and level 0 is "None", so four ranges fit.
MAX_LOD_LEVELS = 4


class _Lods(object):
    """Where each draw command sits in a file's ATTR_LOD ranges.

    Filled by ``_Converter._apply_lods`` and consumed by ``tag``: the ranges
    themselves go on the collection (XPlane2Blender's model), and every object
    that draws inside one gets the matching bucket.
    """

    __slots__ = ("ranges", "leaf", "light")

    def __init__(self):
        self.ranges = []        # [(near, far)], in file order
        self.leaf = {}          # leaf index -> bucket index
        self.light = {}         # event index -> bucket index (None = no range yet)

    def widest(self):
        """The bucket that reaches furthest - the one to use for "always"."""
        if not self.ranges:
            return None
        return max(range(len(self.ranges)), key=lambda i: self.ranges[i][1])

    def tag(self, ob, level):
        """Mark an object with the bucket it draws in.

        Objects the file puts *outside* every ATTR_LOD range draw at all
        distances; the widest bucket reproduces that without writing the line
        once per bucket (which would double every light that sits in two
        overlapping ranges).

        `override_lods` is not optional: XP2B reads an object's `lod[]` flags
        only when it is set, and otherwise inherits its parent's buckets - all
        False - so a file with a single ATTR_LOD used to export with no draws
        and no lights at all.
        """
        if not self.ranges:
            return
        bucket = self.widest() if level is None else level
        if bucket is None:
            return
        try:
            ob.xplane.override_lods = True
            ob.xplane.lod[bucket] = True
        except (AttributeError, IndexError, TypeError):
            pass


def _add_custom_attribute(xplane_group, name, value="", unique=True):
    """Add (or update) a XPlane2Blender custom attribute by name.

    Custom attributes are written to the OBJ verbatim, so they are the way to
    carry a state line XPlane2Blender has no field of its own for - ATTR_no_cull
    is one of them.

    ``unique=False`` appends even when the name is already taken, for the
    commands that may repeat in one batch: ATTR_axis_detent_range appears once
    per detent, and a second one must not overwrite the first.
    """
    try:
        coll = xplane_group.customAttributes
    except AttributeError:
        return
    if unique:
        for attr in coll:
            if attr.name == name:
                attr.value = value
                return
    attr = coll.add()
    attr.name = name
    attr.value = value


def _state_args(words):
    """The arguments of a state line, stopping at a trailing comment.

    OBJ8 allows a trailing comment after any record, and the words after the
    ``#`` are prose rather than arguments: ``ATTR_no_blend # default cutoff``
    carries no ratio at all, and ``ATTR_cockpit_region 1 # region one`` has
    exactly one argument.
    """
    args = []
    for word in words[1:]:
        if word.startswith("#"):
            break
        args.append(word)
    return args


def _new_collection(name):
    coll = bpy.data.collections.new(name)
    return coll


def _empty(coll, name, parent):
    """An Empty, hung under `parent` - an object or an `_Anchor`."""
    ob = bpy.data.objects.new(name, None)
    ob.empty_display_type = 'PLAIN_AXES'
    ob.empty_display_size = 0.08
    coll.objects.link(ob)
    if parent is not None:
        if isinstance(parent, _Anchor):
            parent.attach(ob)
        else:
            ob.parent = parent
            ob.matrix_parent_inverse.identity()
    return ob


def _xp_to_bl(co):
    return (co[0], -co[2], co[1])


# ---------------------------------------------------------------------------
# where a child hangs: an object, or a bone inside an armature
# ---------------------------------------------------------------------------

class _Anchor(object):
    """One point in the authoring hierarchy: an object, or a bone.

    Two or more transform commands around a batch (a static pivot plus a keyed
    rotate, or two nested blocks with one dataref each) is a cascade, and a
    cascade is what an armature is for: one bone per command, parented in the
    order the file multiplies them.  A single command stays an Empty.

    Bones are the only place the hierarchy can express "the inner motion rides
    on the outer one" for a *batch*: OBJ8 has no skinning, so the batch is
    bone-parented (`parent_type='BONE'`) and no Armature modifier is added -
    XPlane2Blender bakes a modifier's deformation into the vertices at frame 1.
    """

    __slots__ = ("obj", "arm", "bone")

    def __init__(self, obj=None, arm=None, bone=None):
        self.obj = obj
        self.arm = arm
        self.bone = bone

    def attach(self, ob):
        if self.bone is not None:
            ob.parent = self.arm
            ob.parent_type = 'BONE'
            ob.parent_bone = self.bone
            # Blender offsets a bone-parented child by the bone's length along
            # its own Y axis; cancel it, so the child's own transform is the
            # identity and its world matrix is the bone's accumulated pose.
            ob.matrix_parent_inverse = Matrix.Translation(
                (0.0, -self.arm.data.bones[self.bone].length, 0.0))
        else:
            ob.parent = self.obj
            ob.matrix_parent_inverse.identity()
        return ob


# ---------------------------------------------------------------------------
# manipulators
# ---------------------------------------------------------------------------

def _manip_line(state):
    """The ATTR_manip_* line (words, raw text), or (None, None)."""
    for cat, raws in state or []:
        if cat == "manip":
            for raw in raws:
                words = raw.split()
                if words and words[0].startswith("ATTR_manip_"):
                    return words, raw
    return None, None


def _tail(words, start):
    return " ".join(words[start:])


#: X-Plane writes a clamped end of a curve as a key at +-999; that is not a
#: value the dataref takes, so anything compared against a manipulator's range
#: has to skip it.
CLAMP_VALUE = 999.0


def _non_clamping(values):
    kept = [v for v in values if abs(v) < CLAMP_VALUE]
    return kept or [float(v) for v in values]


def _detent_ranges(state):
    """The ATTR_axis_detent_range lines of a batch, as (start, end, height)."""
    out = []
    for cat, raws in state or []:
        if cat != "detent_range":
            continue
        for raw in raws:
            words = raw.split()
            if not words or words[0] != "ATTR_axis_detent_range":
                continue
            try:
                out.append(tuple(float(w) for w in words[1:4]))
            except ValueError:
                continue
    return out


def _q(value):
    """A detent number the way XPlane2Blender compares them: three decimals.

    ``validate_axis_detent_ranges`` wraps every start/end/height and the bone's
    lift in ``decimal.Decimal(f"{v:.3f}")``, so a detent that tops out at 0.005
    under a bone that lifts 0.0046 is *accepted* there - both round to 0.005.
    Comparing raw floats instead refused the 737's flap and spoiler manipulators
    (8 ranges) that the export would have taken, so every number in this check
    goes through the same quantisation the exporter uses.
    """
    return Decimal("%.3f" % value)


def _ranges_tile(ranges, v1_min, v1_max, lift=None):
    """True when XPlane2Blender's own axis-detent validation will pass.

    That validation runs *during* export and one failure cancels the whole
    file, so every rule it enforces is checked before the manipulator is handed
    over, in the file's own order (it does not sort either):

    * the ranges tile ``v1_min..v1_max`` gaplessly, first start to last end;
    * a range's start is not past its end, and its height is inside ``0..lift``;
    * a stop pit (start == end) is lower than both neighbours and is never the
      only range.

    Numbers are compared at three decimals because that is what the exporter
    does - see ``_q``.
    """
    if not ranges:
        return False
    if _q(ranges[0][0]) != _q(v1_min) or _q(ranges[-1][1]) != _q(v1_max):
        return False
    for index, (start, end, height) in enumerate(ranges):
        q_start, q_end, q_height = _q(start), _q(end), _q(height)
        if q_start > q_end:
            return False
        if lift is not None and not (_q(0.0) <= q_height <= _q(lift)):
            return False
        if index + 1 < len(ranges):
            if q_end != _q(ranges[index + 1][0]):
                return False
        if q_start == q_end:
            if len(ranges) == 1:
                return False
            if index > 0 and not _q(ranges[index - 1][2]) > q_height:
                return False
            if (index + 1 < len(ranges)
                    and not q_height < _q(ranges[index + 1][2])):
                return False
    return True


def _vector(axis, angle):
    return tuple(axis[i] * angle for i in range(3))


def _same_pair(a, b, tol=0.02):
    """Two sets of two rotation vectors, equal up to which end is which.

    XP2B sorts the key table ascending, so the exported manipulator may list
    the ends in the other order; comparing the (axis * angle) vectors
    themselves also absorbs a flipped axis, which is how the files express a
    negative sweep.
    """
    first, second = list(a), list(b)
    for cand in first:
        hit = None
        for other in second:
            if all(abs(cand[i] - other[i]) <= tol for i in range(3)):
                hit = other
                break
        if hit is None:
            return False
        second.remove(hit)
    return True


def _detent_range_lines(state):
    """The batch's ATTR_axis_detent_range lines, verbatim, in file order."""
    out = []
    for cat, raws in state or []:
        if cat != "detent_range":
            continue
        for raw in raws:
            if raw.split()[:1] == ["ATTR_axis_detent_range"]:
                out.append(raw)
    return out


#: the state categories that are the manipulator's own lines.  Native
#: conversion regenerates all of them from the fields XP2B derives; the verbatim
#: path has to copy every one, or a drag_rotate loses its detent table and its
#: intermediate keyframes while keeping its pivot.
MANIP_STATE_CATEGORIES = ("manip", "detent_axis", "detent_range",
                          "manip_keyframe", "manip_wheel")


def _carry_manip_verbatim(manip, state, warnings, why):
    """Write the file's own ATTR_manip_* lines through custom attributes.

    XPlane2Blender's own manipulator settings are derived from the bones, so a
    file whose line says something the bones do not (a narrower value span, a
    different axis, a detent taller than the bone lifts, a rotation living in a
    sibling block) cannot go through them.  The line itself is the file's data
    and X-Plane reads it from the command stream - the two really are separate
    things - and an object's custom attributes are written verbatim, which is
    how ATTR_manip_device and MAGNET already survive.

    Every line of the manipulator goes along, in the file's own order:
    ATTR_axis_detented, the detent range table, the intermediate
    ATTR_manip_keyframe rows and the wheel rate.  XP2B writes the table only for
    its own detent types and the keyframes only from the bone's keys, so none of
    them can be left to it here.

    So the fallback is not a loss, it just costs the N-panel: the manipulator
    becomes a raw attribute instead of editable fields.
    """
    ob = manip.id_data
    manip.enabled = False                   # XP2B must not write one of its own
    carried = 0
    for cat, raws in state or []:
        if cat not in MANIP_STATE_CATEGORIES:
            continue
        for line in raws:
            keyword = line.split()[0]
            _add_custom_attribute(ob.xplane, keyword,
                                  line[len(keyword):].strip(), unique=False)
            carried += 1
    warnings.append(
        "%s: ATTR_manip_drag_rotate and %d companion line(s) are carried "
        "verbatim (custom attributes, not editable in the N-panel) - %s"
        % (ob.name, max(0, carried - 1), why))
    return True


def _apply_drag_rotate(manip, state, args, warnings, rig):
    """ATTR_manip_drag_rotate -> XP2B's drag_rotate / drag_rotate_detent.

    The file writes the pivot, the rotation axis, both angle ends, the detent
    lift and both value ranges; XPlane2Blender derives every one of those from
    the bones and writes them back, so what is copied to the native settings is
    the cursor, the two datarefs and the tooltip.  Whether that is allowed to
    happen depends on the rig - `rig` is what the bone chain above this batch
    offers, as ``(rotation dataref, detent dataref or None)``.

    When the line and the bones disagree, the native settings would export
    *different* numbers, so the line goes out verbatim instead (see
    ``_carry_manip_verbatim``).  Refusing outright was wrong: the manipulator
    is object data, X-Plane never requires it to agree with the ANIM table, and
    the two disagree in ordinary files (a throttle lever keyed -1..1 with the
    reverse gate's manipulator mapping only -1..0).
    """
    def warn(text):
        warnings.append("%s: %s" % (manip.id_data.name, text))

    def carry(why):
        """XP2B's own settings would write different numbers - go verbatim."""
        return _carry_manip_verbatim(manip, state, warnings, why)

    try:
        cursor = _cursor(args[0])
        v1_min, v1_max = float(args[10]), float(args[11])
        dataref1, dataref2 = args[14], args[15]
        tooltip = _tail(args, 16)
    except (IndexError, ValueError):
        return carry("its arguments did not parse, so nothing could be "
                     "regenerated from them")

    if rig is None:
        return carry(
            "the rotation lives in a sibling block, so XPlane2Blender finds no "
            "rotation bone above this mesh (it writes the pivot, axis and angle "
            "ends from that bone)")

    values, angles, axis, lift = rig
    v_lo, v_hi = min(v1_min, v1_max), max(v1_min, v1_max)
    if abs(values[0] - v_lo) > 1e-3 or abs(values[1] - v_hi) > 1e-3:
        return carry(
            "it maps %.3f..%.3f of its dataref while the rotation bone above it "
            "is keyed %.3f..%.3f, and XPlane2Blender writes the bone's keys as "
            "the manipulator's range"
            % (v1_min, v1_max, values[0], values[1]))
    mine = [_vector((float(args[4]), float(args[5]), float(args[6])), a)
            for a in (float(args[7]), float(args[8]))]
    theirs = [_vector(axis, a) for a in angles]
    if not _same_pair(mine, theirs):
        return carry(
            "it sweeps %s degrees about %s while the bone above the mesh sweeps "
            "%s about %s, and XPlane2Blender writes the bone's rotation"
            % ("/".join("%.3f" % a for a in (float(args[7]), float(args[8]))),
               "/".join("%.3f" % v for v in (float(args[4]), float(args[5]),
                                             float(args[6]))),
               "/".join("%.3f" % a for a in angles),
               "/".join("%.3f" % v for v in axis)))

    ranges = _detent_ranges(state)
    if lift is None:
        if dataref2 != "none":
            return carry(
                "it names detent dataref %s but no detent bone sits under the "
                "rotation, so XPlane2Blender would write a lift of 0" % dataref2)
        if ranges:
            # XPlane2Blender writes ATTR_axis_detent_range only for its detent
            # variants, and this rotation has no detent bone to make one.  The
            # file's own lines ride the custom attributes, byte for byte, which
            # is where the file had them.
            for line in _detent_range_lines(state):
                _add_custom_attribute(
                    manip.id_data.xplane, "ATTR_axis_detent_range",
                    line[len("ATTR_axis_detent_range"):].strip(), unique=False)
            warnings.append(
                "%s: %d ATTR_axis_detent_range line(s) kept as custom attributes "
                "- the rotation has no detent bone, so XPlane2Blender would "
                "write no range of its own"
                % (manip.id_data.name, len(ranges)))
        manip.type = "drag_rotate"
    else:
        if dataref2 == "none":
            return carry(
                "the bone under the rotation lifts %.4f but the file names no "
                "detent dataref, so XPlane2Blender would write a lift the file "
                "did not ask for" % lift)
        if abs(lift - float(args[9])) > 1e-3:
            return carry(
                "its detent lift is %.4f while the bone under the rotation lifts "
                "%.4f, and XPlane2Blender writes the bone's lift"
                % (float(args[9]), lift))
        # Same rule XP2B validates during export ("Height ... must be between
        # 0.0 and the maximum lift height").  Compared at three decimals like
        # the exporter does, or a 0.005 detent under a 0.0046 lift reads as a
        # mismatch here while XP2B accepts it (both round to 0.005).
        tall = [r for r in ranges if not (_q(0.0) <= _q(r[2]) <= _q(lift))]
        if tall:
            return carry(
                "%d ATTR_axis_detent_range height(s) reach %.3f while the bone "
                "under the rotation lifts %.4f, which XPlane2Blender rejects at "
                "export time" % (len(tall), max(r[2] for r in tall), lift))
        if not _ranges_tile(ranges, v1_min, v1_max, lift):
            return carry(
                "its %d ATTR_axis_detent_range line(s) do not tile %.3f..%.3f "
                "the way XPlane2Blender validates them"
                % (len(ranges), v1_min, v1_max))
        manip.type = "drag_rotate_detent"
    manip.cursor = cursor
    manip.dataref1 = dataref1
    manip.dataref2 = dataref2
    manip.tooltip = tooltip
    return True


def _cursor(value):
    value = (value or "hand").lower()
    return {
        "four_arrows": "four_arrows", "up_down": "up_down",
        "left_right": "left_right", "arrow": "arrow",
        "toggle": "toggle", "button": "button", "rotate": "rotate",
        "hand": "hand",
    }.get(value, "hand")


def apply_manipulator(ob, state, warnings, rig=None):
    """Map one ATTR_manip_* line onto object.xplane.manip. False if unhandled.

    `rig` is what the bone chain above this batch can give XPlane2Blender, for
    the types that are derived from bones instead of from the line (see
    ``_apply_drag_rotate``).  None means "nothing known" - such a manipulator
    stays unconverted rather than aborting the export.
    """
    words, raw_line = _manip_line(state)
    if words is None:
        return False
    kw, args = words[0], words[1:]
    manip = ob.xplane.manip
    manip.autodetect_datarefs = False

    try:
        if kw == "ATTR_manip_none":
            return False
        manip.enabled = True
        if kw == "ATTR_manip_noop":
            manip.type = "noop"
            return True
        if kw in ("ATTR_manip_command",):
            manip.type = "command"
            manip.cursor = _cursor(args[0])
            manip.command = args[1]
            manip.tooltip = _tail(args, 2)
            return True
        if kw in ("ATTR_manip_command_knob", "ATTR_manip_command_switch_up_down",
                  "ATTR_manip_command_switch_left_right"):
            manip.type = {"ATTR_manip_command_knob": "command_knob",
                          "ATTR_manip_command_switch_up_down":
                              "command_switch_up_down",
                          "ATTR_manip_command_switch_left_right":
                              "command_switch_left_right"}[kw]
            manip.cursor = _cursor(args[0])
            manip.positive_command = args[1]
            manip.negative_command = args[2]
            manip.tooltip = _tail(args, 3)
            return True
        if kw in ("ATTR_manip_command_knob2",
                  "ATTR_manip_command_switch_up_down2",
                  "ATTR_manip_command_switch_left_right2"):
            manip.type = {"ATTR_manip_command_knob2": "command_knob2",
                          "ATTR_manip_command_switch_up_down2":
                              "command_switch_up_down2",
                          "ATTR_manip_command_switch_left_right2":
                              "command_switch_left_right2"}[kw]
            manip.cursor = _cursor(args[0])
            manip.command = args[1]
            manip.tooltip = _tail(args, 2)
            return True
        if kw == "ATTR_manip_drag_axis":
            manip.type = "drag_axis"
            if any(cat == "detent_axis" for cat, _raws in state or []):
                # ATTR_axis_detented next to the axis is XP2B's manipulator type,
                # not a separate line: it writes the marker back out itself
                manip.type = "drag_axis_detent"
            manip.cursor = _cursor(args[0])
            manip.dx, manip.dy, manip.dz = (float(a) for a in args[1:4])
            manip.v1, manip.v2 = float(args[4]), float(args[5])
            manip.dataref1 = args[6]
            manip.tooltip = _tail(args, 7)
            return True
        if kw == "ATTR_manip_command_axis":
            manip.type = "command_axis"
            manip.cursor = _cursor(args[0])
            manip.dx, manip.dy, manip.dz = (float(a) for a in args[1:4])
            manip.positive_command = args[4]
            manip.negative_command = args[5]
            manip.tooltip = _tail(args, 6)
            return True
        if kw in ("ATTR_manip_axis_knob", "ATTR_manip_axis_switch_up_down",
                  "ATTR_manip_axis_switch_left_right"):
            # cursor, v1, v2, click step, hold step, dataref, tooltip
            manip.type = {"ATTR_manip_axis_knob": "axis_knob",
                          "ATTR_manip_axis_switch_up_down":
                              "axis_switch_up_down",
                          "ATTR_manip_axis_switch_left_right":
                              "axis_switch_left_right"}[kw]
            manip.cursor = _cursor(args[0])
            manip.v1, manip.v2 = float(args[1]), float(args[2])
            manip.click_step, manip.hold_step = float(args[3]), float(args[4])
            manip.dataref1 = args[5]
            manip.tooltip = _tail(args, 6)
            return True
        if kw == "ATTR_manip_drag_xy":
            manip.type = "drag_xy"
            manip.cursor = _cursor(args[0])
            manip.dx, manip.dy = float(args[1]), float(args[2])
            manip.v1_min, manip.v1_max = float(args[3]), float(args[4])
            manip.v2_min, manip.v2_max = float(args[5]), float(args[6])
            manip.dataref1 = args[7]
            manip.dataref2 = args[8]
            manip.tooltip = _tail(args, 9)
            return True
        if kw == "ATTR_manip_drag_rotate":
            return _apply_drag_rotate(manip, state, args, warnings, rig)
        if kw == "ATTR_manip_toggle":
            manip.type = "toggle"
            manip.cursor = _cursor(args[0])
            manip.v_on, manip.v_off = float(args[1]), float(args[2])
            manip.dataref1 = args[3]
            manip.tooltip = _tail(args, 4)
            return True
        if kw == "ATTR_manip_push":
            manip.type = "push"
            manip.cursor = _cursor(args[0])
            manip.v_down, manip.v_up = float(args[1]), float(args[2])
            manip.dataref1 = args[3]
            manip.tooltip = _tail(args, 4)
            return True
        if kw == "ATTR_manip_radio":
            manip.type = "radio"
            manip.cursor = _cursor(args[0])
            manip.v_down = float(args[1])
            manip.dataref1 = args[2]
            manip.tooltip = _tail(args, 3)
            return True
        if kw in ("ATTR_manip_delta", "ATTR_manip_wrap"):
            manip.type = "delta" if kw == "ATTR_manip_delta" else "wrap"
            manip.cursor = _cursor(args[0])
            manip.v_down, manip.v_hold = float(args[1]), float(args[2])
            manip.v1_min, manip.v1_max = float(args[3]), float(args[4])
            manip.dataref1 = args[5]
            manip.tooltip = _tail(args, 6)
            return True
    except (IndexError, ValueError):
        warnings.append("%s: could not parse its arguments" % kw)
        return False

    manip.enabled = False
    if kw.startswith("ATTR_manip_"):
        # XPlane2Blender has no field for this manipulator type (ATTR_manip_device
        # arrived in X-Plane 12.1.0, after the release we target), but an
        # object's custom attributes are written back verbatim and the writer
        # emits them with the object - so the line survives that way instead of
        # being dropped.  manip.enabled stays False, so XP2B writes no
        # ATTR_manip_* of its own and the two cannot collide.
        _add_custom_attribute(ob.xplane, kw,
                              raw_line[len(kw):].strip() if raw_line
                              else " ".join(args))
        warnings.append("%s kept as a custom attribute; XPlane2Blender has no "
                        "field for it" % kw)
        return False
    warnings.append("%s is not converted (no XPlane2Blender equivalent)" % kw)
    return False


# ---------------------------------------------------------------------------
# the conversion
# ---------------------------------------------------------------------------

class _Converter(object):
    def __init__(self, context, header, report, warnings):
        self.context = context
        self.header = header
        self.report = report
        self.warnings = warnings
        self.scene = context.scene
        self.frames_used = 0
        self.anim = None            # the parsed ANIM_* tree
        self.pose = {}              # dataref values the rest poses are laid out at
        self.leaf_anchor = {}       # leaf index -> _Anchor, for bone-rigged batches
        self.children = {}          # block index (or None) -> child blocks
        self.panel_mode = None      # the file's one panel mode, once it is known
        self.material_cache = {}    # (material, values) -> the one clone sharing them
        self.root = None            # the Empty the whole rig hangs off
        self.visibility_placed = 0   # ANIM_show/hide rules put on bones


    # -- helpers ----------------------------------------------------------
    def warn(self, msg):
        self.warnings.append(msg)

    def key_dataref(self, dr, frame, value):
        """One dataref key on frame `frame` - the frame XP2B samples the pose on."""
        self.scene.frame_set(frame)
        dr.value = value
        dr.keyframe_insert(data_path="value", group="XPlane Datarefs")
        self.frames_used = max(self.frames_used, frame)

    # -- nested animation: one bone per transform command -------------------

    def _own_collection(self, label):
        """The collection the import already put this file in, made exportable.

        Import leaves the meshes next to the header, so converting in place
        gives one collection per file.  Building a second one left the first
        behind as an empty shell with the header in it, which reads as two
        scenes for one object.  A folder import shares one collection between
        several files, and the scene's master collection cannot be made
        exportable - both fall back to a new collection.
        """
        master = self.scene.collection
        for coll in list(self.header.users_collection):
            if coll == master or coll.xplane.is_exportable_collection:
                continue
            headers = [ob for ob in coll.objects if ob.type == 'EMPTY'
                       and ob.get(XM.PROP_ROLE) == XM.ROLE_HEADER]
            if len(headers) > 1:
                continue
            coll.name = label
            return coll
        coll = _new_collection(label)
        master.children.link(coll)
        return coll

    def prepare_tree(self, anim):
        """Which blocks carry a bone, and which batch hangs where.

        One armature for the whole file, its bone tree copied from the block
        tree (``XM.bone_tree``, the same one the importer uses, so the two
        agree), with ``all_blocks`` on: every block that moves or shows/hides
        gets a bone, a one-command hinge included.  Every batch hangs off the
        bone of its innermost bone-carrying block, so parts that share a block
        share a bone - which is what the file does and what per-batch rigs got
        wrong.
        """
        self.children = {}
        for block in anim.blocks:
            key = block.parent.index if block.parent is not None else None
            self.children.setdefault(key, []).append(block)
        self.block_by_index = {block.index: block for block in anim.blocks}
        self.specs, self.tips = XM.bone_tree(anim, self.pose, all_blocks=True)

    def build_armature(self, coll, label):
        """The file's one armature: edit bones from the block tree, then keyed.

        Bones carry XPlane2Blender's own ``datarefs`` (on the armature *data*,
        which is where XP2B reads them) and the pose curves (on the object, which
        is where XP2B samples the pose).  Both are keyed frame 1..N, one key per
        file key, the mapping XP2B documents.
        """
        name = label + ".bones"
        data = bpy.data.armatures.new(name)
        arm = bpy.data.objects.new(name, data)
        coll.objects.link(arm)
        # wire, not octahedra, and not drawn through the mesh: a cockpit rig is
        # hundreds to thousands of bones and the shapes are noise on top of the
        # geometry you are trying to work on
        arm.show_in_front = False
        data.display_type = 'WIRE'
        if self.root is not None:
            # the whole rig hangs off one Empty at the world centre: move that
            # and the aircraft moves, which is the one thing a collection full
            # of bone-parented meshes cannot do for itself
            arm.parent = self.root
            arm.matrix_parent_inverse.identity()

        previous = self.context.view_layer.objects.active
        for other in self.context.selected_objects:
            other.select_set(False)
        self.context.view_layer.objects.active = arm
        arm.select_set(True)
        bpy.ops.object.mode_set(mode='EDIT')
        for bone_name, head, cmd, parent_bone, block in self.specs:
            bone = data.edit_bones.new(bone_name)
            bone.head = head
            bone.tail = head + Vector((0.0, 0.1, 0.0))
            bone.roll = 0.0
            bone.use_connect = False        # a connected bone forces location = 0
            if parent_bone is not None:
                bone.parent = data.edit_bones[parent_bone]
        bpy.ops.object.mode_set(mode='OBJECT')
        self.context.view_layer.objects.active = previous
        for other in self.context.selected_objects:
            other.select_set(False)
        if previous is not None:
            previous.select_set(True)

        action = bpy.data.actions.new(name + "_anim")
        action.use_fake_user = True
        arm.animation_data_create()
        arm.animation_data.action = action

        for bone_name, head, cmd, parent_bone, block_index in self.specs:
            bone = data.bones[bone_name]
            if bone_name == self.tips.get(block_index):
                self._bone_show_hide(bone, block_index)
            if cmd is None:
                # a block that only shows or hides: the rule is the bone's whole
                # job, there is no transform to key
                continue
            parent_rest = (data.bones[parent_bone].matrix_local
                           if parent_bone else Matrix.Identity(4))
            base_matrix = parent_rest.inverted() @ bone.matrix_local
            dataref = cmd.get("dataref")
            if dataref:
                values = XM.command_values(cmd)
                if len(values) < 2:
                    self.warn("block %d: %s on bone %s has %d key(s); "
                              "XPlane2Blender needs 2"
                              % (block_index, cmd["kind"], bone_name,
                                 len(values)))
                frames = [float(i + 1) for i in range(len(values))]
            else:
                values = [None]
                frames = [1.0]
            # AXIS_ANGLE: the file's rotation is an axis plus a keyed angle, so
            # the bone stores exactly that - no quaternion, no short-arc
            # surprises, and a 350 degree sweep keeps its direction
            pose_bone = arm.pose.bones[bone_name]
            pose_bone.rotation_mode = 'AXIS_ANGLE'
            rotates = cmd["kind"] in (CMD_ROTATE, CMD_ROTATE_KEYS)
            locations, angles = [], []
            for value in values:
                pose = dict(self.pose)
                if dataref and value is not None:
                    pose[dataref] = value
                m = Matrix([list(row) for row in
                            XA.to_bl(XA.cmd_matrix(cmd, pose))])
                loc, _quat, _scale = (base_matrix.inverted() @ m).decompose()
                locations.append(loc)
                angles.append(math.radians(XM._angle_at(
                    cmd, value if value is not None else 0.0)) if rotates
                    else 0.0)
            axis_bl = XM._unit(_xp_to_bl(cmd.get("axis") or (0.0, 1.0, 0.0)))
            path = 'pose.bones["%s"]' % bone_name
            for index in range(3):
                XM._write_curve(action, arm, path + ".location", index, frames,
                                [loc[index] for loc in locations])
            XM._write_curve(action, arm, path + ".rotation_axis_angle", 0,
                            frames, angles)
            for index in range(3):
                XM._write_curve(action, arm, path + ".rotation_axis_angle",
                                index + 1, frames,
                                [axis_bl[index]] * len(frames))
            if dataref and len(values) >= 2:
                datarefs = bone.xplane.datarefs
                dr = datarefs.add()
                dr.path = dataref
                dr.anim_type = "transform"
                slot = len(datarefs) - 1
                dataref_path = ('bones["%s"].xplane.datarefs[%d].value'
                                % (bone_name, slot))
                for frame, value in zip(frames, values):
                    dr.value = value
                    arm.data.keyframe_insert(
                        data_path=dataref_path, frame=frame,
                        group="XPlane Datarefs " + bone_name)
                self.frames_used = max(self.frames_used, int(max(frames)))
        return arm

    def _bone_show_hide(self, bone, block_index):
        """ANIM_show/hide of a block -> datarefs on that block's innermost bone.

        XPlane2Blender writes these inside the bone's own ``ANIM_begin``, so the
        rule wraps the bone's geometry and every bone under it - the same
        subtree the file's block covers.  The innermost bone is the one whose
        daddies are the block's transforms and whose children are the nested
        blocks, which is why the rule rides there and not on the first bone.
        """
        block = self.block_by_index.get(block_index)
        for cmd in (block.commands if block is not None else []):
            if cmd["kind"] not in (CMD_SHOW, CMD_HIDE) or not cmd.get("dataref"):
                continue
            dr = bone.xplane.datarefs.add()
            dr.path = cmd["dataref"]
            dr.anim_type = "show" if cmd["kind"] == CMD_SHOW else "hide"
            dr.show_hide_v1 = cmd.get("v1", 0.0)
            dr.show_hide_v2 = cmd.get("v2", 0.0)
            if cmd.get("loop"):
                dr.loop = cmd["loop"]
            self.visibility_placed += 1

    # -- main -------------------------------------------------------------
    def run(self):
        header = self.header
        text = header.get(XM.PROP_DUMP, "")
        if not text:
            raise RuntimeError("%s has no stored OBJ8 text" % header.name)
        obj8 = OC.parse_text(text)
        anim = XA.build(obj8)

        src = header.get(XM.PROP_SOURCE, "")
        # The tree is named after the file the geometry came from, extension
        # included: one import reads as "C90B_int_decals.obj" in the outliner,
        # with the Empty and the armature hanging off that same label.  `base`
        # (no extension) stays the name for meshes, materials and the layer,
        # because those feed the exported file name and XP2B appends its own
        # ".obj" only when the name does not already end in a supported suffix.
        filename = os.path.basename(src) or header.name
        base = os.path.splitext(filename)[0]
        # merge identical output vertices: without it XP2B writes one VT per
        # triangle corner, so a cockpit with 14,477 shared vertices exports
        # 44,070 of them (the index count) and three times the file size
        self.scene.xplane.optimize = True
        coll = self._own_collection(filename)
        coll.xplane.is_exportable_collection = True
        coll.xplane.layer.name = base
        coll.xplane.layer.export_type = "aircraft"
        self._apply_texture_options(obj8, coll)
        # PARTICLE_SYSTEM is the layer's *Particle System File*: XP2B writes the
        # record from there, and it treats an emitter without one as an export
        # error, so the global has to land before the emitters are built
        self._apply_particle_system(obj8, coll)
        # ATTR_LOD lives on the collection (XPlane2Blender's model), and every
        # leaf and light is tagged with the level it was drawn under
        lods = self._apply_lods(obj8, coll)

        leaf_objs = json.loads(header.get(XM.PROP_LEAF_OBJS, "[]"))
        leaf_states = json.loads(header.get(XM.PROP_LEAF_STATES, "[]"))
        self._cockpit_regions(obj8, coll)

        # visibility datarefs are not transforms: keep them out of the frame map
        self.anim = anim
        # the rest pose a bone chain is laid out at: every dataref at its own
        # lowest keyed value, which is where the file's first key sits
        self.pose = {dref: lo for dref, (lo, hi) in anim.ranges().items()}
        # the whole rig (armature plus every batch) hangs off one Empty at the
        # world centre: moving it moves the aircraft, which is the only way a
        # collection of bone-parented meshes can be placed as a whole
        self.root = _empty(coll, filename + ".worldcenter", None)
        self.prepare_tree(anim)
        self.armature = self.build_armature(coll, filename) if self.specs else None
        for leaf, chain in anim.leaves.items():
            for block in reversed(chain):
                if self.tips.get(block.index):
                    self.leaf_anchor[leaf] = _Anchor(
                        arm=self.armature, bone=self.tips[block.index])
                    break
        anchors = {}
        for block in anim.blocks:
            parent = None
            if block.parent is not None:
                parent = anchors.get(block.parent.index)
            if self.armature is not None and self.tips.get(block.index):
                anchors[block.index] = _Anchor(arm=self.armature,
                                               bone=self.tips[block.index])
                continue
            # nothing to animate in this block: its children keep the parent's
            # anchor, and top-level geometry hangs off the root Empty
            anchors[block.index] = parent if parent is not None else _Anchor(
                obj=self.root)

        # Lights need the block anchors, so they are built here rather than with
        # the rest of the file: a light inside an ANIM block has to ride that
        # block's bone, exactly like the geometry
        lights_made = self._build_lights(obj8, coll, anchors, lods)
        # emitters ride their block's bone exactly like lights do, which is why
        # they are built here and not with the rest of the file
        emitters_made = self._build_emitters(obj8, coll, anchors)

        self._report_dropped_commands(obj8)

        frame_end = max(1, self.frames_used)
        self.scene.frame_start = 1
        self.scene.frame_end = max(self.scene.frame_end, frame_end)

        # move the geometry under its block, keeping its local transform
        made = 0
        for leaf_index, name in enumerate(leaf_objs):
            if not name:
                continue
            ob = bpy.data.objects.get(name)
            if ob is None:
                self.warn("part %d (%s) is gone from the scene"
                          % (leaf_index, name))
                continue
            chain = anim.leaves.get(leaf_index) or []
            anchor = self.leaf_anchor.get(leaf_index)
            if anchor is None and chain:
                anchor = anchors.get(chain[-1].index)

            for c in list(ob.users_collection):
                if c != coll:
                    c.objects.unlink(ob)
            if ob.name not in coll.objects:
                coll.objects.link(ob)
            if anchor is not None:
                anchor.attach(ob)
            else:
                _Anchor(obj=self.root).attach(ob)
            ob.location = (0.0, 0.0, 0.0)
            ob.rotation_mode = 'XYZ'
            ob.rotation_euler = (0.0, 0.0, 0.0)
            ob.scale = (1.0, 1.0, 1.0)
            # The importer bakes ANIM_show/hide into a keyframed
            # hide_viewport, and XPlane2Blender reads that flag when it
            # collects: a part hidden at frame 1 becomes
            # "export_animation_only" and its geometry is dropped without a
            # word. The visibility now lives on the datarefs above, so the
            # preview Action goes away.
            ob.animation_data_clear()
            ob.hide_set(False)
            ob.hide_viewport = False
            ob.hide_render = False

            state = leaf_states[leaf_index] if leaf_index < len(
                leaf_states) else []
            if any(cat == "draw" and "ATTR_draw_disable" in raws
                   for cat, raws in state):
                self._no_draw_material(ob, base)
            self._apply_light_level(ob, state)
            self._apply_leaf_lod(ob, leaf_index, lods)
            self._apply_camera_state(ob, state)
            self._apply_shade_and_shiny(ob, state)
            self._apply_surface_state(ob, state)
            self._apply_cockpit_state(ob, state, coll)
            if apply_manipulator(ob, state, self.warnings,
                                 rig=self._drag_rig(leaf_index)):
                coll.xplane.layer.export_type = "cockpit"
            self._apply_detent_ranges(ob, state)
            self._apply_manip_wheel(ob, state)
            self._check_state(state, ob.name)
            made += 1

        self._apply_magnets(obj8, leaf_objs)

        if not made and not lights_made and not emitters_made:
            self.warn("%s: no geometry was moved" % base)

        if self.specs:
            # not a loss: the rig is one bone per block by design, and XP2B
            # writes a bone's show/hide inside that bone's own ANIM_begin, so
            # the exported block structure stays 1:1 with the file
            coll["xp2b_rig"] = ("%d bone(s) under %s; %d ANIM_show/hide rule(s) "
                                "on bones" % (len(self.specs), self.root.name,
                                              self.visibility_placed))
        return coll

    def _apply_texture_options(self, obj8, coll):
        """Hand the file's texture set to XPlane2Blender's layer.

        XP2B does not read texture paths out of the material node tree: it
        writes TEXTURE / TEXTURE_LIT / TEXTURE_NORMAL / TEXTURE_MAP from the
        *layer* fields (Collection > X-Plane > Textures: Default, Night,
        Normal/Specular, and on 12.0+ Normal, Material/Gloss, Gloss).  The
        material graph this importer builds is the viewport preview; without
        these fields an imported object exports with no textures at all.

        Paths are stored **as the file spells them**.  An OBJ's texture names
        are relative to the .obj itself and the sim resolves them the same way,
        so keeping `livery_NML.png` short is what makes the written path match
        the source; absolutising it only produces a 15-level `../../..` once
        XP2B relativises it against an export directory.  XP2B relativises
        whatever it is given against the .obj's output directory, so a file
        kept next to its textures lands back as the same one-word name.
        """
        layer = coll.xplane.layer

        def first(words):
            return words[0] if words else ""

        tex = obj8.global_value("TEXTURE")
        lit = obj8.global_value("TEXTURE_LIT")
        nrm = obj8.global_value("TEXTURE_NORMAL")
        maps = XM.texture_map_usages(obj8)
        layer.texture = first(tex)
        layer.texture_lit = first(lit)
        layer.texture_normal = first(nrm)
        layer.texture_map_normal = maps.get("normal", "")
        layer.texture_map_material_gloss = maps.get("material_gloss", "")
        layer.texture_map_gloss = maps.get("gloss", "")
        layer.normal_metalness = obj8.global_value("NORMAL_METALNESS") is not None
        layer.blend_glass = obj8.global_value("BLEND_GLASS") is not None

    def _apply_particle_system(self, obj8, coll):
        """PARTICLE_SYSTEM -> the layer's *Particle System File*.

        XPlane2Blender writes the record from the layer field, and an EMITTER
        without one cancels the export, so the two halves belong together.  The
        path is kept exactly as the file spells it: a .pss resolves relative to
        the .obj, which is the directory the export writes into (the A330 says
        ``particles/aircraft_system.pss``).
        """
        words = obj8.global_value("PARTICLE_SYSTEM")
        if not words:
            return
        coll.xplane.layer.particle_system_file = words[0]

    def _apply_lods(self, obj8, coll):
        """ATTR_LOD -> the collection's LOD levels; returns a _Lods.

        ATTR_LOD is a stream command: geometry after it draws within that
        distance range until the next one, and a file may have several
        (F4D_Wings.obj has 0-1000 then 0-4000).  XPlane2Blender keeps the ranges
        on the collection and marks each object with the levels it belongs to,
        so the ranges are collected in file order and every leaf *and every
        light* is tagged with the index it fell under.

        Tagging means `xplane.override_lods = True` as well as the `lod[i]`
        flag: XP2B only reads `lod[]` when that toggle is on, and otherwise
        inherits the parent's buckets - which are all False, so every object in
        a file that declares a LOD range was dropped from the export (F4D_Wings
        lost all 17 draws and all 24 lights that way).
        """
        lods = _Lods()
        current = None
        for ev_index, (kind, payload) in enumerate(obj8.events):
            if kind == OC.EV_RAW and payload:
                words = payload.split()
                if words and words[0] == "ATTR_LOD" and len(words) >= 3:
                    try:
                        pair = (int(float(words[1])), int(float(words[2])))
                    except ValueError:
                        self.warn("%s: ATTR_LOD arguments are not numbers"
                                  % coll.xplane.layer.name)
                        continue
                    if pair not in lods.ranges:
                        lods.ranges.append(pair)
                    current = pair
                elif words and words[0].startswith("LIGHT") and lods.ranges:
                    lods.light[ev_index] = lods.ranges.index(current) \
                        if current is not None else None
            elif kind == OC.EV_LEAF and lods.ranges:
                lods.leaf[payload] = lods.ranges.index(current) \
                    if current is not None else None

        if not lods.ranges:
            return lods
        if len(lods.ranges) > MAX_LOD_LEVELS:
            self.warn("%s: %d ATTR_LOD ranges, XPlane2Blender holds %d - the "
                      "rest are dropped"
                      % (coll.xplane.layer.name, len(lods.ranges),
                         MAX_LOD_LEVELS))
            lods.ranges = lods.ranges[:MAX_LOD_LEVELS]

        layer = coll.xplane.layer
        try:
            layer.lods = str(len(lods.ranges))
            for i, (near, far) in enumerate(lods.ranges):
                if i >= len(layer.lod):
                    layer.lod.add()
                layer.lod[i].near = near
                layer.lod[i].far = far
        except (AttributeError, TypeError) as exc:
            self.warn("%s: could not set the collection LOD levels: %s"
                      % (coll.xplane.layer.name, exc))
        return lods

    def _apply_leaf_lod(self, ob, leaf_index, lods):
        """Mark the object with the LOD level it was drawn under."""
        lods.tag(ob, lods.leaf.get(leaf_index))

    def _make_light(self, words, coll):
        """One LIGHT_* line -> one Blender light object, plus its Spec.

        XPlane2Blender exports lights from light *objects*: the position comes
        from the object and the rest of the line goes into the light data
        verbatim, so the argument text is kept as-is instead of being parsed
        into fields (every light name has its own parameter list).

        The Spec (`xplights.describe`) is the *preview* half: colour, power,
        cone and the direction the lamp faces.  It never touches `params`, so it
        cannot reach the export; the caller aims the object once the hierarchy
        is built.
        """
        kw = words[0]
        if kw == "LIGHT_PARAM":
            if len(words) < 5:
                return None
            name = words[1]
            try:
                pos = (float(words[2]), float(words[3]), float(words[4]))
            except ValueError:
                return None
            params = " ".join(words[5:])
            if not params:
                # X-Plane accepts a param light with no arguments, but XP2B
                # rejects one with an empty parameter box and cancels the whole
                # export - so this line has to stay unconverted, loudly.
                self.warn("%s: LIGHT_PARAM %s has no parameters; XPlane2Blender "
                          "cancels an export over an empty parameter box, so the "
                          "line was not converted"
                          % (coll.xplane.layer.name, name))
                return None
            light_type = "param"
        elif kw == "LIGHT_NAMED":
            if len(words) < 5:
                return None
            name = words[1]
            try:
                pos = (float(words[2]), float(words[3]), float(words[4]))
            except ValueError:
                return None
            params = ""
            light_type = "named"
        else:
            return None

        spec = XL.describe(name, params) if light_type == "param" else None
        data = bpy.data.lights.new(name, spec.kind if spec else 'POINT')
        try:
            data.xplane.type = light_type
            data.xplane.name = name
            data.xplane.params = params
        except (AttributeError, TypeError) as exc:
            self.warn("%s: could not configure the light: %s" % (name, exc))
            return None
        if spec is not None:
            # preview only: XP2B echoes `params` verbatim and reads neither the
            # colour nor the power of a param light (xplights.py)
            XL.apply(data, spec)
        ob = bpy.data.objects.new(name, data)
        ob.location = _xp_to_bl(pos)
        ob.rotation_mode = 'XYZ'
        coll.objects.link(ob)
        return ob, spec

    def _build_lights(self, obj8, coll, anchors=None, lods=None):
        """Turn every LIGHT_* line into a light object; returns how many.

        A file can be nothing but lights (Cessna_172SP lights.obj has
        POINT_COUNTS 0 0 0 0 and sixteen LIGHT_PARAM lines), and dropping those
        left an empty collection behind.

        A light inside an ANIM block rides that block's anchor, the same way the
        geometry does.  Leaving them all at the collection root exports every
        light outside the ANIM block it came from, and a nav light driven by
        ``sim/flightmodel2/wing/rudder1_deg`` then stops following the rudder.

        Aiming happens at the end, after one view-layer update: the rotation is
        set as a world matrix, so it has to be computed against the parent bone
        that is really there (xplights.aim), and a stale translation read from
        the depsgraph would move the light in the export.
        """
        by_begin = {}
        for blk in getattr(self.anim, "blocks", None) or []:
            if getattr(blk, "begin_ev", None) is not None:
                by_begin[blk.begin_ev] = blk
        stack = []
        made = 0
        skipped = 0
        unknown = set()
        aiming = []
        dimmers = []
        for ev_index, (kind, payload) in enumerate(obj8.events):
            if kind == OC.EV_ANIM_BEGIN:
                stack.append(by_begin.get(ev_index))
                continue
            if kind == OC.EV_ANIM_END:
                if stack:
                    stack.pop()
                continue
            if kind != OC.EV_RAW or not payload:
                continue
            words = payload.split()
            if not words or not words[0].startswith("LIGHT"):
                continue
            made_light = self._make_light(words, coll)
            if made_light is None:
                skipped += 1
                continue
            ob, spec = made_light
            # a light inside an ATTR_LOD region is a draw command like any
            # other: untagged, XP2B drops it from every bucket (see _Lods)
            if lods is not None:
                lods.tag(ob, lods.light.get(ev_index))
            if spec is None and words[0] == "LIGHT_PARAM" and len(words) > 1:
                unknown.add(words[1])
            made += 1
            anchor = None
            if anchors:
                for blk in reversed(stack):
                    if blk is not None and blk.index in anchors:
                        anchor = anchors[blk.index]
                        break
            if anchor is not None:
                anchor.attach(ob)
            elif getattr(self, "root", None) is not None:
                _Anchor(obj=self.root).attach(ob)
            if spec is not None and spec.direction is not None:
                aiming.append((ob, spec))
            if spec is not None and spec.family == "cockpit" \
                    and spec.index is not None:
                dimmers.append((ob, spec, words[1]))
        if aiming:
            # Snapshot every world matrix BEFORE touching anything, then aim:
            # assigning one object's matrix_world invalidates the depsgraph, so
            # a read inside the same loop hands back a stale matrix - and a
            # stale read means the light keeps a wrong translation and XP2B
            # writes both a moved light and an ANIM_rotate to "fix" its aim.
            bpy.context.view_layer.update()
            placed = [(ob, spec.direction, ob.matrix_world.copy())
                      for ob, spec in aiming]
            for ob, direction, world in placed:
                XL.aim(ob, direction, world)
        for ob, spec, name in dimmers:
            XL.bind_dimmer(ob, spec, bpy.context.scene, name)
        if unknown:
            # No table row means no candidate mapping, so nothing was applied
            # and the light keeps Blender's defaults.  Report the names instead
            # of guessing an interpretation of somebody's custom lights.txt.
            names = sorted(unknown)
            self.warn("%s: no light parameters known for %s%s; those lights "
                      "keep Blender's default colour and power"
                      % (coll.xplane.layer.name, ", ".join(names[:6]),
                         " and %d more" % (len(names) - 6)
                         if len(names) > 6 else ""))
        if skipped:
            self.warn("%s: %d light line(s) could not be converted"
                      % (coll.xplane.layer.name, skipped))
        return made

    def _walk_raw(self, obj8, anchors):
        """Yield ``(line, anchor)`` for every raw command in the file.

        ``anchor`` is the innermost enclosing ANIM block's anchor - the bone the
        command has to ride - or None for a command outside every block.  Same
        stack walk ``_build_lights`` does inline: an emitter that lands outside
        its block exports without the transform that drives it, so a wingtip
        vortex tied to ``wing_tip_deflection_deg`` would sit still.
        """
        by_begin = {}
        for blk in getattr(self.anim, "blocks", None) or []:
            if getattr(blk, "begin_ev", None) is not None:
                by_begin[blk.begin_ev] = blk
        stack = []
        for ev_index, (kind, payload) in enumerate(obj8.events):
            if kind == OC.EV_ANIM_BEGIN:
                stack.append(by_begin.get(ev_index))
                continue
            if kind == OC.EV_ANIM_END:
                if stack:
                    stack.pop()
                continue
            if kind != OC.EV_RAW or not payload:
                continue
            anchor = None
            if anchors:
                for blk in reversed(stack):
                    if blk is not None and blk.index in anchors:
                        anchor = anchors[blk.index]
                        break
            yield payload, anchor

    def _make_emitter(self, words, coll):
        """One EMITTER line -> the Empty XPlane2Blender writes it back from.

        ``EMITTER <name> <x> <y> <z> <psi> <the> <phi> [index]`` - a position,
        three Euler angles in degrees, and the entry the emitter takes when one
        .pss particle is attached several times (the F-14 mirrors every exhaust
        emitter at index 0 and 1).

        The Empty carries XPlane2Blender's own *Particle Emitter* settings
        (``special_empty_props``), which is what makes the name, the index and
        the angles editable in the N-panel and what it exports the line from -
        a plain Empty exports nothing, and a custom attribute would be both
        unreadable and ignored, since XP2B builds the whole line itself.
        """
        if len(words) < 8:
            return None
        name = words[1]
        try:
            pos = (float(words[2]), float(words[3]), float(words[4]))
            psi, the, phi = (float(words[5]), float(words[6]), float(words[7]))
        except ValueError:
            return None
        ob = bpy.data.objects.new(name, None)
        ob.empty_display_type = 'PLAIN_AXES'
        ob.empty_display_size = 0.15
        ob.rotation_mode = 'XYZ'
        ob.location = _xp_to_bl(pos)
        # XP2B writes `EMITTER name x y z -eulerZ eulerX eulerY`
        # (xplane_empty.py:87), so the inverse is exactly that: psi/the/phi
        # become -Z/X/Y.  Anything else agrees at the numbers and disagrees in
        # the sim, which is the kind of error that only shows up as "the smoke
        # comes out sideways".
        ob.rotation_euler = (math.radians(the), math.radians(phi),
                             math.radians(-psi))
        props = ob.xplane.special_empty_props
        props.special_type = "emitter_particle"
        props.emitter_props.name = name
        if len(words) > 8:
            try:
                index = int(float(words[8]))
            except ValueError:
                index = -1
            if index >= 0:
                props.emitter_props.index_enabled = True
                props.emitter_props.index = index
        coll.objects.link(ob)
        return ob

    def _build_emitters(self, obj8, coll, anchors=None):
        """Every EMITTER line -> an Empty; returns how many were made.

        Nothing is made when XPlane2Blender would refuse the file: an emitter
        with no .pss on the layer, a .pss path that is not one, or a scene
        version older than 1130 (the release emitters arrived in) all cancel or
        silently skip the export, and a file that stopped exporting is worse
        than a list of emitters that was reported instead.
        """
        lines = [(line, anchor) for line, anchor in self._walk_raw(obj8, anchors)
                 if line.split()[0] == "EMITTER"]
        if not lines:
            return 0
        layer = coll.xplane.layer
        pss = (getattr(layer, "particle_system_file", "") or "").strip()
        if not pss:
            self.warn("%s: %d EMITTER line(s) are not converted - there is no "
                      "PARTICLE_SYSTEM record, and XPlane2Blender cancels the "
                      "whole export over an emitter without a .pss file. Set "
                      "Layer > Particles > Particle System File by hand, then "
                      "add the emitters as Empties of type Particle Emitter."
                      % (layer.name, len(lines)))
            return 0
        if not pss.endswith(".pss"):
            self.warn("%s: %d EMITTER line(s) are not converted - the layer's "
                      "particle file %r is not a .pss, which XPlane2Blender "
                      "treats as an export error" % (layer.name, len(lines), pss))
            return 0
        try:
            version = int(self.scene.xplane.version)
        except (AttributeError, TypeError, ValueError):
            version = 0
        if version < 1130:
            self.warn("%s: %d EMITTER line(s) are not converted - the scene's "
                      "X-Plane version is %s and XPlane2Blender writes "
                      "PARTICLE_SYSTEM and EMITTER from 1130 up"
                      % (layer.name, len(lines), self.scene.xplane.version))
            return 0
        made = 0
        for line, anchor in lines:
            ob = self._make_emitter(line.split(), coll)
            if ob is None:
                self.warn("%s: EMITTER line is not converted, it is not a name "
                          "and six numbers: %s" % (layer.name, line))
                continue
            if anchor is not None:
                anchor.attach(ob)
            elif getattr(self, "root", None) is not None:
                _Anchor(obj=self.root).attach(ob)
            made += 1
        return made

    def _report_dropped_commands(self, obj8):
        """IF blocks are commands XPlane2Blender cannot take from us - say how
        many, per file. Silence here would look like success.

        ATTR_LOD becomes the collection's LOD levels (see ``_apply_lods``) and
        LIGHT_* becomes light objects (see ``_build_lights``), so neither is in
        this list any more.
        """
        counts = {}
        for kind, payload in obj8.events:
            if kind != OC.EV_RAW or not payload:
                continue
            kw = payload.split()[0]
            if kw in ("IF", "ENDIF"):
                key = "IF/ENDIF blocks"
            else:
                continue
            counts[key] = counts.get(key, 0) + 1
        for key, n in sorted(counts.items()):
            self.warn("%d %s were dropped: XPlane2Blender cannot be handed them, "
                      "re-add them there by hand" % (n, key))

    # -- per-part helpers -------------------------------------------------
    def _no_draw_material(self, ob, base):
        """ATTR_draw_disable -> a material copy with XP2B draw = False.

        X-Plane never draws these objects (collision boxes, shadow casters,
        invisible walls), so the copy is made fully transparent as well.  The
        checkbox in XPlane2Blender's panel is what keeps them out of the
        *exported* file; alpha 0 is what keeps the solid block out of the
        viewport and any render of the scene.
        """
        self._material_set(ob, [("draw", False)], "nodraw")
        for slot in ob.material_slots:
            _make_transparent(slot.material)

    def _set_backface_culling(self, ob, cull, tag):
        """ATTR_cull / ATTR_no_cull -> Blender's flag, and back out again.

        Same cloning rule as ``_material_set``: a material carries one value, so
        the batch that disagrees gets its own copy, cached per (material, value)
        so a file with three looks does not produce 160 materials.

        XPlane2Blender has no cull field, but it does have custom attributes and
        writes those verbatim - so the copy carries ATTR_no_cull there and the
        line survives the export.  ATTR_cull needs nothing written: single-sided
        is already what X-Plane does by default.
        """
        for slot in ob.material_slots:
            mat = slot.material
            if mat is None or mat.use_backface_culling == cull:
                continue
            if mat.get(XM.PROP_GLASS):
                # a BLEND_GLASS canopy is two-sided in X-Plane; forcing the
                # single-sided default would make it vanish from the cockpit
                continue
            key = (mat.as_pointer(), "cull", cull)
            clone = self.material_cache.get(key)
            if clone is None:
                clone = mat.copy()
                clone.name = mat.name + "_" + tag
                clone.use_backface_culling = cull
                if not cull:
                    _add_custom_attribute(clone.xplane, "ATTR_no_cull")
                self.material_cache[key] = clone
            slot.material = clone

    def _apply_light_level(self, ob, state):
        """ATTR_light_level -> the *object's* X-Plane light level.

        XP2B has the same fields on the object and on the material, and writes
        the material's only when the object has none.  The object is the right
        home here, for two reasons:

        * the line is per geometry command and an object *is* one command, so
          nothing has to be cloned -- a material carries one value, and
          cloning it per level cost 48 materials on a C90B cockpit;
        * XP2B's material path loses levels: it emits ATTR_light_level when the
          material changes and a reset when leaving it, but does not re-emit the
          level for the next batch that shares the earlier value.  On
          C90B_cockpit.obj that silently dropped 21 of 33 batches
          (laminar/c90/warning/gear_handle).  Writing it per object survives any
          batch order.

        It also sidesteps validatePanel: XP2B refuses a *panel material* that
        overrides the level (and cancels the whole export), but a panel batch's
        object-level override is legal in X-Plane and is what the source file
        has, so those lines are now kept instead of dropped.
        """
        for cat, raws in state or []:
            if cat != "light_level":
                continue
            for raw in raws:
                words = raw.split()
                if not words or words[0] != "ATTR_light_level":
                    continue
                try:
                    v1, v2, dref = float(words[1]), float(words[2]), words[3]
                except (IndexError, ValueError):
                    self.warn("%s: ATTR_light_level arguments not parsed"
                              % ob.name)
                    continue
                x = ob.xplane
                x.lightLevel = True
                x.lightLevel_v1 = v1
                x.lightLevel_v2 = v2
                x.lightLevel_dataref = dref

    def _apply_camera_state(self, ob, state):
        """ATTR_solid_camera -> material.xplane.solid_camera (also a material attr)."""
        for cat, raws in state or []:
            if cat != "camera":
                continue
            for raw in raws:
                kw = raw.split()[0] if raw.split() else ""
                if kw == "ATTR_solid_camera":
                    value = True
                elif kw == "ATTR_no_solid_camera":
                    value = False
                else:
                    continue
                self._material_set(ob, [("solid_camera", value)], "cam")

    def _apply_shade_and_shiny(self, ob, state):
        """ATTR_shade_flat/smooth -> polygon shading, ATTR_shiny_rat -> specular.

        Both are things XPlane2Blender derives from Blender itself, so copying
        the value across is the whole conversion.
        """
        for cat, raws in state or []:
            if cat == "shade":
                for raw in raws:
                    kw = raw.split()[0] if raw.split() else ""
                    if kw not in ("ATTR_shade_flat", "ATTR_shade_smooth"):
                        continue
                    smooth = kw == "ATTR_shade_smooth"
                    ob.data.polygons.foreach_set("use_smooth",
                                                 [smooth] * len(ob.data.polygons))
                    ob.data.update()
            elif cat == "shiny":
                for raw in raws:
                    words = raw.split()
                    if not words or words[0] != "ATTR_shiny_rat":
                        continue
                    try:
                        value = float(words[1])
                    except (IndexError, ValueError):
                        continue
                    for slot in ob.material_slots:
                        if slot.material is not None:
                            slot.material.specular_intensity = value

    def _apply_surface_state(self, ob, state):
        """blend / shadow / poly_os / cull / hard / draped / hud / depth.

        Every state line in the table now has a home:

        * blend, shadow, poly_os -> the matching XPlane2Blender material fields
        * cull -> Blender's own ``use_backface_culling`` (XP2B has no field for
          it) plus an ATTR_no_cull custom attribute so the export keeps the line
        * hard -> Material > X-Plane > Surface Type (and Deck), indexed by the
          number ATTR_hard carries
        * draped -> Material > X-Plane > Draped
        * hud_glass -> Object > X-Plane > HUD Glass
        * depth -> XPlane2Blender has no field at all, so the line rides along
          as a custom attribute, which is written back verbatim

        ATTR_draped is scenery-only in X-Plane and XP2B cancels the export on a
        non-scenery object that sets it ("Must not be draped"), so that one is
        reported rather than silently applied.
        """
        blend = {"ATTR_blend": "on", "ATTR_no_blend": "off",
                 "ATTR_shadow_blend": "shadow"}
        flags = {"ATTR_shadow": ("shadow_local", True),
                 "ATTR_no_shadow": ("shadow_local", False)}
        cull = {"ATTR_cull": True, "ATTR_no_cull": False}
        draped = {"ATTR_draped": True, "ATTR_no_draped": False}
        hud = {"ATTR_hud_glass": True, "ATTR_hud_reset": False}
        for cat, raws in state or []:
            for raw in raws:
                words = raw.split()
                if not words:
                    continue
                kw = words[0]
                if kw in blend:
                    pairs = [("blend_v1000", blend[kw])]
                    # the ratio is optional: ATTR_no_blend and
                    # ATTR_shadow_blend alone mean "0.5", the spec default
                    args = _state_args(words)
                    if kw != "ATTR_blend" and args:
                        # the material defaults to 0.5, so dropping the file's
                        # number silently changes what the sim cuts away
                        try:
                            cutoff = float(args[0])
                        except ValueError:
                            self.warn("%s: %s cutoff %r is not a number"
                                      % (ob.name, kw, args[0]))
                        else:
                            pairs.append(("blendRatio",
                                          min(1.0, max(0.0, cutoff))))
                    self._material_set(ob, pairs, kw[:12])
                elif kw in flags:
                    field, value = flags[kw]
                    self._material_set(ob, [(field, value)], kw[:12])
                elif kw in cull:
                    self._set_backface_culling(ob, cull[kw], kw[:12])
                elif kw in ("ATTR_hard", "ATTR_hard_deck"):
                    # the argument is a surface NAME, not a number: "Starting in
                    # X-Plane 8.50 ATTR_hard can be followed by an additional
                    # surface name"; no argument means a smooth surface
                    name = words[1].strip().lower() if len(words) > 1 else "smooth"
                    if name not in SURFACE_TYPES:
                        self.warn("%s: %s has an unknown surface %r"
                                  % (ob.name, kw, name))
                        continue
                    pairs = [("surfaceType", name)]
                    if kw == "ATTR_hard_deck":
                        pairs.append(("deck", True))
                    self._material_set(ob, pairs, "hard")
                elif kw == "ATTR_no_hard":
                    self._material_set(ob, [("surfaceType", "none"),
                                            ("deck", False)], "nohard")
                elif kw in draped:
                    self._material_set(ob, [("draped", draped[kw])], kw[:12])
                elif kw in hud:
                    try:
                        ob.xplane.hud_glass = hud[kw]
                    except AttributeError:
                        self.warn("%s: this XPlane2Blender has no hud_glass "
                                  "field" % ob.name)
                elif kw in ("ATTR_depth", "ATTR_no_depth"):
                    _add_custom_attribute(ob.xplane, kw)
                elif kw == "ATTR_poly_os" and len(words) > 1:
                    try:
                        value = int(float(words[1]))
                    except ValueError:
                        self.warn("%s: ATTR_poly_os value not a number" % ob.name)
                        continue
                    self._material_set(ob, [("poly_os", value)], "poly_os")
                elif kw == "ATTR_no_poly_os":
                    self._material_set(ob, [("poly_os", 0)], "poly_os")

    def _drag_rig(self, leaf_index):
        """What a drag_rotate manipulator can take from this batch's bones.

        XPlane2Blender derives a drag_rotate's pivot, axis, both angle ends and
        the detent lift from the mesh's ancestor bones: it walks up to the first
        animated bone and wants it to rotate only, and for the detent variant a
        bone that translates only underneath the rotation.  The rig is one bone
        per transform command, so the file's own block chain answers whether
        those bones exist - a block carrying two keyed transforms becomes one
        bone with two animations, which fits neither type.

        Returns ``(value range, end angles, lift)`` for the rotation the chain
        provides - lift is None when there is no detent bone - or None when the
        chain does not line up with what XP2B looks for.  The numbers are what
        XP2B writes back verbatim, so they are the thing a caller has to compare
        against the manipulator line; the datarefs are the file's own and are
        copied from the line, not from here.
        """
        rotate = detent = None
        for block in reversed(self.anim.leaves.get(leaf_index) or []):
            keyed = [cmd for cmd in block.commands
                     if cmd.get("dataref")
                     and cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS,
                                         XA.CMD_TRANS, XA.CMD_TRANS_KEYS)]
            if not keyed:
                continue
            if len(keyed) > 1:
                return None
            cmd = keyed[0]
            rotates = cmd["kind"] in (XA.CMD_ROTATE, XA.CMD_ROTATE_KEYS)
            if rotate is None and detent is None:
                if rotates:
                    rotate = cmd
                    break
                detent = cmd        # the rotation must be a bone above this one
                continue
            if not rotates:
                return None
            rotate = cmd
            break
        if rotate is None or (detent is not None and not detent.get("dataref")):
            return None

        values = _non_clamping(XM.command_values(rotate))
        if len(values) < 2:
            return None
        v_lo, v_hi = min(values), max(values)
        angles = (XM._angle_at(rotate, v_lo), XM._angle_at(rotate, v_hi))
        axis = rotate.get("axis") or (0.0, 1.0, 0.0)
        lift = None
        if detent is not None:
            dv = _non_clamping(XM.command_values(detent))
            if len(dv) < 2:
                return None
            pose_lo = {detent["dataref"]: min(dv)}
            pose_hi = {detent["dataref"]: max(dv)}
            lo = XA.cmd_matrix(detent, pose_lo)
            hi = XA.cmd_matrix(detent, pose_hi)
            lift = math.sqrt(sum((hi[i][3] - lo[i][3]) ** 2 for i in range(3)))
        return ((v_lo, v_hi), tuple(angles), tuple(axis), lift)

    def _apply_manip_wheel(self, ob, state):
        """ATTR_manip_wheel <delta> -> the manipulator's mouse-wheel rate.

        A modifier line, not a manipulator: it belongs to the ATTR_manip_* line
        of the same batch.  ATTR_manip_keyframe needs no conversion - XP2B
        writes those from the rotation keys it regenerates.
        """
        for cat, raws in state or []:
            if cat != "manip_wheel":
                continue
            for raw in raws:
                words = raw.split()
                try:
                    delta = float(words[1])
                except (IndexError, ValueError):
                    self.warn("%s: ATTR_manip_wheel needs a number" % ob.name)
                    continue
                ob.xplane.manip.wheel_delta = delta

    def _apply_magnets(self, obj8, leaf_objs):
        """MAGNET (a VR tablet / flashlight mount point, 1110+) -> custom attribute.

        XPlane2Blender has no field for it.  An object's custom attributes are
        written back verbatim, and X-Plane pins a magnet in model space - so
        riding the first object that has geometry puts the line into the command
        stream without moving the magnet, and writes it exactly once (the state
        snapshot is per-part, so a MAGNET parked there would reappear on every
        object after it).
        """
        lines = []
        for kind, payload in obj8.events:
            if kind != OC.EV_RAW or not payload:
                continue
            words = payload.split()
            if words and words[0] == "MAGNET":
                lines.append(payload)
        if not lines:
            return
        host = None
        for name in leaf_objs:
            if not name:
                continue
            ob = bpy.data.objects.get(name)
            if ob is not None and ob.type == 'MESH':
                host = ob
                break
        if host is None:
            self.warn("%d MAGNET entr%s were dropped: the file has no geometry "
                      "to carry them"
                      % (len(lines), "y" if len(lines) == 1 else "ies"))
            return
        for line in lines:
            words = line.split(None, 1)
            _add_custom_attribute(host.xplane, "MAGNET",
                                  words[1] if len(words) > 1 else "",
                                  unique=False)
        self.warn("%d MAGNET entr%s kept as custom attributes on %s; "
                  "XPlane2Blender has no field for them"
                  % (len(lines), "y" if len(lines) == 1 else "ies", host.name))

    @staticmethod
    def _detent_lift(state):
        """How far ATTR_axis_detented moves its bone, in model units.

        XPlane2Blender validates every ``ATTR_axis_detent_range`` height against
        the bone's own travel, so the importer has to know that travel before it
        can decide whether the table is writable at all.
        """
        for cat, raws in state or []:
            if cat != "detent_axis":
                continue
            for raw in raws:
                words = raw.split()
                if not words or words[0] != "ATTR_axis_detented" or len(words) < 4:
                    continue
                try:
                    dx, dy, dz = (float(words[1]), float(words[2]), float(words[3]))
                except ValueError:
                    continue
                return (dx * dx + dy * dy + dz * dz) ** 0.5
        return None

    def _apply_detent_ranges(self, ob, state):
        """ATTR_axis_detent_range <start> <end> <height> -> the manipulator's
        *Axis Detent Ranges* table (object > X-Plane > Manipulator).

        One line per region, in file order.  XPlane2Blender writes them back as
        the same three numbers, so the table is the whole conversion - except
        when a height is outside the detent bone's travel: XP2B validates that
        ("Height ... must be between 0.0 and the maximum lift height") and
        cancels the entire export.  Laminar's own L-5 Sentinel asks for height
        1.0 on a bone that lifts 0.015, so that case has to survive too: the
        lines go to the object's custom attributes, which are written verbatim,
        and the sim reads the same table back.
        """
        if not ob.xplane.manip.enabled:
            # the ranges only exist on a manipulator, and this one has none:
            # XPlane2Blender writes them per object, so there is nowhere to put
            # them until the ATTR_manip_* line next to them is supported
            for cat, raws in state or []:
                if cat == "detent_range" and raws:
                    self.warn("%s: ATTR_axis_detent_range dropped - its "
                              "manipulator is not converted" % ob.name)
            return
        ranges = []
        for cat, raws in state or []:
            if cat != "detent_range":
                continue
            for raw in raws:
                words = raw.split()
                if not words or words[0] != "ATTR_axis_detent_range":
                    continue
                try:
                    start, end, height = (float(word) for word in words[1:4])
                except ValueError:
                    self.warn("%s: ATTR_axis_detent_range needs three numbers (%s)"
                              % (ob.name, raw))
                    continue
                ranges.append((start, end, height))
        if not ranges:
            if ob.xplane.manip.type == "drag_axis_detent":
                # the file marked a detent but no range reached this object, and
                # XPlane2Blender refuses a drag_axis_detent that carries none
                # ("must have axis detent ranges") - drop to a plain axis and
                # keep the marker verbatim so the sim still reads it
                ob.xplane.manip.type = "drag_axis"
                for cat, raws in state or []:
                    if cat != "detent_axis":
                        continue
                    for raw in raws:
                        words = raw.split(None, 1)
                        if words and words[0] == "ATTR_axis_detented":
                            _add_custom_attribute(
                                ob.xplane, "ATTR_axis_detented",
                                words[1] if len(words) > 1 else "", unique=False)
                self.warn(
                    "%s: ATTR_axis_detented kept as a custom attribute - no "
                    "ATTR_axis_detent_range reached this object, and "
                    "XPlane2Blender cancels an export over a detent with no "
                    "ranges" % ob.name)
            return
        lift = self._detent_lift(state)
        # Same three-decimal quantisation XP2B validates with (see _q): a range
        # at 0.005 under a bone that lifts 0.0046 is accepted there and must not
        # be demoted here.
        if lift is not None and any(not (_q(0.0) <= _q(h) <= _q(lift))
                                    for _s, _e, h in ranges):
            # Demote the manipulator first: XPlane2Blender refuses a
            # drag_axis_detent that carries no table ("must have axis detent
            # ranges"), so the type has to go back to a plain axis.  The two
            # lines that make it a detent in the sim then ride the custom
            # attributes, in file order - XP2B writes them after the manipulator
            # and before the geometry, which is where the file had them.
            ob.xplane.manip.type = "drag_axis"
            for cat, raws in state or []:
                if cat != "detent_axis":
                    continue
                for raw in raws:
                    words = raw.split(None, 1)
                    if words and words[0] == "ATTR_axis_detented":
                        _add_custom_attribute(
                            ob.xplane, "ATTR_axis_detented",
                            words[1] if len(words) > 1 else "", unique=False)
            for start, end, height in ranges:
                _add_custom_attribute(ob.xplane, "ATTR_axis_detent_range",
                                      "%g %g %g" % (start, end, height),
                                      unique=False)
            self.warn(
                "%s: %d ATTR_axis_detent_range line(s) kept as custom attributes "
                "- a height of %.3f is outside the %.3f the detent bone lifts, "
                "which XPlane2Blender cancels an export over; the manipulator "
                "exports as a plain drag axis"
                % (ob.name, len(ranges),
                   max(h for _s, _e, h in ranges), lift))
            return
        for start, end, height in ranges:
            item = ob.xplane.manip.axis_detent_ranges.add()
            item.start = start
            item.end = end
            item.height = height

    def _apply_cockpit_state(self, ob, state, coll):
        """ATTR_cockpit* -> material Cockpit Feature + the layer's panel mode.

        Five spellings, one switch: draw this batch into the panel texture
        (``ATTR_cockpit``), only into its emissive half
        (``ATTR_cockpit_lit_only``), into one region of it
        (``ATTR_cockpit_region``), into a device texture
        (``ATTR_cockpit_device``), or normally again (``ATTR_no_cockpit``).

        XPlane2Blender splits that across two places: the *material* says
        whether it uses the panel (Cockpit Feature -> Panel Texture / Cockpit
        Device), the *layer* says how the panel is drawn (Panel Texture Mode) -
        one mode per file.  A file that mixes modes therefore cannot be
        reproduced exactly; the first mode wins and the mixture is reported.
        """
        for cat, raws in state or []:
            if cat != "cockpit":
                continue
            for raw in raws:
                words = raw.split()
                if not words:
                    continue
                kw = words[0]
                args = _state_args(words)
                if kw == "ATTR_no_cockpit":
                    self._material_set(ob, [("cockpit_feature", "none")],
                                       "cockpit")
                elif kw == "ATTR_cockpit_device":
                    self._apply_cockpit_device(ob, args, ob.name)
                elif kw in PANEL_MODE:
                    self._apply_panel_material(ob, kw, args, coll)

    def _apply_panel_material(self, ob, kw, args, coll):
        """One panel keyword -> the material fields, then the layer's mode."""
        nums = []
        for arg in args:
            try:
                nums.append(int(float(arg)))
            except ValueError:
                break           # a trailing comment or junk: stop here
        region = lum = None
        if kw == "ATTR_cockpit_region":
            region = nums[0] if nums else None
            lum = nums[1] if len(nums) > 1 else None
            if region is None:
                self.warn("%s: ATTR_cockpit_region without a region number is "
                          "the pre-11.10 form; XPlane2Blender needs the number, "
                          "left at region 1" % ob.name)
        else:
            lum = nums[0] if nums else None

        # validatePanel refuses a *panel material* that also overrides the
        # light level ("Must not override light level", which cancels the whole
        # export), so the material keeps the panel and no override.  A panel
        # batch's override lives on the object instead (see
        # _apply_light_level), which is legal in X-Plane and survives.
        pairs = [("cockpit_feature", "panel"), ("lightLevel", False)]
        if region is not None:
            # the file counts regions from 0, XPlane2Blender's enum from 1
            pairs.append(("cockpit_region",
                          str(min(MAX_COCKPIT_REGIONS, max(1, region + 1)))))
        if lum is not None:
            # ATTR_cockpit [luminance] (X-Plane 12): the panel's maximum
            # brightness in nits, its own switch in XP2B
            pairs += [("cockpit_feature_use_luminance", True),
                      ("cockpit_feature_luminance", lum)]
        self._material_set(ob, pairs, "panel")

        mode = PANEL_MODE[kw]
        layer = coll.xplane.layer
        if self.panel_mode is None:
            self.panel_mode = mode
            layer.cockpit_panel_mode = mode
        elif layer.cockpit_panel_mode != mode:
            self.warn("%s: the file mixes cockpit panel modes (%s and %s); "
                      "XPlane2Blender has one per file, kept %s"
                      % (ob.name, layer.cockpit_panel_mode, mode,
                         layer.cockpit_panel_mode))

    def _apply_cockpit_device(self, ob, args, name):
        """ATTR_cockpit_device <name> <bus mask> <rheostat> <auto adjust> [lum]."""
        if len(args) < 4:
            self.warn("%s: ATTR_cockpit_device has %d argument(s), needs device,"
                      " bus, rheostat and auto-adjust; material left plain"
                      % (name, len(args)))
            return
        dev, bus, rheo, auto = args[0], args[1], args[2], args[3]
        try:
            bus = int(float(bus))
            rheo = int(float(rheo))
            auto = int(float(auto))
        except ValueError:
            self.warn("%s: ATTR_cockpit_device arguments are not numbers (%s)"
                      % (name, " ".join(args[:4])))
            return

        # XPlane2Blender knows the built-in devices by name and everything else
        # as a plugin device, which is how the file spells both
        known = set()
        prop = ob.material_slots[0].material.xplane.bl_rna.properties.get(
            "device_name") if ob.material_slots and ob.material_slots[0].material \
            else None
        if prop is not None:
            known = {i.identifier for i in prop.enum_items}
        pairs = [("cockpit_feature", "device"),
                 ("device_name", dev if dev in known else PLUGIN_DEVICE),
                 ("plugin_device", "" if dev in known else dev)]
        # the file's bus is a bit mask over the six systems, XP2B has six flags
        pairs += [("device_bus_%d" % bit, bool(bus & (1 << bit)))
                  for bit in range(6)]
        if rheo < 0:
            self.warn("%s: rheostat channel %d means \"no channel\", which "
                      "XPlane2Blender cannot express; wrote 0" % (name, rheo))
            rheo = 0
        pairs += [("device_lighting_channel", rheo),
                  ("device_auto_adjust", bool(auto))]
        if len(args) > 4:
            try:
                pairs += [("cockpit_feature_use_luminance", True),
                          ("cockpit_feature_luminance", int(float(args[4])))]
            except ValueError:
                pass
        self._material_set(ob, pairs, "device")

    def _cockpit_regions(self, obj8, coll):
        """COCKPIT_REGION -> the layer's region table.

        The file writes ``COCKPIT_REGION <left> <bottom> <right> <top>`` in
        panel-texture pixels, one line per region, in region order.  XP2B keeps
        an origin plus a power-of-two width and height per region, so a region
        that is not a power of two on each axis cannot be expressed.
        """
        rows = []
        for line in obj8.globals:
            words = line.split()
            if not words or words[0] != "COCKPIT_REGION":
                continue
            if len(words) < 5:
                self.warn("COCKPIT_REGION %s: needs left bottom right top, "
                          "region left unnamed" % " ".join(words[1:]))
                rows.append(None)
                continue
            try:
                rows.append([int(float(v)) for v in words[1:5]])
            except ValueError:
                self.warn("COCKPIT_REGION %s: not four numbers, region left "
                          "unnamed" % " ".join(words[1:5]))
                rows.append(None)
        if not rows:
            return
        layer = coll.xplane.layer
        layer.cockpit_regions = str(min(MAX_COCKPIT_REGIONS, len(rows)))
        if len(rows) > MAX_COCKPIT_REGIONS:
            self.warn("%d COCKPIT_REGION entries; XPlane2Blender keeps at most "
                      "%d, the rest are dropped"
                      % (len(rows), MAX_COCKPIT_REGIONS))
        for index, row in enumerate(rows[:MAX_COCKPIT_REGIONS]):
            slot = layer.cockpit_region[index]
            if row is None:
                continue
            left, bottom, right, top = row
            slot.left = left
            slot.top = bottom       # XP2B calls the bottom edge "top"
            slot.width = self._power_of_two(right - left, index, "width")
            slot.height = self._power_of_two(top - bottom, index, "height")

    def _power_of_two(self, size, index, axis):
        """XP2B stores a region's extent as an exponent; say when it cannot."""
        if size <= 0:
            self.warn("COCKPIT_REGION %d: %s is %d, not a positive power of "
                      "two; wrote 1" % (index, axis, size))
            return 1
        exponent = int(round(math.log(size, 2)))
        if 2 ** exponent != size:
            self.warn("COCKPIT_REGION %d: %s is %d, not a power of two; "
                      "XPlane2Blender stores an exponent, wrote 2**%d"
                      % (index, axis, size, exponent))
        return min(11, max(1, exponent))

    def _material_set(self, ob, pairs, tag):
        """Set material fields, cloning the material when a value would change

        Two batches can share a material and disagree about a state, and a
        material carries one value - so the second one gets its own copy.  The
        clone is cached per (material, values), because otherwise every batch
        that carries the same ``ATTR_draw_disable`` would get its own identical
        copy: a 160-material cockpit for a file that has three distinct looks.
        """
        key = tuple(pairs)
        for slot in ob.material_slots:
            mat = slot.material
            if mat is None:
                continue
            if all(getattr(mat.xplane, name) == value for name, value in pairs):
                continue
            slot_key = (mat.as_pointer(),) + key
            clone = self.material_cache.get(slot_key)
            if clone is None:
                clone = mat.copy()
                clone.name = mat.name + "_" + tag
                for name, value in pairs:
                    setattr(clone.xplane, name, value)
                if any(name == "solid_camera" and value for name, value in pairs):
                    # ATTR_solid_camera marks a collision proxy: the sim uses it
                    # for camera hits and never draws it, so it should not sit
                    # in the way in the viewport either.  Setting the alpha to 0
                    # leaves the geometry selectable; the export writes
                    # ATTR_solid_camera back unchanged, because that lives on
                    # xplane.solid_camera and not in the material graph.
                    _make_transparent(clone)
                self.material_cache[slot_key] = clone
            slot.material = clone

    def _check_state(self, state, name):
        # only what is really mapped - anything else has to be reported, or the
        # file comes back looking fine with a state line silently missing
        keep = {"manip", "draw", "light_level", "camera", "shiny", "shade",
                "blend", "shadow", "poly_os", "cockpit", "detent_range",
                "detent_axis", "manip_wheel", "manip_keyframe", "cull",
                "hard", "draped", "hud", "depth"}
        for cat, raws in state or []:
            if cat in keep:
                continue
            for raw in raws:
                kw = raw.split()[0] if raw.split() else raw
                if kw in ("ATTR_LOD",):
                    self.warn("%s: %s is not converted (XPlane2Blender writes "
                              "LOD from the collection layer)" % (name, kw))
                elif kw.startswith("ATTR_manip_"):
                    pass
                else:
                    self.warn("%s: %s is not converted" % (name, kw))


#: what the header Empty carries and the collection now keeps, so the file's
#: own text survives the object
SOURCE_PROPS = (XM.PROP_DUMP, XM.PROP_SOURCE, XM.PROP_LEAF_OBJS,
                XM.PROP_LEAF_STATES, XM.PROP_GLOBALS, XM.PROP_EVENTS,
                XM.PROP_FINGERPRINTS, XM.PROP_CMD_HASH)


def _keep_source(coll, header):
    """Move the file's data from the header Empty onto the collection, then the
    caller deletes the Empty.

    The Empty existed to hold the OBJ8 text and the batch tables.  All of that
    is either replayed into the scene already or only needed for reference, and
    an Empty sitting at the world origin that nothing parents to reads as
    leftover junk in the outliner.
    """
    for name in SOURCE_PROPS:
        value = header.get(name)
        if value is not None:
            coll[name] = value


def convert(context, headers, report=None):
    """Convert imported header Empties into XP2B collections. Returns them."""
    if not available():
        raise RuntimeError(
            "XPlane2Blender is not installed/enabled - it provides the xplane "
            "property group this conversion writes into")
    if context.object is not None and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    out = []
    for header in headers:
        warnings = []
        coll = _Converter(context, header, report, warnings).run()
        if report is not None:
            for w in warnings:
                report({'WARNING'}, w)
        # the header remembers where it went, so the panel can say "already
        # converted" instead of offering the step a second time
        header["xp2b_collection"] = coll.name
        if warnings:
            coll["xp2b_convert_warnings"] = json.dumps(warnings)
            header["xp2b_convert_warnings"] = len(warnings)
        _keep_source(coll, header)
        # The header is the active object at this point, and deleting the
        # active object leaves `context.active_object` as None.  XP2B's sidebar
        # draw does not guard that (`context.active_object.xplane` on None), so
        # a scene that imports perfectly still redraws into an AttributeError
        # traceback in the 3D view.  Hand the selection to something that
        # survives the deletion: the root Empty, else the first mesh.
        # The header is a member of this very collection (conversion owns the
        # one the import built), so it must not be picked as its own
        # replacement -- that would assign a dead StructRNA after the removal.
        # RNA wrappers are never identical objects, so compare by name.
        survivors = [ob for ob in coll.objects if ob.name != header.name]
        replacement = (next((ob for ob in survivors if ob.type == 'EMPTY'), None)
                       or next((ob for ob in survivors if ob.type == 'MESH'), None))
        bpy.data.objects.remove(header, do_unlink=True)
        if replacement is not None:
            for ob in list(context.selected_objects):
                if ob is not replacement:
                    ob.select_set(False)
            context.view_layer.objects.active = replacement
            replacement.select_set(True)
        out.append(coll)
    return out
