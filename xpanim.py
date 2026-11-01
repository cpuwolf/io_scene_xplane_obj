"""X-Plane OBJ8 animation: parse the ANIM_* stack and evaluate it at a pose.

Pure Python, no bpy: `verify/test_anim.py` runs this outside Blender.

Model
-----
`ANIM_begin` / `ANIM_end` nest, and every geometry command inside a block is
transformed by the whole chain of enclosing blocks.  Each block owns the
rotate/trans/show/hide commands that follow its own `ANIM_begin` (the spec
requires them to come first, but the parser does not insist).

Composition order
-----------------
Matrices multiply in file order and vertices are premultiplied, so

    ANIM_begin
    ANIM_trans  p  p  0 0 no_ref      (static: move to the pivot)
    ANIM_rotate 0 -1 0 -90 90 -1 1 dref
    ANIM_trans -p -p  0 0 no_ref      (static: back)
    TRIS 0 3
    ANIM_end

evaluates to T(+p) . R . T(-p): the *last* command applies first, which is
exactly the "rotate about an arbitrary pivot" recipe from Laminar's own
"Fun with Animation" article.  Nested blocks follow the same rule, so a leaf
ends up with M = M_outer . M_inner (outer multiplied on the left).

Datarefs
--------
The driver of an animation is a dataref -- a value the sim owns at run time
(gear deploy ratio, prop angle).  Nothing in Blender can read it, so a pose is
just a dict {dataref: value}; anything missing is 0.0.  Two-endpoint commands
interpolate linearly between (v1, v2) and clamp outside; key tables interpolate
between keys and clamp outside.  A command with no dataref (or the literal
`no_ref`) is static.
"""

import math

try:                          # inside the Blender extension package
    from . import objcodec as OC
except ImportError:           # standalone: verify/ puts the repo on sys.path
    import objcodec as OC

#: rotation that carries X-Plane axes (x east, y up, z south) onto Blender's
#: (x right, y away, z up): (x, y, z) -> (x, -z, y)
XP_TO_BL = ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))

IDENT = ((1.0, 0.0, 0.0, 0.0),
         (0.0, 1.0, 0.0, 0.0),
         (0.0, 0.0, 1.0, 0.0),
         (0.0, 0.0, 0.0, 1.0))

CMD_ROTATE = "rotate"            # ANIM_rotate  (two endpoints)
CMD_ROTATE_KEYS = "rotate_keys"  # ANIM_rotate_begin + ANIM_rotate_key...
CMD_TRANS = "trans"              # ANIM_trans   (two endpoints)
CMD_TRANS_KEYS = "trans_keys"    # ANIM_trans_begin + ANIM_trans_key...
CMD_SHOW = "show"
CMD_HIDE = "hide"

#: keyword sets, so callers can strip animation from a command stream
ANIM_KW = {
    "ANIM_rotate", "ANIM_rotate_begin", "ANIM_rotate_key", "ANIM_rotate_end",
    "ANIM_trans", "ANIM_trans_begin", "ANIM_trans_key", "ANIM_trans_end",
    "ANIM_show", "ANIM_hide", "ANIM_keyframe_loop",
}


# ---------------------------------------------------------------------------
# 4x4 matrices as nested tuples, row major, vertices premultiplied
# ---------------------------------------------------------------------------

def mat_mul(a, b):
    return tuple(tuple(a[i][0] * b[0][j] + a[i][1] * b[1][j] +
                       a[i][2] * b[2][j] + a[i][3] * b[3][j]
                       for j in range(4)) for i in range(4))


def mat_mul_all(mats):
    m = IDENT
    for other in mats:
        m = mat_mul(m, other)
    return m


def mat_trans(x, y, z):
    return ((1.0, 0.0, 0.0, float(x)),
            (0.0, 1.0, 0.0, float(y)),
            (0.0, 0.0, 1.0, float(z)),
            (0.0, 0.0, 0.0, 1.0))


def mat_rot(axis, degrees):
    """Right-handed rotation of `degrees` about `axis`."""
    x, y, z = axis
    n = math.sqrt(x * x + y * y + z * z)
    if n == 0.0 or degrees == 0.0:
        return IDENT
    x, y, z = x / n, y / n, z / n
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    t = 1.0 - c
    return ((t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0.0),
            (t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0.0),
            (t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0.0),
            (0.0, 0.0, 0.0, 1.0))


