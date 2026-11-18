"""Where an .acf places each attached OBJ: `_obja/<slot>/_v10_att_*_acf_prt_ref`.

X-Plane does **not** draw an attached object at the OBJ's own origin. The .acf
carries, per slot, a reference-point offset (feet) and rotation (degrees), and
the sim adds it before drawing:

    P _obja/1/_v10_att_x_acf_prt_ref    0.0
    P _obja/1/_v10_att_y_acf_prt_ref   -4.4
    P _obja/1/_v10_att_z_acf_prt_ref   14.6
    P _obja/1/_v10_att_the_ref          0.0   # pitch, degrees
    P _obja/1/_v10_att_psi_ref          0.0   # heading
    P _obja/1/_v10_att_phi_ref          0.0   # roll

So an aircraft that uses this (Lancair Evolution: 14.6 ft aft, 4.4 ft down, on
every one of its 14 objects) has its OBJ geometry sitting a few metres away from
where the sim actually draws it - and away from where the .snd's sound spaces
are, because those are CG/authoring-point based and never see this offset.

This module reads that table and shifts an imported file's header Empty by it.
The header carries everything the file built (meshes, rigs, armatures), so one
transform per file is enough.

**Why it is opt-in, and why exporting needs care**: XPlane2Blender bakes object
transforms into the OBJ8 it writes. If the scene is shifted here and the user
then exports the objects, they come out pre-shifted while the .acf keeps its
offset -> X-Plane applies it twice. So the offset is a *display* aid: applied on
request, tagged on the object, and removable again with `clear_header()`.
"""

import os
import re

import bpy
from mathutils import Matrix

FEET = 0.3048
SECTION = "_obja"
FIELDS_XYZ = ("_v10_att_x_acf_prt_ref", "_v10_att_y_acf_prt_ref",
              "_v10_att_z_acf_prt_ref")
FIELDS_ROT = ("_v10_att_the_ref", "_v10_att_psi_ref", "_v10_att_phi_ref")
FILE_FIELD = "_v10_att_file_stl"

# what we applied, kept on the object so it can be taken off again (feet, degrees)
PROP = "acf_prt_ref"
PROP_SOURCE = "acf_prt_ref_from"
PROP_ON = "acf_placement_on"

_LINE = re.compile(r"^\s*(?:P\s+)?_obja/(\d+)/(\S+)\s+(.+?)\s*$")


def _num(raw, default=0.0):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def placements(text):
    """{slot: {'file': name, 'xyz': (x, y, z) feet, 'rot': (the, psi, phi) deg}}."""
    table = {}
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line or line[0] in "#;":
            continue
        hit = _LINE.match(line)
        if not hit:
            continue
        slot = int(hit.group(1))
        field = hit.group(2)
        value = hit.group(3).strip().strip('"').strip("'")
        entry = table.setdefault(slot, {"file": None, "xyz": [0.0, 0.0, 0.0],
                                        "rot": [0.0, 0.0, 0.0]})
        if field == FILE_FIELD:
            entry["file"] = value
        elif field in FIELDS_XYZ:
            entry["xyz"][FIELDS_XYZ.index(field)] = _num(value)
        elif field in FIELDS_ROT:
            entry["rot"][FIELDS_ROT.index(field)] = _num(value)
    return table


