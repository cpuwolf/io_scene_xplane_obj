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
        return "manip"
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
        words = _split_line(stripped)
        kw = words[0]

        if kw == "VT":
            # the tail keeps trailing spaces: some exporters pad VT rows to a
            # fixed width and an untouched file must survive unchanged
            obj.vt.append(_parse_vt(words, lineno, line.lstrip()))
            seen_data = True
            continue
        if kw in ("IDX", "IDX10"):
            if kw == "IDX10" and len(words) > 2 and not idx_sep_found(captured):
                obj.idx_sep = _seps(line.lstrip())
                captured.append("IDX10")
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
            if cat == "detent_range":
                state.setdefault(cat, []).append(stripped)
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

    # POINT_COUNTS is mandatory; the writer always emits one, so a missing
    # record is only worth a warning.
    if obj.global_value("POINT_COUNTS") is None:
        obj.warnings.append("missing POINT_COUNTS; counts were inferred")
    return obj


_seps_re = re.compile(r"(\S+)(\s+)(\S+)(\s+)(\S+)")


def _seps(line):
    """(keyword separator, number separator) of a three-token record."""
    m = _seps_re.match(line)
    if not m:
        return ("\t", " ")
    return (m.group(2), m.group(4))


def idx_sep_found(captured):
    return "IDX10" in captured


def _parse_vt(words, lineno, raw=""):
    try:
        nums = [float(w) for w in words[1:]]
    except ValueError:
        raise ValueError("line %d: VT has a non-numeric field: %r"
                         % (lineno, " ".join(words)))
    # keep the exact white space of the original row, so an untouched file is
    # written back byte for byte
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
    out = []
    for cat in STATE_ORDER:
        if cat in state:
            out.append([cat, list(state[cat])])
    seen = set(c for c, _ in out)
    # categories not in STATE_ORDER keep their insertion order
    for cat in order:
        if cat not in seen and cat != "detent_range":
            out.append([cat, list(state[cat])])
            seen.add(cat)
    if "detent_range" in state:
        out.append(["detent_range", list(state["detent_range"])])
    return out


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def fmt_num(v):
    """Format a float the way X-Plane files do: short, no exponent, no -0."""
    if v != v or v in (float("inf"), float("-inf")):
        v = 0.0
    if abs(v) < 5e-7:
        return "0"
    s = "%.6f" % v
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        s = "0"
    return s


def pack_indices(values, per_line=10, seps=("\t", " ")):
    """Yield IDX10 rows of `per_line` values, then single IDX rows."""
    sep_kw, sep_num = seps
    n = len(values)
    full = n - (n % per_line)
    for i in range(0, full, per_line):
        # match the layout X-Plane's own exporter uses: tab after the keyword,
        # spaces between the numbers
        yield "IDX10" + sep_kw + sep_num.join(str(v)
                                             for v in values[i:i + per_line])
    for i in range(full, n):
        yield "IDX" + sep_kw + "%d" % values[i]


def replace_counts(line, counts):
    """Rewrite a POINT_COUNTS line, keeping the original white space."""
    parts = re.split(r"(\s+)", line)
    slots = [i for i, p in enumerate(parts) if p and not p.isspace()]
    if len(slots) < 5:
        return "POINT_COUNTS\t%d\t%d\t%d\t%d" % tuple(counts)
    for k, slot in enumerate(slots[1:5]):
        parts[slot] = str(counts[k])
    return "".join(parts)


def emit(obj, vt_text=None, idx_values=None, leaf_spans=None,
         leaf_states=None, extra_globals=None, header_comment=None):
    """Serialise an Obj8 back to text.

    The defaults reproduce the file this object was parsed from (same VT text,
    same index values, same state per leaf).  Pass the overrides to write
    regenerated geometry:

        vt_text      list of VT row bodies ("x y z nx ny nz s t")
        idx_values   the whole new index table
        leaf_spans   {leaf_index: (offset, count)} into idx_values
        leaf_states  {leaf_index: state-list} replacing the parsed snapshot
    """
    nl = obj.line_ending
    out = []
    if header_comment is None:
        header_comment = ""
    out.append(obj.header_flag)
    out.append(obj.version_line + ("" if not header_comment else
                                   "\t" + header_comment))
    out.append("OBJ")
    out.append("")

    if vt_text is None:
        vt_text = [r.text for r in obj.vt]
    if idx_values is None:
        idx_values = obj.idx

    n_lines = len(obj.vline)
    n_lights = len(obj.vlight)
    # POINT_COUNTS describes the tables, and must agree with what we write.
    counts = (len(vt_text), n_lines, n_lights, len(idx_values))

    globals_out = []
    wrote_counts = False
    for line in obj.globals:
        w = line.split()
        if w and w[0] == "POINT_COUNTS":
            globals_out.append(replace_counts(line, counts))
            wrote_counts = True
        else:
            globals_out.append(line)
    if extra_globals:
        globals_out.extend(extra_globals)
    if not wrote_counts:
        globals_out.append("POINT_COUNTS\t%d\t%d\t%d\t%d" % counts)
    if not globals_out:
        globals_out.append("TEXTURE")
    out.extend(globals_out)
    out.append("")

    for body in vt_text:
        # a re-emitted source row keeps its own padding after the keyword
        if body[:1] in (" ", "\t"):
            out.append("VT" + body)
        else:
            out.append("VT\t" + body)
    for line in obj.vline:
        out.append(line)
    for line in obj.vlight:
        out.append(line)
    for line in pack_indices(idx_values, seps=obj.idx_sep):
        out.append(line)
    out.append("")

    # ---- command stream ------------------------------------------------
    depth = 0
    last_state = {}
    for kind, payload in obj.events:
        if kind == EV_ANIM_BEGIN:
            out.append("\t" * depth + "ANIM_begin")
            depth += 1
            continue
        if kind == EV_ANIM_END:
            depth = max(0, depth - 1)
            out.append("\t" * depth + "ANIM_end")
            continue
        if kind == EV_RAW:
            if payload is None:
                continue
            out.append("\t" * depth + payload)
            continue
        # geometry leaf
        leaf = obj.leaves[payload]
        state = leaf.state
        if leaf_states and payload in leaf_states:
            state = leaf_states[payload]
        # emit only the attributes that differ from what is already in effect
        pending = []
        for cat, raws in state:
            want = tuple(raws)
            if last_state.get(cat) != want:
                pending.extend(raws)
                last_state[cat] = want
        for cat in list(last_state):
            if cat not in dict((c, 0) for c, _ in state):
                last_state.pop(cat, None)   # state disappeared: needs a reset
        span = None
        if leaf_spans is not None:
            span = leaf_spans.get(payload)
        if span is None:
            # unchanged geometry keeps its original offset into the index table
            span = (leaf.offset, leaf.count)
        for line in pending:
            out.append("\t" * depth + line)
        ckw, cnum = obj.cmd_sep
        out.append("\t" * depth + leaf.kind + ckw + "%d" % span[0]
                   + cnum + "%d" % span[1])

    while depth > 0:
        depth -= 1
        out.append("\t" * depth + "ANIM_end")

    return nl.join(out) + nl


def write(obj, path, **kw):
    text = emit(obj, **kw)
    with open(path, "wb") as f:
        f.write(text.encode("latin-1", "replace"))
    return text


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