def _mul3(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3))
                       for j in range(len(b[0]))) for i in range(3))


def _rot_point(r, v):
    return tuple(sum(r[i][k] * v[k] for k in range(3)) for i in range(3))


def _transpose3(r):
    return tuple(tuple(r[j][i] for j in range(3)) for i in range(3))


def to_bl(m):
    """The same transform expressed in Blender axes."""
    r = XP_TO_BL
    rt = _transpose3(r)
    out = _mul3(_mul3(r, tuple(row[:3] for row in m)), rt)
    t = _rot_point(r, (m[0][3], m[1][3], m[2][3]))
    return (out[0] + (t[0],),
            out[1] + (t[1],),
            out[2] + (t[2],),
            (0.0, 0.0, 0.0, 1.0))


def to_xp(m):
    """Blender axes back to X-Plane axes."""
    r = XP_TO_BL
    rt = _transpose3(r)
    out = _mul3(_mul3(rt, tuple(row[:3] for row in m)), r)
    t = _rot_point(rt, (m[0][3], m[1][3], m[2][3]))
    return (out[0] + (t[0],),
            out[1] + (t[1],),
            out[2] + (t[2],),
            (0.0, 0.0, 0.0, 1.0))


def same(a, b, tol=1e-6):
    return all(abs(a[i][j] - b[i][j]) <= tol for i in range(4) for j in range(4))


# ---------------------------------------------------------------------------
# the parsed animation tree
# ---------------------------------------------------------------------------

class Block(object):
    """One ANIM_begin .. ANIM_end pair."""

    __slots__ = ("index", "parent", "commands", "leaves", "begin_ev", "end_ev",
                 "depth")

    def __init__(self, index, parent, begin_ev):
        self.index = index
        self.parent = parent              # Block or None
        self.commands = []                # dicts, in file order
        self.leaves = []                  # geometry commands inside, at any depth
        self.begin_ev = begin_ev          # event index of ANIM_begin
        self.end_ev = None
        self.depth = 0 if parent is None else parent.depth + 1

    def datarefs(self):
        out = []
        for cmd in self.commands:
            d = cmd.get("dataref")
            if d and d not in out:
                out.append(d)
        return out

    def __repr__(self):
        return "<Block %d depth %d %d cmds %d leaves>" % (
            self.index, self.depth, len(self.commands), len(self.leaves))


