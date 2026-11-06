"""Does the imported material reproduce what X-Plane reads out of a combo map?

    Blender -b --factory-startup --python verify/material_combo_test.py

An X-Plane "normal map" is not one: `R,G` are the normal, the blue is base
reflectance (metalness, only when the file says `NORMAL_METALNESS`) and the
alpha is gloss, i.e. roughness inverted.  A texture linked straight into a
Normal Map node therefore throws away two thirds of the material -- and in the
XP12.1 `TEXTURE_MAP normal` layout it reads a blue of 0 as z = -1 and lights
everything inside out.

This test does not read the node graph and call it verified.  Each combo is
built into three fixture OBJs, imported through the real operator, and the
sockets are then **rendered** (emission hijack, one sample, Raw view transform)
so the numbers in the picture are what the shader computes:

    metallic   -> must equal the map's metal channel
    roughness  -> must equal 1 - gloss channel
    normal xy  -> must equal the map's R,G, with the Z rebuilt as 1.0

Exit code and Blender's own output are not signals; grep for `RESULT: PASS`.
"""
import math
import os
import struct
import sys
import zlib

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.join(HERE, "tmp", "material_combo")
sys.path.insert(0, os.path.dirname(REPO))   # the package dir is the repo itself

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("  FAIL %s" % msg, flush=True)
    else:
        print("  ok   %s" % msg, flush=True)


def close(a, b, tol=2e-3):
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def write_png(path, rgbas):
    """8-bit RGBA PNG from a list of byte tuples, one per row, no colour mgmt.

    Blender's own save round-trips through colour management, so the bytes in
    the file would not be the bytes asked for; the expectations below are read
    off these exact bytes.
    """
    h = len(rgbas)
    w = len(rgbas[0])
    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rgbas)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(png)


QUAD = """A
800
OBJ

%s
POINT_COUNTS 4 0 0 6

VT -1 0 -1 0 1 0 0 1
VT 1 0 -1 0 1 0 1 1
VT 1 0 1 0 1 0 1 0
VT -1 0 1 0 1 0 0 0
IDX10 0 1 2 0 2 3
TRIS 0 6
"""

METAL_NRM = (128, 128, 229, 64)      # R,G normal, B metal, A gloss
MAP_NRM = (153, 102, 0, 255)         # XP12.1: R,G only, B/A unused
MAP_MAT = (51, 204, 0, 255)          # R metal, G gloss
CLASSIC_NRM = (128, 128, 255, 255)   # RGB is the whole vector


def fixture(name, globals_text, files):
    d = os.path.join(OUT, name)
    os.makedirs(d, exist_ok=True)
    for fname, rgba in files.items():
        write_png(os.path.join(d, fname), [[rgba] * 8] * 8)
    obj = os.path.join(d, "quad.obj")
    with open(obj, "w") as fh:
        fh.write(QUAD % globals_text)
    return obj, d


# ---------------------------------------------------------------------------
# import + measure
# ---------------------------------------------------------------------------

def fresh():
    """Empty the scene *without* reading a file.

    XP2B's load post-handler runs Python inside the file read and segfaults on
    `wm.read_factory_settings` in 5.2, so the camera and the enabling survive
    and the objects do not.
    """
    for ob in list(bpy.data.objects):          # the startup cube and camera too
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)
    for mat in list(bpy.data.materials):
        bpy.data.materials.remove(mat)


def layer_fields():
    """The XP2B layer (collection) fields the exporter reads textures from."""
    coll = next((c for c in bpy.data.collections
                 if c.xplane.is_exportable_collection), None)
    if coll is None:
        return {}
    L = coll.xplane.layer
    return {"texture": L.texture, "lit": L.texture_lit, "nrm": L.texture_normal,
            "map_normal": L.texture_map_normal,
            "map_material_gloss": L.texture_map_material_gloss,
            "map_gloss": L.texture_map_gloss,
            "metal": L.normal_metalness, "glass": L.blend_glass}


def xp2b_header():
    """TEXTURE-ish records XP2B's own exporter would write, or None."""
    try:
        from io_xplane2blender.xplane_types import xplane_file as XF
        files = XF.createFilesFromBlenderRootObjects(
            bpy.context.scene, bpy.context.view_layer, False)
        text = files[0].write()
    except Exception:                                  # noqa: BLE001
        return None
    want = ("TEXTURE", "TEXTURE_LIT", "TEXTURE_NORMAL", "TEXTURE_MAP",
            "NORMAL_METALNESS", "BLEND_GLASS")
    return ["\t".join(l.split()) for l in text.split("\n")
            if l.split()[:1] and l.split()[0] in want]


def import_material(obj_path):
    try:
        bpy.ops.import_scene.xplane_obj(filepath=obj_path)
    except RuntimeError as exc:
        # "no exportable collection" is reported *after* the meshes and the
        # materials are built, which is all this test is about
        print("  note import reported: %s" % str(exc).splitlines()[0][:90], flush=True)
    mesh = next(o for o in bpy.data.objects if o.type == 'MESH')
    mat = mesh.material_slots[0].material
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.bl_idname == 'ShaderNodeBsdfPrincipled')
    return mesh, mat, nt, bsdf


