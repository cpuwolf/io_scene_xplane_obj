"""The aero airframe: Plane Maker's bodies **and** wings.

X-Plane does not fly a picture - it flies the shapes on Plane Maker's Bodies and
Wings screens, and both are stored in the .acf as point grids::

    P _body/<n>/_geo_xyz/<i>,<j>,<k>   feet
    P _wing/<n>/_geo_xyz/<i>,<j>,<k>   feet
    <prefix>/<n>/_r_dim                 points around one cross-section
    <prefix>/<n>/_s_dim                 sections used

`i` walks along the body / across the span, `j` around the section, `k` = 0/1/2
is x / y / z.  Airplane axes: **x right, y up, z aft**, in feet.  The array is
always 20 x 18 slots and only the first `_s_dim` sections and `_r_dim` points are
live: **the rest is stale**.  Read all of it and a Lancair's side area comes out
18.50 m2 instead of the 7.81 m2 the sim prints (Cycle Dump.txt), because the dead
slots still hold old coordinates.

The two grids differ in one way that matters:

* a **body** is stored in its own frame - add `_part_x/_y/_z` and the
  pitch/heading/roll to place it (a Cessna's spinner sits at z -4.94 ft, which is
  its grid's -1.22 plus the part's -3.72).
* a **wing** is stored **already in the aircraft frame**, with dihedral, sweep and
  incidence baked in, and left and right are separate slots (Cessna: slot 0 spans
  x -8.60..0, slot 1 spans 0..8.60).  Adding `_part_*` to it would double the root
  offset and blow the span up to 39 ft instead of the real 36.

Both are what the flight model measures: front/side/top area, wetted area (Lancair
Body 1: 23.959 m2 here, 23.958 in the sim's own dump), and the wing planform area
(Cessna wing 1 chord 5.20 ft; Lancair's four right-hand wing panels sum to 6.43 m2
against the 6.46 the dump reports for the joined wing).
"""

import math
import re

import bmesh
import bpy

FEET = 0.3048

#: custom property on every object this module makes: what aero part it is.  Also
#: the one thing `_collection_roots` skips - a part must not be moved by the .acf
#: placement offset, which belongs to attached .obj files
PROP = "acf_aero"

#: "body" or "wing"
PROP_KIND = "acf_aero_kind"

#: outliner colour: the cockpit is green, the wreckage orange, the VR layer blue
AERO_TAG = "COLOR_06"

#: the collection the geometry lands in, one per aircraft
GROUP = "Aero"

#: what the flight model measures, so the numbers live in the outliner too
PROP_CD = "acf_aero_cd"
PROP_AREA_RULE = "acf_aero_area_rule"
PROP_SECTIONS = "acf_aero_sections"
PROP_POINTS = "acf_aero_points"

#: what these grids are, told on the collection itself.  A wing's grid is the
#: shape Plane Maker last stored: editing a wing parameter does not rewrite it,
#: and the sim flies the parameters, so the mesh is not evidence of the wing the
#: aircraft flies (docs/aero.md, "A wing grid is the stored shape").  A body has
#: no parameters to fall back on - its grid *is* the aerodynamic data.
PROP_SOURCE = "acf_aero_source"
SOURCE_NOTE = (
    "Wing grids are stored display geometry: Plane Maker does not rewrite them "
    "when a wing parameter changes, and the sim flies _Croot/_Ctip/"
    "_sweep_design/_dihed_design plus the per-rib tables (_chord_for_RIB, "
    "_incidence), not this mesh.  Body grids are the aerodynamic data itself "
    "(_part_* plus the sections).  See docs/aero.md.")

#: how far a gear-attached body's stored position may drift from the wheel its
#: gear defines before the gear is believed instead (feet).  Measured drift on
#: the stock fleet: Cessna 0.19 ft (fine), Van's 0.74, Cirrus 1.44, R22 4.50
#: (`_part_*` left at 0,0,0) - Plane Maker does not rewrite these when the gear
#: changes, so an aircraft whose gear was edited later carries a stale position.
GEAR_SLOP = 0.5

