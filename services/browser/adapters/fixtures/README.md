# Adapter search-page fixtures

Captured, rendered search-result HTML per source site, refreshed with:

```bash
python3 scripts/refresh_adapter_fixtures.py            # every site with a saved profile
python3 scripts/refresh_adapter_fixtures.py --site grainger
```

Files land here as `<site>_search.html`. The refresh script immediately re-runs
the site's adapter parser over the freshly captured page and prints the hit
count, so a fixture whose markup drifted past the parser is caught at refresh
time, not in production.

Live HTML captures are NOT committed (site markup is copyrighted page content
and churns constantly) — the offline test suite uses small hand-written
fixture strings in `test_retail_adapters.py` / `test_adapters.py` /
`test_new_adapters.py` instead. This directory exists for local parser
development against real pages.

Refreshing uses the headless backend selected by `services.browser.driver` plus a
saved profile for the site (`browser_login('<site>')` — see services/browser/README.md).
CEF needs a CEF-enabled Chudware host binary; Playwright/Patchright selections need
their explicitly provisioned browser binary. Headed login remains a residual driver path.
