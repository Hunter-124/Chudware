#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::ecad::symlib {

// Resolve a SymbolDef by libid ("Device:R"). Returns null json if unknown.
nlohmann::json resolve_symbol(const std::string& libid);

// Every libid in the built-in catalog, sorted.
std::vector<std::string> symbol_libids();

// [{libid,name,description,keywords:[..],prefix,pin_count,default_footprint,power:bool}] sorted by libid.
nlohmann::json symbol_catalog();

// Ranked substring/keyword search. limit<=0 means "all".
nlohmann::json search_symbols(const std::string& query, int limit);

// Register/override a SymbolDef at runtime (project libraries, KiCad .kicad_sym import).
void register_symbol(const std::string& libid, const nlohmann::json& def);

// Parse a .kicad_sym file and register every symbol in it. Returns {"ok",bool,"added":[libid...],"warnings":[...]}.
nlohmann::json load_kicad_sym_file(const std::string& path);

}  // namespace chudware::ecad::symlib
