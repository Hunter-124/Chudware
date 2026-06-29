# corpora — consolidated test fixtures

Parity, sketch, render, and SolidWorks fixture corpora for differential testing
(Python oracle ↔ C++ core ↔ host GUI).

| Subdir | Former path | Purpose |
|--------|-------------|---------|
| `sketch/` | `corpus/` | Sketch solver JSON fixtures |
| `render/` | `corpus_render/` | Headless render SVG outputs |
| `sw_parity/` | `corpus_sw_parity/` | SW sketch parity JSON fixtures |
| `sw_fixtures/` | `test_fixtures_sw/` | SolidWorks `.SLDPRT` import fixtures |

Root-level symlinks (`corpus`, `corpus_render`, etc.) preserve legacy paths
during migration.
