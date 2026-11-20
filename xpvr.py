"""The VR half of an aircraft import: ``<acf>_vrconfig.txt`` as scene data.

An aircraft's VR configuration is aircraft-scoped - the file is named after the
``.acf`` and its hotspots are in *aircraft* coordinates - so the only import that
can place them is the one that knows about the aircraft frame: the ``.acf``
import.  A single ``.obj`` has no aircraft origin to measure against.

What this builds, per aircraft:

    <stem>_vrconfig.txt                 plain collection, sibling of the aircraft
    ├ Pilot's Seat                      CAMERA  (head position + view direction)
    ├ Pilot's Seat [AABB]               MESH    (the teleport grab volume)
    └ ...

and stores the file's own text plus the resolved table on the collection, so a
write-back can replace only the blocks that changed.  Saving re-reads the scene,
patches the file and refuses when the file moved on disk since the import; see
:func:`save`.

Two things are deliberately *not* inside the aircraft collection:

* the cameras and boxes, because XPlane2Blender walks ``all_objects`` from every
  exportable root and would meet a ``CAMERA`` it has no writer for;
* the whole thing, because the aircraft collection's placement toggle moves all
  of its objects and the hotspots are already in the aircraft frame.

Coordinates: ``vrconfig.hotspot_xyz`` applies the file's own origin rule (CG /
authoring point / old CG) and returns aircraft-frame metres, then
``vrconfig.to_bl`` maps X-Plane (x, y, z) to Blender (x, -z, y) - the same
mapping ``acfplace`` uses for the placed objects, so hotspots line up with the
cockpit once "Place by .acf" is on.
"""

import json
import math
import os

import bpy
from mathutils import Matrix, Vector

from . import acfcodec
from . import vrconfig

#: what the collection and its objects carry
PROP_SOURCE = "vrconfig_source"
PROP_DUMP = "vrconfig_dump"
PROP_HASH = "vrconfig_hash"
PROP_ORIGIN = "vrconfig_origin"
PROP_HOTSPOTS = "vrconfig_hotspots"
PROP_MANIPS = "vrconfig_manips"
PROP_EDITS = "vrconfig_manip_edits"
PROP_AIRCRAFT = "vr_aircraft"
PROP_HOTSPOT = "vr_hotspot"
PROP_KIND = "vr_kind"
PROP_XYZ = "vr_xyz"
PROP_ANGLES = "vr_angles"
PROP_AABB = "vr_aabb"
PROP_BLOCK = "vr_block"
PROP_OBJ_MANIPS = "vr_manips"

#: cosmetic: a lens this wide previews a seat roughly the way a VR headset sees
#: it.  Nothing about it reaches the file.
LENS = 18.0

#: Blender's own collection palette, in order, is red/orange/yellow/green/**blue**
#: /purple/pink/brown (scripts/presets/interface_theme/Blender_Light.xml,
#: `ThemeCollectionColor`), so COLOR_05 is the blue one.  Everything this module
#: creates is coloured with it - the outliner stripe on the collection, and the
#: object colour on the cameras and boxes - so the VR layer is one glance away
#: from the geometry in any outliner, and reads blue in Solid > Color > Object.
BLUE_TAG = "COLOR_05"
BLUE = (0.041, 0.242, 0.578, 1.0)      # #3887c7 in linear RGB

_CUBE_VERTS = [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
               (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
_CUBE_FACES = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
               (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]


def _collection(context, name, parent):
    coll = bpy.data.collections.new(name)
    coll.color_tag = BLUE_TAG
    if parent is not None:
        parent.children.link(coll)
    else:
        context.scene.collection.children.link(coll)
    return coll


def _box_object(coll, name, size, centre, material):
    """An axis-aligned box, drawn as a wireframe in Solid and translucent in EEVEE.

    `size` is in X-Plane order (x, y, z); the mesh is built in Blender axes, which
    swap the last two - X-Plane (x, y, z) is Blender (x, -z, y) - so a hotspot
    that is 0.6 m wide and 0.2 m tall is 0.6 x 0.6 x 0.2 in the mesh.
    """
    sx, sy, sz = size[0], size[2], size[1]
    hx, hy, hz = max(sx, 1e-4) / 2.0, max(sy, 1e-4) / 2.0, max(sz, 1e-4) / 2.0
    verts = [(x * hx, y * hy, z * hz) for x, y, z in _CUBE_VERTS]
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], _CUBE_FACES)
    me.update()
    if material is not None:
        me.materials.append(material)
    ob = bpy.data.objects.new(name, me)
    ob.location = centre
    # Solid mode ignores material alpha, so the volume is drawn as a wireframe
    # there and as a translucent box in a rendered view (one object, two faces).
    ob.display_type = 'WIRE'
    ob.show_in_front = True
    ob.color = BLUE
    coll.objects.link(ob)
    return ob


