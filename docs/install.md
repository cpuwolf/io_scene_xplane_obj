# Install

Two pieces, in this order. The second one imports; the first one exports.

## 1. XPlane2Blender

The export side is [XPlane2Blender](https://github.com/X-Plane/XPlane2Blender)'s
own operator, so it has to be installed first.

> **On Blender 5.x the official XPlane2Blender does not work — use the armature
> build below, nothing else.** Releases up to 4.5.0 still read `Action.groups`
> and `Action.fcurves`, which Blender 5 removed, and it fails *silently*: the
> export finishes and writes no `ANIM_` block at all, so the aircraft ships with
> every part frozen and no error to explain it. The import in this add-on warns
> once when it recognises that build.

**Blender 4.2 – 4.5** — the official build works: Preferences > Get Extensions >
search *XPlane2Blender*.

**Blender 5.x** — there is no extension build of it yet, so use the armature
build:

```
https://github.com/cpuwolf/XPlane2Blender/archive/refs/tags/blender-5x-armature-v1.zip
```

That URL is a **source archive**, and Blender refuses it as-is — its root is the
whole repository, so the install stops with `ZIP packaged incorrectly;
__init__.py should be in a directory`. Repackage the add-on folder inside it,
then install *that* zip:

```sh
unzip -q XPlane2Blender-blender-5x-armature-v1.zip
cd XPlane2Blender-blender-5x-armature-v1
zip -qr ../io_xplane2blender.zip io_xplane2blender
```

**Edit > Preferences > Get Extensions > ⌄ > Install from Disk…** →
`io_xplane2blender.zip`, then enable **XPlane2Blender** in the add-on list. It
lands as a legacy add-on (`scripts/addons/io_xplane2blender`), which is expected
while 5.x has no extension build.

## 2. X-Plane OBJ8 Object (this add-on)

Any of these:

| how | what to do |
| --- | --- |
| the extension platform | Preferences > Get Extensions > search **X-Plane OBJ8**, or open [extensions.blender.org](https://extensions.blender.org/add-ons/io_scene_xplane_obj/) |
| a release zip | download `io_scene_xplane_obj-<version>.zip` from the releases page and **drag it into Blender** |
| from source | `git clone`, then `blender --command extension build --output-dir dist` and install the zip; `./dev-link.sh` symlinks the repo into Blender's user extensions instead, which is what you want while editing |

Blender 4.2 or newer.

## Restart Blender

Both add-ons load their modules at startup. Installing (or updating) an add-on
while Blender is open leaves the **old code in memory** — Python edits and
reinstalls only take effect after a full quit and relaunch.

## The exact setup this is tested on

If an import misbehaves, match these versions first: a different XPlane2Blender is
the most common reason for an import that half-works.

| | |
| --- | --- |
| OS | macOS 26.5 (build 25F71), Apple silicon |
| Blender | 5.2.2 LTS, built 2026-09-15 01:49:19, hash `d13f752e3b9c` |
| XPlane2Blender | **4.5.0**, armature build — tag `blender-5x-armature-v1` of `cpuwolf/XPlane2Blender`, commit `bbbf32e` |
| X-Plane OBJ8 Object | 0.10.4, installed as the extension `bl_ext.user_default.io_scene_xplane_obj` |
| Test data | the aircraft X-Plane 12.4.3-r2 (build 124311, Apple silicon) ships |

Checking your own copies:

* **Blender** — the splash screen, or `blender --version`.
* **XPlane2Blender** — Preferences > Add-ons, the version next to the name.
* **This add-on** — Preferences > Get Extensions > Installed (the panel also shows
  the permissions the add-on asks for).
* **X-Plane** — `Log.txt` in the sim's folder, first line.

## Check it worked

* **File > Import > X-Plane OBJ8 (.obj)** exists.
* Preferences > Add-ons lists both *XPlane2Blender* and *X-Plane OBJ8 Object*.
* Import any `.obj` from an aircraft: if XPlane2Blender is missing you get
  `XPlane2Blender is not installed/enabled: the file was imported, but there is
  no exportable collection for its exporter to write`.

## Uninstall

Preferences > Add-ons: remove both. Nothing else is written outside the
`.blend` files you make — the add-on touches no network, no clipboard and no
camera, and only reads and writes the `.obj` files you open.
