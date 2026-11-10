# Development and releasing

## Working on it

```sh
./dev-link.sh            # symlink this repo into Blender's user extension folder
                         # then enable "X-Plane OBJ8 Importer" in Preferences
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

**The `.acf` import is modal, and `-b` cannot run a modal.** Background mode has a
`Window` object but no event loop, so `context.window is None` is False there and a
modal handler would import exactly one object and stall - the operator therefore
drains synchronously when `bpy.app.background` is set, which is what
`acf_import.py` exercises. To grade the modal path (status text, progress bar, ESC
cancel) it has to be a **GUI** session: wrap `IMPORT_OT_xplane_acf.modal` to hand
the real handler a stand-in `type='ESC'` event after N objects, wrap `_finish` to
record `(result, made, total, done, stopped)`, and drive a `bpy.app.timers`
poller that prints the exportable-collection count and calls
`bpy.ops.wm.quit_blender()` when the run ends. Measured that way on the SR22:
`RUNNING_MODAL` immediately, collections 2 → 3 → 5 → 7 → 11 → 13 over 23 s, then
`FINISHED` 13/13; with the injected ESC, `CANCELLED` at 3/13 and those three
collections stay.

Two probes print instead of asserting, which is what you want when a real file
misbehaves:

```sh
blender -b --factory-startup --python scripts/import_warnings.py -- <file.obj>   # the convert warnings, verbatim
blender -b --factory-startup --python scripts/material_probe.py -- <file.obj>   # each material's wiring, chain by chain
```

`--factory-startup` loads no add-ons. The exporter ships inside this package as a
Python wheel (`wheels/io_xplane2blender-*.whl`), and it is registered by this
add-on's own `register()`, so a script that loads the repository by path (the
`verify/` and `scripts/` probes do) needs the wheel's site directory on
`sys.path` to import it; one that only enables the *standalone* legacy add-on is
testing that copy instead, and without either it reports "the file was imported,
but there is no exportable collection".

## The bundled exporter (`vendor/XPlane2Blender` -> `wheels/*.whl`)

The export half is XPlane2Blender's armature build, bundled because the platform
forbids a feature that depends on another extension (ToS 5.2/5.3, "even if it is
optional"). Its *history* stays in its own repository: the fork is a submodule at
`vendor/XPlane2Blender`, pinned in `.gitmodules`, so `git log` there is the real
one and this repository only records which commit it shipped.

What ships is a **wheel**, declared with the manifest's top-level
`wheels = [...]` key and unpacked by Blender into
`extensions/.local/lib/pythonX.Y/site-packages`, which sits in `sys.path` ahead
of `scripts/addons/`. The package therefore keeps its upstream top-level name
`io_xplane2blender`, and the code goes into the wheel as upstream writes it — no
import rewriting, no attribute-walk fixes — apart from the single entry in
`PATCHES` in `scripts/build_wheel.py` (the empty-scene crash in
`xplane_ui.RENDER_PT_xplane`, which the pinned commit does not fix yet). The build
dies, naming the file, if that snippet ever moves, so an upstream edit cannot ship
a silently unpatched wheel.

```sh
git submodule update --init                     # once, after cloning
python3 scripts/build_wheel.py                   # after any submodule change
git -C vendor/XPlane2Blender checkout <commit>   # to move the pinned commit
```

* **The wheel is a build artifact and is gitignored.** Regenerate it before every
  `extension build`; CI does it right after checkout. A zip built without it still
  passes `extension validate` and then fails to import in the user's session,
  which is what `verify/bundle_smoke.py` exists to catch.
* **Never edit the extracted copy** under `site-packages` — the next sync replaces
  it. A fix belongs in the submodule.
* **The name is global.** `.local/.../site-packages` is shared by every extension
  and wins over `scripts/addons/`, so a user who also enabled a standalone
  XPlane2Blender gets this copy imported under that name. `_xp2b_registered()`
  keeps us from registering twice; `__init__._xp2b_module_origin()` prints which
  copy ran, because a bug report needs that fact.
* **`tests/` ships inside the wheel.** `xplane_file.py` and
  `xplane_wiper_gradient.py` import `test_creation_helpers` at runtime; a wheel
  without it registers and then exports nothing.
* **CI runs the script** after `checkout (submodules: recursive)`, and
  `verify/bundle_smoke.py` is the gate that matters: it unzips the artifact,
  unpacks the wheel the way Blender does, registers the add-on, imports
  `verify/fixtures/nested.obj` and asserts the export carries `ANIM_` lines (25
  of them).

## The pinned test environment

Everything in this repository was verified on one setup; regressions are usually
a version skew away, so match it before believing a bug report.

| | |
| --- | --- |
| OS | macOS 26.5 (25F71), Apple silicon |
| Blender | 5.2.2 LTS, 2026-09-15, hash `d13f752e3b9c` |
| XPlane2Blender | 4.5.0 armature build, tag `blender-5x-armature-v1` (commit `bbbf32e`), installed as the legacy add-on `scripts/addons/io_xplane2blender` |
| X-Plane | 12.4.3-r2 (build 124311), `~/X-Plane 12` — the aircraft in `Laminar Research/` are the test files |

The armature build is archived outside this repository, because it is the only
XPlane2Blender that works on Blender 5.x and it exists only as a tag on a fork:

```
~/GitHub/xplane-addons-mirror/XPlane2Blender-blender-5x-armature-v1.zip
  29 MB, the untouched GitHub source archive of the tag
  sha256 c96050f8e7bb91325b1e6554da1c08ab9d9be7dfc52854b4939a678f0a69ae3f
~/GitHub/xplane-addons-mirror/io_xplane2blender.zip
  1.6 MB, the same code repackaged so Blender accepts it (root is
  `io_xplane2blender/__init__.py`; the source archive's root is the whole repo,
  which `addon_install` rejects)
  sha256 554297031c3062b1a5fd25fda675a20e5746659e4df36e410f93487e80692dbc
```

Neither can be rebuilt from this repository: keep both copies, and put them on a
release if the fork's tag ever goes away.

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
