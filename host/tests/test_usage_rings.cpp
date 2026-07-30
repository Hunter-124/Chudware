// test_usage_rings.cpp — ui::pickUsageRings, the composer prelude row's ring-selection rule.
//
// Guards the "cluttered" defect the user reported against the live app: the row rendered
// "TOT (ring) 5H (ring) 1k/1.0M" — a windowless lifetime-total ring wedged between the rolling
// window and the context gauge, three 3-char captions of identical weight in a ~90dp slot.
//
// The rule under test: rolling windows are the actionable numbers and win; a lifetime total is
// shown ONLY when there is no rolling window at all; status-only windows (no fraction) never
// render, because a missing fraction is not 0%.
//
// GL-free: links nothing but the header (pickUsageRings is inline), so it is a real test of the
// shipped rule rather than a copy of it.
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>

#include "ui/provider_usage.h"

namespace {

ui::UsageWindow win(const char* window, const char* label, float frac, bool hasFrac = true) {
    ui::UsageWindow w;
    w.window = window;
    w.label = label;
    w.usedFraction = frac;
    w.hasFraction = hasFrac;
    return w;
}

std::vector<std::string> ids(const std::vector<const ui::UsageWindow*>& v) {
    std::vector<std::string> out;
    for (const ui::UsageWindow* w : v) out.push_back(w->window.empty() ? w->label : w->window);
    return out;
}

// The exact shape behind the screenshot: kimi-code reports a windowless "Total quota" FIRST,
// then the rolling 5h window. Catalog order used to decide, so "TOT" won a ring and pushed the
// row to three elements.
void test_lifetime_total_never_rides_beside_a_rolling_window() {
    const std::vector<ui::UsageWindow> wins = {
        win("", "Total quota", 0.25f),
        win("5h", "5h limit", 0.60f),
    };
    const auto got = ids(ui::pickUsageRings(wins, 2));
    assert(got.size() == 1);
    assert(got[0] == "5h");
}

void test_two_rolling_windows_both_show_in_order() {
    const std::vector<ui::UsageWindow> wins = {
        win("", "Total quota", 0.25f),
        win("5h", "5h limit", 0.60f),
        win("7d", "7d limit", 0.30f),
    };
    const auto got = ids(ui::pickUsageRings(wins, 2));
    assert(got.size() == 2);
    assert(got[0] == "5h");
    assert(got[1] == "7d");
}

// The ceiling is real: a provider reporting many windows must not reflow the row.
void test_never_exceeds_the_cap() {
    const std::vector<ui::UsageWindow> wins = {
        win("1h", "1h", 0.1f), win("5h", "5h", 0.2f),
        win("7d", "7d", 0.3f), win("30d", "30d", 0.4f),
    };
    assert(ui::pickUsageRings(wins, 2).size() == 2);
    assert(ui::pickUsageRings(wins, 1).size() == 1);
    assert(ui::pickUsageRings(wins, 0).empty());
}

// A lifetime total is still better than an empty row — it just never competes.
void test_lifetime_total_is_the_fallback_when_alone() {
    const std::vector<ui::UsageWindow> wins = {win("", "Total quota", 0.25f)};
    const auto got = ids(ui::pickUsageRings(wins, 2));
    assert(got.size() == 1);
    assert(got[0] == "Total quota");
}

// "Consumers MUST hide their indicator when there is nothing to show — never render 0%."
void test_status_only_windows_never_render() {
    const std::vector<ui::UsageWindow> wins = {
        win("5h", "5h limit", 0.0f, /*hasFrac=*/false),
        win("", "Total quota", 0.0f, /*hasFrac=*/false),
    };
    assert(ui::pickUsageRings(wins, 2).empty());
    assert(ui::pickUsageRings({}, 2).empty());
}

// A status-only rolling window must not shadow a usable one behind it.
void test_status_only_rolling_window_is_skipped_not_counted() {
    const std::vector<ui::UsageWindow> wins = {
        win("1h", "1h", 0.0f, /*hasFrac=*/false),
        win("5h", "5h", 0.6f),
    };
    const auto got = ids(ui::pickUsageRings(wins, 1));
    assert(got.size() == 1);
    assert(got[0] == "5h");
}

}  // namespace

int main() {
    test_lifetime_total_never_rides_beside_a_rolling_window();
    test_two_rolling_windows_both_show_in_order();
    test_never_exceeds_the_cap();
    test_lifetime_total_is_the_fallback_when_alone();
    test_status_only_windows_never_render();
    test_status_only_rolling_window_is_skipped_not_counted();
    std::printf("test_usage_rings: all passed\n");
    return 0;
}
