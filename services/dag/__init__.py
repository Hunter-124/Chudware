"""services/dag — DAG orchestrator for Chudware (aicad.dag/1).

Contract: contracts/dag_schema.md
Scaffold landed by T-A1. Full executor wiring in T-A2; interrupt() in T-A3; verify in T-A4.

Usage:
    from services.dag import executor
    result = executor.run(graph_json, initial_state={})
"""
