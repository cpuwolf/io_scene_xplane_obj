"""X-Plane OBJ8 (.obj) reader/writer.

Pure Python: no bpy import, so it can be unit-tested and run outside Blender.

Format notes that this module relies on (all from the Laminar OBJ8 spec, and
verified against the King Air C90B EVO object set that ships with X-Plane 12):

* 3-line header: "A" or "I" (Mac vs Unix line endings), the version number
  (800), then the word OBJ.
* Then records, one per line, in three groups: global properties first, then
  the data tables (VT/VLINE/VLIGHT/IDX), then commands.  Commands are executed
  in file order and may never be reordered, so this module keeps them as an
  ordered event list.
* `TRIS <offset> <count>` indexes the *index table*, not the vertex table, and
  the index range must be contiguous.  So a triangle batch's vertex indices
  live in IDX rows, and regenerating geometry means regenerating those rows.
* The front face is the one whose vertices appear CLOCKWISE in file order, which
  in a right-handed Y-up space means the winding normal points *inward*.  Both
  the importer and the exporter flip winding; see `flip_winding()`.

Design: the file is parsed into
    globals  : ordered raw lines of the attribute/global region
    vt       : list of VtRow (pos, normal, uv + the exact source text)
    vline/vlight : raw lines (preserved verbatim, they are never edited)
    idx      : the flat index table
    events   : ordered command stream, where a geometry leaf carries the slice
               of the index table it dispatches, plus a snapshot of the
               attribute state in effect at that point
Nothing is dropped: unrecognised keywords survive as raw events.
"""

import os
import re

# ---------------------------------------------------------------------------
# keyword classification tables
# ---------------------------------------------------------------------------

DATA_KW = {"VT", "VLINE", "VLIGHT", "IDX", "IDX10"}

# Attribute state is sequential and "sticks": a batch of triangles is drawn with
# whatever state is current where its TRIS appears.  Since state is never
# animated, the parser snapshots it per leaf and hoists the lines out of ANIM
# blocks -- that is exactly what a regenerating exporter needs to re-emit.
state_category_map = {
    "ATTR_poly_os": "poly_os",
    "ATTR_shiny_rat": "shiny",
    "ATTR_reset": "shiny",
    "ATTR_blend": "blend",
    "ATTR_no_blend": "blend",
    "ATTR_shadow_blend": "blend",
    "ATTR_cockpit": "cockpit",
    "ATTR_cockpit_region": "cockpit",
    "ATTR_cockpit_lit_only": "cockpit",
    "ATTR_cockpit_device": "cockpit",
    "ATTR_no_cockpit": "cockpit",
    "ATTR_hard": "hard",
    "ATTR_hard_deck": "hard",
    "ATTR_no_hard": "hard",
    "ATTR_draw_enable": "draw",
    "ATTR_draw_disable": "draw",
    "ATTR_solid_camera": "camera",
    "ATTR_no_solid_camera": "camera",
    "ATTR_shadow": "shadow",
    "ATTR_no_shadow": "shadow",
    "ATTR_light_level": "light_level",
    "ATTR_light_level_reset": "light_level",
    "ATTR_draped": "draped",
    "ATTR_no_draped": "draped",
    "ATTR_hud_glass": "hud",
    "ATTR_hud_reset": "hud",
    "ATTR_manip_none": "manip",
    "ATTR_manip_noop": "manip",
    "ATTR_axis_detented": "detent_axis",
    "ATTR_axis_detent_range": "detent_range",   # repeatable
    "ATTR_shade_flat": "shade",
    "ATTR_shade_smooth": "shade",
    # these two ride *with* a manipulator instead of replacing it: they were
    # landing in the "manip" bucket and wiping the ATTR_manip_* line before it
    "ATTR_manip_wheel": "manip_wheel",
    "ATTR_manip_keyframe": "manip_keyframe",
    "ATTR_cull": "cull",
    "ATTR_no_cull": "cull",
    "ATTR_depth": "depth",
    "ATTR_no_depth": "depth",
}

