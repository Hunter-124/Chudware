#pragma once

#include <cmath>
#include <cstddef>
#include <locale>
#include <sstream>
#include <string>
#include <string_view>

namespace chudware::device {

// Parses the finite floating-point prefix of text using the same token boundaries as
// std::from_chars. The classic locale fixes '.' as the decimal separator, noskipws rejects
// leading whitespace, and the explicit check rejects a leading '+' (which from_chars rejects).
inline bool parseFiniteDoublePrefix(std::string_view text, double& value,
                                    std::size_t& consumed) noexcept {
  consumed = 0;
  if (text.empty() || text.front() == '+') return false;

  // Apple libc++ still deletes the floating-point from_chars overloads on some toolchains.
  std::istringstream input{std::string(text)};
  input.imbue(std::locale::classic());
  input >> std::noskipws >> value;
  if (!input || !std::isfinite(value)) return false;

  if (input.eof()) {
    consumed = text.size();
  } else {
    const auto position = input.tellg();
    if (position < 0) return false;
    consumed = static_cast<std::size_t>(position);
  }
  return consumed != 0;
}

// Exact finite-number parser: partial parses and trailing junk are rejected.
inline bool parseFiniteDouble(std::string_view text, double& value) noexcept {
  std::size_t consumed = 0;
  return parseFiniteDoublePrefix(text, value, consumed) && consumed == text.size();
}

}  // namespace chudware::device
