// core/piping/solve.h — steady-state 1-D pipe-network hydraulic solver.
// C++ port of engine_oracle/piping/solve.py: nodal head solve (liquid,
// Darcy-Weisbach + fitting K-factors, pump curves) and the isothermal-
// compressible gas solve (mass balance on P^2), plus the per-segment
// pressure-drop report, pump-curve import with affinity scaling, and the
// pump operating point + NPSH margin. Pure computation — the `fluids`
// library calls the oracle makes (Clamond friction factor, Rennels fitting
// K formulas, B36 schedule tables) are re-implemented/vendored here, so this
// port always behaves like a fluids-present box. Contract-shaped soft
// failures ({"ok": false, "reason", "hint"}) instead of exceptions.
#pragma once

#include <string>

#include "piping/fluid_properties.h"
#include "piping/pyutil.h"

namespace chudware::piping {

using pyutil::ojson;

inline constexpr double kG = 9.80665;        // m/s²
inline constexpr double kSnapTolMm = 1.0;    // endpoint snap tolerance

// Deterministic fitting loss preview; uses the same K evaluator as network/pipe_flow.
ojson piping_fitting_k(const ojson& params);
// Import a pump-curve CSV (text or path) with affinity-law scaling
// (Q' = Q·r, H' = H·r²). {"ok": true, "points": [[Q_m3h, H_m], ...],
// "shutoff_head", "max_flow", "speed_ratio"} or contract-shaped failure.
ojson load_pump_curve(const std::string& csv_source, double speed_ratio = 1.0,
                      const std::string& flow_col = "flow",
                      const std::string& head_col = "head");

// Solve an aicad.piping/1 network for node heads/pressures + link flows.
// network_type "liquid" (default) or "gas"/"compressible" (routes to
// solve_gas). boundary: array of per-node BCs (see contracts/piping_schema.md
// and the oracle docstring); fluid as resolve_solver_fluid; pumps: array of
// {"run", "curve", "speed_ratio"?, "direction"?}.
ojson solve(const ojson& doc, const ojson& boundary, const ojson& fluid = nullptr,
            const ojson& pumps = nullptr, const std::string& network_type = "liquid",
            double tol_mm = kSnapTolMm, int max_iter = 100, double tol_head = 1e-6);

// Isothermal-compressible (Weymouth-form) network solve.
ojson solve_gas(const ojson& doc, const ojson& boundary, const ojson& fluid = nullptr,
                double tol_mm = kSnapTolMm, int max_iter = 100,
                double tol_mass = 1e-9);

// Per-segment pressure-drop report with the K-factor breakdown. Pass a prior
// solve `result`, or a `boundary` to solve first.
ojson pressure_drop_report(const ojson& doc, const ojson& result = nullptr,
                           const ojson& boundary = nullptr, const ojson& fluid = nullptr,
                           const ojson& pumps = nullptr, double tol_mm = kSnapTolMm);

// 1-D pressure-loss estimate of ONE run at a given flow, with exactly the friction law and
// fitting K-factors piping_solve applies to that link (Clamond/Colebrook or 64/Re over the
// corner-to-corner centreline length; Rennels bends, Crane fittings and explicit K models;
// run_id/tee-role attribution, or legacy origin-on-path). A corner with no elbow is still a
// swept bend (run.bend_radius, null = 1.5·OD) and adds a Rennels bend K.
// roughness_mm < 0 keeps the run's material roughness. Plain nlohmann::json in/out so the
// sim layer can take it as a callback (sim::PipeRunLossFn).
// {ok, run_id, inner_dia_mm, velocity_ms, reynolds, friction_factor, roughness_mm,
//  material_roughness_mm, dynamic_pressure_pa, centreline_length_mm, bend_radius_mm, bends,
//  friction_pa, fittings:[{id,type,K,method,dp_pa}], fittings_K, fittings_pa, total_pa}
//  | {ok:false, reason, hint}.
nlohmann::json run_loss_1d(const nlohmann::json& doc, const std::string& run_id, double rho,
                           double mu, double flow_m3s, double roughness_mm);

// Pump duty point + NPSH margin. pump_curve: CSV text/path or a
// load_pump_curve result. system: {"static_head_m", "k"}; derived from
// doc+boundary when omitted.
ojson operating_point(const ojson& pump_curve, const ojson& system = nullptr,
                      const ojson& doc = nullptr, const ojson& boundary = nullptr,
                      const ojson& fluid = nullptr, const ojson& q_ref_m3h = nullptr,
                      const ojson& npshr_curve = nullptr, const ojson& suction = nullptr,
                      double speed_ratio = 1.0);

}  // namespace chudware::piping