#: set on a body whose position came from its gear rather than the file
PROP_GEAR = "acf_aero_gear"

#: Plane Maker's names for the wing slots: 0/1..6/7 are the four wing sections
#: (left, right), 8/9 the horizontal stabiliser, 10/11 the two vertical ones
def _wing_name(slot, right):
    if slot < 8:
        return "Wing %d %s" % (slot // 2 + 1, "R" if right else "L")
    if slot < 10:
        return "H-Stab 1 %s" % ("R" if right else "L")
    return "V-Stab %d" % (slot - 9)


def _int(raw):
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _float(raw, default):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _grids(text, prefix):
    """{slot: {"points": [[(x,y,z) per point] per section], "r", "s", "scal"}}."""
    geo = {}
    dims = {}
    scal = {}
    tag = "P %s/" % prefix
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(tag):
            continue
        if "/_geo_xyz/" in line:
            head, _, tail = line.partition("/_geo_xyz/")
            words = tail.split(None, 1)
            if len(words) < 2:
                continue
            idx = words[0].split(",")
            slot = head[len(tag):]
            if len(idx) != 3 or not slot.isdigit():
                continue
            try:
                i, j, k = (int(v) for v in idx)
                value = float(words[1])
            except ValueError:
                continue
            geo.setdefault(int(slot), {}).setdefault((i, j), {})[k] = value
            continue
        words = line.split(None, 2)
        if len(words) < 3:
            continue
        parts = words[1].split("/")
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        slot, field, value = int(parts[1]), parts[2], words[2]
        if field == "_r_dim":
            dims.setdefault(slot, {})["r"] = _int(value)
        elif field == "_s_dim":
            dims.setdefault(slot, {})["s"] = _int(value)
        else:
            scal.setdefault(slot, {})[field] = value

    out = {}
    for slot in sorted(geo):
        dim = dims.get(slot) or {}
        r, s = dim.get("r"), dim.get("s")
        if not r or not s or r < 3 or s < 2:
            continue
        points = []
        for i in range(s):
            ring = []
            for j in range(r):
                p = geo[slot].get((i, j))
                if p is None or len(p) < 3:
                    ring = []
                    break
                ring.append((p[0], p[1], p[2]))
            if not ring:
                points = []
                break
            points.append(ring)
        if points:
            out[slot] = {"points": points, "r": r, "s": s,
                         "scal": scal.get(slot, {})}
    return out


def _read_text(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if b"\x00" in raw[:1024]:            # the old binary .acf, not ours
        return None
    return raw.decode("utf-8", "replace")


def _matrix(rot):
    """The body's heading / pitch / roll as one 3x3, in X-Plane airplane axes.

    Plane Maker's own table: a positive heading pivots the body to starboard, a
    positive pitch pivots it up, a positive roll rolls it to starboard.  In
    (x right, y up, z aft) that is R_y(-psi) * R_x(theta) * R_z(-phi) - the two
    minus signs are what makes a positive value turn/roll to the right instead
    of to the left.
    """
    the, psi, phi = (math.radians(v) for v in rot)
    ct, st = math.cos(the), math.sin(the)
    cp, sp = math.cos(-psi), math.sin(-psi)
    cr, sr = math.cos(-phi), math.sin(-phi)
    my = ((cp, 0.0, sp), (0.0, 1.0, 0.0), (-sp, 0.0, cp))
    mx = ((1.0, 0.0, 0.0), (0.0, ct, -st), (0.0, st, ct))
    mz = ((cr, -sr, 0.0), (sr, cr, 0.0), (0.0, 0.0, 1.0))
    return _mul(my, _mul(mx, mz))


def _mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3))
                       for j in range(3)) for i in range(3))


def _place(point, offset, matrix):
    """A body-local point (feet) in aircraft axes (feet)."""
    x, y, z = point
    ox, oy, oz = offset
    return (ox + matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z,
            oy + matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z,
            oz + matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z)


