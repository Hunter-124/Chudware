"""The `script` escape-hatch feature compiler (feature_params.md §Escape hatches).

``params.code`` is a legacy build123d snippet executed through the EXISTING forgiving kernel
machinery — the same namespace (forgiving primitive/edge-op wrappers, aliases, error hints) and
the same result/parts resolution the agent loop has always used. We do NOT duplicate that logic:
we import and call ``engine.kernel`` directly so a script node behaves identically to the legacy
``run_code`` path. Its resulting solid(s) become this feature's bodies.

Multi-body is honored: a script that defines ``parts = {...}`` / ``assembly = ...`` (the legacy
multi-part contract) contributes one body per member; a single-``result`` script contributes one
body. Script bodies are STANDALONE — they do not fuse into prior bodies (the legacy script owns
its whole geometry); a later feature can boolean them explicitly.
"""

from __future__ import annotations

from engine.kernel import _exec_model, _find_result, _resolve_members

from .base import CompileContext, CompileOutput, FeatureError, register


@register("script")
def compile_script(feat: dict, ctx: CompileContext) -> CompileOutput:
    code = feat.get("params", {}).get("code")
    if not isinstance(code, str) or not code.strip():
        raise FeatureError("script feature has empty params.code")

    ns, _stdout, err = _exec_model(code)
    if err is not None:
        raise FeatureError("script raised", hint=err.strip().splitlines()[-1] if err.strip() else None)

    members, _explicit_asm, is_multi = _resolve_members(ns)
    new_bodies: list = []
    if is_multi:
        new_bodies = [shp for _name, shp in members if shp is not None]
    else:
        shape = _find_result(ns)
        if shape is not None:
            new_bodies = [shape]

    if not new_bodies:
        raise FeatureError(
            "script produced no geometry",
            hint="assign the final shape to `result`, or define `parts`/`assembly`")

    bodies = list(ctx.bodies) + new_bodies
    # No single tool_body for a script: it may contribute several disjoint bodies.
    return CompileOutput(bodies=bodies, tool_body=None)
