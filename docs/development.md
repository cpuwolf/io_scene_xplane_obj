# Development and releasing

## Working on it

```sh
./dev-link.sh            # symlink this repo into Blender's user extension folder
                         # then enable "X-Plane OBJ8 Object" in Preferences
```

`objcodec.py` is pure Python (no `bpy`), so the format work can be tested and
debugged without starting Blender.

## Tests

```sh
python3 verify/test_fixture.py                     # pinned parser expectations
python3 verify/test_anim.py                        # ANIM_* evaluator, no Blender
python3 verify/test_acf.py                         # .acf -> object paths, no Blender

blender -b --factory-startup --python verify/material_combo_test.py   # combo map, texture + light-level fields, XP2B output
blender -b --factory-startup --python verify/check_bones_xp2b.py -- <file.obj>  # animation -> bones -> XP2B
blender -b --factory-startup --python verify/check_cockpit.py -- <file.obj>     # ATTR_cockpit* -> cockpit feature
blender -b --factory-startup --python verify/check_placement.py -- <file.obj>   # each batch where the file puts it
blender -b --factory-startup --python verify/check_where_anim.py -- <file.obj>  # which block a batch hangs under
blender -b --factory-startup --python verify/check_scale.py -- <file.obj>       # measured against the real aircraft
blender -b --factory-startup --python verify/xp2b_convert.py -- <file.obj>      # just the bridge, then print the warnings
blender -b --factory-startup --python verify/acf_import.py -- <file.acf>        # one aircraft, every object it attaches
```

`acf_import.py` loads this working tree by path, so it grades the code you are
editing rather than whatever is installed; add `--no-xp2b` to skip the
conversion (it then also checks that the import says *why* there is no exportable
collection instead of failing quietly).

Two probes print instead of asserting, which is what you want when a real file
misbehaves:

```sh
blender -b --factory-startup --python scripts/import_warnings.py -- <file.obj>   # the convert warnings, verbatim
blender -b --factory-startup --python scripts/material_probe.py -- <file.obj>   # each material's wiring, chain by chain
```

`--factory-startup` loads no add-ons, so a test that needs XPlane2Blender must
enable it itself (`bpy.ops.preferences.addon_enable(module="io_xplane2blender")`)
or it will report "the file was imported, but there is no exportable collection".

## Releasing

1. bump `version` in `blender_manifest.toml`
2. `git tag -a v<same version>` — the tag message becomes the release notes — and
   `git push origin main v<same version>`

The `v*` tag runs `.github/workflows/blender-extension.yml`, which builds
`io_scene_xplane_obj-<version>.zip` with `blender --command extension build`,
validates it, keeps it as the `blender-extension` build artifact and attaches it
to a **draft** GitHub release. Nothing is published automatically: review the
draft, publish it if you want it on GitHub, then send the same zip to
<https://extensions.blender.org/submit/>, where it appears once the moderation
team approves it.

What ends up in the zip is decided by `[build] paths_exclude_pattern` in the
manifest: the add-on modules, `LICENSE` and the README. Tests, docs, the dev
symlink helper and the CI config stay in the repository.

Tests are not part of the release build — run them yourself (see above).

## Installing a build locally

```sh
blender --command extension build --output-dir dist
blender --command extension install-file -r user_default --enable dist/io_scene_xplane_obj-<version>.zip
```

Then restart Blender: extension modules are loaded at startup, so a reinstall
while it is open leaves the old code in memory.
