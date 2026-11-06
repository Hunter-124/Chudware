---
name: chudware-icon-photo-restore
description: Restore engine-rendered ribbon icon tiles when atlas regeneration or SDF conversion damages them.
---

# chudware-icon-photo-restore

Inspect `assets/icons/atlas.json` for the affected names in `raster_only`, and inspect `assets/make_icons.py` for rendered-tile preservation (`RENDERED_RECIPE_NAMES` and the CAM prefix). A stale runtime atlas can also look like a source failure: inspect the pipeline's current deployment destination and verify the actual artifact under test.

Prefer `python3 scripts/icon_pipeline.py --inject-only` when the render inputs exist; use `--render` only when renders are missing and its prerequisites are available. Confirm both theme atlases and `raster_only` entries; inspect the rendered icons rather than relying only on successful generation. Preserve the rendering recipes during glyph regeneration.

Verify: `scripts/testbox.py --suite gui`
