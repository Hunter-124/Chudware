# Chudware public site

Static marketing site for `chudware.com`, deployed to **Cloudflare Pages**. It is deliberately
plain: hand-written HTML and one stylesheet, **no framework, no build step, no bundler, no
external CDN, no JavaScript at all**. What is in this directory is byte-for-byte what gets served.

The application itself lives at `app.chudware.com` behind Cloudflare Access and is not part of
this deploy. Nothing here talks to the origin, so public and bot traffic never reaches the home
server.

```
public-site/
  index.html        overview / hero / capabilities
  pricing.html      three tiers (PLACEHOLDER PRICES — see below)
  signup.html       how the access model works
  404.html          Pages serves this automatically for unknown paths
  _headers          CSP + security headers (Cloudflare Pages format)
  assets/
    site.css        the only stylesheet
    logo.svg        tesseract mark, extracted from the app portal's favicon data-URI
```

## Deploy

From **this directory** (`Webservice/public-site/`).

One-time, to create the Pages project:

```sh
npx wrangler@latest pages project create chudware-site --production-branch=main
```

Every deploy after that:

```sh
npx wrangler@latest pages deploy . --project-name=chudware-site --branch=main --commit-dirty=true
```

`--branch=main` is what makes it a **production** deploy; any other branch name publishes a
preview URL instead, which is the safe way to look at a change before it is live:

```sh
npx wrangler@latest pages deploy . --project-name=chudware-site --branch=preview
```

Wrangler needs `CLOUDFLARE_API_TOKEN` (scope: *Cloudflare Pages — Edit*) and
`CLOUDFLARE_ACCOUNT_ID` in the environment, or an interactive `npx wrangler login`.

### Manual dashboard alternative (no CLI, no token)

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages** → **Upload assets**.
2. Project name `chudware-site` → **Create project**.
3. Drag the **contents** of `Webservice/public-site/` into the upload box — the files must be at
   the root of the upload, not nested inside a `public-site/` folder, or every path 404s.
4. **Deploy site**. The result is live at `chudware-site.pages.dev`.
5. Redeploying later: project → **Deployments** → **Create deployment** → upload again.

### Custom domain

**Do not do this until the owner says so** — it changes live DNS.

Pages project → **Custom domains** → **Set up a domain** → `chudware.com` (and `www.chudware.com`
if wanted). Cloudflare writes the CNAME itself. `app.chudware.com` is a separate record pointing
at the tunnel and must not be touched.

## Editing notes

- **Placeholder prices.** `pricing.html` ships `$49/month` (Individual) and `$79/seat/month`
  (Team) purely as layout filler, plus several `TBD` allowances. They carry a visible
  `Placeholder price` tag and a page-level notice. Replace the numbers *and delete the
  `<p class="ph-tag">` tags and the `.notice` block* before launch.
- **Screenshot slot.** `index.html` has a placeholder frame:
  ```html
  <div class="shot-slot" role="img" aria-label="…"><span>Screenshot placeholder</span></div>
  ```
  Replace that one element with a real image and keep the frame around it:
  ```html
  <img src="assets/app-screenshot.png" width="1600" height="900"
       alt="A Chudware modeling session: feature tree on the left, part in the viewport, copilot panel on the right.">
  ```
  Then delete the `figcaption` that says it is a placeholder. `img-src 'self'` in `_headers`
  already allows it; no CSP change needed.
- **No inline `<script>` or `style=` attributes.** The CSP is `default-src 'none'` with no
  `script-src` and no `'unsafe-inline'`, so anything inline is silently dropped by the browser.
  If a page ever needs JS, it must be an external file *and* a matching `script-src 'self'`.
- **Colours** come from CSS custom properties at the top of `site.css` and are shared with the
  app portal. Text/`--muted` on `--bg` is 6.0:1, the primary button label is 5.8:1, and the
  interactive border token `--line-ui` is 3.5:1 — keep new colours above those floors.
- **Local preview**: the pages open straight off the filesystem, or
  `python3 -m http.server 8080` from this directory if you want the root-absolute paths in
  `404.html` to resolve.
