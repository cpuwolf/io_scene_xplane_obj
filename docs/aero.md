# The aero airframe: the bodies and wings the flight model measures

X-Plane does not fly the OBJ8 you see — it flies the shapes on Plane Maker's
**Bodies** and **Wings** screens. They live in the `.acf` as point grids, and they
are what the `.acf` import brings in as an `Aero` collection.

## What is in the file

```
P _body/<n>/_geo_xyz/<i>,<j>,<k>   one point, feet
P _wing/<n>/_geo_xyz/<i>,<j>,<k>   one point, feet
P _body/<n>/_r_dim                 points around one cross-section (max 18)
P _body/<n>/_s_dim                 sections used (max 20)
P _body/<n>/_part_x/_y/_z          front tip of the body, feet
P _body/<n>/_part_the/_psi/_phi    pitch / heading / roll, degrees
P _body/<n>/_part_cd               coefficient of drag, on the frontal area
P _body/<n>/_part_area_rule        area rule ratio (transonic/supersonic)
P _body/<n>/_descrip               "Fuselage", "Spinner", "Left Wheel Fairing", …
P _wing/<n>/_semilen_SEG / _Croot / _Ctip / _sweep_design / _dihed_design
P _wing/<n>/_is_right_mult         -1 for the left-hand slot, +1 for the right
```

- `<i>` walks along a body / across a span, `<j>` around the section, `<k>` =
  0/1/2 is x / y / z. Airplane axes: **x right, y up, z aft**, in feet.
- The array is always **20 × 18 slots**, but only the first `_s_dim` sections and
  `_r_dim` section points are live. **The rest is stale**: the Lancair Evolution
  zeroes it, a Cessna 172 leaves three duplicated tail rings behind. Read the
  whole array and the Lancair's side area comes out 18.50 m² instead of the
  7.81 m² the sim prints.