def _aabb_material():
    mat = bpy.data.materials.get("VR hotspot volume")
    if mat is not None:
        return mat
    mat = bpy.data.materials.new("VR hotspot volume")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (0.22, 0.6, 1.0, 1.0)
        bsdf.inputs["Alpha"].default_value = 0.15
    for prop, value in (("surface_render_method", "BLENDED"),
                        ("blend_method", "BLEND"),
                        ("show_transparent_back", False)):
        if hasattr(mat, prop):
            try:
                setattr(mat, prop, value)
            except (TypeError, AttributeError):
                pass
    mat.diffuse_color = (0.22, 0.6, 1.0, 0.15)
    return mat


def camera_matrix(hotspot):
    """The camera's world matrix for a hotspot: view along -Z, up +Y.

    ``PSI``/``THE``/``PHI`` are X-Plane numbers (heading about +Y, pitch about
    +X, roll about +Z); ``vrconfig.view_vectors`` turns them into a forward and
    an up vector, and both are mapped into Blender the same way every other
    X-Plane vector in this add-on is.
    """
    forward, up = vrconfig.view_vectors(hotspot.psi, hotspot.the, hotspot.phi)
    f_bl = Vector(vrconfig.to_bl(forward)).normalized()
    u_bl = Vector(vrconfig.to_bl(up))
    z_axis = -f_bl                        # a Blender camera looks down its -Z
    x_axis = u_bl.cross(z_axis)
    if x_axis.length < 1e-6:              # looking straight up or down
        x_axis = Vector((0.0, 0.0, 1.0)).cross(z_axis)
    x_axis.normalize()
    y_axis = z_axis.cross(x_axis).normalized()
    m = Matrix((x_axis, y_axis, z_axis)).transposed().to_4x4()
    return m


def _camera(coll, hotspot, position):
    data = bpy.data.cameras.new(hotspot.name or "hotspot")
    data.lens = LENS
    data.clip_start = 0.01
    ob = bpy.data.objects.new(hotspot.name or "hotspot", data)
    ob.matrix_world = (Matrix.Translation(position) @ camera_matrix(hotspot))
    ob.color = BLUE
    coll.objects.link(ob)
    ob[PROP_HOTSPOT] = hotspot.name
    ob[PROP_KIND] = hotspot.kind
    ob[PROP_XYZ] = list(hotspot.xyz or (0.0, 0.0, 0.0))
    ob[PROP_ANGLES] = [hotspot.psi, hotspot.the, hotspot.phi]
    if hotspot.aabb is not None:
        ob[PROP_AABB] = list(hotspot.aabb)
    ob[PROP_BLOCK] = "\n".join(hotspot.lines)
    return ob


def _obj_manips(aircraft_coll):
    """[(object name, raw ATTR_manip line)] over every imported batch.

    The conversion to XPlane2Blender leaves each batch's command stream on its
    collection (``xp8_leaf_states``, one state per geometry command) together
    with the object each command became (``xp8_leaf_objs``), so the OBJ side of
    the lookup is read from that rather than from materials.
    """
    from . import xpmesh as XM
    out = []

    def walk(coll):
        names = json.loads(coll.get(XM.PROP_LEAF_OBJS) or "[]")
        states = json.loads(coll.get(XM.PROP_LEAF_STATES) or "[]")
        for i, state in enumerate(states):
            source = names[i] if i < len(names) and names[i] else coll.name
            for _category, raws in state or []:
                for raw in raws:
                    if raw.strip().startswith("ATTR_manip_"):
                        out.append((source, raw))
        for child in coll.children:
            walk(child)

    walk(aircraft_coll)
    return out


def find(acf_path):
    """The vrconfig belonging to this .acf, or None (most third-party aircraft)."""
    return vrconfig.find(acf_path)