class Animation(object):
    """The ANIM_* structure of one Obj8 file, and its evaluation."""

    def __init__(self):
        self.blocks = []
        self.leaves = {}        # leaf index -> [Block, ...] outermost first
        self.warnings = []

    # -- parsing ---------------------------------------------------------
    def parse(self, obj8):
        stack = []
        keys_open = None        # (kind, dict) while inside *_begin / *_end
        for ev_index, (kind, payload) in enumerate(obj8.events):
            if kind == OC.EV_ANIM_BEGIN:
                blk = Block(len(self.blocks), stack[-1] if stack else None,
                            ev_index)
                self.blocks.append(blk)
                stack.append(blk)
                continue
            if kind == OC.EV_ANIM_END:
                if not stack:
                    self.warnings.append("ANIM_end without ANIM_begin")
                    continue
                blk = stack.pop()
                blk.end_ev = ev_index
                if keys_open is not None:
                    self.warnings.append(
                        "block %d: %s without its _end" % (blk.index,
                                                           keys_open[0]))
                    blk.commands.append(keys_open[1])
                    keys_open = None
                if blk.leaves and blk.parent is not None:
                    blk.parent.leaves.extend(
                        lf for lf in blk.leaves if lf not in blk.parent.leaves)
                continue
            if kind == OC.EV_LEAF:
                if stack is not None and stack:
                    stack[-1].leaves.append(payload)
                self.leaves[payload] = list(stack)
                continue
            if kind != OC.EV_RAW or not payload:
                continue
            words = payload.split()
            if not words:
                continue
            kw = words[0]
            if kw.startswith("ANIM_") and kw not in ANIM_KW:
                continue                      # ANIM_begin/end handled as events
            if not stack and kw in ANIM_KW:
                self.warnings.append("%s outside any ANIM_begin: %s"
                                     % (kw, payload[:60]))
                continue
            if kw in ("ANIM_rotate_key", "ANIM_trans_key"):
                if keys_open is None:
                    self.warnings.append("%s with no open table" % kw)
                    continue
                cmd = keys_open[1]
                cmd.setdefault("key_ev", []).append(ev_index)
                cmd["keys"].append(_parse_key(kw, words))
                continue
            if kw in ("ANIM_rotate_end", "ANIM_trans_end"):
                if keys_open is None:
                    self.warnings.append("%s with no open table" % kw)
                    continue
                keys_open[1]["end_ev"] = ev_index
                stack[-1].commands.append(keys_open[1])
                keys_open = None
                continue
            if keys_open is not None:
                # a transform command in the middle of a key table: close it
                stack[-1].commands.append(keys_open[1])
                keys_open = None
            cmd = _parse_cmd(kw, words)
            if cmd is not None:
                cmd["ev"] = ev_index
            if cmd is None:
                if kw in ANIM_KW:
                    self.warnings.append("unparsed animation command: %s"
                                         % payload[:60])
                continue
            if cmd["kind"] in (CMD_ROTATE_KEYS, CMD_TRANS_KEYS):
                if not cmd.get("dataref"):
                    self.warnings.append("%s without a dataref" % kw)
                keys_open = (kw, cmd)
                continue
            stack[-1].commands.append(cmd)
        while stack:
            blk = stack.pop()
            self.warnings.append("block %d: ANIM_begin without ANIM_end"
                                 % blk.index)
        return self

    # -- queries ---------------------------------------------------------
    def datarefs(self):
        out = []
        for blk in self.blocks:
            for d in blk.datarefs():
                if d not in out:
                    out.append(d)
        return out

    def ranges(self):
        """{dataref: (vmin, vmax)} of the **transform** keys -- the time line.

        Visibility ranges are deliberately left out: an `ANIM_show 0.1 999` next
        to a `0..1` rotate would stretch the dataref's span to 999 and squash
        every transform key into the first thousandth of the time line.
        """
        return self._ranges(transforms=True)

    def visibility_ranges(self):
        """{dataref: (vmin, vmax)} of datarefs driven only by show/hide."""
        out = self._ranges(transforms=False)
        for dref in self.ranges():
            out.pop(dref, None)
        return out

    def pose_ranges(self):
        """What a pose sweeps: transforms, plus visibility-only datarefs."""
        out = self.visibility_ranges()
        out.update(self.ranges())
        return out

    def _ranges(self, transforms=True):
        out = {}
        for blk in self.blocks:
            for cmd in blk.commands:
                d = cmd.get("dataref")
                if not d:
                    continue
                if cmd["kind"] in (CMD_ROTATE, CMD_TRANS):
                    if not transforms:
                        continue
                    lo, hi = sorted((cmd["v1"], cmd["v2"]))
                elif cmd["kind"] in (CMD_ROTATE_KEYS, CMD_TRANS_KEYS):
                    if not transforms:
                        continue
                    if not cmd["keys"]:
                        continue
                    lo = cmd["keys"][0][0]
                    hi = cmd["keys"][-1][0]
                elif cmd["kind"] in (CMD_SHOW, CMD_HIDE):
                    if transforms:
                        continue
                    lo, hi = sorted((cmd["v1"], cmd["v2"]))
                else:
                    continue
                if d in out:
                    out[d] = (min(out[d][0], lo), max(out[d][1], hi))
                else:
                    out[d] = (lo, hi)
        return out

    def matrix_at(self, pose=None, leaf=None):
        """Accumulated transform (X-Plane axes) at a pose, for one leaf or all.

        `pose` is {dataref: value}; missing datarefs are 0.0.
        """
        pose = pose or {}
        per_block = {}
        for blk in self.blocks:
            if blk.parent is None:
                m = IDENT
            else:
                m = per_block[blk.parent.index]
            for cmd in blk.commands:
                m = mat_mul(m, cmd_matrix(cmd, pose))
            per_block[blk.index] = m
        if leaf is not None:
            chain = self.leaves.get(leaf) or []
            return per_block[chain[-1].index] if chain else IDENT
        out = {}
        for li, chain in self.leaves.items():
            out[li] = per_block[chain[-1].index] if chain else IDENT
        return out

    def visible_at(self, pose, leaf):
        """Is the batch drawn at this pose?

        ANIM_show/ANIM_hide *override* each other rather than AND together: the
        common ice/effect idiom is an outer ``ANIM_hide 0 0`` (hide everything
        here) with a nested ``ANIM_show 2 2 imc/icing/...`` per part.  Taking
        the innermost-command-wins rule is what makes those parts appear.
        """
        pose = pose or {}
        visible = True
        for blk in self.leaves.get(leaf) or []:
            for cmd in blk.commands:
                if cmd["kind"] not in (CMD_SHOW, CMD_HIDE):
                    continue
                v = pose.get(cmd.get("dataref") or "", 0.0)
                inside = min(cmd["v1"], cmd["v2"]) <= v <= max(cmd["v1"],
                                                              cmd["v2"])
                visible = inside if cmd["kind"] == CMD_SHOW else not inside
        return visible

    def key_values(self, leaf=None):
        """Every dataref value that appears in the chain, low to high."""
        vals = set()
        blocks = self.blocks if leaf is None else (self.leaves.get(leaf) or [])
        for blk in blocks:
            for cmd in blk.commands:
                if cmd["kind"] in (CMD_ROTATE, CMD_TRANS):
                    vals.add(cmd["v1"])
                    vals.add(cmd["v2"])
                elif "keys" in cmd:
                    vals.update(k for k, _ in cmd["keys"])
                elif cmd["kind"] in (CMD_SHOW, CMD_HIDE):
                    vals.add(cmd["v1"])
                    vals.add(cmd["v2"])
        return sorted(vals)

    def transform_datarefs(self, leaf=None):
        """Datarefs that actually move the batch (show/hide does not count).

        A batch driven by more than one of them cannot be re-keyed into a single
        key table: the table has one x axis, so the second dataref's effect would
        be lost (a gear leg that folds *and* steers would stop steering, without
        any error).
        """
        out = []
        blocks = self.blocks if leaf is None else (self.leaves.get(leaf) or [])
        for blk in blocks:
            for cmd in blk.commands:
                if cmd["kind"] not in (CMD_ROTATE, CMD_ROTATE_KEYS, CMD_TRANS,
                                       CMD_TRANS_KEYS):
                    continue
                dref = cmd.get("dataref")
                if dref and dref not in out:
                    out.append(dref)
        return out

    def normalized_values(self, leaf=None):
        """Where the chain's own keys sit on the 0..1 time line.

        The time line is "every dataref from its lowest keyed value to its
        highest", so each raw key value maps to its own fraction of its own
        dataref's range.  These are the positions the keys belong at: an
        animation written as three keys must come back as three keys, not as a
        hundred samples of the line between them.
        """
        rng = self.ranges()
        out = set()
        blocks = self.blocks if leaf is None else (self.leaves.get(leaf) or [])
        for blk in blocks:
            for cmd in blk.commands:
                dref = cmd.get("dataref")
                if not dref:
                    continue
                if cmd["kind"] in (CMD_ROTATE, CMD_TRANS):
                    vals = (cmd["v1"], cmd["v2"])
                elif "keys" in cmd:
                    vals = tuple(k for k, _ in cmd["keys"])
                elif cmd["kind"] in (CMD_SHOW, CMD_HIDE):
                    vals = (cmd["v1"], cmd["v2"])
                else:
                    continue
                lo, hi = rng.get(dref, (0.0, 1.0))
                if hi == lo:
                    continue
                for v in vals:
                    t = (v - lo) / (hi - lo)
                    out.add(min(1.0, max(0.0, round(t, 6))))
        out.add(0.0)
        out.add(1.0)
        return sorted(out)