def _render_setup():
    scn = bpy.context.scene
    scn.render.engine = 'CYCLES'
    scn.cycles.device = 'CPU'
    scn.cycles.samples = 1
    scn.cycles.use_denoising = False
    scn.render.resolution_x = 16
    scn.render.resolution_y = 16
    scn.render.resolution_percentage = 100
    scn.render.filter_size = 0.01
    scn.render.image_settings.file_format = 'OPEN_EXR'
    scn.render.image_settings.color_mode = 'RGBA'
    scn.render.film_transparent = False
    scn.view_settings.view_transform = 'Raw'
    if scn.camera is None:
        cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
        scn.collection.objects.link(cam)
        cam.data.type = 'ORTHO'
        cam.data.ortho_scale = 3.0
        cam.location = (0.0, 0.0, 5.0)
        cam.rotation_euler = (0.0, 0.0, 0.0)
        scn.camera = cam


def measure(socket, tag):
    """Render one socket as emission and return the centre pixel (float32)."""
    mat = socket.id_data if hasattr(socket, "id_data") else None
    nt = socket.node.id_data
    mat = nt.id_data
    out = next(n for n in nt.nodes if n.bl_idname == 'ShaderNodeOutputMaterial')
    prev = out.inputs['Surface'].links[0].from_socket if out.inputs['Surface'].is_linked else None
    em = nt.nodes.new('ShaderNodeEmission')
    nt.links.new(socket, em.inputs['Color'])
    nt.links.new(em.outputs['Emission'], out.inputs['Surface'])

    _render_setup()
    path = os.path.join(OUT, "measure_%s.exr" % tag)
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path)
    px = list(img.pixels)
    w, h = img.size
    i = ((h // 2) * w + w // 2) * 4
    value = tuple(px[i:i + 4])
    bpy.data.images.remove(img)

    # restore the output link so the next measurement starts from the material
    nt.nodes.remove(em)
    if prev is not None:
        nt.links.new(prev, out.inputs['Surface'])
    return value


def measure_metallic(bsdf):
    sock = bsdf.inputs['Metallic']
    if sock.is_linked:
        return measure(sock.links[0].from_socket, "metallic")
    return (sock.default_value,) * 3


def measure_roughness(bsdf):
    sock = bsdf.inputs['Roughness']
    if sock.is_linked:
        return measure(sock.links[0].from_socket, "roughness")
    return (sock.default_value,) * 3


def normal_source(bsdf):
    """The (R, G, Z) feeding the Normal Map node -- measured, not read."""
    sock = bsdf.inputs['Normal']
    check(sock.is_linked, "normal input is wired")
    if not sock.is_linked:
        return None
    nm = sock.links[0].from_node
    check(nm.bl_idname == 'ShaderNodeNormalMap', "normal comes from a Normal Map node")
    src = nm.inputs['Color'].links[0].from_socket
    return measure(src, "normal_color")


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------

def case_metalness(XM):
    print("\n== TEXTURE_NORMAL + NORMAL_METALNESS (XP11/12, 390 of 427 Laminar OBJs)",
          flush=True)
    obj, d = fixture("metalness",
                     "TEXTURE alb.png\nTEXTURE_NORMAL nrm.png\nNORMAL_METALNESS\n"
                     "GLOBAL_specular 1.0",
                     {"alb.png": (200, 200, 200, 255), "nrm.png": METAL_NRM})
    fresh()
    mesh, mat, nt, bsdf = import_material(obj)

    labels = {n.label for n in nt.nodes}
    check("XP normal (R,G)" in labels, "combo map has its own image node")
    check("XP normal split" in labels, "channels are split")
    check("XP normal Z=1" in labels, "Z is rebuilt")
    check("XP gloss -> roughness" in labels, "gloss is inverted into roughness")
    img = next(n.image for n in nt.nodes if n.label == "XP normal (R,G)")
    check(img.colorspace_settings.name == 'Non-Color',
          "vector texture is Non-Color (%s)" % img.colorspace_settings.name)

    z = normal_source(bsdf)
    r, g, b = [v / 255.0 for v in METAL_NRM[:3]]
    check(close(z[0], r), "normal X %.4f == map R %.4f" % (z[0], r))
    check(close(z[1], g), "normal Y %.4f == map G %.4f" % (z[1], g))
    check(close(z[2], 1.0), "normal Z rebuilt as 1.0 (%.4f)" % z[2])

    met = measure_metallic(bsdf)
    check(close(met[0], b), "metallic %.4f == map B %.4f" % (met[0], b))
    rough = measure_roughness(bsdf)
    check(close(rough[0], 1.0 - METAL_NRM[3] / 255.0),
          "roughness %.4f == 1 - map A %.4f" % (rough[0], 1.0 - METAL_NRM[3] / 255.0))

    tex = XM.material_textures(mat)
    check(tex["TEXTURE_NORMAL"] == "nrm.png",
          "write-back still names the normal map: %r" % tex["TEXTURE_NORMAL"])

    # the exporter reads its textures from the layer, not from the node tree:
    # without these fields an imported object exports with no textures at all
    L = layer_fields()
    check(L.get("texture") == "alb.png" and L.get("nrm") == "nrm.png",
          "XP2B layer textures: %r" % (L,))
    check(L.get("metal") is True, "XP2B layer has NORMAL_METALNESS: %r" % (L,))
    head = xp2b_header()
    if head is None:
        print("  note XP2B not available, layer fields only", flush=True)
    else:
        check("TEXTURE\talb.png" in head and "TEXTURE_NORMAL\tnrm.png" in head
              and "NORMAL_METALNESS" in head,
              "XP2B writes the texture records: %r" % (head,))


def case_texture_map(XM):
    print("\n== TEXTURE_MAP normal + material_gloss (XP12.1, the A330 layout)",
          flush=True)
    obj, d = fixture("texture_map",
                     "TEXTURE alb.png\nTEXTURE_MAP normal nrm.png\n"
                     "TEXTURE_MAP material_gloss mat.png\nNORMAL_METALNESS\n"
                     "GLOBAL_specular 1.0",
                     {"alb.png": (200, 200, 200, 255), "nrm.png": MAP_NRM,
                      "mat.png": MAP_MAT})
    fresh()
    mesh, mat, nt, bsdf = import_material(obj)

    check("XP material split" in {n.label for n in nt.nodes},
          "material file gets its own split")
    z = normal_source(bsdf)
    r, g = MAP_NRM[0] / 255.0, MAP_NRM[1] / 255.0
    check(close(z[0], r) and close(z[1], g), "normal X,Y come from the normal file")
    check(close(z[2], 1.0), "normal Z rebuilt as 1.0 (blue of 0 ignored, %.4f)" % z[2])

    met = measure_metallic(bsdf)
    check(close(met[0], MAP_MAT[0] / 255.0),
          "metallic %.4f == material R %.4f" % (met[0], MAP_MAT[0] / 255.0))
    rough = measure_roughness(bsdf)
    check(close(rough[0], 1.0 - MAP_MAT[1] / 255.0),
          "roughness %.4f == 1 - material G %.4f" % (rough[0], 1.0 - MAP_MAT[1] / 255.0))

    L = layer_fields()
    check(L.get("nrm") == "" and L.get("map_normal") == "nrm.png"
          and L.get("map_material_gloss") == "mat.png",
          "XP2B layer holds the 12.1 split, not the classic field: %r" % (L,))
    head = xp2b_header()
    if head is not None:
        check("TEXTURE_MAP\tnormal\tnrm.png" in head
              and "TEXTURE_MAP\tmaterial_gloss\tmat.png" in head,
              "XP2B writes TEXTURE_MAP: %r" % (head,))


def case_classic(XM):
    print("\n== TEXTURE_NORMAL without NORMAL_METALNESS (XP10/11: RGB is the vector)",
          flush=True)
    obj, d = fixture("classic",
                     "TEXTURE alb.png\nTEXTURE_NORMAL nrm.png\nGLOBAL_specular 1.0",
                     {"alb.png": (200, 200, 200, 255), "nrm.png": CLASSIC_NRM})
    fresh()
    mesh, mat, nt, bsdf = import_material(obj)

    labels = {n.label for n in nt.nodes}
    check("XP normal split" not in labels, "no channel split without NORMAL_METALNESS")
    sock = bsdf.inputs['Normal'].links[0].from_node
    check(sock.bl_idname == 'ShaderNodeNormalMap', "still a Normal Map node")
    src = sock.inputs['Color'].links[0].from_node
    check(src.bl_idname == 'ShaderNodeTexImage'
          and os.path.basename(src.image.filepath) == "nrm.png",
          "image goes straight into the Normal Map node (%s)"
          % getattr(getattr(src, "image", None), "filepath", None))
    check(not bsdf.inputs['Metallic'].is_linked, "metallic left alone")
    check(not bsdf.inputs['Roughness'].is_linked, "specular strength is not gloss")
    L = layer_fields()
    check(L.get("nrm") == "nrm.png" and L.get("metal") is False,
          "XP2B layer gets the map with metalness off (this file has no "
          "NORMAL_METALNESS): %r" % (L,))


def main():
    os.makedirs(OUT, exist_ok=True)
    import io_scene_xplane_obj as MOD
    MOD.register()
    import io_scene_xplane_obj.xpmesh as XM
    check(hasattr(XM, "wire_combo_material"), "the installed code has the new wiring")
    # the real import converts in place for XP2B; without it the operator
    # imports the file and then reports that there is nothing to export to
    try:
        bpy.ops.preferences.addon_enable(module="io_xplane2blender")
    except Exception as exc:                      # noqa: BLE001
        print("  note XP2B not enabled: %s" % exc, flush=True)

    case_metalness(XM)
    case_texture_map(XM)
    case_classic(XM)

    print("\nRESULT: %s (%d failure(s))" % ("PASS" if not fails else "FAIL", len(fails)))
    for f in fails:
        print("  - %s" % f)


main()
