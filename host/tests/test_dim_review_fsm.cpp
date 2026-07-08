// test_dim_review_fsm.cpp — ctest for dim_review accept/edit state-machine (RE-22).
// No GL context, no imgui, no rpc needed — tests the pure FSM transitions on DimRow.
//
// Compile:  see CMakeLists.txt (add_executable test_dim_review_fsm).
// Run:      ctest -R test_dim_review_fsm  (or ./test_dim_review_fsm directly)
//
// Acceptance per RE-22 task file:
//   ✓ Idle -> beginEdit -> Editing
//   ✓ Editing -> cancelEdit -> Idle
//   ✓ Editing -> commitEdit (valid buf) -> Pending, editValue updated
//   ✓ Editing -> commitEdit (empty buf)  -> Pending, editValue unchanged (keeps old)
//   ✓ Pending rows are eligible for Accept & Submit; Idle/Editing rows are not
//   ✓ tier colour logic (measured/estimated/guessed categorisation)

#include <cassert>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <algorithm>

// ─── Inline the FSM types (no GL / imgui dependency) ─────────────────────────

enum class RowState { Idle, Editing, Pending, Accepted, Rejected };

struct DimRow {
    std::string key;
    double      value_mm  = 0.0;
    std::string source;
    std::string tier;
    bool        required  = false;

    RowState    rowState  = RowState::Idle;
    double      editValue = 0.0;
    char        editBuf[32] = {};

    void beginEdit() {
        rowState  = RowState::Editing;
        editValue = value_mm;
        std::snprintf(editBuf, sizeof(editBuf), "%.4g", value_mm);
    }
    void cancelEdit() {
        rowState = RowState::Idle;
    }
    void commitEdit() {
        char* end = nullptr;
        double v = std::strtod(editBuf, &end);
        if (end && end != editBuf && v > 0.0) editValue = v;
        rowState = RowState::Pending;
    }
};

// Inline submit-eligibility helper (mirrors drawDimReviewPanel logic).
static bool canSubmit(const std::vector<DimRow>& rows) {
    bool anyPending = std::any_of(rows.begin(), rows.end(),
        [](const DimRow& r) { return r.rowState == RowState::Pending; });
    bool anyEditing = std::any_of(rows.begin(), rows.end(),
        [](const DimRow& r) { return r.rowState == RowState::Editing; });
    return anyPending && !anyEditing;
}

// Inline tier-colour heuristic (category only, not RGB).
static const char* tierCategory(const std::string& tier) {
    if (tier == "measured")  return "green";
    if (tier == "estimated") return "amber";
    return "red";
}

// ─── Tests ───────────────────────────────────────────────────────────────────

static int gPass = 0, gFail = 0;