def _drop_previous(path):
    """Remove whatever a previous import of this same file left behind.

    Without this, importing the same aircraft twice stacks a second vrconfig
    collection with a second set of hotspot cameras on top of the first, and the
    two of them point at one file - saving one then refuses to write the other,
    because the file no longer matches what it was imported from.
    """
    dropped = 0
    for coll in [c for c in bpy.data.collections if c.get(PROP_SOURCE) == path]:
        if manip_edits(coll):
            print("xplane_obj: %d unsaved vrconfig edit(s) for %s discarded by "
                  "the re-import" % (len(manip_edits(coll)),
                                     os.path.basename(path)), flush=True)
        for ob in list(coll.objects):
            bpy.data.objects.remove(ob, do_unlink=True)
        owners = list(bpy.data.collections)
        scene = getattr(bpy.context, "scene", None)
        if scene is not None:
            owners.append(scene.collection)
        for owner in owners:
            if owner.children.get(coll.name) is not None:
                owner.children.unlink(coll)
        bpy.data.collections.remove(coll)
        dropped += 1
    return dropped


def build(context, aircraft_coll, acf_path, report=None):
    """Import the aircraft's vrconfig into the scene.

    Returns a summary dict, or None when the aircraft has no vrconfig (which is
    normal and silent - most third-party aircraft ship without one).
    """
    path = find(acf_path)
    if path is None:
        return None
    try:
        # an undo during a modal import frees the collections it had built
        if bpy.data.collections.get(aircraft_coll.name) is not aircraft_coll:
            return None
    except ReferenceError:
        return None
    dropped = _drop_previous(path)
    if dropped:
        print("xplane_obj: replaced %d earlier vrconfig collection(s) for %s"
              % (dropped, os.path.basename(path)), flush=True)
    vr = vrconfig.parse(path)
    cg_feet = acfcodec.read_cg(acf_path)
    origin = vr.origin(cg_feet)

    pairs = _obj_manips(aircraft_coll)
    index = vrconfig.manip_index(pairs)
    missing = vrconfig.resolve(vr, index)

    name = os.path.basename(path)
    coll = _collection(context, name, None)
    coll[PROP_SOURCE] = path
    coll[PROP_DUMP] = vr.text()
    coll[PROP_HASH] = _digest(vr.text().encode("utf-8"))
    coll[PROP_ORIGIN] = json.dumps(origin)
    coll[PROP_AIRCRAFT] = aircraft_coll.name if aircraft_coll else ""

    material = _aabb_material()
    hotspots = []
    for hotspot in vr.hotspots:
        position = Vector(vrconfig.to_bl(vr.hotspot_xyz(hotspot, origin)))
        _camera(coll, hotspot, position)
        box = None
        if hotspot.aabb is not None:
            centre = Vector(vrconfig.to_bl(_with_origin(hotspot.centre, origin)))
            box = _box_object(coll, "%s [AABB]" % (hotspot.name or "hotspot"),
                              hotspot.size, centre, material)
            box[PROP_HOTSPOT] = hotspot.name
            box[PROP_AABB] = list(hotspot.aabb)
        hotspots.append({
            "name": hotspot.name, "kind": hotspot.kind,
            "xyz": list(hotspot.xyz or ()), "aabb": list(hotspot.aabb or ()),
            "angles": [hotspot.psi, hotspot.the, hotspot.phi],
            "camera": hotspot.name, "box": box.name if box else None,
            "block": "\n".join(hotspot.lines),
        })
    coll[PROP_HOTSPOTS] = json.dumps(hotspots)

    manips = []
    for manip in vr.manips:
        manips.append({"type": manip.type, "a": manip.a, "b": manip.b,
                       "tooltip": manip.tooltip, "action": manip.action,
                       "objects": manip.sources, "block": "\n".join(manip.lines),
                       "params": manip_params(manip)})
        for object_name in manip.sources:
            ob = bpy.data.objects.get(object_name)
            if ob is None:
                continue
            tag = "%s %s" % (manip.type, manip.a or "")
            tags = list(ob.get(PROP_OBJ_MANIPS) or [])
            if tag not in tags:
                tags.append(tag)
                ob[PROP_OBJ_MANIPS] = tags
    coll[PROP_MANIPS] = json.dumps(manips)
    coll[PROP_EDITS] = "{}"

    summary = {
        "file": name, "collection": coll.name,
        "hotspots": len(vr.hotspots), "manips": len(vr.manips),
        "origin": origin, "missing": [m.key() for m in missing],
        "unknown": len(vr.unknown),
    }
    if report is not None:
        if missing:
            shown = ", ".join("%s %s" % (m.type, (m.a or "")) for m in missing[:3])
            report({'WARNING'},
                   "%s: %d of %d manipulator(s) point at geometry this aircraft "
                   "does not have (%s)" % (name, len(missing), len(vr.manips),
                                           shown))
        if vr.unknown:
            report({'WARNING'},
                   "%s: %d line(s) not understood, kept as text" % (name, len(vr.unknown)))
    print("xplane_obj: %s - %d hotspot(s), %d manipulator(s), origin %s"
          % (name, len(vr.hotspots), len(vr.manips), origin["kind"]), flush=True)
    return summary


