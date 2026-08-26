# Box.com mirror seam (`aicad.box_mirror/1`)

Optional cloud MIRROR of a project's saved files on Box.com. Live edits stay
local (documents, autosaves, PLM vault); Box is write-only from Chudware's side
and is never a source of truth. Implementation: `services/plm/box_mirror.py`.

## §1 Config (`config.json` `box` block)

```json
"box": {
  "enabled": false,
  "folder_id": "0",
  "max_file_mb": 512,
  "auth": {
    "method": "developer_token",
    "developer_token": "",
    "client_id": "",
    "client_secret": "",
    "access_token": "",
    "refresh_token": "",
    "access_token_expires_at": 0
  }
}
```

Ownership: the GUI owns `enabled`, `folder_id`, `max_file_mb`, `auth.method`,
`auth.developer_token`, `auth.client_id`, `auth.client_secret`. Python owns the
ROTATING fields `auth.access_token`, `auth.refresh_token`,
`auth.access_token_expires_at` (Box rotates the refresh token on every refresh;
a GUI save that clobbers them revokes the grant). Every writer nested-overlays
the `box`/`box.auth` objects — the same discipline as `keys`/`experimental`.

`auth.method` ∈ `developer_token` (60-minute paste-in token, bring-up only) |
`oauth` (Box Platform app authorization-code flow, loopback redirect
`http://127.0.0.1:53682/box`, which the Box app must whitelist).

## §2 Mirror semantics

- Trigger points spool, never upload inline: post-`Vault.save` commit
  (`services/plm/vault.py`), verified agent `_project_save`
  (`services/mcp/cad_common.py`), and the GUI post-save notify RPC.
- Spool: one JSON entry per project (coalesced) under appdata
  `box_mirror/spool/`; survives process exits. A drain uploads and consumes it;
  the fresh-per-turn agent worker drains with a bounded budget in
  `save_touched_docs`; the long-lived GUI worker drains asynchronously.
- Layout: `<folder_id>/<project-name>/<project-relative path>`. Dot files and
  dot directories (`.aicadvault` above all) are never mirrored; files over
  `max_file_mb` are skipped and counted, not fatal.
- Change detection: per-project state (Box ids + content sha1) under appdata
  `box_mirror/state-*.json`; unchanged files cost no request. Changed files
  upload as NEW VERSIONS of the same Box file id; `409 item_name_in_use`
  adopts the existing Box item.

## §3 Tool envelopes (registered in the `plm` family)

| tool | params | returns |
|---|---|---|
| `box_sync` | `{path?}` (project root, defaulted like `plm_*`) | `{ok, projects:[{project, uploaded, updated, unchanged, skipped_large}]}` / `{ok:false, reason, hint}` |
| `box_status` | `{path?}` | `{ok, enabled, method, configured, connected?, account?{id,name,login}, folder_id, pending, project?{last_sync,last_error,mirrored_files}}` |

Risk: `box_sync` is `RISK_EXTERNAL` (real network egress); `box_status` is read.

## §4 Worker RPCs (GUI surface, `services/plm/worker_rpc.py`)

| method | params | returns |
|---|---|---|
| `box_status` | `{path?, live?}` | as §3 `box_status`; `live:true` verifies the token against `/users/me` |
| `box_sync` | `{path}` | as §3 `box_sync` (long-running — call async) |
| `box_notify_save` | `{path}` (saved file) | `{ok}` — spools the file's project, drains in the background |
| `box_oauth_login` | `{}` | `{ok, authorize_url, redirect_uri, hint}` — caller opens the URL; tokens persist automatically on redirect |
