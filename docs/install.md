# Install

One add-on, one step. The exporter is **inside** the package — XPlane2Blender's
armature build, credited in [CREDITS.md](../CREDITS.md) — so there is no second
add-on to install and no version to match up. A round trip that used to need two
matching add-ons is now one install.

## Install

| how | what to do |
| --- | --- |
| the extension platform | Preferences > Get Extensions > search **X-Plane OBJ8**, or open [extensions.blender.org](https://extensions.blender.org/add-ons/io_scene_xplane_obj/) |
| a release zip | download `io_scene_xplane_obj-<version>.zip` from the releases page and **drag it into Blender** |
| from source | `git clone`, then `blender --command extension build --output-dir dist` and install the zip; `./dev-link.sh` symlinks the repo into Blender's user extensions instead, which is what you want while editing |

Blender 5.0 or newer.

### If you already have XPlane2Blender installed

Nothing to do. This add-on registers its bundled copy only when no standalone
XPlane2Blender is enabled — an existing one wins, so you keep the exporter you
know and you will not find two of every panel. The console says which one is in
use: `xplane_obj: a standalone XPlane2Blender is enabled - using it instead of the
bundled one`.

Removing the standalone one is optional and safe; the bundled copy takes over on
the next restart.

## Restart Blender

Add-ons load their modules at startup. Installing (or updating) one while Blender
is open leaves the **old code in memory** — Python edits and reinstalls only take
effect after a full quit and relaunch. That is the only reason a restart is
needed.

## The exact setup this is tested on

If something misbehaves, match these versions first.

| | |
| --- | --- |
| OS | macOS 26.5 (build 25F71), Apple silicon |
| Blender | 5.2.2 LTS, built 2026-09-15 01:49:19, hash `d13f752e3b9c` |
| X-Plane OBJ8 Import and Export | 0.11.10, installed as the extension `bl_ext.user_default.io_scene_xplane_obj` |
| Exporter | bundled XPlane2Blender **4.5.0 armature build** — tag `blender-5x-armature-v1` of `cpuwolf/XPlane2Blender`, commit `bbbf32e` |
| Test data | the aircraft X-Plane 12.4.3-r2 (build 124311, Apple silicon) ships |

Checking your own copies:

* **Blender** — the splash screen, or `blender --version`.
* **This add-on** — Preferences > Get Extensions > Installed (the panel also
  shows the permissions it asks for).
* **The exporter** — there is no separate version to look up: it ships with the
  add-on. Whether it registered is visible in the N-panel of the 3D viewport,
  which shows the X-Plane layer fields it exports from.
* **X-Plane** — `Log.txt` in the sim's folder, first line.

## Check it worked

* **File > Import > X-Plane OBJ8 (.obj)** exists, and so does **File > Export >
  X-Plane Object (.obj)**.
* Preferences > Get Extensions > Installed lists *X-Plane OBJ8 Import and Export*.
* Import any `.obj` from an aircraft, then export it: the written file carries
  its `ANIM_` blocks. If the export finishes and the file has no `ANIM_` line,
  the exporter did not register — the console has the reason, and the import says
  `XPlane2Blender is not installed/enabled` rather than pretending all is well.

## Uninstall

Preferences > Get Extensions: remove it. Nothing else is written outside the
`.blend` files you make — the add-on touches no network, no clipboard and no
camera, and it only reads the `.obj` / `.acf` files you open and writes the
`.obj` files you export.
