# What the import does

**File > Import > X-Plane OBJ8 (.obj)**, tick *Import whole folder* to bring in a
whole `objects/` directory, or drag `.obj` files from Explorer/Finder onto the
3D viewport (the add-on registers a `.obj` file handler, one file or a
multi-selection).

Drop an aircraft's **`.acf`** and every object that aircraft attaches is
imported — see [the whole aircraft](#the-whole-aircraft-from-an-acf) below.

## One file, one tree, named after the file

```
C90B_cockpit.obj                 collection — the exportable XPlane2Blender layer
└ C90B_cockpit.obj.worldcenter   Empty — move this to move the whole object
  └ C90B_cockpit.obj.bones       armature — one bone per ANIM block
    └ C90B_cockpit_000 …         one mesh per geometry command
```

* The extension is part of the label, so five files imported from one folder give
  five collections you can tell apart. Importing the same file twice gives
  `…obj.001` and a second Empty of its own — two imports never merge.
* The Empty and the armature are both objects, so they cannot share the file's
  name; `.worldcenter` and `.bones` name what they are.
* Meshes keep the bare stem (`C90B_cockpit_000`) because that name is the stable
  per-batch identity, and it carries the batch index in `xp8_leaf`.
* The XP2B *layer* keeps the bare stem too: XPlane2Blender turns the layer name
  into the exported file name, so it stays `C90B_cockpit` → `C90B_cockpit.obj`.

## The whole aircraft, from an `.acf`

**File > Import > X-Plane Aircraft (.acf)** — or drop the `.acf` itself.
The `.acf` is X-Plane's text aircraft file; every attached object is one line in
it, and each becomes the same tree a hand-dropped `.obj` gives:

```
_obja/0/_v10_att_file_stl ../Cessna_172SP_cockpit.obj
```

* **Objects are found the way X-Plane finds them**: relative to the aircraft
  folder, then to its `objects/` subfolder. Both are needed in real files —
  `../X.obj` reaches *out of* `objects/`, `X.obj` reaches *into* it, and a
  resolver that only joins against the `.acf`'s own folder finds almost nothing
  (Laminar C172: 0 of 42).
* **Slot order is import order.** The file writes its properties alphabetically
  (`_obja/10` before `_obja/2`); the slot number is the order Plane-Maker
  attached them in, so that is what is used.
* **The same `.obj` in two slots is imported once.** Aircraft attach one file to
  several slots, and two `.acf` files of one family (a base and a `_seaplane`
  variant) share objects; a second copy would be an identical collection nothing
  refers to.
* A listed object that is **not on disk** is reported as a warning and skipped —
  it does not stop the import.
* The `.acf` is **only read**. Laminar asks that add-ons not write the format,
  because it changes between sim versions, and this importer has no reason to.
* **The scene camera is moved to the cockpit viewpoint first**, because X-Plane
  builds every aircraft around the same axes: location `(-4.4895, 5.0562, 2.4779)`
  m, rotation `(76.327°, 0, -149.23°)` XYZ Euler, scale 1. The active camera is
  used, else the first camera in the scene (made active); a scene without a camera
  gets none created. Untick *Place the camera* in the import dialog to leave it
  alone — a dropped `.acf` always places it.
* **It takes a while (13 to 113 objects), so it reports where it is**: the status
  bar reads `X-Plane .acf  12/42  <file>` with the progress bar moving, one object
  per tick, and **ESC** stops between two files — the objects that already landed
  stay in the scene. The step size is one `.obj`, so the one genuinely slow file
  (an A330 cockpit conversion is minutes on its own) still holds the window; the
  status bar then names the file it is on.

What is *not* taken from the `.acf`: the per-object attach offsets and rotations
(`_v10_att_x/y/z_acf_prt_ref`), the hide datarefs, and anything a livery adds.
The objects come in at their own origin, exactly as if you had imported the
`.obj` files one by one.

## Geometry

* One **mesh per geometry command** (`TRIS` / `LINES`): a 21-part fuselage is 21
  objects you can hide, edit and join, instead of one welded soup.
* Every `VT` row's authored normal is kept as a **custom split normal** —
  X-Plane stores lighting normals per vertex and they are not the averaged face
  normals Blender would invent.
* **Winding is flipped** on both sides: X-Plane calls a *clockwise* triangle the
  front face, Blender a counter-clockwise one. Skipping this makes every model
  render inside-out — no error, just invisible surfaces.
* `LINES` come in read-only; `LIGHT_PARAM` / `LIGHT_NAMED` primitives are kept as
  raw commands in the file text (see [known limits](known-limits.md)).

## Materials

The texture set from the file is used twice, on purpose:

| where | what it is for |
| --- | --- |
| the Blender **material** | the viewport preview: base colour, emission, blend, and the normal map wired as the combo map it really is |
| the XP2B **layer** (Collection > X-Plane > Textures) | what the exporter actually writes: Default / Night / Normal-Specular, and on 12.0+ Normal / Material-Gloss / Gloss, plus *Normal Metalness* and *Blend Glass* |

XP2B does not read texture paths out of a node tree, so the layer fields are the
ones that matter for export; the material graph is what you look at.

An X-Plane normal map is a **combo map**, not a normal map:

* `R,G` → a Normal Map node behind a rebuilt `Z = 1` (the file stores no Z when
  the channels are taken by material data — feeding the raw blue in makes every
  surface light inside-out);
* blue → **Metallic**, but only in *metalness* mode, i.e. when the file declares
  `NORMAL_METALNESS`;
* alpha → **gloss**, inverted into Roughness;
* `TEXTURE_MAP normal` / `material_gloss` (XP 12.1) is read the same way from its
  two files;
* a `TEXTURE_NORMAL` **without** `NORMAL_METALNESS` is an XP10/11 file whose RGB
  is the whole vector and whose alpha is specular strength, so that one keeps a
  plain image → Normal Map link.

The image node is set to **Non-Color**; an sRGB decode on a vector texture
distorts the maths. Material clones are shared per state combination, so a
cockpit that would land 163 materials lands 5.

## Files the sim loads but the spec does not promise

Both are tolerated, because X-Plane tolerates them:

* a file with **no `POINT_COUNTS`** (mandatory in the spec, dropped by some
  exporters) imports with a warning that names the sizes used, and the record is
  inserted on the way out — an untouched import stays byte-identical apart from
  that one line;
* a **trailing `#` comment** on a data or command line (`VT … # 12`) is stripped
  for the numbers and kept in the raw line. A `#` inside a token — a texture
  called `livery#2.png` — is not a comment.

## Where the X-Plane data lives

The file's own text is kept on the **collection** as custom properties: that is
the ground truth a write-back replays, and nothing you do to the meshes can
damage it.

| property | contents |
| --- | --- |
| `xp8_dump` | the whole original file; the conversion re-reads it for the command stream |
| `xp8_events` | ordered command stream: `["A"]` ANIM_begin, `["Z"]` ANIM_end, `["R", line]` any command verbatim, `["L", n]` geometry batch `n` |
| `xp8_globals` | `TEXTURE` / `POINT_COUNTS` / `GLOBAL_*` lines |
| `xp8_leaf_states` | the `ATTR_*` snapshot each batch draws under |
| `xp8_leaf_objs` | batch index → object name |
| `xp8_fingerprints`, `xp8_cmd_hash` | what the "was anything changed?" test compares |
| `xp2b_convert_warnings` | what the conversion to XPlane2Blender could not carry |
| `xp8_leaf` (on a mesh) | which batch it is |
| `xp8_anim_vis` (on a mesh) | the `ANIM_show/hide` rules of its block, in file order |
| `xp8_vt` (mesh attribute) | source `VT` row index per vertex, so untouched rows keep their exact text |

The conversion to XPlane2Blender reads the text from `xp8_dump`, so it is what
the scene was built from; the properties stay afterwards for reference and for
scripts that re-parse it.
