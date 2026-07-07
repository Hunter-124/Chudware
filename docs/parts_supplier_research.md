# Parts / Model / Datasheet Supplier Research — Integration Backlog

Research pass (2026-07-07) to expand the Parts-Library vendor browser + Reverse-Engineering
feed beyond the ~20 adapters already shipped (McMaster-Carr, MISUMI, Digi-Key, Amazon,
GrabCAD, Fastenal, TraceParts, Home Depot, Lowes, Tractor Supply, PCBWay, …).

One adapter = one Python file under `services/browser/adapters/` parsing search results into
part cards (`name, part_number, url, price, has_cad_model, datasheet_url, category`). Vendors with
**downloadable CAD** are highest value; datasheet/spec DBs feed the RE suite (build-from-spec).

Legend — **CAD**: yes / some / no · **Access**: open / login / captcha / hard-wall (Akamai/Cloudflare/PerimeterX) / region.

---

## The single biggest lever: two white-label engines back most "vendor CAD portals"

A large share of manufacturer CAD portals are **white-labeled front-ends of just two engines**:
- **CADENAS** — `3dfindit.com`, `partcommunity.com` (+ per-vendor subdomains e.g. `norelem.partcommunity.com`). Backs norelem, Würth, thousands of catalogs.
- **TraceParts** (already have) — backs Thomasnet CAD, Arrow, Newark/Farnell/RS, Bosch Rexroth mirror, Nexperia MCAD.

And most electronics-distributor CAD is delivered by **third-party ECAD providers** (SamacSys / SnapMagic / Ultra Librarian), *not* native distributor files. The scraper's real job on those is to harvest the product page's **provider part-link/ID and follow it**, not expect inline STEP.

**Implication:** one robust **CADENAS PARTcommunity/3Dfindit adapter** + one **SamacSys/Ultra-Librarian ECAD-follower** likely unlock more coverage than a dozen per-vendor adapters. Configurator gating is the recurring hard problem — parametric parts only emit STEP after driving a JS configurator.

---

## Ranked "add first" (best coverage-per-effort)

1. **FreeCAD-library** (github.com/FreeCAD/FreeCAD-library) — open git, raw `.stp`/`.fcstd`, no auth/JS/captcha. Cleanest possible integration; community parts.
2. **LCSC / EasyEDA** (lcsc.com) — EasyEDA-backed JSON API yields symbol+footprint+**STEP/WRL**+datasheet by LCSC#. Open. High-value Chinese electronics.
3. **Ultra Librarian** (ultralibrarian.com) — real STEP+symbol+footprint bundles, 30+ EDA formats, no bot-wall on marketing host, free-account download. Also the *follow-target* for Rutronik/Arrow.
4. **Component Search Engine / SamacSys** (componentsearchengine.com) — symbol+footprint+3D, free account. The follow-target for Avnet/TME/Findchips.
5. **Speedy Metals** (speedymetals.com) — scraper-friendliest metals: public per-length live pricing + cut-to-size, spec sheets per grade.
6. **Professional Plastics** (professionalplastics.com) — open, live prices, public material datasheets + Document Library.
7. **CADENAS 3Dfindit / PARTcommunity** (3dfindit.com) — the big multiplier (6000+ catalogs) but JS SPA + login + configurator; higher effort, highest payoff.
8. **Future Electronics** (futureelectronics.com) — open catalog, SnapMagic CAD follow.
9. **WCH** (wch-ic.com) — static product pages w/ direct datasheet PDFs (CH340/CH32). Trivial parse.
10. **DFRobot** (wiki.dfrobot.com) — per-SKU datasheet + STEP archives, many on github.com/DFRobot/Wiki. Open.
11. **Digikey/Datasheet aggregators** → **Datasheet4U** (datasheet4u.com) — ~900k parts, static HTML, direct PDFs. RE-feed workhorse.
12. **GB standards** (openstd.samr.gov.cn) — **free full-text** GB/T standards download (only free national-standards portal found).
13. **Coremark Metals / Discount Steel** (coremarkmetals.com) — true cut-to-size storefront, per-item live pricing.
14. **Rock West / ACP / Fibre Glast** (composites) — all open storefronts, live prices, spec/SDS PDFs.
15. **Bosch Rexroth** (boschrexroth.com) — native hydraulics CAD + eConfigurators (also TraceParts-mirrored) → anchors the hydraulics category.

