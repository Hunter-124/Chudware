// Steady-state thermal FEA (heat conduction) on tet4 / tet10 meshes.
//
// The third FEA solver of the simulation core, after fea_static (linear elasticity)
// and fea_modal (free-vibration). It solves the steady Fourier conduction problem
//
//     -div(k grad T) = 0   in the body
//              T = T_fixed  on Dirichlet (fixed-temperature) faces
//        -k dT/dn = q       on Neumann (heat-flux) faces          [q>0 = INTO body]
//        -k dT/dn = h (T - T_amb)  on Robin (convection film) faces
//
// discretised with the SAME isoparametric shape functions the elastic solver uses, but
// with ONE scalar DOF per node (temperature) instead of three. The conductivity matrix is
//
//     K_ij = ∫_V k ∇N_i·∇N_j dV
//
// assembled per element with the 4-point tet Gauss rule (tet10) or the analytic constant-
// gradient tet4 form. A convection face adds  h ∫_S N_i N_j dS  to K and the load
// h·T_amb·∫_S N_i dS to f; a flux face adds  q·∫_S N_i dS  to f. The reduced (free-DOF)
// system is factored with Eigen's sparse Cholesky (SPD), falling back to SparseLU, exactly
// like fea_static. Never throws: a degenerate / singular model returns ok=false + error.
//
// UNITS. The kernel is length-unit agnostic but INTERNALLY CONSISTENT: every quantity is
// expressed in the mesh length unit L (the Chudware mesh is millimetres). Callers pass
//   k  in  W/(L·K)      (conductivity per mesh-length; the glue converts W/(m·K) → W/(mm·K))
//   q  in  W/L^2        (heat flux per mesh-area)
//   h  in  W/(L^2·K)    (film coefficient per mesh-area)
// so that ∫k∇N∇N dV, ∫q N dS and h∫N N dS all come out in W/K · K = W consistently. The
// resulting nodal temperatures are in the same K/°C scale as T_fixed / T_amb.
//
// TRANSIENT. fea_thermal_transient() adds the capacitance term
//
//     C dT/dt + K T = f(t),    C_ij = ∫_V rho·cp N_i N_j dV
//
// integrated with IMPLICIT BACKWARD EULER at a fixed step dt:
//     (K + C/dt) T^{n+1} = (C/dt) T^n + f(t^{n+1})
// For full-size steps the iteration matrix A = K + C/dt is constant and is
// factored once; every step is then one back-substitution. When t_end is not an
// integer multiple of dt, the final shorter step uses its actual duration and
// its own one-off factorization, preserving the requested end-time energy.
// Capacitance is LUMPED by default (row-sum for tet4, HRZ diagonal scaling for
// tet10): a diagonal C keeps A in
// K's sparsity pattern, and lumped capacitance + backward Euler is unconditionally
// stable AND monotone (no spurious initial-transient oscillation at large dt), which is
// the standard choice for conduction. A consistent C is available (lumped=false) for
// accuracy studies. rho·cp arrives VOLUMETRIC in J/(L^3·K) (the glue converts
// kg/m^3 · J/(kg·K) → J/(mm^3·K)).
#pragma once

#include <cstddef>
#include <string>
#include <vector>
#include "sim/sim_cancel.h"

namespace chudware::sim {

// A convection (Robin) film boundary applied to exterior mesh faces: with no explicit
// exterior_face_ids, every exterior face whose corners belong to `nodes` receives
// h·∫N N dS into K and h·T_amb·∫N dS into f. A compound faceset supplies exact face IDs
// to prevent corner-node unions from selecting a face not present in any child.
// `nodes` includes corners and, for tet10, the completed mid-side nodes.
struct ThermalConvection {
    std::vector<int> nodes;   // mesh node indices on the convection face(s)
    double h = 0.0;           // film coefficient, W/(L^2·K) (L = mesh length unit)
    double t_amb = 0.0;       // ambient/bulk temperature, K or °C (same scale as t_fixed)
    // Optional sorted, unique element*4+local-face IDs. Empty selects by nodes alone.
    // A nonempty filter must contain exterior faces whose corners belong to nodes.
    std::vector<std::size_t> exterior_face_ids;
};

struct ThermalResult {
    std::vector<double> temperature;  // n_nodes, nodal temperature field (same scale as BCs)
    double max_temp = 0.0;            // max nodal temperature
    double min_temp = 0.0;            // min nodal temperature
    int n_nodes = 0;
    int n_dof = 0;                    // free DOFs solved (n_nodes - #fixed)
    int n_elems = 0;
    bool ok = false;
    std::string error;  // non-empty when ok==false
};

// Solve steady-state conduction. BCs arrive ALREADY RESOLVED to mesh nodes:
//   points        : n*3 flat node coordinates (mesh length unit)
//   tets          : m*K flat node indices (0-based), K = nodes_per_elem (4 or 10)
//   k_cond        : isotropic conductivity, W/(L·K)  (must be > 0)
//   fixed_nodes   : Dirichlet nodes held at a prescribed temperature
//   fixed_temps   : the prescribed temperature per fixed node (SAME length as fixed_nodes)
//   flux_nodes    : nodes on heat-flux faces (surface faces with all corners in the set get
//                   q·∫N dS added to the load; q>0 = heat INTO the body)
//   flux_q        : the applied surface flux q, W/L^2 (one value for the whole flux set)
//
// N.B. this single-flux signature is the LEGACY entry point (kept because external callers
// and the bit-for-bit regression gate depend on it); it wraps the per-load overload below.
// Studies with more than one flux load MUST use the ThermalFluxLoad overload — collapsing
// them onto one shared magnitude silently drops/overwrites flux (the C3 defect).
//   convections   : zero or more convection film boundaries
//   nodes_per_elem: 4 (tet4) or 10 (tet10)
//
// The problem is SINGULAR (a floating temperature datum) when there is neither a Dirichlet
// fixed-temperature node nor any convection face: pure-Neumann steady conduction has no
// unique solution. That case returns ok=false with an explanatory error (the advisor's
// precheck lints for it up front). Never throws.
ThermalResult fea_thermal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                                double k_cond, const std::vector<int>& fixed_nodes,
                                const std::vector<double>& fixed_temps,
                                const std::vector<int>& flux_nodes, double flux_q,
                                const std::vector<ThermalConvection>& convections,
                                int nodes_per_elem = 4);

