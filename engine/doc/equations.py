"""Safe expression evaluator for document equations (docs/contracts/doc_schema.md §4).

Grammar: numbers, names, + - * / ^ (and **), unary -, parentheses, and a whitelisted
function set. Trig works in DEGREES in and out (CAD-user convention): sin(30) == 0.5,
asin(0.5) == 30. Nothing outside the AST whitelist ever executes.
"""

from __future__ import annotations

import ast
import math
from typing import Mapping


class EquationError(ValueError):
    pass


def _deg_in(fn):
    return lambda x: fn(math.radians(x))


def _deg_out(fn):
    return lambda x: math.degrees(fn(x))


_FUNCS = {
    "sin": _deg_in(math.sin),
    "cos": _deg_in(math.cos),
    "tan": _deg_in(math.tan),
    "asin": _deg_out(math.asin),
    "acos": _deg_out(math.acos),
    "atan": _deg_out(math.atan),
    "sqrt": math.sqrt,
    "abs": abs,
    "min": min,
    "max": max,
    "floor": math.floor,
    "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi}

_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b,
}


def _normalize(expr: str) -> str:
    return expr.replace("^", "**")


def _parse(expr: str) -> ast.Expression:
    if not isinstance(expr, str) or not expr.strip():
        raise EquationError("empty expression")
    try:
        return ast.parse(_normalize(expr), mode="eval")
    except SyntaxError as exc:
        raise EquationError(f"syntax error in '{expr}': {exc.msg}") from exc


def _check(node: ast.AST, expr: str) -> None:
    if isinstance(node, ast.Expression):
        _check(node.body, expr)
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _BINOPS:
            raise EquationError(f"operator not allowed in '{expr}'")
        _check(node.left, expr)
        _check(node.right, expr)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.USub, ast.UAdd)):
            raise EquationError(f"operator not allowed in '{expr}'")
        _check(node.operand, expr)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise EquationError(f"only numbers allowed in '{expr}'")
    elif isinstance(node, ast.Name):
        pass  # resolved at evaluation
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise EquationError(f"unknown function in '{expr}'")
        if node.keywords:
            raise EquationError(f"keyword arguments not allowed in '{expr}'")
        for a in node.args:
            _check(a, expr)
    else:
        raise EquationError(f"construct not allowed in '{expr}'")


def validate_expr(expr: str) -> str | None:
    """Return an error message, or None if the expression is grammatical."""
    try:
        _check(_parse(expr), expr)
    except EquationError as exc:
        return str(exc)
    return None


def expr_names(expr: str) -> set[str]:
    """Names referenced by the expression (after grammar validation)."""
    tree = _parse(expr)
    _check(tree, expr)
    return {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id not in _CONSTS
    }


def _eval(node: ast.AST, env: Mapping[str, float], expr: str) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env, expr)
    if isinstance(node, ast.BinOp):
        a = _eval(node.left, env, expr)
        b = _eval(node.right, env, expr)
        try:
            return _BINOPS[type(node.op)](a, b)
        except ZeroDivisionError as exc:
            raise EquationError(f"division by zero in '{expr}'") from exc
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand, env, expr)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        if node.id in env:
            return float(env[node.id])
        raise EquationError(f"unknown name '{node.id}' in '{expr}'")
    if isinstance(node, ast.Call):
        args = [_eval(a, env, expr) for a in node.args]
        try:
            return float(_FUNCS[node.func.id](*args))  # type: ignore[attr-defined]
        except (ValueError, TypeError) as exc:
            raise EquationError(f"math error in '{expr}': {exc}") from exc
    raise EquationError(f"construct not allowed in '{expr}'")


def evaluate(expr: str, env: Mapping[str, float] | None = None) -> float:
    """Evaluate one expression against an environment of resolved names."""
    tree = _parse(expr)
    _check(tree, expr)
    result = _eval(tree, env or {}, expr)
    if not math.isfinite(result):
        raise EquationError(f"non-finite result from '{expr}'")
    return result


def evaluate_globals(globals_list: list[dict]) -> dict[str, float]:
    """Resolve [{name, expr}, ...] in dependency order. Raises on cycles/unknowns."""
    pending = {g["name"]: g["expr"] for g in globals_list}
    if len(pending) != len(globals_list):
        raise EquationError("duplicate equation names")
    env: dict[str, float] = {}
    for _ in range(len(pending) + 1):
        if not pending:
            return env
        progressed = False
        for name in list(pending):
            try:
                if expr_names(pending[name]) - env.keys():
                    continue
            except EquationError:
                raise
            env[name] = evaluate(pending.pop(name), env)
            progressed = True
        if not progressed:
            unresolved = ", ".join(sorted(pending))
            raise EquationError(
                f"unresolvable equations (cycle or unknown name): {unresolved}"
            )
    return env