---

## By category

### Hydraulics & fluid power (thin today — Hunter explicitly wants pumps/motors/reservoirs/accumulators)
| name | domain | CAD | datasheets | access | note |
|---|---|---|---|---|---|
| Bosch Rexroth | boschrexroth.com | some (STEP via downloads + eConfigurator) | yes | open (some tools login) | native CAD section + configurators; also on TraceParts |
| Parker (Hydraulics) | parker.com | some (TraceParts + native) | yes | open/login | pumps, motors, accumulators, valves — huge catalog |
| Eaton / Danfoss | eaton.com / danfoss.com | some | yes | login | mobile+industrial hydraulics |
| HydraForce | hydraforce.com | some (cartridge-valve CAD) | yes | open | manifolds/cartridge valves |
| Surplus Center | surpluscenter.com | no | some | open | cheap pumps/motors/cylinders — good spec-scrape / RE feed |
| Bailey / Prince | baileyhydraulics.com | no | some | open | cylinders, reservoirs, accumulators, gear pumps |

> Also have `mcp__ai-cad__hydraulic_*` tools in-engine (size_pump/size_cylinder, add_component, BOM) — these suppliers feed real part numbers into that workflow.

### Pneumatics / motion / power transmission
SMC, Festo, Norgren, Bimba (pneumatics) · SKF, NSK, Timken, igus, Thomson, Boston Gear, Gates, Martin Sprocket (bearings/motion) · Maxon, NORD, SEW-Eurodrive, Oriental Motor, ABB/Baldor, Anaheim Automation (motors/gearmotors). **Most route CAD through TraceParts or CADENAS** — prefer the two engine-adapters over per-vendor. igus has a browsable `downloads.igus.com` (static catalog parts scrape; configured parts need the JS configurator).

### Chinese / foreign electronics & mechanical
| name | domain | CAD | access | note |
|---|---|---|---|---|
| LCSC/EasyEDA | lcsc.com | some (STEP/WRL via EasyEDA API) | open | **top pick**; prefer .com over szlcsc.com |
| WCH | wch-ic.com | no | open | static pages, direct datasheet PDFs |
| HDSC (Huada) | hdsc.com.cn/en | no | open | HC32 MCU datasheets; CN-hosted, flaky |
| Nexperia | nexperia.com | some (SamacSys+TraceParts) | open | .cn folds to global |
| Seeed / DFRobot | seeedstudio.com / dfrobot.com | some (off-site: GrabCAD/GitHub) | open | wiki = datasheet+3D goldmine |
| Win Source / Utsource / ICStation | win-source.net / utsource.net / icstation.com | no | open/login | datasheet+price scrape; RE feed |
| Alibaba / Made-in-China | alibaba.com / made-in-china.com | no | login/captcha | photos+spec tables only, no CAD |
| 1688 / Taobao / AliExpress | — | no | **hard-wall** (aliyun/Akamai + CN real-name) | treat as `login_required`; not reliably parseable |

### Electronics distributors (CAD via SamacSys/SnapMagic/UL/TraceParts follow)
| name | domain | access | CAD follow-target |
|---|---|---|---|
| Future Electronics | futureelectronics.com | open | SnapMagic |
| TME | tme.eu / tme.com | open/login | SamacSys |
| Rutronik | rutronik24.com | login | Ultra Librarian |
| Avnet | avnet.com | **hard-wall (Akamai)** | SamacSys — needs stealth session |
| Arrow / Verical | arrow.com / verical.com | **hard-wall (Akamai)** | SnapEDA/UL |
| Newark, Farnell, RS | newark.com / farnell.com / uk.rs-online.com | **hard-wall (Akamai)** | TraceParts (already have engine) |
| Octopart | octopart.com | **hard-wall (CF+PerimeterX)** | **use Nexar GraphQL API, not scraping** |