def _with_origin(point, origin):
    ox, oy, oz = origin["offset"]
    return (point[0] + ox, point[1] + oy, point[2] + oz)


# ---------------------------------------------------------------------------
# manipulator parameters: what the panel shows and edits
# ---------------------------------------------------------------------------

def manip_params(manip):
    """The editable directives of one block, as plain data.

    ``{"DEG_PER_ACTION": [30.0], "SWITCH_THRESHOLD": [8.0, 0.05],
       "HOLD_MANIP": True, "ACTION": "ACTION axis_knob 0 360 0.1 sim/x"}`` -
    only what the block actually carries, so a panel can tell "absent" from
    "present with a value".  The block's own text stays the source of truth; this
    is its projection.
    """
    params = {}
    for keyword in vrconfig.VALUED:
        values = manip.values(keyword)
        if values:
            params[keyword] = values
    for keyword in vrconfig.FLAGS:
        if manip.has(keyword):
            params[keyword] = True
    if manip.action:
        params["ACTION"] = manip.action
    return params


def manip_edits(coll):
    """Pending edits: ``{row index: {keyword: value}}``."""
    try:
        return json.loads(coll.get(PROP_EDITS) or "{}")
    except ValueError:
        return {}


def set_manip_edits(coll, index, wanted, current):
    """Record how `wanted` differs from `current` for one block.

    `wanted` is the panel's state, `current` the block's parameters as the file
    has them.  A key that is in `current` but not in `wanted` becomes ``None``,
    which means "this directive should not be in the file" - an unticked box is
    a removal, not a no-op.  A key that ends up equal to the file is dropped from
    the table, so nothing is written and the panel's "N edited" count is honest.
    """
    diff = {}
    for keyword in set(wanted) | set(current):
        if wanted.get(keyword) != current.get(keyword):
            diff[keyword] = wanted.get(keyword)
    edits = manip_edits(coll)
    if diff:
        edits[str(int(index))] = diff
    else:
        edits.pop(str(int(index)), None)
    coll[PROP_EDITS] = json.dumps(edits)
    return edits


def clear_manip_edits(coll):
    coll[PROP_EDITS] = "{}"


def _from_origin(point, origin):
    ox, oy, oz = origin["offset"]
    return (point[0] - ox, point[1] - oy, point[2] - oz)


# ---------------------------------------------------------------------------
# saving: read the scene back, diff it against what was imported, patch the file
# ---------------------------------------------------------------------------

#: how far a value may drift before it counts as an edit, in metres/degrees.
#: float32 storage in Blender moves a number by ~1e-7, far below this.
EPS = 1e-4


def origin_of(coll):
    """The origin the import used, as stored on the collection."""
    raw = coll.get(PROP_ORIGIN)
    try:
        return json.loads(raw) if raw else {"kind": "cg", "offset": (0.0, 0.0, 0.0)}
    except ValueError:
        return {"kind": "cg", "offset": (0.0, 0.0, 0.0)}


def _camera_angles(ob):
    """PSI/THE/PHI in degrees for a hotspot camera, from its world matrix."""
    rot = ob.matrix_world.to_3x3()
    forward = vrconfig.to_xp(-(rot @ Vector((0.0, 0.0, 1.0))))
    up = vrconfig.to_xp(rot @ Vector((0.0, 1.0, 0.0)))
    return vrconfig.angles_from_vectors(forward, up)


