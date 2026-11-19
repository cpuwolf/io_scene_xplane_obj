"""X-Plane aircraft VR configuration (``<acf>_vrconfig.txt``).

Format: developer.x-plane.com, "Aircraft VR Configuration (_vrconfig.txt) File
Format Specification", last updated 2024-01 (X-Plane 12).  Pure Python, no bpy,
so it can be tested and diffed outside Blender the way ``objcodec`` is.

What this module is for: the *reader*.  Reading it is the part that has to be
exact, because the file is hand-edited and nothing may be lost:

* every line is kept with its index, and re-emitting the stored lines reproduces
  the file byte for byte (a later writer replaces only the blocks that changed,
  so author comments, ``####`` banners and line order survive);
* the numbers are read leniently - the C90B GTN ship file writes
  ``PRESET_XYZ -0.3658, 0.8704, -0.5053`` with commas, and X-Plane accepts it;
* ``PRESET_PSI`` is documented as 0-360 but the corpus contains ``-3.0``.  Values
  are kept as written; nothing is normalised on the way in.

Three facts from the spec that the code has to carry, because getting any of
them wrong is silent - the aircraft loads, the hotspots are just in the wrong
place:

1. **Two coordinate frames.** ``AABB`` and ``PRESET_XYZ`` are in *aircraft*
   coordinates: metres, +X right, +Y up, +Z aft.  ``YOKE_MANIP_*`` and
   ``JOYSTICK_MANIP`` are in **OBJ units** instead (the object's own frame).
2. **Three origins**, chosen by ``REF_POINT_ACF``: absent means the origin is the
   aircraft's *current* CG; bare (or ``0 0``) means the Plane-Maker authoring
   point; with ``Y Z`` it means the *old* CG, given as an offset from the
   authoring point in metres.  ``origin()`` returns which one applies.
3. **``BEGIN_MANIP`` is keyed by the OBJ manipulator**, not by ``ACTION``.  A
   block may turn a ``command_knob`` into an ``axis_knob``; 55 of the 519 blocks
   in the corpus do exactly that, and using ``ACTION`` as the lookup key loses
   all of them.  The key is the type plus one or two commands/datarefs, plus the
   optional tooltip that disambiguates two otherwise identical manipulators.

Measured against the 25 ``*_vrconfig.txt`` files X-Plane 12 ships plus the
third-party aircraft on this machine: 78 hotspots, 519 manip blocks, 516 of them
resolving to exactly one ``ATTR_manip_*`` line, none ambiguous.
"""

import math
import os
import re

SUFFIX = "_vrconfig.txt"
HEADER = ("A", "1100", "VRCONFIG")

#: types the hotspot XYZ points at: a sitting hotspot places the *head* there, a
#: standing one places the *feet* (so a preview camera needs an eye height).
KINDS = ("SITTING", "STANDING")

#: default eye height above the floor for a STANDING hotspot, metres.  Preview
#: only - the file always stores the feet position.
EYE_HEIGHT = 1.70

_NUM = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def _numbers(tokens):
    """Floats from a token list, tolerating ``-0.36,`` and stray text."""
    out = []
    for tok in tokens:
        tok = tok.replace(",", "")
        if _NUM.match(tok):
            out.append(float(tok))
    return out


class Hotspot(object):
    """One ``BEGIN_TELEPORT_HOTSPOT`` block."""

    __slots__ = ("kind", "name", "aabb", "xyz", "psi", "the", "phi",
                 "lines", "first_line")

    def __init__(self):
        self.kind = "SITTING"
        self.name = ""
        self.aabb = None          # (x1, y1, z1, x2, y2, z2) aircraft metres
        self.xyz = None           # (x, y, z) aircraft metres, head or feet
        self.psi = 0.0            # heading, degrees about +Y
        self.the = 0.0            # pitch, degrees about +X
        self.phi = 0.0            # roll, degrees about +Z
        self.lines = []           # raw lines, BEGIN..END inclusive
        self.first_line = 0       # index into VrConfig.lines

    @property
    def centre(self):
        """AABB centre, aircraft metres."""
        x1, y1, z1, x2, y2, z2 = self.aabb
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0, (z1 + z2) / 2.0)

    @property
    def size(self):
        """AABB extents, metres (always positive - a file may write min>max)."""
        x1, y1, z1, x2, y2, z2 = self.aabb
        return (abs(x2 - x1), abs(y2 - y1), abs(z2 - z1))

    def eye(self):
        """Where a preview camera goes: the stored point, raised for STANDING."""
        x, y, z = self.xyz
        return (x, y + (EYE_HEIGHT if self.kind == "STANDING" else 0.0), z)

    def __repr__(self):
        return "<Hotspot %s %s>" % (self.kind, self.name or "?")


