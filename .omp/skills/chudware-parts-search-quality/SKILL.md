---
name: chudware-parts-search-quality
description: Improve Parts Library search relevance, recall, and local-versus-supplier result selection.
---

# chudware-parts-search-quality

Inspect `services/parts/index.py`, `hub.py`, `query_norm.py`, and adapters. Run `scripts/parts_bench.py` with a unique private XDG data directory and compare baseline and changed results from isolated source trees. Inspect MRR, hit rate and false-confident results alongside individual rankings; make sure expected-result patterns are discriminating rather than saturated.

Examine query normalization, FTS ranking, local catalog coverage and suppression of supplier results independently. Add representative positive and negative search cases. Report actual measured results.

Verify: `scripts/testbox.py --suite parts`
