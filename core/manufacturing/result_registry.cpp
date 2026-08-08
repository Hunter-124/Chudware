#include "manufacturing/result_registry.h"

#include <algorithm>
#include <array>
#include <set>
#include <stdexcept>
#include <utility>

namespace chudware::mfg {

std::string_view to_string(EpochPolicy value) {
    switch (value) {
        case EpochPolicy::Falsify: return "falsify";
        case EpochPolicy::Reestablish: return "reestablish";
        case EpochPolicy::EpochInvariant: return "epoch_invariant";
    }
    throw std::invalid_argument("invalid EpochPolicy");
}

ResultRegistry::ResultRegistry() = default;

ResultRegistry::ResultRegistry(std::vector<ResultKindPolicy> policies)
    : policies_(std::move(policies)) {
    std::set<std::string> kinds;
    for (const auto& policy : policies_) {
        if (policy.domain.empty() || policy.kind.empty() || policy.runtime_invalidation_triggers.empty())
            throw std::invalid_argument("result kind requires domain, kind, and runtime triggers");
        if (!kinds.insert(policy.kind).second)
            throw std::invalid_argument("duplicate result kind: " + policy.kind);
        if (policy.kind.ends_with("procedural_fact") && !policy.supersede_with_parent)
            throw std::invalid_argument("mfg_procedural_fact_must_follow_parent");
        if ((policy.kind.ends_with("capability") || policy.kind == "capability_report") &&
            policy.supersede_with_parent)
            throw std::invalid_argument("capability result must not follow parent");
    }
}

ResultRegistry ResultRegistry::manufacturingDefaults() {
    using E = EpochPolicy;
    std::vector<ResultKindPolicy> rows;
    rows.reserve(84);
    auto add = [&](std::string domain, std::string kind, E epoch, std::string triggers,
                   bool retain = false, bool follows = false) {
        rows.push_back({std::move(domain), std::move(kind), epoch, std::move(triggers), retain, follows});
    };

    // Gateway/device lane.
    add("gateway", "controller_snapshot", E::Falsify,
        "falsify on telemetry gap, ttl expiry, reset, alarm, or transport loss", true);
    add("gateway", "device_identity", E::Falsify,
        "falsify on transport loss, identity re-probe mismatch, or fingerprint change");
    add("gateway", "capability_report", E::EpochInvariant,
        "re-derive on any cited evidence invalidation or emulator-stage change");
    add("gateway", "lease", E::Falsify, "falsify on release, expiry, preemption, or process death");
    add("gateway", "preflight_evidence", E::Falsify,
        "falsify on setup change, telemetry loss, bound-input change, or expiry");
    add("gateway", "approval_binding", E::Reestablish,
        "falsify on revoke, expiry, nonce burn, fingerprint change, or bound-input change; otherwise supersede after replay revalidation");
    add("gateway", "run_record", E::Reestablish,
        "terminal rows remain invariant; non-terminal rows falsify and transition to abandoned after process death");
    add("gateway", "nc_artifact_hash", E::EpochInvariant,
        "falsify on content-hash mismatch; supersede on re-post");
    add("gateway", "post_check_result", E::EpochInvariant,
        "falsify on artifact or post-rule change; supersede on an identical-binding re-run");
    add("gateway", "gate_report", E::Reestablish,
        "falsify on any required evidence invalidation; supersede on re-evaluation");
    add("gateway", "procedural_fact", E::EpochInvariant,
        "falsify on failing re-run or parent invalidation; supersede with parent", false, true);
    add("gateway", "waiver", E::EpochInvariant,
        "falsify on explicit revocation; otherwise immutable and never invalidated");
    add("gateway", "simulation_conclusion", E::EpochInvariant,
        "falsify on simulation artifact mismatch; supersede on a newer conclusion");

    // Deterministic twin.
    add("twin", "twin_run", E::EpochInvariant,
        "falsify on bound-input mismatch; supersede on a newer run for identical bindings");
    add("twin", "twin_trace_verification", E::EpochInvariant,
        "falsify on trace or fidelity mismatch; supersede on a newer verification");
    add("twin", "twin_procedural_fact", E::EpochInvariant,
        "falsify on parent-run invalidation or fidelity downgrade; supersede with parent", false, true);
    add("twin", "twin_capability", E::EpochInvariant,
        "re-derive when cited evidence is falsified or superseded");

    // Metrology.
    add("metrology", "metrology.probe_def", E::EpochInvariant,
        "supersede on probe_def_upserted; falsify on probe_def_incompatible");
    add("metrology", "metrology.probe_result", E::EpochInvariant,
        "falsify on probe_result_retracted; supersede on probe_result_rerun");
    add("metrology", "metrology.observation", E::EpochInvariant,
        "falsify on observation_corrected; supersede on observation_superseded");
    add("metrology", "metrology.qualification", E::EpochInvariant,
        "falsify on qualification_withdrawn or qualification_expired; supersede on requalification");
    add("metrology", "metrology.calibration", E::EpochInvariant,
        "falsify on calibration_withdrawn; supersede on recalibration");
    add("metrology", "metrology.station_profile", E::EpochInvariant,
        "falsify on station_falsified; supersede on station_recalibrated");
    add("metrology", "metrology.transform_edge", E::EpochInvariant,
        "falsify on transform_falsified; supersede on transform_remeasured");
    add("metrology", "metrology.registration", E::EpochInvariant,
        "falsify on registration_falsified; supersede on registration_reprobed");
    add("metrology", "metrology.offset_proposal", E::EpochInvariant,
        "supersede on offset_record_superseded; falsify through cited evidence closure");
    add("metrology", "metrology.offset_approval", E::EpochInvariant,
        "falsify on approval_revoked; supersede on offset_record_superseded");
    add("metrology", "metrology.offset_apply", E::EpochInvariant,
        "falsify on readback mismatch, post-apply revoke, or stale offset; supersede on offset_record_superseded");
    add("metrology", "metrology.inspection_plan", E::EpochInvariant,
        "falsify on inspection_plan_incompatible; supersede on inspection_plan_revised");
    add("metrology", "metrology.inspection_result", E::EpochInvariant,
        "supersede on inspection_rerun; falsify through cited observation closure", false, true);
    add("metrology", "metrology.procedural_fact", E::EpochInvariant,
        "falsify on procedural_fact_rerun_failed or parent invalidation", false, true);
    add("metrology", "metrology.capability", E::EpochInvariant,
        "supersede on re-derivation; falsify through cited evidence closure");

    // Additive/print. Gateway's no-probed-domain rule makes these epoch invariant.
    add("additive", "print.slice_result", E::EpochInvariant,
        "falsify when the sidecar binary hash changes");
    add("additive", "print.artifact", E::EpochInvariant,
        "falsify on missing, moved, or hash-mismatched bytes; supersede on re-slice");
    add("additive", "print.procedural_fact", E::EpochInvariant,
        "falsify on failing re-run or profile/config change; supersede with artifact", false, true);
    add("additive", "print.preflight", E::EpochInvariant,
        "falsify on bound-input or live-evidence change; supersede on a new preflight", false, true);
    add("additive", "print.run_link", E::EpochInvariant,
        "none: immutable join of run id, artifact hash, and request hash");
    add("additive", "print.capability", E::EpochInvariant,
        "re-derive when any cited evidence row is falsified or superseded");
    add("additive", "print.media_source", E::EpochInvariant,
        "falsify on endpoint loss or insecure transport; supersede on device reconfiguration");

    // Predictive perception (advisory only).
    add("perception", "perception.event", E::EpochInvariant,
        "falsify on corrupt frames, identity/config/calibration/qualification invalidation; supersede on refinement or newer model/calibration");
    add("perception", "perception.evidence_clip", E::EpochInvariant,
        "falsify on content-hash or camera-identity mismatch; supersede on correction");
    add("perception", "perception.model_export", E::EpochInvariant,
        "falsify on parity, feature-spec, or load failure; supersede on newer export");
    add("perception", "perception.capability", E::EpochInvariant,
        "re-derive when any cited evidence row is falsified or superseded");

    // Read-only monitor is the only peer domain with probed transports.
    add("monitor", "monitor_observation", E::Falsify,
        "falsify on source loss; supersede on newer observation for the subject", true);
    add("monitor", "monitor_condition", E::Falsify,
        "falsify on source loss; supersede on newer condition transition", true);
    add("monitor", "monitor_source_state", E::Falsify, "falsify on disconnect or lane epoch change");
    add("monitor", "monitor_sequence_gap", E::EpochInvariant,
        "supersede only after a later contiguous run; never falsify");
    add("monitor", "monitor_capability", E::EpochInvariant,
        "re-derive on cited evidence, stage, profile, proof, or trust-state change");

    // Cutting.
    add("cut", "cam.cut.recipe", E::EpochInvariant,
        "falsify on value change; supersede on byte-identical revision bump");
    add("cut", "cam.cut.plan", E::EpochInvariant,
        "falsify on head, kerf, profile, setpoint, or geometry change; supersede with recipe", false, true);
    add("cut", "cam.cut.post", E::EpochInvariant,
        "falsify on dialect power-binding change; supersede with plan", false, true);
    add("cut", "cam.cut.parse", E::EpochInvariant,
        "falsify on parser-rule or artifact change; supersede with post", false, true);
    add("cut", "cam.cut.sim", E::EpochInvariant,
        "falsify on simulation-model change; supersede with plan", false, true);
    add("cut", "cam.cut.emulate", E::EpochInvariant,
        "falsify on binding change; supersede on higher stage or with post", false, true);
    add("cut", "cam.plasma.replay", E::EpochInvariant,
        "falsify on ledger hash change; supersede on re-run or with plan", false, true);
    add("cut", "cam.cut.capability", E::EpochInvariant,
        "re-derive when any cited evidence row is falsified or superseded");
    add("cut", "cam.cut.procedural_fact", E::EpochInvariant,
        "falsify with cited artifact; supersede with cited artifact", false, true);

    // Turning.
    add("turn", "cam.turn.profile", E::EpochInvariant,
        "falsify on part or source change; supersede on identical-hash re-extract");
    add("turn", "cam.turn.workholding", E::EpochInvariant,
        "falsify on setup deletion; supersede on new setup declaration");
    add("turn", "cam.turn.sim_remnant", E::EpochInvariant,
        "falsify on job/profile/workholding/tool change; supersede on identical re-run or with parent", false, true);
    add("turn", "cam.turn.procedural_fact", E::EpochInvariant,
        "falsify on artifact/config/snapshot change; supersede with parent", false, true);
    add("turn", "cam.turn.validation_report", E::EpochInvariant,
        "falsify on any binding change; supersede on identical re-run or with parent", false, true);
    add("turn", "cam.turn.capability", E::EpochInvariant,
        "re-derive on stage, dialect, or evidence change");

    // Pick-and-place.
    add("pnp", "pnp.board_revision", E::EpochInvariant,
        "supersede on re-import; falsify on source-hash change");
    add("pnp", "pnp.panel_definition", E::EpochInvariant, "supersede on array edit");
    add("pnp", "pnp.machine_profile", E::EpochInvariant, "supersede on revision bump");
    add("pnp", "pnp.feeder_assignment", E::EpochInvariant, "falsify on incompatibility");
    add("pnp", "pnp.fiducial_fit", E::EpochInvariant, "falsify on rejected re-fit");
    add("pnp", "pnp.job_artifact", E::EpochInvariant, "supersede on any bound-input change");
    add("pnp", "pnp.sim_result", E::EpochInvariant, "supersede with job artifact", false, true);
    add("pnp", "pnp.gate_row", E::EpochInvariant, "falsify with subject through derived_from closure");
    add("pnp", "pnp.export_artifact", E::EpochInvariant, "supersede with job artifact", false, true);
    add("pnp", "pnp.q1_procedural_fact", E::EpochInvariant,
        "falsify on failing re-run; supersede with parent", false, true);
    add("pnp", "pnp.capability", E::EpochInvariant,
        "re-derive when any cited evidence row is falsified or superseded");

    // Robot offline planning.
    add("robot", "robot_cell", E::EpochInvariant,
        "supersede on layout/config edit; falsify on referenced-asset hash change");
    add("robot", "robot_reach", E::EpochInvariant, "falsify with its cell through derived_from closure");
    add("robot", "robot_analysis", E::EpochInvariant,
        "supersede on a newer analysis for the same artifact and config epoch");
    add("robot", "robot_plan", E::EpochInvariant, "supersede on any bound-input change");
    add("robot", "robot_traj", E::EpochInvariant, "supersede on plan re-derivation");
    add("robot", "robot_mockrun", E::EpochInvariant, "supersede with trajectory", false, true);
    add("robot", "robot_procedural_fact", E::EpochInvariant,
        "falsify on failing re-run or with parent; supersede with parent", false, true);
    add("robot", "robot_capability", E::EpochInvariant,
        "re-derive on parent cell or cited evidence invalidation");

    return ResultRegistry(std::move(rows));
}

const ResultKindPolicy* ResultRegistry::find(std::string_view kind) const {
    const auto it = std::find_if(policies_.begin(), policies_.end(),
                                 [&](const auto& row) { return row.kind == kind; });
    return it == policies_.end() ? nullptr : &*it;
}

RegistryDecision ResultRegistry::validate(std::string_view kind) const {
    if (find(kind)) return {true, {}, {}};
    return {false, "mfg_unregistered_result_kind",
            "durable result kind is absent from the gateway registry: " + std::string(kind)};
}

bool ResultRegistry::coversAllManufacturingDomains() const {
    static constexpr std::array<std::string_view, 10> required = {
        "gateway", "twin", "metrology", "additive", "perception",
        "monitor", "cut", "turn", "pnp", "robot"};
    for (const auto domain : required) {
        const bool has_any = std::any_of(policies_.begin(), policies_.end(),
                                         [&](const auto& row) { return row.domain == domain; });
        const bool has_capability = std::any_of(policies_.begin(), policies_.end(), [&](const auto& row) {
            return row.domain == domain &&
                   (row.kind == "capability_report" || row.kind.ends_with("capability"));
        });
        if (!has_any || !has_capability) return false;
    }
    return true;
}

}  // namespace chudware::mfg
