# corpora — consolidated test fixtures

Sketch, render, and SolidWorks fixtures for testing the C++ core and the sole
host GUI. The former Python `engine_oracle/` differential leg was deleted in
T-RF21 (2026-07-23); these are regression and interoperability corpora, not a
three-engine oracle comparison.

| Subdir | Purpose |
|--------|---------|
| `sketch/` | Sketch solver JSON fixtures |
| `render/` | Headless render SVG outputs |
| `sw_parity/` | SW sketch parity JSON fixtures |
| `sw_fixtures/` | SolidWorks `.SLDPRT` import fixtures |
