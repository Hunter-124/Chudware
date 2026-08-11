#include "util/entitlement.h"

#include <array>
#include <cstdint>
#include <iostream>
#include <string>

int main() {
    using namespace chudware::entitlement;
    constexpr std::int64_t day = 86400;
    constexpr std::int64_t now = 1'800'000'000;
    int failures = 0;
    auto check = [&failures](bool condition, const char* label) {
        if (!condition) {
            std::cerr << "FAIL: " << label << '\n';
            ++failures;
        }
    };

    check(graceDaysFor(now, now) == 7, "zero-day grace floor");
    check(graceDaysFor(now, now - 400 * day) == 20, "400-day linear grace");
    check(graceDaysFor(now, now - 689 * day) == 29, "day before grace ceiling");
    // 690 days is the first day the 30-day ceiling binds.
    check(graceDaysFor(now, now - 690 * day) == 30, "grace ceiling boundary");
    check(graceDaysFor(now, now - 5000 * day) == 30, "grace ceiling holds");

    TokenPayload payload;
    payload.graceDays = 30;
    payload.paidThrough = now + day;
    check(graceDeadline(payload, now) == payload.paidThrough + 3 * day,
          "paid-through clamp dominates tenure");

    check(classifyRefresh(401, false, false) == RefreshOutcome::Unreachable,
          "unsigned 401 applies grace");
    check(classifyRefresh(200, false, true) == RefreshOutcome::Refused,
          "signed 200 refusal is immediate");
    check(classifyRefresh(200, true, false) == RefreshOutcome::Fresh,
          "verified fresh token succeeds");
    check(classifyRefresh(0, false, false) == RefreshOutcome::Unreachable,
          "blackholed transport applies grace");
    payload.paidThrough = now + 100 * day;
    CheckinStamp stamp{now, 10'000.0};
    auto beforeChip = offlineAccess(payload, stamp, now + 26 * day,
                                    10'000.0 + 26 * day);
    check(beforeChip.access == OfflineAccess::Allowed,
          "unreachable check has no chip before final three days");
    auto finalThreeDays = offlineAccess(payload, stamp, now + 28 * day,
                                        10'000.0 + 28 * day);
    check(finalThreeDays.access == OfflineAccess::Expiring,
          "unreachable check shows chip only in final three days");

    auto rollback = offlineAccess(payload, stamp, now - 3700, 10'100.0);
    check(rollback.access == OfflineAccess::ClockRollback, "clock rollback expires grace");

    const std::array<unsigned char, 32> vectorPublic = {
        0x03, 0xa1, 0x07, 0xbf, 0xf3, 0xce, 0x10, 0xbe,
        0x1d, 0x70, 0xdd, 0x18, 0xe7, 0x4b, 0xc0, 0x99,
        0x67, 0xe4, 0xd6, 0x30, 0x9b, 0xa5, 0x0d, 0x5f,
        0x1d, 0xdc, 0x86, 0x64, 0x12, 0x55, 0x31, 0xb8,
    };
    std::string signedToken =
        "eyJlbWFpbCI6InRlc3RAZXhhbXBsZS5jb20iLCJncmFjZV9kYXlzIjo3LCJpc3N1ZWRfYXQiOjE4MDAwMDAwMDAsIm5vbmNlIjoiQUFBQUFBQUFBQUFBQUFBQUFBQUFBQSIsInBhaWRfdGhyb3VnaCI6MTgwMDg2NDAwMCwicGxhbiI6ImRlc2t0b3AiLCJzdWIiOiIyNjljMjk3Ny0xNTZmLTRlZjMtODc0MC1jZDYyZmM1ODY2NzciLCJzdWJzY3JpYmVkX3NpbmNlIjoxNzk5MTM2MDAwLCJ2IjoxfQ."
        "MuHbclU4aJMwsAHVgoOkD-Ipa00CGyYqqxSyCn3Dbv03h7j1ZoiY5RbKNDfd6e0k1Y20yTQT-qr31qs5T2qjCg";
    TokenPayload verified;
    check(verifyToken(signedToken, vectorPublic, verified), "valid signed token");
    signedToken[12] = signedToken[12] == 'A' ? 'B' : 'A';
    check(!verifyToken(signedToken, vectorPublic, verified),
          "one-byte token tamper re-prompts without a crash");

    const std::string refusal =
        R"({"entitled":false,"issued_at":1800000000,"nonce":"refusal-nonce","proof":"eyJlbnRpdGxlZCI6ZmFsc2UsImlzc3VlZF9hdCI6MTgwMDAwMDAwMCwibm9uY2UiOiJyZWZ1c2FsLW5vbmNlIiwicmVhc29uIjoiaW5hY3RpdmUiLCJyZXF1ZXN0X25vbmNlIjoicmVxdWVzdC1ub25jZSJ9.dTwtD_s-ykubscXrAgcD527suZORGIFs-BWICJnBn7sDfo9-htay7i21-qgRx7qV-pH1240xwRYwOU8iOGJpBw","reason":"inactive","request_nonce":"request-nonce"})";
    std::string refusalReason;
    check(verifyRefusal(refusal, vectorPublic, "request-nonce", now,
                        &refusalReason) && refusalReason == "inactive",
          "signed refusal causes immediate read-only");
    check(!verifyRefusal(refusal, vectorPublic, "new-request-nonce", now + 301),
          "old signed refusal cannot replay against a new challenge");

    const auto key = deviceKey("test-device\nuser\n/data");
    const std::string token = "compact.token.value";
    auto sealed = sealForDevice(token, key);
    check(!sealed.empty(), "AEAD seal");
    std::string opened;
    check(openForDevice(sealed, key, opened) && opened == token, "AEAD open");
    sealed.back() ^= 1;
    check(!openForDevice(sealed, key, opened), "AEAD tamper rejection");

    if (failures) return 1;
    std::cout << "entitlement arithmetic: PASS\n";
    return 0;
}
