// Cross-platform geometry parity dump. The same executable runs on every release
// architecture; scripts/platform_parity.py compares its small JSON outputs.
#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <BRep_Builder.hxx>
#include <Bnd_Box.hxx>
#include <GProp_GProps.hxx>
#include <STEPControl_Writer.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Mat.hxx>
#include <gp_Pnt.hxx>

#include "aicm_loader.h"
#include "device/device_id.h"
#include "doc/document.h"
#include "doc/stepplus.h"
#include "mate/mate.h"
#ifdef CHUDWARE_WITH_NETGEN
#include "sim/fea_run.h"
#endif

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace {

constexpr const char* kSchema = "chudware.platform-parity/1";

fs::path corpus_path() {
    if (const char* override_path = std::getenv("CHUDWARE_PLATFORM_PARITY_CORPUS"))
        return fs::path(override_path);
    return fs::path(CHUDWARE_PLATFORM_PARITY_CORPUS);
}

json read_json(const fs::path& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open fixture " + path.string());
    try {
        return json::parse(input);
    } catch (const std::exception& exc) {
        throw std::runtime_error("cannot parse fixture " + path.string() + ": " + exc.what());
    }
}

std::vector<fs::path> fixture_paths() {
    const fs::path root = corpus_path();
    if (!fs::is_directory(root))
        throw std::runtime_error("platform parity corpus is not a directory: " + root.string());
    std::vector<fs::path> paths;
    for (const fs::directory_entry& entry : fs::directory_iterator(root))
        if (entry.is_regular_file() && entry.path().extension() == ".json")
            paths.push_back(entry.path());
    std::sort(paths.begin(), paths.end());
    if (paths.size() != 12)
        throw std::runtime_error("platform parity corpus must contain exactly 12 JSON fixtures; found " +
                                 std::to_string(paths.size()));
    return paths;
}

std::string sha256(std::string_view bytes) {
    return chudware::device::detail::sha256_hex(bytes);
}

std::string sha256(const std::vector<uint8_t>& bytes) {
    return sha256(std::string_view(reinterpret_cast<const char*>(bytes.data()), bytes.size()));
}

TopoDS_Compound compound_of(const std::vector<TopoDS_Shape>& shapes) {
    BRep_Builder builder;
    TopoDS_Compound compound;
    builder.MakeCompound(compound);
    for (const TopoDS_Shape& shape : shapes)
        if (!shape.IsNull()) builder.Add(compound, shape);
    return compound;
}

int topology_count(const TopoDS_Shape& shape, TopAbs_ShapeEnum kind) {
    TopTools_IndexedMapOfShape map;
    TopExp::MapShapes(shape, kind, map);
    return map.Extent();
}

json topology(const TopoDS_Shape& shape) {
    return {{"solids", topology_count(shape, TopAbs_SOLID)},
            {"faces", topology_count(shape, TopAbs_FACE)},
            {"edges", topology_count(shape, TopAbs_EDGE)},
            {"vertices", topology_count(shape, TopAbs_VERTEX)}};
}

json bbox(const TopoDS_Shape& shape) {
    Bnd_Box bounds;
    BRepBndLib::Add(shape, bounds, Standard_False);
    if (bounds.IsVoid()) return json::array({0.0, 0.0, 0.0, 0.0, 0.0, 0.0});
    bounds.SetGap(0.0);
    double x0, y0, z0, x1, y1, z1;
    bounds.Get(x0, y0, z0, x1, y1, z1);
    return json::array({x0, y0, z0, x1, y1, z1});
}

json mass_properties(const TopoDS_Shape& shape, double density) {
    GProp_GProps properties;
    BRepGProp::VolumeProperties(shape, properties);
    const double volume = properties.Mass();
    const gp_Pnt center = properties.CentreOfMass();
    const gp_Mat matrix = properties.MatrixOfInertia();
    json inertia = json::array();
    for (int row = 1; row <= 3; ++row)
        for (int column = 1; column <= 3; ++column)
            inertia.push_back(matrix.Value(row, column) * density);
    return {{"volume", volume},
            {"mass", volume * density},
            {"com", json::array({center.X(), center.Y(), center.Z()})},
            {"inertia", std::move(inertia)}};
}

std::uint64_t display_triangle_count(const chudware::doc::CompileResult& compiled) {
    render::DocMesh mesh;
    std::string error;
    if (!render::parseAicm(compiled.aicm.data(), compiled.aicm.size(), mesh, error))
        throw std::runtime_error("host AICM loader rejected parity document: " + error);
    std::uint64_t count = 0;
    for (const render::BodyMesh& body : mesh.bodies) count += body.n_tris;
    return count;
}

json string_or_strings(const json& feature, const char* key) {
    const auto it = feature.find(key);
    if (it == feature.end()) return json();
    if (it->is_string()) return json::array({*it});
    if (it->is_array()) return *it;
    return json();
}

json operation_tree(const json& fixture) {
    json features = json::array();
    static constexpr std::array<const char*, 7> dependency_keys = {
        "profile", "path", "body", "seed", "a", "b", "sections"};
    for (const json& feature : fixture.at("features")) {
        json node{{"id", feature.at("id")}, {"type", feature.at("type")}};
        json inputs = json::object();
        for (const char* key : dependency_keys) {
            json value = string_or_strings(feature, key);
            if (!value.is_null()) inputs[key] = std::move(value);
        }
        node["inputs"] = std::move(inputs);
        if (feature.value("type", std::string()) == "sketch") {
            node["entities"] = json::array();
            for (const json& entity : feature.value("entities", json::array()))
                node["entities"].push_back({{"id", entity.at("id")}, {"type", entity.at("type")}});
            node["constraints"] = json::array();
            for (const json& constraint : feature.value("constraints", json::array()))
                node["constraints"].push_back(
                    {{"id", constraint.at("id")}, {"type", constraint.at("type")},
                     {"refs", constraint.value("refs", json::array())}});
        }
        features.push_back(std::move(node));
    }

    json mates = json::array();
    if (fixture.contains("mate_input")) {
        for (const json& mate : fixture.at("mate_input").at("mates")) {
            json components = json::array();
            for (const json& ref : mate.at("refs")) components.push_back(ref.at("component"));
            mates.push_back({{"id", mate.at("id")}, {"type", mate.at("type")},
                             {"components", std::move(components)}});
        }
    }
    return {{"features", std::move(features)}, {"mates", std::move(mates)}};
}

std::string stepplus_hash(const json& fixture, const std::string& doc_name) {
    const fs::path path = fs::temp_directory_path() /
                          ("chudware-platform-parity-" + doc_name + ".step+");
    chudware::doc::stepplus_save(fixture, path.string(), {});
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("cannot read generated STEP+ " + path.string());
    const std::string bytes((std::istreambuf_iterator<char>(input)),
                            std::istreambuf_iterator<char>());
    input.close();
    std::error_code ignored;
    fs::remove(path, ignored);
    return sha256(bytes);
}

void exercise_assembly(const json& fixture, const std::string& doc_name) {
    if (!fixture.contains("mate_input")) return;
    const json solved = chudware::mate::solve_mates_json(fixture.at("mate_input"));
    if (!solved.value("converged", false))
        throw std::runtime_error(doc_name + ": three-mate assembly did not converge: " + solved.dump());
    if (fixture.at("mate_input").at("mates").size() != 3)
        throw std::runtime_error(doc_name + ": assembly fixture must contain exactly three mates");
}

#ifdef CHUDWARE_WITH_NETGEN
void exercise_netgen(const json& fixture, const TopoDS_Shape& shape, const std::string& doc_name) {
    if (!fixture.contains("fea")) return;
    const fs::path path = fs::temp_directory_path() /
                          ("chudware-platform-parity-" + doc_name + ".step");
    STEPControl_Writer writer;
    if (writer.Transfer(shape, STEPControl_AsIs) != IFSelect_RetDone ||
        writer.Write(path.string().c_str()) != IFSelect_RetDone)
        throw std::runtime_error(doc_name + ": cannot write STEP input for Netgen");
    json study = fixture.at("fea");
    study["step_path"] = path.string();
    const json solved = chudware::sim::fea_run_static(study);
    std::error_code ignored;
    fs::remove(path, ignored);
    if (!solved.value("ok", false))
        throw std::runtime_error(doc_name + ": Netgen FEA solve failed: " + solved.dump());
    if (solved.value("n_nodes", 0) <= 0 || solved.value("n_elems", 0) <= 0)
        throw std::runtime_error(doc_name + ": Netgen returned an empty FEA mesh");
}
#endif

json build_document(const fs::path& fixture_path) {
    const json fixture = read_json(fixture_path);
    const std::string doc_name = fixture.at("doc").get<std::string>();
    const chudware::doc::CompileResult compiled =
        chudware::doc::compile_document(fixture, json::object());
    if (!compiled.ok) {
        std::string failures;
        for (const auto& status : compiled.statuses)
            if (!status.ok) failures += status.id + " (" + status.type + "): " + status.error + "; ";
        throw std::runtime_error(doc_name + ": document compile failed: " + failures);
    }
    if (compiled.shapes.empty())
        throw std::runtime_error(doc_name + ": document compiled without surviving geometry");

    exercise_assembly(fixture, doc_name);
    const TopoDS_Compound shape = compound_of(compiled.shapes);
#ifdef CHUDWARE_WITH_NETGEN
    exercise_netgen(fixture, shape, doc_name);
#endif

    const double density = fixture.value("density_kg_mm3", 1.0);
    return {{"doc", doc_name},
            {"topology", topology(shape)},
            {"mass_props", mass_properties(shape, density)},
            {"bbox", bbox(shape)},
            {"op_tree", operation_tree(fixture)},
            {"aicm_sha256", sha256(compiled.aicm)},
            {"stepplus_sha256", stepplus_hash(fixture, doc_name)},
            {"mesh_tri_count", display_triangle_count(compiled)}};
}

json build_dump() {
    json documents = json::array();
    for (const fs::path& path : fixture_paths()) documents.push_back(build_document(path));
    std::sort(documents.begin(), documents.end(), [](const json& left, const json& right) {
        return left.at("doc").get_ref<const std::string&>() <
               right.at("doc").get_ref<const std::string&>();
    });
    return {{"schema", kSchema}, {"documents", std::move(documents)}};
}

int parity_dump(const char* out_path) {
    try {
        const json output = build_dump();
        std::ofstream stream(out_path);
        if (!stream) {
            std::cerr << "cannot open parity dump output: " << out_path << "\n";
            return 2;
        }
        stream << output.dump(2) << '\n';
        return stream ? 0 : 2;
    } catch (const std::exception& exc) {
        std::cerr << "platform parity dump failed: " << exc.what() << "\n";
        return 2;
    }
}

}  // namespace

TEST_CASE("canonical platform parity corpus builds") {
    const json dump = build_dump();
    CHECK(dump.at("schema") == kSchema);
    CHECK(dump.at("documents").size() == 12);
}

int main(int argc, char** argv) {
#ifndef CHUDWARE_WITH_NETGEN
    std::cout << "SKIP: test_platform_parity requires CHUDWARE_WITH_NETGEN=ON "
                 "because netgen_fea_bracket is a required canonical document\n";
    return 77;
#else
    if (argc == 3 && std::string(argv[1]) == "--parity-dump")
        return parity_dump(argv[2]);
    if (argc >= 2 && std::string(argv[1]) == "--parity-dump") {
        std::cerr << "usage: test_platform_parity --parity-dump <out.json>\n";
        return 2;
    }
    doctest::Context context(argc, argv);
    return context.run();
#endif
}
