"""Turn an imported OBJ8 header into an XPlane2Blender *authoring* scene.

The importer in this addon keeps an OBJ8 file's command stream verbatim, so an
untouched file round-trips byte for byte.  That model is deliberately NOT a
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
* manipulators (the common signatures: command family, toggle, push, radio,
  delta/wrap, drag axis) as ``object.xplane.manip``
* the geometry, with its materials and UVs, as the importer already built it

What does NOT: ``LIGHT_*`` primitives, ``ATTR_LOD``, ``IF/ENDIF``, ``MAGNET``,
``ATTR_hard``, unknown ATTR categories, and drag-rotate/detent manipulators.
XPlane2Blender cannot be handed those as commands, so they are dropped and
counted in a warning per file - the conversion is one-way and lossy by design,
which is exactly why the source scene is left untouched.

One structural note: a block that carries both a translation table and a
rotation table becomes two nested Empty levels, because each dataref gets its
own object.  ``ANIM_begin`` therefore appears twice where the file had one;
the composition order is unchanged.
"""

import json
import math
import os

import bpy
from mathutils import Matrix, Vector

from . import xpanim as XA
from . import objcodec as OC
from . import xpmesh as XM
from .xpanim import CMD_ROTATE, CMD_ROTATE_KEYS, CMD_SHOW, CMD_HIDE, \
    CMD_TRANS, CMD_TRANS_KEYS

# the XPlane2Blender property group on objects / collections / materials
XP2B_PROP = "xplane"

_UNSUPPORTED_MANIP = {
    "ATTR_manip_drag_rotate", "ATTR_manip_drag_rotate_detent",
    "ATTR_manip_drag_axis_pix", "ATTR_manip_axis_knob",
    "ATTR_manip_axis_switch_up_down", "ATTR_manip_axis_switch_left_right",
}


def available():
    """True when XPlane2Blender is installed and enabled."""
    try:
        bpy.types.Object.bl_rna.properties[XP2B_PROP]
        bpy.types.Collection.bl_rna.properties[XP2B_PROP]
    except KeyError:
        return False
    return True


# ---------------------------------------------------------------------------
# small builders
# ---------------------------------------------------------------------------

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


def _add_dataref(ob, path, anim_type="transform"):
    dr = ob.xplane.datarefs.add()
    dr.path = path
    dr.anim_type = anim_type
    return dr


def _set_axis_angle(ob, axis_bl, degrees):
    ob.rotation_mode = 'AXIS_ANGLE'
    ob.rotation_axis_angle = (math.radians(degrees), axis_bl[0], axis_bl[1],
                              axis_bl[2])


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
    for cat, raws in state or []:
        if cat == "manip":
            for raw in raws:
                words = raw.split()
                if words and words[0].startswith("ATTR_manip_"):
                    return words
    return None


def _tail(words, start):
    return " ".join(words[start:])


def _cursor(value):
    value = (value or "hand").lower()
    return {
        "four_arrows": "four_arrows", "up_down": "up_down",
        "left_right": "left_right", "arrow": "arrow",
        "toggle": "toggle", "button": "button", "rotate": "rotate",
        "hand": "hand",
    }.get(value, "hand")


