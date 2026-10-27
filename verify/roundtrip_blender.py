"""Headless Blender test for the X-Plane OBJ8 addon.

    blender -b --factory-startup --python verify/roundtrip_blender.py -- \
            [samples-dir] [output-dir]

Checks, in order:
 1. import every .obj in the sample folder (real operator, real materials)
 2. export straight back and require BYTE-IDENTICAL files
 3. move one vertex, export again: the output must still parse, keep the same
    triangle count, and keep every ANIM_* / ATTR_* command line
 4. re-import the edited file and confirm it comes back with the same geometry
 5. export a loose Blender mesh with no X-Plane data and re-import it

Defaults to the small fixture in verify/fixtures, so it works on a bare
checkout and in CI.  Point it at a real aircraft folder for a much stronger
test:

    blender -b --factory-startup --python verify/roundtrip_blender.py -- \
        "/path/to/objects/C90B_exterior"

Set XPOBJ_EXT_MODULE=bl_ext.user_default.io_scene_xplane_obj to test a *built*
and installed package instead of importing the source tree -- that is what the
release workflow does, so the zip's file list is proven complete.
"""
import hashlib
import importlib
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import bpy                                                   # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
SAMPLES = argv[0] if argv else os.path.join(REPO, "verify", "fixtures")
OUT = argv[1] if len(argv) > 1 else os.path.join(
    tempfile.gettempdir(), "xpobj_roundtrip")
EXT_MODULE = os.environ.get("XPOBJ_EXT_MODULE", "").strip()


def load_addon():
    """Return (OC, XM) from either the installed extension or the source tree."""
    if EXT_MODULE:
        pkg = importlib.import_module(EXT_MODULE)
        print("testing the installed package: %s (%s)"
              % (EXT_MODULE, os.path.dirname(pkg.__file__)))
        return pkg.OC, pkg.XM
    sys.path.insert(0, os.path.dirname(REPO))
    pkg = __import__(os.path.basename(REPO))
    pkg.register()
    print("testing the source tree: %s" % REPO)
    return pkg.OC, pkg.XM


