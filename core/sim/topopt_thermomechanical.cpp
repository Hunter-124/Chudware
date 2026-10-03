#include "sim/topopt_thermomechanical.h"

#include "sim/topopt_elastic.h"
#include "sim/topopt_thermal.h"

#include <Eigen/Dense>
#include <nlohmann/json.hpp>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace chudware {
namespace sim {
namespace topology {

namespace {

using Vec6 = Eigen::Matrix<double, 6, 1>;
using Mat6X = Eigen::Matrix<double, 6, Eigen::Dynamic>;

[[noreturn]] void fail(const std::string &code, const std::string &message) {
  throw ModelError(code, message);
}

void check_cancel(const std::atomic<bool> *cancel) {
  if (cancel && cancel->load(std::memory_order_relaxed))
    throw EvaluationCancelled();
}

std::string fmt(double v) {
  char buf[48];
  std::snprintf(buf, sizeof buf, "%.6g", v);
  return buf;
}

nlohmann::json vec3_json(const Eigen::Vector3d &v) {
  return nlohmann::json::array({v[0], v[1], v[2]});
}

std::shared_ptr<const Problem>
require_thermomechanical(std::shared_ptr<const Problem> p) {
  if (!p || !p->mesh)
    fail("invalid_input",
         "thermomechanical physics needs a prepared problem with a mesh");
  if (p->settings.physics != PhysicsKind::Thermomechanical)
    fail("physics_mismatch",
         std::string("thermoelastic physics needs physics thermomechanical, "
                     "got ") +
             to_string(p->settings.physics));
  if (p->settings.objective != ObjectiveKind::Compliance)
    fail("objective_not_supported",
         std::string("thermomechanical physics optimizes compliance, got ") +
             to_string(p->settings.objective));
  if (!p->structural)
    fail("section_missing",
         "thermomechanical physics needs a prepared structural boundary");
  if (!p->thermal)
    fail("section_missing",
         "thermomechanical physics needs a prepared thermal boundary");
  if (!p->thermal->reference_given)
    fail("section_missing", "thermomechanical needs "
                            "thermal.reference_temperature_c (strain-free "
                            "temperature)");
  if (!p->material.expansion_per_k)
    fail("material_missing",
         "physics thermomechanical needs material.alpha_per_k");
  return p;
}

// True when the prepared thermal boundary can make theta nonzero.
bool thermal_drive(const ThermalBoundary &tb) {
  for (const ThermalFlux &f : tb.fluxes)
    if (!f.patch.faces.empty() && f.flux_w_m2 != 0.0)
      return true;
  for (int i = 0; i < tb.fixed.size(); ++i)
    if (tb.fixed.values[i] != tb.reference_temperature_k)
      return true;
  for (const ThermalConvection &c : tb.convections)
    if (!c.patch.faces.empty() && c.ambient_k != tb.reference_temperature_k)
      return true;
  return false;
}

class ThermomechanicalPhysics final : public Physics {
public:
  explicit ThermomechanicalPhysics(std::shared_ptr<const Problem> problem)
      : problem_(std::move(problem)) {
    const Problem &p = *problem_;
    const StructuralBoundary &sb = *p.structural;
    alpha_ = *p.material.expansion_per_k;

    // The compliance identity F.u = u^T K u and the adjoint chains below need
    // homogeneous supports; prepared supports are zero displacement.
    for (const StructuralCase &c : sb.cases)
      if ((c.fixed.values.array() != 0.0).any())
        fail("invalid_bc", "load case " + c.name +
                               " prescribes a nonzero support displacement; "
                               "thermoelastic compliance needs fixed "
                               "supports at zero");
    const bool drive = alpha_ != 0.0 && thermal_drive(*p.thermal);
    if (!drive)
      for (const StructuralCase &c : sb.cases)
        if (c.load_nodes.empty())
          fail("zero_load",
               "load case " + c.name + " applies no force and " +
                   (alpha_ == 0.0
                        ? std::string("material.alpha_per_k is 0, so "
                                      "temperature produces no load")
                        : std::string("the temperature field equals the "
                                      "strain-free reference")));

    int sets = 0;
    for (std::size_t c = 0; c < sb.cases.size(); ++c) {
      bool seen = false;
      for (std::size_t d = 0; d < c && !seen; ++d)
        seen = sb.cases[d].fixed.dofs.size() == sb.cases[c].fixed.dofs.size() &&
               sb.cases[d].fixed.dofs == sb.cases[c].fixed.dofs &&
               sb.cases[d].fixed.values == sb.cases[c].fixed.values;
      sets += seen ? 0 : 1;
    }
    const std::uint64_t n3 = 3ull * p.mesh->node_count();
    const std::uint64_t E = static_cast<std::uint64_t>(p.mesh->element_count());
    // Per case: forces (here + state), displacement, reaction, adjoint rhs and
    // solution; plus the thermal load and two 6 x E eigenstrain matrices.
    const std::uint64_t dense =
        sizeof(double) * (n3 * sb.cases.size() * 8ull + n3 + 12ull * E);
    admit_elastic_memory(p, std::max(1, sets), dense,
                         "topology thermomechanical evaluator");

    thermal_ = std::make_unique<ThermalSystem>(problem_);
    model_ = std::make_shared<ElasticModel>(problem_);
    cases_ = std::make_unique<ElasticCases>(model_);

    const int Ei = model_->element_count();
    e0_ = Mat6X::Zero(6, Ei);
    grad_e0_.resize(6, Ei);
    thermal_force_.resize(model_->dof_count());
    forces_.resize(model_->dof_count(), cases_->case_count());
    g_theta_.resize(model_->node_count());
    mu_.resize(model_->node_count());
    formulation_ = build_formulation();
  }

