# Monitor: grok45-1

- cycle_dir: `/home/hunter/Repositories/CAD/tools/flywheel/bench/gym/reports/runs/grok45-1`
- generated_at: 2026-07-14T16:46:10Z
- cells_scanned: 3
- events_scanned: 144
- findings: 17
- by_kind: {'cycle_failure_class': 2, 'missing_tool': 10, 'provider_error': 1, 'guardrail_block': 1, 'repeated_tool_error': 1, 'retry_storm': 2}

## Findings

### cycle_failure_class (2)

- **info** C1/xai:grok-4-5/s3: grind: 4 attempts on '?' with no changed hypothesis
  - cell: `C1_xai:grok-4-5_s3`
  - evidence_seqs: [16, 26, 51, 62]
- **info** C1/xai:grok-4-5/s3: grind: 4 attempts on '?' with no changed hypothesis
  - cell: `C1_xai:grok-4-5_s3`
  - evidence_seqs: [54, 56, 67, 74]

### guardrail_block (1)

- **error** hard_guardrail blocked name='verify_battery' reason='missing verify_battery PASS/WAIVED'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [70]

### missing_tool (10)

- **warn** model requested tool not offered / unresolved: 'dag_status'
  - cell: `C1_open_xai:grok-4-5_s1`
  - evidence_seqs: [24]
- **warn** model requested tool not offered / unresolved: 'channel_send'
  - cell: `C1_open_xai:grok-4-5_s1`
  - evidence_seqs: [33]
- **warn** model requested tool not offered / unresolved: 'request_resolve'
  - cell: `C1_open_xai:grok-4-5_s1`
  - evidence_seqs: [33]
- **warn** model requested tool not offered / unresolved: 'council_convene'
  - cell: `C1_open_xai:grok-4-5_s1`
  - evidence_seqs: [33]
- **warn** model requested tool not offered / unresolved: 'dag_status'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [25, 41]
  - count: 2
- **warn** model requested tool not offered / unresolved: 'channel_send'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [30]
- **warn** model requested tool not offered / unresolved: 'request_resolve'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [30]
- **warn** model requested tool not offered / unresolved: 'council_convene'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [30]
- **warn** model requested tool not offered / unresolved: 'dag_run'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [41]
- **warn** model requested tool not offered / unresolved: 'dag_replan'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [41]

### provider_error (1)

- **warn** provider_error: POST https://api.x.ai/v1/chat/completions returned HTTP 429: {"code":"Some resource has been exhausted","error":"The service is temporarily at capacity. Please 
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [29]

### repeated_tool_error (1)

- **error** tool='describe_part' error='No current part. Call new_part(...) first (or pass doc_id=).'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [54, 56, 67, 74]
  - count: 4

### retry_storm (2)

- **warn** identical call repeated: tool='ledger_get' fingerprint='ledger_get|{}'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [16, 26, 51, 62]
  - count: 4
- **warn** identical call repeated: tool='describe_part' fingerprint='describe_part|{}'
  - cell: `C1_open_xai:grok-4-5_s3`
  - evidence_seqs: [54, 56, 67, 74]
  - count: 4

