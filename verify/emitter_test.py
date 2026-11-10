"""PARTICLE_SYSTEM / EMITTER must survive import -> conversion -> XP2B export.

    <blender> -b --factory-startup --python verify/emitter_test.py
    <blender> -b --factory-startup --python verify/emitter_test.py -- <file.obj> ...

The importer keeps the OBJ8 text verbatim, so the expected values are read out
of that same text with the parser instead of being hard-coded: one EMITTER line
in, the same name / index / position / angles out of XPlane2Blender's writer.

Defaults are the two shapes that matter: a particles-only file (no geometry at
all, POINT_COUNTS 0 0 0 0) and a file that carries geometry *and* emitters
nested inside ANIM blocks.
"""
import math
import os
import sys

import bpy

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
sys.path.insert(0, REPO)

import addon_utils                                          # noqa: E402

addon_utils.enable("io_xplane2blender", default_set=True)

import io_scene_xplane_obj as IMP                           # noqa: E402
import io_scene_xplane_obj.objcodec as OC                    # noqa: E402

try:
    IMP.register()
except Exception:                                           # noqa: BLE001
    pass

XP = "/Users/rourou/X-Plane 12/Aircraft"
FIX = os.path.join(REPO, "verify", "fixtures")
DEFAULT = [os.path.join(FIX, "emitter.obj")] + [
    p for p in (
        os.path.join(XP, "Laminar Research", "Boeing 737-800", "objects",
                     "particles", "particles.obj"),
        os.path.join(XP, "Laminar Research", "Airbus A330-300", "objects",
                     "A330_fuselage_C.obj"),
    ) if os.path.exists(p)
]
TOL = 1e-3
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print("   FAIL %s" % msg, flush=True)
    return cond


def source_emitters(text):
    """[(name, index, (x, y, z), (psi, the, phi))] in file order."""
    out = []
    obj8 = OC.parse_text(text)
    for kind, payload in obj8.events:
        if kind != OC.EV_RAW or not payload:
            continue
        words = payload.split()
        if not words or words[0] != "EMITTER":
            continue
        try:
            pos = tuple(float(w) for w in words[2:5])
            ang = tuple(float(w) for w in words[5:8])
        except ValueError:
            continue
        index = int(float(words[8])) if len(words) > 8 else None
        out.append((words[1], index, pos, ang))
    return out


def written_emitters(text):
    """The same rows, parsed out of the file XPlane2Blender wrote."""
    out = []
    for line in text.splitlines():
        words = line.split()
        if not words or words[0] != "EMITTER":
            continue
        pos = tuple(float(w) for w in words[2:5])
        ang = tuple(float(w) for w in words[5:8])
        index = int(float(words[8])) if len(words) > 8 else None
        out.append((words[1], index, pos, ang))
    return out


def close(a, b):
    return all(abs(x - y) <= TOL for x, y in zip(a, b))


def _rot(ang, axis2):
    """One axis rotation, row-major, ``axis2`` being (i, j) of that axis."""
    c, s = math.cos(ang), math.sin(ang)
    m = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    i, j = axis2
    m[i][i] = c
    m[j][j] = c
    m[i][j] = -s
    m[j][i] = s
    return m


def _mul(a, b):
    return [[sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)]
            for r in range(3)]


def orientation(ang):
    """The rotation a (psi, the, phi) triple means, as a matrix.

    Compare the *orientation*, not the three numbers: XPlane2Blender rewrites
    them through an Euler decomposition, so a yaw of +180 comes back as
    -179.99999 and the F-14's 181 as -179 - the same rotation, bit for bit,
    with a different spelling.  A wrong mapping (psi and the swapped, say)
    still produces a different matrix, so this does not blunt the check.
    """
    psi, the, phi = (math.radians(v) for v in ang)
    # XP2B writes `-phi_z theta_x psi_y` of an XYZ euler, i.e. Rz * Ry * Rx
    return _mul(_mul(_rot(-psi, (0, 1)), _rot(phi, (1, 2))), _rot(the, (2, 0)))


def same_orientation(a, b):
    ma, mb = orientation(a), orientation(b)
    return all(abs(ma[r][c] - mb[r][c]) <= TOL
               for r in range(3) for c in range(3))


def expected_chain(text):
    """{emitter name: set of the transform datarefs of its enclosing blocks}.

    The number round trip below cannot tell *which* block an emitter ended up
    in - the numbers are block-local either way - and an emitter one block too
    deep flies wrong while the file looks perfect.  So the ancestry is graded
    against the file: a run of ANIM blocks drives an emitter with exactly the
    union of their datarefs, and each of those sits on a bone above the Empty.
    """
    obj8 = OC.parse_text(text)
    anim = IMP.XA.build(obj8)
    by_begin = {blk.begin_ev: blk for blk in anim.blocks
                if getattr(blk, "begin_ev", None) is not None}
    out = {}
    stack = []
    for ev_index, (kind, payload) in enumerate(obj8.events):
        if kind == OC.EV_ANIM_BEGIN:
            stack.append(by_begin.get(ev_index))
            continue
        if kind == OC.EV_ANIM_END:
            if stack:
                stack.pop()
            continue
        if kind != OC.EV_RAW or not payload:
            continue
        words = payload.split()
        if not words or words[0] != "EMITTER":
            continue
        seen = set()
        for blk in stack:
            for cmd in (blk.commands if blk is not None else []):
                if cmd["kind"] in (IMP.XA.CMD_SHOW, IMP.XA.CMD_HIDE):
                    continue
                if cmd.get("dataref"):
                    seen.add(cmd["dataref"])
        out.setdefault(words[1], set()).add(frozenset(seen))
    return out