- A **body** is stored in its own frame: add `_part_x/_y/_z` plus the
  pitch/heading/roll (a Cessna's spinner sits at z −4.94 ft, which is its grid's
  −1.22 plus the part's −3.72).
- A **wing** is stored **already in the aircraft frame**, with dihedral, sweep and
  incidence baked in, and each side is its own slot: the Cessna's slot 0 spans
  x −8.60..0 and slot 1 spans 0..8.60. Adding `_part_*` to a wing would double the
  root offset and stretch the span to 39 ft instead of the real 36.
- A body can be attached to an engine or a gear leg (`_engn_for_body`,
  `_gear_for_body`), which is how a spinner or a wheel fairing moves with it.

## What the sim computes from it

`Settings > Output One Flight Model Cycle` (or `Cycle Dump.txt` in the X-Plane
folder) prints this per body — and these formulas reproduce it:

| printed | from the grid |
|---|---|
| frontal area | the largest cross-section (ring area in x-y) |
| side area | ½ Σ \|quad projected on (z, y)\| |
| top area | ½ Σ \|quad projected on (z, x)\| |
| wetted area | Σ triangle areas (→ skin friction) |
| lateral / vertical / lift centroids | area centroids of the same projections |

Measured against the Lancair Evolution's own dump — every number to four
decimals:

| body | stations × ring | frontal | side | top | wetted |
|---|---|---|---|---|---|
| Body 1 fuselage | 11 × 10 | 1.6033 | 7.8124 | 6.6464 | 23.9581 |
| Body 2 engine fairing | 6 × 10 | 0.1317 | 0.6998 | 1.1839 | 3.7115 |
| Body 22 exhaust stacks | 5 × 10 | 0.2607 | 0.0880 | 0.4737 | 1.3520 |

Drag on the body is `_part_cd` × frontal area (Plane Maker: ~0.1 for an average
fuselage, 0.025 for a very sleek one), and the wetted area feeds friction, so
reshaping a fuselage changes both. The runtime side of it:
`sim/aircraft/bodies/acf_fuse_cd`, `…/acf_fuse_cd_array` (one per body) and
`sim/aircraft2/body/kill_body`.

For a wing the same projection gives the planform, which is what the dump's
"wing area" is: the Lancair's four right-hand panels project to **6.434 m²**
against the **6.46 m²** it reports for the joined wing (the difference is the
overlap the sim adds when it joins the sections), and its root chord comes out at
5.75 ft against the 1.75 m (5.74 ft) it prints. That holds for a wing whose grid
is still fresh — see the next section for what happens when it is not.

## A wing grid is the stored shape, not the shape that flies

The sim does not fly a wing's `_geo_xyz` grid. It flies the parameters: `_Croot`,
`_Ctip`, `_sweep_design`, `_dihed_design`, the per-rib tables (`_chord_for_RIB/<i>`,
`_c_rat_for_RIB/<i>`, `_incidence/<i>`, `_inc_trim/<i>`) and the airfoils named on
the Wings screen.

Measured on a Cessna 172: change Wing 1's tip chord in Plane Maker (5.20 → 4.60 ft),
save, fly it, dump the cycle.

| | wing 1 area | joined wing |
|---|---|---|
| `Cycle Dump.txt` | **3.1866 m²** | 7.0662 m² |
| from the parameters — 7.00 ft × (5.20 + 4.60) / 2 | **3.1866 m²** | 7.0699 m² |
| from the grid — 7.00 ft × 5.193, untouched by that edit | 3.3771 m² | 7.2574 m² |

Plane Maker changed no `_wing/*/_geo_xyz` point at all when it saved that edit —
only `_Ctip`, the rib table and `_average_mac_acf`. So a saved `.acf` can carry a
wing grid that no longer matches its parameters, and it keeps carrying it: the
grid is a picture of the last time something regenerated it, not a live copy of
the wing.

What follows for the import:

- the wing mesh is worth having — it is the shape Plane Maker shows and the only
  record of how the section curves were placed — but it is **not** evidence of
  what the sim flies. Read `_Croot` / `_Ctip` / the rib table for that;
- **do not edit it**: nothing in Plane Maker reads it back, so a change there
  cannot reach the flight model. The `Aero` collection says so in a custom
  property, `acf_aero_source`;
- **bodies are the opposite**: a body has no parameters to fall back on, its grid
  *is* the aerodynamic data (`_part_*` plus the sections) — which is why the areas
  in the table above reproduce the dump to four decimals. A body is editable; a
  wing panel is a picture.

Two ways to read a wing grid wrongly, both of which raised false alarms across
the stock fleet while working this out:

- **dihedral**: take the slope of the section's centroid. Reading it at the
  quarter-chord point mixes in the twist (`_incidence`) — the 737's wing measures
  2.26° against its 6.0° parameter that way, 6.37° at the centroid;
- **tip chord**: the last station is often a **tip cap**, not the wing tip. The
  PA-18's first 17 stations are 5.243 ft and the 18th is 2.100 ft, so taking the
  last station as the tip chord makes a rectangular wing look 60 % wrong.

## A body on a gear leg is placed by the gear

`_body/<n>/_gear_for_body` ties a fairing to a gear leg (a wheel pant, a skid
fairing). Plane Maker puts such a body where the leg ends, not where `_part_*`
says — and it does not rewrite `_part_*` when the gear changes, so the stored
numbers can be badly out of date:

| aircraft | stored `_part_*` | the gear's wheel | drift |
|---|---|---|---|
| Cessna 172, mains | −4.240 −3.010 4.340 | −4.116 −3.478 4.458 | 0.19 ft |
| Van's RV-10, mains | −3.460 −3.140 0.730 | −4.199 −3.656 0.314 | 0.74 ft |
| Cirrus SR22, mains | −5.960 −3.000 1.400 | −4.518 −3.486 1.214 | 1.44 ft |
| Robinson R22, skids | 0.000 0.000 0.000 | ∓2.850 −3.629 −4.500 | 4.50 ft |

The wheel is the leg vector from `_gear_x/_y/_z`, `_leg_len`, `_latE` and `_lonE`:

```
wheel = attach + leg * ( sin(latE),
                        -cos(latE) * cos(lonE),
                        -cos(latE) * sin(lonE) )
```

Checked against the imported models of those four aircraft: the nearest tyre
island sits 0.08 ft (Cirrus), 0.20 (Cessna), 0.43 (Van's) and 0.35 (R22) from
that point, so it is the wheel within the model's own slack.

So the import takes the wheel whenever the stored position disagrees with it by
more than `GEAR_SLOP` (0.5 ft) and leaves the file alone when it does not: the
Cessna keeps its authored positions, the Cirrus' mains and the R22's skids land
on their wheels. Each re-seated object carries `acf_aero_gear` saying what
happened and what the file said, and the import summary counts them.

⚠️ The fairing's *own* offset from the wheel is not recoverable — `_part_*` was
the only record of it, and it is exactly the thing that goes stale. Putting the
grid origin on the wheel matched the SR22's pant to 0.2 ft, which is as close as
the file alone allows. That is why this is a re-seat with a threshold, not a
claim to reproduce Plane Maker's own placement exactly.

The import draws that line as well: every leg becomes a tapered tube from its
attach point to its wheel, named `Gear 1 strut`, … in a `Gear` child collection
inside `Aero`, so a fairing that has just been re-seated on its wheel does not
look like it is floating. The `.acf` carries no leg radius — the tube's fat end
is 6 % of the leg length, capped at 0.30 ft — so it is a diagram of where the leg
runs, not a model of it. Nothing in it is exported.

## What the import builds

One mesh per body and per wing panel, in an `Aero` collection under the aircraft,
plus one `Gear <n> strut` tube per leg in its `Gear` child collection:

- `Body 1 Fuselage`, `Wing 1 R`, `Wing 2 L`, `H-Stab 1 R`, `V-Stab 1`, … — the
  names are Plane Maker's (slots 0..7 are the four wing sections, left/right;
  8/9 the horizontal stabiliser, 10/11 the two vertical ones);
- metres, mapped to Blender `(x, -z, y)` — the same turn the OBJ8 importer makes,
  so it sits on top of the imported model;
- a body's `_part_*` position and pitch/heading/roll are baked into its points; a
  wing's grid is used as it stands (and may be older than the wing it describes —
  see above). Either way every object has an identity
  transform (nothing for the exporter to do, nothing for a later placement switch
  to undo);
- wireframe in Solid view, translucent when rendered (`show_in_front`), so the
  visible model shows through;
- **not an exportable collection** — these are aerodynamic geometry, not an `.obj`
  the sim attaches, so XPlane2Blender never writes them;
- skipped by *Place by .acf*: the `.acf`'s attachment offset belongs to attached
  objects, and these coordinates are already the aircraft frame.

`acf_aero.py` in `verify/` checks all of the above, including the wetted-area
match against `Cycle Dump.txt`, the planform total, the pitch/heading offsets on a
Baron (2° nose up) and an Aerolite (90° heading), and that a vertical stabiliser
comes out vertical.