class Manip(object):
    """One ``BEGIN_MANIP`` block: a VR override of an existing OBJ manipulator."""

    __slots__ = ("type", "a", "b", "tooltip", "options", "action",
                 "lines", "first_line", "sources")

    def __init__(self):
        self.type = ""            # the OBJ's type, without `ATTR_manip_`
        self.a = None             # command / dataref 1
        self.b = None             # command / dataref 2, or None
        self.tooltip = None       # disambiguator, rarely used (0 of 519 here)
        self.options = []         # the directive lines inside the block
        self.action = None        # ACTION <type> ..., if the block retypes it
        self.lines = []
        self.first_line = 0
        self.sources = []         # filled in by resolve()

    def key(self):
        """Lookup key against the OBJ side: (type, a, b-or-None)."""
        return (self.type, self.a, self.b)

    def __repr__(self):
        return "<Manip %s %s %s>" % (self.type, self.a, self.b or "")


class VrConfig(object):
    """A parsed ``<acf>_vrconfig.txt``."""

    def __init__(self):
        self.path = ""
        self.lines = []           # the whole file, line endings stripped
        self.ending = "\n"        # "\n", "\r\n" or "\r"
        self.trailing = True      # 11 of the 25 shipped files end without one
        self.header = []          # the three header lines, verbatim
        self.ref_point_raw = None  # the REF_POINT_ACF line, if any
        self.ref_point = None     # (y, z) metres, or None when the line is bare
        self.hotspots = []
        self.manips = []
        self.unknown = []         # (line index, text) of directives not modelled

    # -- the file's own text -------------------------------------------------
    def text(self):
        """Re-emit the file exactly as it was read (for a verbatim write-back).

        Byte for byte: the line ending is the file's own, and a file that does not
        end with a newline does not gain one - 11 of the 25 files X-Plane 12 ships
        stop at ``END_MANIP`` with no newline, and a writer that "fixes" that
        turns a one-block edit into a whole-file diff.
        """
        body = self.ending.join(self.lines)
        return body + self.ending if self.trailing else body

    # -- coordinate frame ----------------------------------------------------
    def ref_point_offset(self):
        """``REF_POINT_ACF`` as (y, z) metres, ``None`` for no line, (0, 0) bare."""
        if self.ref_point_raw is None:
            return None
        return self.ref_point or (0.0, 0.0)

    def origin_kind(self):
        raw = self.ref_point_raw
        if raw is None:
            return "cg"
        return "acf" if not self.ref_point else "acf_old_cg"

    def origin(self, cg_feet=None):
        """Where aircraft coordinates start, in the authoring-point frame (m).

        ``cg_feet`` is the aircraft's own CG from the .acf (``acf/_cgX/Y/Z``,
        feet).  It is only needed for the common case where the file has no
        ``REF_POINT_ACF``; X-Plane then measures from the CG.

        Returns ``{"kind", "offset", "note"}`` - the offset is metres in the
        authoring-point frame (X right, Y up, Z aft), which is the frame the
        .acf's own ``_obja/<n>/_v10_att_*_acf_prt_ref`` offsets live in.
        """
        kind = self.origin_kind()
        if kind == "cg":
            if cg_feet is None:
                return {"kind": kind, "offset": (0.0, 0.0, 0.0),
                        "note": "no CG available - coordinates used as they are"}
            x, y, z = (v * 0.3048 for v in cg_feet)
            return {"kind": kind, "offset": (x, y, z),
                    "note": "origin is the aircraft CG (%.2f, %.2f, %.2f m)" % (x, y, z)}
        y, z = self.ref_point_offset()
        return {"kind": kind, "offset": (0.0, y, z),
                "note": "origin is REF_POINT_ACF (%.3f, %.3f m)" % (y, z)}

    def hotspot_xyz(self, hotspot, origin):
        """Aircraft-frame position of a hotspot in metres, origin applied."""
        ox, oy, oz = origin["offset"]
        x, y, z = hotspot.eye()
        return (x + ox, y + oy, z + oz)

    # -- manipulator lookup --------------------------------------------------
    def unresolved(self, index):
        """Hotspot-free helper: manip blocks with no candidate in `index`."""
        return [m for m in self.manips if not index.get(m.key()) and
                not index.get((m.type, m.a, None))]

    def __repr__(self):
        return "<VrConfig %d hotspots %d manips>" % (len(self.hotspots),
                                                     len(self.manips))


