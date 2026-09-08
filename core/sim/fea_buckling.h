#pragma once

#include <string>
#include <vector>
#include "sim/sim_cancel.h"

namespace chudware::sim {
struct FeaBucklingResult {
    std::vector<double> load_factors;
    std::vector<double> mode_shapes;
    int n_modes = 0;
    int n_nodes = 0;
    int n_dof = 0;
    bool ok = false;
    std::string error;
};

// Linear bifurcation about the elastic reference-load equilibrium:
// K phi = -lambda K_G phi. Positive factors below max_load_factor, lowest first.
FeaBucklingResult fea_buckling_solve(
    const std::vector<double>& points, const std::vector<int>& tets,
    double E_mpa, double nu, const std::vector<int>& fixed_nodes,
    const std::vector<double>& nodal_forces, int n_modes = 4,
    int nodes_per_elem = 4, double max_load_factor = 1e4,
    const SimCancelToken& cancel_token = {});
}