def _close(a, b, eps=EPS):
    return all(abs(x - y) <= eps for x, y in zip(a, b))


def _box_aabb(ob):
    """(centre in Blender world, extents in X-Plane order) of the AABB box."""
    size = tuple(ob.dimensions)              # Blender (x, y, z)
    return (tuple(ob.matrix_world.translation),
            (size[0], size[2], size[1]))     # -> X-Plane (x, y, z)


def changes(coll):
    """What the scene says now, per hotspot, compared with what was imported.

    Returns ``[(name, {"xyz": [...], "angles": [...], "aabb": [...]})]`` - only
    the directives that moved.  An untouched camera contributes nothing, so an
    untouched file is not rewritten at all.
    """
    origin = origin_of(coll)
    stored = json.loads(coll.get(PROP_HOTSPOTS) or "[]")
    out = []
    for entry in stored:
        cam = bpy.data.objects.get(entry.get("camera") or "")
        if cam is None:
            continue
        diff = {}
        position = vrconfig.to_xp(tuple(cam.matrix_world.translation))
        if not _close(_from_origin(position, origin), entry.get("xyz") or ()):
            diff["PRESET_XYZ"] = list(_from_origin(position, origin))
        angles = _camera_angles(cam)
        old_angles = entry.get("angles") or (0.0, 0.0, 0.0)
        # PSI is 0-360 in the format and wraps, so a jump of 359.9 degrees is a
        # tenth of a degree of movement; compare it on the circle.
        same_psi = min(abs(angles[0] - old_angles[0]),
                       360.0 - abs(angles[0] - old_angles[0])) <= EPS
        if not (same_psi and _close(angles[1:], old_angles[1:])):
            diff["PRESET_PSI"] = angles[0]
            diff["PRESET_THE"] = angles[1]
            diff["PRESET_PHI"] = angles[2]
        box = bpy.data.objects.get(entry.get("box") or "")
        if box is not None and entry.get("aabb"):
            centre, ext = _box_aabb(box)
            centre_xp = _from_origin(vrconfig.to_xp(centre), origin)
            half = tuple(v / 2.0 for v in ext)
            # the AABB is axis-aligned in aircraft coordinates, so min/max are
            # simply the centre give or take half the extents
            want = (centre_xp[0] - half[0], centre_xp[1] - half[1],
                    centre_xp[2] - half[2],
                    centre_xp[0] + half[0], centre_xp[1] + half[1],
                    centre_xp[2] + half[2])
            if not _close(want, entry["aabb"]):
                diff["AABB"] = list(want)
        if diff:
            out.append((entry["name"], diff))
    return out