def manip_key(raw):
    """Lookup key for an ``ATTR_manip_*`` OBJ line.

    ``ATTR_manip_command_knob  hand  cmd1  cmd2  tooltip`` and
    ``ATTR_manip_axis_knob  hand  0.0 1.0 0.05 0.25  dref  tooltip`` put a cursor
    word and (for axis types) numeric ranges between the type and the
    commands/datarefs, so the key is the type plus the commands/datarefs that
    follow.  Those are the tokens with a ``/`` in them (every command and dataref
    in X-Plane is ``sim/...``, ``laminar/...`` or ``custom/...``); the free-text
    tooltip is not one, quoted or not.  Tooltips are dropped from the key:
    X-Plane matches on the type and the commands/datarefs and only falls back to
    the tooltip when two manipulators are otherwise identical (which the corpus
    never needs).
    """
    words = raw.split()
    if not words:
        return None
    head = words[0]
    if not head.startswith("ATTR_manip_"):
        return None
    kind = head[len("ATTR_manip_"):]
    body = words[2:] if len(words) > 1 else []   # [1] is the cursor word
    args = [w for w in body
            if "/" in w and not _NUM.match(w.replace(",", ""))]
    return (kind, args[0] if args else None, args[1] if len(args) > 1 else None)


def manip_index(lines):
    """{key: [source, ...]} from ``(source, raw ATTR_manip line)`` pairs."""
    index = {}
    for source, raw in lines:
        key = manip_key(raw)
        if key is None:
            continue
        index.setdefault(key, []).append(source)
        # A manipulator with one command/dataref is addressed without a second
        # argument in the vrconfig, so index the short key too.
        index.setdefault((key[0], key[1], None), []).append(source)
    return index


def resolve(vr, index):
    """Attach the OBJ side to every manip block; returns the unresolved ones.

    ``index`` comes from :func:`manip_index`.  Each block records
    ``m.sources`` (the objects or files that matched) - an empty list means the
    vrconfig points at a manipulator this aircraft no longer has, which is a real
    condition in the wild (the CitationX ships three of them) and the one thing
    worth warning about at import time.
    """
    missing = []
    for m in vr.manips:
        hits = index.get(m.key()) or index.get((m.type, m.a, None)) or []
        seen = []
        for h in hits:
            if h not in seen:
                seen.append(h)
        m.sources = seen
        if not seen:
            missing.append(m)
    return missing


def view_vectors(psi, the, phi):
    """Camera basis for a hotspot orientation, in X-Plane aircraft axes.

    ``PSI`` is a heading about +Y (0 = forward, 90 = toward the right wing),
    ``THE`` a pitch about +X (90 = straight up), ``PHI`` a roll about +Z
    (90 = right ear to the shoulder).  Returns ``(forward, up)`` as unit vectors;
    both are rotated by PHI, so a preview camera built from them is upright at
    PHI = 0 and leans correctly otherwise.
    """
    p, t = math.radians(psi), math.radians(the)
    forward = (math.sin(p) * math.cos(t), math.sin(t),
               -math.cos(p) * math.cos(t))
    up = _rotate((0.0, 1.0, 0.0), forward, math.radians(phi))
    return forward, up


def _rotate(vec, axis, angle):
    """Rodrigues rotation of `vec` about the unit `axis`."""
    if abs(angle) < 1e-12:
        return vec
    kx, ky, kz = axis
    vx, vy, vz = vec
    c, s = math.cos(angle), math.sin(angle)
    dot = kx * vx + ky * vy + kz * vz
    cross = (ky * vz - kz * vy, kz * vx - kx * vz, kx * vy - ky * vx)
    return (vx * c + cross[0] * s + kx * dot * (1 - c),
            vy * c + cross[1] * s + ky * dot * (1 - c),
            vz * c + cross[2] * s + kz * dot * (1 - c))