// ── Transient ────────────────────────────────────────────────────────────────────────────

// A piecewise-constant (sample-and-hold) load schedule s(t): scale s = scales[i] holds on
// [times[i], times[i+1]) — times ascending, times[0] == 0, sizes equal. The last scale holds
// to t_end (or, when period > 0, to the end of the period, after which the whole pattern
// repeats: s(t) = s(fmod(t, period))). An EMPTY schedule means s(t) = 1 (always on).
//
// This shape is a direct target for the aicad.powerprofile/1 per-rail duty-cycle profiles:
// a rail with duty d and period p maps to {times:[0, d*p], scales:[1, 0],
// period:p}; an arbitrary residency timeline maps to its own breakpoint list.
struct ThermalSchedule {
    std::vector<double> times;   // ascending breakpoints, starting at 0 (seconds)
    std::vector<double> scales;  // dimensionless multiplier held from each breakpoint
    double period = 0.0;         // > 0: the pattern repeats every `period` seconds
};

// s(t) for a schedule (exposed so tests / glue / the Python oracle share one definition).
double thermal_schedule_scale(const ThermalSchedule& sched, double t);

// One heat-flux load for the transient solver: its own node set, magnitude and schedule
// (the steady solver's single flux set generalises to N independently-scheduled sources —
// one per power rail when driven from a power profile).
struct ThermalFluxLoad {
    std::vector<int> nodes;   // mesh nodes on the loaded face(s)
    double q = 0.0;           // flux magnitude, W/L^2 (q>0 = heat INTO the body)
    ThermalSchedule schedule; // time modulation of q (empty = constant)
    // Narrows node-union selection to these exterior facets; same contract as convection.
    std::vector<std::size_t> exterior_face_ids;
};

// Steady-state conduction with N independent flux loads, each with its OWN node set and
// magnitude (schedules are ignored — steady state has no time axis). This is the primary
// implementation; the single-flux signature above delegates here with one load, so a
// single-flux study is bit-for-bit identical through either entry point.
ThermalResult fea_thermal_solve(const std::vector<double>& points, const std::vector<int>& tets,
                                double k_cond, const std::vector<int>& fixed_nodes,
                                const std::vector<double>& fixed_temps,
                                const std::vector<ThermalFluxLoad>& fluxes,
                                const std::vector<ThermalConvection>& convections,
                                int nodes_per_elem = 4,
                                const SimCancelToken& cancel_token = {});

// Maximum Fourier heat-flux magnitude at element integration points, in W/L^2.
// Inputs must be the validated mesh and temperature field of a successful solve.
double thermal_flux_max(const std::vector<double>& points, const std::vector<int>& tets,
                        const std::vector<double>& temperature, double k_cond,
                        int nodes_per_elem);

struct ThermalTransientOptions {
    double rho_cp = 0.0;   // volumetric heat capacity rho*cp, J/(L^3·K); must be > 0
    double t_end = 0.0;    // total simulated time, s; must be > 0
    double dt = 0.0;       // nominal backward-Euler step, s; must be > 0
    double t_init = 0.0;   // uniform initial temperature (used when init_temps is empty)
    std::vector<double> init_temps;   // optional per-node initial field (size n_nodes)
    std::vector<double> frame_times;  // times at which to record a full nodal frame; each
                                      // snaps to the first step time >= the request. The
                                      // final step (t_end) is ALWAYS recorded.
    bool lumped = true;    // diagonal (row-sum / HRZ) capacitance; false = consistent
};

struct ThermalFrame {
    double time = 0.0;                // step time the frame was recorded at, s
    std::vector<double> temperature;  // n_nodes nodal field
};

struct ThermalTransientResult {
    std::vector<ThermalFrame> frames;  // requested frames + the final step, ascending time
    std::vector<double> envelope_max;  // per-node max temperature over the WHOLE run
                                       // (including the initial condition)
    double max_temp = 0.0;       // global max over the run (max of envelope_max)
    double max_temp_time = 0.0;  // step time at which the global max was first reached
    double min_temp = 0.0;       // global min over the run
    int n_nodes = 0;
    int n_dof = 0;
    int n_elems = 0;
    int n_steps = 0;             // backward-Euler steps taken
    bool ok = false;
    std::string error;
};

// Transient conduction with backward Euler (see the header comment). Unlike the steady
// solver, a model with NO Dirichlet node and NO convection is WELL-POSED here (pure
// adiabatic heating — the capacitance anchors the evolution), so no singularity guard.
ThermalTransientResult fea_thermal_transient(
    const std::vector<double>& points, const std::vector<int>& tets, double k_cond,
    const std::vector<int>& fixed_nodes, const std::vector<double>& fixed_temps,
    const std::vector<ThermalFluxLoad>& fluxes,
    const std::vector<ThermalConvection>& convections, const ThermalTransientOptions& opts,
    int nodes_per_elem = 4, const SimCancelToken& cancel_token = {});

}  // namespace chudware::sim
