# services/agent/advisor — Pillar 20 foreman + preflight helpers.
#
# T-W12: preflight (dry-run plan validation on shadow doc clones).
# T-W5:  advisory engine — stages, engine, render, adapters (the "foreman").
#
# Public surface:
#   from services.agent.advisor import stages, engine, render, adapters
#   from services.agent.advisor.engine import build_advisory, Observation, Finding
#   from services.agent.advisor.render import render_advisory
