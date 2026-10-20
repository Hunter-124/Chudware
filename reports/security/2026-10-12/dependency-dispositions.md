# Dependency advisory dispositions — 2026-10-12 audit

## Scope and evidence

This is a dependency cutover record, not a claim that running deployments or the desktop venv have been upgraded. Inputs are [`dependency-audit.json`](dependency-audit.json) and [`npm-audit.json`](npm-audit.json). The Python table has **92 distinct package/advisory pairs**, retaining every source occurrence through its source column (**192 source/package/advisory records**). The npm table retains **27 direct package/advisory pairs**; the inherited-effect table retains all **3 string-via records**. Aliases are identifiers for the same advisory, not separate fixes.

Public fixed Python versions were resolved from version-specific PyPI JSON metadata; all listed releases exist, have files and are not yanked. npm fixed pins were resolved from version-specific registry metadata. `npm install --package-lock-only --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org` regenerated the existing v3 lock and its integrity/native optional-package records; it did not install packages, run lifecycle scripts, build, test or deploy. No package-index audit calls, provider calls, credentials, private-package resolution, or runtime wheel approvals were performed. One unsuccessful version probe each for http-cache-semantics 4.2.1 and smol-toml 1.8.1 returned 404; the resolved fixed releases are 4.3.0 and 1.9.0.

### Parent post-upgrade observation — 2026-10-14

