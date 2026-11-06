#pragma once

#include <map>
#include <string>
#include <nlohmann/json.hpp>
#include <TopoDS_Shape.hxx>

namespace chudware::doc::scad {
using json = nlohmann::json;
struct Build { TopoDS_Shape shape; json report; };
json inspect(const json& params);
json import_project(const std::string& path);
Build build(const json& params, const std::map<std::string, double>& env);
std::string cache_identity();
json capability();
std::string export_source(const json& params, const std::map<std::string, double>& env = {});
json export_project(const json& params, const std::string& path,
                    const std::map<std::string, double>& env = {});
}