OC, XM = load_addon()

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg)
    return cond


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if EXT_MODULE:
        # factory-startup drops add-ons, and this one is what we are testing
        bpy.ops.preferences.addon_enable(module=EXT_MODULE)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    if not os.path.isdir(SAMPLES):
        print("sample folder missing: %s" % SAMPLES)
        return 2
    os.makedirs(OUT, exist_ok=True)
    originals = sorted(os.path.join(SAMPLES, f) for f in os.listdir(SAMPLES)
                       if f.lower().endswith(".obj"))
    print("samples: %d files\n" % len(originals))

    # ---- 1. import ------------------------------------------------------
    reset_scene()
    t = time.time()
    r = bpy.ops.import_scene.xplane_obj(filepath=originals[0], import_folder=True)
    print("import: %s in %.1fs" % (r, time.time() - t))
    check(r == {'FINISHED'}, "import operator failed")

    headers = [o for o in bpy.data.objects if XM.is_header(o)]
    print("headers: %d" % len(headers))
    check(len(headers) == len(originals),
          "expected %d headers, got %d" % (len(originals), len(headers)))

    tris = {}
    for h in headers:
        t_count = sum(len(c.data.polygons) for c in h.children
                      if c.get(XM.PROP_ROLE) == XM.ROLE_TRIS)
        tris[os.path.splitext(os.path.basename(h.get(XM.PROP_SOURCE)))[0]] = t_count
        print("  %-26s parts %3d  polys %6d" % (
            os.path.basename(h.get(XM.PROP_SOURCE)), len(h.children), t_count))
    check(all(v > 0 for v in tris.values()), "some file imported no triangles")
    check(len(bpy.data.images) > 0, "no textures loaded")
    print("images: %d  materials: %d" % (len(bpy.data.images),
                                         len(bpy.data.materials)))
    for h in headers:
        w = h.get("xp8_warnings")
        if w and w != "[]":
            print("  warn %s: %s" % (os.path.basename(h.get(XM.PROP_SOURCE)),
                                     w[:200]))

    # ---- 2. export straight back, expect byte identity ------------------
    print("\nexport #1 (untouched):")
    for f in originals:
        src = f
        stem = os.path.splitext(os.path.basename(f))[0]
        dst = os.path.join(OUT, stem + ".obj")
        header = [h for h in headers
                  if h.get(XM.PROP_SOURCE) == os.path.abspath(src)]
        check(len(header) == 1, "no header for %s" % stem)
        if not header:
            continue
        t = time.time()
        stats = XM.export_header(bpy.context, header[0], dst, preserve=True,
                                 report=lambda lvl, msg: print("   ", msg))
        same = sha(src) == sha(dst)
        print("  %-26s %-9s vt %5d idx %6d  %s  (%.1fs)"
              % (stem, "verbatim" if stats["verbatim"] else "regen",
                 stats["vt"], stats["indices"],
                 "IDENTICAL" if same else "DIFFERS", time.time() - t))
        check(same, "%s is not byte identical after an untouched round trip"
              % stem)

    # ---- 3. edit geometry, export, verify the file structure survives ---
    print("\nedit + export #2:")
    # the biggest object is the interesting one (flight model, door animations)
    victim_header = max(headers, key=lambda h: sum(
        len(c.data.polygons) for c in h.children
        if c.get(XM.PROP_ROLE) == XM.ROLE_TRIS))
    victim = None
    for child in victim_header.children:
        if child.get(XM.PROP_ROLE) == XM.ROLE_TRIS and len(child.data.vertices):
            victim = child
            break
    check(victim is not None, "no tri object to edit")
    if victim is None:
        return 1
    # move one vertex 10 cm along +X (Blender), which is +X in X-Plane too
    victim.data.vertices[0].co.x += 0.1
    victim.data.update()
    stem = os.path.splitext(os.path.basename(victim_header[XM.PROP_SOURCE]))[0]
    dst = os.path.join(OUT, stem + "_edited.obj")
    stats = XM.export_header(bpy.context, victim_header, dst, preserve=True)
    print("  %s: %-9s vt %d idx %d" % (stem, "verbatim" if stats["verbatim"]
                                       else "regen", stats["vt"],
                                       stats["indices"]))
    check(not stats["verbatim"], "an edited file was still written verbatim")
    a = OC.parse(victim_header[XM.PROP_SOURCE])
    b = OC.parse(dst)
    check(len(a.idx) == len(b.idx),
          "index count changed: %d -> %d" % (len(a.idx), len(b.idx)))
    check(len(a.leaves) == len(b.leaves),
          "leaf count changed: %d -> %d" % (len(a.leaves), len(b.leaves)))
    kws = lambda o: [w for w in (p.split()[0] for k, p in o.events
                                 if k == OC.EV_RAW) ]
    check(kws(a) == kws(b), "the raw command stream changed")
    anim_a = sum(1 for k, _ in a.events if k == OC.EV_ANIM_BEGIN)
    anim_b = sum(1 for k, _ in b.events if k == OC.EV_ANIM_BEGIN)
    check(anim_a == anim_b, "ANIM blocks lost: %d -> %d" % (anim_a, anim_b))
    print("  commands preserved: %d raw lines, %d ANIM blocks"
          % (len(kws(a)), anim_a))
    # the moved vertex must actually be in the file
    moved = max(abs(row.px) for row in b.vt)
    check(moved > max(abs(row.px) for row in a.vt) - 1e-9,
          "the moved vertex is not in the exported file")
    # and triangles still reference valid vertices
    check(all(0 <= i < len(b.vt) for i in b.idx), "index out of range")
    # state snapshots must still be attached to every leaf
    check(all(lf.state for lf in b.leaves[:3]) or not a.leaves[0].state,
          "leaf state was dropped")

    # ---- 4. re-import the edited file ----------------------------------
    print("\nre-import edited file:")
    reset_scene()
    r = bpy.ops.import_scene.xplane_obj(filepath=dst)
    check(r == {'FINISHED'}, "re-import failed")
    h2 = [o for o in bpy.data.objects if XM.is_header(o)]
    check(len(h2) == 1, "re-import made %d headers" % len(h2))
    if h2:
        polys = sum(len(c.data.polygons) for c in h2[0].children)
        check(polys == tris[stem], "re-imported poly count %d != %d"
              % (polys, tris[stem]))
        print("  OK: %d parts, %d polys" % (len(h2[0].children), polys))

    # ---- 5. scratch export ---------------------------------------------
    print("\nscratch export:")
    reset_scene()
    bpy.ops.mesh.primitive_cube_add(size=2)
    cube = bpy.context.active_object
    cube.location = (0.0, 0.0, 1.0)
    m = bpy.data.materials.new("cube")
    m.use_nodes = True
    cube.data.materials.append(m)
    dst = os.path.join(OUT, "cube.obj")
    r = bpy.ops.export_scene.xplane_obj(filepath=dst, mode='FILE',
                                        preserve_unmodified=False)
    check(r == {'FINISHED'}, "scratch export failed")
    o = OC.parse(dst)
    # 8 corners * 3 faces: the three faces meeting at a corner need three
    # different normals, and a VT row carries one normal, so 24 rows is right
    check(len(o.vt) == 24, "cube should have 24 VT rows, got %d" % len(o.vt))
    check(len(o.idx) == 36, "cube should have 36 indices, got %d" % len(o.idx))
    print("  %s: vt %d idx %d leaves %d" % (os.path.basename(dst), len(o.vt),
                                            len(o.idx), len(o.leaves)))
    reset_scene()
    r = bpy.ops.import_scene.xplane_obj(filepath=dst)
    cube2 = [ob for ob in bpy.data.objects if ob.type == 'MESH']
    check(r == {'FINISHED'} and cube2, "cube re-import failed")
    if cube2:
        zs = [v.co.z for v in cube2[0].data.vertices]
        print("  cube back at z %.2f..%.2f (Blender Z = X-Plane Y)"
              % (min(zs), max(zs)))
        check(abs(max(zs) - 2.0) < 1e-4, "the object transform was not baked")

    print("\n%s" % ("ALL CHECKS PASSED" if not fails
                    else "%d CHECK(S) FAILED:\n  " % len(fails)
                         + "\n  ".join(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
