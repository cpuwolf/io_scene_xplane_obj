"""Blender side of the X-Plane OBJ8 reader/writer.

Coordinate systems
------------------
X-Plane: +X east, +Y up, +Z south, right-handed.
Blender: +X right, +Y away from the viewer, +Z up, right-handed.

A +90 degree turn about X carries one onto the other: (x, y, z) -> (x, -z, y).
That is a rotation, so handedness is preserved and only the *winding* needs
flipping -- X-Plane calls a clockwise triangle the front face, Blender/OpenGL
call a counter-clockwise one the front face.

Data model
----------
One OBJ file becomes one Empty (`xp8_role = "header"`) plus one mesh object per
geometry command (`xp8_role = "tris"` / `"lines"`).  The Empty keeps

    xp8_globals        the TEXTURE / GLOBAL_* / POINT_COUNTS lines, verbatim
    xp8_events         the ordered command stream, with geometry replaced by a
                       leaf index (see objcodec.EV_*)
    xp8_leaf_objs      leaf index -> object name
    xp8_dump           the original file text
    xp8_fingerprints   object name -> hash of the geometry the importer built

so an untouched import is written back byte for byte, and an edited one is
regenerated around the preserved command stream (animations, manipulators and
lights survive an edit because they were never thrown away).
"""

import hashlib
import json
import math
import os

import bpy
import numpy as np

from . import objcodec as OC

# ---------------------------------------------------------------------------
# constants and helpers
# ---------------------------------------------------------------------------

XP_TO_BL = (math.pi / 2.0, 'X')     # rotation that maps X-Plane axes to Blender

PROP_ROLE = "xp8_role"
PROP_SOURCE = "xp8_source"
PROP_GLOBALS = "xp8_globals"
PROP_EVENTS = "xp8_events"
PROP_LEAF_OBJS = "xp8_leaf_objs"
PROP_DUMP = "xp8_dump"
PROP_FINGERPRINTS = "xp8_fingerprints"
PROP_LEAF = "xp8_leaf"
PROP_TEXTURE = "xp8_texture"
PROP_TEXTURE_LIT = "xp8_texture_lit"
PROP_TEXTURE_NORMAL = "xp8_texture_normal"
PROP_TEXTURE_MAP = "xp8_texture_map"
PROP_VT_ATTR = "xp8_vt"

ROLE_HEADER = "header"
ROLE_TRIS = "tris"
ROLE_LINES = "lines"

# quantisation used for VT de-duplication and for the "did the geometry change"
# fingerprint.  0.1 mm on position, 1e-4 on a normal, 1e-5 on a UV -- well below
# anything that shows up in the sim, and coarse enough that float32 storage in
# Blender cannot flip a value across a bucket boundary between two reads.
POS_Q = 1e4
NRM_Q = 1e4
UV_Q = 1e5


def xp_to_bl(co):
    """(x, y, z) in X-Plane metres -> Blender metres."""
    return (co[0], -co[2], co[1])


def bl_to_xp(co):
    return (co[0], co[2], -co[1])


def xp_to_bl_normal(n):
    return (n[0], -n[2], n[1])


def bl_to_xp_normal(n):
    return (n[0], n[2], -n[1])


def _q(values):
    """Quantise a (pos, normal, uv) triple to a hashable key."""
    return (int(round(values[0] * POS_Q)), int(round(values[1] * POS_Q)),
            int(round(values[2] * POS_Q)),
            int(round(values[3] * NRM_Q)), int(round(values[4] * NRM_Q)),
            int(round(values[5] * NRM_Q)),
            int(round(values[6] * UV_Q)), int(round(values[7] * UV_Q)))


def is_header(obj):
    return obj is not None and obj.get(PROP_ROLE) == ROLE_HEADER


def is_batch(obj):
    return obj is not None and obj.get(PROP_ROLE) in (ROLE_TRIS, ROLE_LINES)


