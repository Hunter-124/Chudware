#pragma once

#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace chudware::mfg {

enum class EpochPolicy { Falsify, Reestablish, EpochInvariant };

std::string_view to_string(EpochPolicy value);

struct ResultKindPolicy {
    std::string domain;
    std::string kind;
    EpochPolicy epoch_policy = EpochPolicy::Falsify;
    std::string runtime_invalidation_triggers;
    bool retain_as_last_ok = false;
    bool supersede_with_parent = false;
};

struct RegistryDecision {
    bool accepted = false;
    std::string code;
    std::string why;
};

class ResultRegistry {
public:
    ResultRegistry();
    explicit ResultRegistry(std::vector<ResultKindPolicy> policies);

    static ResultRegistry manufacturingDefaults();

    std::span<const ResultKindPolicy> all() const { return policies_; }
    const ResultKindPolicy* find(std::string_view kind) const;
    RegistryDecision validate(std::string_view kind) const;
    bool coversAllManufacturingDomains() const;

private:
    std::vector<ResultKindPolicy> policies_;
};

}  // namespace chudware::mfg