### Engineered products & precision motion (native, clean CAD — good scraper wins)
| name | domain | CAD | note |
|---|---|---|---|
| Thorlabs | thorlabs.com | yes (STEP per part) | opto-mechanical; pristine clean CAD+specs — easiest native win |
| SMC | smcworld.com/cadlib | yes (STEP AP214, native) | pneumatics portal, no middleman |
| SKF | skf.com | yes (native CAD tab) | anchors bearings category |
| igus | igus.com | yes (native "3-step" + 3Dfindit) | bearings/linear/energy-chain |
| Maxon | maxongroup.com | yes (STEP per motor) | precision DC/BLDC; anchors motors |
| THK / HIWIN | thk.com / hiwin.us | yes (STEP) | linear guides/ballscrews |
| Thomson / Newport | thomsonlinear.com / newport.com | yes | linear actuators / precision stages |
| Anaheim Automation / Oriental Motor / Nanotec | — | yes (STEP per part) | steppers/BLDC, clean HTML |
| Ruland / Martin Sprocket / Lovejoy | — | yes (STEP) | couplings/sprockets — native or PARTcommunity |
| Trelleborg / Parker Seals | tss.trelleborg.com / parker.com/o-ring | config-only | seals — better as RE **spec** source than CAD |

> Note: several of these (Parker, Festo, Boston Gear, Rexnord, Camozzi, NORD, SEW) resolve their
> "CAD download" to **CADENAS PARTcommunity or TraceParts** — so the two engine adapters cover them
> without per-brand work. Build native adapters only for SKF/SMC/igus/Maxon/Thorlabs/Anaheim where
> CAD is hosted directly and cleanly.

### Materials
**Metals:** Speedy Metals (open, best), Coremark/DiscountSteel (open), OnlineMetals, Metals Depot, Ryerson (bot-hostile, account-gated pricing), Metal Supermarkets (quote-only+captcha), Alro (pricing behind AlroOnlineStore). **Plastics:** Professional Plastics + ePlastics (open, easiest), Curbell + TAP (hard-wall 403). **Composites:** Rock West, ACP, Fibre Glast — all open storefronts, live prices, spec/SDS PDFs, no CAD (expected).

### CAD-model aggregators
3Dfindit / PARTcommunity (CADENAS, login+configurator), 3D ContentCentral (Dassault, .aspx, free-login download), Thomasnet CAD (TraceParts-powered, root 403), Würth technikportal (open-ish native), norelem (=PARTcommunity instance), FreeCAD-library (open git — do first).

### Datasheet / standards DBs (RE-suite feed)
**Clean wins (static HTML, direct PDF):** Datasheet4U, DatasheetCatalog, DatasheetArchive. **Extra hop:** Alldatasheet (viewer page → PDF). **JS SPA but best `has_cad_model` signal:** Datasheets.com. **Standards — all Western SDOs are metadata-only paywall** (ANSI Webstore, ASME, ASTM, ISO, DIN — scrape catalog metadata + price, PDF is purchase+DRM). **Only free full-text:** GB standards `openstd.samr.gov.cn`.

---

## Adapter-design takeaways
- Build **2 engine adapters** (CADENAS PARTcommunity/3Dfindit; SamacSys/Ultra-Librarian ECAD-follower) before per-vendor ones — they multiply coverage.
- **Configurator gating** is the hard case: parametric parts emit STEP only after driving a JS configurator — the headless browser must interact, not just parse HTML.
- Hard-wall sites (Akamai/Cloudflare/PerimeterX: Avnet, Arrow, Verical, Newark/Farnell/RS, Octopart, Curbell, TAP, 1688/Taobao/AliExpress) belong to the **authenticated-browser-session** path (the CEF login flow), not raw fetch — or honest `login_required`. We do not bypass captchas.
- Octopart is API-first (Nexar GraphQL) — worth a non-scraper integration for pricing/availability/datasheet/CAD-link metadata.