def _parse_key(kw, words):
    try:
        if kw == "ANIM_rotate_key":
            return (float(words[1]), float(words[2]))
        return (float(words[1]), (float(words[2]), float(words[3]),
                                 float(words[4])))
    except (IndexError, ValueError):
        return (0.0, (0.0, 0.0, 0.0) if kw == "ANIM_trans_key" else 0.0)


def _floats(words, start, count):
    return [float(w) for w in words[start:start + count]]


def _parse_cmd(kw, words):
    """One animation command as a dict, or None if it is not one."""
    try:
        if kw == "ANIM_rotate":
            axis = tuple(_floats(words, 1, 3))
            r1, r2 = _floats(words, 4, 2)
            if len(words) >= 8:      # older form: only the two angles
                v1, v2 = _floats(words, 6, 2)
            else:
                v1, v2 = 0.0, 0.0
            return {"kind": CMD_ROTATE, "axis": axis, "r1": r1, "r2": r2,
                    "v1": v1, "v2": v2,
                    "dataref": _dataref(words, 8)}
        if kw == "ANIM_rotate_begin":
            return {"kind": CMD_ROTATE_KEYS, "axis": tuple(_floats(words, 1, 3)),
                    "keys": [], "dataref": _dataref(words, 4)}
        if kw == "ANIM_trans":
            p1 = tuple(_floats(words, 1, 3))
            p2 = tuple(_floats(words, 4, 3))
            # older files write only the two points: no v1/v2, no dataref.
            # Such a row is a placement, so any value gives the same matrix.
            if len(words) >= 9:
                v1, v2 = _floats(words, 7, 2)
            else:
                v1, v2 = 0.0, 0.0
            return {"kind": CMD_TRANS, "p1": p1, "p2": p2, "v1": v1, "v2": v2,
                    "dataref": _dataref(words, 9)}
        if kw == "ANIM_trans_begin":
            return {"kind": CMD_TRANS_KEYS, "keys": [],
                    "dataref": _dataref(words, 1)}
        if kw in ("ANIM_show", "ANIM_hide"):
            v1, v2 = _floats(words, 1, 2)
            return {"kind": CMD_SHOW if kw == "ANIM_show" else CMD_HIDE,
                    "v1": v1, "v2": v2, "dataref": _dataref(words, 3)}
    except (IndexError, ValueError):
        return None
    return None