  PhysicsKind kind() const override { return PhysicsKind::Thermomechanical; }
  std::string formulation() const override { return formulation_; }

  Evaluation evaluate(const Eigen::VectorXd &rho, bool derivatives,
                      const std::atomic<bool> *cancel) override {
    check_density(*problem_, rho);
    check_cancel(cancel);
    const Problem &p = *problem_;
    const StructuralBoundary &sb = *p.structural;
    const ElasticModel &model = *model_;
    const TetMesh &mesh = *p.mesh;
    const Interpolation &ip = p.interpolation;
    const int E = model.element_count();
    const int C = cases_->case_count();

    // 1. Temperature rise over the strain-free reference.
    thermal_->solve(rho, cancel);
    const Eigen::VectorXd &theta = thermal_->temperature_rise();
    double mean_rise = 0;
    for (int e = 0; e < E; ++e) {
      const double t = 0.25 * (theta[mesh.tets(0, e)] + theta[mesh.tets(1, e)] +
                               theta[mesh.tets(2, e)] + theta[mesh.tets(3, e)]);
      mean_rise += mesh.volume_m3[e] * t;
      const double strain = alpha_ * t;
      e0_(0, e) = strain;
      e0_(1, e) = strain;
      e0_(2, e) = strain;
    }
    mean_rise /= mesh.total_volume_m3;
    check_cancel(cancel);

    // 2. Interpolated eigenstrain load plus each case's mechanical load.
    thermal_force_.setZero();
    model.add_eigenstrain_force(rho, e0_, thermal_force_);
    forces_ = cases_->reference_forces();
    forces_.colwise() += thermal_force_;
    for (int c = 0; c < C; ++c) {
      const double norm = forces_.col(c).norm();
      if (!std::isfinite(norm))
        fail("singular_system",
             "load case " + sb.cases[c].name + " total load is not finite");
      if (!(norm > 0))
        fail("zero_load", "load case " + sb.cases[c].name +
                              " has no mechanical force and the thermal "
                              "eigenstrain load vanishes at this density");
    }
    check_cancel(cancel);

    // 3. Mechanical state of every case (one factor per support set).
    cases_->solve(rho, forces_, cancel);
    check_cancel(cancel);

    Evaluation ev;
    ev.derivatives = derivatives;
    ev.objective_unit = "J";
    std::vector<double> mech_work(C), thermal_work(C), elastic_energy(C);
    if (derivatives) {
      ev.objective_gradient.setZero(E);
      grad_e0_.setZero();
    }
    for (int c = 0; c < C; ++c) {
      const ElasticCaseState &st = cases_->state(c);
      const double w = sb.cases[c].weight;
      ev.objective += w * st.compliance;
      mech_work[c] = cases_->reference_forces().col(c).dot(st.displacement);
      thermal_work[c] = thermal_force_.dot(st.displacement);
      if (derivatives) {
        // -u^T K' u + 2 u^T (d f_th/d rho)|theta, and 2 (d f_th/d e0)^T u.
        model.add_stiffness_contraction(rho, st.displacement, st.displacement,
                                        -w, ev.objective_gradient);
        model.eigenstrain_force_adjoint(rho, e0_, st.displacement, 2.0 * w,
                                        &ev.objective_gradient, &grad_e0_);
      }
    }
    if (derivatives)
      add_temperature_chain(grad_e0_, ev.objective_gradient, cancel);
    check_cancel(cancel);

    // 4. Responses.
    const std::optional<double> stress_limit = p.limits.stress_pa;
    const std::optional<double> disp_limit = p.limits.displacement_m;
    const double field_stress_scale =
        stress_limit ? *stress_limit
                     : (p.material.yield_pa ? *p.material.yield_pa
                                            : *p.material.youngs_pa);
    std::vector<StressResponse> stress(C);
    std::vector<DisplacementResponse> disp(C);
    for (int c = 0; c < C; ++c) {
      check_cancel(cancel);
      const ElasticCaseState &st = cases_->state(c);
      stress[c] =
          stress_response(model, rho, st.displacement, &e0_, field_stress_scale,
                          derivatives && stress_limit.has_value());
      if (disp_limit)
        disp[c] = displacement_response(model, st.displacement, *disp_limit,
                                        derivatives);
    }
    if (derivatives && (stress_limit || disp_limit)) {
      const int per = (stress_limit ? 1 : 0) + (disp_limit ? 1 : 0);
      adjoint_rhs_.resize(model.dof_count(), C * per);
      adjoint_case_.assign(static_cast<std::size_t>(C * per), 0);
      int col = 0;
      for (int c = 0; c < C; ++c) {
        if (stress_limit) {
          adjoint_rhs_.col(col) = stress[c].d_displacement;
          adjoint_case_[col++] = c;
        }
        if (disp_limit) {
          adjoint_rhs_.col(col) = disp[c].d_displacement;
          adjoint_case_[col++] = c;
        }
      }
      check_cancel(cancel);
      cases_->solve_adjoint(adjoint_rhs_, adjoint_case_, adjoint_);
    }

    nlohmann::json resp_ev = nlohmann::json::array();
    int col = 0;
    for (int c = 0; c < C; ++c) {
      const std::string suffix = C > 1 ? "@" + sb.cases[c].name : "";
      const ElasticCaseState &st = cases_->state(c);
      if (stress_limit) {
        Constraint k;
        k.name = "max_von_mises" + suffix;
        k.unit = "Pa";
        k.response = stress[c].value;
        k.limit = *stress_limit;
        k.value = k.response / k.limit - 1.0;
        k.aggregation = stress_aggregation_label(stress[c].count);
        if (derivatives) {
          check_cancel(cancel);
          k.gradient = stress[c].d_density;
          model.add_stiffness_contraction(rho, adjoint_.col(col),
                                          st.displacement, -1.0, k.gradient);
          grad_e0_ = stress[c].d_eigenstrain; // direct e0 in C (B u - e0)
          model.eigenstrain_force_adjoint(rho, e0_, adjoint_.col(col), 1.0,
                                          &k.gradient, &grad_e0_);
          add_temperature_chain(grad_e0_, k.gradient, cancel);
          k.gradient /= k.limit;
          ++col;
        }
        resp_ev.push_back(
            {{"constraint", k.name},
             {"aggregate_pa", stress[c].value},
             {"raw_max_pa", stress[c].max_value},
             {"raw_max_element", stress[c].max_element},
             {"aggregate_upper_bound_pa", stress[c].upper_bound}});
        ev.constraints.push_back(std::move(k));
      }
      if (disp_limit) {
        Constraint k;
        k.name = "max_displacement" + suffix;
        k.unit = "m";
        k.response = disp[c].value;
        k.limit = *disp_limit;
        k.value = k.response / k.limit - 1.0;
        k.aggregation = displacement_aggregation_label(disp[c].count);
        if (derivatives) {
          check_cancel(cancel);
          k.gradient.setZero(E);
          model.add_stiffness_contraction(rho, adjoint_.col(col),
                                          st.displacement, -1.0, k.gradient);
          grad_e0_.setZero();
          model.eigenstrain_force_adjoint(rho, e0_, adjoint_.col(col), 1.0,
                                          &k.gradient, &grad_e0_);
          add_temperature_chain(grad_e0_, k.gradient, cancel);
          k.gradient /= k.limit;
          ++col;
        }
        resp_ev.push_back({{"constraint", k.name},
                           {"aggregate_m", disp[c].value},
                           {"raw_max_m", disp[c].max_value},
                           {"raw_max_node", disp[c].max_node},
                           {"aggregate_upper_bound_m", disp[c].upper_bound}});
        ev.constraints.push_back(std::move(k));
      }
    }

    nlohmann::json temperature_ev = nullptr;
    if (p.limits.temperature_k) {
      const double limit = *p.limits.temperature_k;
      Constraint k =
          thermal_->max_temperature_constraint(limit, derivatives, cancel);
      int node = -1;
      const double peak = thermal_->peak_temperature_k(&node);
      const double beta = thermal_->max_temperature_beta(limit);
      temperature_ev = {
          {"limit_k", limit},
          {"ks_k", k.response},
          {"raw_peak_k", peak},
          {"raw_peak_node", node},
          {"raw_value", peak / limit - 1.0},
          {"beta_per_k", beta},
          {"ks_minus_peak_k", k.response - peak},
          {"ks_gap_bound_k",
           std::log(static_cast<double>(thermal_->referenced_node_count())) /
               beta}};
      resp_ev.push_back({{"constraint", k.name},
                         {"aggregate_k", k.response},
                         {"raw_max_k", peak},
                         {"raw_max_node", node}});
      ev.constraints.push_back(std::move(k));
    }
    check_cancel(cancel);

    // 5. Fields.
    Field temperature{"temperature", "K", FieldLocation::Node, 1, {}};
    thermal_->temperature(temperature.values);
    ev.fields.push_back(std::move(temperature));
    Field flux{"heat_flux", "W/m2", FieldLocation::Element, 3, {}};
    thermal_->heat_flux(flux.values);
    ev.fields.push_back(std::move(flux));
    ev.fields.push_back({"thermal_strain", "-", FieldLocation::Element, 1,
                         Eigen::VectorXd(e0_.row(0).transpose())});

    const Eigen::Matrix<double, 6, 6> &Cm = model.elasticity();
    nlohmann::json stress_ev = nlohmann::json::array();
    for (int c = 0; c < C; ++c) {
      const std::string suffix = C > 1 ? "@" + sb.cases[c].name : "";
      const ElasticCaseState &st = cases_->state(c);
      Field sig{"stress" + suffix, "Pa", FieldLocation::Element, 6,
                Eigen::VectorXd(6 * E)};
      Field sed{"strain_energy_density" + suffix, "J/m3",
                FieldLocation::Element, 1, Eigen::VectorXd(E)};
      double energy = 0, solid_max = 0;
      for (int e = 0; e < E; ++e) {
        const Vec6 eps = model.strain(e, st.displacement) - e0_.col(e);
        const Vec6 ce = Cm * eps;
        const double s = ip.scale(rho[e]);
        sig.values.segment<6>(6 * e) = s * ce;
        sed.values[e] = 0.5 * s * eps.dot(ce);
        energy += mesh.volume_m3[e] * sed.values[e];
        if (rho[e] >= p.settings.density_threshold)
          solid_max = std::max(solid_max, stress[c].solid_von_mises[e]);
      }
      elastic_energy[c] = energy;
      stress_ev.push_back(
          {{"case", sb.cases[c].name},
           {"max_relaxed_von_mises_pa", stress[c].max_value},
           {"max_relaxed_element", stress[c].max_element},
           {"max_solid_von_mises_above_threshold_pa", solid_max}});
      ev.fields.push_back({"displacement" + suffix, "m", FieldLocation::Node, 3,
                           st.displacement});
      ev.fields.push_back(std::move(sig));
      ev.fields.push_back({"von_mises" + suffix, "Pa", FieldLocation::Element,
                           1, std::move(stress[c].von_mises)});
      ev.fields.push_back(std::move(sed));
      ev.fields.push_back(
          {"reaction" + suffix, "N", FieldLocation::Node, 3, st.reaction});
    }

    // 6. Evidence.
    nlohmann::json terms = nlohmann::json::array();
    for (int c = 0; c < C; ++c)
      terms.push_back({{"case", sb.cases[c].name},
                       {"weight", sb.cases[c].weight},
                       {"compliance_j", cases_->state(c).compliance},
                       {"mechanical_load_work_j", mech_work[c]},
                       {"thermal_load_work_j", thermal_work[c]},
                       {"elastic_strain_energy_j", elastic_energy[c]}});
    Eigen::Vector3d resultant = Eigen::Vector3d::Zero();
    double gross = 0;
    for (int n = 0; n < model.node_count(); ++n)
      for (int d = 0; d < 3; ++d) {
        resultant[d] += thermal_force_[3 * n + d];
        gross += std::abs(thermal_force_[3 * n + d]);
      }
    ev.evidence = {
        {"formulation", formulation_},
        {"objective",
         {{"name", "compliance"},
          {"definition", "sum_c w_c (f_mech,c + f_th).u_c with f_th = sum_e "
                         "s(rho_e) V_e B_e^T C e0_e"},
          {"unit", "J"},
          {"value", ev.objective},
          {"terms", std::move(terms)}}},
        {"coupling",
         {{"expansion_per_k", alpha_},
          {"strain_free_reference_k", thermal_->reference_temperature_k()},
          {"eigenstrain", "alpha * mean_element(T - T_ref) * [1,1,1,0,0,0]"},
          {"density_model",
           "alpha intrinsic; stiffness and thermal load share s(rho)"},
          {"mean_temperature_rise_k", mean_rise},
          {"thermal_load_norm_n", thermal_force_.norm()},
          {"thermal_load_resultant_n", vec3_json(resultant)},
          {"thermal_load_relative_resultant",
           gross > 0 ? resultant.norm() / gross : 0.0}}},
        {"thermal", thermal_->evidence()},
        {"elastic", cases_->evidence()},
        {"stress",
         {{"measure", "eta(rho)*von_mises(C (B u - e0)), qp-relaxed"},
          {"cases", std::move(stress_ev)}}},
        {"responses", std::move(resp_ev)}};
    if (!temperature_ev.is_null())
      ev.evidence["max_temperature"] = std::move(temperature_ev);
    return ev;
  }

private:
  // grad += mu^T (-K_T' theta) with K_T mu = sum_e (d e0_e / d theta)^T
  // g_e0(:, e): the eigenstrain sensitivity chained through the conduction
  // solve. d e0_e / d theta_n = alpha / 4 [1,1,1,0,0,0] for the 4 nodes of e.
  void add_temperature_chain(const Mat6X &g_e0, Eigen::VectorXd &grad,
                             const std::atomic<bool> *cancel) {
    const TetMesh &mesh = *problem_->mesh;
    g_theta_.setZero();
    const double k = 0.25 * alpha_;
    bool any = false;
    for (int e = 0; e < mesh.element_count(); ++e) {
      const double s = k * (g_e0(0, e) + g_e0(1, e) + g_e0(2, e));
      if (s == 0.0)
        continue;
      any = true;
      for (int a = 0; a < 4; ++a)
        g_theta_[mesh.tets(a, e)] += s;
    }
    if (!any)
      return;
    thermal_->solve_adjoint(g_theta_, mu_, cancel);
    thermal_->add_state_gradient(mu_, 1.0, grad);
  }

