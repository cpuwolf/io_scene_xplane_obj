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

Nothing is changed when building the wheel: it is the pinned commit byte for
byte. The fork (`cpuwolf/XPlane2Blender`) carries one fix the upstream release
does not — `xplane_ui` and `xplane_ops` read `context.active_object.xplane`
without a None guard, so **an empty scene threw on every redraw of the Render
panel** as soon as this exporter registered, reported by a user installing on
Windows with nothing open.

Everything else — operators, panels, properties, the updater, resources, tests —
is upstream code at the pinned commit.

## Test data

The aircraft used to verify the importer and the exporter are the ones X-Plane
12 ships; nothing from them is redistributed.
