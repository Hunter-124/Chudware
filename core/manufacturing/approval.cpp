#include "manufacturing/approval.h"

namespace chudware::mfg {
namespace {

std::optional<Falsification> changed(bool condition, std::string reason, std::string why) {
    if (condition) return Falsification{std::move(reason), std::move(why)};
    return std::nullopt;
}

}  // namespace

std::optional<Falsification> falsification_against(const ApprovalBinding& binding,
                                                   const ApprovalInputs& current,
                                                   uint64_t now_mono_ns) {
    if (auto value = changed(binding.machine.device_id != current.machine.device_id,
                             "machine_identity_changed",
                             "bound device identity differs from the live device")) return value;
    if (auto value = changed(binding.machine.fingerprint_sha256 != current.machine.fingerprint_sha256,
                             "machine_fingerprint_changed",
                             "live machine fingerprint differs from approval")) return value;
    if (auto value = changed(binding.artifact.hash_sha256 != current.artifact.hash_sha256,
                             "artifact_rehashed", "artifact hash differs from approval")) return value;
    if (auto value = changed(binding.artifact.bytes != current.artifact.bytes ||
                             binding.artifact.kind != current.artifact.kind,
                             "artifact_identity_changed", "artifact bytes or kind differs from approval")) return value;
    if (auto value = changed(binding.artifact.post_profile_id != current.artifact.post_profile_id ||
                             binding.artifact.post_profile_hash != current.artifact.post_profile_hash,
                             "post_profile_changed", "post profile binding differs from approval")) return value;
    if (auto value = changed(binding.artifact.slicer_profile_hash != current.artifact.slicer_profile_hash,
                             "slicer_profile_changed", "slicer profile binding differs from approval")) return value;
    if (auto value = changed(binding.job.job_id != current.job.job_id ||
                             binding.job.setup_revision != current.job.setup_revision ||
                             binding.job.setup_revision_seq != current.job.setup_revision_seq ||
                             binding.job.quantity != current.job.quantity,
                             "job_or_setup_revision_changed",
                             "job, setup revision, or quantity differs from approval")) return value;
    if (auto value = changed(binding.snapshot.tool_table_hash != current.snapshot.tool_table_hash,
                             "tool_table_changed", "tool table differs from approval")) return value;
    if (auto value = changed(binding.snapshot.fixture_hash != current.snapshot.fixture_hash,
                             "fixture_changed", "fixture binding differs from approval")) return value;
    if (auto value = changed(binding.snapshot.wcs.system != current.snapshot.wcs.system ||
                             binding.snapshot.wcs.offset != current.snapshot.wcs.offset ||
                             binding.snapshot.wcs.established_by != current.snapshot.wcs.established_by,
                             "wcs_changed", "WCS binding differs from approval")) return value;
    if (auto value = changed(binding.preflight.evidence != current.preflight.evidence ||
                             binding.preflight.gate_report != current.preflight.gate_report ||
                             binding.preflight.established_at_seq != current.preflight.established_at_seq,
                             "preflight_evidence_changed",
                             "preflight evidence set differs from approval")) return value;
    if (auto value = changed(binding.policy_revision != current.policy_revision,
                             "machine_class_policy_changed",
                             "commissioning policy revision differs from approval")) return value;
    if (auto value = changed(!current.attendance_attestation_active,
                             "attendance_attestation_withdrawn",
                             "approval attendance attestation is no longer active")) return value;
    if (auto value = changed(now_mono_ns >= binding.expiry.t_mono_ns,
                             "approval_expired", "approval expiry has been reached")) return value;
    if (auto value = changed(binding.max_starts != 1,
                             "invalid_max_starts", "approval max_starts must equal one")) return value;
    if (auto value = changed(binding.nonce.state != NonceState::Unburned,
                             "approval_nonce_not_unburned",
                             "approval nonce has already been burned or voided")) return value;
    return std::nullopt;
}

bool NonceLedger::register_approval(const ApprovalBinding& binding, std::string* why) {
    std::lock_guard lock(mutex_);
    if (binding.approval_id.empty() || binding.nonce.value.empty()) {
        if (why) *why = "approval id and nonce are required";
        return false;
    }
    if (binding.max_starts != 1 || binding.nonce.state != NonceState::Unburned) {
        if (why) *why = "nonce ledger accepts only unburned max_starts=1 approvals";
        return false;
    }
    if (rows_.contains(binding.approval_id)) {
        if (why) *why = "approval is already registered";
        return false;
    }
    rows_.emplace(binding.approval_id, binding.nonce);
    return true;
}

NonceBurnResult NonceLedger::burn(std::string_view approval_id, std::string_view expected_nonce,
                                  device::Seq at_seq) {
    std::lock_guard lock(mutex_);
    const auto it = rows_.find(approval_id);
    if (it == rows_.end())
        return {false, "nonce.approval_not_registered", "approval nonce is not registered", std::nullopt};
    if (it->second.value != expected_nonce)
        return {false, "nonce.value_mismatch", "presented nonce does not match approval", std::nullopt};
    if (it->second.state == NonceState::Burned)
        return {false, "nonce.replay_refused", "approval nonce has already been burned",
                it->second.burned_at_seq};
    if (it->second.state == NonceState::Void)
        return {false, "nonce.void", "approval nonce was voided", std::nullopt};
    if (at_seq == 0)
        return {false, "nonce.invalid_burn_seq", "nonce burn requires a durable nonzero seq", std::nullopt};
    it->second.state = NonceState::Burned;
    it->second.burned_at_seq = at_seq;
    return {true, {}, {}, at_seq};
}

NonceBurnResult NonceLedger::burn_durably(
        std::string_view approval_id, std::string_view expected_nonce,
        const std::function<device::Seq()>& durable_append) {
    std::lock_guard lock(mutex_);
    const auto it = rows_.find(approval_id);
    if (it == rows_.end())
        return {false, "nonce.approval_not_registered", "approval nonce is not registered", std::nullopt};
    if (it->second.value != expected_nonce)
        return {false, "nonce.value_mismatch", "presented nonce does not match approval", std::nullopt};
    if (it->second.state == NonceState::Burned)
        return {false, "nonce.replay_refused", "approval nonce has already been burned",
                it->second.burned_at_seq};
    if (it->second.state == NonceState::Void)
        return {false, "nonce.void", "approval nonce was voided", std::nullopt};
    const auto at_seq = durable_append();
    if (at_seq == 0)
        return {false, "nonce.invalid_burn_seq", "durable append returned seq zero", std::nullopt};
    it->second.state = NonceState::Burned;
    it->second.burned_at_seq = at_seq;
    return {true, {}, {}, at_seq};
}

std::optional<ApprovalNonce> NonceLedger::describe(std::string_view approval_id) const {
    std::lock_guard lock(mutex_);
    const auto it = rows_.find(approval_id);
    if (it == rows_.end()) return std::nullopt;
    return it->second;
}

}  // namespace chudware::mfg
