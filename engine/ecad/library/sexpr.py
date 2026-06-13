"""S-expression reader/writer for KiCad file formats.

Clean-room implementation: implements the well-known S-expression grammar used by
EDA tools. No KiCad source consulted or copied.

Grammar handled:
  sexpr  := atom | '(' sexpr* ')'
  atom   := quoted_string | number | bare_symbol
  quoted_string := '"' (char | '\\\"' | '\\\\' | '\\n')* '"'
  number := int or float (bare, without quotes)
  bare_symbol := non-whitespace, non-paren, non-quote chars

Public API:
  parse(text: str) -> list | atom    -- parse full text; returns nested lists/atoms
  dumps(tree) -> str                  -- serialize back to S-expression text
"""

from __future__ import annotations

import re
from typing import Union

_Atom = Union[str, int, float]
_Tree = Union[_Atom, list]


# ─── tokenizer ────────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(
    r"""
    \s*                         # skip leading whitespace
    (                           # capture one of:
        "(?:[^"\\]|\\.)*"       #   quoted string (handles escapes inside)
      | [()]                    #   open / close paren
      | [^\s()"]+               #   bare symbol / number
    )
    """,
    re.VERBOSE,
)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _parse_atom(tok: str) -> _Atom:
    """Convert a raw token to its Python value (no parens; those are handled above)."""
    if tok.startswith('"'):
        # decode escape sequences; slice off surrounding quotes
        inner = tok[1:-1]
        inner = inner.replace('\\"', '"').replace("\\\\", "\\").replace("\\n", "\n")
        return inner
    # try int
    try:
        return int(tok)
    except ValueError:
        pass
    # try float
    try:
        return float(tok)
    except ValueError:
        pass
    # bare symbol
    return tok


def _build(tokens: list[str], pos: int) -> tuple[_Tree, int]:
    """Recursive descent; returns (tree, next_pos)."""
    tok = tokens[pos]
    if tok == "(":
        pos += 1
        children: list[_Tree] = []
        while pos < len(tokens) and tokens[pos] != ")":
            child, pos = _build(tokens, pos)
            children.append(child)
        if pos >= len(tokens):
            raise ValueError("Unexpected end of input: unmatched '('")
        pos += 1  # consume ')'
        return children, pos
    if tok == ")":
        raise ValueError(f"Unexpected ')' at token index {pos}")
    return _parse_atom(tok), pos + 1


def parse(text: str) -> _Tree:
    """Parse an S-expression string into a nested list/atom tree.

    The outermost form is usually a list ``(kicad_symbol_lib ...)``.
    Raises ``ValueError`` on malformed input.
    """
    tokens = _tokenize(text)
    if not tokens:
        raise ValueError("Empty input")
    tree, pos = _build(tokens, 0)
    # skip trailing whitespace/comments already stripped by tokenizer
    if pos < len(tokens):
        raise ValueError(f"Trailing content at token {pos}: {tokens[pos]!r}")
    return tree


# ─── writer ───────────────────────────────────────────────────────────────────

def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _need_quote(s: str) -> bool:
    """Return True if the string must be quoted.

    Quote if: contains whitespace, parens, or quotes; OR if the bare token
    would be re-parsed as a number (so "1" round-trips as str, not int).
    """
    if re.search(r'[\s()"]', s):
        return True
    # Would bare emission re-parse as a number?
    try:
        int(s)
        return True
    except ValueError:
        pass
    try:
        float(s)
        return True
    except ValueError:
        pass
    return False


def dumps(tree: _Tree, _indent: int = 0) -> str:
    """Serialize a nested list/atom tree back to S-expression text.

    Lists are either single-line (short) or multi-line with indentation.
    """
    if isinstance(tree, list):
        if not tree:
            return "()"
        # estimate one-liner length
        parts = [dumps(c) for c in tree]
        one_liner = "(" + " ".join(parts) + ")"
        if len(one_liner) <= 80 and "\n" not in one_liner:
            return one_liner
        # multi-line
        indent = "  " * (_indent + 1)
        inner = ("\n" + indent).join(
            dumps(c, _indent + 1) for c in tree
        )
        return "(" + inner + ")"
    if isinstance(tree, bool):
        return "true" if tree else "false"
    if isinstance(tree, int):
        return str(tree)
    if isinstance(tree, float):
        # avoid scientific notation; trim trailing zeros
        s = f"{tree:.10g}"
        return s
    # string atom
    s = str(tree)
    if _need_quote(s):
        return _quote(s)
    return s
