// expr.{h,cpp} — safe arithmetic expression evaluator for document dimension
// expressions. C++ port of engine/doc/equations.py (the `evaluate` entry point).
//
// Grammar (mirrors equations.py exactly): numbers, names, + - * / ^ (and **),
// unary - / +, parentheses, and a whitelisted function set. `^` is normalized to
// `**` textually before parsing (so it inherits Python's `**` precedence: high &
// right-associative, NOT bitwise-xor). Trig works in DEGREES in and out
// (CAD-user convention): sin(30) == 0.5, asin(0.5) == 30. The constant `pi` is
// available; names not in the function/const set resolve from `env`.
//
// This exists so the C++ resolve_dim() matches the Python _resolve_dim() for
// arithmetic-expression dims (e.g. expr:"A*B", "W/2", "2*pi*R") within 1e-9, the
// differential-harness coordinate tolerance.
#pragma once

#include <map>
#include <stdexcept>
#include <string>

namespace aicad::util {

// Thrown for syntax errors, unknown names/functions, disallowed constructs,
// division by zero, math-domain errors, and non-finite results — mirroring
// equations.EquationError. Callers may catch this to degrade gracefully.
class ExprError : public std::runtime_error {
 public:
    explicit ExprError(const std::string& msg) : std::runtime_error(msg) {}
};

// Evaluate one expression against an environment of resolved variable names.
// Throws ExprError on any failure. Matches engine/doc/equations.evaluate().
double evaluate(const std::string& expr, const std::map<std::string, double>& env);

}  // namespace aicad::util