  std::string build_formulation() const {
    const Problem &p = *problem_;
    const Interpolation &ip = p.interpolation;
    std::string s =
        "one-way coupled steady thermoelasticity on tet4 (isotropic, small "
        "strain): conduction k = k_s s(rho) solved for theta = T - T_ref with "
        "T_ref the strain-free reference; element eigenstrain alpha * "
        "mean(theta) [1,1,1,0,0,0] with intrinsic alpha (not interpolated); "
        "stiffness and thermal load share s(rho) = eps + (1 - eps) f, ";
    s += ip.kind == Interpolation::Kind::Simp
             ? "simp f = rho^" + fmt(ip.exponent)
             : "ramp f = rho/(1+" + fmt(ip.exponent) + "(1-rho))";
    s += ", eps = " + fmt(ip.floor) +
         "; objective weighted total-load compliance sum w_c (f_mech + "
         "f_th).u_c, exact gradient -u^T K' u + 2 u^T df_th/drho plus thermal "
         "adjoint for dtheta/drho; " +
         std::to_string(cases_->case_count()) + " load case(s) on " +
         std::to_string(cases_->system_count()) +
         " support set(s), one Cholesky factor per set";
    if (p.limits.stress_pa)
      s += "; stress: KS(P=" + fmt(kElasticKsParameter) +
           ") of squared qp-relaxed von Mises of C (B u - e0), mechanical + "
           "thermal adjoint";
    if (p.limits.displacement_m)
      s += "; displacement: KS(P=" + fmt(kElasticKsParameter) +
           ") of squared nodal |u|, mechanical + thermal adjoint";
    if (p.limits.temperature_k)
      s += "; max temperature by KS upper bound, thermal adjoint";
    return s;
  }

  std::shared_ptr<const Problem> problem_;
  double alpha_ = 0;
  std::unique_ptr<ThermalSystem> thermal_;
  std::shared_ptr<const ElasticModel> model_;
  std::unique_ptr<ElasticCases> cases_;
  std::string formulation_;

  // Per-evaluation storage, sized once.
  Mat6X e0_, grad_e0_;
  Eigen::VectorXd thermal_force_, g_theta_, mu_;
  Eigen::MatrixXd forces_, adjoint_rhs_, adjoint_;
  std::vector<int> adjoint_case_;
};

} // namespace

std::unique_ptr<Physics>
make_thermomechanical_physics(std::shared_ptr<const Problem> problem) {
  return std::make_unique<ThermomechanicalPhysics>(
      require_thermomechanical(std::move(problem)));
}

} // namespace topology
} // namespace sim
} // namespace chudware