The preceding paragraph records the original dependency worker's limited operation, not the later parent verification. Actual requests to [OSV's batch query API](https://google.github.io/osv.dev/post-v1-querybatch/) at `2026-10-14T03:05:15Z` returned **zero advisory matches and no pending pages** for both 54 exact public declaration versions and all 171 unique public versions found by `importlib.metadata` in the disposable upgraded gateway Python 3.14 environment. Duplicate `lib`/`lib64` metadata paths were deduplicated only after verifying identical versions. All 17 exact gateway declarations match installed versions; `pip 26.2` and `26.2.0` are compared under PEP 440.

Inputs: root declarations SHA-256 `256fe7b80535f08e4378965a8a1649ae73f0099b666e4ad25d03fe540effdc4d`; gateway declarations SHA-256 `6d3fe446803c95083653a12a864eab5ef33fc9ac3f8aff3ef744f7caffffb110`. This query did not install anything or send private SDK identities to the package service. A database version match is not an exploit/reachability test or complete native/browser/OS dependency assessment; the disposable environment is not an approved production image. The original `dependency-audit.json` and `npm-audit.json` remain the immutable vulnerable-inventory evidence. Runtime lock/image and external release authority prerequisites below remain open.


### Authenticated runtime release prerequisite

`requirements.txt` remains an offline declaration file (`--no-index`, binary wheels only). The installer checks its byte digest against `vendor/python/RUNTIME_WHEELS.lock` before provisioning (`services/installer/wheelhouse.py:178-187`), validates every approved wheel hash/target/dependency, and refuses mismatches. That reviewed lock is **absent from this checkout** (`vendor/` contains only `chudcode/`), and changing private/runtime wheel approvals is explicitly outside this assignment. It has not been created or altered. Accordingly the root declaration changes cannot be considered a deployable authenticated runtime until the release owner supplies a reviewed complete per-target lock and wheelhouse matching the new declarations and the authenticated private SDK source. Public registry hashes alone are not approval. Do not edit the digest by hand, loosen validation, resolve the private SDK from an index, or fall back to unreviewed execution.

Existing root/gateway declarations have exact version pins but no per-file hashes; their convention is preserved rather than inventing hashes without target-wheel review. The npm lock preserves registry `resolved` URLs and `integrity` hashes via the normal resolver. No service Python lock was found in the tracked source manifests. `packaging/requirements-release.txt` already pins cryptography 50.0.0 and intentionally stays unchanged; it agrees with the updated root/gateway pins.

### Python cutover

Root pins: anyio 4.14.2; cryptography 50.0.0; dulwich 1.2.12; langgraph-checkpoint-sqlite 3.1.1; mcp 1.28.1; Pillow 12.3.0; pip 26.2.0 (upstream normalizes to 26.2); PyJWT[crypto] 2.15.0; pypdf 6.19.0; setuptools 83.0.0; urllib3 2.8.0; Werkzeug 3.1.9. Gateway pins the shared anyio/cryptography/dulwich/pip/PyJWT/urllib3 versions; it does not add desktop-only MCP, image/PDF, DAG, or Werkzeug servers. The fixed sqlite package requires langgraph-checkpoint >=4.1.0,<5.0.0, which the reviewed full runtime closure must satisfy.

### npm cutover

Astro 7.3.8 and Wrangler 4.149.0 are exact direct pins. Fixed transitive overrides are devalue 5.9.4, http-cache-semantics 4.3.0, js-yaml 4.3.2, sharp 0.35.5, smol-toml 1.9.0, source-map-js 1.2.2, svgo 4.1.0 and undici 7.29.1. Wrangler metadata pins Miniflare 5.20261006.1-alpha, whose metadata pins sharp 0.35.5 and undici 7.29.1. The Node floor now matches Astro/MDX: >=22.12.0. Existing MDX 7.0.5 accepts Astro ^7.0.0; it is not silently replaced. All sharp platform packages are resolver-managed, not hand-edited mismatched binaries. An override can exclude an affected package even if upgrading its parent removes that node.

## Reachability evidence

Reachability codes below identify the observed consumer seam. **A reachable package is not evidence that every exploit condition is reachable.** Missing feature-specific proof is explicit and is not used to waive an upgrade. `[INFERENCE]` marks potential exposure not demonstrated by execution.

- **R-anyio (anyio)**: Reachable async dependency: services/mcp/server.py:1107-1115 and 1400-1410 use anyio stdio transport; gateway FastAPI/httpx also depend on it. Advisory-specific exploit not exercised.

- **R-crypto (cryptography)**: Reachable verification/encryption dependency: Webservice/gateway/auth.py:552-558 verifies RS256; entitlement.py:26-30 uses Ed25519; provider_keys.py:18-19 and marketplace_sources.py:27-28 use AESGCM; services/installer/payload_manifest.py:675-676 verifies signed payloads. Advisory-specific primitive/condition not established; upgrade conservatively.

- **R-dulwich (dulwich)**: Reachable untrusted repository/transport dependency: services/plm/gitbind.py:43-44 opens the vault, remote.py:381 invokes fetch and :451-451/:503-505 select transports; Webservice/gateway/plm_seed.py:28-31 imports the same implementation. Malicious repository/server exposure is plausible [INFERENCE]; no exploit run.

- **R-sqlite (langgraph-checkpoint-sqlite)**: Installed DAG persistence dependency: services/dag/executor.py:21 imports SqliteSaver. GHSA-47pj-3jcm-6whg affects SqliteStore/AsyncSqliteStore namespace searches, not the observed SqliteSaver seam; affected-store reachability is not established. Upgrade rather than waive.

- **R-mcp (mcp)**: Reachable protocol dependency: services/mcp/server.py:1401-1410 starts the SDK stdio server; services/agent/reviewer_exec.py:130 uses FastMCP. Exact advisory preconditions not exercised. No public gateway MCP endpoint is inferred from an installed package.

- **R-pillow (pillow)**: Reachable image decoder: services/agent/tool_images.py:238-267 opens files/inline bytes and services/deliverables/engines.py:115-118 opens user images. Decoder-format-specific preconditions not exercised; treat attacker images as reachable.

- **R-pip (pip)**: Provisioning/build-time package manager, not an application request handler. services/installer/provision.py:437-453 bootstraps and invokes offline pip after authenticated wheel verification; wheelhouse.py:315-317 enforces no-index/no-deps/require-hashes. Upgrade the audited installed component without enabling tenant package installation.

- **R-jwt (pyjwt)**: Reachable authentication verifier: Webservice/gateway/auth.py:552-558 and oauth.py:336-343/:378-394 decode user tokens with an RS256 allowlist and newly constructed options dicts. Signature/audience/expiry behaviour remains security-critical; advisory-specific exploits not run.

- **R-pypdf (pypdf)**: Reachable PDF parser/editor: services/mcp/office_tools.py:847 imports pypdf for page-level editing. User-supplied PDF parsing is reachable; individual malformed-stream/structure exploits not exercised.

- **R-setuptools (setuptools)**: Audited desktop installed packaging component; no product request entrypoint established. Runtime installers disallow build backends/source distributions (services/installer/wheelhouse.py:315-317). Pin fixed bytes for the reviewed closure; do not claim these restrictions repair the library.

- **R-urllib3 (urllib3)**: Transitive reachable HTTP transport: Dulwich 1.2.12 metadata requires urllib3>=2.2.2; services/plm/remote.py:451/503 selects HTTPS transports. Gateway boto3 also brings an HTTP transport closure [INFERENCE]. Specific redirect/response exploit not exercised.

- **R-werkzeug (werkzeug)**: Present in desktop installed inventory only; no product Werkzeug server entrypoint established by this investigation. Pin its fixed release without creating/enabling a server; installed-only is not a proven non-reachability waiver.


### Site/tooling reachability

- **N-astro (astro)**: Static site builder (astro.config.mjs:6 output:static); no deployed Astro SSR/image endpoint is configured. Build-time image decoding is potentially reachable [INFERENCE]; critical AVIF RCE is upgraded even though a production optimization endpoint is not established.

- **N-devalue (devalue)**: Astro dependency. Static output has no configured server-side user serialization endpoint; malformed/untrusted build content may reach it [INFERENCE]. Do not waive library advisories based only on static output.

- **N-cache (http-cache-semantics)**: Astro dependency. No production shared user-response cache is established for the static site; build-time fetch/cache use is possible [INFERENCE]. Fixed security override applies to every copy.

- **N-yaml (js-yaml)**: Build-time dependency in the audited site graph; malformed YAML/frontmatter in source/build inputs is potential exposure [INFERENCE], not a public gateway endpoint. Fixed override excludes vulnerable versions even if parent upgrades remove this node.

- **N-sharp (sharp)**: Astro optional image decoder and Miniflare development image decoder; bundled libheif/librsvg handle image data [INFERENCE]. Upgrade both audited copies and their platform-specific optional native packages through the generated lock.

- **N-toml (smol-toml)**: Astro build/config parser dependency; crafted TOML in build inputs is potential exposure [INFERENCE]. Not a deployed HTTP parser. Exact pin excludes the audited quadratic parser.

- **N-sourcemap (source-map-js)**: Transitive build/source-map decoder; untrusted map sections in source inputs are potential exposure [INFERENCE]. No deployed map-processing service established. Pin fixed release for all consumers.

- **N-svgo (svgo)**: Astro SVG build optimizer; malicious SVG in build/content inputs can potentially survive optimization [INFERENCE]. removeScripts is not a product trust boundary or substitute for sanitization. Upgrade to fixed library.

- **N-undici (undici)**: Miniflare development HTTP/WebSocket transport, not the Python production gateway. A hostile response/server may hit development fetch paths [INFERENCE]; feature-specific interceptor/WebSocket use is not established. Upgrade all copies.

- **N-miniflare (miniflare)**: Wrangler development emulator; inherited sharp/undici findings. No production Miniflare server is configured by static Astro output. Fixed parent pins the patched child closure.

- **N-wrangler (wrangler)**: Development/deployment tool; inherited Miniflare findings. No deploy command or Cloudflare API/provider request was executed. Upgrade the parent and emulator closure.


## Python advisory-pair table

Sources: **P** python-primary; **S** python-secondary; **G** gateway-installed; **D** desktop-installed. “Pin fixed” is a source remediation, **pending the authenticated runtime release prerequisite above** and central installation/testing. It does not describe the currently installed inventory.

| Package | Advisory and aliases | Audited version / sources | Reported fix | Reachability | Disposition |
|---|---|---|---|---|---|
| anyio | [PYSEC-2026-4023](https://github.com/advisories/GHSA-3w57-8xmc-8v26)<br>CVE-2026-63349, GHSA-3w57-8xmc-8v26 | 4.14.1 / D | 4.14.2 | R-anyio | Pin fixed `4.14.2` |
| anyio | [PYSEC-2026-4024](https://github.com/advisories/GHSA-5p39-cfhj-2xmp)<br>CVE-2026-64847, GHSA-5p39-cfhj-2xmp | 4.14.1 / D | 4.14.2 | R-anyio | Pin fixed `4.14.2` |
| anyio | [PYSEC-2026-4025](https://github.com/advisories/GHSA-82r6-8w77-94w6)<br>CVE-2026-63374, GHSA-82r6-8w77-94w6 | 4.14.1 / D | 4.14.2 | R-anyio | Pin fixed `4.14.2` |
| cryptography | [PYSEC-2026-3552](https://github.com/advisories/GHSA-g6cj-pr64-35w5)<br>CVE-2026-69247, GHSA-g6cj-pr64-35w5 | 49.0.0 / G,D | 50.0.0 | R-crypto | Pin fixed `50.0.0` |
| dulwich | [GHSA-35mr-4567-66vg](https://github.com/advisories/GHSA-35mr-4567-66vg) | 1.2.7 / S,D | 1.2.9 | R-dulwich | Pin fixed `1.2.12` |
| dulwich | [GHSA-5fqc-mrg8-w798](https://github.com/advisories/GHSA-5fqc-mrg8-w798) | 1.2.7 / S,D | 1.2.8 | R-dulwich | Pin fixed `1.2.12` |
| dulwich | [GHSA-8w8g-wq8h-fq33](https://github.com/advisories/GHSA-8w8g-wq8h-fq33) | 1.2.7 / S,D | 1.2.8 | R-dulwich | Pin fixed `1.2.12` |
| dulwich | [GHSA-cm62-gvxx-vmxx](https://github.com/advisories/GHSA-cm62-gvxx-vmxx) | 1.2.7 / S,D | 1.2.8 | R-dulwich | Pin fixed `1.2.12` |
| langgraph-checkpoint-sqlite | [PYSEC-2026-3636](https://github.com/advisories/GHSA-47pj-3jcm-6whg)<br>CVE-2026-71433, GHSA-47pj-3jcm-6whg | 3.1.0 / D | 3.1.1 | R-sqlite | Pin fixed `3.1.1` |
| mcp | [PYSEC-2026-3483](https://github.com/advisories/GHSA-vj7q-gjh5-988w)<br>CVE-2026-59950, GHSA-vj7q-gjh5-988w | 1.27.2 / P,G,D | 1.28.1 | R-mcp | Pin fixed `1.28.1` |
| pillow | [PYSEC-2026-2253](https://github.com/advisories/GHSA-8v84-f9pq-wr9x)<br>BIT-pillow-2026-54059, CVE-2026-54059, GHSA-8v84-f9pq-wr9x | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-2254](https://github.com/advisories/GHSA-5x94-69rx-g8h2)<br>BIT-pillow-2026-54060, CVE-2026-54060, GHSA-5x94-69rx-g8h2 | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-2255](https://github.com/advisories/GHSA-45hq-cxwh-f6vc)<br>BIT-pillow-2026-55379, CVE-2026-55379, GHSA-45hq-cxwh-f6vc | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-2256](https://github.com/advisories/GHSA-phj9-mv4w-65pm)<br>BIT-pillow-2026-55380, CVE-2026-55380, GHSA-phj9-mv4w-65pm | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-2257](https://github.com/advisories/GHSA-4x4j-2g7c-83w6)<br>CVE-2026-55798, GHSA-4x4j-2g7c-83w6 | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-3451](https://github.com/advisories/GHSA-6r8x-57c9-28j4)<br>BIT-pillow-2026-59199, CVE-2026-59199, GHSA-6r8x-57c9-28j4 | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-3452](https://github.com/advisories/GHSA-pg7v-jwj7-p798)<br>BIT-pillow-2026-59203, CVE-2026-59203, GHSA-pg7v-jwj7-p798 | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-3453](https://github.com/advisories/GHSA-9hw9-ch79-4vh6)<br>BIT-pillow-2026-59205, CVE-2026-59205, GHSA-9hw9-ch79-4vh6 | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-3454](https://github.com/advisories/GHSA-xj96-63gp-2gmr)<br>CVE-2026-59197, GHSA-xj96-63gp-2gmr | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-3493](https://github.com/advisories/GHSA-62p4-gmf7-7g93)<br>CVE-2026-54058, GHSA-62p4-gmf7-7g93 | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-3494](https://github.com/advisories/GHSA-fj7v-r99m-22gq)<br>CVE-2026-59198, GHSA-fj7v-r99m-22gq | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-3495](https://github.com/advisories/GHSA-jjj6-mw9f-p565)<br>CVE-2026-59200, GHSA-jjj6-mw9f-p565 | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pillow | [PYSEC-2026-3496](https://github.com/advisories/GHSA-vjc4-5qp5-m44j)<br>CVE-2026-59204, GHSA-vjc4-5qp5-m44j | 12.2.0 / P,D | 12.3.0 | R-pillow | Pin fixed `12.3.0` |
| pip | [PYSEC-2026-3721](https://github.com/advisories/GHSA-qwm4-qh6w-59xr)<br>CVE-2026-13346, GHSA-qwm4-qh6w-59xr | 26.1.2 / G | 26.2.0 | R-pip | Pin fixed `26.2.0` |
| pyjwt | [PYSEC-2026-4140](https://github.com/advisories/GHSA-2gx3-rcp4-g85q)<br>CVE-2026-101917, GHSA-2gx3-rcp4-g85q | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4141](https://github.com/advisories/GHSA-42vr-xj54-vc7v)<br>CVE-2026-101918, GHSA-42vr-xj54-vc7v | 2.13.0 / P,G,D | 2.15.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4142](https://github.com/advisories/GHSA-8wjv-2p76-3863)<br>CVE-2026-102265, GHSA-8wjv-2p76-3863 | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4143](https://github.com/advisories/GHSA-9j54-fg26-wv3r)<br>CVE-2026-102266, GHSA-9j54-fg26-wv3r | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4144](https://github.com/advisories/GHSA-9v7f-9g4p-ffgj)<br>CVE-2026-102267, GHSA-9v7f-9g4p-ffgj | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4145](https://github.com/advisories/GHSA-ffc3-869f-jxw9)<br>CVE-2026-102268, GHSA-ffc3-869f-jxw9 | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4146](https://github.com/advisories/GHSA-gvp8-978c-rx2q)<br>CVE-2026-103001, GHSA-gvp8-978c-rx2q | 2.13.0 / P,G,D | None published | R-jwt | No published fix: fresh per-call options mitigate the stated dict-reuse condition; pin 2.15.0 is outside the advisory’s stated <=2.13.0 range, **not proof of a released patch**. See residual disposition below. |
| pyjwt | [PYSEC-2026-4147](https://github.com/advisories/GHSA-hxm8-2xgr-2p9m)<br>CVE-2026-102269, GHSA-hxm8-2xgr-2p9m | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4148](https://github.com/advisories/GHSA-jwrc-g2q2-pq5p)<br>CVE-2026-102270, GHSA-jwrc-g2q2-pq5p | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4149](https://github.com/advisories/GHSA-p4g4-x82p-q773)<br>CVE-2026-102271, GHSA-p4g4-x82p-q773 | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4150](https://github.com/advisories/GHSA-r6x4-923q-g947)<br>CVE-2026-102272, GHSA-r6x4-923q-g947 | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4151](https://github.com/advisories/GHSA-w2cx-738m-mc7w)<br>CVE-2026-102273, GHSA-w2cx-738m-mc7w | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4152](https://github.com/advisories/GHSA-w6j9-cwv2-h6wq)<br>CVE-2026-102274, GHSA-w6j9-cwv2-h6wq | 2.13.0 / P,G,D | 2.14.0 | R-jwt | Pin fixed `2.15.0` |
| pyjwt | [PYSEC-2026-4183](https://github.com/advisories/GHSA-x33g-cr3x-6449)<br>CVE-2026-102275, GHSA-x33g-cr3x-6449 | 2.13.0 / P,G,D | 2.15.0 | R-jwt | Pin fixed `2.15.0` |
| pypdf | [CVE-2026-57204](https://github.com/advisories/GHSA-jm82-fx9c-mx94)<br>GHSA-jm82-fx9c-mx94 | 5.5.0 / P,D | 6.13.3 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-1827](https://github.com/advisories/GHSA-2q4j-m29v-hq73)<br>CVE-2026-24688, GHSA-2q4j-m29v-hq73 | 5.5.0 / P,D | 6.6.2 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-1828](https://github.com/advisories/GHSA-4f6g-68pf-7vhv)<br>CVE-2026-22691, GHSA-4f6g-68pf-7vhv | 5.5.0 / P,D | 6.6.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-1829](https://github.com/advisories/GHSA-4xc4-762w-m6cg)<br>CVE-2026-22690, GHSA-4xc4-762w-m6cg | 5.5.0 / P,D | 6.6.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-1830](https://github.com/advisories/GHSA-7hfw-26vp-jp8m)<br>CVE-2025-55197, GHSA-7hfw-26vp-jp8m | 5.5.0 / P,D | 6.0.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-1831](https://github.com/advisories/GHSA-jfx9-29x2-rv3j)<br>CVE-2025-62708, GHSA-jfx9-29x2-rv3j | 5.5.0 / P,D | 6.1.3 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-1832](https://github.com/advisories/GHSA-m449-cwjh-6pw7)<br>CVE-2025-66019, GHSA-m449-cwjh-6pw7 | 5.5.0 / P,D | 6.4.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-1833](https://github.com/advisories/GHSA-vr63-x8vc-m265)<br>CVE-2025-62707, GHSA-vr63-x8vc-m265 | 5.5.0 / P,D | 6.1.3 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3004](https://github.com/advisories/GHSA-248m-82v9-q6g6)<br>CVE-2026-48156, GHSA-248m-82v9-q6g6 | 5.5.0 / P,D | 6.12.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3005](https://github.com/advisories/GHSA-2rw7-x74f-jg35)<br>CVE-2026-27628, GHSA-2rw7-x74f-jg35 | 5.5.0 / P,D | 6.7.2 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3006](https://github.com/advisories/GHSA-3crg-w4f6-42mx)<br>CVE-2026-40260, GHSA-3crg-w4f6-42mx | 5.5.0 / P,D | 6.10.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3007](https://github.com/advisories/GHSA-4pxv-j86v-mhcw)<br>CVE-2026-41313, GHSA-4pxv-j86v-mhcw | 5.5.0 / P,D | 6.10.2 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3009](https://github.com/advisories/GHSA-52x6-gq3r-vpf4)<br>CVE-2026-54530, GHSA-52x6-gq3r-vpf4 | 5.5.0 / P,D | 6.13.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3010](https://github.com/advisories/GHSA-5hgr-hg42-57jg)<br>CVE-2026-49460, GHSA-5hgr-hg42-57jg | 5.5.0 / P,D | 6.12.2 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3011](https://github.com/advisories/GHSA-7gw9-cf7v-778f)<br>CVE-2026-41312, GHSA-7gw9-cf7v-778f | 5.5.0 / P,D | 6.10.2 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3012](https://github.com/advisories/GHSA-87mj-5ggw-8qc3)<br>CVE-2026-33699, GHSA-87mj-5ggw-8qc3 | 5.5.0 / P,D | 6.9.2 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3013](https://github.com/advisories/GHSA-996q-pr4m-cvgq)<br>CVE-2026-27024, GHSA-996q-pr4m-cvgq | 5.5.0 / P,D | 6.7.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3014](https://github.com/advisories/GHSA-9m86-7pmv-2852)<br>CVE-2026-28804, GHSA-9m86-7pmv-2852 | 5.5.0 / P,D | 6.7.5 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3015](https://github.com/advisories/GHSA-9mvc-8737-8j8h)<br>CVE-2026-27026, GHSA-9mvc-8737-8j8h | 5.5.0 / P,D | 6.7.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3016](https://github.com/advisories/GHSA-cj93-chg6-vgv8)<br>CVE-2026-48155, GHSA-cj93-chg6-vgv8 | 5.5.0 / P,D | 6.12.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3017](https://github.com/advisories/GHSA-f2v5-7jq9-h8cg)<br>CVE-2026-28351, GHSA-f2v5-7jq9-h8cg | 5.5.0 / P,D | 6.7.4 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3018](https://github.com/advisories/GHSA-g9xf-7f8q-9mcj)<br>CVE-2026-54651, GHSA-g9xf-7f8q-9mcj | 5.5.0 / P,D | 6.13.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3019](https://github.com/advisories/GHSA-hqmh-ppp3-xvm7)<br>CVE-2026-31826, GHSA-hqmh-ppp3-xvm7 | 5.5.0 / P,D | 6.8.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3020](https://github.com/advisories/GHSA-j543-4vmf-qm7v)<br>CVE-2026-49461, GHSA-j543-4vmf-qm7v | 5.5.0 / P,D | 6.12.2 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3021](https://github.com/advisories/GHSA-jj6c-8h6c-hppx)<br>CVE-2026-41168, GHSA-jj6c-8h6c-hppx | 5.5.0 / P,D | 6.10.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3022](https://github.com/advisories/GHSA-m2v9-299j-rv96)<br>CVE-2026-54531, GHSA-m2v9-299j-rv96 | 5.5.0 / P,D | 6.13.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3023](https://github.com/advisories/GHSA-qpxp-75px-xjcp)<br>CVE-2026-33123, GHSA-qpxp-75px-xjcp | 5.5.0 / P,D | 6.9.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3024](https://github.com/advisories/GHSA-wgvp-vg3v-2xq3)<br>CVE-2026-27025, GHSA-wgvp-vg3v-2xq3 | 5.5.0 / P,D | 6.7.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3025](https://github.com/advisories/GHSA-wjqc-6w8f-h24c)<br>CVE-2026-48735, GHSA-wjqc-6w8f-h24c | 5.5.0 / P,D | 6.12.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3026](https://github.com/advisories/GHSA-x284-j5p8-9c5p)<br>CVE-2026-41314, GHSA-x284-j5p8-9c5p | 5.5.0 / P,D | 6.10.2 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3027](https://github.com/advisories/GHSA-x7hp-r3qg-r3cj)<br>CVE-2026-27888, GHSA-x7hp-r3qg-r3cj | 5.5.0 / P,D | 6.7.3 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3610](https://github.com/advisories/GHSA-55h5-xmcq-c37v)<br>CVE-2026-59937, GHSA-55h5-xmcq-c37v | 5.5.0 / P,D | 6.14.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3611](https://github.com/advisories/GHSA-5qjq-93h5-hrgp)<br>CVE-2026-59938, GHSA-5qjq-93h5-hrgp | 5.5.0 / P,D | 6.14.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3612](https://github.com/advisories/GHSA-5xf7-4p34-54qr)<br>CVE-2026-59936, GHSA-5xf7-4p34-54qr | 5.5.0 / P,D | 6.14.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3613](https://github.com/advisories/GHSA-g867-7843-wf8q)<br>CVE-2026-59935, GHSA-g867-7843-wf8q | 5.5.0 / P,D | 6.14.2 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3655](https://github.com/advisories/GHSA-fp3f-mc75-235c)<br>CVE-2026-71870, GHSA-fp3f-mc75-235c | 5.5.0 / P,D | 6.15.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3656](https://github.com/advisories/GHSA-fwg2-594c-jp42)<br>CVE-2026-71852, GHSA-fwg2-594c-jp42 | 5.5.0 / P,D | 6.15.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3910](https://github.com/advisories/GHSA-23w6-3w8w-8484)<br>CVE-2026-84310, GHSA-23w6-3w8w-8484 | 5.5.0 / P,D | 6.16.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3911](https://github.com/advisories/GHSA-763m-79hh-57f2)<br>CVE-2026-84311, GHSA-763m-79hh-57f2 | 5.5.0 / P,D | 6.16.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3912](https://github.com/advisories/GHSA-fc8x-2rww-xw9m)<br>CVE-2026-82398, GHSA-fc8x-2rww-xw9m | 5.5.0 / P,D | 6.15.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-3913](https://github.com/advisories/GHSA-jp53-mhqp-8xcg)<br>CVE-2026-84309, GHSA-jp53-mhqp-8xcg | 5.5.0 / P,D | 6.16.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-4153](https://github.com/advisories/GHSA-5jq2-8x83-x246)<br>CVE-2026-102994, GHSA-5jq2-8x83-x246 | 5.5.0 / P,D | 6.18.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-4154](https://github.com/advisories/GHSA-fp3h-c4fm-7vvf)<br>CVE-2026-102995, GHSA-fp3h-c4fm-7vvf | 5.5.0 / P,D | 6.18.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-4155](https://github.com/advisories/GHSA-g9cg-prrw-2r8q)<br>CVE-2026-102996, GHSA-g9cg-prrw-2r8q | 5.5.0 / P,D | 6.18.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-4156](https://github.com/advisories/GHSA-jw7q-gvrg-4vj3)<br>CVE-2026-102997, GHSA-jw7q-gvrg-4vj3 | 5.5.0 / P,D | 6.18.1 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-4157](https://github.com/advisories/GHSA-php9-fj8v-98fj)<br>CVE-2026-102998, GHSA-php9-fj8v-98fj | 5.5.0 / P,D | 6.19.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-4158](https://github.com/advisories/GHSA-qv6h-rv94-w285)<br>CVE-2026-102993, GHSA-qv6h-rv94-w285 | 5.5.0 / P,D | 6.17.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-4159](https://github.com/advisories/GHSA-v247-6f48-mgcj)<br>CVE-2026-102999, GHSA-v247-6f48-mgcj | 5.5.0 / P,D | 6.19.0 | R-pypdf | Pin fixed `6.19.0` |
| pypdf | [PYSEC-2026-4160](https://github.com/advisories/GHSA-w23x-9jrw-r45c)<br>CVE-2026-103000, GHSA-w23x-9jrw-r45c | 5.5.0 / P,D | 6.19.0 | R-pypdf | Pin fixed `6.19.0` |
| setuptools | [PYSEC-2026-3447](https://github.com/advisories/GHSA-h35f-9h28-mq5c)<br>BIT-setuptools-2026-59890, CVE-2026-59890, GHSA-h35f-9h28-mq5c | 80.10.2 / D | 83.0.0 | R-setuptools | Pin fixed `83.0.0` |
| urllib3 | [PYSEC-2026-4175](https://github.com/advisories/GHSA-8988-9cw3-xx77)<br>CVE-2026-97687, GHSA-8988-9cw3-xx77 | 2.7.0 / G,D | 2.8.0 | R-urllib3 | Pin fixed `2.8.0` |
| urllib3 | [PYSEC-2026-4176](https://github.com/advisories/GHSA-gh4c-6fx4-qh6g)<br>CVE-2026-97688, GHSA-gh4c-6fx4-qh6g | 2.7.0 / G,D | 2.8.0 | R-urllib3 | Pin fixed `2.8.0` |
| urllib3 | [PYSEC-2026-4177](https://github.com/advisories/GHSA-vxq7-64xx-v4gw)<br>CVE-2026-97689, GHSA-vxq7-64xx-v4gw | 2.7.0 / G,D | 2.8.0 | R-urllib3 | Pin fixed `2.8.0` |
| werkzeug | [CVE-2026-102598](https://github.com/advisories/GHSA-g6x2-hccm-hh4m)<br>GHSA-g6x2-hccm-hh4m | 3.1.8 / D | 3.1.9 | R-werkzeug | Pin fixed `3.1.9` |


### Residual PyJWT advisory (not silently waived)

[GHSA-gvp8-978c-rx2q](https://github.com/advisories/GHSA-gvp8-978c-rx2q) has no published patched release in the audit or the advisory page. Its affected range is >=2.11.0,<=2.13.0 and requires reuse of a mutable `options` object across an insecure `verify_signature=False` decode and a later verified decode. The observed gateway sites construct fresh options dicts on each call; auth.py and Google OAuth require expiry/issuer/audience, and Microsoft OAuth uses its separate explicit issuer comparison rather than turning signature verification off. This application precondition is absent at the observed sites; preserve that property. Pinning 2.15.0 repairs the other listed JWT advisories (including GHSA-42vr-xj54-vc7v), but this no-fix record is retained as **conditional mitigation, no release-fix attestation**. Central auth tests must continue to reject invalid/expired/wrong-audience tokens. SDK/internal option reuse has not been proven absent.

## npm advisory-pair table

Node paths are the audit paths, not a statement that obsolete nested paths survive the regenerated lock. For sharp both audit paths are retained in the same advisory row.

| Package | Advisory | Audited affected range | Audited nodes | Reachability | Disposition |
|---|---|---|---|---|---|
| astro | [GHSA-26w7-cxv4-gfx2](https://github.com/advisories/GHSA-26w7-cxv4-gfx2) — Astro: Remote code execution through AVIF image optimization | `<7.2.8` | `node_modules/astro` | N-astro | Pin fixed `7.3.8`; install/build verification pending |
| astro | [GHSA-376h-93r7-7g6f](https://github.com/advisories/GHSA-376h-93r7-7g6f) — Astro: Authorization bypass from missing path-segment boundary check when stripping the configured base | `<=7.2.3` | `node_modules/astro` | N-astro | Pin fixed `7.3.8`; install/build verification pending |
| devalue | [GHSA-9rgm-9g3h-6x36](https://github.com/advisories/GHSA-9rgm-9g3h-6x36) — Svelte devalue: DoS via malformed input | `<5.9.1` | `node_modules/devalue` | N-devalue | Pin fixed `5.9.4`; install/build verification pending |
| devalue | [GHSA-j22f-vq7h-c4qm](https://github.com/advisories/GHSA-j22f-vq7h-c4qm) — devalue: `stringify`/`uneval` serialize shared memory | `>=5.1.0 <=5.9.2` | `node_modules/devalue` | N-devalue | Pin fixed `5.9.4`; install/build verification pending |
| devalue | [GHSA-hx4r-w6wj-j8fg](https://github.com/advisories/GHSA-hx4r-w6wj-j8fg) — devalue: Residual sparse-array CPU amplification in uneval | `<=5.9.2` | `node_modules/devalue` | N-devalue | Pin fixed `5.9.4`; install/build verification pending |
| devalue | [GHSA-mcm9-63f2-9j32](https://github.com/advisories/GHSA-mcm9-63f2-9j32) — devalue: Repeated primitive strings cause quadratic expansion in uneval | `<=5.9.2` | `node_modules/devalue` | N-devalue | Pin fixed `5.9.4`; install/build verification pending |
| devalue | [GHSA-wf3x-273g-mvxv](https://github.com/advisories/GHSA-wf3x-273g-mvxv) — devalue: Sparse arrays emitted by uneval cause eager allocation when evaluated | `>=1.0.0 <=5.9.2` | `node_modules/devalue` | N-devalue | Pin fixed `5.9.4`; install/build verification pending |
| devalue | [GHSA-x5rw-q4pp-hg5g](https://github.com/advisories/GHSA-x5rw-q4pp-hg5g) — devalue: stringifyAsync can cause an unhandled rejection despite a caught returned promise | `>=5.8.0 <=5.9.2` | `node_modules/devalue` | N-devalue | Pin fixed `5.9.4`; install/build verification pending |
| devalue | [GHSA-4q55-j62x-fr9h](https://github.com/advisories/GHSA-4q55-j62x-fr9h) — devalue: Malformed null-prototype object keys bypass __proto__ rejection via property-key coercion | `<=5.9.2` | `node_modules/devalue` | N-devalue | Pin fixed `5.9.4`; install/build verification pending |
| http-cache-semantics | [GHSA-ch52-4w7c-c8xp](https://github.com/advisories/GHSA-ch52-4w7c-c8xp) — http-cache-semantics max-stale handling can disclose cross-user cached responses | `<=4.2.0` | `node_modules/http-cache-semantics` | N-cache | Pin fixed `4.3.0`; install/build verification pending |
| js-yaml | [GHSA-2883-xcg3-v3hh](https://github.com/advisories/GHSA-2883-xcg3-v3hh) — js-yaml: maxTotalMergeKeys does not limit CPU use for empty merge sources | `>=4.0.0 <4.3.2` | `node_modules/js-yaml` | N-yaml | Pin fixed `4.3.2`; install/build verification pending |
| sharp | [GHSA-rgj7-g3m4-5g8c](https://github.com/advisories/GHSA-rgj7-g3m4-5g8c) — sharp: Vulnerabilities in libheif: GHSA-g89c-p67h-r497 and GHSA-2jg2-4ch7-h545 | `<0.35.4` | `node_modules/miniflare/node_modules/sharp`<br>`node_modules/sharp` | N-sharp | Pin fixed `0.35.5`; install/build verification pending |
| sharp | [GHSA-wq5f-xc86-pv6w](https://github.com/advisories/GHSA-wq5f-xc86-pv6w) — sharp : Vulnerability in librsvg dependency CVE-2026-96889 | `<0.35.5` | `node_modules/miniflare/node_modules/sharp`<br>`node_modules/sharp` | N-sharp | Pin fixed `0.35.5`; install/build verification pending |
| smol-toml | [GHSA-r4xh-jqrq-34v2](https://github.com/advisories/GHSA-r4xh-jqrq-34v2) — smol-toml: Quadratic-time parse() from parseKey rescanning to end of document on each key line | `<=1.8.0` | `node_modules/smol-toml` | N-toml | Pin fixed `1.9.0`; install/build verification pending |
| source-map-js | [GHSA-68fv-2mgg-jv7q](https://github.com/advisories/GHSA-68fv-2mgg-jv7q) — source-map-js allows event-loop denial of service through indexed source-map section offsets | `>=1.0.0 <1.2.2` | `node_modules/source-map-js` | N-sourcemap | Pin fixed `1.2.2`; install/build verification pending |
| svgo | [GHSA-w27v-7q3p-w38r](https://github.com/advisories/GHSA-w27v-7q3p-w38r) — SVGO: removeScripts allows executable links through namespace and control-character bypasses | `>=4.0.0 <4.1.0` | `node_modules/svgo` | N-svgo | Pin fixed `4.1.0`; install/build verification pending |
| svgo | [GHSA-4vpr-x523-8j87](https://github.com/advisories/GHSA-4vpr-x523-8j87) — SVGO: removeScripts incompletely sanitizes executable HTML in SVG foreignObject elements | `>=4.0.0 <4.1.0` | `node_modules/svgo` | N-svgo | Pin fixed `4.1.0`; install/build verification pending |
| undici | [GHSA-3wwx-pv8p-q78v](https://github.com/advisories/GHSA-3wwx-pv8p-q78v) — undici vulnerable to Denial of Service via unhandled error in WebSocket permessage-deflate decompression | `>=7.28.0 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |
| undici | [GHSA-pmjh-fq2x-6v4x](https://github.com/advisories/GHSA-pmjh-fq2x-6v4x) — undici vulnerable to Denial of Service via orphaned RetryHandler response body | `>=7.11.0 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |
| undici | [GHSA-r53p-7pc4-xj5r](https://github.com/advisories/GHSA-r53p-7pc4-xj5r) — undici vulnerable to downstream response splitting via retry interceptor | `>=7.0.0 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |
| undici | [GHSA-rfgv-xxqx-mfg5](https://github.com/advisories/GHSA-rfgv-xxqx-mfg5) — undici vulnerable to Denial of Service via unrequested WebSocket subprotocol | `>=7.0.0 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |
| undici | [GHSA-3xpg-4rpp-hhhm](https://github.com/advisories/GHSA-3xpg-4rpp-hhhm) — undici vulnerable to Denial of Service via unbounded decompression of compressed responses | `>=7.15.0 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |
| undici | [GHSA-2jfj-6hjv-fm6j](https://github.com/advisories/GHSA-2jfj-6hjv-fm6j) — undici vulnerable to cross-user cookie disclosure via Set-Cookie caching in shared caches | `>=7.0.0 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |
| undici | [GHSA-2gqq-gqf2-x968](https://github.com/advisories/GHSA-2gqq-gqf2-x968) — undici vulnerable to response truncation via oversized chunked responses in the dump interceptor | `>=7.1.0 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |
| undici | [GHSA-w293-vg96-wgc3](https://github.com/advisories/GHSA-w293-vg96-wgc3) — undici vulnerable to TLS certificate validation bypass via dropped connect options in BalancedPool | `>=7.24.1 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |
| undici | [GHSA-8436-99hf-9mmv](https://github.com/advisories/GHSA-8436-99hf-9mmv) — undici vulnerable to caching and replay of unsafe HTTP method responses | `>=7.0.0 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |
| undici | [GHSA-rx4f-c7p8-82vq](https://github.com/advisories/GHSA-rx4f-c7p8-82vq) — undici vulnerable to Denial of Service via WebSocketStream unclean close | `>=7.0.0 <7.29.1` | `node_modules/undici` | N-undici | Pin fixed `7.29.1`; install/build verification pending |


### Inherited npm findings

| Audited package | via | Audited nodes | Reachability | Disposition |
|---|---|---|---|---|
| miniflare | sharp | `node_modules/miniflare` | N-miniflare | Wrangler 4.149.0 → Miniflare 5.20261006.1-alpha → sharp 0.35.5 / undici 7.29.1; exact overrides enforce fixed children |
| miniflare | undici | `node_modules/miniflare` | N-miniflare | Wrangler 4.149.0 → Miniflare 5.20261006.1-alpha → sharp 0.35.5 / undici 7.29.1; exact overrides enforce fixed children |
| wrangler | miniflare | `node_modules/wrangler` | N-wrangler | Wrangler 4.149.0 → Miniflare 5.20261006.1-alpha → sharp 0.35.5 / undici 7.29.1; exact overrides enforce fixed children |


## Parent installation and verification commands (not run here)

Run only in disposable environments; do not replace the currently open user runtime or deployment. Package-index contact below is for pinned public-package installation/lock resolution; the authenticated runtime remains offline. The gateway suite uses its existing local fixtures; configure no provider credentials or external deployment endpoints.

### Authenticated runtime (only after reviewed lock + wheelhouse are supplied)

Use an absolute path to a trusted CPython base outside the candidate venv and a target-compatible reviewed wheelhouse. The absolute paths below are operator-supplied prerequisites, not checked-in approvals.

```bash
export CHUDWARE_BASE_PYTHON=/absolute/path/to/trusted/python3
export CHUDWARE_REVIEWED_WHEELHOUSE=/absolute/path/to/reviewed/wheelhouse
"$CHUDWARE_BASE_PYTHON" -m services.installer.provision --base-python "$CHUDWARE_BASE_PYTHON" --wheelhouse "$CHUDWARE_REVIEWED_WHEELHOUSE" --venv-dir scratch/security-dependencies-runtime
scratch/security-dependencies-runtime/bin/python -m pip check
scratch/security-dependencies-runtime/bin/python -m pytest services/installer/test_wheelhouse.py services/plm/test_remote.py services/plm/test_remote_transports.py services/plm/test_plm_vault.py services/mcp/test_office_tools.py services/agent/test_chudcode_tools.py services/dag -q
```

The reviewed closure must include compatible target wheels for the new pins, the private chudcode-rpc bytes/source provenance, the existing pytest/test dependencies for test runs, and the release declarations if building sidecars. Root declarations cannot be installed from public PyPI. Do not copy the old runtime stamp into the disposable venv.

### Gateway public dependencies

```bash
python3 -m venv scratch/security-dependencies-gateway
scratch/security-dependencies-gateway/bin/python -m pip install --only-binary=:all: --disable-pip-version-check pip==26.2.0 pytest==9.1.1 -r Webservice/gateway/requirements.txt
scratch/security-dependencies-gateway/bin/python -m pip check
PYTHONPATH=Webservice scratch/security-dependencies-gateway/bin/python -m pytest Webservice/gateway/tests -q
```

The gateway venv uses only public deployment packages plus the pinned test runner; it does not fetch the private SDK or require operator credentials. The gateway fixtures require local PostgreSQL initdb/pg_ctl binaries and a non-root caller, and by default start an isolated throwaway Unix-socket cluster (Webservice/gateway/tests/conftest.py:1-11). Do not point CHUDWARE_TEST_DB_URL at production or mutate a live deployment venv to obtain this evidence.

### Static site

```bash
npm --prefix Webservice/public-site ci --ignore-scripts --no-audit --no-fund
npm --prefix Webservice/public-site ls astro wrangler miniflare sharp undici devalue http-cache-semantics js-yaml smol-toml source-map-js svgo
npm --prefix Webservice/public-site run build
npm --prefix Webservice/public-site run verify:product-images
```

Use Node >=22.12.0 and npm >=9.6.5. Do not run wrangler deploy/publish or provider calls. npm ci must accept the committed package/lock pair without rewriting it; verify each affected surviving node is outside the audit ranges, including both native decoder paths.

### Audit disposition coverage

Central verification should expand the Python source column against all four input inventories and compare the 192 source/package/advisory occurrences, then compare all 27 npm dictionary-via records and all 3 inherited string-via records. Keep input audit JSON immutable so the original vulnerable inventory remains reviewable. No build/test/lint/formatter/smoke/audit verification was run in this dependency worker.
