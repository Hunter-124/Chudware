---
name: chudware-tesseract-brand-mark
description: Change the tesseract brand animation while preserving logo parity and persisted style IDs.
---

# chudware-tesseract-brand-mark

`host/src/ui/tesseract.h` and `tesseract.cpp` define the animation styles and painter. Append style IDs rather than changing persisted meanings. Settings accept values from zero through `TesseractAnim::Count - 1`; invalid values fall back to EightCell.

`assets/make_logo.py` mirrors EightCell rotation and camera/projection constants: update and regenerate assets when their shared math changes. Preserve spinner behavior and check small-size level of detail. Inspect full-cycle filmstrips and size ladders for clipping, projection singularities and visual distinction; use a private GUI/XDG environment and owned daemon for screenshots.

Verify: `scripts/testbox.py --suite gui`
