"""LIGHT_PARAM / LIGHT_NAMED -> a Blender light that looks like the lamp.

XPlane2Blender writes a light object as one line: the position comes from the
object and the parameter text is echoed verbatim.  None of the fields set here -
colour, power, cone, aim - is read back by the exporter, which is exactly what
makes them worth setting: an imported aircraft lights up in Blender instead of
being a grid of identical white 10 W points, and the bytes XP2B writes stay
byte-identical to the file that was imported.

The one field that *could* reach the file is the aim.  For a directional
parameter light whose object is not a POINT light, XP2B compares the artist's
direction with the DX/DY/DZ in the parameters and, when they disagree, wraps the
line in a static ANIM_rotate (xplane_light.py, `should_autocorrect_preautomatic`).
`aim()` therefore points the object along exactly the direction the parameters
already name - `vec_x_to_b(DX, DY, DZ)` under the object's local -Z - so the
correction angle is zero and no ANIM_rotate is written.  verify/lights_test.py
asserts both halves.

Where the numbers come from ("Airplane Parameterized Light Guide"):
    R G B      ratios 0..1, the tint of a spill
    INTENSITY  candela (X-Plane 12), written as `300000cd`
    SIZE       metres of spill reach (the v11 component lights), or the
               billboard's size in its own arbitrary units
    WIDTH      the cosine of half the cone angle; 1.0 is omnidirectional
    DX DY DZ   where the light points.  On a billboard the *length* encodes how
               directional it is, so only the direction is used here.

Everything below is data plus three functions; there is no XP2B import here on
purpose (the bridge keeps its coupling at the `xplane` property group and the
operator ids).  A light name that is not in the table is left untouched at
Blender's defaults and reported once per file, never guessed.
"""

import math

# --- how a lamp becomes a Blender light -------------------------------------
# The mapping is a compromise, not photometry: candela really is ~4*pi watts per
# steradian, but 300000 cd of landing light converted 1:1 turns every render
# white at Blender's default exposure.  Brightness therefore follows the square
# root of the candela, anchored at a nav light, which keeps the *order* of
# brightness (nav < strobe < taxi < landing) and stays inside 0..255.
REFERENCE_CD = 250.0        # a navigation light, the dimmest real case
REFERENCE_W = 5.0           # watts that reads as "on" for that light
WATTS_PER_METRE = 12.0      # v11 spills: SIZE is reach in metres
BILLBOARD_WATTS = 0.0       # a billboard is a glow quad, not a lamp: the
                            # paired _pm/_sp at the same position is the light
FALLBACK_WATTS = 2.0        # a lamp whose parameters carry neither number
SOFT_SIZE_PER_METRE = 0.05  # shadow softness, not the reach
DEFAULT_SOFT_SIZE = 0.05
SPOT_BLEND = 0.15
MIN_SPOT, MAX_SPOT = 1.0, 179.0

# Billboard names carry no RGB - the tint is in the texture - but the guide is
# explicit about which position light is which colour, so the fallback is
# documented behaviour rather than a guess.
_FALLBACK_COLOURS = (
    ("nav_left", (1.0, 0.0, 0.0)),
    ("nav_right", (0.05, 1.0, 0.1)),
    ("nav_tail", (1.0, 1.0, 1.0)),
    ("beacon", (1.0, 0.08, 0.03)),
    ("strobe", (1.0, 1.0, 1.0)),
)

