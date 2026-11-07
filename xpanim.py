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
except ImportError:           # loaded as a plain module by verify/test_anim.py
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
            if kw == "ANIM_keyframe_loop":
                # legacy loop marker; it sits inside a key table and says the
                # table repeats, which changes nothing we evaluate
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
                    # real files list keys in any order (a gauge table can put
                    # its 2250 sentinel between 220 and 260), so take the span,
                    # not the first and last line
                    vals = [k for k, _ in cmd["keys"]]
                    lo, hi = min(vals), max(vals)
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

def build(obj8):
    """Parse the ANIM_* structure of an Obj8.  Returns an Animation."""
    anim = Animation().parse(obj8)
    return anim