def _dataref(words, index):
    if len(words) <= index:
        return None
    name = words[index]
    if not name or name in ("no_ref", "none", "-"):
        return None
    return name


# ---------------------------------------------------------------------------
# evaluation of a single command
# ---------------------------------------------------------------------------

def _lerp(v, v1, v2, a, b):
    if v2 == v1:
        return a
    t = (v - v1) / (v2 - v1)
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return _lerp_component(a, b, t)


def _key_lookup(keys, v):
    """Piecewise-linear lookup, clamped outside the table."""
    if not keys:
        return None
    if v <= keys[0][0]:
        return keys[0][1]
    if v >= keys[-1][0]:
        return keys[-1][1]
    for i in range(1, len(keys)):
        v1, a = keys[i - 1]
        v2, b = keys[i]
        if v <= v2:
            if v2 == v1:
                return b
            t = (v - v1) / (v2 - v1)
            return _lerp_component(a, b, t)
    return keys[-1][1]


def _lerp_component(a, b, t):
    if isinstance(a, tuple):
        return tuple(x + (y - x) * t for x, y in zip(a, b))
    return a + (b - a) * t


def cmd_matrix(cmd, pose):
    kind = cmd["kind"]
    if kind in (CMD_SHOW, CMD_HIDE):
        return IDENT
    d = cmd.get("dataref")
    v = pose.get(d, 0.0) if d else cmd.get("v1", 0.0)
    if kind == CMD_ROTATE:
        return mat_rot(cmd["axis"], _lerp(v, cmd["v1"], cmd["v2"],
                                          cmd["r1"], cmd["r2"]))
    if kind == CMD_TRANS:
        p = _lerp(v, cmd["v1"], cmd["v2"], cmd["p1"], cmd["p2"])
        return mat_trans(p[0], p[1], p[2])
    if kind == CMD_ROTATE_KEYS:
        keys = sorted(cmd["keys"], key=lambda k: k[0])
        angle = _key_lookup(keys, v)
        return mat_rot(cmd["axis"], 0.0 if angle is None else angle)
    if kind == CMD_TRANS_KEYS:
        keys = sorted(cmd["keys"], key=lambda k: k[0])
        p = _key_lookup(keys, v)
        return mat_trans(*(p or (0.0, 0.0, 0.0)))
    return IDENT


# ---------------------------------------------------------------------------
# writing animation back out (used by the exporter)
# ---------------------------------------------------------------------------

def static_trans(p, dataref="no_ref"):
    """A placement-only translate: same point at both ends, no dataref."""
    body = "%s %s %s" % (OC.fmt_num(p[0]), OC.fmt_num(p[1]), OC.fmt_num(p[2]))
    return "ANIM_trans %s %s 0 0 %s" % (body, body, dataref)


