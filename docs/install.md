# Install

Two pieces, in this order. The second one imports; the first one exports.

## 1. XPlane2Blender

The export side is [XPlane2Blender](https://github.com/X-Plane/XPlane2Blender)'s
own operator, so it has to be installed first.

**Blender 5.x** — the shipped releases are still on 4.x, so use the armature
build:

```
https://github.com/cpuwolf/XPlane2Blender/archive/refs/tags/blender-5x-armature-v1.zip
```

Download that zip, then in Blender: **Edit > Preferences > Get Extensions >
⌄ (top right) > Install from Disk…**, pick the zip, and enable **XPlane2Blender**
in the add-on list. It installs as a legacy add-on
(`scripts/addons/io_xplane2blender`), which is expected — 5.x has no extension
build of it yet.

**Blender 4.2 – 4.5** — the official build works: Preferences > Get Extensions >
search *XPlane2Blender*.

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