# Order used when a state snapshot has to be written back out.  Anything not
# listed lands at the end in its original insertion order.
STATE_ORDER = [
    "shade", "cull", "depth", "poly_os", "blend", "shiny", "shadow",
    "cockpit", "hud", "hard", "draw", "camera", "draped", "light_level",
    "manip", "detent_axis", "detent_range",
]


def state_category(kw):
    """Attribute state category for a keyword, or None if it is not state."""
    if kw.startswith("ATTR_manip_"):
        return state_category_map.get(kw, "manip")
    return state_category_map.get(kw)


_num_re = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


# ---------------------------------------------------------------------------
# small value holders
# ---------------------------------------------------------------------------

class VtRow(object):
    """One VT row: position, normal, texture coordinate and its source text."""

    __slots__ = ("px", "py", "pz", "nx", "ny", "nz", "s", "t",
                 "has_normal", "text")

    def __init__(self, px, py, pz, nx, ny, nz, s, t, has_normal, text):
        self.px, self.py, self.pz = px, py, pz
        self.nx, self.ny, self.nz = nx, ny, nz
        self.s, self.t = s, t
        self.has_normal = has_normal
        self.text = text          # everything after the keyword, verbatim

    def as_tuple(self):
        return (self.px, self.py, self.pz, self.nx, self.ny, self.nz,
                self.s, self.t)

    def __repr__(self):
        return "VtRow(%r)" % (self.as_tuple(),)


class Leaf(object):
    """A geometry command (TRIS or LINES) and the state it draws under."""

    __slots__ = ("kind", "indices", "state", "offset", "count")

    def __init__(self, kind, indices, state, offset, count):
        self.kind = kind            # "TRIS" or "LINES"
        self.indices = list(indices)   # values into vt (TRIS) or vline (LINES)
        self.state = state          # list of [category, [raw, ...]]
        self.offset = offset        # original <offset> argument
        self.count = count          # original <count> argument


# event kinds in the ordered command stream
EV_ANIM_BEGIN = "A"
EV_ANIM_END = "Z"
EV_RAW = "R"
EV_LEAF = "L"


class Obj8(object):
    def __init__(self):
        self.line_ending = "\n"
        self.header_flag = "I"        # "A" (CR) or "I" (LF/CRLF)
        self.version_line = "800"
        self.globals = []             # raw lines, in file order
        self.vt = []
        self.vline = []               # raw lines
        self.vlight = []              # raw lines
        self.idx = []
        self.leaves = []
        self.events = []              # (kind, payload)
        self.trailer = []             # comments after the last command
        self.warnings = []
        # White space conventions of the source file, so regenerated tables and
        # commands look exactly like the file they came from (tabs and spaces
        # are both legal, and the two exporters in the wild disagree).
        self.idx_sep = ("\t", " ")
        self.cmd_sep = ("\t", " ")

    # -- convenience ------------------------------------------------------
    def global_value(self, keyword):
        """Tuple of argument words of the first matching global line, or None."""
        for line in self.globals:
            w = line.split()
            if w and w[0] == keyword:
                return tuple(w[1:])
        return None

    def global_values(self, keyword):
        """Every matching global line's argument words, in file order.

        ``TEXTURE_MAP`` repeats (one per usage), so the single-value helper is
        not enough to read a file's material setup.
        """
        out = []
        for line in self.globals:
            w = line.split()
            if w and w[0] == keyword:
                out.append(tuple(w[1:]))
        return out

    def triangles(self):
        return [lf for lf in self.leaves if lf.kind == "TRIS"]

    def __repr__(self):
        return "<Obj8 %d VT %d IDX %d leaves %d events>" % (
            len(self.vt), len(self.idx), len(self.leaves), len(self.events))


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def detect_line_ending(text):
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    if crlf >= cr and crlf >= lf and crlf:
        return "\r\n"
    if cr > lf and cr:
        return "\r"
    return "\n"


def parse(path):
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("latin-1")        # OBJ8 is 7-bit ASCII; latin-1 is lossless
    obj = parse_text(text)
    obj.source_path = os.path.abspath(path)
    return obj