def apply_manipulator(ob, state, warnings):
    """Map one ATTR_manip_* line onto object.xplane.manip. False if unhandled."""
    words = _manip_line(state)
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
            manip.cursor = _cursor(args[0])
            manip.dx, manip.dy, manip.dz = (float(a) for a in args[1:4])
            manip.v1, manip.v2 = float(args[4]), float(args[5])
            manip.dataref1 = args[6]
            manip.tooltip = _tail(args, 7)
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

    # -- helpers ----------------------------------------------------------
    def warn(self, msg):
        self.warnings.append(msg)

    def key_dataref(self, dr, frame, value):
        """One dataref key on frame `frame` - the frame XP2B samples the pose on."""
        self.scene.frame_set(frame)
        dr.value = value
        dr.keyframe_insert(data_path="value", group="XPlane Datarefs")
        self.frames_used = max(self.frames_used, frame)

    def empties_for_block(self, block, parent, coll, base):
        """Build the Empty chain reproducing one ANIM block. Returns its anchor."""
        anc = []          # empties created for this block, outermost first

        def make(suffix):
            ob = _empty(coll, "%s_b%03d%s" % (base, block.index, suffix),
                        anc[-1] if anc else parent)
            anc.append(ob)
            return ob

        # A two-endpoint ANIM_trans / ANIM_rotate carrying a dataref *is* a
        # two-key table: same motion, one line per key instead of two
        # endpoints - and a real animation, not a pose.  Kept as a pair it would
        # come out of the conversion as a static Empty and lose its dataref
        # (verify/fixtures/nested.obj's door: 45 degrees of travel gone).
        commands = []
        for cmd in block.commands:
            kind = cmd["kind"]
            if kind in (CMD_TRANS, CMD_ROTATE) and cmd.get("dataref"):
                if kind == CMD_TRANS:
                    keys = [(cmd["v1"], cmd["p1"]), (cmd["v2"], cmd["p2"])]
                    kind = CMD_TRANS_KEYS
                else:
                    keys = [(cmd["v1"], cmd.get("r1", 0.0)),
                            (cmd["v2"], cmd.get("r2", 0.0))]
                    kind = CMD_ROTATE_KEYS
                commands.append(dict(cmd, kind=kind, keys=sorted(keys)))
            else:
                commands.append(cmd)

        # 1. static transforms, in file order, one Empty each
        static_rot = 0
        for cmd in commands:
            if cmd["kind"] == CMD_TRANS:
                p1, p2 = cmd.get("p1"), cmd.get("p2")
                if p1 != p2:
                    self.warn(
                        "block %d: ANIM_trans with two different points is a "
                        "dataref-less animation; imported as a static offset at "
                        "the first point" % block.index)
                ob = make("_place")
                ob.location = _xp_to_bl(p1 or (0.0, 0.0, 0.0))
            elif cmd["kind"] == CMD_ROTATE:
                r1, r2 = cmd.get("r1", 0.0), cmd.get("r2", 0.0)
                if abs(r1 - r2) > 1e-6:
                    self.warn(
                        "block %d: ANIM_rotate %g..%g has no dataref; kept the "
                        "first angle" % (block.index, r1, r2))
                static_rot += 1
                ob = make("_rot")
                _set_axis_angle(ob, _xp_to_bl(cmd.get("axis",
                                                      (0.0, 1.0, 0.0))), r1)

        # 2. keyed translation tables
        for cmd in [c for c in commands if c["kind"] == CMD_TRANS_KEYS]:
            dr_path = cmd.get("dataref")
            keys = cmd.get("keys") or []
            if not dr_path:
                self.warn("block %d: ANIM_trans_begin without a dataref"
                          % block.index)
                continue
            if len(keys) < 2:
                self.warn("block %d: translation table has %d key(s); "
                          "XPlane2Blender needs 2" % (block.index, len(keys)))
                continue
            ob = make("_trans")
            dr = _add_dataref(ob, dr_path, "transform")
            for n, (value, xyz) in enumerate(keys, start=1):
                # the frame first: the transform key has to land on the same
                # frame as the dataref key, XP2B samples the pose there
                self.scene.frame_set(n)
                ob.location = _xp_to_bl(xyz)
                ob.keyframe_insert(data_path="location")
                self.key_dataref(dr, n, value)

        # 3. keyed rotation tables
        for cmd in [c for c in commands if c["kind"] == CMD_ROTATE_KEYS]:
            dr_path = cmd.get("dataref")
            keys = cmd.get("keys") or []
            if not dr_path:
                self.warn("block %d: ANIM_rotate_begin without a dataref"
                          % block.index)
                continue
            if len(keys) < 2:
                self.warn("block %d: rotation table has %d key(s); "
                          "XPlane2Blender needs 2" % (block.index, len(keys)))
                continue
            ob = make("_rotate")
            axis_bl = _xp_to_bl(cmd.get("axis", (0.0, 1.0, 0.0)))
            dr = _add_dataref(ob, dr_path, "transform")
            for n, (value, degrees) in enumerate(keys, start=1):
                self.scene.frame_set(n)
                _set_axis_angle(ob, axis_bl, degrees)
                ob.keyframe_insert(data_path="rotation_axis_angle")
                self.key_dataref(dr, n, value)

        return _Anchor(anc[-1]) if anc else parent

    # -- nested animation: one bone per transform command -------------------
    TRANSFORM_KINDS = (CMD_ROTATE, CMD_ROTATE_KEYS, CMD_TRANS, CMD_TRANS_KEYS)

    def prepare_tree(self, anim):
        """Which blocks carry a bone, and which batch hangs where.

        One armature for the whole file, its bone tree copied from the block
        tree (``XM.bone_tree``, the same one the importer uses, so the two
        agree): a block carries bones when it is on the chain of a batch that
        needs more than one transform command.  Every batch hangs off the bone of
        its innermost bone-carrying block, so parts that share a block share a
        bone - which is what the file does and what per-batch rigs got wrong.
        """
        self.children = {}
        for block in anim.blocks:
            key = block.parent.index if block.parent is not None else None
            self.children.setdefault(key, []).append(block)
        self.specs, self.tips = XM.bone_tree(anim, self.pose)

    def build_armature(self, coll, base):
        """The file's one armature: edit bones from the block tree, then keyed.

        Bones carry XPlane2Blender's own ``datarefs`` (on the armature *data*,
        which is where XP2B reads them) and the pose curves (on the object, which
        is where XP2B samples the pose).  Both are keyed frame 1..N, one key per
        file key, the mapping XP2B documents.
        """
        name = base + "_bones"
        data = bpy.data.armatures.new(name)
        arm = bpy.data.objects.new(name, data)
        coll.objects.link(arm)
        arm.show_in_front = True
        data.display_type = 'OCTAHEDRAL'

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

        for bone_name, head, cmd, parent_bone, block in self.specs:
            bone = data.bones[bone_name]
            parent_rest = (data.bones[parent_bone].matrix_local
                           if parent_bone else Matrix.Identity(4))
            base_matrix = parent_rest.inverted() @ bone.matrix_local
            dataref = cmd.get("dataref")
            if dataref:
                values = XM.command_values(cmd)
                if len(values) < 2:
                    self.warn("block %d: %s on bone %s has %d key(s); "
                              "XPlane2Blender needs 2"
                              % (block, cmd["kind"], bone_name, len(values)))
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

    def visibility_to_batches(self, anim, leaf_objs):
        """ANIM_show/hide goes on the batches themselves, not on a host object.

        XP2B writes those datarefs for an object, and the only object that can
        sit above a bone chain is the mesh: a bone's children are bones, so an
        Empty host would break the chain.  Every batch inside the block therefore
        carries the command, written outermost block first and innermost last -
        the order X-Plane reads them in, where the innermost command wins (that
        is how `ANIM_hide 0 0` wrapping a per-part `ANIM_show 2 2` works).
        """
        placed = 0
        for block in anim.blocks:
            commands = [cmd for cmd in block.commands
                        if cmd["kind"] in (CMD_SHOW, CMD_HIDE)
                        and cmd.get("dataref")]
            if not commands:
                continue
            for leaf in block.leaves:
                name = leaf_objs[leaf] if leaf < len(leaf_objs) else None
                ob = bpy.data.objects.get(name) if name else None
                if ob is None:
                    continue
                for cmd in commands:
                    dr = _add_dataref(ob, cmd["dataref"],
                                      "show" if cmd["kind"] == CMD_SHOW
                                      else "hide")
                    dr.show_hide_v1 = cmd.get("v1", 0.0)
                    dr.show_hide_v2 = cmd.get("v2", 0.0)
                    placed += 1
        return placed

    # -- main -------------------------------------------------------------
    def run(self):
        header = self.header
        text = header.get(XM.PROP_DUMP, "")
        if not text:
            raise RuntimeError("%s has no stored OBJ8 text" % header.name)
        obj8 = OC.parse_text(text)
        anim = XA.build(obj8)

        src = header.get(XM.PROP_SOURCE, "")
        base = os.path.splitext(os.path.basename(src))[0] or header.name
        coll = _new_collection(base + "_xp2b")
        self.scene.collection.children.link(coll)
        coll.xplane.is_exportable_collection = True
        coll.xplane.layer.name = base
        coll.xplane.layer.export_type = "aircraft"

        leaf_objs = json.loads(header.get(XM.PROP_LEAF_OBJS, "[]"))
        leaf_states = json.loads(header.get(XM.PROP_LEAF_STATES, "[]"))

        # visibility datarefs are not transforms: keep them out of the frame map
        self.anim = anim
        # the rest pose a bone chain is laid out at: every dataref at its own
        # lowest keyed value, which is where the file's first key sits
        self.pose = {dref: lo for dref, (lo, hi) in anim.ranges().items()}
        self.prepare_tree(anim)
        self.armature = self.build_armature(coll, base) if self.specs else None
        if self.armature is not None:
            for leaf, chain in anim.leaves.items():
                if len(XM.chain_transforms(chain)) <= 1:
                    continue        # a hinge: an Empty still carries it
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
            anchors[block.index] = self.empties_for_block(block, parent,
                                                          coll, base)

        placed = self.visibility_to_batches(anim, leaf_objs)
        if placed:
            self.warn("%d ANIM_show/hide rule(s) were put on the batches "
                      "themselves: XPlane2Blender writes them per object, and a "
                      "bone chain can only carry them there" % placed)

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
                ob.parent = None
                ob.matrix_parent_inverse.identity()
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
            self._apply_camera_state(ob, state)
            self._apply_shade_and_shiny(ob, state)
            self._apply_surface_state(ob, state)
            if apply_manipulator(ob, state, self.warnings):
                coll.xplane.layer.export_type = "cockpit"
            self._check_state(state, ob.name)
            made += 1

        if not made:
            self.warn("%s: no geometry was moved" % base)

        nested = [leaf for leaf in anim.leaves
                  if len(XM.chain_transforms(anim.leaves[leaf])) > 1]
        armatures = set()
        for anchor in self.leaf_anchor.values():
            armatures.add(anchor.arm.name)
        if self.leaf_anchor:
            self.warn("%d nested animation chain(s) became %d armature(s), one "
                      "bone per transform command"
                      % (len(self.leaf_anchor), len(armatures)))
        blocked = [leaf for leaf in nested if leaf not in self.leaf_anchor]
        if blocked:
            self.warn("%d nested batch(es) kept their Empty chain instead of "
                      "bones: their subtree carries ANIM_show/hide, which "
                      "XP2B writes on an object and never on a bone"
                      % len(blocked))
        return coll

    def _report_dropped_commands(self, obj8):
        """Lights, LODs and IF blocks are commands XPlane2Blender cannot take
        from us - say how many, per file. Silence here would look like success."""
        counts = {}
        for kind, payload in obj8.events:
            if kind != OC.EV_RAW or not payload:
                continue
            kw = payload.split()[0]
            if kw.startswith("LIGHT"):
                key = "LIGHT_* primitives"
            elif kw == "ATTR_LOD":
                key = "ATTR_LOD levels"
            elif kw in ("IF", "ENDIF"):
                key = "IF/ENDIF blocks"
            elif kw.startswith("MAGNET"):
                key = "MAGNET entries"
            else:
                continue
            counts[key] = counts.get(key, 0) + 1
        for key, n in sorted(counts.items()):
            self.warn("%d %s were dropped: XPlane2Blender cannot be handed them, "
                      "re-add them there by hand" % (n, key))

    # -- per-part helpers -------------------------------------------------
    def _no_draw_material(self, ob, base):
        """ATTR_draw_disable -> a material copy with XP2B draw = False."""
        for slot in ob.material_slots:
            mat = slot.material
            if mat is None:
                continue
            if getattr(mat.xplane, "draw", True):
                clone = mat.copy()
                clone.name = mat.name + "_nodraw"
                clone.xplane.draw = False
                slot.material = clone

    def _apply_light_level(self, ob, state):
        """ATTR_light_level -> material.xplane.lightLevel_* (an XP2B material attr)."""
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
                for slot in ob.material_slots:
                    mat = slot.material
                    if mat is None:
                        continue
                    if getattr(mat.xplane, "lightLevel_dataref", "") not in ("",
                                                                            dref):
                        clone = mat.copy()
                        clone.name = mat.name + "_light"
                        slot.material = clone
                        mat = clone
                    mat.xplane.lightLevel = True
                    mat.xplane.lightLevel_v1 = v1
                    mat.xplane.lightLevel_v2 = v2
                    mat.xplane.lightLevel_dataref = dref

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
                for slot in ob.material_slots:
                    mat = slot.material
                    if mat is None or mat.xplane.solid_camera == value:
                        continue
                    clone = mat.copy()
                    clone.name = mat.name + "_cam"
                    slot.material = clone
                    clone.xplane.solid_camera = value

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
        """blend / shadow / poly_os -> the matching material properties.

        Unambiguous one-to-one mappings only.  ATTR_hard, ATTR_cull,
        ATTR_depth, ATTR_draped and ATTR_hud_glass are deliberately left as
        warnings: each either needs a table we would be guessing at (ATTR_hard
        takes a surface number) or has no XPlane2Blender field at all, and a
        wrong guess changes how the object renders.  ATTR_draped is also
        rejected outright by XP2B outside scenery objects ("Must not be
        draped"), which is where that experiment ended.
        """
        blend = {"ATTR_blend": "on", "ATTR_no_blend": "off",
                 "ATTR_shadow_blend": "shadow"}
        flags = {"ATTR_shadow": ("shadow_local", True),
                 "ATTR_no_shadow": ("shadow_local", False)}
        for cat, raws in state or []:
            for raw in raws:
                words = raw.split()
                if not words:
                    continue
                kw = words[0]
                if kw in blend:
                    self._material_set(ob, [("blend_v1000", blend[kw])], kw[:12])
                elif kw in flags:
                    field, value = flags[kw]
                    self._material_set(ob, [(field, value)], kw[:12])
                elif kw == "ATTR_poly_os" and len(words) > 1:
                    try:
                        value = int(float(words[1]))
                    except ValueError:
                        self.warn("%s: ATTR_poly_os value not a number" % ob.name)
                        continue
                    self._material_set(ob, [("poly_os", value)], "poly_os")
                elif kw == "ATTR_no_poly_os":
                    self._material_set(ob, [("poly_os", 0)], "poly_os")

    def _material_set(self, ob, pairs, tag):
        """Set material fields, cloning the material when a value would change

        Two batches can share a material and disagree about a state, and a
        material carries one value - so the second one gets its own copy.
        """
        for slot in ob.material_slots:
            mat = slot.material
            if mat is None:
                continue
            if any(getattr(mat.xplane, name) == value for name, value in pairs):
                continue
            clone = mat.copy()
            clone.name = mat.name + "_" + tag
            slot.material = clone
            for name, value in pairs:
                setattr(clone.xplane, name, value)

    def _check_state(self, state, name):
        # only what is really mapped - anything else has to be reported, or the
        # file comes back looking fine with a state line silently missing
        keep = {"manip", "draw", "light_level", "camera", "shiny", "shade",
                "blend", "shadow", "poly_os"}
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
        if warnings:
            coll["xp2b_convert_warnings"] = json.dumps(warnings)
            header["xp2b_convert_warnings"] = len(warnings)
        out.append(coll)
    return out
