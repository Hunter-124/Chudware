#pragma once

#include <cmath>
#include <cstddef>
#include <istream>
#include <locale>
#include <streambuf>
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
  class ViewBuffer final : public std::streambuf {
   public:
    explicit ViewBuffer(std::string_view view) {
      // Only the get area is exposed: extraction never writes through this pointer.
      auto* begin = const_cast<char*>(view.data());
      setg(begin, begin, begin + view.size());
    }
    std::size_t consumed() const { return static_cast<std::size_t>(gptr() - eback()); }
  } buffer(text);
  std::istream input(&buffer);
  input.imbue(std::locale::classic());
  input >> std::noskipws >> value;
  if (!input || !std::isfinite(value)) return false;

  consumed = buffer.consumed();
  return consumed != 0;
}

// Exact finite-number parser: partial parses and trailing junk are rejected.
inline bool parseFiniteDouble(std::string_view text, double& value) noexcept {
  std::size_t consumed = 0;
  return parseFiniteDoublePrefix(text, value, consumed) && consumed == text.size();
}

}  // namespace chudware::device
