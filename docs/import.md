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
* **Everything lands under one collection named after the `.acf`.** The aircraft
  folder is the unit you manage, so the import builds `Cessna_172SP.acf` and puts
  every per-object exportable collection inside it — one fold-out in the outliner
  instead of 42 siblings. That parent is a plain collection, not an exportable
  one; XPlane2Blender walks collections recursively, so all 42 layers still export
  as before. Drop several `.acf` files at once and each gets its own parent (an
  object two of them share stays with the first, so nothing is imported twice).
* A listed object that is **not on disk** is reported as a warning and skipped —
  it does not stop the import.
* **The aircraft's cockpit object gets a green colour tag.** X-Plane only honours
  manipulators (clickspots) inside the cockpit objects, and the `.acf` says which
  ones they are: `_obja/<slot>/_obj_flags` bit 11 is the object drawn in the
  interior view, bit 12 the exterior one. Those bits are read, nothing is guessed
  from file names — five of Laminar's own are called `Cockpit_screens.obj`,
  `COCKPIT_PANEL.obj`, `cockpit_panel_interactive.obj` and the like. One aircraft
  may have two (b738, A330: `*_cockpit.obj` + `*_cockpit_OUT.obj`) or attach one
  file twice (F-4, C90B EVO); either way every tagged collection is a place a
  clickspot can live, and an untagged one cannot. The tag is a display-only
  outliner colour (`COLOR_04`) — it changes nothing about the export, the layer
  name still carries the file name.
* The `.acf` is **only read**. Laminar asks that add-ons not write the format,
  because it changes between sim versions, and this importer has no reason to.
* **It takes a while (13 to 113 objects), so it reports where it is**: the status
  bar reads `X-Plane .acf  12/42  <file>` with the progress bar moving, one object
  per tick, and **ESC** stops between two files — the objects that already landed
  stay in the scene. The step size is one `.obj`, so the one genuinely slow file
  (an A330 cockpit conversion is minutes on its own) still holds the window; the
  status bar then names the file it is on.

What is *not* taken from the `.acf`: the hide datarefs and anything a livery adds.
The reference offsets (`_v10_att_x/y/z_acf_prt_ref`) are read but never applied
during the import — they are a per-aircraft switch instead, because they must not
reach the exported vertices; see [placement](placement.md).
The objects come in at their own origin, exactly as if you had imported the
`.obj` files one by one.

## VR hotspots (`<acf>_vrconfig.txt`)

An aircraft's VR configuration is imported with it: **File > Import > X-Plane
Aircraft (.acf)** reads `<acf>_vrconfig.txt` from the same folder (that is where
and how X-Plane looks for it) and builds one collection beside the aircraft:

```
C90B_vrconfig.txt               plain collection, never exported
├ Pilot's Seat                  CAMERA at the eye point, aimed the way the seat looks
├ Pilot's Seat [AABB]           the teleport grab volume, drawn as a wire box
└ …
```

* The hotspot's `PRESET_XYZ` and `PSI`/`THE`/`PHI` become the camera's location
  and rotation, so **Numpad 0 is the view from that seat** - which is the one
  thing about a hotspot that cannot be read off the numbers.
* `AABB` becomes a box whose extents are the file's, drawn as a wireframe in
  Solid (material alpha does not show there) and as a translucent volume in a
  rendered view. It is the **grab** volume, not the seat: a file may put the head
  above it, and 12 of the 78 hotspots X-Plane 12 ships do.
* Coordinates are read the way the file means them: aircraft metres relative to
  the CG unless `REF_POINT_ACF` moves the origin (three forms: absent = current
  CG from the `.acf`, bare = the Plane-Maker authoring point, `Y Z` = the old CG).
  The origin actually used is stored on the collection and printed at import.
