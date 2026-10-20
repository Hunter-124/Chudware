#include "ui/ribbon_def.h"
#include "ui/account_panel.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include <thread>

namespace {

using ui::account_detail::Operation;
using ui::account_detail::PendingResult;
using ui::account_detail::ResultMailbox;

// Disposable storage sink for the production mailbox's UI-owner commit boundary.
// No signed entitlement, endpoint, secret_store or real account is used in this test.
struct AccountFixture {
    std::filesystem::path root;
    bool signedIn = false;
    std::string token;
    std::string status;
    unsigned commits = 0;

    AccountFixture() {
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        root = std::filesystem::temp_directory_path() /
            ("chudware-entitlement-generation-" + std::to_string(stamp));
        if (!std::filesystem::create_directory(root))
            throw std::runtime_error("could not create isolated entitlement fixture");
    }

    ~AccountFixture() {
        std::error_code error;
        std::filesystem::remove_all(root, error);
    }

    void put(const char* name, const std::string& value) {
        std::ofstream file(root / name, std::ios::binary | std::ios::trunc);
        file << value;
        if (!file) throw std::runtime_error("fixture storage write failed");
    }

    std::string get(const char* name) const {
        std::ifstream file(root / name, std::ios::binary);
        return {std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
    }

    void signOut(ResultMailbox& mailbox) {
        mailbox.invalidate();
        for (const char* name : {"token", "subject", "stamp", "policy", "enforcement", "candidate"})
            std::filesystem::remove(root / name);
        token.clear();
        signedIn = false;
        status = "signed out";
    }

    bool apply(ResultMailbox& mailbox) {
        return mailbox.consume([&](PendingResult result) {
            ++commits;
            status = result.detail;
            if (!result.acknowledgedEnforcement.empty()) {
                put("enforcement", result.acknowledgedEnforcement);
                put("queue", result.batchId + ":" + std::to_string(result.batchSequence));
            }
            if (result.kind == PendingResult::Kind::Fresh) {
                token = result.token;
                signedIn = true;
                put("token", token);
                put("subject", token + "-subject");
                put("stamp", std::to_string(result.generation));
                put("policy", result.securityPolicy);
                put("enforcement", result.enforcementState);
                if (result.operation == Operation::Login) put("identity", get("candidate"));
            }
        });
    }
};

PendingResult success(std::uint64_t generation, const std::string& token) {
    PendingResult result{PendingResult::Kind::Fresh, token, {},
                         token + "-policy", token + "-enforcement"};
    result.generation = generation;
    result.acknowledgedEnforcement = token + "-ack";
    result.batchId = token + "-batch";
    result.batchSequence = generation;
    return result;
}

// Explicit release barriers make old-response ordering deterministic, without sleeps.
struct DelayedResponse {
    std::promise<void> release;
    std::thread worker;
    bool accepted = false;

    DelayedResponse(ResultMailbox& mailbox, PendingResult result) {
        std::promise<void> started;
        auto ready = started.get_future();
        auto response = release.get_future();
        worker = std::thread([this, &mailbox, result = std::move(result),
                              started = std::move(started), response = std::move(response)]() mutable {
            started.set_value();
            response.wait();
            accepted = mailbox.complete(std::move(result));
        });
        ready.wait();
    }