def static_rotate(axis, degrees, dataref="no_ref"):
    """A constant rotation, written as a two-endpoint command with equal angles.

    The exporter uses it for the part's rest orientation, so that the animated
    half of the rotation stays about one fixed axis.
    """
    return "ANIM_rotate %s %s %s %s %s 0 0 %s" % (
        OC.fmt_num(axis[0]), OC.fmt_num(axis[1]), OC.fmt_num(axis[2]),
        OC.fmt_num(degrees), OC.fmt_num(degrees), dataref)


def trans_block(keys, dataref):
    """(v, (x, y, z)) pairs -> ANIM_trans_begin / keys / end."""
    out = ["ANIM_trans_begin %s" % dataref]
    for v, p in sorted(keys, key=lambda k: k[0]):
        out.append("ANIM_trans_key %s %s %s %s"
                   % (OC.fmt_num(v), OC.fmt_num(p[0]), OC.fmt_num(p[1]),
                      OC.fmt_num(p[2])))
    out.append("ANIM_trans_end")
    return out


def rotate_block(axis, keys, dataref):
    """(v, angle) pairs -> ANIM_rotate_begin / keys / end."""
    out = ["ANIM_rotate_begin %s %s %s %s"
           % (OC.fmt_num(axis[0]), OC.fmt_num(axis[1]), OC.fmt_num(axis[2]),
              dataref)]
    for v, a in sorted(keys, key=lambda k: k[0]):
        out.append("ANIM_rotate_key %s %s" % (OC.fmt_num(v), OC.fmt_num(a)))
    out.append("ANIM_rotate_end")
    return out


def visibility_block(show, v1, v2, dataref):
    return "%s %s %s %s" % ("ANIM_show" if show else "ANIM_hide",
                            OC.fmt_num(v1), OC.fmt_num(v2), dataref)


def strip_animation(events):
    """Event list with every animation command and ANIM_begin/end removed.

    Used by the export mode that regenerates the animation from Blender: what
    is left is the non-animation part of the command stream (ATTR_*, LIGHT_*,
    manipulators, IF blocks, geometry), in its original order.
    """
    out = []
    for kind, payload in events:
        if kind in (OC.EV_ANIM_BEGIN, OC.EV_ANIM_END):
            continue
        if kind == OC.EV_RAW and payload:
            words = payload.split()
            if words and words[0] in ANIM_KW:
                continue
        out.append((kind, payload))
    return out


def build(obj8):
    """Parse the ANIM_* structure of an Obj8.  Returns an Animation."""
    anim = Animation().parse(obj8)
    return anim


# ---------------------------------------------------------------------------
# key-table compaction
# ---------------------------------------------------------------------------

def _deviation(point, p1, p2, t):
    """Distance between `point` and the straight line p1..p2 at parameter t."""
    if isinstance(p1, tuple):
        mid = tuple(x + (y - x) * t for x, y in zip(p1, p2))
        return max(abs(x - y) for x, y in zip(point, mid))
    return abs(point - (p1 + (p2 - p1) * t))


def _fits(points, i, j, tol):
    """True when every point between i and j lies on the segment i..j."""
    v1, p1 = points[i]
    v2, p2 = points[j]
    if v2 == v1:
        return False
    for k in range(i + 1, j):
        v, p = points[k]
        if _deviation(p, p1, p2, (v - v1) / (v2 - v1)) > tol:
            return False
    return True


def simplify(points, tol):
    """Drop keys a straight segment already reproduces, keeping both ends.

    Sampling an Action at every frame gives 101 keys per channel; a hinge that
    swings linearly needs two.  Without this every exported object would grow
    by a few thousand lines of key table.
    """
    if len(points) <= 2:
        return list(points)
    keep = [0]
    i = 0
    n = len(points)
    while i < n - 1:
        best = i + 1
        j = i + 2
        while j < n and _fits(points, i, j, tol):
            best = j
            j += 1
        keep.append(best)
        i = best
    return [points[k] for k in keep]

# ---------------------------------------------------------------------------
# how many keys does this animation actually need?
# ---------------------------------------------------------------------------

def _axis_180(rel):
    """Rotation axis of a 180 degree rotation, where the skew part vanishes."""
    diag = [rel[i][i] + 1.0 for i in range(3)]
    j = max(range(3), key=lambda i: diag[i])
    v = [rel[i][j] + (1.0 if i == j else 0.0) for i in range(3)]
    n = math.sqrt(sum(x * x for x in v))
    return tuple(x / n for x in v) if n > 1e-9 else None


