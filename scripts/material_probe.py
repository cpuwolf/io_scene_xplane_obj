"""Print what the importer wired for each material of a real OBJ.

    Blender -b --factory-startup --python scripts/material_probe.py -- <file.obj>

Reads the imported node graph back (chain by chain, not by node type) and prints
the map channels the wiring reads, so a file whose material layout the importer
does not understand shows up as a plain link instead of a silently flat surface.
"""
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
path = argv[0]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))

import io_scene_xplane_obj as MOD                     # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)   # before XP2B is enabled:
MOD.register()                                    # its load handler crashes after
try:                                    # the import converts in place for XP2B
    bpy.ops.preferences.addon_enable(module="io_xplane2blender")
except Exception as exc:                # noqa: BLE001
    print("note XP2B not enabled: %s" % exc)

try:
    bpy.ops.import_scene.xplane_obj(filepath=path)
except RuntimeError as exc:             # reported after the meshes are built
    print("note import reported: %s" % str(exc).splitlines()[0][:90])

seen = set()
for ob in bpy.data.objects:
    if ob.type != 'MESH':
        continue
    for slot in ob.material_slots:
        mat = slot.material
        if mat is None or mat.name in seen:
            continue
        seen.add(mat.name)
        nt = mat.node_tree
        bsdf = next((n for n in nt.nodes
                     if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
        print("\n%s  (%s)" % (mat.name, ",".join(sorted(
            n.label for n in nt.nodes if n.bl_idname.startswith("ShaderNode")
            and n.label and n.label != n.bl_label and n.label not in
            ("Principled BSDF", "Material Output")))))
        print("   stored: TEXTURE=%r  LIT=%r  NORMAL=%r  MAP=%r"
              % (mat.get("xp8_texture"), mat.get("xp8_texture_lit"),
                 mat.get("xp8_texture_normal"), mat.get("xp8_texture_map")))

        def describe(socket):
            if socket is None:
                return "absent"
            if not socket.is_linked:
                value = socket.default_value
                if hasattr(value, "__len__"):
                    return "value (%s)" % " ".join("%.3f" % v for v in value)
                return "value %.3f" % value
            parts = []
            node = socket.links[0].from_node
            while node is not None:
                label = node.label or node.bl_idname
                if node.bl_idname == 'ShaderNodeTexImage':
                    img = node.image
                    label = "%s[%s]" % (img.name if img else "?",
                                        img.colorspace_settings.name if img else "-")
                    if img is not None:
                        label += " px0=(%.3f %.3f %.3f %.3f)" % tuple(img.pixels[:4])
                    parts.append(label)
                    break
                parts.append(label)
                feed = [i for i in node.inputs if i.is_linked]
                node = feed[0].links[0].from_node if feed else None
            return " -> ".join(reversed(parts))

        if bsdf is None:
            print("   no Principled BSDF on this material")
            continue
        for name in ("Normal", "Metallic", "Roughness"):
            print("   %-9s %s" % (name, describe(bsdf.inputs.get(name))))