def _to_blender(p):
    """X-Plane airplane axes (feet) -> Blender metres: (x, -z, y) * FEET."""
    return (p[0] * FEET, -p[2] * FEET, p[1] * FEET)


def gear_wheels(text):
    """{gear slot: wheel centre in aircraft feet}.

    A leg hangs from `_gear_x/_y/_z` and extends `_leg_len` feet down, `_latE`
    degrees sideways and `_lonE` degrees fore/aft, so

        wheel = attach + leg * ( sin(latE),
                                -cos(latE) * cos(lonE),
                                -cos(latE) * sin(lonE) )

    Checked against the tyre geometry of four imported models: the nearest tyre
    island lands 0.08 ft (Cirrus), 0.20 (Cessna), 0.43 (Van's) and 0.35 (R22)
    from this point, so it is the wheel to within the model's own slack.
    """
    raw = {}
    for line in text.splitlines():
        m = re.match(r"^P _gear/(\d+)/(_gear_[xyz]|_leg_len|_latE|_lonE)\s+(\S+)$",
                     line.strip())
        if m:
            raw.setdefault(int(m.group(1)), {})[m.group(2)] = _float(m.group(3), 0.0)
    out = {}
    for slot, g in raw.items():
        leg = g.get("_leg_len", 0.0)
        lat = math.radians(g.get("_latE", 0.0))
        lon = math.radians(g.get("_lonE", 0.0))
        out[slot] = (g.get("_gear_x", 0.0) + leg * math.sin(lat),
                     g.get("_gear_y", 0.0) - leg * math.cos(lat) * math.cos(lon),
                     g.get("_gear_z", 0.0) - leg * math.cos(lat) * math.sin(lon))
    return out


def bodies(path):
    """Every Plane Maker body in the .acf, already placed in the aircraft frame."""
    text = _read_text(path)
    if text is None:
        return []
    wheels = gear_wheels(text)
    out = []
    for slot, grid in _grids(text, "_body").items():
        scal = grid["scal"]
        offset = tuple(_float(scal.get(k), 0.0)
                       for k in ("_part_x", "_part_y", "_part_z"))
        matrix = _matrix(tuple(_float(scal.get(k), 0.0)
                               for k in ("_part_the", "_part_psi", "_part_phi")))
        # a body bolted to a gear leg sits where the gear puts it.  Plane Maker
        # leaves `_part_*` behind when the gear is edited, and the sim/Plane
        # Maker recompute the wheel from the gear, so a stored position that
        # disagrees by more than GEAR_SLOP is stale - take the gear's wheel.
        fix = None
        gear = _int(scal.get("_gear_for_body"))
        if gear is not None and gear >= 0 and gear in wheels:
            wheel = wheels[gear]
            if max(abs(wheel[i] - offset[i]) for i in range(3)) > GEAR_SLOP:
                fix = {"gear": gear, "stored": offset, "wheel": wheel}
                offset = wheel
        points = [[_place(p, offset, matrix) for p in ring]
                  for ring in grid["points"]]
        out.append({
            "kind": "body",
            "slot": slot,
            "name": "Body %d %s" % (slot + 1, scal.get("_descrip", "").strip()
                                    or "untitled"),
            "points": points,
            "r": grid["r"],
            "s": grid["s"],
            "cd": _float(scal.get("_part_cd"), None),
            "area_rule": _float(scal.get("_part_area_rule"), None),
            "gear_fix": fix,
        })
    return out


def wings(path):
    """Every wing / stabiliser in the .acf.

    No placement is applied: the grid already is the aircraft frame, with
    dihedral, sweep and incidence baked in, and each side has its own slot.
    """
    text = _read_text(path)
    if text is None:
        return []
    out = []
    for slot, grid in _grids(text, "_wing").items():
        scal = grid["scal"]
        right = _float(scal.get("_is_right_mult"), 1.0) > 0
        if slot < 10 and "_is_right_mult" not in scal:
            right = slot % 2 == 1
        name = _wing_name(slot, right)
        if slot >= 10:
            name = "V-Stab %d" % (slot - 9)
        out.append({
            "kind": "wing",
            "slot": slot,
            "name": name,
            "points": grid["points"],
            "r": grid["r"],
            "s": grid["s"],
            "cd": None,
            "area_rule": None,
            "semi": _float(scal.get("_semilen_SEG"), None),
            "chord": _float(scal.get("_Croot"), None),
        })
    return out


