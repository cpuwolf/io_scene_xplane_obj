# Credits

**X-Plane OBJ8 Importer / exporter** — © 2026 Wei Shuai. GPL-3.0-or-later.

## Bundled: XPlane2Blender

The export half of this add-on is [XPlane2Blender](https://github.com/X-Plane/XPlane2Blender),
carried inside this package as `xplane2blender/` so that one install imports and
exports X-Plane objects — the extension platform does not allow an add-on whose
features depend on another add-on.

* Copyright © XPlane2Blender contributors: **Ted Greene, Ben Supnik, Amy Parent,
  Maya F. Eroğlu** — the same four names `blender_manifest.toml` credits, in the
  order upstream `bl_info` lists them. The upstream repository was created
  2010-08-10, which is the span the manifest uses per holder.
* Licence: the repository's `LICENSE` is the GPL v3 text (what GitHub reports as
  `GPL-3.0`), while the per-file header in `xplane2blender/` offers
  GPL-2.0-or-later. Both allow redistribution here under GPL-3.0-or-later; this
  package ships as `SPDX:GPL-3.0-or-later` and keeps every upstream file's own
  header untouched.
* Source: `cpuwolf/XPlane2Blender` (XPlane2Blender 4.5.0 plus the Blender 5.x
  armature work), tracked here as the submodule `vendor/XPlane2Blender` and
  pinned to the commit recorded in `.gitmodules` — that pin, not a copy inside
  this repository, is the authoritative revision and it keeps upstream's own
  history.

What is changed when packaging it, and nothing else — `scripts/vendor_bundle.py`
does both, so the submodule itself stays untouched:

1. Imports that named the package absolutely (`from io_xplane2blender.x import y`)
   are relative (`from .x import y`), because the package now lives inside this
   one and must not borrow the standalone add-on's top-level name.
2. The places that did `import io_xplane2blender` and then walked its attributes
   import the module they use instead.

Everything else — operators, panels, properties, the updater, resources, tests —
is upstream code at the pinned commit.

## Test data

The aircraft used to verify the importer and the exporter are the ones X-Plane
12 ships; nothing from them is redistributed.