def _split_line(line):
    """Split a record into keyword + argument words, ignoring any whitespace."""
    return line.split()


def strip_comment(line):
    """Drop a trailing ``# ...``, keeping a ``#`` that sits inside a token.

    A trailing comment is not in the OBJ8 spec, but exporters write the vertex
    index after a `VT` row (``VT ... # 12``) and add-ons ship such files for
    years, so a file the sim loads must import.  `#` only starts a comment at a
    token boundary, which is what keeps a texture named `livery#2.png` whole.
    The comment itself is not parsed but stays in the stored raw line, so the
    text the conversion reads back is the file's own.
    """
    start = 0
    while True:
        i = line.find("#", start)
        if i < 0:
            return line
        if i == 0 or line[i - 1] in " \t":
            return line[:i].rstrip()
        start = i + 1


def parse_text(text):
    obj = Obj8()
    obj.line_ending = detect_line_ending(text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # ---- header ------------------------------------------------------
    if len(lines) < 3:
        raise ValueError("not an OBJ8 file: fewer than 3 header lines")
    flag = lines[0].strip()
    if flag not in ("A", "I"):
        raise ValueError("not an OBJ8 file: header line 1 is %r" % flag[:20])
    obj.header_flag = flag
    obj.version_line = lines[1].strip()
    if lines[2].strip() != "OBJ":
        raise ValueError("not an OBJ8 file: header line 3 is %r" % lines[2][:20])

    # ---- body --------------------------------------------------------
    # `phase` tracks where we are in the file's three-section layout:
    #   0 globals, 1 data tables, 2 commands.  Records are classified by
    #   keyword first, so a file with slightly odd ordering still parses.
    seen_data = False
    in_commands = False
    state = {}                     # category -> [raw, ...]
    state_order = []
    events = obj.events
    leaves = obj.leaves
    pending = []                   # comment lines not yet assigned a section
    captured = []                  # separator detection bookkeeping

    def flush_pending(to_globals):
        if not pending:
            return
        if to_globals:
            obj.globals.extend(pending)
        else:
            for p in pending:
                events.append((EV_RAW, p))
        del pending[:]

    for lineno, line in enumerate(lines[3:], start=4):
        stripped = line.strip()
        if not stripped:
            continue
        # A `#` tail is tolerated the way the sim tolerates it: ignored for the
        # numbers, kept verbatim in the raw line so the file still round-trips.
        body = strip_comment(stripped)
        words = _split_line(body) if body else [stripped]
        kw = words[0]

        if kw == "VT":
            # the tail keeps trailing spaces: some exporters pad VT rows to a
            # fixed width and an untouched file must survive unchanged
            obj.vt.append(_parse_vt(words, lineno, line.lstrip()))
            seen_data = True
            continue
        if kw in ("IDX", "IDX10"):
            if len(words) > 1 and not idx_sep_found(captured):
                # both layouts: IDX10 packs many values per row, IDX has one,
                # and a file may use either (or only single rows)
                obj.idx_sep = _seps(line.lstrip())
                captured.append(kw)
            for w in words[1:]:
                obj.idx.append(int(w))
            seen_data = True
            continue
        if kw == "VLINE":
            obj.vline.append(stripped)
            seen_data = True
            continue
        if kw == "VLIGHT":
            obj.vlight.append(stripped)
            seen_data = True
            continue

        is_comment = kw.startswith("#")

        if not in_commands:
            if not seen_data and _is_global(kw):
                flush_pending(True)
                obj.globals.append(stripped)
                continue
            if is_comment:
                pending.append(stripped)
                continue
            # anything else starts the command section
            in_commands = True
            flush_pending(False)

        if kw == "ANIM_begin":
            events.append((EV_ANIM_BEGIN, None))
            continue
        if kw == "ANIM_end":
            events.append((EV_ANIM_END, None))
            continue

        cat = state_category(kw)
        if cat is not None:
            if cat in ("detent_range", "manip_keyframe"):
                state.setdefault(cat, []).append(stripped)
            elif cat == "manip":
                # ATTR_axis_detent_range lines belong to the manipulator that
                # follows them, not to the file: they are repeatable, so a new
                # manipulator starts a new list (C90B cockpit: 30 ranges piled
                # onto one object instead of 3 on each of ten)
                state.pop("detent_range", None)
                state.pop("manip_keyframe", None)
                state[cat] = [stripped]
            else:
                if cat not in state:
                    state_order.append(cat)
                state[cat] = [stripped]
            continue

        if kw in ("TRIS", "LINES"):
            if len(words) < 3:
                obj.warnings.append("line %d: %s needs offset and count"
                                    % (lineno, kw))
                events.append((EV_RAW, stripped))
                continue
            off, cnt = int(words[1]), int(words[2])
            if off < 0 or cnt < 0 or off + cnt > len(obj.idx):
                obj.warnings.append(
                    "line %d: %s %d %d runs past the index table (%d entries)"
                    % (lineno, kw, off, cnt, len(obj.idx)))
            idx = obj.idx[off:off + cnt]
            step = 3 if kw == "TRIS" else 2
            if cnt % step:
                obj.warnings.append(
                    "line %d: %s count %d is not a multiple of %d"
                    % (lineno, kw, cnt, step))
            if "TRIS" not in captured:
                obj.cmd_sep = _seps(line.lstrip())
                captured.append("TRIS")
            leaves.append(Leaf(kw, idx, _snapshot(state, state_order), off, cnt))
            events.append((EV_LEAF, len(leaves) - 1))
            continue

        # anything we do not model: preserved verbatim in place
        events.append((EV_RAW, stripped))

    flush_pending(True)

    # POINT_COUNTS is mandatory: X-Plane's loader sizes its tables from it
    # before it reads a single row.  The writer always emits one, so a missing
    # record is only worth a warning here -- but say which numbers were used,
    # because those are the ones a written file will carry.
    if obj.global_value("POINT_COUNTS") is None:
        obj.warnings.append(
            "missing POINT_COUNTS; inferred %d %d %d %d from the tables and it "
            "is added when the file is written"
            % (len(obj.vt), len(obj.vline), len(obj.vlight), len(obj.idx)))
    return obj


def with_point_counts(text, obj=None):
    """The same file text with a POINT_COUNTS record, if it has none.

    The counts are exactly the tables the text parses to, so an untouched
    import of a file that never carried the record can still be handed back as
    something the sim will load -- without touching any other byte.
    """
    if obj is None:
        obj = parse_text(text)
    if obj.global_value("POINT_COUNTS") is not None:
        return text
    nl = obj.line_ending or "\n"
    counts = (len(obj.vt), len(obj.vline), len(obj.vlight), len(obj.idx))
    line = "POINT_COUNTS\t%d\t%d\t%d\t%d" % counts
    lines = text.split(nl)
    at = None
    for i, ln in enumerate(lines):
        w = ln.split()
        if w and w[0] in DATA_KW:
            at = i
            break
    if at is None:
        if lines and lines[-1] == "":
            lines.insert(len(lines) - 1, line)
        else:
            lines.append(line)
    else:
        # put it after the global records, before the blank line that opens the
        # data tables (a file may have several blank lines there)
        while at > 0 and not lines[at - 1].strip():
            at -= 1
        lines.insert(at, line)
    return nl.join(lines)


_seps_re = re.compile(r"(\S+)(\s+)(\S+)(\s+)(\S+)")
_seps_2_re = re.compile(r"(\S+)(\s+)(\S+)")


def _seps(line):
    """(keyword separator, number separator) of a record.

    A single-value `IDX 0` row has no second separator to read, so the number
    separator falls back to a space -- otherwise a file whose indices are
    written one per row would come back with tabs and the writer would stop
    being idempotent.
    """
    m = _seps_re.match(line)
    if m:
        return (m.group(2), m.group(4))
    m = _seps_2_re.match(line)
    if m:
        return (m.group(2), " ")
    return ("\t", " ")


def idx_sep_found(captured):
    return "IDX10" in captured or "IDX" in captured


def _parse_vt(words, lineno, raw=""):
    try:
        nums = [float(w) for w in words[1:]]
    except ValueError:
        raise ValueError("line %d: VT has a non-numeric field: %r"
                         % (lineno, " ".join(words)))
    # keep the exact white space of the original row: the stored text is what
    # the conversion re-parses
    text = raw[len(words[0]):] if raw else " " + " ".join(words[1:])
    if len(nums) == 8:
        return VtRow(nums[0], nums[1], nums[2], nums[3], nums[4], nums[5],
                     nums[6], nums[7], True, text)
    if len(nums) == 5:
        # OBJ7-style row: position + texture coordinate, no normal.
        return VtRow(nums[0], nums[1], nums[2], 0.0, 0.0, 0.0,
                     nums[3], nums[4], False, text)
    raise ValueError("line %d: VT must have 8 numbers (or 5 without a normal), "
                     "got %d" % (lineno, len(nums)))


GLOBAL_KEYWORDS = {
    "TEXTURE", "TEXTURE_LIT", "TEXTURE_NORMAL", "TEXTURE_MAP", "TEXTURE_DRAPED",
    "TEXTURE_DRAPED_LIT", "TEXTURE_DRAPED_NORMAL", "POINT_COUNTS",
    "GLOBAL_cockpit_lit", "GLOBAL_specular", "GLOBAL_no_blend",
    "GLOBAL_shadow_blend", "GLOBAL_no_shadow", "GLOBAL_tint", "GLOBAL_luminance",
    "GLOBAL_luminence", "NORMAL_METALNESS", "BLEND_GLASS", "COCKPIT_REGION",
    "slung_load_weight", "ATTR_layer_group", "ATTR_layer_group_draped",
    "ATTR_LOD_draped", "SLOPE_LIMIT", "TILTED", "REQUIRE_WET", "REQUIRE_DRY",
    "PARTICLE_SYSTEM", "RAIN_scale", "RAIN_friction", "THERMAL_texture",
    "THERMAL_source", "THERMAL_source2", "WIPER_texture", "WIPER_param",
    "LOAD_CENTER", "TEXTURE_NOWRAP", "TEXTURE_LIT_NOWRAP",
    "TEXTURE_NORMAL_NOWRAP", "NO_BLEND", "TWO_SIDED", "SPECULAR",
    "BUMP_LEVEL", "NO_SHADOW",
}


def _is_global(kw):
    return kw in GLOBAL_KEYWORDS or kw.startswith("GLOBAL_")


def _snapshot(state, order):
    """The state a batch draws under, in write order.

    Every category the parser recorded has to survive here: a repeatable one
    (``ATTR_axis_detent_range``, ``ATTR_manip_keyframe``) is appended to
    ``state`` but never to ``state_order``, and dropping it from the snapshot
    made the rows invisible to the conversion - the S-76C's manipulator
    keyframes and every detent table would vanish between parse and export.
    """
    out = []
    for cat in STATE_ORDER:
        if cat in state:
            out.append([cat, list(state[cat])])
    seen = set(c for c, _ in out)
    # categories not in STATE_ORDER keep their insertion order, in `order`
    # first (that is where the parser records them) and then whatever else the
    # state still holds
    for cat in list(order) + [c for c in state if c not in order]:
        if cat not in seen and cat in state:
            out.append([cat, list(state[cat])])
            seen.add(cat)
    return out


# ---------------------------------------------------------------------------
# geometry helper: winding
# ---------------------------------------------------------------------------

def flip_winding(indices):
    """Swap the last two vertices of every triangle.

    X-Plane: front face is CLOCKWISE in file order.  Blender/OpenGL convention
    treats COUNTER-clockwise as front.  Both directions of the trip need this
    swap, otherwise every imported model is inside-out.
    """
    out = list(indices)
    for i in range(0, len(out) - 2, 3):
        out[i + 1], out[i + 2] = out[i + 2], out[i + 1]
    return out