#define CHECK(cond) do { \
    if (cond) { ++gPass; } \
    else { ++gFail; std::fprintf(stderr, "FAIL  %s:%d  %s\n", __FILE__, __LINE__, #cond); } \
} while(0)

static void test_idle_to_editing() {
    DimRow r;
    r.key      = "shaft.dia";
    r.value_mm = 8.0;
    r.tier     = "measured";
    CHECK(r.rowState == RowState::Idle);
    r.beginEdit();
    CHECK(r.rowState == RowState::Editing);
    CHECK(r.editValue == 8.0);
    // editBuf should contain a representation of 8.0
    double parsed = std::strtod(r.editBuf, nullptr);
    CHECK(parsed == 8.0);
}

static void test_editing_cancel() {
    DimRow r;
    r.value_mm = 5.0;
    r.beginEdit();
    CHECK(r.rowState == RowState::Editing);
    r.cancelEdit();
    CHECK(r.rowState == RowState::Idle);
    CHECK(r.editValue == 5.0);  // not reset — only rowState reverts
}

static void test_editing_commit_valid() {
    DimRow r;
    r.value_mm = 8.0;
    r.beginEdit();
    // User types a new value in editBuf.
    std::strncpy(r.editBuf, "12.5", sizeof(r.editBuf));
    r.commitEdit();
    CHECK(r.rowState == RowState::Pending);
    CHECK(r.editValue == 12.5);
}

static void test_editing_commit_empty_buf() {
    DimRow r;
    r.value_mm = 8.0;
    r.beginEdit();
    r.editBuf[0] = '\0';  // simulate empty field
    r.commitEdit();
    CHECK(r.rowState == RowState::Pending);
    // editValue should remain the previous value (8.0) because strtod parse fails.
    CHECK(r.editValue == 8.0);
}

static void test_editing_commit_negative() {
    // Negative values rejected (v > 0.0 guard) — editValue stays unchanged.
    DimRow r;
    r.value_mm = 5.0;
    r.beginEdit();
    std::strncpy(r.editBuf, "-3.0", sizeof(r.editBuf));
    r.commitEdit();
    CHECK(r.rowState == RowState::Pending);
    CHECK(r.editValue == 5.0);  // unchanged
}

static void test_submit_eligibility() {
    std::vector<DimRow> rows(3);
    rows[0].value_mm = 8.0;
    rows[1].value_mm = 18.0;
    rows[2].value_mm = 2.0;
    // All idle — not eligible.
    CHECK(!canSubmit(rows));

    // One editing, none pending — not eligible.
    rows[0].beginEdit();
    CHECK(!canSubmit(rows));

    // Commit row 0 → Pending; row 1 still idle — eligible.
    rows[0].commitEdit();
    CHECK(canSubmit(rows));

    // Begin editing row 1 — blocks submit.
    rows[1].beginEdit();
    CHECK(!canSubmit(rows));

    // Cancel edit — eligible again.
    rows[1].cancelEdit();
    CHECK(canSubmit(rows));
}

static void test_accepted_state() {
    DimRow r;
    r.value_mm = 8.0;
    r.beginEdit();
    std::strncpy(r.editBuf, "9.0", sizeof(r.editBuf));
    r.commitEdit();
    CHECK(r.rowState == RowState::Pending);
    // Simulate submit acceptance.
    r.rowState = RowState::Accepted;
    CHECK(r.rowState == RowState::Accepted);

    // Accepted rows don't block further submits (no Editing state).
    std::vector<DimRow> rows = {r};
    DimRow r2; r2.value_mm = 5.0; r2.beginEdit(); r2.commitEdit();
    rows.push_back(r2);
    CHECK(canSubmit(rows));  // one Pending, no Editing
}

static void test_tier_colours() {
    CHECK(std::string(tierCategory("measured"))  == "green");
    CHECK(std::string(tierCategory("estimated")) == "amber");
    CHECK(std::string(tierCategory("guessed"))   == "red");
    CHECK(std::string(tierCategory("unknown"))   == "red");
}

static void test_multi_row_all_accepted() {
    // Once all rows are Accepted, canSubmit is false (none Pending).
    std::vector<DimRow> rows(2);
    rows[0].rowState = RowState::Accepted;
    rows[1].rowState = RowState::Accepted;
    CHECK(!canSubmit(rows));
}

static void test_required_flag_preserved() {
    DimRow r;
    r.key      = "mount.spacing";
    r.value_mm = 18.0;
    r.required = true;
    r.tier     = "measured";
    r.beginEdit();
    CHECK(r.required == true);
    r.cancelEdit();
    CHECK(r.required == true);
}

// ─── main ────────────────────────────────────────────────────────────────────

int main() {
    test_idle_to_editing();
    test_editing_cancel();
    test_editing_commit_valid();
    test_editing_commit_empty_buf();
    test_editing_commit_negative();
    test_submit_eligibility();
    test_accepted_state();
    test_tier_colours();
    test_multi_row_all_accepted();
    test_required_flag_preserved();

    std::fprintf(stdout, "dim_review_fsm: %d passed, %d failed\n", gPass, gFail);
    return (gFail == 0) ? 0 : 1;
}
