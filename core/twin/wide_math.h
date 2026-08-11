#pragma once

#include <bit>
#include <cctype>
#include <cstdint>
#include <limits>
#include <optional>
#include <stdexcept>
#include <string_view>
#include <vector>

#if defined(_MSC_VER) && !defined(__clang__)
#include <intrin.h>
#endif

namespace chudware::twin::wide_math {
namespace detail {

struct U128 {
    std::uint64_t hi = 0;
    std::uint64_t lo = 0;
};

inline U128 multiply(std::uint64_t lhs, std::uint64_t rhs) noexcept {
#if defined(_MSC_VER) && !defined(__clang__)
    U128 result;
    result.lo = _umul128(lhs, rhs, &result.hi);
    return result;
#else
    const unsigned __int128 result = static_cast<unsigned __int128>(lhs) * rhs;
    return {static_cast<std::uint64_t>(result >> 64), static_cast<std::uint64_t>(result)};
#endif
}

inline int compare(U128 lhs, U128 rhs) noexcept {
    if (lhs.hi != rhs.hi) return lhs.hi < rhs.hi ? -1 : 1;
    if (lhs.lo != rhs.lo) return lhs.lo < rhs.lo ? -1 : 1;
    return 0;
}

inline bool add(U128& lhs, U128 rhs) noexcept {
    const std::uint64_t old_lo = lhs.lo;
    lhs.lo += rhs.lo;
    const std::uint64_t carry = lhs.lo < old_lo ? 1 : 0;
    const std::uint64_t old_hi = lhs.hi;
    lhs.hi += rhs.hi;
    const bool overflow = lhs.hi < old_hi;
    const std::uint64_t before_carry = lhs.hi;
    lhs.hi += carry;
    return overflow || lhs.hi < before_carry;
}

inline U128 subtract(U128 lhs, U128 rhs) noexcept {
    const std::uint64_t borrow = lhs.lo < rhs.lo ? 1 : 0;
    lhs.lo -= rhs.lo;
    lhs.hi = lhs.hi - rhs.hi - borrow;
    return lhs;
}

inline U128 shiftLeft(U128 value) noexcept {
    return {(value.hi << 1) | (value.lo >> 63), value.lo << 1};
}

inline bool bit(U128 value, unsigned index) noexcept {
    return index < 64 ? ((value.lo >> index) & 1u) != 0
                      : ((value.hi >> (index - 64)) & 1u) != 0;
}

inline void setBit(U128& value, unsigned index) noexcept {
    if (index < 64) value.lo |= std::uint64_t{1} << index;
    else value.hi |= std::uint64_t{1} << (index - 64);
}

struct DivResult {
    U128 quotient;
    U128 remainder;
};

inline DivResult divide(U128 numerator, U128 denominator) {
    if (denominator.hi == 0 && denominator.lo == 0)
        throw std::invalid_argument("wide division by zero");
#if defined(_MSC_VER) && !defined(__clang__)
    if (denominator.hi == 0 && numerator.hi < denominator.lo) {
        DivResult result;
        result.quotient.lo = _udiv128(numerator.hi, numerator.lo, denominator.lo,
                                      &result.remainder.lo);
        return result;
    }
#else
    const unsigned __int128 n = (static_cast<unsigned __int128>(numerator.hi) << 64) |
                                numerator.lo;
    const unsigned __int128 d = (static_cast<unsigned __int128>(denominator.hi) << 64) |
                                denominator.lo;
    const unsigned __int128 q = n / d;
    const unsigned __int128 r = n % d;
    return {{static_cast<std::uint64_t>(q >> 64), static_cast<std::uint64_t>(q)},
            {static_cast<std::uint64_t>(r >> 64), static_cast<std::uint64_t>(r)}};
#endif

    // `_udiv128` has a 64-bit divisor and traps when the quotient does not fit.
    // A full-width denominator is rare here, so use the standard 128-step binary
    // divide for that case. Multiplication still uses MSVC's widening intrinsic.
    DivResult result;
    for (int index = 127; index >= 0; --index) {
        const bool overflow = (result.remainder.hi >> 63) != 0;
        result.remainder = shiftLeft(result.remainder);
        result.remainder.lo |= bit(numerator, static_cast<unsigned>(index)) ? 1u : 0u;
        if (overflow || compare(result.remainder, denominator) >= 0) {
            result.remainder = subtract(result.remainder, denominator);
            setBit(result.quotient, static_cast<unsigned>(index));
        }
    }
    return result;
}

inline std::uint64_t ordered(std::int64_t value) noexcept {
    return std::bit_cast<std::uint64_t>(value) ^ (std::uint64_t{1} << 63);
}

inline std::int64_t fromOrdered(std::uint64_t value) noexcept {
    return std::bit_cast<std::int64_t>(value ^ (std::uint64_t{1} << 63));
}

inline std::uint64_t distance(std::int64_t lhs, std::int64_t rhs) noexcept {
    const std::uint64_t a = ordered(lhs);
    const std::uint64_t b = ordered(rhs);
    return a >= b ? a - b : b - a;
}

inline std::int64_t signedMagnitude(std::uint64_t magnitude, bool negative) {
    constexpr std::uint64_t kNegativeLimit = std::uint64_t{1} << 63;
    if (negative) {
        if (magnitude > kNegativeLimit) throw std::overflow_error("wide result is below int64");
        if (magnitude == kNegativeLimit) return std::numeric_limits<std::int64_t>::min();
        return -static_cast<std::int64_t>(magnitude);
    }
    if (magnitude > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
        throw std::overflow_error("wide result is above int64");
    return static_cast<std::int64_t>(magnitude);
}

inline std::uint64_t quotient64(U128 numerator, std::uint64_t denominator) {
    const DivResult result = divide(numerator, {0, denominator});
    if (result.quotient.hi != 0) throw std::overflow_error("wide quotient exceeds uint64");
    return result.quotient.lo;
}

}  // namespace detail

inline std::uint64_t absoluteDistance(std::int64_t lhs,
                                      std::int64_t rhs) noexcept {
    return detail::distance(lhs, rhs);
}

inline std::int64_t ceilDivProductsPositive(std::uint64_t numerator_lhs,
                                             std::uint64_t numerator_rhs,
                                             std::uint64_t denominator_lhs,
                                             std::uint64_t denominator_rhs) {
    const detail::U128 numerator = detail::multiply(numerator_lhs, numerator_rhs);
    const detail::U128 denominator = detail::multiply(denominator_lhs, denominator_rhs);
    detail::DivResult divided = detail::divide(numerator, denominator);
    if (divided.remainder.hi != 0 || divided.remainder.lo != 0) {
        if (detail::add(divided.quotient, {0, 1}))
            throw std::overflow_error("wide ceiling quotient overflow");
    }
    if (divided.quotient.hi != 0 ||
        divided.quotient.lo > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
        throw std::overflow_error("wide ceiling quotient exceeds int64");
    return static_cast<std::int64_t>(divided.quotient.lo);
}

inline std::optional<std::int64_t> parseScaledDecimal(std::string_view text,
                                                       std::int64_t scale) {
    if (text.empty() || scale <= 0) return std::nullopt;
    bool negative = false;
    std::size_t pos = 0;
    if (text[pos] == '+' || text[pos] == '-') {
        negative = text[pos] == '-';
        if (++pos == text.size()) return std::nullopt;
    }

    std::uint64_t whole = 0;
    bool whole_overflow = false;
    bool any = false;
    while (pos < text.size() && std::isdigit(static_cast<unsigned char>(text[pos]))) {
        any = true;
        const unsigned digit = static_cast<unsigned>(text[pos++] - '0');
        if (whole > (std::numeric_limits<std::uint64_t>::max() - digit) / 10) {
            whole_overflow = true;
        } else if (!whole_overflow) {
            whole = whole * 10 + digit;
        }
    }

    std::uint64_t fraction = 0;
    std::uint64_t divisor = 1;
    if (pos < text.size() && text[pos] == '.') {
        ++pos;
        while (pos < text.size() && std::isdigit(static_cast<unsigned char>(text[pos]))) {
            any = true;
            if (divisor < 1'000'000'000'000'000'000ull) {
                fraction = fraction * 10 + static_cast<unsigned>(text[pos] - '0');
                divisor *= 10;
            }
            ++pos;
        }
    }
    if (!any || pos != text.size() || whole_overflow) return std::nullopt;

    detail::U128 scaled = detail::multiply(whole, static_cast<std::uint64_t>(scale));
    detail::U128 fractional = detail::multiply(fraction, static_cast<std::uint64_t>(scale));
    if (detail::add(fractional, {0, divisor / 2})) return std::nullopt;
    const detail::DivResult rounded = detail::divide(fractional, {0, divisor});
    if (detail::add(scaled, rounded.quotient)) return std::nullopt;

    const std::uint64_t limit = negative ? (std::uint64_t{1} << 63)
                                         : static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
    if (scaled.hi != 0 || scaled.lo > limit) return std::nullopt;
    return detail::signedMagnitude(scaled.lo, negative);
}

inline bool withinSquaredRadius(std::int64_t x, std::int64_t y,
                                std::int64_t center_x, std::int64_t center_y,
                                std::int64_t radius) noexcept {
    if (radius < 0) return false;
    const std::uint64_t dx = detail::distance(x, center_x);
    const std::uint64_t dy = detail::distance(y, center_y);
    detail::U128 sum = detail::multiply(dx, dx);
    const detail::U128 y_square = detail::multiply(dy, dy);
    if (detail::add(sum, y_square)) return false;
    const detail::U128 radius_square = detail::multiply(static_cast<std::uint64_t>(radius),
                                                        static_cast<std::uint64_t>(radius));
    return detail::compare(sum, radius_square) <= 0;
}

inline std::int64_t interpolate(std::int64_t start, std::int64_t end,
                                std::int64_t elapsed, std::int64_t total) {
    if (elapsed < 0 || total <= 0 || elapsed > total)
        throw std::invalid_argument("invalid interpolation interval");
    const std::uint64_t start_key = detail::ordered(start);
    const std::uint64_t end_key = detail::ordered(end);
    const std::uint64_t span = start_key >= end_key ? start_key - end_key : end_key - start_key;
    const std::uint64_t step = detail::quotient64(
        detail::multiply(span, static_cast<std::uint64_t>(elapsed)),
        static_cast<std::uint64_t>(total));
    return detail::fromOrdered(start_key <= end_key ? start_key + step : start_key - step);
}

inline std::int64_t scaleDifference(std::int64_t current, std::int64_t previous,
                                    std::int64_t multiplier, std::int64_t divisor) {
    if (divisor == 0) throw std::invalid_argument("scaled difference division by zero");
    const bool negative = (current < previous) != (multiplier < 0) != (divisor < 0);
    const std::uint64_t multiplier_magnitude = multiplier < 0
        ? static_cast<std::uint64_t>(-(multiplier + 1)) + 1
        : static_cast<std::uint64_t>(multiplier);
    const std::uint64_t divisor_magnitude = divisor < 0
        ? static_cast<std::uint64_t>(-(divisor + 1)) + 1
        : static_cast<std::uint64_t>(divisor);
    const std::uint64_t quotient = detail::quotient64(
        detail::multiply(detail::distance(current, previous), multiplier_magnitude),
        divisor_magnitude);
    return detail::signedMagnitude(quotient, negative);
}

inline std::int64_t saturatingDifferenceSum(const std::vector<std::int64_t>& values,
                                            std::int64_t baseline) {
    detail::U128 sum;
    for (const std::int64_t value : values) {
        if (value < baseline) throw std::invalid_argument("difference sum value is below baseline");
        if (detail::add(sum, {0, detail::distance(value, baseline)}))
            return std::numeric_limits<std::int64_t>::max();
    }
    if (sum.hi != 0 ||
        sum.lo > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
        return std::numeric_limits<std::int64_t>::max();
    return static_cast<std::int64_t>(sum.lo);
}

}  // namespace chudware::twin::wide_math
