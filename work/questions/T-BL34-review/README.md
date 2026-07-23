# T-BL34 review artifacts (sonnet-ivo, 2026-07-04)

- `icon_contact_sheet_dark.png` / `icon_contact_sheet_light.png` — every generated tile
  (22 modeling + 19 Manufacture toolpath), review size + true-32px copy in each cell's
  corner. Regenerate any time: `xvfb-run -a python3 scripts/icon_pipeline.py --render`.
- `ribbon_manufacture_dark.png` / `ribbon_manufacture_light.png` — in-context headless
  captures (CHUDWARE_SHOT=ribbon, manufacture tab) proving the C++ loader renders the 19
  new cam_* cells from the extended atlas. NOTE: the first-run "Chudware" info toast
  overlays the Job group in every headless ribbon capture (pre-existing, all tabs).

Manufacture tiles are REAL generated toolpaths (cam_new -> add_tool/add_setup/
add_operation -> cam_generate -> cam_simulate, rendered from the returned move list —
never stylized). CAM bug found by this smoke path: issue #13 (lathe sim volume).