def _hexdigest(parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# textures and materials
# ---------------------------------------------------------------------------

def resolve_texture(obj_dir, name):
    """Find a texture named in an OBJ file.  Returns an absolute path or None."""
    if not name:
        return None
    name = name.replace("\\", "/").strip()
    if not name:
        return None
    candidates = []
    if os.path.isabs(name):
        candidates.append(name)
    else:
        candidates.append(os.path.normpath(os.path.join(obj_dir, name)))
        candidates.append(os.path.normpath(os.path.join(obj_dir, os.path.basename(name))))
    for c in candidates:
        if os.path.isfile(c):
            return c
    # case-insensitive fallback (files authored on Windows)
    for c in candidates:
        d, base = os.path.split(c)
        if os.path.isdir(d):
            for entry in os.listdir(d):
                if entry.lower() == base.lower():
                    return os.path.join(d, entry)
    return None


def _load_image(path, non_color=False):
    if not path:
        return None
    try:
        img = bpy.data.images.load(path, check_existing=True)
    except RuntimeError:
        return None
    if non_color:
        try:
            img.colorspace_settings.name = 'Non-Color'
        except TypeError:
            pass
    return img


def build_material(obj8, obj_dir, name):
    """A Principled material wired from the file's TEXTURE / _LIT / _NORMAL."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = None
    for node in nt.nodes:
        if node.bl_idname == 'ShaderNodeBsdfPrincipled':
            bsdf = node
            break
    if bsdf is None:
        return mat

    tex = obj8.global_value("TEXTURE")
    tex_lit = obj8.global_value("TEXTURE_LIT")
    tex_nrm = obj8.global_value("TEXTURE_NORMAL")
    tex_map = obj8.global_value("TEXTURE_MAP")

    raw_tex = tex[0] if tex else ""
    raw_lit = tex_lit[0] if tex_lit else ""
    raw_nrm = tex_nrm[0] if tex_nrm else ""
    used = resolve_texture(obj_dir, raw_tex)
    if raw_tex and not used:
        obj8.warnings.append("texture not found: %s" % raw_tex)

    x = -420
    if used:
        img = _load_image(used)
        if img:
            n = nt.nodes.new('ShaderNodeTexImage')
            n.image = img
            n.location = (x, 240)
            nt.links.new(n.outputs['Color'], bsdf.inputs['Base Color'])
            if img.depth == 32 or img.alpha_mode in ('STRAIGHT', 'PREMUL'):
                nt.links.new(n.outputs['Alpha'], bsdf.inputs['Alpha'])

    lit = resolve_texture(obj_dir, raw_lit)
    if lit:
        img = _load_image(lit)
        if img:
            n = nt.nodes.new('ShaderNodeTexImage')
            n.image = img
            n.location = (x, -60)
            nt.links.new(n.outputs['Color'], bsdf.inputs['Emission Color'])
            try:
                bsdf.inputs['Emission Strength'].default_value = 1.0
            except KeyError:
                pass

    nrm = resolve_texture(obj_dir, raw_nrm)
    if nrm:
        img = _load_image(nrm, non_color=True)
        if img:
            n = nt.nodes.new('ShaderNodeTexImage')
            n.image = img
            n.location = (x, -360)
            nm = nt.nodes.new('ShaderNodeNormalMap')
            nm.location = (x + 280, -360)
            nt.links.new(n.outputs['Color'], nm.inputs['Color'])
            nt.links.new(nm.outputs['Normal'], bsdf.inputs['Normal'])

    # Blend mode: BLEND_GLASS is real transparency (windows), everything else
    # keeps dithering so alpha cut-outs behave like X-Plane's alpha test.
    if obj8.global_value("BLEND_GLASS") is not None:
        try:
            mat.surface_render_method = 'BLENDED'
        except (AttributeError, TypeError):
            pass
        bsdf.inputs['Roughness'].default_value = 0.05
    if "_LIT" in raw_tex.upper() or raw_lit:
        pass

    mat[PROP_TEXTURE] = raw_tex
    mat[PROP_TEXTURE_LIT] = raw_lit
    mat[PROP_TEXTURE_NORMAL] = raw_nrm
    if tex_map and len(tex_map) >= 2:
        mat[PROP_TEXTURE_MAP] = " ".join(tex_map)
    return mat


def material_textures(mat):
    """(albedo, lit, normal) texture names to write, preferring what the OBJ said.

    A node-graph edit should win: if the image plugged into Base Color is no
    longer the one named in the material's stored path, the node graph is taken
    as the truth.  If it still is, the original relative path is kept so that
    `../../objects/foo.png` does not degrade to `foo.png`.
    """
    out = {"TEXTURE": mat.get(PROP_TEXTURE, ""),
           "TEXTURE_LIT": mat.get(PROP_TEXTURE_LIT, ""),
           "TEXTURE_NORMAL": mat.get(PROP_TEXTURE_NORMAL, "")}
    nt = mat.node_tree
    if nt is None:
        return out
    bsdf = None
    for node in nt.nodes:
        if node.bl_idname == 'ShaderNodeBsdfPrincipled':
            bsdf = node
            break
    if bsdf is None:
        return out

    def image_into(socket):
        for link in nt.links:
            if link.to_socket == socket:
                n = link.from_node
                if n.bl_idname == 'ShaderNodeTexImage' and n.image:
                    return n.image
                if n.bl_idname == 'ShaderNodeNormalMap':
                    for l2 in nt.links:
                        if l2.to_socket == n.inputs['Color']:
                            n2 = l2.from_node
                            if n2.bl_idname == 'ShaderNodeTexImage' and n2.image:
                                return n2.image
        return None

    for key, socket in (("TEXTURE", bsdf.inputs.get('Base Color')),
                        ("TEXTURE_LIT", bsdf.inputs.get('Emission Color')),
                        ("TEXTURE_NORMAL", bsdf.inputs.get('Normal'))):
        if socket is None:
            continue
        img = image_into(socket)
        if img is None:
            continue
        stored = out[key]
        if stored and os.path.basename(stored.replace("\\", "/")) == img.name:
            continue                       # node still uses the imported file
        out[key] = img.name
    return out


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def _parse_line_rows(rows):
    out = []
    for row in rows:
        w = row.split()
        try:
            out.append((float(w[1]), float(w[2]), float(w[3])))
        except (IndexError, ValueError):
            out.append((0.0, 0.0, 0.0))
    return out


def import_file(context, path, into_collection=None, report=None):
    """Import one .obj file.  Returns the header Empty (or None on failure)."""
    path = os.path.abspath(path)
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    text = raw.decode("latin-1")
    obj8 = OC.parse_text(text)
    warnings = list(obj8.warnings)
    obj_dir = os.path.dirname(path)
    base = os.path.splitext(os.path.basename(path))[0]

    coll = into_collection
    if coll is None:
        coll = context.collection or context.scene.collection

    header = bpy.data.objects.new(base, None)
    header.empty_display_type = 'PLAIN_AXES'
    header.empty_display_size = 0.5
    coll.objects.link(header)
    header[PROP_ROLE] = ROLE_HEADER
    header[PROP_SOURCE] = path

    mat = build_material(obj8, obj_dir, base)

    leaf_objs = [None] * len(obj8.leaves)
    vline_pts = _parse_line_rows(obj8.vline)

    for leaf_index, leaf in enumerate(obj8.leaves):
        if leaf.kind == "TRIS":
            ob = _build_tris_object(context, obj8, leaf, base, leaf_index,
                                    mat, coll, warnings)
        else:
            ob = _build_lines_object(obj8, leaf, base, leaf_index, vline_pts,
                                     coll, warnings)
        if ob is None:
            warnings.append("geometry command %d (%s) built no mesh"
                            % (leaf_index, leaf.kind))
            continue
        ob.parent = header
        ob.matrix_parent_inverse.identity()
        ob[PROP_LEAF] = leaf_index
        leaf_objs[leaf_index] = ob.name

    header[PROP_GLOBALS] = json.dumps(obj8.globals)
    header[PROP_EVENTS] = json.dumps([[k, p] for k, p in obj8.events])
    header[PROP_LEAF_OBJS] = json.dumps(leaf_objs)
    header[PROP_DUMP] = text
    header["xp8_warnings"] = json.dumps(warnings)
    header["xp8_header_flag"] = obj8.header_flag
    header["xp8_version"] = obj8.version_line
    header["xp8_line_ending"] = {"\r\n": "crlf", "\r": "cr", "\n": "lf"}[
        obj8.line_ending]

    # fingerprint the geometry we just built: export uses it to decide whether
    # the file can be written back verbatim
    fps = {}
    for name in leaf_objs:
        ob = bpy.data.objects.get(name) if name else None
        if ob is not None:
            fps[name] = object_fingerprint(context, ob)
    header[PROP_FINGERPRINTS] = json.dumps(fps)

    # make the header the active object: the exporter and the side panel both
    # start from it, and in background mode nothing is selected otherwise
    for other in context.selected_objects:
        other.select_set(False)
    header.select_set(True)
    context.view_layer.objects.active = header

    for w in warnings:
        if report:
            report({'WARNING'}, w)
    return header


def _new_mesh(name):
    me = bpy.data.meshes.new(name)
    return me


def _set_custom_normals(mesh, normals):
    try:
        mesh.normals_split_custom_set(normals)
    except Exception:
        try:
            mesh.normals_split_custom_set_from_vertices(normals[::3])
        except Exception:
            pass


def _build_tris_object(context, obj8, leaf, base, leaf_index, mat, coll, warnings):
    idx = leaf.indices
    usable = len(idx) - (len(idx) % 3)
    if usable != len(idx):
        warnings.append("TRIS %d %d: dropped %d trailing index/indices"
                        % (leaf.offset, leaf.count, len(idx) - usable))
    if usable == 0:
        return None
    tris = OC.flip_winding(idx[:usable])   # X-Plane CW front -> Blender CCW front

    remap = {}
    verts = []
    vrows = []
    for vi in tris:
        if vi in remap:
            continue
        if not (0 <= vi < len(obj8.vt)):
            warnings.append("TRIS %d %d references VT %d of %d"
                            % (leaf.offset, leaf.count, vi, len(obj8.vt)))
            remap[vi] = 0 if not verts else len(verts) - 1
            continue
        row = obj8.vt[vi]
        remap[vi] = len(verts)
        verts.append(xp_to_bl((row.px, row.py, row.pz)))
        vrows.append(row)
    if not verts:
        return None

    local = [remap[v] for v in tris]
    faces = [tuple(local[i:i + 3]) for i in range(0, len(local), 3)]

    name = "%s_%03d" % (base, leaf_index)
    me = _new_mesh(name)
    me.from_pydata(verts, [], faces)
    me.validate(verbose=False)
    me.update()

    # per-loop UVs and normals, straight out of the VT table
    uv = me.uv_layers.new(name="UVMap")
    flat_uv = [0.0] * (len(tris) * 2)
    normals = []
    have_normals = True
    for k, vi in enumerate(tris):
        row = obj8.vt[vi] if 0 <= vi < len(obj8.vt) else None
        if row is not None:
            flat_uv[k * 2] = row.s
            flat_uv[k * 2 + 1] = row.t
            if row.has_normal:
                normals.append(xp_to_bl_normal((row.nx, row.ny, row.nz)))
            else:
                have_normals = False
                normals.append((0.0, 0.0, 1.0))
        else:
            normals.append((0.0, 0.0, 1.0))
    uv.data.foreach_set("uv", flat_uv)

    me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
    if have_normals:
        _set_custom_normals(me, normals)

    # where each Blender vertex came from, so an untouched row can be written
    # back with its original text
    try:
        attr = me.attributes.new(PROP_VT_ATTR, 'INT', 'POINT')
        attr.data.foreach_set("value", [vi for vi in remap])
    except Exception:
        pass

    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    ob[PROP_ROLE] = ROLE_TRIS
    return ob


def _build_lines_object(obj8, leaf, base, leaf_index, vline_pts, coll, warnings):
    pts = []
    edges = []
    remap = {}
    for i, vi in enumerate(leaf.indices):
        if not (0 <= vi < len(vline_pts)):
            warnings.append("LINES %d %d references VLINE %d of %d"
                            % (leaf.offset, leaf.count, vi, len(vline_pts)))
            return None
        if vi not in remap:
            remap[vi] = len(pts)
            pts.append(xp_to_bl(vline_pts[vi]))
        if i % 2 == 1:
            edges.append((remap[leaf.indices[i - 1]], remap[vi]))
    if not edges:
        return None
    name = "%s_%03d_lines" % (base, leaf_index)
    me = _new_mesh(name)
    me.from_pydata(pts, edges, [])
    me.update()
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    ob[PROP_ROLE] = ROLE_LINES
    return ob


# ---------------------------------------------------------------------------
# geometry extraction: one code path for the import fingerprint, the export
# tables and the "did anything change" test
# ---------------------------------------------------------------------------

def triangle_corners(context, ob, uv_name=None):
    """All triangles of `ob` as X-Plane-space (pos, normal, uv) corner triples.

    Returns (corners, loop_vertex_index) or None.  Winding is already flipped
    back to X-Plane's clockwise-front convention.
    """
    depsgraph = context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(depsgraph)
    try:
        me = ev.to_mesh()
    except RuntimeError:
        return None
    if me is None:
        return None
    try:
        me.calc_loop_triangles()
        n_loops = len(me.loops)
        if n_loops == 0:
            return []
        verts = np.empty(len(me.vertices) * 3, dtype=np.float64)
        me.vertices.foreach_get("co", verts)
        verts = verts.reshape(-1, 3)

        normals = np.array([tuple(n.vector) for n in me.corner_normals],
                           dtype=np.float64).reshape(-1, 3)

        uv = None
        if me.uv_layers:
            uv = me.uv_layers.get(uv_name) if uv_name else None
            if uv is None:
                uv = me.uv_layers.active or me.uv_layers[0]
        if uv is not None:
            uvs = np.empty(n_loops * 2, dtype=np.float64)
            uv.data.foreach_get("uv", uvs)
            uvs = uvs.reshape(-1, 2)
        else:
            uvs = np.zeros((n_loops, 2), dtype=np.float64)

        m = np.array(ob.matrix_world, dtype=np.float64)
        rot = m[:3, :3]
        trans = m[:3, 3]
        world = verts @ rot.T + trans
        # Blender (x, y, z) -> X-Plane (x, z, -y); same rotation for normals
        xp = np.empty_like(world)
        xp[:, 0] = world[:, 0]
        xp[:, 1] = world[:, 2]
        xp[:, 2] = -world[:, 1]
        nrm = normals @ rot.T
        lengths = np.linalg.norm(nrm, axis=1)
        lengths[lengths == 0.0] = 1.0
        nrm = nrm / lengths[:, None]
        nx = np.empty_like(nrm)
        nx[:, 0] = nrm[:, 0]
        nx[:, 1] = nrm[:, 2]
        nx[:, 2] = -nrm[:, 1]

        loop_vert = np.empty(n_loops, dtype=np.int64)
        me.loops.foreach_get("vertex_index", loop_vert)

        corners = []
        order = []
        for lt in me.loop_triangles:
            a, b, c = lt.loops
            # Blender front face is CCW; X-Plane's is CW
            for li in (a, c, b):
                vi = loop_vert[li]
                corners.append((tuple(xp[vi]), tuple(nx[li]), tuple(uvs[li])))
                order.append(int(vi))
        return corners, order
    finally:
        ev.to_mesh_clear()


def corner_key(pos, nrm, uv):
    return (int(round(pos[0] * POS_Q)), int(round(pos[1] * POS_Q)),
            int(round(pos[2] * POS_Q)),
            int(round(nrm[0] * NRM_Q)), int(round(nrm[1] * NRM_Q)),
            int(round(nrm[2] * NRM_Q)),
            int(round(uv[0] * UV_Q)), int(round(uv[1] * UV_Q)))


def object_fingerprint(context, ob, uv_name=None):
    """Hash of the geometry exactly as the exporter would read it."""
    got = triangle_corners(context, ob, uv_name)
    if got is None:
        return "unreadable"
    corners = got[0]
    m = np.array(ob.matrix_world, dtype=np.float64).ravel()
    parts = [("%.5f|" % v).encode() for v in m]
    for pos, nrm, uv in corners:
        parts.append(("%d,%d,%d,%d,%d,%d,%d,%d|"
                      % corner_key(pos, nrm, uv)).encode())
    return _hexdigest(parts)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def _header_template(header):
    """Rebuild the Obj8 skeleton (globals, events, leaves, VT text) of a file."""
    dump = header.get(PROP_DUMP)
    template = None
    if dump:
        try:
            template = OC.parse_text(dump)
        except ValueError:
            template = None
    if template is None:
        template = OC.Obj8()
    if PROP_GLOBALS in header:
        try:
            template.globals = list(json.loads(header[PROP_GLOBALS]))
        except ValueError:
            pass
    if PROP_EVENTS in header:
        try:
            template.events = [(k, p) for k, p in json.loads(header[PROP_EVENTS])]
        except ValueError:
            pass
    return template


def _leaf_object_names(header):
    try:
        return list(json.loads(header.get(PROP_LEAF_OBJS, "[]")))
    except ValueError:
        return []


def build_tables(context, template, leaf_objects, uv_name=None, report=None):
    """Meshes -> (vt row texts, index table, spans per leaf index, fingerprints).

    VT rows a vertex still matches are reused with their original text, so a
    file that was only touched here and there keeps its exact numbers.  The
    match cannot be exact: Blender stores custom normals as int16 per axis, so
    every normal comes back ~3e-5 off (positions and UVs are exact).  Hence a
    coarse bucket lookup plus a tolerance check.
    """
    orig_rows = template.vt
    orig_by_key = {}
    orig_by_pos = {}
    for i, row in enumerate(orig_rows):
        if not row.has_normal:
            continue
        orig_by_key.setdefault(
            corner_key((row.px, row.py, row.pz),
                       (row.nx, row.ny, row.nz),
                       (row.s, row.t)), i)
        # bucket on POSITION ONLY.  Position and UV survive the Blender round
        # trip exactly, but a float32 value times a coefficient still lands on
        # the other side of a rounding boundary now and then (UV 0.5885 reads
        # back as 0.588500022 -> *1000 gives 588.4999 vs 588.50002), and the
        # normals are int16-quantised.  So: cheap bucket, exact tolerance check.
        bucket = (int(round(row.px * 100.0)), int(round(row.py * 100.0)),
                  int(round(row.pz * 100.0)))
        orig_by_pos.setdefault(bucket, []).append(i)

    def find_original(pos, nrm, uv):
        """Row index of an equal source row, within Blender's normal noise."""
        bucket = (int(round(pos[0] * 100.0)), int(round(pos[1] * 100.0)),
                  int(round(pos[2] * 100.0)))
        for i in orig_by_pos.get(bucket, ()):
            row = orig_rows[i]
            if abs(row.px - pos[0]) > 1e-4 or abs(row.py - pos[1]) > 1e-4 \
                    or abs(row.pz - pos[2]) > 1e-4:
                continue
            if abs(row.s - uv[0]) > 1e-4 or abs(row.t - uv[1]) > 1e-4:
                continue
            if abs(row.nx - nrm[0]) > 2e-3 or abs(row.ny - nrm[1]) > 2e-3 \
                    or abs(row.nz - nrm[2]) > 2e-3:
                continue
            return i
        return None

    texts = [row.text for row in orig_rows]
    used = [False] * len(texts)
    key_to_index = {}
    idx_values = []
    spans = {}
    fps = {}

    for leaf_index, name in enumerate(leaf_objects):
        ob = bpy.data.objects.get(name) if name else None
        if ob is None:
            if report:
                report({'WARNING'},
                       "geometry command %d has no object (%r); skipped"
                       % (leaf_index, name))
            continue
        leaf = template.leaves[leaf_index] if leaf_index < len(template.leaves) else None
        if leaf is not None and leaf.kind == "LINES":
            # line primitives are not edited through the mesh: reuse the indices
            offset = len(idx_values)
            idx_values.extend(leaf.indices)
            spans[leaf_index] = (offset, len(leaf.indices))
            continue
        if ob.type != 'MESH':
            if report:
                report({'WARNING'}, "%s is not a mesh; skipped" % name)
            continue

        got = triangle_corners(context, ob, uv_name)
        if not got or not got[0]:
            continue
        corners = got[0]
        offset = len(idx_values)
        parts = []
        for pos, nrm, uv in corners:
            key = corner_key(pos, nrm, uv)
            slot = key_to_index.get(key)
            if slot is None:
                slot = orig_by_key.get(key)
            if slot is None:
                slot = find_original(pos, nrm, uv)
            if slot is None:
                body = "\t".join(OC.fmt_num(v) for v in
                                 (pos[0], pos[1], pos[2], nrm[0], nrm[1],
                                  nrm[2], uv[0], uv[1]))
                slot = len(texts)
                texts.append(body)
                used.append(True)
            else:
                used[slot] = True
            key_to_index[key] = slot
            idx_values.append(slot)
            parts.append(("%d,%d,%d,%d,%d,%d,%d,%d|" % key).encode())
        spans[leaf_index] = (offset, len(corners))
        m = np.array(ob.matrix_world, dtype=np.float64).ravel()
        fps[name] = _hexdigest([("%.5f|" % v).encode() for v in m] + parts)

    # drop the rows nothing points at any more, keeping the original order
    keep = [i for i, u in enumerate(used) if u]
    remap = dict((old, new) for new, old in enumerate(keep))
    vt_text = [texts[i] for i in keep]
    # note: span offsets point into the index table, which compaction of the
    # vertex table does not move -- only the values change
    idx_values = [remap[v] for v in idx_values]
    return vt_text, idx_values, spans, fps


def _unmodified(context, header, uv_name):
    stored = header.get(PROP_FINGERPRINTS)
    if not stored:
        return False
    try:
        stored = json.loads(stored)
    except ValueError:
        return False
    if not stored:
        return False
    for name, want in stored.items():
        ob = bpy.data.objects.get(name)
        if ob is None:
            return False
        if object_fingerprint(context, ob, uv_name) != want:
            return False
    return True


def export_header(context, header, filepath, uv_name=None, preserve=True,
                  report=None):
    """Write one OBJ8 file from a header Empty and its meshes."""
    template = _header_template(header)
    leaf_objs = _leaf_object_names(header)
    if not template.events:
        template.events = [(OC.EV_LEAF, i) for i in range(len(leaf_objs))]

    if preserve and header.get(PROP_DUMP) and _unmodified(context, header, uv_name):
        with open(filepath, "wb") as f:
            f.write(header[PROP_DUMP].encode("latin-1", "replace"))
        return {"verbatim": True, "objects": len(leaf_objs),
                "vt": len(template.vt), "indices": len(template.idx)}

    vt_text, idx_values, spans, fps = build_tables(
        context, template, leaf_objs, uv_name, report=report)
    text = OC.emit(template, vt_text=vt_text, idx_values=idx_values,
                   leaf_spans=spans)
    with open(filepath, "wb") as f:
        f.write(text.encode("latin-1", "replace"))
    return {"verbatim": False, "objects": len(fps), "vt": len(vt_text),
            "indices": len(idx_values)}


def collect_scene_meshes(header, only_used=False):
    """Meshes under a header Empty, in object order."""
    out = []
    for child in header.children:
        if child.type == 'MESH':
            out.append(child)
    return out


def export_unparented_objects(context, filepath, objects, uv_name=None,
                              report=None):
    """Write a brand new OBJ8 from loose mesh objects (no import needed)."""
    meshes = [ob for ob in objects if ob.type == 'MESH']
    if not meshes:
        if report:
            report({'ERROR'}, "nothing to export: no mesh objects selected")
        return None
    obj8 = OC.Obj8()
    obj8.header_flag = "I"
    obj8.line_ending = "\n"

    tex = {"TEXTURE": "", "TEXTURE_LIT": "", "TEXTURE_NORMAL": ""}
    for ob in meshes:
        hit = False
        for mat in ob.data.materials:
            if mat is None or mat.node_tree is None:
                continue
            got = material_textures(mat)
            for k in tex:
                if not tex[k] and got.get(k):
                    tex[k] = os.path.basename(got[k].replace("\\", "/"))
            if any(got.values()):
                hit = True
                break
        if hit:
            break
    obj8.globals = ["GLOBAL_specular 1"]
    obj8.globals.append("TEXTURE\t%s" % tex["TEXTURE"] if tex["TEXTURE"]
                        else "TEXTURE")
    if tex["TEXTURE_LIT"]:
        obj8.globals.append("TEXTURE_LIT\t%s" % tex["TEXTURE_LIT"])
    if tex["TEXTURE_NORMAL"]:
        obj8.globals.append("TEXTURE_NORMAL\t%s" % tex["TEXTURE_NORMAL"])
    obj8.globals.append("POINT_COUNTS\t0 0 0 0")

    for i, ob in enumerate(meshes):
        obj8.leaves.append(OC.Leaf("TRIS", [], [], 0, 0))
        obj8.events.append((OC.EV_LEAF, i))

    leaf_objs = [ob.name for ob in meshes]
    vt_text, idx_values, spans, fps = build_tables(
        context, obj8, leaf_objs, uv_name, report=report)
    text = OC.emit(obj8, vt_text=vt_text, idx_values=idx_values,
                   leaf_spans=spans)
    with open(filepath, "wb") as f:
        f.write(text.encode("latin-1", "replace"))
    return {"verbatim": False, "objects": len(meshes), "vt": len(vt_text),
            "indices": len(idx_values)}
