# Chudware Web — credential setup walkthrough

Every external credential the gateway can consume, what to type into each
provider's console, and where the value lands. Companion to `RUNBOOK.md` (which
covers operating the deployment) and `STRIPE_CATALOG.md` (which is the generated
catalog record).

Nothing here is optional-by-accident: each section says what breaks if you skip
it, so you can stop at the subset you actually need.

## 0. The origin, before anything else

Every redirect URI, webhook URL and mailed link is built from one setting:

```
CHUDWARE_WEB_PUBLIC_BASE_URL=https://app.chudware.com
```

**`app.chudware.com` is the gateway. The apex `chudware.com` and `www` are a
Cloudflare Pages static site with no origin route to the gateway at all**
(`RUNBOOK.md` §topology). A redirect URI or webhook pointed at the apex reaches
the marketing site and fails — and a verification link built from it mails
customers into a static page. Use the `app.` host everywhere below.

Local development instead uses `http://127.0.0.1:8300`, and every URI below
changes to match.

Gateway environment values (not the entitlement signing key) live in one
gitignored, mode-600 file:

| Deployment | File |
|---|---|
| production | `~/chudware-web-data/gateway.env` |
| staging | `~/chudware-web-staging/gateway.env` |

`deploy/setup.sh` writes the skeleton. Never commit it, and never put these
values in a repo file, a systemd unit's `Environment=`, or a command line.

**Quote any value containing shell metacharacters** — `<`, `>`, `|`, `&`, spaces
with punctuation. systemd's `EnvironmentFile` strips surrounding quotes, but
`RUNBOOK.md` also sources this file with `set -a; . gateway.env`, where an
unquoted `CHUDWARE_MAIL_FROM=Chudware <noreply@chudware.com>` is a shell
redirection: the source aborts on a syntax error and **every variable after that
line silently goes unset**. Verify with `bash -c 'set -a; . gateway.env'` after
editing.

### Online entitlement signing credential

Run `Webservice/deploy/provision_entitlement_credential.sh prod` as the production
service account, supplying exactly 32 raw bytes on stdin from the owner's secure
key source. Staging must use its dedicated `chudware-staging` account and
`/var/lib/chudware-staging` home with the `staging` argument; it cannot provision
production credentials. Do not put key bytes in argv, shell variables, or env
files. Use `--replace` only for an intentional rotation.

The helper validates the bounded input in memory, then streams it to
`systemd-creds encrypt` on stdin. It never creates a plaintext temporary file;
only the encrypted output is staged in the user-owned mode-700
`~/.config/credstore.encrypted` directory before atomic mode-600 placement.
Failed encryption leaves the previous encrypted credential intact and removes
the encrypted temporary output. The gateway's existing startup checks still
require a private, correctly owned signing credential whose public half matches
the shipped entitlement root; provisioning does not bypass those checks.

## 1. Session secret — REQUIRED

It signs the single-use OAuth `state` parameter. The gateway **refuses to boot**
if any OAuth client pair is configured without it, because the callback would
have no CSRF protection at all (`config.py` `__post_init__`).

```bash
printf 'CHUDWARE_WEB_SESSION_SECRET=%s\n' \
  "$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
  >> ~/chudware-web-data/gateway.env
```

Rotating it does not log anybody out — sessions are database rows, not signed
blobs.

## 2. Google sign-in — optional

