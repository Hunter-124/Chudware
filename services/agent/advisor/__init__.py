# services/agent/advisor — Pillar 20 foreman + preflight helpers.
#
# preflight: dry-run plan validation on shadow doc clones.
# advisory engine: stages, engine, render, adapters (the "foreman").
# overseer: plan-audit engine + change-request routing (the judgment layer).
#
# Public surface:
#   from services.agent.advisor import stages, engine, render, adapters
#   from services.agent.advisor.engine import build_advisory, Observation, Finding
#   from services.agent.advisor.render import render_advisory
#   from services.agent.advisor.overseer import Overseer, AuditScope, validate_audit
#   from services.agent.advisor.change_request import route_change_request
#   from services.agent.advisor.overseer_tools import audit_now, change_request
