"""The aircraft's aero bodies: the 3-D fuselage the flight model measures.

X-Plane does not fly a picture - it flies the geometry Plane Maker draws on the
**Bodies** screen, which is stored in the .acf as a point grid::

    P _body/<n>/_geo_xyz/<i>,<j>,<k>   <feet>
    P _body/<n>/_r_dim <ring points around one section>
    P _body/<n>/_s_dim <stations used>
    P _body/<n>/_part_x/_y/_z        front tip of the body (feet)
    P _body/<n>/_part_the/_psi/_phi  pitch / heading / roll (degrees)
    P _body/<n>/_part_cd             coefficient of drag, on the frontal area
    P _body/<n>/_part_area_rule      area rule ratio

The array is always 20 x 18 slots; only the first `_s_dim` stations and the first
`_r_dim` ring points are live, and **the rest is stale junk** - read the whole
array and a Lancair's side area comes out 18.50 m2 instead of the 7.81 m2 the sim
itself prints (Cycle Dump.txt), because the dead slots still hold old coordinates.

The grid is exactly what the flight model measures: frontal area = the largest
section, side/top = half the sum of the quads projected on (z,y)/(z,x), wetted =
the sum of the triangle areas.  All three bodies of the Lancair Evolution
reproduce the numbers in Cycle Dump.txt to four decimals, which is why importing
this geometry is worth doing: it is the aircraft's actual aerodynamic shape, not
an approximation of it.

Coordinates are X-Plane airplane axes in feet (x right / y up / z aft), so the
mesh is built in metres and mapped to Blender (x, -z, y) - the same turn the OBJ8
importer makes, which is what lets these sit on top of the imported model.
"""

import math

import bmesh
import bpy

FEET = 0.3048

#: custom property on every object this module makes: what a body is, and the
#: one thing `_collection_roots` skips (a body must not be moved by the `.acf`
#: placement offset - its coordinates are already in the aircraft frame)
PROP = "acf_body"

#: outliner colour: the cockpit is green, the wreckage orange, the VR layer blue
BODY_TAG = "COLOR_06"

#: the collection the bodies land in, one per aircraft
GROUP = "Bodies"

#: what the flight model measures, so the numbers live in the outliner too
PROP_CD = "acf_body_cd"
PROP_AREA_RULE = "acf_body_area_rule"
PROP_STATIONS = "acf_body_stations"
PROP_RING = "acf_body_ring"


def _read(path):
    """Every body the .acf carries, or [] when it has none we can read."""
    geo = {}
    dims = {}
    scal = {}
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    if b"\x00" in raw[:1024]:
        return []
    text = raw.decode("utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("P _body/") or "/_geo_xyz/" in line:
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
    # the grid keys look like `_body/<n>/_geo_xyz/<i>,<j>,<k>`, which the
    # three-part split above does not fit: parse them on their own
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("P _body/") or "/_geo_xyz/" not in line:
            continue
        head, _, tail = line.partition("/_geo_xyz/")
        words = tail.split(None, 1)
        if len(words) < 2:
            continue
        idx = words[0].split(",")
        if len(idx) != 3:
            continue
        slot = head.split("/")[1]
        if not slot.isdigit():
            continue
        try:
            i, j, k = (int(v) for v in idx)
            value = float(words[1])
        except ValueError:
            continue
        geo.setdefault(int(slot), {}).setdefault((i, j), {})[k] = value

    out = []
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
        if not points:
            continue
        sc = scal.get(slot, {})
        out.append({
            "slot": slot,
            "descrip": sc.get("_descrip", "").strip(),
            "points": points,
            "r": r,
            "s": s,
            "offset": tuple(_float(sc.get(k), 0.0)
                            for k in ("_part_x", "_part_y", "_part_z")),
            "rot": tuple(_float(sc.get(k), 0.0)
                         for k in ("_part_the", "_part_psi", "_part_phi")),
            "cd": _float(sc.get("_part_cd"), None),
            "area_rule": _float(sc.get("_part_area_rule"), None),
        })
    return out


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
    # R_y(-psi)
    my = ((cp, 0.0, sp), (0.0, 1.0, 0.0), (-sp, 0.0, cp))
    # R_x(the)
    mx = ((1.0, 0.0, 0.0), (0.0, ct, -st), (0.0, st, ct))
    # R_z(-phi)
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


def _collection(aircraft):
    coll = bpy.data.collections.new(GROUP)
    coll.color_tag = BODY_TAG
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


def _mesh(body, material):
    """One body's surface: (s-1) * r quads around a closed ring."""
    matrix = _matrix(body["rot"])
    offset = body["offset"]
    s, r = body["s"], body["r"]
    verts = [_to_blender(_place(p, offset, matrix))
             for ring in body["points"] for p in ring]
    faces = []
    for i in range(s - 1):
        for j in range(r):
            jn = (j + 1) % r
            faces.append((i * r + j, (i + 1) * r + j,
                          (i + 1) * r + jn, i * r + jn))
    name = "Body %d %s" % (body["slot"] + 1, body["descrip"] or "untitled")
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    # the nose and tail stations collapse to a point, so the first and last ring
    # of quads are degenerate: weld them and drop what is left over
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
    return name, me


def build(context, aircraft, acf_path):
    """Build every aero body of `acf_path` under `aircraft`; None when none.

    Returns {"collection", "bodies", "vertices", "faces"} for the caller's
    summary.  The objects are plain meshes in a non-exportable collection: they
    are the aerodynamic shape, not an .obj the sim attaches, so XPlane2Blender
    must not write them (it only writes exportable collections).
    """
    bodies = _read(acf_path)
    if not bodies:
        return None
    coll = _collection(aircraft)
    material = _material()
    verts = faces = 0
    for body in bodies:
        name, me = _mesh(body, material)
        ob = bpy.data.objects.new(name, me)
        ob[PROP] = "%s#%d" % (acf_path.rsplit("/", 1)[-1], body["slot"])
        if body["cd"] is not None:
            ob[PROP_CD] = body["cd"]
        if body["area_rule"] is not None:
            ob[PROP_AREA_RULE] = body["area_rule"]
        ob[PROP_STATIONS] = body["s"]
        ob[PROP_RING] = body["r"]
        # a wireframe in Solid view (so the imported model shows through it) and
        # a translucent surface in a rendered view - one object, two faces
        ob.display_type = 'WIRE'
        ob.show_in_front = True
        ob.color = (0.55, 0.35, 0.85, 1.0)
        coll.objects.link(ob)
        verts += len(me.vertices)
        faces += len(me.polygons)
    return {"collection": coll.name, "bodies": len(bodies),
            "vertices": verts, "faces": faces}
