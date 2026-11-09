# Credits

**X-Plane OBJ8 Importer / exporter** — © 2026 Wei Shuai. GPL-3.0-or-later.

## Bundled: XPlane2Blender

The export half of this add-on is [XPlane2Blender](https://github.com/X-Plane/XPlane2Blender),
carried inside this package as `xplane2blender/` so that one install imports and
exports X-Plane objects — the extension platform does not allow an add-on whose
features depend on another add-on.

* Copyright © XPlane2Blender contributors: Ted Greene, Ben Supnik, Amy Parent,
  Maya F. Eroğlu.
* Licence: GPL-2.0-or-later (the per-file header in `xplane2blender/` says so);
  redistributed here under GPL-3.0-or-later, which GPL-2.0-or-later permits.
  The full text is in `xplane2blender/LICENSE` upstream; the licence block at the
  top of `xplane2blender/__init__.py` is the authoritative statement.
* Source: the `blender-5x-armature-v1` build of `cpuwolf/XPlane2Blender`, commit
  `bbbf32e`, which is XPlane2Blender 4.5.0 plus the Blender 5.x armature fix.

What was changed when vendoring it, and nothing else:

1. Imports that named the package absolutely (`from io_xplane2blender.x import y`)
   are relative (`from .x import y`), because the package now lives inside this
   one and must not borrow the standalone add-on's top-level name.
2. The six places that did `import io_xplane2blender` and then walked its
   attributes now import the module they use.
3. `tests/` keeps only the three modules the runtime imports; its own test
   fixtures are not shipped.

Everything else — operators, panels, properties, the updater, resources — is
upstream code at the commit above.

## Test data

The aircraft used to verify the importer and the exporter are the ones X-Plane
12 ships; nothing from them is redistributed.