    void finish() {
        release.set_value();
        worker.join();
    }
};

template <typename Check>
void generationRegression(Check check) {
    ResultMailbox mailbox;
    AccountFixture fixture;
    fixture.put("queue", "unacknowledged");

    const auto refresh = mailbox.begin(Operation::Refresh);
    DelayedResponse oldRefresh(mailbox, success(refresh, "old-refresh"));
    fixture.signOut(mailbox);
    oldRefresh.finish();
    check(!oldRefresh.accepted && !fixture.apply(mailbox),
          "refresh success delayed past signout never reaches the UI commit");
    check(!fixture.signedIn && fixture.token.empty() &&
          !std::filesystem::exists(fixture.root / "token") &&
          !std::filesystem::exists(fixture.root / "subject") &&
          !std::filesystem::exists(fixture.root / "stamp") &&
          !std::filesystem::exists(fixture.root / "policy") &&
          !std::filesystem::exists(fixture.root / "enforcement") &&
          fixture.get("queue") == "unacknowledged",
          "old refresh cannot resurrect credentials, stamp, policy, enforcement or drain queue");

    const auto firstLogin = mailbox.begin(Operation::Login);
    fixture.put("candidate", "old-identity");
    DelayedResponse oldLogin(mailbox, success(firstLogin, "old-login"));
    fixture.signOut(mailbox);
    const auto newLogin = mailbox.begin(Operation::Login);
    fixture.put("candidate", "new-identity");
    fixture.status = "waiting for new authorization";
    oldLogin.finish();
    check(!oldLogin.accepted && mailbox.running() == Operation::Login &&
          !fixture.apply(mailbox) && fixture.status == "waiting for new authorization" &&
          fixture.get("candidate") == "new-identity" && fixture.get("queue") == "unacknowledged",
          "old login leaves newer login progress, candidate and security queue intact");
    check(mailbox.complete(success(newLogin, "new-login")) && fixture.apply(mailbox),
          "current login success reaches the commit");
    check(fixture.signedIn && fixture.token == "new-login" &&
          fixture.get("token") == "new-login" && fixture.get("subject") == "new-login-subject" &&
          fixture.get("stamp") == std::to_string(newLogin) &&
          fixture.get("policy") == "new-login-policy" &&
          fixture.get("enforcement") == "new-login-enforcement" &&
          fixture.get("identity") == "new-identity" &&
          fixture.get("queue") == "new-login-batch:" + std::to_string(newLogin),
          "current success persists only new credentials, identity, stamp and queue acknowledgement");
    check(!mailbox.complete(success(firstLogin, "late-again")) && !fixture.apply(mailbox) &&
          fixture.get("token") == "new-login" && fixture.get("identity") == "new-identity",
          "stale completion after new success cannot overwrite new credentials");

    const auto overlappingRefresh = mailbox.begin(Operation::Refresh);
    DelayedResponse superseded(mailbox, success(overlappingRefresh, "superseded-refresh"));
    const auto overlappingLogin = mailbox.begin(Operation::Login);
    fixture.put("candidate", "latest-identity");
    check(overlappingLogin != 0 && overlappingLogin != overlappingRefresh,
          "new login supersedes running refresh without waiting for its response");
    check(mailbox.complete(success(overlappingLogin, "latest-login")),
          "newer success can be queued while old refresh is delayed");
    superseded.finish();
    check(!superseded.accepted && fixture.apply(mailbox) && fixture.token == "latest-login" &&
          fixture.get("identity") == "latest-identity",
          "old refresh cannot replace an already queued newer success");

    const auto queuedRefresh = mailbox.begin(Operation::Refresh);
    check(mailbox.complete(success(queuedRefresh, "queued-before-signout")),
          "current refresh can complete before UI consumption");
    fixture.signOut(mailbox);
    check(!fixture.apply(mailbox) && !fixture.signedIn &&
          !std::filesystem::exists(fixture.root / "token"),
          "signout also invalidates current success queued before UI consumption");

    const auto errorGeneration = mailbox.begin(Operation::Refresh);
    PendingResult failure{PendingResult::Kind::Invalid, {}, "current failure"};
    failure.generation = errorGeneration;
    check(mailbox.complete(std::move(failure)) && fixture.apply(mailbox) &&
          fixture.status == "current failure",
          "current errors are delivered rather than silently treated as stale");
}

}  // namespace

int main() {
    using ui::CommandCapability;
    int failures = 0;
    auto check = [&failures](bool condition, const char* label) {
        if (!condition) {
            std::cerr << "FAIL: " << label << '\n';
            ++failures;
        }
    };
    check(ui::ribbonCommandCapability("extrude_boss") == CommandCapability::Mutate,
          "edit command is mutating");
    check(ui::ribbonCommandCapability("new_command_added_later") == CommandCapability::Mutate,
          "unknown command fails closed");
    check(ui::ribbonCommandCapability("eval_measure") == CommandCapability::Inspect,
          "measure remains enabled");
    check(ui::ribbonCommandCapability("display_wireframe") == CommandCapability::Inspect,
          "view remains enabled");
    check(ui::ribbonCommandCapability("sm_export_flat_dxf") == CommandCapability::Export,
          "flat export remains enabled");
    check(ui::ribbonCommandCapability("dwg_export_pdf") == CommandCapability::Export,
          "drawing export remains enabled");
    generationRegression(check);
    if (failures) return 1;
    std::cout << "entitlement commands and delayed account generations: PASS\n";
    return 0;
}
