#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "doc/scad.h"
#include "doc/scad_projection.h"
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#ifndef _WIN32
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
#endif

using chudware::doc::scad::json;
namespace scad = chudware::doc::scad;
namespace fs = std::filesystem;

namespace {
struct TempDir {
    fs::path path;
    TempDir() {
        path = fs::temp_directory_path() / ("chudware-scad-test-" + std::to_string(std::chrono::high_resolution_clock::now().time_since_epoch().count()));
        fs::create_directory(path);
    }
    ~TempDir() { std::error_code ec; fs::remove_all(path, ec); }
};
void put(const fs::path& path, const std::string& text) {
    fs::create_directories(path.parent_path());
    std::ofstream f(path, std::ios::binary); f << text;
}
json params(const std::string& source) {
    return {{"source", source}, {"files",json::array()}, {"parameters",json::object()},
            {"parameter_schema",json::array()}, {"mm_per_source_unit",1.0}};
}
json control(const json& inspected, const std::string& name) {
    for (const auto& c : inspected.at("controls")) if (c.at("name") == name) return c;
    return nullptr;
}
#ifndef _WIN32
struct BinEnv {
    bool existed; std::string old;
    explicit BinEnv(const std::string& value) {
        const char* previous = std::getenv("CHUDWARE_OPENSCAD_BIN"); existed = previous != nullptr;
        if (previous) old = previous;
        setenv("CHUDWARE_OPENSCAD_BIN", value.c_str(), 1);
    }
    ~BinEnv() { if (existed) setenv("CHUDWARE_OPENSCAD_BIN", old.c_str(), 1); else unsetenv("CHUDWARE_OPENSCAD_BIN"); }
};
#endif
}

TEST_CASE("SCAD inspection exposes only simple top-level literals and declared units") {
    auto p = params("// ignored = 2;\nwidth = 2; count = 3; flag = true; text = \"bolt\";\nmodule m(arg=9){hidden=5;}\ncomputed = width*2; cube(width);\n");
    p["mm_per_source_unit"] = 25.4;
    p["parameter_schema"] = json::array({{{"name","width"},{"kind","length"},{"label","Width"}}, {{"name","count"},{"kind","integer"}}});
    auto result = scad::inspect(p);
    REQUIRE(result.at("ok") == true);
    CHECK(control(result,"width").at("value").get<double>() == doctest::Approx(50.8));
    CHECK(control(result,"width").at("label") == "Width");
    CHECK(control(result,"count").at("kind") == "integer");
    CHECK(control(result,"flag").at("value") == true);
    CHECK(control(result,"text").at("value") == "bolt");
    CHECK(control(result,"hidden").is_null());
    CHECK(control(result,"computed").is_null());
    CHECK(control(result,"arg").is_null());
    p["parameters"]["width"] = {{"value", 12.7}, {"expr",nullptr}};
    CHECK(control(scad::inspect(p),"width").at("value").get<double>() == doctest::Approx(12.7));
    p["parameters"]["count"] = 3.5;
    CHECK(scad::inspect(p).at("ok") == false);
    p["parameters"].erase("count"); p["mm_per_source_unit"] = 0;
    CHECK(scad::inspect(p).at("ok") == false);
}

TEST_CASE("automatic controls discard later computed assignments") {
    auto p = params("size=10; size=2*10; count=1; count=7; cube(size);");
    auto status = scad::inspect(p);
    REQUIRE(status.at("ok") == true);
    CHECK(control(status, "size").is_null());
    CHECK(control(status, "count").at("value") == 7);
    p["parameter_schema"] = json::array({{{"name","size"},{"kind","length"}}});
    p["parameters"]["size"] = 25;
    status = scad::inspect(p);
    REQUIRE(status.at("ok") == true);
    CHECK(control(status, "size").at("value") == 25);
    CHECK(control(status, "size").at("overridden") == true);
}

TEST_CASE("SCAD exports preserve overrides and executable scale; metadata is checked") {
    TempDir tmp;
    auto p = params("width=1; flag=false; cube([width,1,1]);\n");
    p["mm_per_source_unit"] = 25.4;
    p["parameter_schema"] = json::array({{{"name","width"},{"kind","length"}}});
    p["parameters"] = {{"width",{{"value", 5.0},{"expr","W/2"}}},{"flag",true}};
    auto text = scad::export_source(p, {{"W", 25.4}});
    CHECK(text.find("scale([25.399999") != std::string::npos);
    CHECK(text.find("width = 0.5;") != std::string::npos);
    CHECK(text.find("flag = true;") != std::string::npos);
    auto path = tmp.path / "part.scad"; put(path,text);
    auto imported = scad::import_project(path.string());
    CHECK(imported["source"] == p["source"]);
    CHECK(imported["parameters"]["width"].get<double>() == doctest::Approx(12.7));
    text += "sphere(10);\n"; put(path,text);
    CHECK_THROWS(scad::import_project(path.string()));
}