# --- parameter signatures ----------------------------------------------------
# One entry per row of lights.txt's LIGHT_PARAM_DEF for the airplane family:
# the order IS the parameter order in the OBJ line, and the count has to match
# it exactly - XP2B reads the first `argc` tokens as the parameters and treats
# the rest as a comment, so a table that is one slice off aims the lamp at the
# wrong direction and makes XP2B write an ANIM_rotate to "correct" it.
_V12 = ("R", "G", "B", "INDEX", "INTENSITY", "DX", "DY", "DZ", "WIDTH")
_SPILL9 = ("R", "G", "B", "INDEX", "SIZE", "DX", "DY", "DZ", "WIDTH")
# The v11 spill has no direction parameter at all: its SPILL_SW overload fixes
# it at 0 0 -1, which is the direction `describe` has to report for it.
_SPILL6 = ("R", "G", "B", "INDEX", "SIZE", "WIDTH")
# core/glow/flare take three def literals (ZERO ZERO_ NEG_ONE) where the file
# still writes "0 0 -1"; they are the billboard's, not the lamp's, direction, so
# they are not read at all.
_COMPONENT = ("_D0", "_D1", "_D2", "INDEX", "SIZE")
_SIZE_INDEX = ("SIZE", "WIDTH", "INDEX")
_SIZE_MAG = ("SIZE", "DIR_MAG")
_STROBE_DIR = ("_X1", "_Y1", "_Z1", "INDEX", "SIZE", "DX", "DY", "DZ", "W2")
_STATIC = ("R", "G", "B", "INTENSITY", "DX", "DY", "DZ", "WIDTH")
_HW_SPILL = ("R", "G", "B", "A", "SIZE", "DX", "DY", "DZ", "WIDTH")
_FREQ_PHASE = ("FREQ", "PHASE")
_ROTATE_SP = ("FREQ", "PHASE", "SIZE", "WIDTH")
_FLASH = ("SIZE", "INDEX")

_FORWARD = (0.0, 0.0, -1.0)     # what a directionless spill's overload assumes
_BACKWARD = (0.0, 0.0, 1.0)     # airplane_beacon_rotate_sp's overload
_OMNI = (0.0, 0.0, 0.0)         # airplane_beacon_strobe_sp has no width: omni

SPECS = {}


def _add(names, sig, family, fixed=None):
    for name in names:
        SPECS[name] = (sig, family, fixed)


for _p in ("landing", "taxi", "spot", "generic"):
    _add(["airplane_%s_bb" % _p], _V12, "billboard")
    _add(["airplane_%s_pm" % _p], _V12, "spill")
    _add(["airplane_%s_sp" % _p], _SPILL6, "spill", _FORWARD)
    _add(["airplane_%s_core" % _p, "airplane_%s_glow" % _p,
          "airplane_%s_flare" % _p], _COMPONENT, "billboard")
    _add(["airplane_%s_size" % _p, "airplane_%s_flash" % _p],
         _SIZE_INDEX, "billboard")
_add(["airplane_generic_static"], _STATIC, "spill")
_add(["airplane_nav_pm"], _V12, "spill")
_add(["airplane_nav_bb"], _V12, "billboard")
_add(["airplane_nav_sp"], _SPILL9, "spill")
_add(["airplane_nav_left_size", "airplane_nav_right_size",
      "airplane_nav_tail_size"], _SIZE_MAG, "billboard")
_add(["airplane_strobe_pm"], _V12, "spill")
_add(["airplane_strobe_bb"], _V12, "billboard")
_add(["airplane_strobe_sp"], _SPILL9, "spill")
_add(["airplane_strobe_dir"], _STROBE_DIR, "billboard")
_add(["airplane_strobe_omni"], _COMPONENT, "billboard")
_add(["airplane_strobe_size"], _SIZE_INDEX, "billboard")
_add(["airplane_beacon_pm"], _V12, "spill")
_add(["airplane_beacon_bb"], _V12, "billboard")
_add(["airplane_beacon_rotate"], _FREQ_PHASE, "billboard")
_add(["airplane_beacon_rotate_sp"], _ROTATE_SP, "spill", _BACKWARD)
_add(["airplane_beacon_strobe"], _FLASH, "billboard")
_add(["airplane_beacon_strobe_sp"], _FLASH, "spill", _OMNI)
_add(["airplane_beacon_size"], _SIZE_INDEX, "billboard")
_add(["airplane_panel_sp", "airplane_inst_sp"], _SPILL9, "cockpit")
# scenery spills that show up in aircraft files (night logos, panel floods)
_add(["full_custom_halo", "full_custom_halo_", "full_custom_halo_night"],
     _HW_SPILL, "spill")