def to_bl(vec):
    """X-Plane (x, y, z) -> Blender (x, -z, y); works for points and vectors.

    The same mapping ``xpmesh.xp_to_bl`` and ``acfplace._axis_matrix`` use, so a
    hotspot lands in the frame the placed objects are drawn in.
    """
    return (vec[0], -vec[2], vec[1])


def parse_text(text, path=""):
    """Parse a vrconfig file that is already in memory."""
    vr = VrConfig()
    vr.path = path
    if "\r\n" in text:
        vr.ending = "\r\n"
    elif "\r" in text:
        vr.ending = "\r"
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    vr.trailing = body.endswith("\n") or body == ""
    vr.lines = body.split("\n")
    if vr.lines and vr.lines[-1] == "":
        vr.lines.pop()                        # the trailing newline, not a line

    cur_hotspot = None
    cur_manip = None
    seen_header = 0
    for i, line in enumerate(vr.lines):
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        word = stripped.split()[0]
        if seen_header < 3 and word in HEADER:
            vr.header.append(stripped)
            seen_header += 1
            continue
        if cur_hotspot is not None:
            cur_hotspot.lines.append(line)
            if word == "END_TELEPORT_HOTSPOT":
                vr.hotspots.append(cur_hotspot)
                cur_hotspot = None
            elif word == "AABB":
                nums = _numbers(stripped.split()[1:])
                cur_hotspot.aabb = tuple(nums[:6]) if len(nums) >= 6 else None
            elif word == "PRESET_XYZ":
                nums = _numbers(stripped.split()[1:])
                cur_hotspot.xyz = tuple(nums[:3]) if len(nums) >= 3 else None
            elif word == "PRESET_PSI":
                nums = _numbers(stripped.split()[1:])
                if nums:
                    cur_hotspot.psi = nums[0]
            elif word == "PRESET_THE":
                nums = _numbers(stripped.split()[1:])
                if nums:
                    cur_hotspot.the = nums[0]
            elif word == "PRESET_PHI":
                nums = _numbers(stripped.split()[1:])
                if nums:
                    cur_hotspot.phi = nums[0]
            continue
        if cur_manip is not None:
            cur_manip.lines.append(line)
            if word == "END_MANIP":
                vr.manips.append(cur_manip)
                cur_manip = None
            else:
                cur_manip.options.append(stripped)
                if word == "ACTION":
                    cur_manip.action = stripped
            continue
        if word == "BEGIN_TELEPORT_HOTSPOT":
            hs = Hotspot()
            hs.first_line = i
            hs.lines = [line]
            args = stripped.split()[1:]
            if args and args[0].upper() in KINDS:
                hs.kind = args[0].upper()
                args = args[1:]
            hs.name = " ".join(args)
            cur_hotspot = hs
        elif word == "BEGIN_MANIP":
            mb = Manip()
            mb.first_line = i
            mb.lines = [line]
            args = stripped.split()[1:]
            if args:
                mb.type = args[0]
            if len(args) > 1:
                mb.a = args[1]
            if len(args) > 2:
                mb.b = args[2]
            if len(args) > 3:                 # the optional tooltip
                mb.tooltip = " ".join(args[3:])
            cur_manip = mb
        elif word == "REF_POINT_ACF":
            vr.ref_point_raw = stripped
            nums = _numbers(stripped.split()[1:])
            vr.ref_point = (nums[0], nums[1]) if len(nums) >= 2 else None
        elif word == "END_TELEPORT_HOTSPOT" or word == "END_MANIP":
            vr.unknown.append((i, stripped))   # unbalanced terminator
        else:
            vr.unknown.append((i, stripped))
    if cur_hotspot is not None:
        vr.hotspots.append(cur_hotspot)        # unterminated: keep what we have
    if cur_manip is not None:
        vr.manips.append(cur_manip)
    return vr


def parse(path):
    """Parse a ``<acf>_vrconfig.txt``."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return parse_text(text, path)


def find(acf_path):
    """The vrconfig that belongs to an .acf, or None.

    The name has to match the ACF's prefix and sit in the same folder - that is
    how X-Plane finds it, so the same rule is used here.
    """
    if not acf_path:
        return None
    stem = os.path.splitext(os.path.basename(acf_path))[0]
    candidate = os.path.join(os.path.dirname(acf_path), stem + SUFFIX)
    return candidate if os.path.isfile(candidate) else None