def _collection(aircraft):
    coll = bpy.data.collections.new(GROUP)
    coll.color_tag = AERO_TAG
    coll[PROP_SOURCE] = SOURCE_NOTE
    aircraft.children.link(coll)
    return coll


def _material():
    mat = bpy.data.materials.get("Aero body")
    if mat is not None:
        return mat
    mat = bpy.data.materials.new("Aero body")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (0.55, 0.35, 0.85, 1.0)
        bsdf.inputs["Alpha"].default_value = 0.18
        bsdf.inputs["Roughness"].default_value = 0.6
    for prop, value in (("surface_render_method", "BLENDED"),
                        ("blend_method", "BLEND"),
                        ("show_transparent_back", False)):
        if hasattr(mat, prop):
            try:
                setattr(mat, prop, value)
            except (TypeError, AttributeError):
                pass
    mat.diffuse_color = (0.55, 0.35, 0.85, 0.18)
    return mat


def _mesh(name, points, r, s, material):
    """A lofted surface: (s-1) * r quads around a closed section."""
    verts = [_to_blender(p) for ring in points for p in ring]
    faces = []
    for i in range(s - 1):
        for j in range(r):
            jn = (j + 1) % r
            faces.append((i * r + j, (i + 1) * r + j,
                          (i + 1) * r + jn, i * r + jn))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    # a body's nose and tail stations collapse to a point and a wing's section
    # closes on itself, so weld the coincident corners and drop what is left
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    me.validate()
    me.update()
    if material is not None:
        me.materials.append(material)
    return me


def build(context, aircraft, acf_path):
    """Build every body and wing under `aircraft`; None when the file has none.

    Returns {"collection", "bodies", "wings", "vertices", "faces"}.  The objects
    are plain meshes in a non-exportable collection: they are the aerodynamic
    airframe, not an .obj the sim attaches, so XPlane2Blender must not write them
    (it only writes exportable collections).
    """
    parts = bodies(acf_path) + wings(acf_path)
    if not parts:
        return None
    coll = _collection(aircraft)
    material = _material()
    verts = faces = 0
    count = {"body": 0, "wing": 0}
    geared = []
    for part in parts:
        me = _mesh(part["name"], part["points"], part["r"], part["s"], material)
        ob = bpy.data.objects.new(part["name"], me)
        ob[PROP] = "%s#%d" % (acf_path.rsplit("/", 1)[-1], part["slot"])
        ob[PROP_KIND] = part["kind"]
        fix = part.get("gear_fix")
        if fix:
            ob[PROP_GEAR] = ("gear %d: the file says (%.2f, %.2f, %.2f), the "
                             "gear's wheel is (%.2f, %.2f, %.2f) - placed on the "
                             "wheel" % ((fix["gear"],) + fix["stored"] + fix["wheel"]))
            geared.append(part["name"])
        if part["cd"] is not None:
            ob[PROP_CD] = part["cd"]
        if part["area_rule"] is not None:
            ob[PROP_AREA_RULE] = part["area_rule"]
        ob[PROP_SECTIONS] = part["s"]
        ob[PROP_POINTS] = part["r"]
        # a wireframe in Solid view (so the imported model shows through it) and
        # a translucent surface in a rendered view - one object, two faces
        ob.display_type = 'WIRE'
        ob.show_in_front = True
        ob.color = (0.55, 0.35, 0.85, 1.0)
        coll.objects.link(ob)
        count[part["kind"]] += 1
        verts += len(me.vertices)
        faces += len(me.polygons)
    return {"collection": coll.name, "bodies": count["body"],
            "wings": count["wing"], "vertices": verts, "faces": faces,
            "geared": geared}
