# T-H5 Questions & Decisions
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

## Q1: DVC remote backend choice (S3 bucket vs. local/NAS path)

**Question (sonnet-bree):** The task requires an S3-compatible remote for fleet-wide
sync. Before the first real upload, Erik must decide the bucket name, region, and
IAM credentials. What should the shipped default be?

**Recommended answer:** Ship with a plain local directory path as the default remote
(`/data/chudware-training` or user-configured). DVC makes the cloud swap a one-line URL
change (path → `s3://bucket/prefix`). No code change required.

**ANSWER (Fable): APPROVED per Erik 2026-07-02.** DVC remote = LOCAL/NAS (plain
absolute path) for now, not cloud. Remote is a config value; cloud is a one-line swap.
Implementation: `dvc_init.py` `remote_url` parameter defaults to a local path;
`flywheel export --remote /path/to/nas` overrides at runtime.

**Erik provisioning action needed (when ready):**
- Decide NAS mount point or cloud bucket (s3://bucket/chudware-training-data)
- For S3: create IAM user with `s3:PutObject` / `s3:GetObject` on that prefix
- Run: `python -m tools.flywheel.dvc_init --repo training --remote s3://bucket/prefix init`

---

## Q2: Argilla hosting machine

**Question (sonnet-bree):** Argilla requires a persistent server. Which machine should
host it, and should it run on a custom port?

**Recommended answer:** hunter-pc (localhost), port 6900 via Docker Compose. The
client already defaults to `http://localhost:6900`.

**ANSWER (Fable): APPROVED per Erik 2026-07-02.** Argilla + MLflow = Docker Compose
on hunter-pc (localhost). `docker-compose.argilla.yml` ships pointing at localhost:6900.
Argilla client remains optional/stubbed so all tests pass offline without containers.

**Erik provisioning action needed (when ready):**
1. `cp tools/flywheel/.env.argilla.example tools/flywheel/.env.argilla`
2. Edit `.env.argilla` — set `ARGILLA_API_KEY` and `ARGILLA_AUTH_SECRET_KEY`
3. `docker compose -f tools/flywheel/docker-compose.argilla.yml --env-file tools/flywheel/.env.argilla up -d`
4. Set `ARGILLA_API_URL=http://localhost:6900` and `ARGILLA_API_KEY=<key>` in shell
   or in `~/.local/share/Chudware/config.json` under `"argilla"` key
5. Load first batch: `python -c "from tools.flywheel.argilla_loader import load_shards_to_argilla; ..."`

---

## Q3: Consent/corpus policy for external users

**Question (sonnet-bree):** Should external users default to `meta` tier rather than
`off`, or stay at `off`?

**Recommended answer:** Keep external default at `off` (conservative; no surprise
data sharing). Team-dogfood corpus (full consent) is the first corpus; external opt-in
is a later product decision.

**ANSWER (Fable): CONFIRMED per Erik 2026-07-02.** External default stays OFF. Team
dogfood full-consent corpus first. This matches the telemetry contract §3 defaults table.

---

## Implementation notes for the foreman

- DVC `file://` URIs do not work on Linux (DVC resolves them as relative paths). Use
  plain absolute paths. This is documented in `dvc_init.py` and the test.
- Argilla server is Apache-2.0 (Docker). Client is Apache-2.0 (in-process, optional).
- All licenses.md rows added in the same T-H5 commit.
- `training/` directory layout is documented in `tools/flywheel/README.md`.
- DoD #3 (Argilla 50-record loop) is proven via `ArgillaStub` offline; live Argilla
  needs the docker-compose stack running — marked `[~] needs-argilla-box` in task DoD.
