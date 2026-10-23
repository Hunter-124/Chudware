"""services/agent/buildgraph — Build-order DAG planner for aicad.buildgraph/1.

Contract: contracts/advisor.md §13 (FROZEN), §3 (advisory items).

Public surface
--------------
  from services.agent.buildgraph import planner, schema, executor
  from services.agent.buildgraph.planner import build_graph
  from services.agent.buildgraph.schema import validate_graph
  from services.agent.buildgraph.executor import BuildGraphExecutor
"""