def _fit_single_axis(mats):
    """(rest rotation, axis, [angle in degrees]) if the family is a hinge.

    Every sample is written as `rest . R(axis, angle)`: `rest` is what a static
    ANIM_rotate in the file puts the part at, everything the dataref does on top
    of it is one axis.  A pure translation (no rotation at all) fits with a
    zero angle.  Returns None when the samples do not have that shape.
    """
    rest = tuple(row[:3] for row in mats[0])
    rt = _transpose3(rest)
    rels = [_mul3(rt, tuple(row[:3] for row in m)) for m in mats]
    found = []
    axis = None
    for rel in rels:
        tr = rel[0][0] + rel[1][1] + rel[2][2]
        cos = max(-1.0, min(1.0, (tr - 1.0) * 0.5))
        angle = math.degrees(math.acos(cos))
        skew = (rel[2][1] - rel[1][2], rel[0][2] - rel[2][0],
                rel[1][0] - rel[0][1])
        n = math.sqrt(sum(v * v for v in skew))
        ax = None
        if n > 1e-9:
            ax = tuple(v / n for v in skew)
        elif angle > 1e-6:
            ax = _axis_180(rel)
        found.append((angle, ax))
        if ax is not None and axis is None:
            axis = ax
    if axis is None:
        return rest, (0.0, 0.0, 1.0), [0.0] * len(rels)

    angles = []
    for rel, (angle, ax) in zip(rels, found):
        if ax is None:
            angles.append(0.0)
            continue
        signed = angle if sum(a * b for a, b in zip(axis, ax)) >= 0 else -angle
        check = mat_rot(axis, signed)
        if max(abs(check[i][j] - rel[i][j])
               for i in range(3) for j in range(3)) > 1e-4:
            return None
        angles.append(signed)
    return rest, axis, angles


def _pose_at_t(ranges, t):
    return {d: lo + (hi - lo) * t for d, (lo, hi) in ranges.items()}


def refine_keys(anim, leaf, tol=1e-3, max_keys=33):
    """The smallest set of key positions that reproduces the leaf's motion.

    The file's own keys are the starting point -- a hinge needs exactly those.
    Two datarefs composed (gear deploy *and* tire steer), or two rotations about
    different axes driven by one dataref, bow away from the straight line between
    keys; there the missing keys are added where the bow shows, and only there,
    so a three-key animation stays three keys and a curved one gets as many as it
    needs to stay within `tol` of the real path.

    Returns the positions, or None when the motion is not a hinge at all (the
    caller then leaves the file's animation alone).
    """
    if len(anim.transform_datarefs(leaf)) > 1:
        return None                     # one key table cannot carry two datarefs
    ts = anim.normalized_values(leaf)
    if len(ts) < 2:
        return ts
    ranges = anim.ranges()
    if not ranges:
        return ts
    for _ in range(8):
        mats = [anim.matrix_at(_pose_at_t(ranges, t), leaf) for t in ts]
        fitted = _fit_single_axis(mats)
        if fitted is None:
            return None
        rest, axis, angles = fitted
        offsets = [(m[0][3], m[1][3], m[2][3]) for m in mats]
        worst = None
        for i in range(len(ts) - 1):
            t1, t2 = ts[i], ts[i + 1]
            for k in (0.25, 0.5, 0.75):
                t = t1 + (t2 - t1) * k
                want = anim.matrix_at(_pose_at_t(ranges, t), leaf)
                angle = angles[i] + (angles[i + 1] - angles[i]) * k
                off = tuple(a + (b - a) * k
                            for a, b in zip(offsets[i], offsets[i + 1]))
                model = _mul3(rest, mat_rot(axis, angle))
                dev = max(abs(model[r][c] - want[r][c])
                          for r in range(3) for c in range(3))
                dev = max(dev, max(abs(off[c] - want[c][3]) for c in range(3)))
                if dev > tol and (worst is None or dev > worst[0]):
                    worst = (dev, t)
        if worst is None:
            return ts
        if len(ts) >= max_keys:
            return None
        if any(abs(worst[1] - t) < 1e-6 for t in ts):
            return None
        ts = sorted(ts + [round(worst[1], 6)])
    return None