def actual_chain(ob):
    """The transform datarefs on the bones above an emitter Empty."""
    seen = set()
    if ob.parent_type != 'BONE' or ob.parent is None:
        return seen
    arm = ob.parent
    bone = arm.data.bones.get(ob.parent_bone)
    while bone is not None:
        for dr in bone.xplane.datarefs:
            if getattr(dr, "anim_type", "") in ("show", "hide"):
                continue
            if dr.path:
                seen.add(dr.path)
        bone = bone.parent
    return seen


def run(path):
    print("CASE %s" % path, flush=True)
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)

    try:
        result = bpy.ops.import_scene.xplane_obj(filepath=path)
    except RuntimeError as exc:
        check(False, "%s: the import raised %s" % (path, exc))
        return
    check('FINISHED' in result, "%s: import returned %s" % (path, sorted(result)))

    colls = [c for c in bpy.data.collections
             if getattr(getattr(c, "xplane", None), "is_exportable_collection",
                        False)]
    if not check(len(colls) == 1, "%s: %d exportable collections"
                 % (path, len(colls))):
        return
    coll = colls[0]

    dump = coll.get(IMP.XM.PROP_DUMP, "")
    expected = source_emitters(dump)
    check(bool(expected), "%s: the source text has no EMITTER line" % path)

    pss = OC.parse_text(dump).global_value("PARTICLE_SYSTEM")
    want_pss = pss[0] if pss else ""
    got_pss = coll.xplane.layer.particle_system_file
    check(got_pss == want_pss,
          "%s: layer particle_system_file is %r, the file says %r"
          % (path, got_pss, want_pss))

    # every emitter became an Empty with XP2B's own emitter settings, and the
    # ones inside an ANIM block ride that block's bone
    empties = [ob for ob in coll.all_objects
               if ob.type == 'EMPTY'
               and getattr(getattr(ob, "xplane", None), "special_empty_props",
                           None) is not None
               and ob.xplane.special_empty_props.special_type
               == "emitter_particle"]
    check(len(empties) == len(expected),
          "%s: %d emitter Empties for %d EMITTER lines"
          % (path, len(empties), len(expected)))
    placed = sum(1 for ob in empties if ob.parent_type == 'BONE')
    print("   emitters=%d bone-parented=%d meshes=%d"
          % (len(empties), placed,
             len([o for o in coll.all_objects if o.type == 'MESH'])), flush=True)

    chains = expected_chain(dump)
    for ob in empties:
        name = ob.xplane.special_empty_props.emitter_props.name
        want = chains.get(name)
        if not check(want is not None,
                     "%s: emitter %s is not in the file's command stream"
                     % (path, name)):
            continue
        got = frozenset(actual_chain(ob))
        check(got in want,
              "%s: emitter %s rides %s, the file's enclosing blocks carry %s"
              % (path, name, sorted(got), [sorted(x) for x in want]))

    warnings = coll.get("xp2b_convert_warnings")
    print("   convert warnings: %s" % warnings, flush=True)

    from io_scene_xplane_obj.xplane2blender.xplane_types import \
        xplane_file as XF                                      # noqa: E402
    files = XF.createFilesFromBlenderRootObjects(bpy.context.scene,
                                                 bpy.context.view_layer)
    if not check(len(files) == 1, "%s: XP2B made %d files" % (path, len(files))):
        return
    text = files[0].write() or ""
    check(bool(text.strip()), "%s: XP2B wrote nothing" % path)
    check("PARTICLE_SYSTEM" in text,
          "%s: the written file has no PARTICLE_SYSTEM record" % path)

    got = written_emitters(text)
    check(len(got) == len(expected),
          "%s: %d EMITTER lines written for %d in the source"
          % (path, len(got), len(expected)))

    # order is XP2B's (objects sort by name), so pair by (name, index) - the
    # only key that is unique per line in these files
    pool = list(got)
    for name, index, pos, ang in expected:
        hit = None
        for cand in pool:
            if cand[0] == name and cand[1] == index:
                hit = cand
                break
        if hit is None:
            check(False, "%s: emitter %s (index %s) is missing from the export"
                  % (path, name, index))
            continue
        pool.remove(hit)
        check(close(hit[2], pos),
              "%s: emitter %s position %s, source says %s"
              % (path, name, hit[2], pos))
        check(same_orientation(hit[3], ang),
              "%s: emitter %s angles %s, source says %s (different rotation)"
              % (path, name, hit[3], ang))
    if not pool and not fails:
        print("   OK %s" % path, flush=True)


argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
paths = argv or [p for p in DEFAULT if os.path.exists(p)]
if not paths:
    print("RESULT: SKIP - no test file found", flush=True)
    raise SystemExit(0)
for path in paths:
    run(path)

print("RESULT: %s (%d failure(s))" % ("PASS" if not fails else "FAIL",
                                      len(fails)), flush=True)
