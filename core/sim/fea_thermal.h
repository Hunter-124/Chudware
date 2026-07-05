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
// expressed in the mesh length unit L (the AI-CAD mesh is millimetres). Callers pass
//   k  in  W/(L·K)      (conductivity per mesh-length; the glue converts W/(m·K) → W/(mm·K))
//   q  in  W/L^2        (heat flux per mesh-area)
//   h  in  W/(L^2·K)    (film coefficient per mesh-area)
// so that ∫k∇N∇N dV, ∫q N dS and h∫N N dS all come out in W/K · K = W consistently. The
// resulting nodal temperatures are in the same K/°C scale as T_fixed / T_amb. Transient
// (dT/dt) conduction is OUT of scope — a follow-up (needs the consistent capacitance C and
// a time integrator); this solver is steady-state only.
#pragma once

#include <string>
#include <vector>

namespace aicad::sim {

// A convection (Robin) film boundary applied to a set of mesh nodes: every ELEMENT SURFACE
// FACE whose corner nodes are all in `nodes` receives h·∫N N dS into K and h·T_amb·∫N dS
// into f. `nodes` is the resolved node set for the face (corners for tet4, corners+mid-side
// for tet10 — the glue completes the mid-side nodes just as the static loader does).
struct ThermalConvection {
    std::vector<int> nodes;   // mesh node indices on the convection face(s)
    double h = 0.0;           // film coefficient, W/(L^2·K) (L = mesh length unit)
    double t_amb = 0.0;       // ambient/bulk temperature, K or °C (same scale as t_fixed)
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

}  // namespace aicad::sim
