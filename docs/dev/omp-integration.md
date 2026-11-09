# Chudware and Oh My Pi integration

Online turns use the official [Oh My Pi](https://github.com/can1357/oh-my-pi) (OMP) runtime and its `omp_rpc` Python client, not a fork or a second provider loop. The immutable upstream source commit, SDK archive and binary digests are recorded in [`vendor/omp/OMP_ARTIFACT.lock`](../../vendor/omp/OMP_ARTIFACT.lock); `requirements.txt` pins the SDK source archive. Preserve the bundled license and third-party notices when distributing the runtime. See [releasing](../reference/RELEASING.md) for packaging and signing prerequisites.

Runtime discovery checks an explicit binary or `CHUDWARE_OMP_BIN`, then `omp` beside the active Python executable; PATH lookup is developer-only. Preserve the interpreter's virtualenv path rather than resolving its symlink to the system Python directory. The trusted extension uses the same adjacent-payload rule, with the installed source module as its development fallback.

OMP owns provider authentication, model metadata, session journals, nested agents, memory and compaction. Chudware owns the workspace, CAD registry, document operations, policy, roles, missions, approvals and event presentation. Online tool-internal helpers use `services/agent/providers/omp_provider.py` and `OmpSessionHost.complete_text` with scoped settings and cancellation. The experimental browser deployment has no provider credential route to tenant cells; do not mistake provider setup discovery for a usable online model there. The one exception is a public-demo container (`CHUDWARE_WEB_DEMO=1`, see [web/README.md](../../web/README.md#public-demo-mode)): its actor sandboxes reach only a fixed loopback lane to the front door's free-model allowlist (`services/agent/demo_lane.py`), with a placeholder key and a private `models.yml`; key management and the model probe are refused in every cell.

## Sessions and lifecycle

The worker handles `agent_admin_turn` through `services/agent/worker_rpc.py`, `online_turn.py` and `omp_host.OmpSessionHost`, using `omp_rpc` to drive a separate `omp` process per bound conversation. Authenticated private `omp_control` endpoints handle stop, steer, status and answers without waiting behind the turn: owner-only Unix sockets or owner/SYSTEM Windows named pipes, never public TCP. Do not expose endpoint tokens or credentials in logs or model-visible text.

CAD conversation processes replace OMP's coding-agent instruction block with
`services/agent/prompts/omp_cad_system.md` via `--system-prompt-template` when
the selected binary advertises that flag. The launch falls back to the existing
OMP prompt, with one warning per binary identity, when the flag or packaged
template is unavailable. Tool-free helper completions do not use this template.
Release sidecars ship the template beside the executable. OMP reads the template
once at process launch; an already-running conversation is not restarted or
rewritten when it changes.

The Handlebars template renders live tool inventory, internal URLs, mounted
`xd://` device docs and calling guidance, and applicable OMP skills and rules.
OMP still supplies tool schemas and its separate project-context/footer block,
including workspace context and Chudware's appended role prompt. Do not render
`contextFiles` or `appendPrompt` in the template: that would duplicate the
footer. Compare the template with the installed OMP system template and
`omp://system-prompt-customization.md` when upgrading OMP.

The model is never told, unasked, which documents the GUI has open. The composer sends no per-turn `scope` and the route carries no top-level `doc_id`; the only editor context in a request is the `PICKED CONTEXT` block rendered from the user's `context_refs` chips (`worker_rpc._picked_context_text`). After a daemon restart the one-time `RESUMED CONVERSATION` note is a constant warning that quoted `doc_id`s may be stale; it lists no documents. The model identifies documents with `list_parts()`/`import_part(path=...)` and passes `doc_id` explicitly; an omitted `doc_id` resolves only to the agent's own handle (`cad_common.current_doc_id()`), never to the GUI's active tab.

An ordinary turn has no total execution deadline unless a caller explicitly supplies a positive finite timeout. A prompt response is not completion: asynchronous child work must settle (`session_settled` or the runtime's settled state) before the turn is considered done. Manual Stop uses the runtime's abort-and-restore-queue path, preserving unread user text and images; stop and maintenance settlement are bounded even if compaction fails to reply. Maintenance timeout, cancellation, skipped work and error are not success. Never automatically restart a healthy active session to apply a setting; use a supported live setting or stage the change at a safe boundary.

A slow settlement probe is not a dead runtime. `wait_for_settled` opens with a `get_state` request that queues behind OMP's serial commands and the event stream ahead of it, so the host keeps exactly one probe in flight and keeps honoring Stop, the budget and session closure while it finishes; only the probe's own failure (process exit, protocol error) ends the turn. Conversation clients retain a one-frame event history: they observe frames through listeners, and each retained `message_update` carries the whole streamed message twice.

Turn failures reported by the host, worker, OMP process or control protocol use
`runtime_error`; reported provider/API failures use `provider_error`. Explicit
turn-budget and cancellation outcomes retain their own reasons.

Source updates do not hot-reload existing Python workers or trusted extensions.
After affected runs settle, fully quit the app and allow the daemon's idle exit
before reopening; an immediate GUI restart can reconnect to the old worker.
Never force-stop ongoing work to apply an integration repair.

Manual compaction must acquire worker admission before it can run. If its conversation
already runs, its old worker cannot release the idle runtime, or the pool has no
capacity, hostd returns `worker_lane_busy` instead of queueing maintenance behind an
unbounded turn. An owner busy with another conversation may release the idle session
to an available worker. Refusal must not abort existing work, change the method, or run
later when capacity frees up; ordinary turns retain single-flight waiting. A compaction
spinner alone is not evidence of provider activity: for a stalled legacy session, inspect
the worker state without pausing it (for example, `py-spy dump --pid <worker-pid>
--nonblocking`, without `--locals`). A surviving turn may be waiting for an unanswered
question even after its GUI was closed. Do not terminate that session to apply this fix.

A desktop turn streams only to the connection that started it. Once that connection is
gone (GUI crash or quit), nobody can see the run's events or answer its `ask_user`, so
the conversation's next `agent_admin_turn` cancels that orphaned run over the worker
control lane before it queues, then resumes from the journal, which already holds the
orphan's finished work. A run whose requester is still connected is never superseded.

For user-requested recovery of an already-stalled legacy chat, back up its journal and
target only the stale run's persisted `run_id` with `agent_cancel`; never stop the shared
daemon or answer the hidden question on the user's behalf. A control timeout is an
unknown outcome, not proof that cancellation failed: inspect the worker's next operation
before retrying. Verify the requested compaction's committed journal entry, its exact
method, the GUI's terminal receipt, and preservation of the original journal prefix.

Each conversation remains owned by its worker; transfer requires release of the idle runtime. Existing compatible journals are opened through OMP's session operations, preserving opaque thought, signature, cache and lineage data. Unsupported history must produce a recoverable error, not silently reconstruct a session from visible chat text. For one-time legacy session-link migration, the [GUI](../../host/src/ui/omp_session_actions.cpp) reads the old persisted link when `omp` is absent and writes the `omp` link on save. The worker parameter is `omp_session_file`. Do not stop, uninstall or restart an already-running user session during migration.

Idle-runtime release, explicit close, and eviction keep a closing session
registered until its process has actually stopped and flushed the journal.
A concurrent or repeated release refuses `session_busy`; a timed-out or failed
teardown is not proof that another worker may resume the same file. Stop is
also honored while a turn or maintenance request waits for a worker or a
session transfer, including the interval before the worker registers the run.
It prevents that request from starting without cancelling unrelated work.

Cell gateways broadcast an actor's agent events to its gateway connections, never to
its sandboxed workers' connections: those are request/response clients that read only
while awaiting a reply, so a streamed turn would fill one until hostd's write timeout
dropped it ("event delivery failed ... disconnecting for re-sync") and the worker's next
call, typically the end-of-turn `doc_list`, met a broken pipe. Each GUI
turn socket accepts only its own already-issued run handle; the GUI does not
also render the duplicate delivered to its primary event socket. Worker-mapped
nested events carry the owning root turn and session IDs for routing, retaining
their subagent and span IDs for trace lineage. Native offline streams retain
their child-run activity.

In a cell the OMP process runs in a nested actor sandbox (`chudware-actor-sandbox`,
bubblewrap `--die-with-parent`) and two properties are load-bearing:

- `--die-with-parent` is `PR_SET_PDEATHSIG`, which the kernel delivers when the *thread*
  that forked the child exits, not when its process does. `omp_host._start_client` therefore
  runs `client.start()` on a thread that stays parked until `_stop_client` releases it; a
  launch thread that returns with `start()` gets the sandbox killed right after its extension
  loads, and the turn then waits out a bridge timeout (`extension operation timed out`).
- The extension bridge socket lives beneath `/actor/run` (shared by every nested sandbox of
  the actor) and is authenticated only by its per-runtime token. It carries no hostd request
  context, so `ControlServer` binds one only for worker controls; its CAD tool bindings
  restore the owner they captured. Binding an absent context in a cell is a refusal, which
  would answer every bridge call with `cell_refused` in a success envelope.

Each conversation also owns its context usage, reported window and window
provenance; switching or background telemetry never borrows another chat's
denominator. Token peaks from the native lane update that same owner.
Each conversation owns its transient status and phase too: switching restores that
conversation's view, and a background result updates its owner without repainting
the selected conversation, even when both chats use the same document.
The project-wide Active Work ticker retains concurrent conversations' operations
independently and filters them by their owning conversation's project.

Cold resume repairs missing image blobs from the desktop legacy store only when
the original bytes match their SHA-256 references. Import through OMP's session
manager and its directory resolver, never the journal directory; do not re-encode
images, replace existing blobs, or rewrite history. Missing or corrupt images
required by the restored context produce a recoverable `session_asset_error`.
Missing compacted or off-branch images are reported without blocking an
otherwise valid continuation. BYOK and cell runtimes must not consult the desktop
legacy store. Adding blobs does not reload images already missing in a live
runtime; never restart that runtime automatically.

Use OMP's public raw loader, in-memory migration and standalone context builder
for required-image selection, not a second compaction parser or an eagerly opened
session. Existing unsafe canonical blobs must fail before OMP's eager hydration,
including historical references. Selected archive frames must pass verification;
missing or budget-omitted frames follow OMP's omission policy with an explicit
notice, not a silent claim that all archived images were restored.

## Settings and compaction

A turn captures settings from its owning conversation/workspace rather than an unrelated visible session. The root chat's explicit model pin is independent of the Small, Medium and Large assignments used by role policy; preserve dormant advanced selections. A provider available for account setup is not necessarily a model usable with current credentials. Model discovery and configuration are not credential validation; keyless providers require explicit opt-in. Account secrets remain in OMP storage; exposed descriptors are redacted.

Provider discovery batches redacted authentication state through the trusted extension.
An unrelated provider failure remains unknown rather than hiding healthy providers.
Pickers retain last-confirmed availability during transient refresh failures and retry;
explicit sign-out or disabled credentials revoke it. A disk-cached catalog alone never
authorizes a model, and online selections preserve OMP's exact provider/model identity.

Compaction uses the selected method exactly: Automatic follows the runtime's effective order with the experimental `snapcompact` removed (an order of only `snapcompact` becomes `soft`), so snapcompact never runs unless selected; Off disables it; explicit Snapcompact, Shake and Summary select `snapcompact`, `shake` and `soft` respectively as singleton orders. No silent fallback or substitution is allowed. If an exact manual operation cannot be supported, report that rather than changing the selected policy. Memory selections are off, local, hindsight, mnemopi and sharpshooter; the historical `mnemo` value migrates explicitly to `mnemopi`, without backend fallback.

OMP runtime settings that Chudware does not own are never copied into `config.json`.
Settings → AI shows rows of OMP's own settings panel under Chudware's tabs (Models, Context,
Memory, Tools, Coding, Agents, Providers) with OMP's label, description, choices, order,
validation and provenance. `services/agent/omp_settings.py` is the whole policy: which OMP
(tab, group) feeds which Chudware tab, the keys never shown (Chudware's own controls, the
per-session pins, terminal-only rows, credentials), and the keys Chudware overrides. The
extension reads the rows through OMP's own panel definitions (`config_list`, `config_get`),
validates a Chudware-layer value with the setting's own `parse` / `assertWritable`
(`config_parse`) and writes OMP-scope values with `Setting.set` (`config_set`); the Python side
shapes the reply (`omp_config_get` / `omp_config_set`). Memory rows are the group of the
selected backend only.

A change has one of two scopes. `omp`: the user's own OMP config, shared with their other OMP
sessions. `chudware`: `<chudware root>/omp/cad-settings.yml` (the actor's state root in a cell),
created with Chudware's defaults, passed as `--config` to every Chudware OMP process (sessions,
helpers, the service runtime) before the per-session overlay, so the overlay's pins still win
(the last `--config` file wins; verified against OMP 18.17). OMP RPC runtimes watch their
`--config` files and apply an edit, including an atomic replace, within a second; the file is
written by replacing it. `--config` files are nested YAML: OMP silently ignores a dotted key at
the top level (`task.maxRuntimeMs: 0` pins nothing; `task:\n  maxRuntimeMs: 0` does), so every
overlay and layer key is written nested, and a test fails on a dotted top-level key. The
overlay pins `task.maxRuntimeMs: 0` (a CAD run never stops on a time limit, whatever the user's
OMP config says), the task-list preference (`todo.enabled` / `todo.eager`), the memory backend
and, in a tenancy cell, `browser.enabled` and `computer.enabled` off; each outranks the user's
own OMP value (checked in the real-runtime settings test). The Chudware layer holds
`lsp.enabled` and `lsp.diagnosticsOnWrite` (off) and
`task.maxEffort` (`high`, measured: xhigh doubled a sub-agent turn's output tokens). LSP is a
setting, not the `--no-lsp` flag, for conversation processes: OMP's `lsp.enabled` only gates the
`lsp` tool and discovery, while the `write` tool starts language servers whenever
`lsp.diagnosticsOnWrite` is on, so both are off by default. OMP refuses to start with a tool its
settings disable, so `lsp` is listed in a conversation's `--tools` only while the layer turns
LSP on, and a change applies to the next process. Tool-free helpers keep `--no-lsp`.

Manual compaction receipts are verified against a new entry in the owning OMP journal,
then include the actual method and current before/after context breakdown when available.
Tokenizer estimates are marked; an unknown after-count invalidates the meter instead of
recounting the visible transcript, which still contains compacted history. Session identity
and the original journal prefix remain intact.
A confirmed commit takes precedence over a late Stop. Unsettled maintenance timeouts
retire only their owning runtime and prevent its reuse until teardown completes.
The transcript presents manual compaction as one maintenance status, not an
assistant reply or run receipt; its identity survives reload. A Stop stays
pending until the owning receipt settles. Background results and retry drafts
belong to their originating conversation. If the app closes during maintenance,
the restored context usage is unknown until the runtime reports it again.
Explicit offline compaction rewrites only the unchanged originating transcript:
`snapcompact` and `shake` are available locally, while an unavailable selected
summary/automatic method is refused rather than substituted with snapcompact.
An off/no-replacement response is reported as skipped.

## CAD tools and events

`omp_tools.py` projects the live `services/mcp` registry and enforces existing authorization, guardrails and result envelopes. `omp_extension.ts` is a trusted per-session extension; each root or child factory binds its own agent identity and reaches the same Python registry through `omp_bridge.py` and its private authenticated control transport. Tools are discoverable, including the CAD toolbox/discovery and enablement helpers; callbacks carry trusted agent context, not identity supplied in tool arguments. Preserve structured results, images and spill references, and consult the actual registry schemas/result contracts instead of inventing tools or fields. `omp_events.py` maps runtime frames to the feed, including real settlement and errors. `omp_questions.py` handles user-question batches.

The Copilot transcript mirrors OMP's own activity views from frames the runtime already
streams, never from a Chudware board or prompt injection: IRC traffic between task
sub-agents (`irc_message`, including nested `Parent.Child` pairs relayed to the main
session) as OMP-style IRC cards with their send time; one usage line per billed root
request from `turn_end` (time, elapsed since the prompt, tokens in/out, cache reads, time
to first token, output rate); the builtin `wait` as one live "Waiting on N jobs" row from
its `details.jobs`; and the root `todo` result's phases as the plan's todo list with
`closed/total` per phase. These projections only observe: they send nothing to the
runtime, so they cannot change the provider prefix or cost a cache hit. Sub-agent and
`delegate` session frames keep their tool rows only. See
[agent events](../../contracts/agent_events.md#omp-activity-surface-append-only-2026-11-05).

OMP's `todo` is the only task-list tool online; Chudware's `task_list` is not advertised
to this lane (`omp_tools.OMP_SUPERSEDED_TOOLS`). Settings → "Task list preference" becomes
the conversation process's todo policy in its `--config` overlay: Off sets
`todo.enabled: false` and drops the tool; the other three set `todo.enabled: true` (the
preference outranks a user's own `todo.enabled: false`, which would stop OMP from starting
with `todo` in `--tools`), and Encourage adds `todo.eager: preferred`, Require
`todo.eager: always` (OMP forces a `todo` call on a chat's first message and re-nudges
after compaction). Like the memory backend, a change applies when the chat's process
next starts.

Registration and activation are separate. The full authorized CAD catalog remains registered, but only the core/meta bootstrap and explicitly enabled categories are initially active. Inactive tools must appear in neither provider tool schemas nor the system prompt's `xd://` catalog. Merely marking a tool discoverable is insufficient: OMP lists mounted tools in that catalog. `enable_tools` republishes the active subset and waits for the calling root or child factory to apply the new generation before returning; tool availability must not depend on a later frame or another agent's refresh.

The bridge retains each CAD tool's original runtime presentation across root
and child factories. Tools initially inactive, or registered after startup,
stay discoverable `xd://` devices when enabled even if the host descriptor
changes their load mode to essential. Initially active bootstrap tools retain
their configured placement. Each factory reconciles activation and mounts on
refresh, a no-op `enable_tools` call, and before the next model request; a
matching bridge generation alone does not prove the mounted set survived.
The successful `enable_tools` result tells the model to read `xd://<tool_name>`
for the schema and write JSON arguments to that URI; skill-triggered loads give
the same guidance. Activation does not pre-mount the unused catalog or add new
top-level provider schemas. OMP's Eval description otherwise enumerates the
live bridge tools, so the supported `before_provider_request` hook replaces
only that volatile roster with general access guidance in its provider
description. It leaves the system prompt, message history, signatures and
other tools untouched. For a prefix-bound Anthropic session this keeps the
provider-visible prompt and tool array stable while the device is immediately
callable. Authorization, definition changes and revocation still take
precedence over cache reuse; legacy transcripts or non-prefix-bound models do
not promise this prefix.

OMP 18.14.1 cold-revives parked subagents with enabled tool names but without
their original mounted partition, pinning those names top-level for the new
session. The public presentation API cannot undo those pins. Such tools remain
callable through `xd://`, but the device roster and provider tool prefix may
differ; exact mounted-partition and prefix preservation on that path require an
upstream runtime fix.

Explicitly loaded concrete tool names are flushed to private OMP custom entries
during the turn and restored before the next model request. Recheck the current
registry, role, policy and mode; remembered loads are not authorization. Bind them
to the conversation, canonical workspace and credential owner. An official fork
may adopt inherited selections only within the same workspace/credential authority,
then records its own conversation and runtime identity. Another conversation
opening the same journal cannot claim those selections.

GUI-owned legacy journals migrate only successful, structured `enable_tools`
result metadata on the active branch, after matching the GUI link, journal,
runtime ID and workspace. Restore the concrete names actually added, not current
members of historical categories. Failed calls, prose, summaries and unowned
history are not activation evidence.

Activation-only updates do not re-register tool definitions or force a prompt
rebuild. Schema fingerprints ignore object-key ordering; changed definitions use
OMP's registration refresh. Force a presentation rebuild only when the generated
base inventory/catalog still advertises a revoked tool. Seed the authorized
bootstrap before a cold session switch so OMP does not freeze an empty CAD catalog.
Unchanged prefix-bound sessions must preserve prompt bytes, opaque signatures and
cache lineage; changed policies, schemas or historical runtime baselines cannot
promise compatibility. Never strip thought history to manufacture a cache hit.

Online prefix invariant. For one conversation the provider request prefix — the
appended system prompt, the host-tool array (set, order, presentation, descriptions,
schemas) and earlier messages — depends only on session-stable inputs: the role card,
Settings (system-prompt override, tool options, bug-report floor), the capability tier,
the registry and skills catalog, and tools enabled through `enable_tools`. It never
depends on the current request, the Copilot mode or context fullness:
`mission_dispatch.role_prompt` takes no preferences, `omp_tools._effective_names`
applies no mode policy, and descriptions are never trimmed by context fullness. Policy
that used to hide tools (Ask/Plan/Edit read-only, delegation depth 0, advisor off) is
enforced when the call is dispatched by `omp_tools._turn_policy_refusal` against the
turn's own binding context, returning a `mode_refused` or `turn_policy_refused`
result. The model learns the turn's policy from the `TURN PREFERENCES` block
(`TurnPreferences.turn_notice`) that `worker_rpc` appends to the end of the new
user message, after picked context and before the budget notice. Every turn restates
it, so a switch back to Agent after an Ask turn is announced explicitly. The bridge
pins the first description published for each tool for the life of the runtime:
Settings tool notes/timeouts and skill-pack pointers edited mid-conversation apply
from the next launch, while enforcement (the real tool timeout, dispatch policy) stays
live. A relaunched or resumed conversation recomposes the same bytes from the same
inputs; an explicit Settings or skills edit made in between is the one expected change.
Helper completions (`complete_text`) use a provider session key derived from the
conversation and the helper's own system prompt, never the conversation's key.

The explicit offline loop (`agent_turn.run_turn`) re-sends the whole conversation
every turn to a local server that reuses its KV cache only for an identical prefix,
so it keeps the same invariant. The system prompt contains only request-independent
text (role card, harness protocol, role and skills catalogs, project files, tool
schemas): the skills catalog is sent every turn rather than on a session's first,
and the per-turn Copilot mode, a delegation request, project-memory recall and
request-triggered skill bodies are appended to the tail of the current user message.
`enable_tools` mid-turn appends the newly enabled tools' schemas as one message
instead of rewriting the system prompt. Prior history is kept verbatim (entries are
clipped to a fixed length) and, once over the entry/character caps, the oldest
entries drop in aligned chunks so the kept prefix changes once per chunk, not per
turn. Context-overflow recovery (`_drop_oldest_tool_results`) is the one deliberate
exception: it runs once per turn, only after the provider reported an overflow, and
trims in a single batch.

`list_tool_categories(query=...)` matches text phrases and individual exact tool names
separated by whitespace or commas, case-insensitively. Bulk-name lookups retain category
filtering, deterministic menu order, deduplication and pagination. Discovery does not
activate tools or bypass authorization; use `enable_tools` for the selected schemas.

`cad_guide` accepts the 2D sketch operation topics `sketch_fully_define`,
`sketch_add_constraint` and `sketch_set_dim`, plus `check_sketch` and `sketch_health`.
They resolve to the existing `sketch` cheatsheet and its runnable examples, not a second
copy of sketch-authoring instructions.

Eval's omitted timeout is revised to 120 seconds by the official `tool_call` input hook
in each root and child extension factory. OMP's 30-second default otherwise expires
before CAD's 45-second solve-to-job handoff. Explicit deadlines, including zero, Stop,
and the runtime's configured `tools.maxTimeout` ceiling retain their meaning. This
does not extend a session's lifetime or restart an existing runtime. Prefer
`add_features(background=True)` for costly edits, retain its job ID, and call `wait_job`
directly rather than inside Eval. An Eval cancellation is not evidence that the CAD
mutation stopped; inspect its job/current revision before retrying. A reset Python
kernel loses helpers and variables, not native documents.

CAD persistence tracks mutations by turn and workspace at the core bridge.
The terminal save sweep persists touched documents without using creator identity
as a proxy for ownership, skips outstanding human edits as `skipped_human_edits`,
and reports save failures. In daemon mode hostd serializes the save decision
with document mutations; the in-process fallback cannot atomically guard an agent
save against a concurrent human edit. See the
[document persistence contract](../../contracts/worker_rpc.md#hostd-document-persistence-amendment--contract-revision-21).

Operation timeouts and ordinary settlement-wait expirations do not rotate the OMP
conversation, reprompt it, or replace its journal. The Eval deadline hook changes
execution input only, not the provider schema, mounted tools or system prompt.
Keep long CAD work in background jobs rather than extending every timeout.
Provider cache retention is separate: even an unchanged conversation can miss after
idle expiry or eviction. The default 120-second job wait is below a common five-minute
cache lifetime, but accumulated idle time and provider policy still matter; an explicit
900-second wait can exceed that lifetime. Never issue synthetic model keepalives or
reconstruct thought history to claim a guaranteed cache hit.

Eval's `details.xdev.inner` is already decoded: JSON objects/arrays remain structured,
while `cad_guide` and `cad_skill` remain plain strings. The shared `edit-part` helper
loads structured `_truncated.full_result` JSON before checking `ok` or consuming fields;
a spill is not a failed operation. Missing recovery files require a narrower read, never
replaying an already-applied mutation. Plain-text spills retain their explicit recovery
instructions. Diagnostic validation failures remain failures; call the diagnostic directly
when its failed report is the data being inspected, rather than assuming every `ok:false`
is an execution error. Topic hyphens normalize to underscores; `feature_history` provides
focused insertion/reorder/rollback guidance. Failed Python Eval feed previews put the
exception before bounded stack and earlier-output context; success output and the runtime
journal are unchanged.

Nested CAD calls made through Eval emit individual feed rows with their trusted
child identity and, when unambiguous, the owning Eval call id. Overlapping Evals
for the same agent leave the parent unknown rather than guessing; direct
`write xd://` calls are not duplicated. A nested structured `ok:false` produces a
failed row but remains ordinary diagnostic data in the returned tool result.

The GUI keeps `parent_id` and `subagent_id` on each tool row (persisted with the session) and nests
rows in the transcript instead of listing them flat: a call whose `parent_id` names an Eval's row
sits under that Eval, any other call with a `subagent_id` under that sub-agent's card (stage
`subrun:<subagent_id>`), and a sub-agent's own Eval under its card with its bridged calls under it.
Rows with neither (including `parent_unknown`) stay top level. The owner shows "N calls · M failed ·
last <tool>" and, opened, its 50 most recent calls under an "N earlier calls" line; failures inside
are counted on the owner and in the turn receipt, whose jump opens the owners above them. The
Eval's start (`status` phase `tool`) creates its running row, which its final `agent.tool_call`
updates in place, so bridged calls nest under it while it runs.
Copy/export still lists every row. `CHUDWARE_SHOT=toolnest` renders the nesting headlessly.

A task sub-agent's `sub_run` card also carries its own run totals, which Main's context meter
never shows. `omp_events._subagent_frame` maps OMP's `AgentProgress` (`subagent_progress`) onto
the row as `requests`, `tokens` (input + output + cache writes), `context_tokens`,
`context_window`, `cost_usd`, `duration_ms`, `resolved_model` and `retry_state`
(`attempt`/`max_attempts`/`delay_ms`/`error`), each only when the frame has it as a finite
non-negative number (a string for the model); lifecycle frames carry none. `feed.cpp` keeps the
latest value of each in a `\x1eUSE\x1e` trailer on the card's detail (`copilot/subrun_usage.h`),
so it persists with the session like the tool-row trailers and stays out of copied text.
`retry_state` is a snapshot: a progress frame without it clears the pending retry. The card's
second line reads "160 requests · ctx 346k / 1M · $6.58 · sonnet-5-5" (cost only above zero;
the tooltip adds billed volume, run time and the full model id), and a running card asleep
between provider retries shows a "waiting on provider retry" chip. `test_omp_events.py` pins the
mapping; `CHUDWARE_SHOT=toolnest` shows the line and the chip. The AI workspace roster reads
the same totals per sub-agent from the execution trace (`agent_dag.cpp`). Contract:
[agent events](../../contracts/agent_events.md#task-sub-agent-run-usage-append-only-2026-11-06).

The private CAD bridge preserves tool-authored `ValueError` and native `CoreError`
diagnostics as failed tool calls, bounded to 2,000 characters through the existing secret
scrubber and with the bridge's literal token/endpoint removed. The extension includes the
tool name and recovery reason instead of replacing them with only “CAD tool failed”.
Unexpected exceptions remain generic; provider/control errors are not made public.
`test_omp_bridge.py` covers the privacy/size boundary; the provider-free official-runtime
fixture in `test_omp_extension.py` exercises root and child error presentation.

CAD tool bodies run in the same worker process as the bridge's `ControlServer`, so a native
call that holds the GIL (a numpy sort, one JSON decode of a large engine reply) leaves the
server unable to accept anything, including the extension's poll for that very call, and
it also starves every other agent's calls. Keep such work off the GIL (array operations
that release it, no per-vertex Python objects, no `np.unique(axis=...)` on mesh tables);
`render_part`'s `shot_planner._edge_faces` held it for 7-10 s on a 2.6M-vertex assembly and
surfaced as `bridge timeout` on the render and on an unrelated render queued behind it. The
extension gives a poll for a running call a 60 s wait (other bridge requests keep 5 s), never
times one out and re-sends it (the server still executes an abandoned poll, which drains
updates and drops a finished call), stops waiting on Stop, and sends `tool_cancel` when a
poll fails so an abandoned call cannot keep running unowned. A dead worker still fails at
once (`bridge unavailable`). `test_omp_bridge_gil.py` runs the real extension under Bun
against a bridge whose tool pins the GIL; `test_shot_planner_edge_faces.py` pins the
array implementation against the previous per-vertex one.

The fork-only speech-to-text, repair and duplicate compaction endpoints are removed; no compatibility stubs or substitute RPC commands are promised. The official client and extension APIs are the boundary. See [worker RPC](../../contracts/worker_rpc.md) and [agent events](../../contracts/agent_events.md) for versioned contracts and [engineering notes](engineering-notes.md) for worker and persistence invariants.
