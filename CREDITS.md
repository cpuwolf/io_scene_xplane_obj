# Credits

**X-Plane OBJ8 Importer / exporter** — © 2026 Wei Shuai. GPL-3.0-or-later.

## Bundled: XPlane2Blender

The export half of this add-on is [XPlane2Blender](https://github.com/X-Plane/XPlane2Blender),
carried inside this package as a wheel (`wheels/io_xplane2blender-4.5.0*.whl`)
so that one install imports and exports X-Plane objects — the extension platform
does not allow an add-on whose features depend on another add-on.

* Copyright © XPlane2Blender contributors: **Ted Greene, Ben Supnik, Amy Parent,
  Maya F. Eroğlu** — the same four names `blender_manifest.toml` credits, in the
  order upstream `bl_info` lists them. The upstream repository was created
  2010-08-10, which is the span the manifest uses per holder.
* Licence: the repository's `LICENSE` is the GPL v3 text (what GitHub reports as
  `GPL-3.0`), while the per-file header in `io_xplane2blender/` offers
  GPL-2.0-or-later. Both allow redistribution here under GPL-3.0-or-later; this
  package ships as `SPDX:GPL-3.0-or-later`, keeps every upstream file's own
  header untouched, and carries the upstream `LICENSE` inside the wheel.
* Source: `cpuwolf/XPlane2Blender` (XPlane2Blender 4.5.0 plus the Blender 5.x
  armature work), tracked here as the submodule `vendor/XPlane2Blender` and
  pinned to the commit recorded in `.gitmodules` — that pin, not a copy inside
  this repository, is the authoritative revision and it keeps upstream's own
  history. The wheel records the commit it was built from in its `METADATA`
  (`X-Vendored-Commit`).

Because the wheel keeps the upstream top-level package name, the code travels
unmodified: no imports are rewritten and no module paths are redirected.

Exactly one change is made when building the wheel (`PATCHES` in
`scripts/build_wheel.py`), and it is a crash rather than a style fix:

* `xplane_ui.RENDER_PT_xplane` reads `context.active_object.xplane` with no None
  guard, so **an empty scene throws on every redraw** as soon as this exporter is
  registered — reported by a user installing on Windows with nothing open. One
  `is not None`; reported upstream and to be dropped from `PATCHES` as soon as
  the pinned commit carries it. The build fails loudly if that line ever moves,
  so an upstream edit cannot ship a wheel that is silently unpatched.

Everything else — operators, panels, properties, the updater, resources, tests —
is upstream code at the pinned commit.

## Test data

The aircraft used to verify the importer and the exporter are the ones X-Plane
12 ships; nothing from them is redistributed.
