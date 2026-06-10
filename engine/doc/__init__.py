"""Parametric document model (contracts in docs/contracts/).

M0 ships the contract enforcement layer: schema validation + equation evaluation.
The compiler, selectors, tessellation, and sessions land in M1 (see docs/PLAN.md).
"""

from .equations import EquationError, evaluate, evaluate_globals, validate_expr
from .schema import (
    SCHEMA_ASM,
    SCHEMA_PART,
    FEATURE_TYPES,
    RESERVED_DATUMS,
    validate_assembly,
    validate_document,
    validate_part,
)

__all__ = [
    "EquationError",
    "evaluate",
    "evaluate_globals",
    "validate_expr",
    "SCHEMA_ASM",
    "SCHEMA_PART",
    "FEATURE_TYPES",
    "RESERVED_DATUMS",
    "validate_assembly",
    "validate_document",
    "validate_part",
]
