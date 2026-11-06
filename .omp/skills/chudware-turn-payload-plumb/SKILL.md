---
name: chudware-turn-payload-plumb
description: Trace composer attachments and other turn metadata through the actual runtime to model messages.
---

# Turn payload plumbing

A field emitted by the composer can still be dropped downstream. Trace a representative value through the entire selected runtime, including slash-command handling, and verify the resulting model input rather than relying on a payload comment or successful RPC.

- Trace the online composer → `agent_admin_turn` → OMP host/session path in `services/agent/worker_rpc.py` and `services/agent/omp_host.py`. Preserve runtime session journals and thought traces rather than reconstructing conversation history.
- Follow each field through validation, forwarding and message construction. Check both ordinary and slash-command requests: message rebuilding after slash resolution has previously dropped attachments. An absent field should retain existing behavior.
- For images, validate inputs at the responsible boundary and assert that actual image bytes reach the provider adapter; a successful envelope alone does not prove delivery. Exercise relevant malformed inputs without exposing private files.

Verify: `scripts/testbox.py --suite agent`
