// Closed ECAD op vocabulary — SINGLE SOURCE for apply_one dispatch names.
// When you add a handler in ops.cpp::apply_one, also add the name here.
// test_op_registry (doc side) pattern; ECAD mirrors the same discipline.
#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace aicad::ecad {

std::vector<std::string> ecad_op_kinds();
std::vector<std::string> suggest_ecad_ops(const std::string& unknown, std::size_t n = 5);
std::string unknown_ecad_op_message(int index, const std::string& k);

}  // namespace aicad::ecad