* Every `BEGIN_MANIP` block is resolved against the manipulators the imported
  objects carry, and the blocks that point at geometry the aircraft no longer has
  are reported (Laminar's CitationX ships three). Each resolved mesh is tagged
  with the VR overrides that apply to it (`vr_manips`).
* **Editable, and only what you moved is rewritten.** Move a camera, aim it, drag
  a box edge, then **Save VR Config** in the N-panel: the directives that
  changed are written back in the author's own style (their indent, their
  separator, their decimal places), and every other line - comments, `####`
  banners, block order, a file with no final newline, the untouched blocks - comes
  out byte for byte as it went in. A 178-line file with one hotspot moved comes
  back as a 2-line diff.
* It **refuses to save** when the file changed on disk since the import
  (an aircraft updater, another editor): reload the aircraft instead of writing a
  stale scene over someone else's work.
* **The manipulator parameters are editable too, on the object that carries
  them**: Properties > **Object** > **VR manipulators**, right under
  XPlane2Blender's own X-Plane panel.  A `BEGIN_MANIP` block is keyed by an
  `ATTR_manip_*` line *inside a mesh*, so it is listed on that mesh: the block's
  type and command/dataref, the file it came from, `DEG_PER_ACTION`,
  `SWITCH_THRESHOLD` (tilt in degrees, then motion in metres), the `HOLD_MANIP` /
  `INVERT_MANIP` / `WRAP_MANIP` / `DISABLE_VISUALS` flags and the raw `ACTION`
  line.  Ticking a flag adds its line, unticking removes it, and the numbers are
  rewritten in the author's own decimal places - so moving one parameter is a
  one-line diff.  **Unticking a box takes that directive out of the file** - it
  is not "forget my edit" - and clearing the `ACTION` field removes that line,
  which is a legal thing to do (the block's own type is then the whole story).
  **Save VR Config** (here, or in the 3D view's VR panel) writes it all,
  together with any hotspot you moved.
* Importing the same aircraft again **replaces its VR collection** instead of
  stacking a second one: hotspot cameras do not double, and the manipulator rows
  are rebuilt from the new import.  Edits you had not saved are discarded (the
  console says how many).
* Importing the same aircraft again **replaces its VR collection** instead of
  stacking a second one: hotspot cameras do not double, and the manipulator rows
  are rebuilt from the new import.  Edits you had not saved are discarded (the
  console says how many).
* Note that this is **not one block per mesh**: the file customises a subset of
  the manipulators (261 of 322 distinct `ATTR_manip_*` lines in a C90B are not
  mentioned at all), a block can cover several batches (a yoke split into three),
  and a file can carry the same key twice. The list is keyed on the block, and
  each row names the meshes it resolved to.
* Everything it creates is **blue**: Blender's `COLOR_05` tag on the collection
  (the stripe in the outliner) and the same `#3887c7` as the object colour, so
  Solid > Color > Object shows the whole VR layer apart from the aircraft.
* Hotspots are in the aircraft frame, so they line up with the cockpit once
  *Place by .acf* is on. Without it each `.obj` sits at its own file origin and
  the import says so.

## What a long `.acf` import does to the scene

The `.acf` import runs one object per UI tick so the status bar can show progress
and `ESC` can stop it.  That means it builds the scene **over time**, and
anything that rolls the scene back while it runs (an undo applied mid-import)
takes the objects it has already imported with it.  The import survives that - it
carries on into a fresh aircraft collection and says how many collections were
lost - but the aircraft will be short, so run the import again.  Nothing holds a
Blender datablock across a tick for this reason.

Every run leaves one line in
`~/Library/Application Support/Blender/<version>/config/xp8_import.log`
(`%APPDATA%\Blender Foundation\Blender\<version>\config` on Windows), and a
failed step or a lost collection is written there in full.  That file plus
Blender's `crash.txt` is what to attach to a bug report.

## Geometry

* One **mesh per geometry command** (`TRIS` / `LINES`): a 21-part fuselage is 21
  objects you can hide, edit and join, instead of one welded soup.
* Every `VT` row's authored normal is kept as a **custom split normal** —
  X-Plane stores lighting normals per vertex and they are not the averaged face
  normals Blender would invent.
* **One Blender vertex per `VT` row** by default, which is what the file says.
  OBJ8 stores a row per triangle *corner*, so a hard edge or a texture seam
  repeats the same position: a Laminar cockpit lands 39 % more vertices than it
  has distinct points, and moving one of them leaves a crack. *Weld vertices by
  position* builds one vertex per position instead — UVs and normals are
  per-corner in Blender, so they are written from the source rows either way and
  the mesh looks the same. What the weld costs, exactly, on the C90B cockpit:
  * the **28 triangles of 25,399 whose corners share a position** have no area;
    welding collapses them into a face with a repeated vertex and Blender drops
    it (the import reports them),
  * sharing a vertex between corners with **different** normals makes Blender
    store those as int16 offsets against the averaged vertex normal, so they can
    move by up to **0.33°** (measured max; invisible in the sim),
  * and because XPlane2Blender's vertex optimizer compares exact floats, that
    drift can grow the exported `VT` table slightly (25,558 → 26,713 rows here).
  It is off by default, and `verify/weld_test.py` grades it on a fixture and on
  any real file.
* **Winding is flipped** on both sides: X-Plane calls a *clockwise* triangle the
  front face, Blender a counter-clockwise one. Skipping this makes every model
  render inside-out — no error, just invisible surfaces.
* `LINES` come in read-only; `LIGHT_PARAM` / `LIGHT_NAMED` primitives are kept as
  raw commands in the file text (see [known limits](known-limits.md)).

## Lights

A `LIGHT_PARAM` line becomes a **Blender light that looks like the lamp**, not a
white 10 W point: the parameters are read through `xplights.py` and the tint,
power, cone and aim come from them (`R G B` → colour, `INTENSITY` in candela or
`SIZE` in metres → power, `WIDTH` = cos(half cone) → `spot_size`, `DX DY DZ` →
where the object's −Z points).

None of that reaches the export. XP2B writes a param light as
`LIGHT_PARAM <name> <x> <y> <z> <params verbatim>` and reads neither the colour
nor the power; what it *does* read is the object's aim, and only to correct it:
a directional light whose Blender direction disagrees with `DX DY DZ` gets
wrapped in a static `ANIM_rotate`. So the aim is set to exactly the direction the
line already names, the correction angle is zero, and the file comes back
byte-identical — `verify/lights_test.py` is the gate for that, and
`verify/export_snapshot.py -- <in.obj> <out.txt>` is how to diff a real aircraft.

Two deliberate choices:

* a **billboard** (`_bb`, `_core`, `_glow`, `_flare`, `_size`, `_flash`, `_rotate`,
  `_strobe`) is a glow quad, not a lamp, so it imports with **0 W**. The paired
  spill at the same position is the thing that lights the scene; giving both
  power doubles every light on the aircraft.
* a light name with no row in `xplights.SPECS` (a custom `lights.txt` name, e.g.
  `spot_params_sp`) is **not guessed**: it keeps Blender's defaults and the
  import reports the names once, per file.

Cockpit lights dim the way the sim's do. `airplane_panel_sp` and
`airplane_inst_sp` carry an `INDEX`, so each one gets a driver on its power that
reads a scene property - `xp8_panel_<INDEX>` and `xp8_inst_<INDEX>`, one per
index the file actually uses (0-3 in Laminar's aircraft, and a file may carry
more), under *Scene Properties > Custom Properties*, all defaulting to 1.0. Set one to 0 and that bank goes dark in the viewport and in renders; the
export does not move, because a parameter light's power is never written. From a
script, call `scene.update_tag()` after writing the property - a plain
`scene[key] = x` assignment does not re-evaluate the driver.

Brightness is a display mapping, not photometry — read the constants at the top of
`xplights.py` if the imported aircraft looks too dim or too hot.

A light inside an `ATTR_LOD` range is tagged with that bucket (`override_lods`
plus `lod[i]`), exactly like geometry: XP2B writes a draw command only for the
buckets an object carries, so a file with a single `ATTR_LOD` line used to export
with no lights *and no `TRIS`* at all — vertices and nothing else. An object the
file puts outside every range draws at all distances, which is reproduced with
the widest bucket so its line is not written twice.

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