TEST_CASE("managed import rejects executable values inconsistent with restored equations") {
    TempDir tmp;
    json doc = {{"schema","aicad.part/1"},
        {"equations", {{"globals", json::array({{{"name","W"},{"expr","10"}}})}}},
        {"features", json::array({{{"id","box"},{"type","box"},
            {"params",{{"length",{{"value",10},{"expr","W"}}},{"width",4},{"height",2}}}}})}};
    auto projected = scad::project_native(doc, 1.0);
    REQUIRE(projected.at("ok") == true);
    json p = projected.at("params");
    auto path = tmp.path / "managed.scad";
    put(path, scad::export_source(p, {{"W",10}}));
    auto imported = scad::import_project(path.string());
    CHECK(imported["parameters"]["p0_length"]["expr"] == "W");
    CHECK(imported["native_projection"] == p["native_projection"]);
    put(path, scad::export_source(p, {{"W",100}}));
    CHECK_THROWS(scad::import_project(path.string()));
}

TEST_CASE("explicit embedded dynamic dependencies export and reimport") {
    TempDir tmp;
    auto p = params("file_name=\"part.stl\"; import(file_name);");
    p["files"] = json::array({{{"path","part.stl"},{"data_b64","c3Rs"}}});
    auto path = tmp.path / "dynamic.scad";
    auto report = scad::export_project(p, path.string());
    CHECK(report["exported_files"].size() == 2);
    CHECK(scad::import_project(path.string()) == p);
    p["files"] = json::array();
    CHECK_THROWS(scad::export_source(p));
}

TEST_CASE("SCAD dependency capture is recursive, confined, and portable") {
    TempDir tmp;
    put(tmp.path / "part.scad", "include <lib/a.scad>\nmodule base(){cube(1);}\nbase();\n");
    put(tmp.path / "lib/a.scad", "use <../b.scad>\nmodule a(){b();}\n");
    put(tmp.path / "b.scad", "module b(){sphere(1);}\n");
    auto p = scad::import_project((tmp.path / "part.scad").string());
    REQUIRE(p["files"].size() == 2);
    TempDir output;
    auto report = scad::export_project(p, (output.path / "export.scad").string());
    CHECK(report["geometry_fidelity"] == "faceted");
    CHECK(report["history_recovered"] == false);
    CHECK(report["exported_files"].size() == 3);
    auto again = scad::import_project((output.path / "export.scad").string());
    CHECK(again == p);
    put(output.path / "lib/a.scad", "module a(){sphere(4);}");
    CHECK_THROWS(scad::import_project((output.path / "export.scad").string()));
    CHECK_THROWS(scad::export_project(p, (output.path / "export.scad").string()));
    put(tmp.path / "bad.scad", "include <../outside.scad>\n");
    CHECK_THROWS(scad::import_project((tmp.path / "bad.scad").string()));
    std::error_code symlink_error;
    fs::create_symlink(tmp.path / "b.scad", tmp.path / "link.scad", symlink_error);
    if (!symlink_error) {
        put(tmp.path / "symlink.scad", "include <link.scad>\n");
        CHECK_THROWS(scad::import_project((tmp.path / "symlink.scad").string()));
    }
    put(tmp.path / "dynamic.scad", "import(str(\"x\",\".stl\"));\n");
    CHECK_THROWS(scad::import_project((tmp.path / "dynamic.scad").string()));
}

TEST_CASE("source wrapper keeps included declarations and hoists nested use paths") {
    TempDir tmp;
    put(tmp.path / "part.scad", "include <lib/a.scad>\na();\n");
    put(tmp.path / "lib/a.scad", "use <../b.scad>\nmodule a(){ import(\"mesh.stl\"); b(); }\n");
    put(tmp.path / "lib/mesh.stl", "stl bytes");
    put(tmp.path / "b.scad", "module b(){cube(1);}\n");
    auto p = scad::import_project((tmp.path / "part.scad").string());
    const auto text = scad::export_source(p);
    CHECK(text.find("use <b.scad>\nmodule __chudware_source()") != std::string::npos);
    CHECK(text.find("module a(){ import(\"lib/mesh.stl\"); b(); }") != std::string::npos);
    CHECK(text.find("scale([1,1,1]) __chudware_source();") != std::string::npos);
    TempDir output;
    scad::export_project(p, (output.path / "out.scad").string());
    CHECK(scad::import_project((output.path / "out.scad").string()) == p);
}

