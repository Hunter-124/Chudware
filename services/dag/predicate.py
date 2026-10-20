"""Bounded, data-only expression interpreter for DAG verification predicates."""
from __future__ import annotations

import ast
import math
import operator
from typing import Any

_FUNCTIONS = {"abs": abs, "int": int, "float": float, "bool": bool,
              "len": len, "all": all, "any": any, "min": min, "max": max}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
            ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
            ast.Is: operator.is_, ast.IsNot: operator.is_not}
_MAX_ITEMS = 4096


def _check_data(value: Any, budget: list[int], depth: int = 0) -> Any:
    budget[0] -= 1
    if budget[0] < 0 or depth > 32:
        raise ValueError("predicate data limit exceeded")
    kind = type(value)
    if kind in (type(None), bool):
        return value
    if kind is int:
        if value.bit_length() > 1024:
            raise ValueError("predicate integer limit exceeded")
    elif kind is float:
        if not math.isfinite(value):
            raise ValueError("predicate requires finite numbers")
    elif kind is str:
        if len(value) > _MAX_ITEMS:
            raise ValueError("predicate string limit exceeded")
    elif kind in (dict, list, tuple):
        if len(value) > _MAX_ITEMS:
            raise ValueError("predicate collection limit exceeded")
        if kind is dict:
            for key, item in value.items():
                if type(key) not in (str, int, float, bool, type(None)):
                    raise ValueError("predicate dictionary key is not data")
                _check_data(key, budget, depth + 1)
                _check_data(item, budget, depth + 1)
        else:
            for item in value:
                _check_data(item, budget, depth + 1)
    else:
        raise ValueError("predicate accepts only plain data")
    return value


def evaluate(expression: str, namespace: dict[str, Any]) -> bool:
    if not isinstance(expression, str) or len(expression) > 8192:
        raise ValueError("predicate expression limit exceeded")
    tree = ast.parse(expression, mode="eval")
    nodes = list(ast.walk(tree))
    if len(nodes) > 512:
        raise ValueError("predicate syntax limit exceeded")
    allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.List, ast.Tuple,
               ast.Dict, ast.Subscript, ast.Slice, ast.BoolOp, ast.And, ast.Or,
               ast.UnaryOp, ast.Not, ast.UAdd, ast.USub, ast.BinOp, ast.Compare,
               ast.Call, ast.Attribute, *_BINARY, *_COMPARE)
    for node in nodes:
        if not isinstance(node, allowed):
            raise ValueError(f"predicate syntax refused: {type(node).__name__}")
        if isinstance(node, ast.Attribute):
            # Compatibility syntax only: interpreted dict lookup, never getattr.
            if node.attr != "get" or not any(isinstance(parent, ast.Call) and parent.func is node for parent in nodes):
                raise ValueError("predicate attributes are refused")
        if isinstance(node, ast.Call):
            if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
                raise ValueError("predicate expanded/keyword calls are refused")
            if isinstance(node.func, ast.Name):
                if node.func.id not in _FUNCTIONS:
                    raise ValueError("predicate function is refused")
            elif not (isinstance(node.func, ast.Attribute) and node.func.attr == "get"):
                raise ValueError("predicate call is refused")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError("predicate private names are refused")
    _check_data(namespace, [_MAX_ITEMS])

    def visit(node: ast.AST, depth: int = 0) -> Any:
        if depth > 64:
            raise ValueError("predicate depth limit exceeded")
        def sub(child: ast.AST) -> Any:
            return visit(child, depth + 1)
        if isinstance(node, ast.Constant):
            value = node.value
        elif isinstance(node, ast.Name):
            value = namespace[node.id]
        elif isinstance(node, (ast.List, ast.Tuple)):
            items = [sub(item) for item in node.elts]
            value = tuple(items) if isinstance(node, ast.Tuple) else items
        elif isinstance(node, ast.Dict):
            value = {sub(key): sub(item) for key, item in zip(node.keys, node.values)}
        elif isinstance(node, ast.Subscript):
            base = sub(node.value)
            if type(base) not in (dict, list, tuple, str):
                raise ValueError("predicate lookup requires a dictionary or sequence")
            value = base[sub(node.slice)]
        elif isinstance(node, ast.Slice):
            return slice(*(sub(item) if item is not None else None for item in (node.lower, node.upper, node.step)))
        elif isinstance(node, ast.BoolOp):
            value = sub(node.values[0])
            for child in node.values[1:]:
                if (isinstance(node.op, ast.And) and not value) or (isinstance(node.op, ast.Or) and value):
                    break
                value = sub(child)
        elif isinstance(node, ast.UnaryOp):
            value = sub(node.operand)
            if isinstance(node.op, ast.Not):
                value = not value
            elif type(value) not in (int, float):
                raise ValueError("predicate arithmetic requires numbers")
            else:
                value = value if isinstance(node.op, ast.UAdd) else -value
        elif isinstance(node, ast.BinOp):
            left, right = sub(node.left), sub(node.right)
            if type(left) not in (int, float) or type(right) not in (int, float):
                raise ValueError("predicate arithmetic requires numbers")
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("predicate exponent limit exceeded")
            if (isinstance(node.op, ast.Pow) and type(left) is int and type(right) is int
                    and right > 0 and left.bit_length() * right > 1024):
                raise ValueError("predicate integer limit exceeded")
            value = _BINARY[type(node.op)](left, right)
        elif isinstance(node, ast.Compare):
            left = sub(node.left)
            value = True
            for op, child in zip(node.ops, node.comparators):
                right = sub(child)
                if not _COMPARE[type(op)](left, right):
                    value = False
                    break
                left = right
        elif isinstance(node, ast.Call):
            args = [sub(arg) for arg in node.args]
            if isinstance(node.func, ast.Attribute):
                base = sub(node.func.value)
                if type(base) is not dict or not 1 <= len(args) <= 2:
                    raise ValueError("predicate get requires a dictionary and one or two arguments")
                value = base.get(*args)
            else:
                value = _FUNCTIONS[node.func.id](*args)
        else:
            raise ValueError("predicate syntax cannot be evaluated")
        return _check_data(value, [_MAX_ITEMS])

    return bool(visit(tree.body))