def save(context, coll, report=None):
    """Write the hotspot edits back into the aircraft's vrconfig.

    Only the directives that moved are rewritten, each in the author's own style
    (indent, separator, decimal places), and only inside the blocks that moved -
    comments, block order, a missing final newline and every untouched block come
    out byte for byte as they went in.

    Refuses to write when the file changed on disk since the import: someone else
    (the aircraft's own updater, an editor) may have touched it, and silently
    applying a stale scene over their work is the failure nobody forgives.
    """
    info = {"written": 0, "changed": [], "manips": [],
            "path": coll.get(PROP_SOURCE)}
    path = info["path"]
    if not path or not os.path.isfile(path):
        if report is not None:
            report({'ERROR'}, "vrconfig: the file is gone (%s)" % (path or "no path"))
        return info

    with open(path, "rb") as fh:
        raw = fh.read()
    now = _digest(raw)
    before = coll.get(PROP_HASH)
    if before and now != before:
        if report is not None:
            report({'ERROR'},
                   "%s changed on disk since it was imported - reload the aircraft "
                   "before saving over it" % os.path.basename(path))
        return info

    diffs = changes(coll)
    pending = manip_edits(coll)
    if not diffs and not pending:
        if report is not None:
            report({'INFO'}, "vrconfig: nothing changed, %s left alone"
                   % os.path.basename(path))
        return info

    vr = vrconfig.parse_text(raw.decode("utf-8", "replace"), path)
    by_name = {}
    for hotspot in vr.hotspots:
        by_name.setdefault(hotspot.name, hotspot)
    edits = []

    # a hotspot reads the scene; a manipulator block reads the pending table
    for row, params in sorted(pending.items(), key=lambda kv: int(kv[0])):
        index = int(row)
        if not (0 <= index < len(vr.manips)):
            if report is not None:
                report({'WARNING'},
                       "vrconfig: manipulator row %d is no longer in the file" % index)
            continue
        manip = vr.manips[index]
        lines = list(manip.lines)
        for keyword, value in params.items():
            if keyword in vrconfig.FLAGS:
                lines = vrconfig.rewrite_flag(lines, keyword, bool(value))
            elif keyword == "ACTION":
                text = (value or "").strip()
                if not text:
                    lines = vrconfig.rewrite_drop(lines, keyword)
                    continue
                else:
                    words = text.split()
                    if len(words) < 2 or words[0] != "ACTION":
                        # the panel hands back whatever was typed; a line that
                        # does not start with ACTION would be silently ignored
                        # by X-Plane, which looks like "my knob stopped working"
                        if report is not None:
                            report({'WARNING'},
                                   "vrconfig: %r is not an ACTION line - %s %s "
                                   "left as it is" % (text, manip.type,
                                                      manip.a or ""))
                        continue
                lines = vrconfig.rewrite_raw(lines, keyword, text or None)
            elif keyword in vrconfig.VALUED:
                if not value:
                    lines = vrconfig.rewrite_drop(lines, keyword)
                else:
                    lines = vrconfig.rewrite_lines(lines, keyword, value)
        if lines != list(manip.lines):
            edits.append((manip, lines))
            info["manips"].append("%s %s" % (manip.type, manip.a or ""))

    for name, diff in diffs:
        hotspot = by_name.get(name)
        if hotspot is None:
            if report is not None:
                report({'WARNING'}, "vrconfig: %r is no longer in the file" % name)
            continue
        lines = list(hotspot.lines)
        for keyword, values in diff.items():
            lines = vrconfig.rewrite_lines(lines, keyword, values)
        edits.append((hotspot, lines))
        info["changed"].append(name)
    if not edits:
        clear_manip_edits(coll)
        return info

    text = vrconfig.splice(vr, edits)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(text.encode("utf-8"))
    os.replace(tmp, path)
    info["written"] = len(edits)

    # the new file is the new baseline: a second save must not undo this one
    coll[PROP_DUMP] = text
    coll[PROP_HASH] = _digest(text.encode("utf-8"))
    _refresh_stored(coll, path)
    clear_manip_edits(coll)
    if report is not None:
        what = []
        if info["changed"]:
            what.append("%d hotspot(s): %s"
                        % (len(info["changed"]), ", ".join(info["changed"])))
        if info["manips"]:
            what.append("%d manipulator(s)" % len(info["manips"]))
        report({'INFO'}, "vrconfig: wrote %s to %s"
               % (" and ".join(what), os.path.basename(path)))
    print("xplane_obj: vrconfig %s - wrote %d block(s)"
          % (os.path.basename(path), len(edits)), flush=True)
    return info


def _digest(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def _refresh_stored(coll, path):
    """Re-read the file we just wrote into the collection's stored tables."""
    vr = vrconfig.parse(path)
    hotspots = json.loads(coll.get(PROP_HOTSPOTS) or "[]")
    by_name = {}
    for hotspot in vr.hotspots:
        by_name.setdefault(hotspot.name, hotspot)
    for entry in hotspots:
        hotspot = by_name.get(entry.get("name"))
        if hotspot is None:
            continue
        entry["xyz"] = list(hotspot.xyz or ())
        entry["aabb"] = list(hotspot.aabb or ())
        entry["angles"] = [hotspot.psi, hotspot.the, hotspot.phi]
        entry["block"] = "\n".join(hotspot.lines)
    coll[PROP_HOTSPOTS] = json.dumps(hotspots)

    # and the manipulator table, so the panel shows the numbers that are now in
    # the file instead of the ones it was imported with
    manips = json.loads(coll.get(PROP_MANIPS) or "[]")
    for index, entry in enumerate(manips):
        if index >= len(vr.manips):
            break
        manip = vr.manips[index]
        entry["type"] = manip.type
        entry["a"] = manip.a
        entry["b"] = manip.b
        entry["tooltip"] = manip.tooltip
        entry["action"] = manip.action
        entry["block"] = "\n".join(manip.lines)
        entry["params"] = manip_params(manip)
    coll[PROP_MANIPS] = json.dumps(manips)