[console.cloud.google.com](https://console.cloud.google.com/) → your project →
**Google Auth Platform → Clients → Create client**.

| Field | Value |
|---|---|
| Application type | **Web application** |
| Name | `chudware` (console-only label) |
| Authorized JavaScript origins | *leave empty* — the browser never calls Google directly; the gateway does the code exchange server-side |
| Authorized redirect URIs | `https://app.chudware.com/api/auth/oauth/google/callback` |

Under **Audience**, set the user type. **External** + *Publishing status:
Testing* limits sign-in to test users you list explicitly; publish it when you
want the public to sign in. Scopes stay at the defaults — the gateway requests
only `openid email`.

```
CHUDWARE_WEB_GOOGLE_CLIENT_ID=<Client ID>
CHUDWARE_WEB_GOOGLE_CLIENT_SECRET=<Client secret>
```

The email is taken from Google's signed `id_token`, verified against Google's
published JWKS — never from an unauthenticated claim.

## 3. GitHub sign-in — optional

[github.com/settings/developers](https://github.com/settings/developers) →
**OAuth Apps → New OAuth App**.

| Field | Value |
|---|---|
| Application name | `Chudware` — this is shown on the consent screen users see |
| Homepage URL | `https://app.chudware.com` — **the site's front page, not the callback** |
| Application description | free text, shown to users |
| Authorization callback URL / Redirect URI | `https://app.chudware.com/api/auth/oauth/github/callback` |
| Allow wildcard matching | **off** |
| Enable Device Flow | **off** — the gateway uses the standard web flow |
| Expire user access tokens | on (harmless; the gateway makes one `/user/emails` call and keeps no token) |

Putting the callback in **Homepage URL** and leaving **Redirect URI** blank is
the common mistake: the authorize request is then rejected for a missing
redirect, and the homepage link on the consent screen sends users to a bare API
endpoint.

```
CHUDWARE_WEB_GITHUB_CLIENT_ID=<Client ID>
CHUDWARE_WEB_GITHUB_CLIENT_SECRET=<generated client secret>
```

GitHub issues no identity token, so the gateway makes a second authenticated
call to `GET /user/emails` and accepts only the address GitHub marks **primary
and verified**. A user whose primary email is unverified cannot sign in — that
is deliberate.

## 4. Microsoft sign-in (Entra ID) — optional, org + personal accounts

[entra.microsoft.com](https://entra.microsoft.com/) → **App registrations → New
registration**.

| Field | Value |
|---|---|
| Name | `Chudware` |
| Supported account types | **Accounts in any organizational directory and personal Microsoft accounts** (the console may label this "Any Entra ID Tenant + Personal Microsoft accounts") |
| Redirect URI platform | **Web** — not SPA, not public client; the gateway holds a client secret and exchanges the code server-side |
| Redirect URI | `https://app.chudware.com/api/auth/oauth/microsoft/callback` |

That account-type choice is what makes both work/school accounts **and**
personal Outlook.com / Hotmail accounts sign in, and it is the one that pairs
with `CHUDWARE_WEB_MICROSOFT_TENANT=common`.

Then **Certificates & secrets → New client secret**. Copy the **Value** column
immediately — Microsoft never shows it again (the Secret ID is not the secret).

### 4a. The `xms_edov` claim — mandatory, and the step that gets missed

**Token configuration → Add optional claim → ID → check `email` → Add.** Accept
the prompt about turning on the required Microsoft Graph permission.

`xms_edov` is not in that picker. Add it in **Manage → Manifest**, inside
`optionalClaims.idToken`:

```json
"optionalClaims": {
    "idToken": [
        { "name": "email",     "essential": false, "additionalProperties": [] },
        { "name": "xms_edov",  "essential": false, "additionalProperties": [] }
    ],
    "accessToken": [],
    "saml2Token": []
}
```

Save, then sign in once to confirm.

**Why it is not optional here.** `link_account()` binds a provider identity to
an account *by email*. In a multi-tenant app, any tenant administrator can set
an arbitrary, unverified `email` on a user in their own directory — including an
address at a domain they do not own — and would thereby take over the matching
Chudware account. That is the published **nOAuth** vector.
`xms_edov` is Microsoft asserting the domain is verified as owned by the issuing
tenant, so it is the only condition under which the claim is proof instead of
tenant-controlled input. Without a true `xms_edov`, `oauth.py` **refuses the
sign-in** rather than trusting it.

Per Microsoft's optional-claims reference, an MSA-sourced email counts as
domain-verified, so personal Outlook.com accounts get `xms_edov: true` and work
normally once the claim is configured.

```
CHUDWARE_WEB_MICROSOFT_CLIENT_ID=<Application (client) ID>
CHUDWARE_WEB_MICROSOFT_CLIENT_SECRET=<secret Value>
CHUDWARE_WEB_MICROSOFT_TENANT=common
```

`TENANT` is part of every endpoint URL:

| Value | Who can sign in |
|---|---|
| `common` | work/school **and** personal Microsoft accounts |
| `organizations` | work/school only |
| `consumers` | personal only |
| a tenant GUID or verified domain | exactly one directory, enforced against the token's own `tid` |

Identity is keyed on `tid.oid`, not `sub`: `sub` is a pairwise pseudonym that
would rotate if the app registration were recreated, orphaning every link.

## 5. Transactional mail — REQUIRED before taking public signups

The default backend is `console`, which writes the rendered message to the
gateway log and delivers nothing. Verification links, password resets and
dunning notices are then silently undeliverable while the gateway reports
perfect health. A backend named without its credential refuses to boot rather
than queueing mail that can never leave.

Resend (simplest):

```
CHUDWARE_MAIL_BACKEND=resend
CHUDWARE_RESEND_API_KEY=re_...
CHUDWARE_MAIL_FROM=Chudware <noreply@chudware.com>
```

Any SMTP relay (Amazon SES above Resend's 100/day free tier):

```
CHUDWARE_MAIL_BACKEND=smtp
CHUDWARE_SMTP_HOST=email-smtp.us-east-1.amazonaws.com
CHUDWARE_SMTP_PORT=587
CHUDWARE_SMTP_USER=...
CHUDWARE_SMTP_PASS=...
CHUDWARE_SMTP_TLS=starttls
```

There is no plaintext option: `starttls` requires the relay to advertise
STARTTLS or delivery fails rather than sending the password in clear.
`CHUDWARE_MAIL_FROM` must be on a domain verified with the chosen provider (SPF,
DKIM, DMARC published) or every message is rejected or spam-filed.

**This is sending only.** Nothing in this repository receives mail; inbound
`hunter@chudware.com` is a free Cloudflare Email Routing forward, provisioned
by `deploy/cf_mail.py` — see **Mail** below. Admin sign-in does **not** need a
mailbox at all — see §7.

## Mail — inbound forwarding and the mail DNS records

`deploy/cf_mail.py` owns chudware.com's mail plumbing idempotently, using the
same token as `cf_provision.py` (`$CF_API_TOKEN`, else the gitignored
`Webservice/Cloudflare-info.txt`; needs DNS Write + Email Routing Rules Write +
Email Routing Addresses Write):

```bash
python3 Webservice/deploy/cf_mail.py status                 # routing state, destinations, rules, mail TXT
python3 Webservice/deploy/cf_mail.py dns                    # upsert SPF + DMARC, prints CREATE/UPDATE/NO-OP
python3 Webservice/deploy/cf_mail.py ensure --to you@gmail.com   # forward hunter@chudware.com there
```

`dns` publishes SPF `v=spf1 include:_spf.mx.cloudflare.net include:_spf.google.com ~all`
(the Google include is for outbound through `smtp.gmail.com`) and `_dmarc`
`v=DMARC1; p=none; rua=mailto:hunter@chudware.com`. The MX rows and the
`cf2024-1._domainkey` DKIM TXT are Cloudflare-managed and read-only.

**Human step:** `ensure` creates the destination address, and Cloudflare then
emails a verification link to it. Until the mailbox owner clicks that link,
`status` shows the destination `UNVERIFIED` and **forwarding silently does not
deliver**. No catch-all rule is created, so only `hunter@chudware.com` routes.

## 6. Stripe billing — optional until you charge

Entirely optional; unset is a supported state. Once *any* subscription
credential is set, the secret key, webhook secret and all four subscription
prices are required together or the gateway refuses to boot.

Do not create products by hand in the dashboard. `deploy/provision_stripe.py`
owns the catalog declaratively and idempotently — four products, seven prices, a
customer-portal configuration and the production webhook endpoint
(`STRIPE_CATALOG.md`).

The webhook endpoint is provisioned in **live mode only**. One gateway verifies
one signing secret, so a test-mode endpoint at `app.chudware.com` is rejected
with 400 the moment live keys land, and Stripe auto-disables any endpoint that
keeps failing (it did exactly that on 2026-09-05, when the sandbox endpoints
existed but `gateway.env` had no Stripe keys and every delivery got a 503).
Staging has no public ingress, so it gets no endpoint either; test-mode
delivery is `stripe listen --forward-to 127.0.0.1:8310/api/billing/webhook`
against a local gateway, with the CLI's printed `whsec_` as that gateway's
`CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET`.

1. Create the Chudware Stripe account; enable **Stripe Tax** (products carry the
   SaaS tax code `txcd_10103001`).
2. Note the account id (`acct_…`, Settings → Account details) and the **test**
   secret key (Developers → API keys).
3. Preview offline — `--dry-run` loads no secret and contacts nothing — then
   apply the sandbox catalog (products, prices, portal; no webhook):

```bash
export STRIPE_SECRET_KEY='sk_test_...'
export CHUDWARE_STRIPE_ACCOUNT='acct_...'
python3 Webservice/deploy/provision_stripe.py --dry-run --expect-account "$CHUDWARE_STRIPE_ACCOUNT"
python3 Webservice/deploy/provision_stripe.py --apply   --expect-account "$CHUDWARE_STRIPE_ACCOUNT"
```

4. Paste the emitted `CHUDWARE_STRIPE_PRICE_*` fragment into the staging
   `gateway.env`. Rerun `--apply`; every row must report `NO-OP`.
5. Going live needs the account activated (business details, bank account) and
   explicit `--live` acknowledgement. This run also creates the production
   webhook and prints its signing secret **once** — Stripe returns it only on
   creation, a no-op rerun cannot recover it. Paste the price fragment **and**
   the `CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET` line into the production
   `gateway.env` before anything else; a live endpoint whose gateway answers
   503 gets disabled again.

```bash
export STRIPE_SECRET_KEY='sk_live_...'
python3 Webservice/deploy/provision_stripe.py --dry-run --live --expect-account "$CHUDWARE_STRIPE_ACCOUNT"
python3 Webservice/deploy/provision_stripe.py --apply   --live --expect-account "$CHUDWARE_STRIPE_ACCOUNT"
```

If the secret was lost, delete the endpoint in the dashboard (Developers →
Webhooks) and rerun `--apply --live`: the provisioner recreates it and prints a
fresh secret. systemd loads `gateway.env` only at unit start, so
`systemctl --user restart chudware-gateway` after editing it; confirm with
`curl -X POST https://app.chudware.com/api/billing/webhook` → `400 missing
Stripe-Signature header` (was `503 billing not configured`).

Resulting env:

```
CHUDWARE_WEB_STRIPE_SECRET_KEY=sk_live_...
CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET=whsec_...
CHUDWARE_WEB_STRIPE_SUCCESS_URL=https://app.chudware.com/?billing=success
CHUDWARE_WEB_STRIPE_CANCEL_URL=https://app.chudware.com/?billing=cancel
CHUDWARE_STRIPE_PRICE_DESKTOP=price_...          # $30/mo
CHUDWARE_STRIPE_PRICE_DESKTOP_EDU=price_...      # $15/mo
CHUDWARE_STRIPE_PRICE_STUDIO=price_...           # $150/mo
CHUDWARE_STRIPE_PRICE_STUDIO_EDU=price_...       # $75/mo
# Credit packs are optional independently; an unset pack returns 503 rather
# than accepting an amount supplied by the browser.
CHUDWARE_STRIPE_PRICE_CREDITS_STARTER=price_...  # $10 / 8k credits
CHUDWARE_STRIPE_PRICE_CREDITS_STANDARD=price_... # $25 / 20k credits
CHUDWARE_STRIPE_PRICE_CREDITS_BULK=price_...     # $100 / 80k credits
```

## 7. The first admin — no mailbox required

`role` has no HTTP writer by design: it is the one column an authenticated
caller must never be able to move. The only writer is the operator CLI, run
against the store with database credentials.

```bash
cd Webservice/gateway
.venv/bin/python -m authn grant-admin --email hunter@chudware.com
```

It registers the address if needed, prompts twice for a password (never argv,
which is world-readable in `/proc`), marks the address verified, activates the
account and grants `admin`. Verification is granted by your database access
standing in for a mailed link — which is why this works on a deployment whose
mail backend is still `console`, the exact state a first admin is created in.
Idempotent: rerun it to reset a forgotten password.

Then sign in at `https://app.chudware.com/signin` and open `/admin`.

That `role` grant is the *whole* `/admin` allowlist: there is no Cloudflare Access
policy to add the address to as well. If `/admin` shows a Cloudflare login page
instead of the gateway's, the retired `app.chudware.com/admin*` Access application
still exists — delete it in Zero Trust (`RUNBOOK.md` §(b)).

## 8. AI provider keys (server-side agent turns)

Not gateway settings. Shared keys go in a separate mode-600 file,
`~/chudware-web-data/ai_providers.env`, as bare `KEY=VALUE` lines:

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
OPENROUTER_API_KEY=...
XAI_API_KEY=...
GEMINI_API_KEY=...
```

`engine_bridge.py` resolves these server-side as the fallback when the host
config carries no `keys.*` slot. They are never sent to the browser, and
client-supplied keys are stripped from agent-turn RPCs. Web turns spend the host
account's quota, so `harden_host.sh` can disable them entirely.

## 9. Verify

```bash
systemctl --user restart chudware-gateway
systemctl --user status chudware-gateway --no-pager      # boot-time config validation
Webservice/deploy/verify_public.sh                        # public surface + admin gating
```

A configured provider appears as a sign-in button; `oauth.configured_providers()`
lists only providers with **both** halves of their credentials present, so a
half-filled pair silently shows nothing. Check the journal for the boot-time
`ValueError` if the gateway refuses to start — every requirement above fails at
boot rather than on a customer's first request.
