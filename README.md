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
2. `git tag v<same version> && git push --tags`

CI (`.github/workflows/blender-extension.yml`) then

* runs the codec tests and the headless import/export round trip on Blender
  4.2 (the manifest's floor) and the current release,
* builds and validates the package, asserts only the add-on ships in it,
* installs the built zip and re-runs the functional test through the installed
  package (a file the build wrongly excluded fails there),
* attaches the zip and `SHA256SUMS` to the GitHub release,
* uploads the version to extensions.blender.org, where it waits in the
  moderation queue.

Two one-time steps on the platform: submit the add-on once via
<https://extensions.blender.org/submit/> (the API only adds *versions* to an
extension that already exists), and put the API token from your profile page
into the `BLENDER_EXTENSIONS_TOKEN` repository secret.

## License

GPL-3.0-or-later, matching the [extension platform requirements](https://docs.blender.org/manual/en/latest/advanced/extensions/licenses.html).