TEST_CASE("SCAD project export rolls back new dependencies on conflict") {
    TempDir tmp;
    auto p = params("cube(1);");
    p["files"] = json::array({{{"path","nested/a.scad"},{"data_b64","YQ=="}},
                               {{"path","b.scad"},{"data_b64","Yg=="}}});
    put(tmp.path / "b.scad", "unrelated");
    CHECK_THROWS(scad::export_project(p, (tmp.path / "out.scad").string()));
    CHECK_FALSE(fs::exists(tmp.path / "nested/a.scad"));
    CHECK_FALSE(fs::exists(tmp.path / "out.scad"));
    std::ifstream original(tmp.path / "b.scad");
    std::string content; original >> content;
    CHECK(content == "unrelated");
}

#ifdef _WIN32
TEST_CASE("SCAD export rejects case-insensitive source and dependency collisions") {
    TempDir tmp;
    auto p = params("cube(1);");
    p["files"] = json::array({{{"path","OUT.scad"},{"data_b64","YQ=="}}});
    CHECK_THROWS(scad::export_project(p, (tmp.path / "out.scad").string()));
    CHECK_FALSE(fs::exists(tmp.path / "out.scad"));
}
#endif

#ifndef _WIN32
TEST_CASE("SCAD export refuses an existing FIFO without blocking") {
    TempDir tmp;
    auto fifo = tmp.path / "blocked.scad";
    REQUIRE(::mkfifo(fifo.c_str(), 0600) == 0);
    auto p = params("cube(1);");
    p["files"] = json::array({{{"path","blocked.scad"},{"data_b64","YQ=="}}});
    pid_t child = ::fork();
    REQUIRE(child >= 0);
    if (child == 0) {
        ::alarm(3);
        try { scad::export_project(p, (tmp.path / "out.scad").string()); }
        catch (...) { ::_exit(0); }
        ::_exit(1);
    }
    int status = 0;
    REQUIRE(::waitpid(child, &status, 0) == child);
    CHECK(WIFEXITED(status));
    if (WIFEXITED(status)) CHECK(WEXITSTATUS(status) == 0);
    CHECK_FALSE(fs::exists(tmp.path / "out.scad"));
}

TEST_CASE("SCAD missing renderer is honest and cache fingerprint detects replacement") {
    TempDir tmp;
    BinEnv configured((tmp.path / "missing").string());
    auto cap = scad::capability();
    CHECK(cap.at("available") == false);
    CHECK(cap.at("renderer_available") == false);
    CHECK(cap.at("error").get<std::string>().find("OpenSCAD") != std::string::npos);
    CHECK_THROWS(scad::build(params("cube(1);"), {}));
    CHECK(scad::cache_identity().empty());
    auto bin = tmp.path / "missing";
    put(bin,"first"); chmod(bin.c_str(),0700);
    auto first = scad::cache_identity(); REQUIRE_FALSE(first.empty());
    put(bin,"second, different length");
    CHECK(scad::cache_identity() != first);
}
#endif

TEST_CASE("SCAD export identity includes source, embedded inputs, unit scale and overrides") {
    auto p = params("n=1; cube(n);");
    auto base = scad::export_source(p);
    p["parameters"]["n"] = 2;
    auto changed_value = scad::export_source(p); CHECK(changed_value != base);
    p["mm_per_source_unit"] = 10;
    auto changed_units = scad::export_source(p); CHECK(changed_units != changed_value);
    p["source"] = "n=1; cube(n); // altered";
    auto changed_source = scad::export_source(p); CHECK(changed_source != changed_units);
    p["files"] = json::array({{{"path","unused.scad"},{"data_b64","YQ=="}}});
    CHECK(scad::export_source(p) != changed_source);
    p["files"][0]["path"] = "../escape.scad";
    CHECK_THROWS(scad::export_source(p));
    auto missing = params("include <missing.scad>\ncube(1);");
    CHECK_THROWS(scad::export_source(missing));
}
