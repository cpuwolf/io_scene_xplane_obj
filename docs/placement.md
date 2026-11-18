# Aircraft placement

X-Plane does not draw an `.obj` where its vertices say. It draws it wherever the
aircraft's `.acf` puts it:

```
_obja/3/_v10_att_file_stl            n844x_int1.obj
_obja/3/_v10_att_x_acf_prt_ref        0.000000
_obja/3/_v10_att_y_acf_prt_ref       -4.400000
_obja/3/_v10_att_z_acf_prt_ref       14.600000      <- feet
```

That is the part's reference point in aircraft coordinates, and it is why a model
imported at its own origin looks fine on its own but does not line up with
anything else that lives in aircraft coordinates — an FMOD `sound space` (see
[io_scene_xplane_snd](https://github.com/cpuwolf/io_scene_xplane_snd)), a
different tool's gizmo, or a second import of the same aircraft.

## What the add-on does

An `.acf` import builds one collection per aircraft, named after the `.acf`. That
collection remembers the offset the file declares (in feet, exactly as written),
and nothing else in the scene does. The offset is *not* applied while importing.

Sidebar (`N`) → **X-Plane** → **Aircraft placement** lists every imported
aircraft:

```
N844X.acf
offset   aft +14.60 ft   down +4.40 ft
[✓] Placed        ⚠ clear before exporting .obj
```

* `Placed` moves the whole aircraft — every top-level object of its collection,
  children follow — by that offset. Pressing it again puts everything back.
* An aircraft whose `.acf` declares no offset says
  `no placement offset in this .acf` and cannot be placed. On the aircraft we
  measured (Laminar C172, C90B, CitationX, F-14, F-4, ASK21, S-76, SF50) the
  value is zero; Lancair Evolution is `aft 14.6 ft / down 4.4 ft`, Van's RV-10
  `aft 1.6 ft / down 4.3 ft`.
* `Place by .acf` on the import operator does the same thing immediately after
  the import, for people who always want it.

`Object ▸ Clear ACF Placement` clears every placed aircraft at once. It is the
one command to reach for before exporting.

## Why the offset must be removed again before exporting

XPlane2Blender bakes an object's transform into the exported `VT` table
(`xplane_mesh.bakeMatrix` → `mesh.transform(...)`). A placed aircraft therefore
exports with the offset in its vertices — and the `.acf` still declares that same
offset, so X-Plane would apply it a second time and the part would sit twice as
far out.

So the two states are exclusive:

| state | viewport | `.obj` export |
| --- | --- | --- |
| `Not placed` | parts at their own origins | correct |
| `Placed` | matches X-Plane and other aircraft-coordinate tools | double offset |

Place to *look* and to *line things up*; clear before you export. The panel says
so, and the toggle reports it.

## Precision

The offset is read from the `.acf` text as written (6 decimal places, feet) and
applied as a pure translation; `_att_the/psi/phi_ref` rotations are carried too,
for the files that use them. Toggling is an exact inverse matrix multiply, so a
place/clear cycle returns every value bit-for-bit — `verify/acf_placement.py`
grades exactly that, plus the fact that an import never shifts anything by
itself.