class Spec(object):
    """What one light line says about the lamp, in Blender terms."""

    __slots__ = ("kind", "family", "colour", "energy", "cone", "direction",
                 "radius")

    def __init__(self, kind, family, colour, energy, cone, direction, radius):
        self.kind = kind
        self.family = family
        self.colour = colour
        self.energy = energy
        self.cone = cone            # radians, None for an omnidirectional light
        self.direction = direction   # X-Plane space, None when the line is omni
        self.radius = radius

    def __repr__(self):
        return ("Spec(kind=%r, energy=%.2f, cone=%r, direction=%r)"
                % (self.kind, self.energy, self.cone, self.direction))


def _number(token):
    """A parameter as a float; `300000cd` (X-Plane 12) counts as 300000."""
    if token.endswith("cd"):
        token = token[:-2]
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


def _fallback_colour(name):
    for key, colour in _FALLBACK_COLOURS:
        if key in name:
            return colour
    return (1.0, 1.0, 1.0)


def describe(name, params):
    """One LIGHT_PARAM line -> Spec, or None when the name has no table row."""
    entry = SPECS.get(name)
    if entry is None:
        return None
    sig, family, fixed = entry
    tokens = (params or "").split()
    if len(tokens) < len(sig):
        return None
    vals = {}
    for key, token in zip(sig, tokens):
        vals[key] = _number(token)

    red, green, blue = vals.get("R"), vals.get("G"), vals.get("B")
    if (red is not None and green is not None and blue is not None
            and max(red, green, blue) > 0.0):
        colour = tuple(min(1.0, max(0.0, v)) for v in (red, green, blue))
    else:
        colour = _fallback_colour(name)

    dx, dy, dz = vals.get("DX"), vals.get("DY"), vals.get("DZ")
    direction = None
    if (dx is not None and dy is not None and dz is not None
            and (dx * dx + dy * dy + dz * dz) > 1e-12):
        direction = (dx, dy, dz)
    elif fixed is not None and (fixed[0] or fixed[1] or fixed[2]):
        # the light's overload fixes the direction in lights.txt, so the file
        # carries no vector for it - XP2B still aims it, and an object aimed
        # anywhere else makes it write an ANIM_rotate
        direction = fixed

    candela, size = vals.get("INTENSITY"), vals.get("SIZE")
    if family == "billboard":
        energy = BILLBOARD_WATTS
    elif candela is not None and candela > 0.0:
        energy = REFERENCE_W * math.sqrt(candela / REFERENCE_CD)
    elif size is not None and size > 0.0:
        energy = WATTS_PER_METRE * size
    else:
        energy = FALLBACK_WATTS

    width = vals.get("WIDTH")
    cone = None
    if (family != "billboard" and direction is not None and width is not None
            and 0.0 < width < 1.0):
        cone = math.radians(min(MAX_SPOT, max(MIN_SPOT,
                                              math.degrees(2.0 * math.acos(width)))))

    radius = (SOFT_SIZE_PER_METRE * size) if size and size > 0.0 \
        else DEFAULT_SOFT_SIZE
    return Spec('SPOT' if cone is not None else 'POINT', family, colour,
                energy, cone, direction, radius)


def apply(data, spec):
    """Write the Spec onto a light data-block (never onto `xplane.params`)."""
    data.color = spec.colour
    data.energy = spec.energy
    if spec.radius:
        data.shadow_soft_size = spec.radius
    if spec.cone is not None:
        data.spot_size = spec.cone
        data.spot_blend = SPOT_BLEND
    return data


def aim(ob, direction, world=None):
    """Point the object's local -Z along the line's own direction.

    The parent (a bone inside an ANIM block, or the world-center Empty) is
    already in place, so the rotation is applied as a world matrix and Blender
    works out the local one - which is the same space XP2B's autocorrect reads.
    `world` is the object's matrix *before* any of this, passed in by the caller
    because assigning matrix_world invalidates the depsgraph and a re-read
    inside the same batch is stale.
    """
    from mathutils import Matrix, Vector

    target = Vector((direction[0], -direction[2], direction[1]))  # vec_x_to_b
    if target.length < 1e-9:
        return False
    quat = target.normalized().to_track_quat('-Z', 'Y')
    where = (world if world is not None else ob.matrix_world).translation.copy()
    ob.matrix_world = Matrix.Translation(where) @ quat.to_matrix().to_4x4()
    return True
