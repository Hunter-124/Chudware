// Topology optimization native adapter — see sim/topopt_run.h.
#include "sim/topopt_run.h"

#include "sim/progress.h"
#include "sim/topopt_manufacturing.h"
#include "sim/topopt_optimizer.h"
#include "sim/topopt_reanalysis.h"
#include "sim/topopt_surface.h"
#include "util/sha256.h"
#include "util/time_local.h"
#include "version_info.h" // CHUDWARE_HOSTD_BUILD_ID

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <functional>
#include <limits>
#include <optional>
#include <random>
#include <set>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

#if defined(_WIN32)
#include <io.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif
#ifdef __linux__
#include <sys/resource.h>
#endif

#ifndef CHUDWARE_POLYMESH_COMMIT
#define CHUDWARE_POLYMESH_COMMIT "unknown"
#endif

namespace chudware {
namespace sim {

using nlohmann::json;
namespace fs = std::filesystem;
using namespace topology;

namespace {

[[noreturn]] void refuse(const std::string &code, const std::string &message) {
  throw ModelError(code, message);
}

// ── Small helpers
// ────────────────────────────────────────────────────────────
std::string utc_stamp(const char *format) {
  const std::time_t now = std::time(nullptr);
  std::tm parts{};
  if (!chudware::util::gmTime(now, parts))
    return std::string();
  char buf[40] = {0};
  if (std::strftime(buf, sizeof(buf), format, &parts) == 0)
    return std::string();
  return buf;
}

std::string sha256_text(std::string_view bytes) {
  return util::sha256_hex(bytes);
}

// Canonical digest: nlohmann::json objects are key-sorted std::maps, so dump()
// is canonical for a given value.
std::string sha256_json(const json &value) { return sha256_text(value.dump()); }

std::string sha256_file(const fs::path &path, std::uint64_t *bytes) {
  std::FILE *f = std::fopen(path.string().c_str(), "rb");
  if (!f)
    refuse("source_unreadable",
           "cannot read " + path.string() + ": " + std::strerror(errno));
  util::Sha256 sha;
  std::vector<std::uint8_t> buffer(1 << 20);
  std::uint64_t total = 0;
  for (;;) {
    const std::size_t n = std::fread(buffer.data(), 1, buffer.size(), f);
    if (n > 0) {
      sha.update(buffer.data(), n);
      total += n;
    }
    if (n < buffer.size()) {
      if (std::ferror(f)) {
        std::fclose(f);
        refuse("source_unreadable", "read error on " + path.string());
      }
      break;
    }
  }
  std::fclose(f);
  if (bytes)
    *bytes = total;
  return sha.finish_hex();
}

json to_array(const Eigen::VectorXd &v) {
  json out = json::array();
  for (Eigen::Index i = 0; i < v.size(); ++i)
    out.push_back(std::isfinite(v[i]) ? json(v[i]) : json(nullptr));
  return out;
}

bool is_present(const json &study, const char *key) {
  if (!study.contains(key))
    return false;
  const json &v = study[key];
  if (v.is_null())
    return false;
  if ((v.is_array() || v.is_object()) && v.empty())
    return false;
  return true;
}

const json &empty_array() {
  static const json empty = json::array();
  return empty;
}

const json &array_at(const json &obj, const char *key, const std::string &ctx) {
  if (!obj.is_object() || !obj.contains(key) || obj[key].is_null())
    return empty_array();
  if (!obj[key].is_array())
    refuse("invalid_input", ctx + "." + key + " must be an array");
  return obj[key];
}

void check_entry_keys(const json &entry, const std::set<std::string> &allowed,
                      const std::string &label) {
  if (!entry.is_object())
    refuse("invalid_input", label + " must be an object");
  for (auto it = entry.begin(); it != entry.end(); ++it)
    if (!allowed.count(it.key()))
      refuse("unknown_key",
             label + ": key '" + it.key() +
                 "' is not supported here (not dropped silently)");
}

std::string entry_label(const json &entry, const std::string &fallback) {
  if (entry.is_object() && entry.contains("label") &&
      entry["label"].is_string() && !entry["label"].get<std::string>().empty())
    return entry["label"].get<std::string>();
  return fallback;
}

// One resolved doc_face_points faceset (object with points, or a one-element
// array of such). Unresolved document selectors are refused by name.
const json &faceset_of(const json &faces, const std::string &label) {
  if (faces.is_object() && faces.contains("points"))
    return faces;
  if (faces.is_array() && faces.size() == 1 && faces[0].is_object() &&
      faces[0].contains("points"))
    return faces[0];
  if (faces.is_array() && faces.size() > 1)
    refuse("faces_not_merged",
           label + ": faces holds " + std::to_string(faces.size()) +
               " facesets; merge them into one resolved faceset (points "
               "concatenated) before the run");
  if (faces.is_null() || (faces.is_array() && faces.empty()))
    refuse("invalid_bc", label + ": no faces selected");
  refuse("unresolved_selector",
         label + ": faces must be a resolved faceset with points "
                 "(doc_face_points output); document selectors are resolved "
                 "before sim_run_topology");
}

// ── Envelope keys
// ────────────────────────────────────────────────────────────
const std::set<std::string> &envelope_keys() {
  static const std::set<std::string> keys = {
      // metadata
      "id", "type", "name", "job_id", "study_id", "doc_id",
      // mesh source and options (mesh_options_from_study vocabulary)
      "step_path", "points", "tets", "nodes_per_elem", "mesh_order", "order",
      "fineness", "target_size_mm", "min_size_mm", "max_size_mm",
      "mesh_target_mm", "mesh_fineness", "mesh_min_mm", "learned_advisor",
      "advisor_max_dof", "governor_probes",
      // physics
      "material", "topology", "fixtures", "loads", "load_cases", "thermal",
      "flow", "preserve", "exclude", "build_plate"};
  return keys;
}

const std::pair<const char *, const char *> kMeshAliases[] = {
    {"mesh_target_mm", "target_size_mm"},
    {"mesh_fineness", "fineness"},
    {"mesh_min_mm", "min_size_mm"}};

json mesh_study_of(const json &study) {
  json m = json::object();
  if (study.contains("step_path") && study["step_path"].is_string() &&
      !study["step_path"].get<std::string>().empty()) {
    m["step_path"] = study["step_path"];
  } else {
    m["points"] = study["points"];
    m["tets"] = study["tets"];
    m["nodes_per_elem"] = 4;
  }
  m["mesh_order"] = 1;
  for (const char *key :
       {"fineness", "target_size_mm", "min_size_mm", "max_size_mm",
        "learned_advisor", "advisor_max_dof", "governor_probes"})
    if (study.contains(key))
      m[key] = study[key];
  for (const auto &[alias, key] : kMeshAliases)
    if (study.contains(alias) && !m.contains(key))
      m[key] = study[alias];
  return m;
}

// ── Mechanical entries
// ───────────────────────────────────────────────────────
struct FixtureEntry {
  std::string label;
  json faceset; // faceset with `dofs` when given
  json dofs;    // null = all
};

FixtureEntry fixture_entry(const json &e, const std::string &fallback) {
  FixtureEntry f;
  f.label = entry_label(e, fallback);
  if (e.is_object() && e.contains("points")) { // bare faceset (static syntax)
    f.faceset = e;
    f.faceset.erase("label");
    if (e.contains("dofs"))
      f.dofs = e["dofs"];
  } else {
    check_entry_keys(e, {"faces", "dofs", "label"}, f.label);
    if (!e.contains("faces"))
      refuse("invalid_bc", f.label + ": fixture needs faces");
    f.faceset = faceset_of(e["faces"], f.label);
    f.faceset.erase("dofs");
    if (e.contains("dofs")) {
      f.dofs = e["dofs"];
      f.faceset["dofs"] = e["dofs"];
    }
  }
  return f;
}

std::vector<FixtureEntry> fixture_entries(const json &arr,
                                          const std::string &what) {
  std::vector<FixtureEntry> out;
  for (std::size_t i = 0; i < arr.size(); ++i)
    out.push_back(fixture_entry(arr[i], what + " " + std::to_string(i + 1)));
  return out;
}

struct LoadEntry {
  std::string label;
  json load; // {faces:<faceset>, type, vector|magnitude}
};

LoadEntry load_entry(const json &e, const std::string &fallback) {
  LoadEntry l;
  l.label = entry_label(e, fallback);
  check_entry_keys(e, {"faces", "type", "vector", "vec", "magnitude", "label"},
                   l.label);
  if (!e.contains("faces"))
    refuse("invalid_bc", l.label + ": load needs faces");
  if (!e.contains("type") || !e["type"].is_string())
    refuse("invalid_bc", l.label + ": load type must be \"force\" or "
                                   "\"pressure\"");
  const std::string type = e["type"].get<std::string>();
  l.load = json::object();
  l.load["faces"] = faceset_of(e["faces"], l.label);
  l.load["type"] = type;
  if (type == "force") {
    if (e.contains("magnitude"))
      refuse("key_not_applicable",
             l.label + ": magnitude applies to pressure loads only");
    if (e.contains("vector") && e.contains("vec") && e["vector"] != e["vec"])
      refuse("invalid_bc", l.label + ": vector and vec disagree");
    const json &v = e.contains("vector") ? e["vector"]
                    : e.contains("vec")  ? e["vec"]
                                         : json();
    if (!v.is_array() || v.size() != 3)
      refuse("invalid_bc", l.label + ": force load needs vector [x,y,z] N");
    for (const auto &c : v)
      if (!c.is_number() || !std::isfinite(c.get<double>()))
        refuse("invalid_bc", l.label + ": force vector must be finite");
    l.load["vector"] = v;
  } else if (type == "pressure") {
    if (e.contains("vector") || e.contains("vec"))
      refuse("key_not_applicable",
             l.label + ": vector applies to force loads only");
    if (!e.contains("magnitude") || !e["magnitude"].is_number() ||
        !std::isfinite(e["magnitude"].get<double>()))
      refuse("invalid_bc",
             l.label + ": pressure load needs a finite magnitude (MPa)");
    l.load["magnitude"] = e["magnitude"];
  } else {
    refuse("invalid_bc",
           l.label + ": load type \"" + type + "\" is not force or pressure");
  }
  return l;
}

std::vector<LoadEntry> load_entries(const json &arr, const std::string &what) {
  std::vector<LoadEntry> out;
  for (std::size_t i = 0; i < arr.size(); ++i)
    out.push_back(load_entry(arr[i], what + " " + std::to_string(i + 1)));
  return out;
}

json nodes_json(const std::vector<int> &nodes) { return json(nodes); }

// Resolve a face selection on the mesh (nullptr = dry: no nodes).
json face_nodes(const json &faceset, const PreparedMesh *mesh,
                const std::string &label, const char *section, bool need_face,
                json *evidence) {
  if (!mesh)
    return json::array();
  BoundarySelection sel;
  std::string error;
  if (!resolve_boundary_selection(faceset, *mesh, label, sel, error))
    refuse("boundary_resolution_failed", error);
  if (need_face && sel.facets.empty())
    refuse("invalid_bc",
           label + ": resolved to " + std::to_string(sel.nodes.size()) +
               " node(s) but no complete exterior face; a " + section +
               " needs a face, not a point or edge pick");
  if (evidence)
    evidence->push_back({{"section", section},
                         {"label", label},
                         {"nodes", sel.nodes.size()},
                         {"complete_faces", sel.facets.size()},
                         {"area_mm2", sel.area_mm2}});
  return nodes_json(sel.nodes);
}

json copy_without_faces(const json &entry, const std::string &label) {
  json out = entry;
  out.erase("faces");
  out["label"] = label;
  return out;
}

// ── Normalization (dry when mesh == nullptr)
// ─────────────────────────────────
json normalize_envelope(const json &study, const json &topology,
                        PhysicsKind phys, const PreparedMesh *mesh,
                        bool include_regions, json *evidence) {
  json ev = json::array();
  json out = json::object();
  out["topology"] = topology;
  out["material"] =
      study.contains("material") ? study["material"] : json::object();

  if (include_regions && study.contains("build_plate")) {
    const json &entry = study["build_plate"];
    const std::string label = entry_label(entry, "Build plate");
    check_entry_keys(entry, {"faces", "label"}, label);
    const json faces = entry.value("faces", json());
    if (faces.is_null() || (faces.is_array() && faces.empty()))
      refuse("build_plate_empty", label + ": no boundary triangle matched");
    const json &faceset = faceset_of(faces, label);
    json nodes = json::array();
    if (mesh) {
      BoundarySelection selection;
      std::string error;
      if (!resolve_boundary_selection(faceset, *mesh, label, selection, error) ||
          selection.facets.empty())
        refuse("build_plate_empty", label + ": no boundary triangle matched");
      nodes = nodes_json(selection.nodes);
      ev.push_back({{"section", "build_plate"},
                    {"label", label},
                    {"nodes", selection.nodes.size()},
                    {"complete_faces", selection.facets.size()},
                    {"area_mm2", selection.area_mm2}});
    }
    out["build_plate"] = {{"label", label}, {"nodes", std::move(nodes)}};
  }
  // ── Structural
  if (uses_structural_boundary(phys)) {
    const std::vector<FixtureEntry> shared =
        fixture_entries(array_at(study, "fixtures", "study"), "Fixture");
    struct CaseIn {
      std::string name;
      json weight;
      std::vector<FixtureEntry> fixtures;
      std::vector<LoadEntry> loads;
    };
    std::vector<CaseIn> cases;
    if (phys != PhysicsKind::Modal) {
      const json &lc = array_at(study, "load_cases", "study");
      if (!lc.empty()) {
        for (std::size_t c = 0; c < lc.size(); ++c) {
          const std::string ctx = "load case " + std::to_string(c + 1);
          check_entry_keys(lc[c], {"name", "weight", "fixtures", "loads"}, ctx);
          CaseIn in;
          in.name = lc[c].contains("name") && lc[c]["name"].is_string()
                        ? lc[c]["name"].get<std::string>()
                        : "case_" + std::to_string(c + 1);
          if (lc[c].contains("weight"))
            in.weight = lc[c]["weight"];
          in.fixtures = fixture_entries(array_at(lc[c], "fixtures", ctx),
                                        in.name + " fixture");
          in.loads =
              load_entries(array_at(lc[c], "loads", ctx), in.name + " load");
          cases.push_back(std::move(in));
        }
      } else {
        CaseIn in;
        in.name = "default";
        in.loads = load_entries(array_at(study, "loads", "study"), "Load");
        cases.push_back(std::move(in));
      }
    }

    auto fixture_json = [](const FixtureEntry &f, const json &nodes) {
      json j = {{"label", f.label}, {"nodes", nodes}};
      if (!f.dofs.is_null())
        j["dofs"] = f.dofs;
      return j;
    };
    auto resolve_supports = [&](const std::vector<FixtureEntry> &list,
                                const std::string &ctx) {
      std::vector<json> nodes;
      if (!mesh) {
        nodes.assign(list.size(), json::array());
        return nodes;
      }
      json arr = json::array();
      std::string names;
      for (const auto &f : list) {
        arr.push_back(f.faceset);
        names += (names.empty() ? "" : ", ") + f.label;
      }
      MechanicalFixtureSet set;
      std::string error;
      if (!resolve_mechanical_fixtures(arr, *mesh, set, error, "topology"))
        refuse("boundary_resolution_failed",
               ctx + " supports [" + names + "]: " + error);
      for (std::size_t i = 0; i < list.size(); ++i)
        nodes.push_back(nodes_json(set.selections[i].nodes));
      return nodes;
    };

    json fixtures = json::array();
    if (phys == PhysicsKind::Modal) {
      const auto nodes = resolve_supports(shared, "modal");
      for (std::size_t i = 0; i < shared.size(); ++i) {
        fixtures.push_back(fixture_json(shared[i], nodes[i]));
        ev.push_back({{"section", "fixture"},
                      {"label", shared[i].label},
                      {"nodes", nodes[i].size()},
                      {"dofs", shared[i].dofs.is_null() ? json({0, 1, 2})
                                                        : shared[i].dofs}});
      }
    } else {
      json out_cases = json::array();
      const std::size_t N = mesh ? mesh->n_nodes() : 0;
      for (std::size_t c = 0; c < cases.size(); ++c) {
        const CaseIn &in = cases[c];
        std::vector<FixtureEntry> all = shared;
        all.insert(all.end(), in.fixtures.begin(), in.fixtures.end());
        const auto nodes = resolve_supports(all, "load case '" + in.name + "'");
        if (c == 0)
          for (std::size_t i = 0; i < shared.size(); ++i) {
            fixtures.push_back(fixture_json(shared[i], nodes[i]));
            ev.push_back({{"section", "fixture"},
                          {"label", shared[i].label},
                          {"nodes", nodes[i].size()},
                          {"dofs", shared[i].dofs.is_null() ? json({0, 1, 2})
                                                            : shared[i].dofs}});
          }
        json case_fixtures = json::array();
        for (std::size_t i = 0; i < in.fixtures.size(); ++i) {
          case_fixtures.push_back(
              fixture_json(in.fixtures[i], nodes[shared.size() + i]));
          ev.push_back({{"section", "case_fixture"},
                        {"case", in.name},
                        {"label", in.fixtures[i].label},
                        {"nodes", nodes[shared.size() + i].size()}});
        }
        std::vector<double> forces(3 * N, 0.0);
        for (const LoadEntry &l : in.loads) {
          if (!mesh)
            continue;
          MechanicalLoadVector vec;
          std::string error;
          if (!resolve_mechanical_load_case(json::array({l.load}), *mesh, vec,
                                            error, "topology"))
            refuse("boundary_resolution_failed",
                   "load case '" + in.name + "' " + l.label + ": " + error);
          std::size_t loaded = 0;
          for (const auto &s : vec.selections)
            loaded += s.nodes.size();
          for (std::size_t k = 0; k < forces.size(); ++k)
            forces[k] += vec.forces[k];
          ev.push_back(
              {{"section", "load"},
               {"case", in.name},
               {"label", l.label},
               {"type", l.load["type"]},
               {"nodes", loaded},
               {"resultant_n",
                {vec.resultant_n[0], vec.resultant_n[1], vec.resultant_n[2]}},
               {"notes", vec.notes}});
        }
        json cj = {{"name", in.name}, {"forces_n", forces}};
        if (!in.weight.is_null())
          cj["weight"] = in.weight;
        if (!case_fixtures.empty())
          cj["fixtures"] = std::move(case_fixtures);
        if (in.loads.empty())
          ev.push_back({{"section", "load"},
                        {"case", in.name},
                        {"label", "thermal expansion only"},
                        {"note", "no mechanical load; the case is driven by "
                                 "the thermal eigenstrain"}});
        out_cases.push_back(std::move(cj));
      }
      out["load_cases"] = std::move(out_cases);
    }
    if (!fixtures.empty())
      out["fixtures"] = std::move(fixtures);
  }

  // ── Thermal
  if (uses_thermal_boundary(phys)) {
    const json &th = study["thermal"];
    check_entry_keys(th, {"reference_temperature_c", "fixtures", "loads"},
                     "thermal");
    json t = json::object();
    if (th.contains("reference_temperature_c"))
      t["reference_temperature_c"] = th["reference_temperature_c"];
    json fx = json::array();
    const json &fixtures = array_at(th, "fixtures", "thermal");
    for (std::size_t i = 0; i < fixtures.size(); ++i) {
      const std::string label = entry_label(
          fixtures[i], "Fixed temperature " + std::to_string(i + 1));
      check_entry_keys(fixtures[i], {"faces", "temperature_c", "label"}, label);
      json item = copy_without_faces(fixtures[i], label);
      item["nodes"] =
          face_nodes(faceset_of(fixtures[i].value("faces", json()), label),
                     mesh, label, "fixed temperature", false, &ev);
      fx.push_back(std::move(item));
    }
    t["fixtures"] = std::move(fx);
    json ld = json::array();
    const json &loads = array_at(th, "loads", "thermal");
    for (std::size_t i = 0; i < loads.size(); ++i) {
      const std::string label =
          entry_label(loads[i], "Heat load " + std::to_string(i + 1));
      check_entry_keys(loads[i],
                       {"faces", "type", "magnitude_w_m2", "power_w", "h_w_m2k",
                        "ambient_c", "label"},
                       label);
      json item = copy_without_faces(loads[i], label);
      item["nodes"] =
          face_nodes(faceset_of(loads[i].value("faces", json()), label), mesh,
                     label, "heat load", true, &ev);
      ld.push_back(std::move(item));
    }
    t["loads"] = std::move(ld);
    out["thermal"] = std::move(t);
  }

  // ── Flow
  if (uses_flow_boundary(phys)) {
    const json &fl = study["flow"];
    check_entry_keys(fl,
                     {"viscosity_pa_s", "density_kg_m3", "brinkman_max",
                      "reference_length_mm", "fluid_k_w_mk", "fluid_cp_j_kgk",
                      "inlets", "outlets", "walls"},
                     "flow");
    json f = fl;
    auto ports = [&](const char *key, const char *what,
                     const std::set<std::string> &allowed, bool need_face) {
      json arr = json::array();
      const json &in = array_at(fl, key, "flow");
      for (std::size_t i = 0; i < in.size(); ++i) {
        const bool bare = in[i].is_object() && in[i].contains("points");
        const std::string label =
            entry_label(in[i], std::string(what) + " " + std::to_string(i + 1));
        json item;
        json nodes;
        if (bare) {
          item = {{"label", label}};
          nodes = face_nodes(in[i], mesh, label, what, need_face, &ev);
        } else {
          check_entry_keys(in[i], allowed, label);
          item = copy_without_faces(in[i], label);
          nodes = face_nodes(faceset_of(in[i].value("faces", json()), label),
                             mesh, label, what, need_face, &ev);
        }
        item["nodes"] = std::move(nodes);
        arr.push_back(std::move(item));
      }
      return arr;
    };
    f["inlets"] =
        ports("inlets", "inlet",
              {"faces", "velocity_m_s", "temperature_c", "label"}, true);
    f["outlets"] =
        ports("outlets", "outlet", {"faces", "pressure_pa", "label"}, true);
    // Explicit wall labels name subsets of the default no-slip boundary; on a
    // reconstructed domain (include_regions == false) every non-port exterior
    // face is a wall already, so they are not re-resolved there.
    json walls = include_regions
                     ? ports("walls", "wall", {"faces", "label"}, false)
                     : json::array();
    if (!include_regions && is_present(fl, "walls"))
      ev.push_back({{"section", "wall"},
                    {"note", "explicit wall labels not re-resolved; every "
                             "non-port boundary is no-slip"}});
    if (walls.empty())
      f.erase("walls");
    else
      f["walls"] = std::move(walls);
    out["flow"] = std::move(f);
  }

  // ── Passive regions
  if (include_regions) {
    for (const char *which : {"preserve", "exclude"}) {
      const json &arr = array_at(study, which, "study");
      if (arr.empty())
        continue;
      const std::string title =
          std::string(which) == "preserve" ? "Preserve" : "Exclude";
      json dst = json::array();
      for (std::size_t i = 0; i < arr.size(); ++i) {
        const std::string label =
            entry_label(arr[i], title + " " + std::to_string(i + 1));
        if (arr[i].is_object() && arr[i].contains("faces")) {
          check_entry_keys(arr[i], {"faces", "thickness_mm", "label"}, label);
          json item = copy_without_faces(arr[i], label);
          item["nodes"] = face_nodes(faceset_of(arr[i]["faces"], label), mesh,
                                     label, which, false, &ev);
          dst.push_back(std::move(item));
        } else {
          check_entry_keys(arr[i], {"box_min_mm", "box_max_mm", "label"},
                           label);
          json item = arr[i];
          item["label"] = label;
          dst.push_back(std::move(item));
          ev.push_back({{"section", which}, {"label", label}, {"box", true}});
        }
      }
      out[which] = std::move(dst);
    }
  }
  if (evidence)
    *evidence = std::move(ev);
  return out;
}

// ── Report units
// ─────────────────────────────────────────────────────────────
std::string objective_unit(ObjectiveKind k) {
  switch (k) {
  case ObjectiveKind::Compliance:
    return "N*mm";
  case ObjectiveKind::Mass:
    return "g";
  case ObjectiveKind::ThermalCompliance:
    return "W*K";
  case ObjectiveKind::Frequency:
    return "Hz";
  case ObjectiveKind::BucklingFactor:
    return "-";
  case ObjectiveKind::Dissipation:
    return "W";
  case ObjectiveKind::MeanTemperature:
    return "C";
  }
  return "-";
}

double objective_report(ObjectiveKind k, double si) {
  switch (k) {
  case ObjectiveKind::Compliance: // J -> N*mm
  case ObjectiveKind::Mass:       // kg -> g
    return si * 1e3;
  case ObjectiveKind::MeanTemperature:
    return units::kelvin_to_celsius(si);
  default:
    return si;
  }
}

std::string base_name(const std::string &name) {
  return name.substr(0, name.find('@'));
}

struct Reported {
  double value;
  std::string unit;
};

Reported report_value(const std::string &name, const std::string &unit,
                      double si) {
  const std::string base = base_name(name);
  if (unit == "Pa")
    return base.rfind("max_von_mises", 0) == 0 || base == "von_mises"
               ? Reported{units::pa_to_mpa(si), "MPa"}
               : Reported{si, "Pa"};
  if (unit == "m")
    return {units::m_to_mm(si), "mm"};
  if (unit == "K")
    return {units::kelvin_to_celsius(si), "C"};
  if (unit == "kg")
    return {si * 1e3, "g"};
  if (unit == "J")
    return {si * 1e3, "N*mm"};
  if (unit.empty() || unit == "1")
    return {si, "-"};
  return {si, unit};
}

std::string constraint_label(const std::string &name) {
  const std::string base = base_name(name);
  const std::string suffix = name.size() > base.size()
                                 ? " (" + name.substr(base.size() + 1) + ")"
                                 : "";
  static const std::pair<const char *, const char *> labels[] = {
      {"max_von_mises", "Max von Mises stress"},
      {"max_displacement", "Max displacement"},
      {"max_temperature", "Max temperature"},
      {"min_frequency", "Min fundamental frequency"},
      {"min_buckling_factor", "Min buckling load factor"},
      {"pressure_drop", "Pressure drop"},
      {"solid_volume_fraction", "Solid volume fraction"},
      {"fluid_volume_fraction", "Fluid volume fraction"}};
  for (const auto &[key, label] : labels)
    if (base == key)
      return label + suffix;
  return name;
}

bool is_volume_constraint(const std::string &name) {
  return name == "solid_volume_fraction" || name == "fluid_volume_fraction";
}

json constraints_json(const std::vector<Constraint> &cs, double tolerance,
                      double volume_tolerance) {
  json out = json::array();
  for (const Constraint &c : cs) {
    const Reported response = report_value(c.name, c.unit, c.response);
    const Reported limit = report_value(c.name, c.unit, c.limit);
    const double allowed =
        is_volume_constraint(c.name) ? volume_tolerance : tolerance;
    out.push_back(
        {{"name", c.name},
         {"label", constraint_label(c.name)},
         {"value", response.value},
         {"limit", limit.value},
         {"units", response.unit},
         {"g", c.value},
         {"violation", std::max(0.0, c.value)},
         {"tolerance", allowed},
         {"satisfied", c.value <= allowed},
         {"aggregation", c.aggregation},
         {"kind", is_volume_constraint(c.name) ? "volume" : "response"}});
  }
  return out;
}

Field report_field(const Field &f) {
  Field r = f;
  const std::string base = base_name(f.name);
  if (f.unit == "m") {
    r.values *= 1e3;
    r.unit = "mm";
  } else if (f.unit == "K") {
    r.values.array() -= units::kKelvinAtZeroCelsius;
    r.unit = "C";
  } else if (f.unit == "Pa" && base != "pressure") {
    r.values *= 1e-6;
    r.unit = "MPa";
  }
  return r;
}

json field_json(const Field &f) {
  return {{"name", f.name},
          {"unit", f.unit},
          {"location", f.location == FieldLocation::Node ? "node" : "element"},
          {"components", f.components},
          {"values", to_array(f.values)}};
}

// reanalysis.vtu fields in report units. The physics gives von Mises per
// element (the constant-strain tet4 stress C B u of the reanalysis). FEA
// viewers and render_animation read a POINT field von_mises[@case] in MPa, as
// run_fea_static writes. So every element von_mises[@case] is written twice:
//   * point von_mises[@case]: at each node, the volume-weighted average over
//     the incident RETAINED elements (rho >= threshold); a node with no
//     retained element has 0 MPa (no material carries stress there);
//   * cell element_von_mises[@case]: the element values, unchanged.
// A stress-bearing reanalysis domain is the clipped solid (every element
// rho = 1), so there the mask keeps every element.
constexpr const char *kReanalysisPointStress =
    "Point von_mises[@case] is the volume-weighted average of the element von "
    "Mises of the retained elements at each node (MPa). A node with no "
    "retained element has 0 MPa. Cell element_von_mises[@case] has the "
    "element values.";

std::vector<Field> reanalysis_vtu_fields(const TetMesh &mesh,
                                         const Eigen::VectorXd &rho,
                                         double threshold,
                                         const std::vector<Field> &si_fields) {
  Eigen::VectorXd retained(rho.size());
  for (Eigen::Index e = 0; e < rho.size(); ++e)
    retained[e] = rho[e] >= threshold ? 1.0 : 0.0;
  Eigen::VectorXd share; // nodal retained-volume share, on first use
  std::vector<Field> out;
  out.reserve(2 * si_fields.size());
  for (const Field &si : si_fields) {
    Field f = report_field(si);
    if (base_name(f.name) == "von_mises" &&
        f.location == FieldLocation::Element && f.components == 1) {
      if (share.size() == 0)
        share = nodal_average(mesh, retained, 1);
      const Eigen::VectorXd sum =
          nodal_average(mesh, f.values.cwiseProduct(retained), 1);
      Field point{f.name, f.unit, FieldLocation::Node, 1,
                  Eigen::VectorXd::Zero(sum.size())};
      for (Eigen::Index n = 0; n < sum.size(); ++n)
        if (share[n] > 0) // NaN (unreferenced node) and 0 keep 0
          point.values[n] = sum[n] / share[n];
      f.name = "element_" + f.name;
      out.push_back(std::move(point));
    }
    out.push_back(std::move(f));
  }
  return out;
}

// ── Durable files
// ────────────────────────────────────────────────────────────
struct Written {
  std::string path, sha256;
  std::uint64_t bytes = 0;
};

void sync_file(std::FILE *f) {
#if defined(_WIN32)
  _commit(_fileno(f));
#else
  ::fsync(fileno(f));
#endif
}

Written write_new_file(const fs::path &path, std::string_view bytes) {
  std::FILE *f = std::fopen(path.string().c_str(), "wbx");
  if (!f) {
    if (errno == EEXIST)
      refuse("artifact_exists",
             path.string() + " already exists; results are never overwritten");
    refuse("artifact_io",
           "cannot create " + path.string() + ": " + std::strerror(errno));
  }
  const bool ok =
      std::fwrite(bytes.data(), 1, bytes.size(), f) == bytes.size() &&
      std::fflush(f) == 0;
  if (ok)
    sync_file(f);
  const bool closed = std::fclose(f) == 0;
  if (!ok || !closed) {
    std::error_code ec;
    fs::remove(path, ec);
    refuse("artifact_io", "failed writing " + path.string());
  }
  return {path.string(), sha256_text(bytes), bytes.size()};
}

// Own-file replace (checkpoint): exclusive temp + fsync + rename.
Written replace_own_file(const fs::path &path, std::string_view bytes) {
  fs::path tmp = path;
  tmp += ".tmp";
  std::error_code ec;
  fs::remove(tmp, ec);
  Written w = write_new_file(tmp, bytes);
  fs::rename(tmp, path, ec);
  if (ec) {
    fs::remove(tmp, ec);
    refuse("artifact_io", "cannot publish " + path.string());
  }
  w.path = path.string();
  return w;
}

void sync_directory(const fs::path &dir) {
#if !defined(_WIN32)
  const int fd = ::open(dir.string().c_str(), O_RDONLY);
  if (fd >= 0) {
    ::fsync(fd);
    ::close(fd);
  }
#else
  (void)dir;
#endif
}

json artifact_json(const Written &w, const char *format, const char *label) {
  return {{"path", w.path},
          {"format", format},
          {"sha256", w.sha256},
          {"bytes", w.bytes},
          {"label", label}};
}

json artifact_json(const ArtifactFile &a, const char *format,
                   const char *label) {
  return {{"path", a.path},
          {"format", format},
          {"encoding", a.format},
          {"sha256", a.sha256},
          {"bytes", a.bytes},
          {"label", label},
          {"length_unit", a.length_unit},
          {"points", a.points},
          {"triangles", a.triangles},
          {"cells", a.cells},
          {"arrays", a.arrays}};
}

fs::path default_result_root() {
#if defined(_WIN32)
  if (const char *base = std::getenv("APPDATA"); base && base[0])
    return fs::path(base) / "Chudware" / "sim" / "topology_runs";
#else
  if (const char *xdg = std::getenv("XDG_DATA_HOME"); xdg && xdg[0])
    return fs::path(xdg) / "Chudware" / "sim" / "topology_runs";
  if (const char *home = std::getenv("HOME"); home && home[0])
    return fs::path(home) / ".local" / "share" / "Chudware" / "sim" /
           "topology_runs";
#endif
  refuse("result_dir_unavailable",
         "no topology.result_dir given and no user data directory "
         "(XDG_DATA_HOME/HOME/APPDATA) to own a default one");
}

struct RunDirectory {
  fs::path dir;
  std::string run_id;
};

RunDirectory create_run_directory(const Settings &s) {
  const fs::path root =
      s.result_dir.empty() ? default_result_root() : fs::path(s.result_dir);
  std::error_code ec;
  fs::create_directories(root, ec);
  if (ec || !fs::is_directory(root, ec))
    refuse("invalid_result_dir",
           "topology.result_dir " + root.string() +
               " is not a usable directory" +
               (ec ? ": " + ec.message() : std::string()));
  std::random_device rd;
  for (int attempt = 0; attempt < 32; ++attempt) {
    char suffix[9];
    std::snprintf(suffix, sizeof(suffix), "%08x", static_cast<unsigned>(rd()));
    RunDirectory r;
    r.run_id = "native-" + utc_stamp("%Y%m%dT%H%M%SZ") + "-" +
               to_string(s.physics) + "-" + to_string(s.method) + "-" + suffix;
    r.dir = root / r.run_id;
    if (fs::create_directory(r.dir, ec))
      return r;
    if (ec)
      refuse("artifact_io", "cannot create run directory " + r.dir.string() +
                                ": " + ec.message());
  }
  refuse("artifact_io",
         "could not allocate a unique run directory under " + root.string());
}

json source_identity(const json &study, const char *when) {
  json out = json::object();
  if (study.contains("step_path") && study["step_path"].is_string() &&
      !study["step_path"].get<std::string>().empty()) {
    std::uint64_t bytes = 0;
    out["kind"] = "step";
    out["path"] = study["step_path"];
    out[std::string("sha256_") + when] =
        sha256_file(study["step_path"].get<std::string>(), &bytes);
    out["bytes"] = bytes;
  } else {
    out["kind"] = "inline_mesh";
    out[std::string("sha256_") + when] =
        sha256_json(json{{"points", study["points"]}, {"tets", study["tets"]}});
  }
  return out;
}

double peak_rss_gb() {
#ifdef __linux__
  rusage usage{};
  if (getrusage(RUSAGE_SELF, &usage) == 0)
    return static_cast<double>(usage.ru_maxrss) / (1024.0 * 1024.0);
#endif
  return std::numeric_limits<double>::quiet_NaN();
}

// ── Capabilities derivation
// ──────────────────────────────────────────────────
const char *method_label(Method m) {
  switch (m) {
  case Method::Simp:
    return "SIMP";
  case Method::Ramp:
    return "RAMP";
  case Method::Beso:
    return "BESO";
  case Method::LevelSet:
    return "Level set (reaction-diffusion)";
  }
  return "";
}

const char *method_formulation(Method m) {
  switch (m) {
  case Method::Simp:
    return "Solid isotropic material with penalization, property = "
           "P_solid (eps + (1-eps) rho^p); volume-weighted density filter, "
           "optional tanh projection with continuation; Method of Moving "
           "Asymptotes (Svanberg 1987) update with every response constraint";
  case Method::Ramp:
    return "Rational approximation of material properties, f = rho / (1 + q "
           "(1 - rho)); density filter, optional tanh projection; Method of "
           "Moving Asymptotes update with every response constraint";
  case Method::Beso:
    return "Bi-directional evolutionary structural optimization (soft-kill, "
           "Huang & Xie): binary elements removed AND reintroduced from "
           "filtered, history-averaged sensitivity numbers; evolutionary "
           "volume target V_k+1 = max(V*, V_k (1 - ER)) with an admission cap";
  case Method::LevelSet:
    return "Reaction-diffusion level set after Yamada et al. 2010 (fictitious "
           "interface energy): implicit phi in [-1,1] evolved by an implicit "
           "diffusion-regularized update of the topological sensitivity; C2 "
           "regularized Heaviside ersatz density; volume multiplier by Newton";
  }
  return "";
}

struct PhysicsText {
  const char *label, *formulation, *validity;
};

PhysicsText physics_text(PhysicsKind p) {
  switch (p) {
  case PhysicsKind::Structural:
    return {"Linear elastic structural",
            "tet4 small-strain linear elasticity; weighted multi-load-case "
            "compliance (self-adjoint) or mass; optional KS-aggregated relaxed "
            "von Mises stress and displacement limits (adjoint)",
            "Linear elastic, small displacement, static loads; no contact, "
            "plasticity or geometric nonlinearity"};
  case PhysicsKind::Thermal:
    return {"Steady heat conduction",
            "tet4 steady conduction with fixed temperature, flux/power and "
            "convection boundaries; thermal compliance q^T (T - T_ref) "
            "(adjoint); optional KS max-temperature limit",
            "Steady, linear conduction; convection film coefficient fixed, not "
            "design dependent"};
  case PhysicsKind::Modal:
    return {
        "Natural frequency",
        "tet4 stiffness and consistent mass (Du-Olhoff mass interpolation); "
        "shift-invert eigen-solve with Sylvester inertia completeness "
        "check; soft-min (P=16) aggregated fundamental frequency",
        "Linear free vibration about the fixed supports"};
  case PhysicsKind::Buckling:
    return {"Prestressed linear buckling",
            "static prestress, geometric stiffness K_G(u); eigenvalue "
            "derivative includes the prestress adjoint; soft-min aggregated "
            "smallest positive load factor (case weights 1)",
            "Linear bifurcation buckling of the reference loads; compressive "
            "prestress required; no post-buckling or imperfection analysis"};
  case PhysicsKind::Flow:
    return {"Stokes-Brinkman fluid flow",
            "steady incompressible Stokes-Brinkman, MINI (P1 + bubble / P1) "
            "element, alpha(rho) = alpha_max f(rho); dissipated power "
            "objective (adjoint); optional mean pressure-drop limit",
            "Creeping (Stokes) flow only: refused above Re = 1; not "
            "Navier-Stokes or turbulent; solid is a Brinkman resistance with "
            "finite leakage; rho = 1 solid, volume_fraction budgets fluid"};
  case PhysicsKind::Thermomechanical:
    return {"Coupled thermomechanical",
            "steady conduction -> thermal eigenstrain load on tet4 linear "
            "elasticity; weighted compliance of mechanical + thermal loads "
            "with coupled adjoint; optional stress, displacement and "
            "max-temperature limits",
            "One-way coupling (temperature drives strain), steady, linear "
            "elastic, small displacement"};
  case PhysicsKind::Cht:
    return {"Conjugate heat transfer",
            "Stokes-Brinkman flow coupled to a steady advection-diffusion "
            "energy equation with solid/fluid conductivity interpolation and "
            "constant fluid heat capacity on the superficial velocity; mean "
            "temperature objective with coupled adjoint; optional "
            "max-temperature and pressure-drop limits",
            "Creeping flow (Re <= 1), steady, one-way flow -> heat coupling; "
            "solid is a Brinkman resistance; rho = 1 solid, volume_fraction "
            "budgets fluid; Brinkman leakage through nominal solid advects "
            "heat and is reported (thermal.heat_leakage)"};
  }
  return {"", "", ""};
}

std::string interpolation_label(const Settings &s) {
  char buf[96];
  switch (s.method) {
  case Method::Simp:
    std::snprintf(buf, sizeof(buf), "simp(p=%g)", s.penalty);
    return buf;
  case Method::Ramp:
    std::snprintf(buf, sizeof(buf), "ramp(q=%g)", s.ramp_q);
    return buf;
  case Method::Beso:
    std::snprintf(buf, sizeof(buf), "soft-kill simp(p=%g)", s.penalty);
    return buf;
  case Method::LevelSet:
    return "linear ersatz material over the regularized Heaviside";
  }
  return "";
}

// Sample values that pass parse_settings ranges; offered iff parse +
// compatibility accept them for the pair.
const std::vector<std::pair<const char *, json>> &limit_probes() {
  static const std::vector<std::pair<const char *, json>> probes = {
      {"stress_limit_mpa", 100.0},  {"max_displacement_mm", 1.0},
      {"max_temperature_c", 100.0}, {"min_frequency_hz", 100.0},
      {"min_buckling_factor", 2.0}, {"max_pressure_drop_pa", 100.0}};
  return probes;
}

const std::vector<std::pair<const char *, json>> &parameter_probes() {
  static const std::vector<std::pair<const char *, json>> probes = {
      {"volume_fraction", json::object()},
      {"max_iterations", json{{"max_iterations", 100}}},
      {"change_tolerance", json{{"change_tolerance", 0.01}}},
      {"move_limit", json{{"move_limit", 0.2}}},
      {"filter_radius_mm", json{{"filter_radius_mm", 1.0}}},
      {"projection_beta", json{{"projection_beta", 1.0}}},
      {"density_threshold", json{{"density_threshold", 0.5}}},
      {"min_density", json{{"min_density", 1e-3}}},
      {"penalty", json{{"penalty", 3.0}}},
      {"ramp_q", json{{"ramp_q", 5.0}}},
      {"beso_evolution_rate", json{{"beso_evolution_rate", 0.02}}},
      {"level_set_regularization", json{{"level_set_regularization", 1e-3}}},
      {"symmetry_axes", json{{"symmetry_axes", json::array({"x"})}}},
      {"symmetry_origin_mm",
       json{{"symmetry_axes", json::array({"x"})},
            {"symmetry_origin_mm", json::array({0.0, 0.0, 0.0})}}},
      {"extrusion_axis", json{{"extrusion_axis", "z"}}},
      {"preserve_thickness_mm", json{{"preserve_thickness_mm", 1.0}}},
      {"exclude_thickness_mm", json{{"exclude_thickness_mm", 1.0}}},
      {"min_member_mm", json{{"min_member_mm", 2.0}}},
      {"build_direction",
       json{{"build_direction", json::array({0.0, 0.0, 1.0})}}},
      {"overhang_angle_deg", json{{"overhang_angle_deg", 45.0}}},
      {"overhang_penalty",
       json{{"build_direction", json::array({0.0, 0.0, 1.0})},
            {"overhang_penalty", 1.0}}},
      {"no_enclosed_voids", json{{"no_enclosed_voids", true}}},
      {"result_dir", json{{"result_dir", "/probe"}}}};
  return probes;
}

bool accepted(const json &topology, std::string *reason) {
  try {
    const Settings s = parse_settings(topology);
    const Compatibility c = check_compatibility(s);
    if (!c.supported && reason)
      *reason = c.message;
    return c.supported;
  } catch (const ModelError &e) {
    if (reason)
      *reason = e.what();
    return false;
  }
}

json defaults_for(const std::vector<std::string> &offered) {
  const Settings d{};
  const json all = {{"max_iterations", d.max_iterations},
                    {"change_tolerance", d.change_tolerance},
                    {"projection_beta", d.projection_beta},
                    {"penalty", d.penalty},
                    {"ramp_q", d.ramp_q},
                    {"min_density", d.min_density},
                    {"move_limit", d.move_limit},
                    {"density_threshold", d.density_threshold},
                    {"overhang_angle_deg", d.overhang_angle_deg},
                    {"overhang_penalty", d.overhang_penalty},
                    {"no_enclosed_voids", d.no_enclosed_voids},
                    {"preserve_thickness_mm", d.preserve_thickness_mm},
                    {"exclude_thickness_mm", d.exclude_thickness_mm},
                    {"beso_evolution_rate", d.beso_evolution_rate},
                    {"level_set_regularization", d.level_set_regularization}};
  json out = json::object();
  for (const auto &key : offered)
    if (all.contains(key))
      out[key] = all[key];
  return out;
}

json combination(Method m, PhysicsKind p) {
  json base = {{"physics", to_string(p)},
               {"method", to_string(m)},
               {"volume_fraction", 0.5}};
  json c = {{"method", to_string(m)},
            {"physics", to_string(p)},
            {"volume_phase", to_string(design_phase(p))}};
  std::string reason;
  if (!accepted(base, &reason)) {
    c["supported"] = false;
    c["reason"] = reason.empty() ? "unsupported by the optimizer" : reason;
    return c;
  }
  json objectives = json::array();
  std::string objective_reason;
  for (ObjectiveKind o : objectives_for(p)) {
    json t = base;
    t["objective"] = to_string(o);
    if (o == ObjectiveKind::Mass)
      t["stress_limit_mpa"] = 100.0; // mass needs an explicit limit
    std::string why;
    if (accepted(t, &why))
      objectives.push_back(to_string(o));
    else if (objective_reason.empty())
      objective_reason = std::string(to_string(o)) + ": " + why;
  }
  json limits = json::array();
  for (const auto &[key, value] : limit_probes()) {
    json t = base;
    t[key] = value;
    if (accepted(t, nullptr))
      limits.push_back(key);
  }
  std::vector<std::string> params;
  for (const auto &[key, extra] : parameter_probes()) {
    json t = base;
    for (auto it = extra.begin(); it != extra.end(); ++it)
      t[it.key()] = it.value();
    if (accepted(t, nullptr))
      params.push_back(key);
  }
  const Settings probe = parse_settings(base);
  const PhysicsText text = physics_text(p);
  c["supported"] = !objectives.empty();
  if (objectives.empty())
    c["reason"] = "no objective is supported: " + objective_reason;
  c["formulation"] = std::string(method_label(m)) + " [" +
                     interpolation_label(probe) + "] on " + text.formulation;
  c["objectives"] = std::move(objectives);
  c["limits"] = std::move(limits);
  c["parameters"] = params;
  c["defaults"] = defaults_for(params);
  c["default_notes"] = {
      {"filter_radius_mm", "absent: min_member_mm / 2, else 2 x median element "
                           "size"},
      {"volume_fraction", std::string("required; budgets the ") +
                              to_string(design_phase(p)) + " phase"}};
  return c;
}

// ── Result building helpers
// ──────────────────────────────────────────────────
json history_json(const OptimizerState &st, ObjectiveKind k) {
  json h = json::object();
  std::vector<int> iteration, added, removed, rejected, trials, voids;
  std::vector<double> objective, scaled, merit, vf, vt, gmax, change, beta,
      gray, overhang, iface, dual, seconds, feasible;
  std::vector<std::vector<double>> g(st.constraint_names.size());
  for (const IterationRecord &r : st.history) {
    iteration.push_back(r.iteration);
    objective.push_back(objective_report(k, r.objective));
    scaled.push_back(r.objective_scaled);
    merit.push_back(r.merit);
    vf.push_back(r.volume_fraction);
    vt.push_back(r.volume_target);
    gmax.push_back(r.max_constraint);
    change.push_back(r.change);
    beta.push_back(r.beta);
    gray.push_back(r.grayness);
    overhang.push_back(r.overhang);
    iface.push_back(units::m2_to_mm2(r.interface_area_m2));
    dual.push_back(r.dual_gradient);
    seconds.push_back(r.seconds);
    feasible.push_back(r.feasible ? 1.0 : 0.0);
    added.push_back(r.added);
    removed.push_back(r.removed);
    rejected.push_back(r.rejected);
    trials.push_back(r.line_search_trials);
    voids.push_back(r.enclosed_void_elements);
    for (std::size_t i = 0; i < g.size(); ++i)
      g[i].push_back(i < r.constraints.size()
                         ? r.constraints[i]
                         : std::numeric_limits<double>::quiet_NaN());
  }
  h["iteration"] = iteration;
  h["objective"] = objective;
  h["objective_scaled"] = scaled;
  h["merit"] = merit;
  h["volume_fraction"] = vf;
  h["volume_target"] = vt;
  h["max_constraint"] = gmax;
  h["change"] = change;
  h["beta"] = beta;
  h["grayness"] = gray;
  h["overhang"] = overhang;
  h["interface_area_mm2"] = iface;
  h["dual_gradient"] = dual;
  h["seconds"] = seconds;
  h["feasible"] = feasible;
  h["added"] = added;
  h["removed"] = removed;
  h["rejected"] = rejected;
  h["line_search_trials"] = trials;
  h["enclosed_void_elements"] = voids;
  for (std::size_t i = 0; i < g.size(); ++i) {
    json series = json::array();
    for (double v : g[i])
      series.push_back(std::isfinite(v) ? json(v) : json(nullptr));
    h["g:" + st.constraint_names[i]] = std::move(series);
  }
  return h;
}

json check_row(const char *id, const char *label, json requested, json measured,
               const char *units, const char *status, std::string note) {
  json row = {{"id", id},
              {"label", label},
              {"requested", std::move(requested)},
              {"measured", std::move(measured)},
              {"status", status}};
  if (units && units[0])
    row["units"] = units;
  if (!note.empty())
    row["note"] = std::move(note);
  return row;
}

std::string fmt_g(double v) {
  char buf[48];
  std::snprintf(buf, sizeof(buf), "%.6g", v);
  return buf;
}

json manufacturing_json(const Settings &s, const OptimizationResult &r,
                        const Reconstruction *rec, const ReanalysisResult *rea,
                        double tolerance) {
  json checks = json::array();
  const ManufacturingReport &m = r.manufacturing;
  if (rec) {
    const SurfaceEvidence &se = rec->solid.surface.evidence;
    checks.push_back(check_row(
        "surface_manifold", "Closed manifold oriented surface", true,
        se.manifold, "", se.manifold ? "pass" : "fail",
        std::string("surface state ") + to_string(se.state) +
            "; clipped boundary matches surface: " +
            (rec->solid.volume.boundary_matches_surface ? "yes" : "no")));
    checks.push_back(check_row("single_body", "Single solid body", 1, se.bodies,
                               "", se.bodies == 1 ? "pass" : "fail",
                               "bodies counted on the reconstructed surface"));
    const json &pg = rec->evidence["passive_geometry"];
    checks.push_back(check_row(
        "passive_regions",
        "Fixed-solid / fixed-void regions on the reconstructed geometry",
        json{{"fixed_solid_elements", pg["fixed_solid"]["elements"]},
             {"fixed_void_elements", pg["fixed_void"]["elements"]}},
        json{{"missing_solid_mm3", pg["fixed_solid"]["missing_volume_mm3"]},
             {"solid_in_void_mm3", pg["fixed_void"]["solid_volume_mm3"]}},
        "mm3", rec->passive_satisfied ? "pass" : "fail",
        "measured per element on the clipped volume, not inferred from "
        "relaxed densities"));
    // A cavity count is only meaningful when every shell was classified:
    // touching/crossing shells or inconsistent nesting leave it unproven.
    const bool classified = se.intersecting_shells == 0 &&
                            se.inconsistent_nesting == 0 &&
                            se.unenclosed_cavities == 0;
    checks.push_back(check_row(
        "enclosed_voids", "Enclosed voids", s.no_enclosed_voids,
        json{{"cavities", se.cavities},
             {"intersecting_shells", se.intersecting_shells},
             {"inconsistent_nesting", se.inconsistent_nesting},
             {"unenclosed_cavities", se.unenclosed_cavities}},
        "", classified && se.cavities == 0 ? "pass" : "fail",
        std::string(classified ? "cavities measured on the reconstructed "
                                 "surface"
                               : "shell classification incomplete on the "
                                 "reconstructed surface (intersecting shells, "
                                 "inconsistent nesting or unenclosed "
                                 "cavities); absence of voids is not proven") +
            "; element flood fill at the threshold found " +
            std::to_string(m.enclosed.regions) + " region(s); enforcement " +
            (s.no_enclosed_voids ? m.enclosed_void_enforcement
                                 : std::string("not requested"))));
  } else {
    checks.push_back(check_row(
        "surface_manifold", "Closed manifold oriented surface", true, nullptr,
        "", "not_measured", "no reconstruction for this termination"));
  }
  if (m.overhang_evaluated) {
    checks.push_back(check_row(
        "overhang", "Unsupported overhang (heuristic)", m.overhang_angle_deg,
        m.unsupported_volume_fraction, "fraction",
        m.unsupported_volume_fraction == 0.0 ? "pass" : "fail",
        "build-direction cone heuristic on the thresholded design mesh (" +
            m.overhang_formulation +
            "); does not guarantee a support-free part"));
  } else if (s.build_direction) {
    checks.push_back(check_row("overhang", "Unsupported overhang (heuristic)",
                               s.overhang_angle_deg, nullptr, "deg",
                               "not_measured", "overhang not evaluated"));
  }
  if (s.min_member_mm)
    checks.push_back(check_row("min_member", "Minimum member size",
                               *s.min_member_mm, nullptr, "mm", "not_measured",
                               "regularization intent via filter radius " +
                                   fmt_g(units::m_to_mm(m.filter_radius_m)) +
                                   " mm (" + m.filter_radius_source +
                                   "); member thickness is not measured"));
  if (s.symmetry_axes[0] || s.symmetry_axes[1] || s.symmetry_axes[2]) {
    json axes = json::array();
    for (int a = 0; a < 3; ++a)
      if (s.symmetry_axes[a])
        axes.push_back(std::string(1, "xyz"[a]));
    checks.push_back(check_row(
        "symmetry", "Mirror symmetry ties", axes,
        units::m_to_mm(m.max_tie_distance_m), "mm", "pass",
        "design variables tied (" + std::to_string(m.tied_groups) +
            " groups); measured value is the largest accepted mirror-image "
            "centroid mismatch"));
  }
  if (s.extrusion_axis >= 0)
    checks.push_back(check_row(
        "extrusion", "Extrusion ties", std::string(1, "xyz"[s.extrusion_axis]),
        m.tied_groups, "groups", "pass",
        "design variables tied along the axis in in-plane bins"));
  if (rea) {
    if (rea->evaluated) {
      double worst = 0.0;
      for (const Constraint &c : rea->evaluation.constraints)
        worst = std::max(worst, c.value);
      checks.push_back(check_row(
          "reconstruction_response",
          "Response limits on the reconstructed geometry", tolerance, worst,
          "g", worst <= tolerance ? "pass" : "fail",
          "independent reanalysis of the " + rea->domain + " domain"));
    } else {
      checks.push_back(
          check_row("reconstruction_response",
                    "Response limits on the reconstructed geometry", tolerance,
                    nullptr, "g", "fail", rea->code + ": " + rea->message));
    }
  }
  return {{"checks", std::move(checks)},
          {"geometry_valid", rec ? rec->geometry_valid : false},
          {"dfam",
           {{"checked", false},
            {"passed", false},
            {"summary", "DFAM checks are not run by the native solver"}}},
          {"print_ready", false},
          {"enforcement", to_json(m)}};
}

// ── Failure packet
// ───────────────────────────────────────────────────────────
json failure_packet(const std::string &code, const std::string &message,
                    const json &study, const char *termination) {
  json r = {{"ok", false},
            {"converged", false},
            {"feasible", false},
            {"termination", termination},
            {"termination_detail", message},
            {"error", message},
            {"error_code", code}};
  if (code.rfind("build_plate_", 0) == 0)
    r["field"] = "build_plate";
  if (study.is_object() && study.contains("topology") &&
      study["topology"].is_object()) {
    const json &t = study["topology"];
    if (t.contains("method"))
      r["method"] = t["method"];
    if (t.contains("physics"))
      r["physics"] = t["physics"];
  }
  return r;
}

json provenance_base(const json &study) {
  json p = {{"engine_build_id", CHUDWARE_HOSTD_BUILD_ID},
            {"polymesh_commit", CHUDWARE_POLYMESH_COMMIT},
            {"solver", "chudware/topology"},
            {"timestamp_utc", utc_stamp("%Y-%m-%dT%H:%M:%SZ")},
            {"units",
             {{"length", "mm"},
              {"force", "N"},
              {"stress", "MPa"},
              {"temperature", "C"},
              {"pressure", "Pa"},
              {"power", "W"},
              {"mass", "g"},
              {"internal", "SI (m, Pa, N, W, K)"}}}};
  if (study.is_object() && study.contains("job_id") &&
      study["job_id"].is_string())
    p["job_id"] = study["job_id"];
  const double rss = peak_rss_gb();
  if (std::isfinite(rss)) {
    p["peak_rss_gb"] = rss;
    p["peak_rss_scope"] = "solver_process_lifetime";
  }
  return p;
}

} // namespace

// ── Public: validation / resolution / preparation
// ────────────────────────────
namespace topology {

Settings validate_topology_envelope(const json &study) {
  if (!study.is_object())
    refuse("invalid_input", "study must be an object");
  for (const char *key : {"contact", "interfaces", "bond_gap_mm"})
    if (study.contains(key))
      refuse("contact_unsupported",
             std::string("study.") + key +
                 ": topology optimizes one conforming continuum; contact and "
                 "bonded interfaces are not supported");
  for (auto it = study.begin(); it != study.end(); ++it)
    if (!envelope_keys().count(it.key()))
      refuse("unknown_key", "study." + it.key() +
                                " is not a topology run key (not dropped "
                                "silently)");
  if (study.contains("type") &&
      !(study["type"].is_string() && study["type"] == "topology"))
    refuse("invalid_input", "study.type must be \"topology\"");

  // Mesh source and order.
  const bool has_step = study.contains("step_path");
  const bool has_inline = study.contains("points") || study.contains("tets") ||
                          study.contains("nodes_per_elem");
  if (has_step && has_inline)
    refuse("ambiguous_mesh_source",
           "give either step_path or inline points/tets, not both");
  if (!has_step && !has_inline)
    refuse("mesh_source_missing",
           "study needs a step_path or inline points + tets (tet4)");
  if (has_step) {
    if (!study["step_path"].is_string() ||
        study["step_path"].get<std::string>().empty())
      refuse("invalid_input", "study.step_path must be a non-empty string");
    std::error_code ec;
    if (!fs::is_regular_file(study["step_path"].get<std::string>(), ec))
      refuse("source_unreadable", "step_path " +
                                      study["step_path"].get<std::string>() +
                                      " is not a readable file");
  } else {
    if (!study.contains("points") || !study["points"].is_array() ||
        study["points"].empty() || !study.contains("tets") ||
        !study["tets"].is_array() || study["tets"].empty())
      refuse("invalid_mesh",
             "inline meshes need non-empty flat points and tets arrays");
    if (study.contains("nodes_per_elem") &&
        !(study["nodes_per_elem"].is_number_integer() &&
          study["nodes_per_elem"].get<long long>() == 4))
      refuse("unsupported_element_order",
             "topology optimization uses tet4 elements; nodes_per_elem " +
                 study["nodes_per_elem"].dump() + " is refused, not converted");
  }
  for (const char *key : {"mesh_order", "order"})
    if (study.contains(key) &&
        !(study[key].is_number_integer() && study[key].get<long long>() == 1))
      refuse("unsupported_element_order",
             std::string("topology optimization meshes tet4 (mesh_order 1); ") +
                 key + " " + study[key].dump() + " is refused, not changed");
  for (const auto &[alias, key] : kMeshAliases)
    if (study.contains(alias) && study.contains(key) &&
        study[alias] != study[key])
      refuse("invalid_input",
             std::string("study.") + alias + " and " + key + " disagree");
  for (const char *key :
       {"fineness", "target_size_mm", "min_size_mm", "max_size_mm",
        "mesh_target_mm", "mesh_fineness", "mesh_min_mm"})
    if (study.contains(key) &&
        !(study[key].is_number() && std::isfinite(study[key].get<double>()) &&
          study[key].get<double>() >= 0.0))
      refuse("invalid_input",
             std::string("study.") + key + " must be a finite number >= 0");

  // Settings and capability.
  if (!study.contains("topology") || !study["topology"].is_object())
    refuse("section_missing", "study.topology is required");
  const Settings s =
      parse_settings(study["topology"], study.contains("build_plate"));
  const Compatibility compat = check_compatibility(s);
  if (!compat.supported)
    refuse(compat.code.empty() ? "method_physics_unsupported" : compat.code,
           compat.message);
  if (!s.result_dir.empty() && !fs::path(s.result_dir).is_absolute())
    refuse("invalid_result_dir",
           "topology.result_dir must be an absolute path, got '" +
               s.result_dir + "'");

  // Material (resolved physical object).
  if (!study.contains("material") || !study["material"].is_object())
    refuse("section_missing",
           "study.material must be a resolved object of physical keys "
           "(E_mpa, nu, density_g_cm3, k_w_mk, ...)");
  require_material(convert_material(parse_material(study["material"])),
                   s.physics);

  // Section applicability.
  const PhysicsKind p = s.physics;
  const char *ps = to_string(p);
  const bool structural_input = is_present(study, "fixtures") ||
                                is_present(study, "loads") ||
                                is_present(study, "load_cases");
  if (structural_input && !uses_structural_boundary(p))
    refuse("section_not_applicable",
           std::string("fixtures/loads/load_cases do not apply to physics ") +
               ps + "; remove them or switch physics");
  if (p == PhysicsKind::Modal &&
      (is_present(study, "loads") || is_present(study, "load_cases")))
    refuse("section_not_applicable",
           "modal topology uses fixtures only; remove loads/load_cases");
  if (is_present(study, "loads") && is_present(study, "load_cases"))
    refuse("ambiguous_loads",
           "give top-level loads OR load_cases (with their own loads), not "
           "both");
  if (uses_structural_boundary(p)) {
    bool supported = is_present(study, "fixtures");
    if (!supported && is_present(study, "load_cases")) {
      supported = true;
      for (const auto &c : study["load_cases"])
        supported = supported && c.is_object() && is_present(c, "fixtures");
    }
    if (!supported)
      refuse("section_missing", std::string("physics ") + ps +
                                    " needs fixtures (shared or in "
                                    "every load case)");
  }
  if ((p == PhysicsKind::Structural || p == PhysicsKind::Buckling) &&
      !is_present(study, "loads") && !is_present(study, "load_cases"))
    refuse("section_missing",
           std::string("physics ") + ps + " needs loads or load_cases");
  if (is_present(study, "load_cases"))
    for (std::size_t c = 0; c < study["load_cases"].size(); ++c) {
      const json &lc = study["load_cases"][c];
      if (p != PhysicsKind::Thermomechanical &&
          !(lc.is_object() && is_present(lc, "loads")))
        refuse("section_missing",
               "load case " + std::to_string(c + 1) + " has no loads");
    }
  if (is_present(study, "thermal") && !uses_thermal_boundary(p))
    refuse("section_not_applicable", std::string("thermal does not apply to "
                                                 "physics ") +
                                         ps + "; remove it or switch physics");
  if (uses_thermal_boundary(p) &&
      !(study.contains("thermal") && study["thermal"].is_object()))
    refuse("section_missing",
           std::string("physics ") + ps + " needs a thermal section");
  if (is_present(study, "flow") && !uses_flow_boundary(p))
    refuse("section_not_applicable", std::string("flow does not apply to "
                                                 "physics ") +
                                         ps + "; remove it or switch physics");
  if (uses_flow_boundary(p) &&
      !(study.contains("flow") && study["flow"].is_object()))
    refuse("section_missing",
           std::string("physics ") + ps + " needs a flow section");
  if (uses_flow_boundary(p)) {
    const json &fl = study["flow"];
    for (const char *key : {"inlets", "outlets"})
      if (!is_present(fl, key))
        refuse("section_missing", std::string("flow needs at least one ") +
                                      (key[0] == 'i' ? "inlet" : "outlet"));
  }
  if (uses_thermal_boundary(p)) {
    const json &th = study["thermal"];
    bool heat_input = false, convection = false;
    if (th.contains("loads") && th["loads"].is_array())
      for (const auto &l : th["loads"]) {
        const std::string type =
            l.is_object() && l.contains("type") && l["type"].is_string()
                ? l["type"].get<std::string>()
                : std::string();
        heat_input = heat_input || type == "flux" || type == "power";
        convection = convection || type == "convection";
      }
    if ((p == PhysicsKind::Thermal || p == PhysicsKind::Cht) && !heat_input)
      refuse("zero_load", std::string("physics ") + ps +
                              " needs a flux or power heat input in "
                              "thermal.loads");
    const bool fixed = is_present(th, "fixtures") ||
                       (p == PhysicsKind::Cht && uses_flow_boundary(p));
    if (!fixed && !convection)
      refuse("thermal_unconstrained",
             "the temperature field needs a fixed temperature or a "
             "convection boundary in thermal");
  }

  // Every entry's shape, plus the typed parser on the normalized form (node
  // lists empty until the mesh exists).
  const json dry =
      normalize_envelope(study, study["topology"], p, nullptr, true, nullptr);
  (void)parse_problem_spec(dry);
  return s;
}

json resolve_topology_boundaries(const json &study, const json &topology,
                                 const PreparedMesh &mesh, bool include_regions,
                                 json *evidence) {
  if (mesh.nodes_per_elem != 4)
    refuse("unsupported_element_order",
           "boundary resolution needs a tet4 mesh");
  const Settings s =
      parse_settings(topology, include_regions && study.contains("build_plate"));
  return normalize_envelope(study, topology, s.physics, &mesh, include_regions,
                            evidence);
}

PreparedTopology prepare_topology_study(const json &study) {
  PreparedTopology out;
  out.settings = validate_topology_envelope(study);
  std::string error;
  PrepareMeshOptions options;
  options.context = "topology";
  out.source_mesh = prepare_fea_mesh(mesh_study_of(study), error, options);
  if (!out.source_mesh)
    refuse("mesh_preparation_failed", error);
  if (out.source_mesh->nodes_per_elem != 4)
    refuse("unsupported_element_order",
           "the mesher returned " +
               std::to_string(out.source_mesh->nodes_per_elem) +
               "-node elements; topology optimization requires tet4");
  out.mesh = build_tet_mesh(out.source_mesh->points, out.source_mesh->tets, 4);
  out.normalized = resolve_topology_boundaries(
      study, study["topology"], *out.source_mesh, true, &out.boundary_evidence);
  out.problem = prepare_problem(out.mesh, parse_problem_spec(out.normalized));
  out.settings = out.problem->settings;
  return out;
}

} // namespace topology

// ── Capabilities
// ─────────────────────────────────────────────────────────────
json topology_capabilities() {
  try {
    const Method methods[] = {Method::Simp, Method::Ramp, Method::Beso,
                              Method::LevelSet};
    const PhysicsKind physics[] = {
        PhysicsKind::Structural, PhysicsKind::Thermal,
        PhysicsKind::Modal,      PhysicsKind::Buckling,
        PhysicsKind::Flow,       PhysicsKind::Thermomechanical,
        PhysicsKind::Cht};
    json out = {{"schema", "chudware.topology_capabilities/1"}, {"ok", true}};
    json ms = json::array();
    for (Method m : methods)
      ms.push_back({{"id", to_string(m)},
                    {"label", method_label(m)},
                    {"formulation", method_formulation(m)}});
    json ps = json::array();
    for (PhysicsKind p : physics) {
      const PhysicsText t = physics_text(p);
      json objectives = json::array();
      for (ObjectiveKind o : objectives_for(p))
        objectives.push_back(
            {{"id", to_string(o)},
             {"units", objective_unit(o)},
             {"sense", objective_sense(o) == ObjectiveSense::Minimize
                           ? "minimize"
                           : "maximize"}});
      ps.push_back({{"id", to_string(p)},
                    {"label", t.label},
                    {"formulation", t.formulation},
                    {"validity", t.validity},
                    {"volume_phase", to_string(design_phase(p))},
                    {"objectives", std::move(objectives)},
                    {"sections",
                     {{"structural", uses_structural_boundary(p)},
                      {"structural_loads",
                       uses_structural_boundary(p) && p != PhysicsKind::Modal},
                      {"thermal", uses_thermal_boundary(p)},
                      {"flow", uses_flow_boundary(p)}}}});
    }
    json combos = json::array();
    for (Method m : methods)
      for (PhysicsKind p : physics)
        combos.push_back(combination(m, p));
    out["methods"] = std::move(ms);
    out["physics"] = std::move(ps);
    out["combinations"] = std::move(combos);
    out["mesh"] = {{"element", "tet4"},
                   {"mesh_order", 1},
                   {"note", "other element orders are refused, not converted"}};
    out["density_convention"] =
        "rho = 1 is solid; volume_fraction budgets the volume_phase";
    return out;
  } catch (const std::exception &e) {
    return {{"ok", false},
            {"error", std::string("topology_capabilities: ") + e.what()}};
  }
}

// ── Run
// ──────────────────────────────────────────────────────────────────────
nlohmann::json fea_run_topology(const nlohmann::json &study) {
  const auto started = std::chrono::steady_clock::now();
  auto elapsed = [&] {
    return std::chrono::duration<double>(std::chrono::steady_clock::now() -
                                         started)
        .count();
  };
  std::optional<RunDirectory> dir;
  json artifacts = json::object();
  json run_info;
  auto finish_failure = [&](json packet) {
    packet["summary"] = packet.value("summary", json::object());
    packet["summary"]["elapsed_s"] = elapsed();
    packet["provenance"] = provenance_base(study);
    if (dir) {
      packet["result_dir"] = dir->dir.string();
      const fs::path result_path = dir->dir / "result.json";
      artifacts["result"] = {{"path", result_path.string()},
                             {"format", "json"},
                             {"label", "Run result (this reply)"}};
      packet["artifacts"] = artifacts;
      try {
        write_new_file(result_path, packet.dump());
        sync_directory(dir->dir);
      } catch (const ModelError &e) {
        packet["artifacts"].erase("result");
        packet["result_write_error"] = e.what();
      }
    } else {
      packet["artifacts"] = json::object();
    }
    return packet;
  };

  try {
    SimJobScope job(study);
    const std::atomic<bool> *cancel = job.token().get();

    Settings settings;
    try {
      settings = validate_topology_envelope(study);
    } catch (const ModelError &e) {
      return finish_failure(
          failure_packet(e.code(), e.what(), study, "failed"));
    }
    const json &topology_json = study["topology"];
    const char *method_s = to_string(settings.method);
    const char *physics_s = to_string(settings.physics);

    // ── Durable run directory (before any costly work, after validation).
    dir = create_run_directory(settings);
    run_info = {{"schema", "chudware.topology.native_run/1"},
                {"run_id", dir->run_id},
                {"physics", physics_s},
                {"method", method_s},
                {"objective", to_string(settings.objective)},
                {"volume_phase", to_string(design_phase(settings.physics))},
                {"job_id", study.value("job_id", json(nullptr))},
                {"started_utc", utc_stamp("%Y-%m-%dT%H:%M:%SZ")},
                {"engine_build_id", CHUDWARE_HOSTD_BUILD_ID}};
    artifacts["run"] =
        artifact_json(write_new_file(dir->dir / "run.json", run_info.dump(2)),
                      "json", "Run identity");
    artifacts["inputs"] =
        artifact_json(write_new_file(dir->dir / "study.json", study.dump()),
                      "json", "Run envelope as received");
    const json source_before = source_identity(study, "before");

    // ── Prepare once.
    report_progress("topology.prepare");
    PreparedTopology prep;
    try {
      prep = prepare_topology_study(study);
    } catch (const ModelError &e) {
      return finish_failure(
          failure_packet(e.code(), e.what(), study, "failed"));
    }
    const Problem &problem = *prep.problem;
    settings = problem.settings;
    const TetMesh &mesh = *prep.mesh;
    const int E = mesh.element_count();
    artifacts["normalized_spec"] = artifact_json(
        write_new_file(dir->dir / "normalized_spec.json",
                       prep.normalized.dump()),
        "json", "Normalized resolved problem (parse_problem_spec input)");

    std::unique_ptr<Physics> physics;
    try {
      physics = make_topology_physics(prep.problem);
    } catch (const ModelError &e) {
      return finish_failure(
          failure_packet(e.code(), e.what(), study, "failed"));
    }
    if (job.cancelled())
      return finish_failure(
          failure_packet("cancelled", "cancelled before the first iteration",
                         study, "cancelled"));

    // ── Optimize with progress, history and checkpoints.
    const fs::path jsonl_path = dir->dir / "history.jsonl";
    std::FILE *jsonl = std::fopen(jsonl_path.string().c_str(), "wbx");
    if (!jsonl)
      refuse("artifact_io", "cannot create " + jsonl_path.string());
    struct Closer {
      std::FILE *&f;
      ~Closer() {
        if (f)
          std::fclose(f);
      }
    } closer{jsonl};
    std::string callback_error;
    double previous_objective = std::numeric_limits<double>::quiet_NaN();
    auto checkpoint_packet = [&](const OptimizerState &state,
                                 const Eigen::VectorXd &density) {
      return json{{"schema", "chudware.topology.native_checkpoint/1"},
                  {"run_id", dir->run_id},
                  {"method", method_s},
                  {"physics", physics_s},
                  {"objective", to_string(settings.objective)},
                  {"volume_phase", to_string(design_phase(settings.physics))},
                  {"iteration", state.iteration},
                  {"density_threshold", settings.density_threshold},
                  {"state", to_json(state)},
                  {"density", to_array(density)}};
    };
    OptimizerOptions options;
    options.cancel = cancel;
    options.checkpoint_interval = 5;
    options.progress = [&](const Progress &pr) {
      try {
        json line = to_json(pr.record);
        const std::string text = line.dump() + "\n";
        if (std::fwrite(text.data(), 1, text.size(), jsonl) != text.size() ||
            std::fflush(jsonl) != 0)
          refuse("artifact_io", "failed appending " + jsonl_path.string());
        if (pr.checkpoint)
          replace_own_file(dir->dir / "checkpoint.json",
                           checkpoint_packet(pr.state, pr.density).dump());
      } catch (const std::exception &e) {
        callback_error = e.what();
        throw;
      }
      if (progress_enabled()) {
        IterationProgress ip;
        ip.index = pr.record.iteration;
        ip.max_iterations = settings.max_iterations;
        ip.objective =
            objective_report(settings.objective, pr.record.objective);
        if (std::isfinite(previous_objective))
          ip.objective_change = std::abs(ip.objective - previous_objective) /
                                std::max(std::abs(previous_objective), 1e-300);
        previous_objective = ip.objective;
        ip.volume_fraction = pr.record.volume_fraction;
        ip.design_change = pr.record.change;
        ip.max_constraint = pr.record.max_constraint;
        json cs = json::object();
        for (std::size_t i = 0; i < pr.state.constraint_names.size() &&
                                i < pr.record.constraints.size();
             ++i)
          cs[pr.state.constraint_names[i]] = pr.record.constraints[i];
        ip.constraints = std::move(cs);
        ip.metrics = {{"volume_target", pr.record.volume_target},
                      {"grayness", pr.record.grayness},
                      {"beta", pr.record.beta},
                      {"seconds", pr.record.seconds}};
        report_iteration_progress("topology.iteration", ip,
                                  static_cast<std::size_t>(E));
      }
    };
    OptimizationResult r;
    try {
      r = optimize(prep.problem, *physics, options);
    } catch (const ModelError &e) {
      return finish_failure(
          failure_packet(e.code(), e.what(), study, "failed"));
    }
    std::fclose(jsonl);
    jsonl = nullptr;
    {
      std::uint64_t bytes = 0;
      const std::string sha = sha256_file(jsonl_path, &bytes);
      artifacts["history_jsonl"] = {{"path", jsonl_path.string()},
                                    {"format", "jsonl"},
                                    {"sha256", sha},
                                    {"bytes", bytes},
                                    {"label", "Per-iteration records (SI)"}};
    }
    const bool io_in_callback =
        !callback_error.empty() && r.termination == Termination::Failed;
    if (!r.state.history.empty() || r.density.size() == E)
      artifacts["checkpoint"] = artifact_json(
          replace_own_file(dir->dir / "checkpoint.json",
                           checkpoint_packet(r.state, r.density).dump()),
          "json",
          "Optimizer state checkpoint (no resume input; phi for level set)");

    const ObjectiveKind objective = settings.objective;
    const DesignPhase phase = design_phase(settings.physics);
    const double tol = options.feasibility_tolerance;
    const bool has_density = r.density.size() == E;

    json result = json::object();
    result["ok"] = r.ok;
    result["converged"] = r.ok && r.converged &&
                          r.termination == Termination::Converged && r.feasible;
    result["feasible"] = r.feasible;
    result["termination"] = to_string(r.termination);
    result["termination_detail"] = r.message;
    if (!r.ok) {
      result["error"] =
          io_in_callback ? r.message + " (" + callback_error + ")" : r.message;
      result["error_code"] = io_in_callback ? std::string("artifact_io")
                             : r.error_code.empty()
                                 ? std::string(to_string(r.termination))
                                 : r.error_code;
    }
    result["method"] = method_s;
    result["physics"] = physics_s;
    result["objective"] = to_string(objective);
    result["formulation"] = r.update_formulation + "; interpolation " +
                            r.interpolation + "; " + r.physics_formulation;
    result["run_id"] = dir->run_id;
    result["result_dir"] = dir->dir.string();

    // ── Summary (report units)
    json summary = json::object();
    summary["objective"] = to_string(objective);
    summary["objective_units"] = objective_unit(objective);
    summary["objective_sense"] =
        r.sense == ObjectiveSense::Minimize ? "minimize" : "maximize";
    summary["objective_baseline"] =
        r.has_baseline ? json(objective_report(objective, r.baseline_objective))
                       : json(nullptr);
    summary["objective_final"] =
        r.has_final ? json(objective_report(objective, r.final_objective))
                    : json(nullptr);
    summary["final_reevaluated"] = r.final_reevaluated;
    summary["fields_at"] = r.final_reevaluated ? "final_design"
                           : r.has_final       ? "last_completed_iteration"
                                               : "none";
    summary["volume_phase"] = to_string(phase);
    summary["volume_fraction_target"] = settings.volume_fraction;
    summary["volume_fraction_baseline"] =
        r.has_baseline && !r.state.history.empty()
            ? json(r.state.history.front().volume_fraction)
            : json(nullptr);
    summary["volume_fraction_final"] =
        has_density ? json(r.volume_fraction) : json(nullptr);
    if (problem.material.density_kg_m3) {
      const double rho_m = *problem.material.density_kg_m3;
      auto mass_g = [&](double phase_fraction) {
        const double solid =
            phase == DesignPhase::Solid ? phase_fraction : 1.0 - phase_fraction;
        return solid * mesh.total_volume_m3 * rho_m * 1e3;
      };
      if (!summary["volume_fraction_baseline"].is_null())
        summary["mass_baseline_g"] =
            mass_g(summary["volume_fraction_baseline"].get<double>());
      if (has_density)
        summary["mass_final_g"] = mass_g(r.volume_fraction);
    }
    summary["design_domain_volume_mm3"] =
        units::m3_to_mm3(mesh.total_volume_m3);
    summary["iterations"] = r.iterations;
    summary["evaluations"] = r.evaluations;
    summary["max_constraint_violation"] = r.max_violation;
    summary["convergence_criterion"] = r.convergence_criterion;
    summary["final_change"] = r.state.last_change;
    summary["feasibility_tolerance"] = tol;
    summary["volume_tolerance"] = r.volume_tolerance;
    summary["interpolation"] = r.interpolation;
    summary["update_formulation"] = r.update_formulation;
    summary["physics_formulation"] = r.physics_formulation;
    summary["design_variables"] = problem.design.variable_count;
    summary["elements"] = E;
    summary["nodes"] = mesh.node_count();
    summary["filter_radius_mm"] = units::m_to_mm(problem.filter_radius_m);
    summary["filter_radius_source"] = problem.filter_radius_source;
    if (problem.evidence["filter"].contains("min_member_filter_radius_mm")) {
      summary["min_member_filter_radius_mm"] =
          problem.evidence["filter"]["min_member_filter_radius_mm"];
      summary["mesh_floor_radius_mm"] =
          problem.evidence["filter"]["mesh_floor_radius_mm"];
    }
    summary["overhang_measure"] = r.final_overhang;
    summary["density_threshold"] = settings.density_threshold;

    result["history"] = history_json(r.state, objective);
    result["constraints"] =
        constraints_json(r.final_constraints, tol, r.volume_tolerance);

    // ── Fields at the final (re-evaluated) design, report units.
    std::vector<Field> fields;
    if (r.has_final)
      for (const Field &f : r.final_evaluation.fields)
        fields.push_back(report_field(f));
    json fields_out = json::array();
    for (const Field &f : fields)
      fields_out.push_back(field_json(f));
    result["fields"] = std::move(fields_out);
    if (has_density)
      result["density"] = to_array(r.density);
    if (settings.method == Method::LevelSet && r.level_set.size() == E)
      result["level_set"] = to_array(r.level_set);
    result["topology"] = topology_json;
    result["topology"]["overhang_angle_deg"] = settings.overhang_angle_deg;
    result["topology"]["overhang_penalty"] = settings.overhang_penalty;
    if (settings.build_direction) {
      const auto &d = *settings.build_direction;
      result["topology"]["build_direction"] = {d[0], d[1], d[2]};
    }
    result["evidence"] = {
        {"baseline", r.has_baseline ? r.baseline.evidence : json(nullptr)},
        {"final", r.has_final ? r.final_evaluation.evidence : json(nullptr)},
        {"optimizer", r.evidence},
        {"problem", problem.evidence},
        {"boundary_resolution", prep.boundary_evidence}};

    // ── Reconstruction + independent reanalysis of the actual geometry.
    std::optional<Reconstruction> rec;
    std::optional<ReanalysisResult> rea;
    bool reanalysis_cancelled = false;
    json reconstruction = json::object();
    if (r.ok && has_density) {
      report_progress("topology.reconstruct");
      try {
        rec = reconstruct_design(problem, r.density);
      } catch (const ModelError &e) {
        reconstruction = {
            {"status", "failed"}, {"code", e.code()}, {"message", e.what()}};
      }
      if (rec) {
        reconstruction = rec->evidence;
        reconstruction["status"] = "completed";
        report_progress("topology.reanalyze");
        BoundaryResolver resolver = [&study](const PreparedMesh &pm,
                                             const json &topo, json *ev) {
          return resolve_topology_boundaries(study, topo, pm, false, ev);
        };
        try {
          rea = reanalyze_reconstruction(problem, *rec, topology_json, resolver,
                                         cancel);
        } catch (const EvaluationCancelled &) {
          reanalysis_cancelled = true;
        }
        json ra = json::object();
        if (reanalysis_cancelled) {
          ra = {{"status", "cancelled"}};
        } else {
          ra = rea->evidence;
          ra["status"] = rea->status;
          if (!rea->code.empty()) {
            ra["code"] = rea->code;
            ra["message"] = rea->message;
          }
          if (rea->evaluated) {
            const double re =
                objective_report(objective, rea->evaluation.objective);
            ra["objective"] = re;
            ra["objective_units"] = objective_unit(objective);
            ra["relaxed_objective"] = summary["objective_final"];
            if (summary["objective_final"].is_number()) {
              const double relaxed = summary["objective_final"].get<double>();
              ra["relative_difference"] =
                  (re - relaxed) / std::max(std::abs(relaxed), 1e-300);
            }
            ra["constraints"] =
                constraints_json(rea->evaluation.constraints, tol, tol);
            double worst = 0.0;
            for (const Constraint &c : rea->evaluation.constraints)
              worst = std::max(worst, c.value);
            ra["max_constraint_violation"] = worst;
            ra["feasible"] = worst <= tol;
            json descriptors = json::array();
            for (const Field &f : rea->evaluation.fields) {
              const Field rf = report_field(f);
              descriptors.push_back(
                  {{"name", rf.name},
                   {"unit", rf.unit},
                   {"location",
                    rf.location == FieldLocation::Node ? "node" : "element"},
                   {"components", rf.components}});
            }
            ra["fields"] = std::move(descriptors);
            summary["reanalysis_objective"] = re;
            summary["reanalysis_feasible"] = worst <= tol;
          }
          summary["reanalysis_status"] = rea->status;
        }
        reconstruction["reanalysis"] = std::move(ra);
        summary["reconstructed_solid_fraction"] =
            rec->evidence["reconstructed_solid_fraction"];
        summary["geometry_valid"] = rec->geometry_valid;
      }
    } else {
      reconstruction = {
          {"status", "skipped"},
          {"reason", std::string("termination ") + to_string(r.termination) +
                         ": an incomplete design is not reconstructed"}};
    }
    result["reconstruction"] = std::move(reconstruction);
    if (reanalysis_cancelled) {
      result["ok"] = false;
      result["converged"] = false;
      result["termination"] = "cancelled";
      result["termination_detail"] =
          "cancelled during the reconstruction reanalysis; the optimized "
          "design, history and checkpoint are retained";
      result["error"] = result["termination_detail"];
      result["error_code"] = "cancelled";
    }
    result["manufacturing"] =
        manufacturing_json(settings, r, rec ? &*rec : nullptr,
                           rea && !reanalysis_cancelled ? &*rea : nullptr, tol);

    // ── Artifacts (exclusive, hashed; I/O failure fails the run).
    report_progress("topology.artifacts");
    json refusals = json::object();
    std::string io_error;
    auto attempt = [&](const char *key, const std::function<void()> &fn) {
      try {
        fn();
      } catch (const ModelError &e) {
        refusals[key] = {{"code", e.code()}, {"message", e.what()}};
        if ((e.code() == "artifact_io" || e.code() == "artifact_exists") &&
            io_error.empty())
          io_error = std::string(key) + ": " + e.what();
      }
    };
    attempt("history", [&] {
      artifacts["history"] = artifact_json(
          write_new_file(dir->dir / "history.json", result["history"].dump()),
          "json", "Optimization history (report units)");
    });
    if (has_density)
      attempt("density_vtu", [&] {
        std::vector<Field> vtu = fields;
        if (settings.method == Method::LevelSet && r.level_set.size() == E) {
          Field phi;
          phi.name = "level_set";
          phi.unit = "-";
          phi.location = FieldLocation::Element;
          phi.components = 1;
          phi.values = r.level_set;
          vtu.push_back(std::move(phi));
        }
        artifacts["density_vtu"] = artifact_json(
            write_density_vtu((dir->dir / "density.vtu").string(), mesh,
                              r.density, settings.density_threshold, vtu),
            "vtu", "Element density and final fields (mm, MPa, C)");
      });
    PrintSurfaceEvidence print_evidence;
    print_evidence.reason = "no geometry-valid reconstruction";
    if (rec) {
      const SurfaceMesh &surface = rec->solid.surface.mesh;
      std::optional<SurfaceMesh> print_surface;
      if (rec->geometry_valid) {
        attempt("surface_stl", [&] {
          artifacts["surface_stl"] = artifact_json(
              write_stl((dir->dir / "surface.stl").string(), surface,
                        "chudware topology " + dir->run_id),
              "stl", "Reconstructed solid surface (validated)");
        });
        attempt("surface_print_stl", [&] {
          try {
            SurfaceMesh candidate =
                smooth_print_surface(surface, PrintSurfaceOptions{}, print_evidence);
            artifacts["surface_print_stl"] = artifact_json(
                write_stl((dir->dir / "surface_print.stl").string(), candidate,
                          "chudware topology print " + dir->run_id),
                "stl", "Print surface (smoothed, validated)");
            print_surface = std::move(candidate);
          } catch (const ModelError &e) {
            print_evidence.state = "refused";
            print_evidence.reason = e.what();
            throw;
          }
        });
      } else {
        refusals["surface_stl"] = {
            {"code", "geometry_invalid"},
            {"message", "surface not exported: " +
                            rec->evidence["invalid_reasons"].dump()}};
      }
      const SurfaceMesh &displayed = print_surface ? *print_surface : surface;
      const char *surface_name = print_surface ? "print" : "reconstruction";
      std::optional<SurfaceOverhang> surface_overhang;
      if (settings.build_direction && displayed.triangle_count() > 0) {
        const Eigen::Vector3d &d = *settings.build_direction;
        double plate = std::numeric_limits<double>::infinity();
        for (int n = 0; n < mesh.node_count(); ++n)
          if (mesh.node_referenced[n])
            plate = std::min(plate, mesh.nodes_m.col(n).dot(d));
        const double plate_tol =
            1e-6 * std::max((mesh.bbox_max_m - mesh.bbox_min_m).norm(), 1e-300);
        surface_overhang = measure_surface_overhang(
            displayed, d, settings.overhang_angle_deg, plate, plate_tol);
        auto &overhang = result["manufacturing"]["enforcement"]["overhang"];
        overhang["surface_unsupported_area_mm2"] =
            surface_overhang->unsupported_area_m2 * 1e6;
        overhang["surface_unsupported_area_fraction"] =
            surface_overhang->total_area_m2 > 0
                ? surface_overhang->unsupported_area_m2 /
                      surface_overhang->total_area_m2
                : 0;
        overhang["surface_measured_on"] = surface_name;
        for (json &row : result["manufacturing"]["checks"])
          if (row["id"] == "overhang")
            row["note"] = row.value("note", std::string()) +
                          "; displayed " + surface_name + " surface needs " +
                          fmt_g(surface_overhang->unsupported_area_m2 * 1e6) +
                          " mm2 of overhang support";
      }
      if (surface.triangle_count() > 0)
        attempt("overlay_aicm", [&] {
          std::vector<SurfaceScalar> scalars;
          json skipped = json::array();
          auto add_scalar = [&](const std::string &name,
                                const std::string &unit,
                                const Eigen::VectorXd &nodal) {
            try {
              scalars.push_back(
                  {name, unit, interpolate_nodal_field(surface, nodal, 1)});
            } catch (const ModelError &e) {
              skipped.push_back({{"name", name}, {"reason", e.what()}});
            }
          };
          add_scalar("density", "-", nodal_average(mesh, r.density, 1));
          for (const Field &f : fields) {
            Eigen::VectorXd values;
            std::string name = f.name;
            if (f.components == 1) {
              values = f.values;
            } else {
              const Eigen::Index count = f.values.size() / f.components;
              values.resize(count);
              for (Eigen::Index i = 0; i < count; ++i)
                values[i] =
                    f.values.segment(i * f.components, f.components).norm();
              name += "_magnitude";
            }
            add_scalar(name, f.unit,
                       f.location == FieldLocation::Node
                           ? values
                           : nodal_average(mesh, values, 1));
          }
          if (surface_overhang)
            scalars.push_back({"overhang", "-", surface_overhang->vertex_fraction});
          ArtifactFile a =
              write_surface_aicm((dir->dir / "overlay.aicm").string(), displayed,
                                 scalars, "topology " + dir->run_id);
          json entry =
              artifact_json(a, "aicm", "Viewport density/field overlay");
          entry["surface"] = surface_name;
          if (!skipped.empty())
            entry["skipped_scalars"] = std::move(skipped);
          artifacts["overlay_aicm"] = std::move(entry);
          result["overlay_aicm"] = a.path;
        });
    }
    result["reconstruction"]["print_surface"] = to_json(print_evidence);
    if (rea && rea->evaluated)
      attempt("reanalysis_vtu", [&] {
        constexpr double kReanalysisRetained = 0.5;
        const std::vector<Field> rf =
            reanalysis_vtu_fields(*rea->mesh, rea->density, kReanalysisRetained,
                                  rea->evaluation.fields);
        json entry = artifact_json(
            write_density_vtu((dir->dir / "reanalysis.vtu").string(),
                              *rea->mesh, rea->density, kReanalysisRetained,
                              rf),
            "vtu", "Reanalysis of the reconstructed domain (mm, MPa, C)");
        for (const Field &f : rf)
          if (f.location == FieldLocation::Node &&
              base_name(f.name) == "von_mises") {
            entry["point_stress"] = kReanalysisPointStress;
            break;
          }
        artifacts["reanalysis_vtu"] = std::move(entry);
      });
    if (!refusals.empty())
      result["artifact_refusals"] = std::move(refusals);
    if (!io_error.empty()) {
      result["ok"] = false;
      result["error_code"] = "artifact_io";
      result["error"] = "artifact write failed: " + io_error;
    }

    // ── Provenance, mesh quality, source integrity.
    json source = source_before;
    try {
      const json after = source_identity(study, "after");
      source["sha256_after"] = after["sha256_after"];
      source["unchanged"] = after["sha256_after"] == source["sha256_before"];
    } catch (const ModelError &e) {
      source["sha256_after"] = nullptr;
      source["unchanged"] = false;
      source["after_error"] = e.what();
    }
    json provenance = provenance_base(study);
    {
      util::Sha256 mesh_sha;
      const auto &pts = prep.source_mesh->points;
      const auto &tets = prep.source_mesh->tets;
      mesh_sha.update(reinterpret_cast<const std::uint8_t *>(pts.data()),
                      pts.size() * sizeof(double));
      mesh_sha.update(reinterpret_cast<const std::uint8_t *>(tets.data()),
                      tets.size() * sizeof(int));
      json setup = json::object();
      for (const char *key : {"fixtures", "loads", "load_cases", "thermal",
                              "flow", "preserve", "exclude", "build_plate"})
        if (study.contains(key))
          setup[key] = study[key];
      provenance["hashes"] = {
          {"algorithm", "sha256"},
          {"parameters", sha256_json(topology_json)},
          {"material", sha256_json(study["material"])},
          {"physics_setup", sha256_json(setup)},
          {"normalized_spec", artifacts["normalized_spec"]["sha256"]},
          {"mesh", mesh_sha.finish_hex()}};
    }
    provenance["source"] = std::move(source);
    provenance["run_id"] = dir->run_id;
    provenance["result_dir"] = dir->dir.string();
    provenance["mesh_type_used"] = prep.source_mesh->mesh_type_used;
    provenance["mesh_notes"] = prep.source_mesh->notes;
    provenance["governor"] = prep.source_mesh->governor;
    provenance["update_formulation"] = r.update_formulation;
    provenance["physics_formulation"] = r.physics_formulation;
    result["provenance"] = std::move(provenance);
    json mesh_quality = prep.source_mesh->mesh_quality.is_object()
                            ? prep.source_mesh->mesh_quality
                            : json::object();
    mesh_quality["topology_mesh"] = {
        {"element", "tet4"},
        {"nodes", mesh.node_count()},
        {"elements", E},
        {"min_shape_quality", mesh.quality.min_shape_quality},
        {"mean_shape_quality", mesh.quality.mean_shape_quality},
        {"min_volume_mm3", units::m3_to_mm3(mesh.quality.min_volume_m3)},
        {"min_edge_mm", units::m_to_mm(mesh.quality.min_edge_m)},
        {"max_edge_mm", units::m_to_mm(mesh.quality.max_edge_m)},
        {"median_element_size_mm",
         units::m_to_mm(mesh.quality.median_element_size_m)},
        {"reoriented_elements", mesh.quality.reoriented_elements},
        {"unreferenced_nodes", mesh.quality.unreferenced_nodes},
        {"components", mesh.quality.components}};
    result["mesh_quality"] = std::move(mesh_quality);

    summary["elapsed_s"] = elapsed();
    result["summary"] = std::move(summary);

    const fs::path result_path = dir->dir / "result.json";
    artifacts["result"] = {{"path", result_path.string()},
                           {"format", "json"},
                           {"label", "Run result (this reply)"}};
    result["artifacts"] = artifacts;
    try {
      write_new_file(result_path, result.dump());
      sync_directory(dir->dir);
    } catch (const ModelError &e) {
      result["artifacts"].erase("result");
      result["ok"] = false;
      result["error_code"] = "artifact_io";
      result["error"] = std::string("result.json not written: ") + e.what();
    }
    return result;
  } catch (const ModelError &e) {
    return finish_failure(failure_packet(e.code(), e.what(), study, "failed"));
  } catch (const std::exception &e) {
    return finish_failure(failure_packet("internal_error",
                                         std::string("topology: ") + e.what(),
                                         study, "failed"));
  }
}

} // namespace sim
} // namespace chudware
