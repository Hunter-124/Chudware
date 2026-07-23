// Parameterized known-good ECAD designs — C++ port of engine/ecad/recipes.py.
//
// Each recipe returns a STRUCTURED design dict (components / nets / study / notes),
// not ops and not a netlist. They are pure + deterministic (no RNG) closed-form
// designs, so they port verbatim and gate EXACTLY against the Python oracle:
//   voltage_divider(vin, vout) / rc_lowpass(fc) / inverting_amp(gain) /
//   rc_555_astable(freq), plus closest_recipe(request) (keyword scoring + numeric
//   extraction). The LLM orchestrator (engine/ecad/orchestrator.py) that CONSUMES
//   these is DEFERRED to the agent/LLM epic — it drives a provider, not deterministic.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::ecad::recipes {

nlohmann::json voltage_divider(double vin = 5.0, double vout = 2.5);
nlohmann::json rc_lowpass(double fc = 1000.0);
nlohmann::json inverting_amp(double gain = 10.0);
nlohmann::json rc_555_astable(double freq = 1.0);

// build(name, params?) — build a recipe by name. params is an object that may carry
// {vin, vout} / {fc} / {gain} / {freq}; missing keys use the defaults. Throws
// OpError("invalid_op") on an unknown name (mirrors Python's KeyError surfacing).
nlohmann::json build(const std::string& name, const nlohmann::json& params);

// all_recipes() — every recipe at its default parameters (planner few-shots).
nlohmann::json all_recipes();

// closest_recipe(request) — pick + parameterize the best-matching recipe (deterministic
// keyword scoring + numeric extraction). Always returns a fully-built design.
nlohmann::json closest_recipe(const std::string& request);

}  // namespace chudware::ecad::recipes
