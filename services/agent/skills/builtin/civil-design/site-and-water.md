## 1. Establish the design basis and resume the actual site

1. Identify the working document, project limits, survey source/accuracy, projected
   metre CRS, vertical datum and local-grid parameters. Gather jurisdictional
   criteria, required operating/fire/storm cases, available supply heads, demands,
   pipe/soil properties, groundwater, outfalls and receiving-water tailwater.
   Resolve missing inputs with the user through the available question interface;
   never invent IDF data, geotechnical values, pump curves or market prices.
2. Reopened project: `civil_load(doc_id, site=<saved name>)` restores model **and
   results**. New project: `civil_site_new(name, crs, criteria, doc_id)`.
   Select `site` explicitly when multiple sites are open: omitted site can select
   the active session site instead of the document you intended.
3. Criteria are editable profile values, not a universal acceptance code.
   `ten_states_2014` is guidance; confirm adoption, local amendments and overrides.
   Retain references and distinguish published recommendations from practice
   starting values. Apply approved changes with `civil_site_edit` set_criteria.
4. Import through `civil_import` with format `landxml`, `geojson`, `geopackage`,
   `epanet`, `swmm`, `dem_ascii` or `dem_xyz`. Review unit/CRS provenance,
   assumptions, dropped/unmapped fields and supported feature types. Foreign
   feet/gpm/cfs are adapter conversions, not metres already. DEM inputs must be
   metre-based. LandXML text coordinates are north/east, not x/y.
5. Use `civil_crs_transform` for horizontal/local-grid conversion and optional
   scale factors; it does not imply a vertical datum conversion or edit the model.
   Set the site CRS deliberately with `civil_site_edit`. Never label an unknown
   grid with a guessed EPSG code just to make GIS export succeed.
6. Read `civil_site_summary` counts, CRS, validation errors and stored result keys.
   Repair incomplete imports with upsert/remove operations through
   `civil_site_edit`. Imported drawings may lack demands, roughness or elevations;
   an imported network is not yet a solved design.

## 2. Ground, grading, alignment and constructible utility elevations

1. Build existing ground with `civil_surface_build`: survey points, constrained
   breaklines and boundary, or DEM source/stride. Inspect resolution, gaps and the
   modeled footprint before computing quantities or pipe cover.
2. Use `civil_surface_analyze` contours/slope/profile to inspect drainage and
   terrain; use volume/volume_to_elevation to quantify earthwork and stage_storage
   for pond volume. Record which surface is top/bottom and the datum: for
   surface-surface volume, positive top-minus-bottom means fill and net=fill-cut.
3. Grade with `civil_grade_pad`, giving pad elevation, polygon and **H:V** cut/fill
   slopes. Check daylight reach and surface coverage. Do not deliver grading that
   depends on terrain extrapolation. The graded surface is a model edit: recompute
   cover, drainage and quantities on the final proposed surface.
4. Create `civil_alignment` from PIs/radii/spirals or explicit horizontal elements,
   plus the vertical PVI profile. Use `civil_alignment_analyze` profile and
   design_vs_existing for utility/road levels; sections and earthwork for corridor
   quantities; sight_distance with the actual design speed for curve screening.
5. Inspect continuity, profile grades, section spacing, shrink/swell assumptions
   and returned curve checks. Stationing is horizontal distance; station equations
   and superelevation are not modeled. No smooth-looking view replaces numbers.

## 3. Pressurized utilities and fire-flow adequacy

1. Check network source heads, junction elevations/demands, hydraulic diameters,
   statuses, pump curves, tank geometry and operating levels. Hazen-Williams uses
   C; Darcy-Weisbach roughness is **m**. Confirm source head versus elevation.
2. Run `civil_water_solve` for required normal/peak and extended-period cases.
   Inspect convergence and continuity/energy residuals before reading design
   verdicts. Review pressure/velocity checks, delivered versus requested demands,
   source balance, pumps/valves and tank levels at each relevant time.
3. Use `civil_fire_flow` with prescribed flow to test the requirement; omit flow
   to find available withdrawal at the specified residual pressure. Cite flow,
   minimum pressure, `limiting_node_id`, the system-wide residual-pressure map,
   convergence and the profile/explicit residual limit.
4. Choose and disclose DDA versus PDA. DDA can deliver requested demand at
   unrealistic low pressure; PDA reduces **base** demand with pressure. Neither
   makes a low-pressure design pass. Fire withdrawal remains fixed in both.
   Fire-flow analysis is at the initial operating/tank state, not endurance;
   separately establish required duration and storage/supply-volume adequacy.
5. Treat unsupported hydraulic controls/emitters/quality or pump/tank forms as
   explicit limitations, not ignored records. Change the real design/modeling
   basis or report the missing capability; never delete essential behavior.
