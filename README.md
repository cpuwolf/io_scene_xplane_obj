# X-Plane OBJ8 Object — Blender importer/exporter

Import and export [X-Plane OBJ8](https://developer.x-plane.com/article/obj8-file-format-specification/)
`.obj` objects with their textures, edit them in Blender, and get a `.obj` back
that X-Plane still loads — animations, manipulators and lights included.

## Install

From [extensions.blender.org](https://extensions.blender.org/add-ons/io_scene_xplane_obj/)
(Preferences > Get Extensions > search "X-Plane OBJ8"), or drag the release
`io_scene_xplane_obj-<version>.zip` into Blender. Requires Blender 4.2 or newer.

Menu: **File > Import > X-Plane OBJ8 (.obj)**, **File > Export > X-Plane OBJ8 (.obj)**.
Tick "Import whole folder" to bring in an aircraft's `objects/` directory at once.

## What it does

* one `.obj` becomes a header Empty plus one mesh per geometry command, so a
  21-part fuselage is 21 objects you can hide, edit and join;
* `TEXTURE` / `TEXTURE_LIT` / `TEXTURE_NORMAL` become a Principled material
  (base colour, emission, normal map), and the original *relative* texture paths
  (`../../objects/foo/bar.png`) are written back out unchanged;
* every `VT` row's authored normal is kept as a custom split normal — X-Plane
  stores lighting normals per vertex and they are not the averaged face normals
  Blender would otherwise compute;
* the command stream (`ANIM_*`, `ATTR_*`, `LIGHT_*`, `ATTR_LOD`, `IF`/`ENDIF`,
  …) is kept verbatim on the header Empty and replayed around the regenerated
  geometry, so moving a vertex does **not** delete the aircraft's door animation;
* a file you did not touch is written back **byte for byte**;
* tri / line / light counts and `POINT_COUNTS` are recomputed from what was
  actually written, and `TRIS`/`LINES` offsets are rebuilt with the index table.

Triangle winding is flipped on both sides: X-Plane calls a *clockwise* triangle
the front face, Blender a counter-clockwise one. Forgetting this makes every
model render inside-out — no error, just invisible surfaces.

## Where the X-Plane data lives

One `.obj` becomes one Empty plus one mesh per geometry command. Blender has no
concept of a dataref, so the file's animation is **not** turned into actions or
drivers — it stays text in custom properties on the Empty and is re-emitted on
export. Nothing you do to the meshes can damage it.

| property | on | contents |
| --- | --- | --- |
| `xp8_dump` | Empty | the whole original file, for the byte-identical write-back |
| `xp8_events` | Empty | ordered command stream: `["A"]` ANIM_begin, `["Z"]` ANIM_end, `["R", line]` any command line verbatim, `["L", n]` geometry batch `n` |
| `xp8_globals` | Empty | `TEXTURE` / `POINT_COUNTS` / `GLOBAL_*` lines |
| `xp8_leaf_states` | Empty | the `ATTR_*` snapshot each batch draws under |
| `xp8_leaf_objs` | Empty | batch index → object name |
| `xp8_fingerprints`, `xp8_cmd_hash` | Empty | what the "was anything changed?" test compares |
| `xp8_leaf`, `xp8_placement` | mesh | which batch it is, and the static `ANIM_trans` applied to place it |
| `xp8_vt` | mesh attribute | source `VT` row index per vertex, so untouched rows keep their exact text |

Editing `xp8_events`, `xp8_globals` or `xp8_leaf_states` and exporting does reach
the file: the untouched-file shortcut switches itself off as soon as any of them,
or any geometry, differs from what was imported.

## Known limits

* `LINES` are imported read-only and exported from their stored indices; light
  primitives (`LIGHT_PARAM`, `LIGHT_NAMED`) are preserved as raw commands.
* There is no manipulator/animation *authoring* UI — existing commands survive
  an export, new ones cannot be created here. Use
  [XPlane2Blender](https://github.com/X-Plane/XPlane2Blender) to build those from
  scratch.
* One `.obj` has one texture set, so an object with several materials exports
  with the first material's textures.
* Blender stores custom normals as int16 per axis, so regenerated normals can
  differ from the source by ~1e-4. Positions and UVs round-trip exactly.

## Development

```sh
./dev-link.sh            # symlink this repo into Blender's user extension folder
                         # then enable "X-Plane OBJ8 Object" in Preferences

python3 verify/test_codec.py verify/fixtures       # codec only, no Blender
python3 verify/test_fixture.py                     # pinned expectations
blender -b --factory-startup --python verify/roundtrip_blender.py -- verify/fixtures

# stronger: run the same tests against a real aircraft
blender -b --factory-startup --python verify/roundtrip_blender.py -- \
    "/path/to/X-Plane 12/Aircraft/<plane>/objects/<folder>"
```

`objcodec.py` is pure Python (no `bpy`), so the format work can be tested and
debugged without starting Blender.

## Releasing

1. bump `version` in `blender_manifest.toml`
2. `git tag -a v<same version>` (annotated: its message becomes the release
   notes) and `git push origin main v<same version>`

The `v*` tag runs `.github/workflows/blender-extension.yml`, which builds
`io_scene_xplane_obj-<version>.zip` with `blender --command extension build`,
validates it, keeps it as the `blender-extension` build artifact and attaches it
to a **draft** GitHub release. Nothing is published automatically: review the
draft and publish it if you want it on GitHub, then send the same zip to
<https://extensions.blender.org/submit/>, where it appears once the moderation
team approves it.


What ends up in the zip is decided by `[build] paths_exclude_pattern` in the
manifest: the add-on modules, `LICENSE` and this README. The test fixtures, the
dev symlink helper and the CI config stay in the repository.

Tests are not part of the release build — run them yourself:

```sh
python3 verify/test_codec.py verify/fixtures
python3 verify/test_fixture.py
blender -b --factory-startup --python verify/roundtrip_blender.py -- verify/fixtures
```


## License

GPL-3.0-or-later, matching the [extension platform requirements](https://docs.blender.org/manual/en/latest/advanced/extensions/licenses.html).