def read_placements(acf_path):
    """Same as `placements()` but for a file. Returns {} when it cannot be read."""
    try:
        with open(acf_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    if b"\x00" in raw[:1024]:            # X-Plane 9 binary format, not ours
        return {}
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return placements(text)


def find_acf(obj_path):
    """The .acf that most likely attaches this .obj: same folder, then upwards."""
    folder = os.path.dirname(os.path.abspath(obj_path))
    for _ in range(3):
        try:
            names = sorted(n for n in os.listdir(folder)
                           if n.lower().endswith(".acf"))
        except OSError:
            names = []
        if names:
            return os.path.join(folder, names[0])
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None


def for_object(obj_path, acf_path=None):
    """The offset this .obj is drawn with, or None when there is none.

    Matching is by file name only - the .acf stores a path relative to the
    aircraft (`objects/x.obj`, or `../x.obj` for one folder up) and the caller
    has the resolved absolute path.
    """
    acf_path = acf_path or find_acf(obj_path)
    if not acf_path:
        return None
    want = os.path.basename(obj_path).lower()
    for slot in sorted(read_placements(acf_path).values(), key=lambda e: e["file"] or ""):
        name = slot.get("file")
        if not name:
            continue
        if os.path.basename(name.replace("\\", "/")).lower() != want:
            continue
        xyz = tuple(slot["xyz"])
        rot = tuple(slot["rot"])
        if not any(xyz) and not any(rot):
            return None
        return {"xyz": xyz, "rot": rot, "acf": os.path.basename(acf_path),
                "slot_file": name}
    return None


def _axis_matrix():
    """X-Plane (x, y, z) -> Blender (x, -z, y), as a rotation."""
    return Matrix(((1.0, 0.0, 0.0, 0.0),
                   (0.0, 0.0, -1.0, 0.0),
                   (0.0, 1.0, 0.0, 0.0),
                   (0.0, 0.0, 0.0, 1.0)))


def offset_matrix(spec):
    """Feet/degrees -> a Blender matrix (translation + rotation, X-Plane order)."""
    from math import radians
    x, y, z = (v * FEET for v in spec["xyz"])
    the, psi, phi = (radians(v) for v in spec["rot"])
    # X-Plane applies pitch (THE, about +X), then heading (PSI, about +Y),
    # then roll (PHI, about +Z) - all in aircraft axes, so build it there and
    # conjugate into Blender space.
    m = (Matrix.Rotation(the, 4, "X")
         @ Matrix.Rotation(psi, 4, "Y")
         @ Matrix.Rotation(phi, 4, "Z"))
    axis = _axis_matrix()
    m_bl = axis @ m @ axis.inverted()
    m_bl.translation = axis.to_3x3() @ Matrix.Translation((x, y, z)).translation
    return m_bl


def apply_header(header, spec, report=None):
    """Shift an imported file's header Empty to where X-Plane draws that object."""
    if header is None or not spec:
        return False
    if header.get(PROP):
        return False                       # already applied, do not stack
    base = header.matrix_basis.copy()
    header.matrix_basis = offset_matrix(spec) @ base
    header[PROP] = [float(v) for v in spec["xyz"]] + [float(v) for v in spec["rot"]]
    header[PROP_SOURCE] = spec.get("acf") or ""
    if report is not None and any(spec["rot"]):
        report({'WARNING'},
               "%s: rotation offset in %s is unverified (%.2f, %.2f, %.2f deg)"
               % (header.name, spec.get("acf", "the .acf"), *spec["rot"]))
    return True


def clear_header(header):
    """Take the offset back off (restores the geometry as the OBJ authored it)."""
    if header is None or not header.get(PROP):
        return False
    values = list(header[PROP])
    spec = {"xyz": values[:3], "rot": values[3:6], "acf": header.get(PROP_SOURCE, "")}
    header.matrix_basis = offset_matrix(spec).inverted() @ header.matrix_basis
    del header[PROP]
    if PROP_SOURCE in header:
        del header[PROP_SOURCE]
    return True


def aircraft_placement(acf_path):
    """The one offset that applies to the whole aircraft, or None.

    Every entry in `_obja` carries its own `_acf_prt_ref`, but in the aircraft
    we could measure (Lancair Evolution, RV-10) they are identical, because the
    reference point belongs to the aircraft, not to the object.  Return that
    shared offset; when the entries disagree, say so instead of quietly picking
    one - a per-file answer is then the only honest one.
    """
    try:
        table = read_placements(acf_path)
    except (OSError, ValueError):
        return None
    if not table:
        return None
    specs = list(table.values())
    base = specs[0]
    for spec in specs[1:]:
        if any(abs(spec["xyz"][i] - base["xyz"][i]) > 1e-3 for i in range(3)) or \
                any(abs(spec["rot"][i] - base["rot"][i]) > 1e-3 for i in range(3)):
            return None
    if not any(abs(v) > 1e-6 for v in base["xyz"] + base["rot"]):
        return None
    return base
