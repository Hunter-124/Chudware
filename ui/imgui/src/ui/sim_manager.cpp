// SimManager implementation — see sim_manager.h for the port's scope + documented adaptations.
#include "ui/sim_manager.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <mutex>
#include <sstream>

#include "imgui.h"

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/domain_tokens.h"
#include "theme/icons.h"
#include "ui/appearance_common.h"
#include "ui/layout.h"
#include "ui/viewport.h"
#include <platform/paths.h>
#include <platform/run_subprocess.h>

namespace ui {

// ---------------------------------------------------------------- small helpers

static void simTip(const char* tip) {
    if (tip && *tip && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("%s", tip);
}

// Repo root for the built-in engine/sim/materials.json (dev-harness convention shared with
// scenarios.cpp's repoBase(); an installed build would need a resourcesDir()-relative copy —
// out of scope here, matches how this tree currently locates other engine-side fixtures).
static std::string repoRoot() {
    const char* r = std::getenv("AI_CAD_REPO");
    return r ? std::string(r) : std::string(".");
}

static bool isFlowFamily(StudyType t) { return t == StudyType::Flow || t == StudyType::Thermal; }

static const char* studyTypeKey(StudyType t) {
    switch (t) {
        case StudyType::Modal:   return "modal";
        case StudyType::Flow:    return "flow";
        case StudyType::Fatigue: return "fatigue";
        case StudyType::Thermal: return "thermal";
        default:                 return "static";
    }
}
static StudyType studyTypeFromKey(const std::string& k) {
    if (k == "modal")   return StudyType::Modal;
    if (k == "flow")    return StudyType::Flow;
    if (k == "fatigue") return StudyType::Fatigue;
    if (k == "thermal") return StudyType::Thermal;
    return StudyType::Static;
}
static const char* studyTypeLabel(StudyType t) {
    switch (t) {
        case StudyType::Modal:   return "Frequency";
        case StudyType::Flow:    return "Flow";
        case StudyType::Fatigue: return "Fatigue";
        case StudyType::Thermal: return "Thermal";
        default:                 return "Static";
    }
}
static const char* studyTypeIcon(StudyType t) {
    switch (t) {
        case StudyType::Modal:   return "study_modal";
        case StudyType::Flow:    return "study_flow";
        case StudyType::Fatigue: return "study_fatigue";
        case StudyType::Thermal: return "study_thermal";
        default:                 return "study_static";
    }
}

// allowHex=false forces tetrahedral (modal has no hex8 mass matrix).
static void addMeshParams(nlohmann::json& j, const Study& s, int meshOrder, bool allowHex = true) {
    j["fineness"] = s.meshFineness;
    const bool hex = allowHex && s.meshHex;
    j["structured"] = hex;
    j["mesh_type"] = hex ? "hex" : "tet";
    j["mesh_order"] = meshOrder;
    j["order"] = meshOrder;
    j["curvature"] = s.meshCurvature;
    j["curv_elems_per_circle"] = s.meshCurvPerCircle;
    if (s.meshTargetMm > 0.0f) j["target_size_mm"] = s.meshTargetMm;
    if (s.meshMinMm > 0.0f) j["min_size_mm"] = s.meshMinMm;
    j["algorithm3d"] = s.meshAlgo;
    j["optimize"] = s.meshOptimize;
}

static void clearTransientResult(Study& s) {
    s.hasResult = false;
    s.summary.clear();
    s.banner.clear();
    s.resultSummary = nlohmann::json::object();
    s.overlayPath.clear();
    s.freqs.clear();
    s.dragTrace.clear();
    s.dragN = 0.0; s.cd = 0.0; s.frontalMm2 = 0.0;
    s.solverNotes.clear();
}

std::string SimWell::label() const {
    if (picks.empty()) return "(empty)";
    return std::to_string(picks.size()) + (picks.size() == 1 ? " face" : " faces");
}

// ---------------------------------------------------------------- result fields

namespace {
struct ResultField { const char* chunk; const char* label; int kind; };  // kind: 0 stress,1 length,2 dimensionless,3 velocity
const ResultField kStaticFields[] = {
    {"von_mises",        "von Mises (distortion energy)", 0},
    {"tresca",           "Tresca (max shear stress)",     0},
    {"abs_principal",    "Max normal stress",             0},
    {"p1",               "1st principal (P1)",            0},
    {"p3",               "3rd principal (P3)",            0},
    {"max_shear",        "Max shear",                     0},
    {"displacement_mag", "Displacement (URES)",           1},
    {"disp_x",           "Displacement X",                1},
    {"disp_y",           "Displacement Y",                1},
    {"disp_z",           "Displacement Z",                1},
    {"fos",              "Factor of safety (von Mises)",  2},
    {"fos_tresca",       "Factor of safety (Tresca)",     2},
    {"fos_rankine",      "Factor of safety (max normal)", 2},
};
const int kNStaticFields = (int)(sizeof(kStaticFields) / sizeof(kStaticFields[0]));
const ResultField kFatigueFields[] = {
    {"fatigue_fos", "Fatigue factor of safety", 2},
    {"life_log10",  "Life (log10 cycles)",      2},
    {"von_mises",   "Peak von Mises stress",    0},
};
const int kNFatigueFields = (int)(sizeof(kFatigueFields) / sizeof(kFatigueFields[0]));
const ResultField kFlowFields[] = {
    {"pressure_cp", "Pressure coefficient (Cp)", 2},
    {"temperature", "Temperature (degC)",        3},
    {"velocity",    "Velocity (slices)",         3},
};
const int kNFlowFields = (int)(sizeof(kFlowFields) / sizeof(kFlowFields[0]));

static bool flowFieldPresent(const std::vector<std::string>& fields, const char* chunk) {
    if (fields.empty()) return true;
    for (const auto& f : fields) if (f == chunk) return true;
    return false;
}

struct FluidPreset { const char* label; float density; float visc; };
static const FluidPreset kFluids[3] = {
    {"Air (sea level, 15 C)", 1.225f,  1.5e-5f},
    {"Water (fresh, 20 C)",   998.0f,  1.0e-6f},
    {"Custom",                1.225f,  1.5e-5f},
};
static const int kNFluids = 3;

struct RoughPreset { const char* label; float ks_um; };
static const RoughPreset kRoughness[6] = {
    {"Smooth / polished",            0.0f},
    {"Smooth paint / gelcoat",       6.0f},
    {"Bare aluminium / sheet metal", 30.0f},
    {"Galvanised / rough paint",     150.0f},
    {"Cast / rough composite",       500.0f},
    {"Custom (um)",                  0.0f},
};
static const int kNRoughness = 6;

static void flowFluidProps(const Study& s, float& density, float& visc) {
    int fi = (s.flowFluid < 0 || s.flowFluid >= kNFluids) ? 0 : s.flowFluid;
    if (fi == kNFluids - 1) { density = s.flowDensity; visc = s.flowViscosity; }
    else                    { density = kFluids[fi].density; visc = kFluids[fi].visc; }
}
static float flowRoughnessUm(const Study& s) {
    int ri = (s.flowRoughness < 0 || s.flowRoughness >= kNRoughness) ? 0 : s.flowRoughness;
    return (ri == kNRoughness - 1) ? s.flowRoughnessUm : kRoughness[ri].ks_um;
}
static const char* kDirLabels[6] = {"+X", "-X", "+Y", "-Y", "+Z", "-Z"};
static void flowIncomingDir(const Study& s, float out[3]) {
    static const float dirs[6][3] = {{1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1}};
    int di = (s.flowDir < 0 || s.flowDir > 5) ? 0 : s.flowDir;
    out[0] = dirs[di][0]; out[1] = dirs[di][1]; out[2] = dirs[di][2];
    // AoA/sideslip are cosmetic-only in this readout (host's full rotation composition is not
    // reproduced here); the resolved RUN direction always uses the base axis, matching runFlow().
}

void fieldMeta(const SimManager::UnitSys& u, const std::string& chunk,
              std::string& label, std::string& unit, double& fromSI) {
    if (chunk == "mode_amplitude") { label = "Mode amplitude"; unit = ""; fromSI = 1.0; return; }
    if (chunk == "temperature")    { label = "Temperature"; unit = "degC"; fromSI = 1.0; return; }
    auto scan = [&](const ResultField* arr, int n) -> bool {
        for (int i = 0; i < n; ++i)
            if (chunk == arr[i].chunk) {
                label = arr[i].label;
                if (arr[i].kind == 0)      { unit = u.stress; fromSI = u.stressFromMpa; }
                else if (arr[i].kind == 1) { unit = u.len;    fromSI = u.lenFromMm; }
                else if (arr[i].kind == 3) { unit = "m/s";    fromSI = 1.0; }
                else                       { unit = "";       fromSI = 1.0; }
                return true;
            }
        return false;
    };
    if (scan(kStaticFields, kNStaticFields)) return;
    if (scan(kFatigueFields, kNFatigueFields)) return;
    if (scan(kFlowFields, kNFlowFields)) return;
    label = chunk; unit = ""; fromSI = 1.0;
}

// ---- fatigue math (Marin endurance correction, reliability, S-N life) ----
const char* kTheoryKeys[5]  = {"goodman", "gerber", "soderberg", "asme", "morrow"};
const char* kTheoryNames[5] = {"Goodman", "Gerber", "Soderberg", "ASME-elliptic", "Morrow"};

double marin_ka(double Sut, int surface) {
    static const double A[4] = {1.58, 4.51, 57.7, 272.0};
    static const double b[4] = {-0.085, -0.265, -0.718, -0.995};
    int i = (surface < 0 || surface > 3) ? 1 : surface;
    double ka = A[i] * std::pow(Sut, b[i]);
    return ka > 1.0 ? 1.0 : ka;
}
double marin_kb(double d_mm, int loadCase) {
    if (loadCase == 1) return 1.0;
    if (d_mm <= 0) d_mm = 7.62;
    if (d_mm < 2.79) d_mm = 2.79;
    if (d_mm <= 51.0) return std::pow(d_mm / 7.62, -0.107);
    return 1.51 * std::pow(d_mm, -0.157);
}
double marin_kc(int loadCase) { return loadCase == 1 ? 0.85 : loadCase == 2 ? 0.59 : 1.0; }
double marin_kd(double T_c) {
    static const double Ts[7] = {20, 100, 200, 300, 400, 500, 550};
    static const double kd[7] = {1.0, 1.020, 1.025, 0.975, 0.90, 0.825, 0.76};
    if (T_c <= Ts[0]) return 1.0;
    for (int i = 1; i < 7; ++i)
        if (T_c <= Ts[i]) { double t = (T_c - Ts[i-1]) / (Ts[i] - Ts[i-1]); return kd[i-1] + t * (kd[i] - kd[i-1]); }
    return kd[6];
}
double reliability_ke(double Rpct) {
    static const double R[7]  = {50, 90, 95, 99, 99.9, 99.99, 99.999};
    static const double ke[7] = {1.000, 0.897, 0.868, 0.814, 0.753, 0.702, 0.659};
    if (Rpct <= R[0]) return 1.0;
    for (int i = 1; i < 7; ++i)
        if (Rpct <= R[i]) { double t = (Rpct - R[i-1]) / (R[i] - R[i-1]); return ke[i-1] + t * (ke[i] - ke[i-1]); }
    return ke[6];
}
double sn_life(double sa_eq, double Se, double Sut) {
    if (Se <= 0 || Sut <= 0 || sa_eq <= 0) return 1e6;
    if (sa_eq <= Se) return 1e6;
    const double f = 0.9, a = (f * Sut) * (f * Sut) / Se, b = -(1.0 / 3.0) * std::log10((f * Sut) / Se);
    double N = std::pow(sa_eq / a, 1.0 / b);
    return N < 1 ? 1 : (N > 1e6 ? 1e6 : N);
}
void fatigueLoadFactors(const Study& s, float& lmin, float& lmax) {
    switch (s.fatigueLoadType) {
        case 1: lmin = 0;  lmax = 1; break;
        case 2: lmin = s.fatigueRatio; lmax = 1; break;
        case 3: lmin = s.fatigueLmin; lmax = s.fatigueLmax; break;
        default: lmin = -1; lmax = 1; break;
    }
}
double seBase(const Study& s, double matSe, double Sut) {
    if (s.fatigueSe > 0) return s.fatigueSe;
    if (matSe > 0) return matSe;
    return std::min(0.5 * Sut, 700.0);
}

static std::string customMaterialsPath() {
    return platform::joinPath(platform::appdataRoot(), "custom_materials.json");
}
static void loadMaterialsFile(const std::string& path, std::vector<SimManager::Material>& out, bool custom) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return;
    std::stringstream ss; ss << f.rdbuf();
    try {
        auto j = nlohmann::json::parse(ss.str());
        if (!j.is_object()) return;
        for (auto it = j.begin(); it != j.end(); ++it) {
            SimManager::Material m;
            m.key = it.key();
            const auto& v = it.value();
            m.name = v.value("name", it.key());
            m.E_mpa = v.value("E_mpa", 0.0);
            m.nu = v.value("nu", 0.0);
            m.density_g_cm3 = v.value("density_g_cm3", 0.0);
            m.yield_mpa = v.value("yield_mpa", 0.0);
            m.ultimate_mpa = v.value("ultimate_mpa", 0.0);
            m.endurance_mpa = v.value("endurance_mpa", 0.0);
            m.custom = custom;
            out.push_back(std::move(m));
        }
    } catch (...) {}
}
static std::vector<SimManager::Material>& materialStore() { static std::vector<SimManager::Material> m; return m; }
static bool& materialsLoaded() { static bool b = false; return b; }
}  // namespace

// ---------------------------------------------------------------- materials

const std::vector<SimManager::Material>& SimManager::materials() {
    auto& mats = materialStore();
    if (!materialsLoaded()) {
        materialsLoaded() = true;
        mats.clear();
        loadMaterialsFile(repoRoot() + "/engine/sim/materials.json", mats, false);
        loadMaterialsFile(customMaterialsPath(), mats, true);
    }
    return mats;
}

std::string SimManager::addCustomMaterial(const Material& in) {
    std::string base = "custom_";
    for (char c : in.name) {
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) base += c;
        else if (c >= 'A' && c <= 'Z') base += (char)(c - 'A' + 'a');
        else if (c == ' ' || c == '-' || c == '_') base += '_';
    }
    if (base == "custom_") base = "custom_material";
    nlohmann::json j = nlohmann::json::object();
    {
        std::ifstream f(customMaterialsPath(), std::ios::binary);
        if (f) { std::stringstream ss; ss << f.rdbuf();
                 try { auto p = nlohmann::json::parse(ss.str()); if (p.is_object()) j = p; } catch (...) {} }
    }
    std::string key = base;
    for (int n = 2; j.contains(key); ++n) key = base + "_" + std::to_string(n);
    j[key] = {{"name", in.name.empty() ? key : in.name}, {"E_mpa", in.E_mpa}, {"nu", in.nu},
              {"density_g_cm3", in.density_g_cm3}, {"yield_mpa", in.yield_mpa},
              {"ultimate_mpa", in.ultimate_mpa}, {"endurance_mpa", in.endurance_mpa}};
    try { std::filesystem::create_directories(std::filesystem::path(customMaterialsPath()).parent_path()); }
    catch (...) {}
    std::ofstream o(customMaterialsPath(), std::ios::binary | std::ios::trunc);
    if (o) o << j.dump(2);
    materialsLoaded() = false;
    return key;
}

int SimManager::materialIndexByKey(const std::string& key) {
    const auto& mats = materials();
    for (int i = 0; i < (int)mats.size(); ++i) if (mats[i].key == key) return i;
    return -1;
}

SimManager::UnitSys SimManager::unitSys(const app::AppState& st) const {
    const bool us = (unitsMode_ == 2) || (unitsMode_ == 0 && c5::ips(st));
    UnitSys u;
    u.us = us;
    if (us) {
        u.stress = "psi"; u.stressFromMpa = 145.0377;
        u.len = "in";     u.lenFromMm = 1.0 / 25.4;
        u.force = "lbf";  u.forceFromN = 0.2248089;
        u.press = "psi";  u.pressFromMpa = 145.0377;
    }
    return u;
}

// ---------------------------------------------------------------- ctor

SimManager::SimManager() {}

// ---------------------------------------------------------------- JSON <-> Study

nlohmann::json SimManager::studyToJson(const Study& s) {
    nlohmann::json fixtures = nlohmann::json::array();
    for (const auto& p : s.fixtures.picks) fixtures.push_back(p.selector);
    nlohmann::json loads = nlohmann::json::array();
    for (const auto& ld : s.loads) {
        nlohmann::json faces = nlohmann::json::array();
        for (const auto& p : ld.faces.picks) faces.push_back(p.selector);
        nlohmann::json l = {{"faces", faces}};
        if (ld.pressure) { l["type"] = "pressure"; l["magnitude"] = ld.pressureMpa; }
        else { l["type"] = "force"; l["vec"] = {ld.vec[0], ld.vec[1], ld.vec[2]}; }
        loads.push_back(l);
    }
    return nlohmann::json{
        {"id", s.id}, {"type", studyTypeKey(s.type)}, {"name", s.name}, {"material", s.materialKey},
        {"fixtures", fixtures}, {"loads", loads},
        {"mesh_fineness", s.meshFineness}, {"mesh_hex", s.meshHex}, {"mesh_curvature", s.meshCurvature},
        {"mesh_curv_per_circle", s.meshCurvPerCircle}, {"mesh_order", s.meshOrder},
        {"mesh_target_mm", s.meshTargetMm}, {"mesh_min_mm", s.meshMinMm}, {"mesh_algo", s.meshAlgo},
        {"mesh_optimize", s.meshOptimize}, {"n_modes", s.nModes}, {"criterion", (int)s.criterion},
        {"flow", {{"dir", s.flowDir}, {"speed", s.flowSpeed}, {"preset", s.flowPreset},
                  {"fluid", s.flowFluid}, {"density", s.flowDensity}, {"viscosity", s.flowViscosity},
                  {"aoa", s.flowAoA}, {"sideslip", s.flowSideslip},
                  {"roughness", s.flowRoughness}, {"roughness_um", s.flowRoughnessUm},
                  {"ref_area_mm2", s.flowRefAreaMm2}, {"solver", s.flowSolver},
                  {"thermal", s.flowThermal}, {"thermal_type", s.flowThermalType},
                  {"inlet_temp_c", s.flowInletTempC},
                  {"wall_mode", s.flowWallMode}, {"wall_temp_c", s.flowWallTempC},
                  {"wall_flux_wm2", s.flowWallFluxWm2}, {"wall_power_w", s.flowWallPowerW},
                  {"solid_kappa", s.flowSolidKappa}, {"power_w", s.flowPowerW}}},
        {"fatigue", {{"method", s.fatigueMethod}, {"load_type", s.fatigueLoadType},
                     {"ratio", s.fatigueRatio}, {"lmin", s.fatigueLmin}, {"lmax", s.fatigueLmax},
                     {"se_mpa", s.fatigueSe}, {"sut_mpa", s.fatigueSut},
                     {"surface", s.fatigueSurface}, {"load_case", s.fatigueLoadCase},
                     {"size_mm", s.fatigueSizeMm}, {"temp_c", s.fatigueTempC},
                     {"reliability", s.fatigueReliability}}},
        // T-SIM1: thermal (conduction) BC INPUTS — persisted so a refresh keeps the user's
        // settings (parallel to the flow block; conductivity is re-derived from the material).
        {"thermal", {{"hot_temp_c", s.thermalHotTempC}, {"use_cold_face", s.thermalUseColdFace},
                     {"cold_temp_c", s.thermalColdTempC}, {"use_convection", s.thermalUseConvection},
                     {"conv_h", s.thermalConvH}, {"amb_c", s.thermalAmbC},
                     {"use_flux", s.thermalUseFlux}, {"flux_wm2", s.thermalFluxWm2}}},
        {"result_summary", s.summary},
    };
}

static SimSel selFromJson(const nlohmann::json& selector) {
    SimSel ss;
    ss.selector = selector;
    if (selector.is_object()) {
        auto it = selector.find("_pick");
        if (it != selector.end() && it->is_object()) {
            ss.pickBody = (uint32_t)it->value("body", 0);
            ss.pickTopo = (uint32_t)it->value("topo", 0);
        }
    }
    return ss;
}

Study SimManager::studyFromJson(const nlohmann::json& j) {
    Study s;
    s.id = j.value("id", std::string());
    s.type = studyTypeFromKey(j.value("type", std::string("static")));
    s.name = j.value("name", std::string("Study"));
    s.materialKey = j.value("material", std::string());
    s.meshFineness = j.value("mesh_fineness", j.value("mesh_density", 0.5f));
    s.meshHex = j.value("mesh_hex", false);
    s.meshCurvature = j.value("mesh_curvature", true);
    s.meshCurvPerCircle = j.value("mesh_curv_per_circle", 12.0f);
    s.meshOrder = j.value("mesh_order", 2);
    s.meshTargetMm = j.value("mesh_target_mm", 0.0f);
    s.meshMinMm = j.value("mesh_min_mm", 0.0f);
    s.meshAlgo = j.value("mesh_algo", 1);
    s.meshOptimize = j.value("mesh_optimize", true);
    s.nModes = j.value("n_modes", 6);
    s.criterion = (FailureCriterion)j.value("criterion", 0);
    if (j.contains("flow") && j["flow"].is_object()) {
        const auto& fl = j["flow"];
        s.flowDir = fl.value("dir", 0); s.flowSpeed = fl.value("speed", 10.0);
        s.flowPreset = fl.value("preset", 0); s.flowFluid = fl.value("fluid", 0);
        s.flowDensity = fl.value("density", 1.225); s.flowViscosity = fl.value("viscosity", 1.5e-5);
        s.flowAoA = fl.value("aoa", 0.0); s.flowSideslip = fl.value("sideslip", 0.0);
        s.flowRoughness = fl.value("roughness", 0); s.flowRoughnessUm = fl.value("roughness_um", 0.0);
        s.flowRefAreaMm2 = fl.value("ref_area_mm2", 0.0); s.flowSolver = fl.value("solver", 0);
        s.flowThermal = fl.value("thermal", false); s.flowThermalType = fl.value("thermal_type", 0);
        s.flowInletTempC = fl.value("inlet_temp_c", 20.0); s.flowWallMode = fl.value("wall_mode", 2);
        s.flowWallTempC = fl.value("wall_temp_c", 80.0); s.flowWallFluxWm2 = fl.value("wall_flux_wm2", 0.0);
        s.flowWallPowerW = fl.value("wall_power_w", 10.0); s.flowSolidKappa = fl.value("solid_kappa", 200.0);
        s.flowPowerW = fl.value("power_w", 20.0);
    }
    if (j.contains("thermal") && j["thermal"].is_object()) {
        const auto& th = j["thermal"];
        s.thermalHotTempC = th.value("hot_temp_c", 100.0);
        s.thermalUseColdFace = th.value("use_cold_face", false);
        s.thermalColdTempC = th.value("cold_temp_c", 20.0);
        s.thermalUseConvection = th.value("use_convection", true);
        s.thermalConvH = th.value("conv_h", 25.0);
        s.thermalAmbC = th.value("amb_c", 20.0);
        s.thermalUseFlux = th.value("use_flux", false);
        s.thermalFluxWm2 = th.value("flux_wm2", 0.0);
    }
    if (j.contains("fatigue") && j["fatigue"].is_object()) {
        const auto& fa = j["fatigue"];
        s.fatigueMethod = fa.value("method", 0); s.fatigueLoadType = fa.value("load_type", 0);
        s.fatigueRatio = fa.value("ratio", -1.0); s.fatigueLmin = fa.value("lmin", -1.0);
        s.fatigueLmax = fa.value("lmax", 1.0); s.fatigueSe = fa.value("se_mpa", 0.0);
        s.fatigueSut = fa.value("sut_mpa", 0.0); s.fatigueSurface = fa.value("surface", 1);
        s.fatigueLoadCase = fa.value("load_case", 0); s.fatigueSizeMm = fa.value("size_mm", 25.0);
        s.fatigueTempC = fa.value("temp_c", 20.0); s.fatigueReliability = fa.value("reliability", 50.0);
    }
    s.summary = j.value("result_summary", std::string());
    if (j.contains("fixtures") && j["fixtures"].is_array())
        for (const auto& sel : j["fixtures"]) s.fixtures.picks.push_back(selFromJson(sel));
    if (j.contains("loads") && j["loads"].is_array())
        for (const auto& ld : j["loads"]) {
            SimLoad l;
            l.pressure = ld.value("type", std::string("force")) == "pressure";
            if (l.pressure) l.pressureMpa = ld.value("magnitude", 0.1);
            else if (ld.contains("vec") && ld["vec"].is_array() && ld["vec"].size() == 3)
                for (int k = 0; k < 3; ++k) l.vec[k] = ld["vec"][k].get<float>();
            if (ld.contains("faces") && ld["faces"].is_array())
                for (const auto& sel : ld["faces"]) l.faces.picks.push_back(selFromJson(sel));
            s.loads.push_back(std::move(l));
        }
    return s;
}

// ---------------------------------------------------------------- refresh / mirror

void SimManager::refresh(app::AppState& st) {
    if (!st.docMirror.hasDoc()) { studies_.clear(); sel_ = -1; return; }
    nlohmann::json res; std::string err;
    if (!st.worker.call("sim_study_list", {{"doc_id", st.docMirror.docId()}}, res, err, 15000)) {
        c5::addLog(st, "[sim] sim_study_list failed: " + err);
        return;
    }
    if (res.contains("code")) return;
    std::vector<Study> prev = std::move(studies_);
    studies_.clear();
    if (res.contains("studies") && res["studies"].is_array())
        for (const auto& j : res["studies"]) {
            Study s = studyFromJson(j);
            for (const auto& p : prev)
                if (p.id == s.id) {
                    s.hasResult = p.hasResult; s.banner = p.banner; s.dragTrace = p.dragTrace;
                    s.resultSummary = p.resultSummary; s.overlayPath = p.overlayPath;
                    s.nNodes = p.nNodes; s.nElems = p.nElems; s.nDof = p.nDof; s.freqs = p.freqs;
                    s.dragN = p.dragN; s.cd = p.cd; s.frontalMm2 = p.frontalMm2;
                    s.liftN = p.liftN; s.sideN = p.sideN; s.cl = p.cl; s.ld = p.ld;
                    s.reynolds = p.reynolds; s.refAreaMm2 = p.refAreaMm2;
                    s.cpMin = p.cpMin; s.cpMax = p.cpMax; s.vmaxMps = p.vmaxMps;
                    s.flowFields = p.flowFields;
                    s.solverNotes = p.solverNotes;
                    s.meshHasQuality = p.meshHasQuality; s.meshAspectMin = p.meshAspectMin;
                    s.meshAspectAvg = p.meshAspectAvg; s.meshAspectMax = p.meshAspectMax;
                    s.meshJacobianMin = p.meshJacobianMin; s.meshPctPoor = p.meshPctPoor;
                    s.meshAspectHist = p.meshAspectHist;
                    // T-SIM3: advisor postcheck result is transient — carry it across refresh.
                    s.advisorVerdict = p.advisorVerdict; s.advisorMatClass = p.advisorMatClass;
                    s.advisorSf = p.advisorSf; s.advisorNarration = p.advisorNarration;
                    s.advisorItems = p.advisorItems;
                    if (!p.summary.empty()) s.summary = p.summary;
                    break;
                }
            studies_.push_back(std::move(s));
        }
    lastDocId_ = st.docMirror.docId();
    lastRevision_ = (long)st.docMirror.revision();
    if (sel_ >= (int)studies_.size()) sel_ = studies_.empty() ? -1 : (int)studies_.size() - 1;
}

void SimManager::persist(app::AppState& st, Study& s) {
    if (s.id.empty() || !st.docMirror.hasDoc()) return;
    nlohmann::json res; std::string err;
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"study_id", s.id}, {"study", studyToJson(s)}};
    if (!st.worker.call("sim_study_edit", params, res, err, 15000) || res.contains("code")) {
        c5::addLog(st, "[sim] sim_study_edit failed: " + (res.contains("code") ? res.value("message", err) : err));
        return;
    }
    c5::markActiveTabDirty(st);
}

void SimManager::newStudy(app::AppState& st, StudyType t) {
    if (!st.docMirror.hasDoc()) {
        c5::toast(ToastLevel::Warn, "Open a part before creating a study");
        return;
    }
    refresh(st);
    Study s;
    s.type = t;
    s.name = std::string(studyTypeLabel(t)) + " " + std::to_string((int)studies_.size() + 1);
    if (!isFlowFamily(t) && !materials().empty()) {
        std::string partKey;
        const nlohmann::json& tr = st.docMirror.tree();
        if (tr.is_object() && tr.contains("material") && tr["material"].is_object())
            partKey = tr["material"].value("key", std::string());
        s.materialKey = (!partKey.empty() && materialIndexByKey(partKey) >= 0) ? partKey : materials()[0].key;
    }
    if (t == StudyType::Thermal) { s.flowSolver = 1; s.flowThermal = true; }
    nlohmann::json res; std::string err;
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"study", studyToJson(s)}};
    if (!st.worker.call("sim_study_add", params, res, err, 15000) || res.contains("code")) {
        c5::addLog(st, "[sim] sim_study_add failed: " + (res.contains("code") ? res.value("message", err) : err));
        return;
    }
    c5::markActiveTabDirty(st);
    refresh(st);
    std::string newId = res.value("study_id", std::string());
    sel_ = -1;
    for (int i = 0; i < (int)studies_.size(); ++i) if (studies_[i].id == newId) { sel_ = i; break; }
    if (sel_ < 0 && !studies_.empty()) sel_ = (int)studies_.size() - 1;
    node_ = SimNode::Study;
}

void SimManager::deleteStudy(app::AppState& st, int i) {
    if (i < 0 || i >= (int)studies_.size() || !st.docMirror.hasDoc()) return;
    std::string id = studies_[i].id;
    nlohmann::json res; std::string err;
    if (!st.worker.call("sim_study_delete", {{"doc_id", st.docMirror.docId()}, {"study_id", id}},
                        res, err, 15000) || res.contains("code")) {
        c5::addLog(st, "[sim] sim_study_delete failed: " + (res.contains("code") ? res.value("message", err) : err));
        return;
    }
    c5::markActiveTabDirty(st);
    clearResultOverlay(st);
    refresh(st);
    if (sel_ >= (int)studies_.size()) sel_ = studies_.empty() ? -1 : (int)studies_.size() - 1;
    node_ = SimNode::Study;
}

// ---------------------------------------------------------------- ribbon commands (unwired here)

bool SimManager::handleCommand(app::AppState& st, const std::string& cmdId) {
    if (cmdId == "sim_new_static")  { newStudy(st, StudyType::Static);  return true; }
    if (cmdId == "sim_new_modal")   { newStudy(st, StudyType::Modal);   return true; }
    if (cmdId == "sim_new_flow")    { newStudy(st, StudyType::Flow);    return true; }
    if (cmdId == "sim_new_thermal") { newStudy(st, StudyType::Thermal); return true; }
    if (cmdId == "sim_new_fatigue") { newStudy(st, StudyType::Fatigue); return true; }
    refresh(st);
    const bool haveStudy = sel_ >= 0 && sel_ < (int)studies_.size();
    if (cmdId == "sim_material" || cmdId == "sim_results" || cmdId == "sim_mesh") {
        if (!haveStudy) return true;
        if (cmdId == "sim_material") {
            // T-SIM3: the ribbon command now opens the material-assignment dialog (it used
            // to only navigate to the Material node, leaving no way to actually assign).
            node_ = SimNode::Material;
            matDialogOpen_ = true;
        }
        else if (cmdId == "sim_results")  node_ = SimNode::Results;
        else                              node_ = SimNode::Mesh;
        return true;
    }
    if (!haveStudy) return true;
    Study& s = studies_[sel_];
    // T-G9 parity fix: the H4 port dropped host's captureSelectionInto() in these three
    // branches, so a viewport pre-selection was silently ignored when adding a fixture/
    // load via ribbon command (host adopts it into the well; see host sim_manager.cpp).
    if (cmdId == "sim_fixture")      { captureSelectionInto(st, s.fixtures); persist(st, s);
                                       st.selection.clear();
                                       node_ = SimNode::Fixtures; return true; }
    if (cmdId == "sim_load_force")   { s.loads.push_back(SimLoad{});
                                       captureSelectionInto(st, s.loads.back().faces); persist(st, s);
                                       st.selection.clear();
                                       node_ = SimNode::Load; loadIdx_ = (int)s.loads.size() - 1; return true; }
    if (cmdId == "sim_load_pressure"){ SimLoad l; l.pressure = true; s.loads.push_back(l);
                                       captureSelectionInto(st, s.loads.back().faces); persist(st, s);
                                       st.selection.clear();
                                       node_ = SimNode::Load; loadIdx_ = (int)s.loads.size() - 1; return true; }
    if (cmdId == "sim_run") { run(st, s); return true; }
    return false;
}

void SimManager::configureActive(app::AppState& st, float density, const std::string& materialKey) {
    if (sel_ < 0 || sel_ >= (int)studies_.size()) return;
    studies_[sel_].meshFineness = density;
    if (!materialKey.empty()) studies_[sel_].materialKey = materialKey;
    persist(st, studies_[sel_]);
}

void SimManager::configureActiveFlow(app::AppState& st, int fluid, float aoaDeg, float sideslipDeg, int preset) {
    if (sel_ < 0 || sel_ >= (int)studies_.size()) return;
    Study& s = studies_[sel_];
    s.flowFluid = fluid;
    if (fluid >= 0 && fluid < kNFluids - 1) { s.flowDensity = kFluids[fluid].density; s.flowViscosity = kFluids[fluid].visc; }
    s.flowAoA = aoaDeg; s.flowSideslip = sideslipDeg; s.flowPreset = preset;
    persist(st, s);
}

// ---------------------------------------------------------------- pick capture

void SimManager::captureSelectionInto(app::AppState& st, SimWell& well) {
    for (const auto& it : st.selection.items()) {
        if (it.selector.empty()) continue;
        try {
            auto sel = nlohmann::json::parse(it.selector);
            if (sel.value("kind", std::string()) != "face") continue;
            sel["_pick"] = {{"body", it.hit.body_id}, {"topo", it.hit.topo_id}};
            SimSel ss; ss.selector = sel; ss.pickBody = it.hit.body_id; ss.pickTopo = it.hit.topo_id;
            bool dup = false;
            for (const auto& p : well.picks)
                if (p.pickBody == ss.pickBody && p.pickTopo == ss.pickTopo) { dup = true; break; }
            if (!dup) well.picks.push_back(std::move(ss));
        } catch (...) {}
    }
}

// ---- viewport pick mux: unwired (see sim_manager.h header note) — kept for a future app.cpp wire-up.
bool SimManager::wantsViewportFacePick(app::AppState&) const { return false; }
bool SimManager::routeFacePick(app::AppState&, const nlohmann::json&, uint32_t, uint32_t) { return false; }

// ---------------------------------------------------------------- result field reload

void SimManager::applyResultField(app::AppState& st, Study& s) {
    if (s.overlayPath.empty()) return;
    std::error_code ec;
    if (!std::filesystem::exists(s.overlayPath, ec)) {
        c5::addLog(st, "[sim] overlay file missing (re-run to regenerate): " + s.overlayPath);
        return;
    }
    float vmin = 0.f, vmax = 1.f, dispHint = 1.0f;
    if (!viewportLoadResultAicm(s.overlayPath, activeScalarField_, vmin, vmax, dispHint)) {
        c5::addLog(st, "[sim] overlay reload failed: " + s.overlayPath);
        return;
    }
    UnitSys u = unitSys(st);
    std::string label, unit; double fromSI = 1.0;
    fieldMeta(u, activeScalarField_, label, unit, fromSI);
    auto& vs = viewportState();
    // T-SIM2: honour this study's "Show contour" toggle on (re)load. viewportLoadResultAicm
    // unconditionally turns the colormap on, so re-apply the toggle intent here and gate the
    // legend to match — a field switch or result reload with contour OFF stays off.
    viewportSetColormapMode(s.contourOn);
    vs.legendOn = s.contourOn;
    vs.legendTitle = label;
    vs.legendUnits = unit;
    vs.legendMin = (float)(vmin * fromSI);
    vs.legendMax = (float)(vmax * fromSI);
    // SolidWorks-style default: show the deformed shape at a visible auto scale on a fresh result.
    if (dispHint > 0.f) viewportSetDeformScale(dispHint);
}

// ---------------------------------------------------------------- mesh preview (stats only — see header note)

void SimManager::previewMesh(app::AppState& st, Study& s) {
    if (!st.docMirror.hasDoc()) return;
    std::string stepPath = platform::joinPath(platform::cacheDir(), "sim_mesh_preview.step");
    {
        nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stepPath}};
        std::string e;
        if (!st.worker.call("doc_export_step", p, r, e, 60000) || r.contains("code")) {
            c5::toast(ToastLevel::Error, "Mesh preview: export failed");
            return;
        }
        stepPath = r.value("path", stepPath);
    }
    nlohmann::json res, params = {{"step_path", stepPath}};
    addMeshParams(params, s, s.meshOrder);
    std::string err;
    if (!st.worker.call("sim_mesh", params, res, err, 180000) || res.contains("code") || !res.value("ok", true)) {
        const std::string msg = res.contains("code") ? res.value("message", err)
                                : res.contains("error") ? res.value("error", err) : err;
        c5::toast(ToastLevel::Error, msg.empty() ? "Mesh preview: meshing failed" : msg);
        return;
    }
    s.nNodes = res.value("n_nodes", 0);
    s.nElems = res.value("n_elems", 0);
    s.meshAspectMin = (float)res.value("aspect_min", 0.0);
    s.meshAspectAvg = (float)res.value("aspect_avg", 0.0);
    s.meshAspectMax = (float)res.value("aspect_max", 0.0);
    s.meshJacobianMin = (float)res.value("jacobian_min", 0.0);
    s.meshPctPoor = (float)res.value("pct_poor", 0.0);
    s.meshAspectHist.clear();
    if (res.contains("aspect_hist") && res["aspect_hist"].is_array())
        for (const auto& b : res["aspect_hist"]) s.meshAspectHist.push_back((float)b.get<int>());
    s.meshHasQuality = s.nElems > 0;
    c5::toast(ToastLevel::Info, "Mesh: " + std::to_string(s.nNodes) + " nodes, " +
                                std::to_string(s.nElems) + " elements");
}

// ---------------------------------------------------------------- run

nlohmann::json SimManager::facePointsFor(app::AppState& st, const nlohmann::json& selector, std::string& err) {
    nlohmann::json result;
    nlohmann::json sel = selector;
    if (sel.is_object()) sel.erase("_pick");
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"selector", sel}};
    if (!st.worker.call("doc_face_points", params, result, err, 30000)) return {};
    if (result.contains("code")) { err = result.value("message", std::string("face_points error")); return {}; }
    return result;
}

void SimManager::run(app::AppState& st, Study& s) {
    // T-SIM3: advisor precheck runs BEFORE any dispatch (BC lint + mesh suggestion rendered
    // as an advisory card in the panel). Advise-never-veto: this only returns false when the
    // user opted into blocking via AI_CAD_SIM_PRECHECK_BLOCK=1.
    if (!advisorPrecheck(st, s)) return;
    // T-SIM1: a Thermal study is a steady-state CONDUCTION FEA (sim_run_thermal), NOT the
    // OpenFOAM-CHT flow path. Intercept it here before the isFlowFamily() branch (which stays
    // shared with the Flow study and the T-SIM2/T-SIM3 rendering) so the flow dispatch is
    // untouched — additive seam only.
    if (s.type == StudyType::Thermal) { runThermal(st, s); return; }
    if (isFlowFamily(s.type)) runFlow(st, s);
    else if (s.type == StudyType::Fatigue) runFatigue(st, s);
    else runStaticOrModal(st, s);
}

void SimManager::runFatigue(app::AppState& st, Study& s) {
    if (!st.docMirror.hasDoc()) { c5::addLog(st, "[sim] no document to analyse"); return; }
    std::string stepPath = platform::joinPath(platform::cacheDir(), "sim_fatigue.step");
    {
        nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stepPath}};
        std::string e;
        if (!st.worker.call("doc_export_step", p, r, e, 60000) || r.contains("code")) {
            c5::addLog(st, "[sim] export STEP failed: " + (r.contains("code") ? r.value("message", e) : e));
            c5::toast(ToastLevel::Error, "Simulation: export failed");
            return;
        }
        lastStepPath_ = r.value("path", stepPath);
    }
    nlohmann::json fixtures = nlohmann::json::array();
    for (const auto& p : s.fixtures.picks) {
        std::string e; auto fp = facePointsFor(st, p.selector, e);
        if (!fp.is_null() && fp.contains("points")) fixtures.push_back(fp);
    }
    if (fixtures.empty()) {
        c5::addLog(st, "[sim] add at least one fixture face before running");
        c5::toast(ToastLevel::Warn, "Add a fixture before running");
        return;
    }
    int mi = materialIndexByKey(s.materialKey);
    double matSut = 0, matSe = 0, matSy = 0;
    nlohmann::json material = nlohmann::json::object();
    if (mi >= 0) {
        const auto& m = materials()[mi];
        matSut = m.ultimate_mpa; matSe = m.endurance_mpa; matSy = m.yield_mpa;
        material = {{"E_mpa", m.E_mpa}, {"nu", m.nu}, {"density_g_cm3", m.density_g_cm3},
                    {"yield_mpa", m.yield_mpa}, {"ultimate_mpa", m.ultimate_mpa},
                    {"endurance_mpa", m.endurance_mpa}};
    }
    const double sutUsed = s.fatigueSut > 0 ? s.fatigueSut : matSut;
    if (sutUsed <= 0) {
        c5::toast(ToastLevel::Warn, "Set an ultimate strength (Sut) for this material before a fatigue run");
        c5::addLog(st, "[sim] fatigue: material has no ultimate strength (Sut)");
        return;
    }
    const double seBaseV = seBase(s, matSe, sutUsed);
    const double ka = marin_ka(sutUsed, s.fatigueSurface), kb = marin_kb(s.fatigueSizeMm, s.fatigueLoadCase),
                 kc = marin_kc(s.fatigueLoadCase), kd = marin_kd(s.fatigueTempC);
    const double seCorr = ka * kb * kc * kd * seBaseV;
    float lmin, lmax; fatigueLoadFactors(s, lmin, lmax);

    nlohmann::json loads = nlohmann::json::array();
    for (const auto& ld : s.loads) {
        nlohmann::json faces = nlohmann::json::array();
        for (const auto& p : ld.faces.picks) {
            std::string e; auto fp = facePointsFor(st, p.selector, e);
            if (!fp.is_null() && fp.contains("points")) {
                if (faces.empty()) faces = fp;
                else for (auto& pt : fp["points"]) faces["points"].push_back(pt);
            }
        }
        if (faces.empty()) continue;
        nlohmann::json l = {{"faces", faces}};
        if (ld.pressure) { l["type"] = "pressure"; l["magnitude"] = ld.pressureMpa; }
        else { l["type"] = "force"; l["vector"] = {ld.vec[0], ld.vec[1], ld.vec[2]}; }
        loads.push_back(l);
    }
    nlohmann::json study = {
        {"step_path", lastStepPath_}, {"material", material}, {"fixtures", fixtures}, {"loads", loads},
        {"fatigue", {{"lmin", lmin}, {"lmax", lmax}, {"se_corrected_mpa", seCorr}, {"se_base_mpa", seBaseV},
                     {"ultimate_mpa", sutUsed}, {"yield_mpa", matSy},
                     {"marin", {{"ka", ka}, {"kb", kb}, {"kc", kc}, {"kd", kd}}}}},
    };
    addMeshParams(study, s, s.meshOrder);
    std::string note;
    if (st.simClient.launch("sim_run_fatigue", {{"study", study}}, note, st.docMirror.docId())) {
        clearTransientResult(s);
        resultShown_ = false; shownStudyId_.clear();
        running_ = true; runningStudy_ = sel_; runningStudyId_ = s.id;
    }
    c5::addLog(st, note);
}

void SimManager::runStaticOrModal(app::AppState& st, Study& s) {
    if (!st.docMirror.hasDoc()) { c5::addLog(st, "[sim] no document to analyse"); return; }
    std::string stepPath = platform::joinPath(platform::cacheDir(), "sim_study.step");
    {
        nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stepPath}};
        std::string e;
        if (!st.worker.call("doc_export_step", p, r, e, 60000) || r.contains("code")) {
            c5::addLog(st, "[sim] export STEP failed: " + (r.contains("code") ? r.value("message", e) : e));
            c5::toast(ToastLevel::Error, "Simulation: export failed");
            return;
        }
        lastStepPath_ = r.value("path", stepPath);
    }
    nlohmann::json fixtures = nlohmann::json::array();
    for (const auto& p : s.fixtures.picks) {
        std::string e; auto fp = facePointsFor(st, p.selector, e);
        if (!fp.is_null() && fp.contains("points")) fixtures.push_back(fp);
    }
    if (fixtures.empty()) {
        c5::addLog(st, "[sim] add at least one fixture face before running");
        c5::toast(ToastLevel::Warn, "Add a fixture before running");
        return;
    }
    nlohmann::json material = nlohmann::json::object();
    int mi = materialIndexByKey(s.materialKey);
    if (mi >= 0) {
        const auto& m = materials()[mi];
        material = {{"E_mpa", m.E_mpa}, {"nu", m.nu}, {"density_g_cm3", m.density_g_cm3}, {"yield_mpa", m.yield_mpa}};
    }
    nlohmann::json study = {{"step_path", lastStepPath_}, {"material", material}, {"fixtures", fixtures}};
    addMeshParams(study, s, s.type == StudyType::Static ? s.meshOrder : 1,
                 /*allowHex=*/s.type == StudyType::Static);
    std::string note;
    if (s.type == StudyType::Static) {
        nlohmann::json loads = nlohmann::json::array();
        for (const auto& ld : s.loads) {
            nlohmann::json faces = nlohmann::json::array();
            for (const auto& p : ld.faces.picks) {
                std::string e; auto fp = facePointsFor(st, p.selector, e);
                if (!fp.is_null() && fp.contains("points")) {
                    if (faces.empty()) faces = fp;
                    else for (auto& pt : fp["points"]) faces["points"].push_back(pt);
                }
            }
            if (faces.empty()) continue;
            nlohmann::json l = {{"faces", faces}};
            if (ld.pressure) { l["type"] = "pressure"; l["magnitude"] = ld.pressureMpa; }
            else { l["type"] = "force"; l["vector"] = {ld.vec[0], ld.vec[1], ld.vec[2]}; }
            loads.push_back(l);
        }
        study["loads"] = loads;
        if (st.simClient.launch("sim_run_static", {{"study", study}}, note, st.docMirror.docId())) {
            clearTransientResult(s);
            resultShown_ = false; shownStudyId_.clear();
            running_ = true; runningStudy_ = sel_; runningStudyId_ = s.id;
        }
    } else {
        if (st.simClient.launch("sim_run_modal", {{"study", study}, {"n_modes", s.nModes}}, note,
                                st.docMirror.docId())) {
            clearTransientResult(s);
            resultShown_ = false; shownStudyId_.clear();
            running_ = true; runningStudy_ = sel_; runningStudyId_ = s.id;
        }
    }
    c5::addLog(st, note);
}

void SimManager::runFlow(app::AppState& st, Study& s) {
    if (!st.docMirror.hasDoc()) { c5::addLog(st, "[sim] no document to analyse"); return; }
    std::string stlPath = platform::joinPath(platform::cacheDir(), "sim_flow.stl");
    nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stlPath}};
    std::string e;
    if (!st.worker.call("doc_export_step", p, r, e, 60000) || r.contains("code")) {
        c5::addLog(st, "[sim] export STL failed: " + (r.contains("code") ? r.value("message", e) : e));
        return;
    }
    lastStepPath_ = r.value("path", stlPath);
    static const int dirs[6][3] = {{1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1}};
    static const char* presets[4] = {"coarse", "medium", "fine", "xfine"};
    int di = (s.flowDir < 0 || s.flowDir > 5) ? 0 : s.flowDir;
    int pi = (s.flowPreset < 0 || s.flowPreset > 3) ? 0 : s.flowPreset;
    float density, visc; flowFluidProps(s, density, visc);
    const char* fluidName = (s.flowFluid == 1) ? "water" : (s.flowFluid == 2) ? "custom" : "air";
    nlohmann::json params = {
        {"stl_path", lastStepPath_}, {"direction", {dirs[di][0], dirs[di][1], dirs[di][2]}},
        {"speed_mps", s.flowSpeed}, {"preset", presets[pi]},
        {"aoa_deg", s.flowAoA}, {"sideslip_deg", s.flowSideslip},
        {"fluid", {{"name", fluidName}, {"density_kgm3", density}, {"kinematic_visc_m2s", visc}}},
        {"roughness_um", flowRoughnessUm(s)}, {"ref_area_mm2", s.flowRefAreaMm2},
        {"solver", s.flowSolver == 1 ? "openfoam" : "lbm"},
    };
    if (s.flowSolver == 1 && s.flowThermal) {
        if (s.flowThermalType == 1) {
            params["thermal"] = {{"enabled", true}, {"mode", "conjugate"}, {"inlet_temp_c", s.flowInletTempC},
                                 {"solid_kappa", s.flowSolidKappa}, {"power_w", s.flowPowerW}};
        } else {
            const char* wallMode = (s.flowWallMode == 0) ? "temp" : (s.flowWallMode == 1) ? "flux" : "power";
            params["thermal"] = {{"enabled", true}, {"mode", "fluid"}, {"inlet_temp_c", s.flowInletTempC},
                                 {"wall_mode", wallMode}, {"wall_temp_c", s.flowWallTempC},
                                 {"wall_flux_w_m2", s.flowWallFluxWm2}, {"wall_power_w", s.flowWallPowerW}};
        }
    }
    std::string note;
    if (st.simClient.launch("sim_run_flow", params, note, st.docMirror.docId())) {
        clearTransientResult(s);
        resultShown_ = false; shownStudyId_.clear();
        running_ = true; runningStudy_ = sel_; runningStudyId_ = s.id;
    }
    c5::addLog(st, note);
}

// T-SIM1: thermal conductivity table (W/m·K) keyed by the same sim keys as materials.json.
// Loaded from the cited data file engine/sim/thermal_conductivity.json (never invented
// inline). Returns 0.0 when the material has no tabulated conductivity.
static double thermalConductivityFor(const std::string& materialKey) {
    static std::map<std::string, double> table;
    static bool loaded = false;
    if (!loaded) {
        loaded = true;
        try {
            std::ifstream f(repoRoot() + "/engine/sim/thermal_conductivity.json");
            if (f) {
                nlohmann::json j; f >> j;
                for (auto it = j.begin(); it != j.end(); ++it)
                    if (it.key().rfind("_", 0) != 0 && it.value().is_number())
                        table[it.key()] = it.value().get<double>();
            }
        } catch (...) { /* missing/table parse failure -> empty table, auto=0 */ }
    }
    auto it = table.find(materialKey);
    return it != table.end() ? it->second : 0.0;
}

void SimManager::runThermal(app::AppState& st, Study& s) {
    if (!st.docMirror.hasDoc()) { c5::addLog(st, "[sim] no document to analyse"); return; }
    // Conductivity from the cited data table (W/m·K); the glue converts to mesh units.
    const double k_w_mk = thermalConductivityFor(s.materialKey);
    if (!(k_w_mk > 0.0)) {
        c5::toast(ToastLevel::Warn, "This material has no thermal conductivity in the table");
        c5::addLog(st, "[sim] thermal: material '" + s.materialKey + "' has no conductivity (k)");
        return;
    }
    s.thermalKwmk = (float)k_w_mk;
    std::string stepPath = platform::joinPath(platform::cacheDir(), "sim_thermal.step");
    {
        nlohmann::json r, p = {{"doc_id", st.docMirror.docId()}, {"path", stepPath}};
        std::string e;
        if (!st.worker.call("doc_export_step", p, r, e, 60000) || r.contains("code")) {
            c5::addLog(st, "[sim] export STEP failed: " + (r.contains("code") ? r.value("message", e) : e));
            c5::toast(ToastLevel::Error, "Simulation: export failed");
            return;
        }
        lastStepPath_ = r.value("path", stepPath);
    }
    // Fixtures -> Dirichlet fixed-temperature faces (all at the hot temperature).
    nlohmann::json fixtures = nlohmann::json::array();
    for (const auto& p : s.fixtures.picks) {
        std::string e; auto fp = facePointsFor(st, p.selector, e);
        if (!fp.is_null() && fp.contains("points")) { fp["temp_c"] = s.thermalHotTempC; fixtures.push_back(fp); }
    }
    if (fixtures.empty()) {
        c5::addLog(st, "[sim] thermal: add at least one fixed-temperature face before running");
        c5::toast(ToastLevel::Warn, "Add a fixed-temperature face before running");
        return;
    }
    // Loads -> the cold-side boundary on the LOAD faces: a second fixed temperature, a
    // convection film, or a heat flux (mirrors how the force/pressure dialogs map load faces).
    nlohmann::json loads = nlohmann::json::array();
    nlohmann::json coldFixtures = nlohmann::json::array();
    for (const auto& ld : s.loads) {
        nlohmann::json faces = nlohmann::json::array();
        for (const auto& p : ld.faces.picks) {
            std::string e; auto fp = facePointsFor(st, p.selector, e);
            if (!fp.is_null() && fp.contains("points")) {
                if (faces.empty()) faces = fp;
                else for (auto& pt : fp["points"]) faces["points"].push_back(pt);
            }
        }
        if (faces.empty()) continue;
        if (s.thermalUseColdFace) {
            faces["temp_c"] = s.thermalColdTempC;
            coldFixtures.push_back(faces);
        } else if (s.thermalUseFlux) {
            loads.push_back({{"faces", faces}, {"type", "flux"}, {"magnitude", s.thermalFluxWm2}});
        } else {  // convection (default)
            loads.push_back({{"faces", faces}, {"type", "convection"},
                             {"h", s.thermalConvH}, {"t_amb_c", s.thermalAmbC}});
        }
    }
    for (auto& cf : coldFixtures) fixtures.push_back(cf);

    nlohmann::json material = {{"k_w_mk", k_w_mk}};
    nlohmann::json study = {{"step_path", lastStepPath_}, {"material", material},
                            {"fixtures", fixtures}, {"loads", loads}};
    addMeshParams(study, s, s.meshOrder);
    std::string note;
    if (st.simClient.launch("sim_run_thermal", {{"study", study}}, note, st.docMirror.docId())) {
        clearTransientResult(s);
        resultShown_ = false; shownStudyId_.clear();
        running_ = true; runningStudy_ = sel_; runningStudyId_ = s.id;
    }
    c5::addLog(st, note);
}

// ---------------------------------------------------------------- sim advisor (T-SIM3)
// The T-W9 sim advisor (services/agent/advisor/sim_advisor.py) is the single source of
// advisor policy (BC lint, mesh suggestion, margin_policy.json verdict, narration). The GUI
// calls it through the one-shot JSON shim services/agent/advisor/sim_advisor_cli.py — the
// rules are never re-implemented in C++.

// Freedom rule (advise, never veto): dispatch is blocked ONLY when the user explicitly sets
// AI_CAD_SIM_PRECHECK_BLOCK=1|true. Note sim_advisor.py's own module default is block-ON for
// agent callers; the GUI passes its effective gate through to the subprocess so the returned
// severity labels ("blocker" vs "action") match what the GUI will actually do.
static bool precheckBlockEnabled() {
    const char* v = std::getenv("AI_CAD_SIM_PRECHECK_BLOCK");
    return v && (std::strcmp(v, "1") == 0 || std::strcmp(v, "true") == 0);
}

// Run one advisor request through the CLI shim (stdin JSON -> stdout JSON). Failures set
// `err` and return null — callers treat an unreachable advisor as "no advice" (never veto).
static nlohmann::json callSimAdvisor(const nlohmann::json& request, std::string& err) {
    const char* py = std::getenv("AI_CAD_PYTHON");
    const std::string python = (py && *py) ? py : "python3";
    const std::string script = repoRoot() + "/services/agent/advisor/sim_advisor_cli.py";
    std::error_code ec;
    if (!std::filesystem::exists(script, ec)) { err = "advisor shim not found: " + script; return {}; }
    const std::string cmd = std::string("AI_CAD_SIM_PRECHECK_BLOCK=") +
                            (precheckBlockEnabled() ? "1" : "0") + " " +
                            platform::shellQuote(python) + " " + platform::shellQuote(script);
    bool ok = false;
    const std::string out = platform::runShellCapture(cmd, request.dump(), ok);
    if (!ok || out.empty()) { err = "advisor call failed (is python3 available?)"; return {}; }
    try {
        auto j = nlohmann::json::parse(out);
        if (!j.value("ok", false)) { err = j.value("error", std::string("advisor error")); return {}; }
        return j;
    } catch (...) { err = "advisor returned unparseable output"; return {}; }
}

// Mission context (inspectability rule): when a mission run id is present in the
// environment, the shim appends a ledger artifact event for each advisory call.
static void addMissionContext(nlohmann::json& req) {
    const char* run = std::getenv("AI_CAD_MISSION_RUN_ID");
    if (run && *run) req["ledger_run_id"] = run;
}

// The advisor writes typographic UTF-8 (— ≥ × ⇒ ≈ °C …) that the app font atlas does not
// cover — those bytes rendered as '?'. Fold the known symbols to ASCII for display (the raw
// JSON is preserved in the Study / logs; this is presentation-only).
static std::string asciiFold(const std::string& in) {
    static const struct { const char* from; const char* to; } kMap[] = {
        {"\xe2\x80\x94", "-"},   // — em dash
        {"\xe2\x80\x93", "-"},   // – en dash
        {"\xe2\x89\xa5", ">="},  // ≥
        {"\xe2\x89\xa4", "<="},  // ≤
        {"\xe2\x89\x88", "~"},   // ≈
        {"\xc3\x97",     "x"},   // ×
        {"\xe2\x87\x92", "=>"},  // ⇒
        {"\xe2\x80\xb2", "'"},   // ′ prime
        {"\xe2\x80\x99", "'"},   // ’
        {"\xc2\xb0",     " deg"},// ° (degree)
        {"\xc2\xb2",     "2"},   // ²
        {"\xc2\xb3",     "3"},   // ³
        {"\xc2\xb7",     "."},   // ·
        {"\xce\x94",     "d"},   // Δ
    };
    std::string out = in;
    for (const auto& m : kMap) {
        size_t pos = 0;
        const size_t flen = std::strlen(m.from);
        while ((pos = out.find(m.from, pos)) != std::string::npos) {
            out.replace(pos, flen, m.to);
            pos += std::strlen(m.to);
        }
    }
    return out;
}

// Build the study-spec dict sim_advisor.precheck/postcheck expect. Face lists mirror what
// dispatch will actually send (loads with no captured faces are skipped, exactly like
// runStaticOrModal/runThermal do), so the advisor lints the real solve inputs. Material
// numbers come from the cited data tables via materials() — provenance rule.
nlohmann::json SimManager::advisorSpec(app::AppState& st, const Study& s, bool withResults) const {
    (void)st;
    nlohmann::json spec;
    spec["type"] = studyTypeKey(s.type);
    spec["mesh_fineness"] = s.meshFineness;
    nlohmann::json mat = nlohmann::json::object();
    const int mi = materialIndexByKey(s.materialKey);
    if (mi >= 0) {
        const auto& m = materials()[mi];
        mat = {{"name", m.name}, {"E_mpa", m.E_mpa}, {"yield_mpa", m.yield_mpa},
               {"ultimate_mpa", m.ultimate_mpa}};
    }
    nlohmann::json fixtures = nlohmann::json::array();
    nlohmann::json loads = nlohmann::json::array();
    if (s.type == StudyType::Thermal) {
        const double k = thermalConductivityFor(s.materialKey);
        if (k > 0.0) mat["k_w_mk"] = k;
        for (size_t i = 0; i < s.fixtures.picks.size(); ++i)
            fixtures.push_back({{"temp_c", s.thermalHotTempC}});
        for (const auto& ld : s.loads) {
            if (ld.faces.empty()) continue;
            if (s.thermalUseColdFace)      fixtures.push_back({{"temp_c", s.thermalColdTempC}});
            else if (s.thermalUseFlux)     loads.push_back({{"type", "flux"}, {"magnitude", s.thermalFluxWm2}});
            else                           loads.push_back({{"type", "convection"},
                                                            {"h", s.thermalConvH}, {"t_amb_c", s.thermalAmbC}});
        }
    } else {
        for (size_t i = 0; i < s.fixtures.picks.size(); ++i)
            fixtures.push_back("face_" + std::to_string(i + 1));
        for (const auto& ld : s.loads) {
            if (ld.faces.empty()) continue;
            if (ld.pressure) loads.push_back({{"type", "pressure"}, {"magnitude", ld.pressureMpa}});
            else             loads.push_back({{"force", {ld.vec[0], ld.vec[1], ld.vec[2]}}});
        }
    }
    spec["fixtures"] = fixtures;
    spec["loads"] = loads;
    spec["material"] = mat;
    if (withResults) spec["results"] = s.resultSummary;
    return spec;
}

bool SimManager::advisorPrecheck(app::AppState& st, Study& s) {
    precheckItems_ = nlohmann::json::array();
    precheckStudyId_ = s.id;
    precheckBlocked_ = false;
    advisorNote_.clear();
    // The T-W9 battery covers static-family BC lint + the thermal datum lint; Flow and Modal
    // have no advisor checks yet — dispatch them unadvised rather than inventing rules here.
    if (s.type != StudyType::Static && s.type != StudyType::Fatigue && s.type != StudyType::Thermal)
        return true;
    nlohmann::json req = {{"mode", "precheck"}, {"study", advisorSpec(st, s, false)},
                          {"study_id", s.id}};
    addMissionContext(req);
    std::string err;
    nlohmann::json res = callSimAdvisor(req, err);
    if (!err.empty()) {
        advisorNote_ = "Advisor unavailable — ran without precheck (" + err + ")";
        c5::addLog(st, "[sim] advisor precheck skipped: " + err);
        return true;  // freedom rule: an unreachable advisor must never block a run
    }
    precheckItems_ = res.value("items", nlohmann::json::array());
    bool hasBlocker = false;
    for (const auto& it : precheckItems_)
        if (it.value("severity", std::string()) == "blocker") hasBlocker = true;
    c5::addLog(st, "[sim] advisor precheck: " + std::to_string(precheckItems_.size()) +
                   " item(s)" + (hasBlocker ? " (blocker)" : ""));
    if (hasBlocker && precheckBlockEnabled()) {
        precheckBlocked_ = true;
        c5::toast(ToastLevel::Warn, "Run blocked by sim precheck (AI_CAD_SIM_PRECHECK_BLOCK=1)");
        return false;
    }
    return true;
}

void SimManager::advisorPostcheck(app::AppState& st, Study& s) {
    s.advisorVerdict.clear();
    s.advisorMatClass.clear();
    s.advisorSf = 0.0;
    s.advisorNarration.clear();
    s.advisorItems = nlohmann::json::array();
    // The margin policy is a static-stress verdict (max von Mises vs yield/ultimate).
    if (s.type != StudyType::Static) return;
    nlohmann::json req = {{"mode", "postcheck"}, {"study", advisorSpec(st, s, true)},
                          {"study_id", s.id}};
    if (precheckStudyId_ == s.id && precheckItems_.is_array() && !precheckItems_.empty())
        req["precheck_items"] = precheckItems_;  // share one anchor computation (T-W9 contract)
    addMissionContext(req);
    std::string err;
    nlohmann::json res = callSimAdvisor(req, err);
    if (!err.empty()) { c5::addLog(st, "[sim] advisor postcheck skipped: " + err); return; }
    s.advisorVerdict = res.value("verdict", std::string());
    s.advisorMatClass = res.value("mat_class", std::string());
    s.advisorSf = res.value("sf_used", 0.0);
    s.advisorNarration = res.value("narration", std::string());
    s.advisorItems = res.value("items", nlohmann::json::array());
    if (!s.advisorVerdict.empty())
        c5::addLog(st, "[sim] advisor verdict: " + s.advisorVerdict +
                       " (" + s.advisorMatClass + ")");
}

// T-SIM3 shot seam — see sim_manager.h. Numbers are a seeded fixture; the postcheck is live.
void SimManager::debugSeedStaticResult(app::AppState& st, double vmMpa, double dispMm, double minFos) {
    if (sel_ < 0 || sel_ >= (int)studies_.size()) return;
    Study& s = studies_[sel_];
    s.hasResult = true;
    s.resultSummary = {{"max_von_mises_mpa", vmMpa}, {"max_disp_mm", dispMm}, {"min_fos", minFos}};
    char buf[256];
    std::snprintf(buf, sizeof(buf), "max vM %.2f MPa | max disp %.4f mm | min FoS %.2f",
                  vmMpa, dispMm, minFos);
    s.summary = buf;
    node_ = SimNode::Results;
    advisorPostcheck(st, s);
}

// ---------------------------------------------------------------- tick / result view

void SimManager::clearResultOverlay(app::AppState&) {
    auto& vs = viewportState();
    vs.legendOn = false;
    viewportSetColormapMode(false);
    viewportSetDeformScale(0.0f);
    resultShown_ = false;
    shownStudyId_.clear();
}

void SimManager::restoreModelMesh(app::AppState& st) {
    clearResultOverlay(st);
    // Force the next ensureDocLoaded (viewport.cpp, every frame) to reload the real, editable,
    // face-bearing doc mesh over the result overlay (which replaced it with an unpickable FEA
    // surface) — mirrors host's restoreModelMesh, minus the explicit re-activate() since the
    // new tree's per-frame ensureDocLoaded already re-derives from s.engine.aicmPath().
    st.viewMeshRev = -1;
}

void SimManager::syncResultView(app::AppState& st) {
    const bool valid = sel_ >= 0 && sel_ < (int)studies_.size();
    const bool wantShow = valid && node_ == SimNode::Results && studies_[sel_].hasResult &&
                          !studies_[sel_].overlayPath.empty();
    if (wantShow) {
        if (!resultShown_ || shownStudyId_ != studies_[sel_].id) {
            applyResultField(st, studies_[sel_]);
            resultShown_ = true;
            shownStudyId_ = studies_[sel_].id;
        }
    } else if (resultShown_) {
        restoreModelMesh(st);
    }
}

void SimManager::tick(app::AppState& st) {
    syncResultView(st);
    auto& job = st.simClient.job();
    const bool owns = (job.ownerDocId.empty() && running_) ||
                      (st.docMirror.hasDoc() && job.ownerDocId == st.docMirror.docId());
    if (!owns) return;

    {
        std::vector<core::SimProgress> evs;
        {
            std::lock_guard<std::mutex> lk(job.evMtx);
            if (!job.events.empty()) evs.swap(job.events);
        }
        for (auto& ev : evs) {
            if (!ev.text.empty() || !ev.stage.empty()) {
                // Bottom-left current operation: "Meshing — surface pass (42%)" so the user
                // can tell which part we are waiting on. Prefix the prettified stage (never
                // the raw token) and keep it free of "fail/warn/error" so the status-bar
                // colour classifier does not misread a normal solve as an error.
                auto pretty = [](const std::string& s) -> std::string {
                    if (s == "voxelize") return "Voxelizing";
                    if (s == "mesh")     return "Meshing";
                    if (s == "assemble") return "Assembling";
                    if (s == "solve")    return "Solving";
                    if (s == "stream")   return "Streaming results";
                    if (s == "post")     return "Post-processing";
                    if (s == "Starting") return "Starting";
                    return s;
                };
                std::string op = pretty(ev.stage);
                if (!ev.text.empty()) op += (op.empty() ? "" : " - ") + ev.text;
                if (ev.pct) op += " (" + std::to_string(ev.pct) + "%)";
                st.status = op;
            }
            if (ev.hasTrace) {
                int traceStudy = -1;
                if (!runningStudyId_.empty())
                    for (int i = 0; i < (int)studies_.size(); ++i)
                        if (studies_[i].id == runningStudyId_) { traceStudy = i; break; }
                if (traceStudy < 0 && runningStudy_ >= 0 && runningStudy_ < (int)studies_.size())
                    traceStudy = runningStudy_;
                if (traceStudy >= 0) studies_[traceStudy].dragTrace.push_back((float)ev.traceDrag);
            }
        }
    }

    if (job.ready.exchange(false)) {
        running_ = false;
        std::lock_guard<std::mutex> lk(job.mtx);
        int si = -1;
        if (!runningStudyId_.empty())
            for (int i = 0; i < (int)studies_.size(); ++i)
                if (studies_[i].id == runningStudyId_) { si = i; break; }
        if (si < 0) si = runningStudy_;
        runningStudyId_.clear();
        if (si < 0 || si >= (int)studies_.size()) return;
        Study& s = studies_[si];
        if (!job.ok) {
            s.summary = "Run failed: " + job.error;
            st.status = s.summary;
            c5::addLog(st, "[sim] " + s.summary);
            return;
        }
        const auto& res = job.result;
        s.hasResult = true;
        s.contourOn = true;  // T-SIM2: a fresh result shows its contour by default
        s.solverNotes.clear();
        if (res.contains("solver_notes") && res["solver_notes"].is_array())
            for (const auto& n : res["solver_notes"]) if (n.is_string()) s.solverNotes.push_back(n.get<std::string>());
        if (s.type == StudyType::Thermal) {
            // T-SIM1: steady-state conduction result — {summary:{max_temp_c,min_temp_c},
            // temperature[n], n_nodes,...}. Parsed here before the shared isFlowFamily branch
            // (which handles the OpenFOAM-CHT flow result) so the flow path stays untouched.
            s.nNodes = res.value("n_nodes", 0); s.nElems = res.value("n_elems", 0);
            s.nDof = res.value("n_dof", 0);
            s.resultSummary = (res.contains("summary") && res["summary"].is_object())
                                  ? res["summary"] : nlohmann::json::object();
            s.maxTempC = s.resultSummary.value("max_temp_c", 0.0);
            s.minTempC = s.resultSummary.value("min_temp_c", 0.0);
            char buf[128];
            std::snprintf(buf, sizeof(buf), "max %.1f degC | min %.1f degC (k=%.1f W/m.K)",
                          s.maxTempC, s.minTempC, s.thermalKwmk);
            s.summary = buf;
            resultShown_ = true; shownStudyId_ = s.id;
            c5::addLog(st, "[sim] thermal done: " + s.summary);
            persist(st, s);
            node_ = SimNode::Results;
            return;
        }
        if (isFlowFamily(s.type)) {
            s.dragN = res.value("drag_n", 0.0); s.liftN = res.value("lift_n", 0.0);
            s.sideN = res.value("side_n", 0.0); s.cd = res.value("cd", 0.0); s.cl = res.value("cl", 0.0);
            s.ld = res.value("ld", 0.0); s.reynolds = res.value("reynolds", 0.0);
            s.frontalMm2 = res.value("frontal_area_mm2", 0.0); s.refAreaMm2 = res.value("ref_area_mm2", s.frontalMm2);
            s.cpMin = res.value("cp_min", -1.0); s.cpMax = res.value("cp_max", 1.0);
            s.vmaxMps = res.value("vmax_mps", s.flowSpeed * 1.5);
            s.flowFields.clear();
            if (res.contains("scalar_fields") && res["scalar_fields"].is_array())
                for (const auto& f : res["scalar_fields"]) if (f.is_string()) s.flowFields.push_back(f.get<std::string>());
            bool isThermal = false;
            for (const auto& f : s.flowFields) if (f == "temperature") isThermal = true;
            char buf[256];
            if (isThermal)
                std::snprintf(buf, sizeof(buf), "Surface %.0f-%.0f degC | inlet %.0f degC",
                              s.cpMin, s.cpMax, s.flowInletTempC);
            else
                std::snprintf(buf, sizeof(buf), "Drag %.3g N | Lift %.3g N | Cd %.3f | Cl %.3f",
                              s.dragN, s.liftN, s.cd, s.cl);
            s.summary = buf;
            s.banner = res.value("reynolds_note", std::string());
            s.overlayPath = res.value("overlay_aicm", res.value("lines_aicm", std::string()));
            activeScalarField_ = isThermal ? "temperature" : "pressure_cp";
            applyResultField(st, s);
            resultShown_ = true; shownStudyId_ = s.id;
            c5::addLog(st, "[sim] flow done: " + s.summary);
        } else {
            s.nNodes = res.value("n_nodes", 0); s.nElems = res.value("n_elems", 0); s.nDof = res.value("n_dof", 0);
            s.resultSummary = (res.contains("summary") && res["summary"].is_object()) ? res["summary"] : nlohmann::json::object();
            s.overlayPath = res.value("overlay_aicm", std::string());
            activeScalarField_ = (s.type == StudyType::Modal)   ? "mode_amplitude"
                               : (s.type == StudyType::Fatigue) ? "fatigue_fos" : "von_mises";
            applyResultField(st, s);
            resultShown_ = true; shownStudyId_ = s.id;
            s.freqs.clear();
            if (s.type == StudyType::Modal && res.contains("freqs_hz") && res["freqs_hz"].is_array()) {
                std::string f = "Frequencies (Hz):";
                for (auto& v : res["freqs_hz"]) {
                    s.freqs.push_back((float)v.get<double>());
                    char b[32]; std::snprintf(b, sizeof(b), " %.1f", v.get<double>()); f += b;
                }
                s.summary = f;
            } else if (s.type == StudyType::Fatigue && !s.resultSummary.empty()) {
                const auto& sm = s.resultSummary;
                const double SeC = sm.value("se_corrected_mpa", 0.0), Sut = sm.value("sut_mpa", 0.0);
                const double SeD = reliability_ke(s.fatigueReliability) * SeC;
                const int m = (s.fatigueMethod < 0 || s.fatigueMethod > 4) ? 0 : s.fatigueMethod;
                double saeq = 0.0;
                if (sm.contains("theories") && sm["theories"].contains(kTheoryKeys[m]))
                    saeq = sm["theories"][kTheoryKeys[m]].value("sa_eq_mpa", 0.0);
                const double fos = (saeq > 1e-9) ? SeD / saeq : 15.0;
                const double life = sn_life(saeq, SeD, Sut);
                char buf[256];
                if (life >= 1e6)
                    std::snprintf(buf, sizeof(buf), "%s FoS %.2f | life: infinite (>1e6 cycles)", kTheoryNames[m], fos);
                else
                    std::snprintf(buf, sizeof(buf), "%s FoS %.2f | life %.3g cycles", kTheoryNames[m], fos, life);
                s.summary = buf;
            } else if (!s.resultSummary.empty()) {
                const auto& sm = s.resultSummary;
                char buf[256];
                std::snprintf(buf, sizeof(buf), "max vM %.2f MPa | max disp %.4f mm | min FoS %.2f",
                              sm.value("max_von_mises_mpa", 0.0), sm.value("max_disp_mm", 0.0), sm.value("min_fos", 0.0));
                s.summary = buf;
            }
            c5::addLog(st, "[sim] FEA done: " + s.summary);
            // T-SIM3: advisor postcheck — margin verdict (PASS/WARN/FAIL vs margin_policy.json)
            // + narration, rendered with the results (drawResultControls). Static only; the
            // policy is a stress-vs-yield rule.
            if (s.type == StudyType::Static) advisorPostcheck(st, s);
        }
        persist(st, s);
        node_ = SimNode::Results;
    }
}

// ---- pre-flight readiness gate ----
static bool studyReady(const Study& s, std::string& blocker) {
    // T-SIM1: a thermal conduction study needs a material (for conductivity) and at least one
    // fixed-temperature (hot) face. Checked before the isFlowFamily early-return, which stays
    // the gate for the actual Flow study.
    if (s.type == StudyType::Thermal) {
        if (s.materialKey.empty()) {
            blocker = "Material required - pick a material with a thermal conductivity.";
            return false;
        }
        if (s.fixtures.empty()) {
            blocker = "At least one fixed-temperature (hot) face is required.";
            return false;
        }
        return true;
    }
    if (isFlowFamily(s.type)) return true;
    if (s.materialKey.empty()) { blocker = "Material required - pick a material before running."; return false; }
    if (s.type != StudyType::Modal && s.fixtures.empty()) {
        blocker = "At least one fixed face is required to constrain the part.";
        return false;
    }
    if (s.type == StudyType::Fatigue) {
        int mi = SimManager::materialIndexByKey(s.materialKey);
        double matSut = (mi >= 0) ? SimManager::materials()[mi].ultimate_mpa : 0.0;
        if (s.fatigueSut <= 0.0f && matSut <= 0.0) {
            blocker = "Fatigue study needs ultimate strength (Sut) - set it in Fatigue parameters "
                      "or choose a material with Sut data.";
            return false;
        }
    }
    return true;
}

// ============================================================ draw

static const char* kPresetLabels[4] = {
    "Coarse  (~23 s)", "Medium  (~2 min)", "Fine  (~16 min)", "Extra fine  (~73 min)"};

void SimManager::drawEmbedded(app::AppState& st, const ImVec2& vpMin, const ImVec2& vpMax) {
    const float S = theme::S();
    const float colW = 360.f * S;
    // T-SIM3: dodge the pinned FeatureManager column (overlay slot 0) instead of pinning at
    // the same left edge underneath it — the studies panel was buried under the Model Tree.
    const float fmEdge = overlayColumnEdge(0);
    const float colX = std::max(vpMin.x, fmEdge > 0.f ? fmEdge : vpMin.x) + 8.f * S;
    ImVec2 colMin(colX, vpMin.y + 8.f * S);
    float colH = std::max(200.f * S, (vpMax.y - vpMin.y) - 16.f * S);
    ImGui::SetCursorScreenPos(colMin);
    ImGui::PushStyleColor(ImGuiCol_ChildBg, c5::glassBg());
    ImGui::PushStyleColor(ImGuiCol_Border, c5::glassBorder());
    ImGui::BeginChild("##sim_manager_col", ImVec2(colW, colH), ImGuiChildFlags_Border);

    if (st.docMirror.hasDoc() &&
        (lastDocId_ != st.docMirror.docId() || lastRevision_ != (long)st.docMirror.revision()))
        refresh(st);

    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextColored(c5::accent(), "Simulation");
    ImGui::PopFont();
    ImGui::Separator();

    if (ImGui::Button("+ New Study", ImVec2(-FLT_MIN, 0)))
        ImGui::OpenPopup("##newStudyPicker");
    simTip("Add a simulation study to this part: Static, Fatigue, Frequency, Flow, or Thermal.");
    if (ImGui::BeginPopup("##newStudyPicker")) {
        struct SEntry { StudyType type; const char* name; const char* desc; };
        static const SEntry kEntries[] = {
            {StudyType::Static,  "Static",    "Linear stress, displacement and factor of safety."},
            {StudyType::Fatigue, "Fatigue",   "Cyclic loading life via Goodman/Gerber/Soderberg."},
            {StudyType::Modal,   "Frequency", "Natural frequencies and mode shapes."},
            {StudyType::Flow,    "Flow",      "External aerodynamics: drag, lift, streamlines."},
            {StudyType::Thermal, "Thermal",   "Heat transfer via OpenFOAM convection / CHT."},
        };
        for (const auto& e : kEntries) {
            ImGui::PushID((int)e.type);
            IconUV iv = getIcon(studyTypeIcon(e.type), 20);
            if (iv.valid) { ImGui::Image(iv.texId, ImVec2(16*S, 16*S), iv.uv0, iv.uv1); ImGui::SameLine(0, 6*S); }
            bool hit = ImGui::Selectable(e.name, false, 0, ImVec2(80.f * S, 0));
            ImGui::SameLine(0, 8 * S);
            ImGui::TextColored(c5::txtDim(), "%s", e.desc);
            if (hit) { newStudy(st, e.type); ImGui::CloseCurrentPopup(); }
            ImGui::PopID();
        }
        ImGui::EndPopup();
    }
    ImGui::Spacing();

    if (!st.docMirror.hasDoc()) {
        ImGui::TextWrapped("Open a part to set up a simulation study.");
        ImGui::EndChild(); ImGui::PopStyleColor(2);
        return;
    }
    if (studies_.empty()) {
        ImGui::Separator();
        ImGui::TextColored(c5::txtDim(), "%s",
            "No studies yet. Create a study above to add fixtures, loads, and run an analysis.");
        ImGui::EndChild(); ImGui::PopStyleColor(2);
        return;
    }

    ImGui::Separator();
    for (int i = 0; i < (int)studies_.size(); ++i) drawStudyRow(st, i);

    if (sel_ >= 0 && sel_ < (int)studies_.size()) {
        ImGui::Spacing(); ImGui::Separator(); ImGui::Spacing();
        drawForm(st, studies_[sel_]);
    }

    ImGui::EndChild();
    ImGui::PopStyleColor(2);
}

void SimManager::drawStudyRow(app::AppState& st, int i) {
    Study& s = studies_[i];
    ImGui::PushID(i);
    const bool selStudy = (sel_ == i);
    ImGuiTreeNodeFlags flags = ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanAvailWidth |
                               ImGuiTreeNodeFlags_DefaultOpen;
    if (selStudy && node_ == SimNode::Study) flags |= ImGuiTreeNodeFlags_Selected;
    IconUV tIcon = getIcon(studyTypeIcon(s.type), 20);
    if (tIcon.valid) { ImGui::Image(tIcon.texId, ImVec2(16*theme::S(), 16*theme::S()), tIcon.uv0, tIcon.uv1); ImGui::SameLine(0, 4*theme::S()); }
    std::string title = s.name + "  (" + studyTypeLabel(s.type) + ")";
    bool open = ImGui::TreeNodeEx("##study", flags, "%s", title.c_str());
    if (ImGui::IsItemClicked() && !ImGui::IsItemToggledOpen()) { sel_ = i; node_ = SimNode::Study; }
    bool wantDelete = false;
    if (ImGui::BeginPopupContextItem("##studyctx")) {
        if (ImGui::MenuItem("Delete study")) wantDelete = true;
        ImGui::EndPopup();
    }
    int pendingDeleteLoad = -1;  // T-SIM3: per-load delete from the tree (applied after the loop)
    if (open) {
        auto child = [&](const char* icon, const std::string& label, SimNode n, bool ok, const char* tip, int loadI = -1) {
            const bool selected = selStudy && node_ == n && (n != SimNode::Load || loadIdx_ == loadI);
            IconUV iv = getIcon(icon, 20);
            if (iv.valid) { ImGui::Image(iv.texId, ImVec2(15*theme::S(), 15*theme::S()), iv.uv0, iv.uv1); ImGui::SameLine(0, 5*theme::S()); }
            if (ImGui::Selectable(label.c_str(), selected)) { sel_ = i; node_ = n; loadIdx_ = loadI; }
            // T-SIM3: loads get a right-click Delete on their tree row (the form's X remains).
            if (n == SimNode::Load && loadI >= 0) {
                if (ImGui::BeginPopupContextItem((label + "##loadctx").c_str())) {
                    if (ImGui::MenuItem("Delete load")) pendingDeleteLoad = loadI;
                    ImGui::EndPopup();
                }
            }
            ImGui::SameLine();
            if (ok) {
                // T-SIM3 style pass: U+2022 "•" is not in the app font atlas (it rendered as
                // '?', indistinguishable from the warn marker) — draw the status dot directly.
                ImDrawList* dl = ImGui::GetWindowDrawList();
                const ImVec2 p = ImGui::GetCursorScreenPos();
                const float r = 2.5f * theme::S();
                const float lh = ImGui::GetTextLineHeight();
                dl->AddCircleFilled(ImVec2(p.x + r, p.y + lh * 0.55f), r,
                                    ImGui::ColorConvertFloat4ToU32(c5::accent()));
                ImGui::Dummy(ImVec2(2.f * r + 2.f, lh));
            } else {
                ImGui::TextColored(c5::warnCol(), "?");
            }
            simTip(tip);
        };
        const std::string matName = [&]{
            int mi = materialIndexByKey(s.materialKey);
            return mi >= 0 ? materials()[mi].name : std::string("(not set)");
        }();
        if (!isFlowFamily(s.type))
            child("material", "Material: " + matName, SimNode::Material, !s.materialKey.empty(),
                  s.materialKey.empty() ? "No material assigned - pick one before running." : "");
        if (!isFlowFamily(s.type)) {
            bool hasFix = !s.fixtures.empty();
            child("fixture", "Fixtures (" + s.fixtures.label() + ")", SimNode::Fixtures, hasFix,
                  hasFix ? "" : (s.type == StudyType::Modal ? "No fixtures - frequency study will run free-free."
                                                             : "A static study needs at least one fixed face."));
            if (s.type != StudyType::Modal) {
                for (int li = 0; li < (int)s.loads.size(); ++li) {
                    const SimLoad& ld = s.loads[li];
                    std::string lbl = (ld.pressure ? "Pressure " : "Force ") + std::to_string(li + 1) +
                                      " (" + ld.faces.label() + ")";
                    child(ld.pressure ? "load_pressure" : "load_force", lbl, SimNode::Load, !ld.faces.empty(),
                          ld.faces.empty() ? "This load has no faces selected." : "", li);
                }
            }
            child("mesh", "Mesh", SimNode::Mesh, true, "Mesh density for the solve.");
        }
        // T-SIM1: Thermal (conduction) tree — a self-contained ADDITIVE branch (does not touch
        // the shared !isFlowFamily block above or the Flow tree). Material / fixed-temp faces /
        // cold-boundary loads / mesh, then the shared Results node below.
        if (s.type == StudyType::Thermal) {
            child("material", "Material: " + matName, SimNode::Material, !s.materialKey.empty(),
                  s.materialKey.empty() ? "Pick a material with a thermal conductivity." : "");
            bool hasFix = !s.fixtures.empty();
            child("fixture", "Fixed-temp faces (" + s.fixtures.label() + ")", SimNode::Fixtures, hasFix,
                  hasFix ? "" : "A thermal study needs at least one fixed-temperature face.");
            for (int li = 0; li < (int)s.loads.size(); ++li) {
                const SimLoad& ld = s.loads[li];
                std::string lbl = "Cold boundary " + std::to_string(li + 1) + " (" + ld.faces.label() + ")";
                child("load_pressure", lbl, SimNode::Load, !ld.faces.empty(),
                      ld.faces.empty() ? "This boundary has no faces selected." : "", li);
            }
            child("mesh", "Mesh", SimNode::Mesh, true, "Mesh density for the solve.");
        }
        child("results", s.hasResult ? "Results" : "Results (not run)", SimNode::Results, s.hasResult,
              s.hasResult ? "" : "Run the study to generate results.");
        ImGui::TreePop();
    }
    if (pendingDeleteLoad >= 0 && pendingDeleteLoad < (int)s.loads.size()) {
        s.loads.erase(s.loads.begin() + pendingDeleteLoad);
        if (node_ == SimNode::Load) {
            if (loadIdx_ > pendingDeleteLoad) --loadIdx_;
            else if (loadIdx_ == pendingDeleteLoad) { node_ = SimNode::Study; loadIdx_ = -1; }
        }
        persist(st, s);
    }
    ImGui::PopID();
    if (wantDelete) deleteStudy(st, i);
}

void SimManager::drawCustomMaterialPopup(app::AppState& st, Study& s) {
    if (ImGui::BeginPopupModal("New material", nullptr, ImGuiWindowFlags_AlwaysAutoResize)) {
        const float S = theme::S();
        ImGui::TextUnformatted("Define a custom material");
        ImGui::TextColored(c5::txtDim(), "Saved to your library; available in every study.");
        ImGui::Separator();
        ImGui::TextUnformatted("Name"); ImGui::SetNextItemWidth(240*S);
        ImGui::InputText("##cmname", cmName_, sizeof(cmName_));
        ImGui::TextUnformatted("Young's modulus E (MPa)"); ImGui::SetNextItemWidth(240*S);
        ImGui::DragFloat("##cme", &cmE_, 100.0f, 1.0f, 1.0e6f, "%.0f MPa");
        ImGui::TextUnformatted("Poisson ratio"); ImGui::SetNextItemWidth(240*S);
        ImGui::SliderFloat("##cmnu", &cmNu_, 0.0f, 0.499f, "%.3f");
        ImGui::TextUnformatted("Density (g/cm^3)"); ImGui::SetNextItemWidth(240*S);
        ImGui::DragFloat("##cmrho", &cmDensity_, 0.01f, 0.0f, 25.0f, "%.3f g/cm3");
        ImGui::TextUnformatted("Yield strength (MPa)"); ImGui::SetNextItemWidth(240*S);
        ImGui::DragFloat("##cmyield", &cmYield_, 1.0f, 0.0f, 1.0e5f, "%.0f MPa");
        ImGui::TextUnformatted("Ultimate strength Sut (MPa)"); ImGui::SetNextItemWidth(240*S);
        ImGui::DragFloat("##cmult", &cmUlt_, 1.0f, 0.0f, 1.0e5f, "%.0f MPa");
        ImGui::TextUnformatted("Endurance limit Se (MPa)"); ImGui::SetNextItemWidth(240*S);
        ImGui::DragFloat("##cmend", &cmEnd_, 1.0f, 0.0f, 1.0e5f, "%.0f MPa");
        ImGui::Separator();
        const bool ok = cmName_[0] != '\0' && cmE_ > 0.0f && cmNu_ > 0.0f && cmNu_ < 0.5f;
        if (!ok) ImGui::BeginDisabled();
        if (ImGui::Button("Add", ImVec2(120*S, 0))) {
            Material m; m.name = cmName_; m.E_mpa = cmE_; m.nu = cmNu_; m.density_g_cm3 = cmDensity_;
            m.yield_mpa = cmYield_; m.ultimate_mpa = cmUlt_; m.endurance_mpa = cmEnd_;
            s.materialKey = addCustomMaterial(m);
            persist(st, s);
            ImGui::CloseCurrentPopup();
        }
        if (!ok) ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(120*S, 0))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }
}

// T-SIM3: modal material-assignment dialog. Sources, in order: the document's own material
// (quick-assign chip), then the global library = built-in engine/sim/materials.json + the
// user's custom_materials.json (via materials()). Every number shown comes from those data
// tables — provenance rule, nothing invented in the GUI. Assignment is whole-body: it sets
// Study::materialKey (per-face assignment needs solver-side regions; out of scope, cheap it
// is not).
void SimManager::drawMaterialDialog(app::AppState& st, Study& s) {
    const float S = theme::S();
    if (matDialogOpen_) {
        ImGui::OpenPopup("Assign material");
        matDialogOpen_ = false;
        matFilter_[0] = '\0';
        matDialogSel_ = materialIndexByKey(s.materialKey);
    }
    ImGui::SetNextWindowSize(ImVec2(560 * S, 0), ImGuiCond_Appearing);
    if (!ImGui::BeginPopupModal("Assign material", nullptr, ImGuiWindowFlags_AlwaysAutoResize))
        return;
    const auto& mats = materials();

    // Document material quick-assign (when the part carries one that the library resolves).
    std::string partKey;
    const nlohmann::json& tr = st.docMirror.tree();
    if (tr.is_object() && tr.contains("material") && tr["material"].is_object())
        partKey = tr["material"].value("key", std::string());
    const int partIdx = partKey.empty() ? -1 : materialIndexByKey(partKey);
    if (partIdx >= 0) {
        ImGui::TextColored(c5::txtDim(), "Part material:");
        ImGui::SameLine();
        if (ImGui::SmallButton((mats[partIdx].name + "##partmat").c_str())) {
            s.materialKey = mats[partIdx].key;
            persist(st, s);
            ImGui::CloseCurrentPopup();
        }
        simTip("Assign the document's own material to this study.");
        ImGui::Spacing();
    }

    ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputTextWithHint("##matfilter", "Filter by name...", matFilter_, sizeof(matFilter_));
    ImGui::Spacing();

    auto passesFilter = [&](const Material& m) {
        if (!matFilter_[0]) return true;
        auto lower = [](std::string v) {
            for (char& c : v) c = (char)std::tolower((unsigned char)c);
            return v;
        };
        const std::string f = lower(matFilter_);
        return lower(m.name).find(f) != std::string::npos ||
               lower(m.key).find(f) != std::string::npos;
    };
    if (ImGui::BeginTable("##matlib", 5,
                          ImGuiTableFlags_RowBg | ImGuiTableFlags_BordersInnerH |
                          ImGuiTableFlags_ScrollY | ImGuiTableFlags_SizingStretchProp,
                          ImVec2(-FLT_MIN, 260 * S))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Material", ImGuiTableColumnFlags_WidthStretch, 2.2f);
        ImGui::TableSetupColumn("E (MPa)");
        ImGui::TableSetupColumn("Yield (MPa)");
        ImGui::TableSetupColumn("Density (g/cm3)");
        ImGui::TableSetupColumn("Library");
        ImGui::TableHeadersRow();
        for (int i = 0; i < (int)mats.size(); ++i) {
            const auto& m = mats[i];
            if (!passesFilter(m)) continue;
            ImGui::PushID(i);
            ImGui::TableNextRow();
            ImGui::TableNextColumn();
            const bool cur = (m.key == s.materialKey);
            if (ImGui::Selectable(m.name.c_str(), matDialogSel_ == i,
                                  ImGuiSelectableFlags_SpanAllColumns |
                                  ImGuiSelectableFlags_AllowDoubleClick)) {
                matDialogSel_ = i;
                if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
                    s.materialKey = m.key;
                    persist(st, s);
                    ImGui::PopID();
                    ImGui::EndTable();
                    ImGui::CloseCurrentPopup();
                    ImGui::EndPopup();
                    return;
                }
            }
            if (cur) { ImGui::SameLine(); ImGui::TextColored(c5::accent(), "\xe2\x80\xa2"); }
            ImGui::TableNextColumn(); ImGui::Text("%.0f", m.E_mpa);
            ImGui::TableNextColumn(); ImGui::Text("%.0f", m.yield_mpa);
            ImGui::TableNextColumn(); ImGui::Text("%.2f", m.density_g_cm3);
            ImGui::TableNextColumn();
            ImGui::TextColored(c5::txtDim(), "%s", m.custom ? "custom" : "built-in");
            ImGui::PopID();
        }
        ImGui::EndTable();
    }
    ImGui::TextColored(c5::txtDim(),
                       "Values from engine/sim/materials.json + your custom library.");
    ImGui::Spacing();

    const bool haveSel = matDialogSel_ >= 0 && matDialogSel_ < (int)mats.size();
    if (!haveSel) ImGui::BeginDisabled();
    if (ImGui::Button("Assign", ImVec2(120 * S, 0))) {
        s.materialKey = mats[matDialogSel_].key;
        persist(st, s);
        ImGui::CloseCurrentPopup();
    }
    if (!haveSel) ImGui::EndDisabled();
    simTip("Assign the selected material to this study (whole body).");
    ImGui::SameLine();
    if (ImGui::Button("New custom material...", ImVec2(200 * S, 0))) {
        if (haveSel) {
            std::snprintf(cmName_, sizeof(cmName_), "%s", (mats[matDialogSel_].name + " (copy)").c_str());
            cmE_ = (float)mats[matDialogSel_].E_mpa; cmNu_ = (float)mats[matDialogSel_].nu;
            cmDensity_ = (float)mats[matDialogSel_].density_g_cm3;
            cmYield_ = (float)mats[matDialogSel_].yield_mpa;
            cmUlt_ = (float)mats[matDialogSel_].ultimate_mpa;
            cmEnd_ = (float)mats[matDialogSel_].endurance_mpa;
        }
        ImGui::OpenPopup("New material");
    }
    drawCustomMaterialPopup(st, s);  // nested modal; adds to the custom library + assigns
    ImGui::SameLine();
    if (ImGui::Button("Cancel", ImVec2(120 * S, 0))) ImGui::CloseCurrentPopup();
    ImGui::EndPopup();
}

void SimManager::drawMaterialCombo(app::AppState& st, Study& s) {
    ImGui::SeparatorText("Material");
    const float S = theme::S();
    const float fh = ImGui::GetFrameHeight();
    const auto& mats = materials();
    int mi = materialIndexByKey(s.materialKey);
    const char* cur = mi >= 0 ? mats[mi].name.c_str() : "(select material)";
    ImGui::SetNextItemWidth(-(fh + 6*S));
    if (ImGui::BeginCombo("##material", cur)) {
        bool customHeader = false;
        for (int i = 0; i < (int)mats.size(); ++i) {
            if (mats[i].custom && !customHeader) { ImGui::Separator(); ImGui::TextDisabled("Custom materials"); customHeader = true; }
            if (ImGui::Selectable(mats[i].name.c_str(), mi == i)) { s.materialKey = mats[i].key; persist(st, s); }
        }
        ImGui::EndCombo();
    }
    simTip("Material defines stiffness (E), Poisson ratio, density and yield strength used for the stress / FoS solve.");
    ImGui::SameLine(0, 4*S);
    if (ImGui::Button("+##newmat", ImVec2(fh, fh))) {
        if (mi >= 0) {
            std::snprintf(cmName_, sizeof(cmName_), "%s", (mats[mi].name + " (copy)").c_str());
            cmE_ = (float)mats[mi].E_mpa; cmNu_ = (float)mats[mi].nu; cmDensity_ = (float)mats[mi].density_g_cm3;
            cmYield_ = (float)mats[mi].yield_mpa; cmUlt_ = (float)mats[mi].ultimate_mpa; cmEnd_ = (float)mats[mi].endurance_mpa;
        }
        ImGui::OpenPopup("New material");
    }
    drawCustomMaterialPopup(st, s);
    if (mi >= 0) {
        const auto& m = mats[mi];
        ImGui::TextColored(c5::txtDim(), "E %.0f MPa  -  nu %.2f  -  yield %.0f MPa  -  rho %.2f g/cm3",
                           m.E_mpa, m.nu, m.yield_mpa, m.density_g_cm3);
    }
    // T-SIM3: full library browser (doc material + built-ins + custom, filter + properties).
    if (ImGui::Button("Browse material library...", ImVec2(-FLT_MIN, 0)))
        matDialogOpen_ = true;
    simTip("Pick from the document material and the material library, with the table values shown.");
}

void SimManager::drawWellRow(app::AppState& st, const char* label, SimWell& well, const char* tip) {
    ImGui::TextUnformatted(label); simTip(tip);
    if (ImGui::Button("Add selected faces", ImVec2(-FLT_MIN, 0))) {
        captureSelectionInto(st, well);
        if (sel_ >= 0 && sel_ < (int)studies_.size()) persist(st, studies_[sel_]);
    }
    simTip("Select faces in the viewport (click / ctrl-click), then click here to add them.");
    if (!well.picks.empty()) {
        // T-SIM3: per-entry delete is a visible affordance (an "x" beside each captured
        // face), not just the old right-click gesture (which still works).
        const float S = theme::S();
        for (int k = 0; k < (int)well.picks.size(); ++k) {
            ImGui::PushID(k);
            ImGui::SmallButton(("Face " + std::to_string(k + 1)).c_str());
            bool remove = ImGui::IsItemClicked(ImGuiMouseButton_Right);
            simTip("A captured face. Right-click (or the x) removes it.");
            ImGui::SameLine(0, 2.f * S);
            remove = ImGui::SmallButton("x") || remove;
            simTip("Remove this face from the list.");
            if (remove) {
                well.picks.erase(well.picks.begin() + k);
                if (sel_ >= 0 && sel_ < (int)studies_.size()) persist(st, studies_[sel_]);
                ImGui::PopID();
                break;
            }
            if (k % 3 != 2 && k + 1 < (int)well.picks.size()) ImGui::SameLine(0, 8.f * S);
            ImGui::PopID();
        }
        ImGui::SameLine(0, 8.f * S);
        if (ImGui::SmallButton("Clear")) { well.picks.clear(); if (sel_ >= 0 && sel_ < (int)studies_.size()) persist(st, studies_[sel_]); }
        simTip("Remove all captured faces.");
    }
}

void SimManager::drawFixturesForm(app::AppState& st, Study& s) {
    // T-SIM3 style pass: forms use SeparatorText section headers (the glass-column kit
    // language the fatigue form already speaks) instead of ad-hoc H2 text.
    ImGui::SeparatorText(s.type == StudyType::Modal ? "Fixtures (optional)" : "Fixtures");
    ImGui::TextWrapped("Faces that are clamped (zero displacement).");
    ImGui::Spacing();
    drawWellRow(st, "Fixed faces", s.fixtures, "Faces held rigidly in place.");
}

void SimManager::drawLoadsForm(app::AppState& st, Study& s) {
    ImGui::SeparatorText("Loads");
    const float S = theme::S();
    for (int li = 0; li < (int)s.loads.size(); ++li) {
        SimLoad& ld = s.loads[li];
        ImGui::PushID(li);
        ImGui::Separator();
        std::string hdr = (ld.pressure ? "Pressure " : "Force ") + std::to_string(li + 1);
        ImGui::TextUnformatted(hdr.c_str());
        ImGui::SameLine(ImGui::GetContentRegionAvail().x - 22*S);
        if (ImGui::SmallButton("X")) { s.loads.erase(s.loads.begin() + li); persist(st, s); ImGui::PopID(); break; }
        drawWellRow(st, "Faces", ld.faces, "Faces the load is applied to.");
        if (ImGui::Checkbox("Pressure", &ld.pressure)) persist(st, s);
        UnitSys u = unitSys(st);
        if (ld.pressure) {
            ImGui::Text("Magnitude (into faces)");
            float disp = ld.pressureMpa * (float)u.pressFromMpa;
            ImGui::SetNextItemWidth(-FLT_MIN);
            char pfmt[24]; std::snprintf(pfmt, sizeof(pfmt), "%%.4g %s", u.press);
            ImGui::InputFloat("##pmag", &disp, 0, 0, pfmt);
            if (ImGui::IsItemDeactivatedAfterEdit()) { ld.pressureMpa = disp / (float)u.pressFromMpa; persist(st, s); }
        } else {
            ImGui::Text("Force (world X / Y / Z)");
            float disp[3] = {ld.vec[0]*(float)u.forceFromN, ld.vec[1]*(float)u.forceFromN, ld.vec[2]*(float)u.forceFromN};
            ImGui::SetNextItemWidth(-FLT_MIN);
            char ffmt[24]; std::snprintf(ffmt, sizeof(ffmt), "%%.4g %s", u.force);
            ImGui::InputFloat3("##fvec", disp, ffmt);
            if (ImGui::IsItemDeactivatedAfterEdit()) {
                for (int k = 0; k < 3; ++k) ld.vec[k] = disp[k] / (float)u.forceFromN;
                persist(st, s);
            }
        }
        ImGui::PopID();
    }
    ImGui::Spacing();
    if (ImGui::Button("+ Force")) { s.loads.push_back(SimLoad{}); persist(st, s); node_ = SimNode::Load; loadIdx_ = (int)s.loads.size()-1; }
    ImGui::SameLine();
    if (ImGui::Button("+ Pressure")) { SimLoad l; l.pressure = true; s.loads.push_back(l); persist(st, s); node_ = SimNode::Load; loadIdx_ = (int)s.loads.size()-1; }
}

void SimManager::drawMeshForm(app::AppState& st, Study& s) {
    ImGui::SeparatorText("Mesh");
    ImGui::TextWrapped("The mesh the solve runs on. Finer captures stress concentrations better but takes longer.");
    ImGui::Spacing();
    const bool modal = (s.type == StudyType::Modal);
    if (modal && !s.loads.empty()) { s.loads.clear(); persist(st, s); }
    ImGui::TextUnformatted("Mesh type");
    static const char* kType[2] = {"Tetrahedral (any shape)", "Hexahedral (box / prismatic only)"};
    if (modal) ImGui::BeginDisabled();
    int ti = (s.meshHex && !modal) ? 1 : 0;
    for (int i = 0; i < 2; ++i)
        if (ImGui::RadioButton(kType[i], ti == i) && ti != i) { s.meshHex = (i == 1); persist(st, s); }
    if (modal) ImGui::EndDisabled();
    ImGui::Spacing();
    const bool hexMesh = s.meshHex && !modal;

    ImGui::TextUnformatted("Mesh fineness");
    ImGui::SetNextItemWidth(-FLT_MIN);
    const char* fineWord = s.meshFineness < 0.2f ? "Coarse" : s.meshFineness < 0.4f ? "Coarse-medium"
                          : s.meshFineness < 0.6f ? "Medium" : s.meshFineness < 0.8f ? "Fine-medium" : "Fine";
    ImGui::SliderFloat("##fineness", &s.meshFineness, 0.0f, 1.0f, fineWord);
    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    ImGui::Spacing();

    if (hexMesh) {
        ImGui::TextDisabled("Regular hex8 grid. The part must be a box / rectangular prism.");
    } else {
        if (ImGui::Checkbox("Refine curved features (holes, fillets)", &s.meshCurvature)) persist(st, s);
        ImGui::Spacing();
        ImGui::TextUnformatted("Element order");
        static const char* kOrder[2] = {"Draft - linear (1st order, fast)", "High quality - quadratic (2nd order)"};
        int oi = (!modal && s.meshOrder == 2) ? 1 : 0;
        if (modal) ImGui::BeginDisabled();
        for (int i = 0; i < 2; ++i)
            if (ImGui::RadioButton(kOrder[i], oi == i) && oi != i) { s.meshOrder = i ? 2 : 1; persist(st, s); }
        if (modal) { ImGui::EndDisabled(); ImGui::TextDisabled("Frequency studies always use 1st-order elements."); }
        ImGui::Spacing();
        if (modal) ImGui::BeginDisabled();
        if (ImGui::Checkbox("Optimize element quality (Netgen)", &s.meshOptimize)) persist(st, s);
        if (modal) ImGui::EndDisabled();
        ImGui::Spacing();
        if (ImGui::CollapsingHeader("Advanced")) {
            ImGui::Indent();
            bool edited = false;
            if (s.meshCurvature) {
                ImGui::TextUnformatted("Curvature refinement (elements per circle)");
                ImGui::SetNextItemWidth(-FLT_MIN);
                ImGui::SliderFloat("##curvper", &s.meshCurvPerCircle, 6.0f, 24.0f, "%.0f");
                edited = edited || ImGui::IsItemDeactivatedAfterEdit();
            }
            ImGui::TextUnformatted("Element size override (mm, 0 = auto)");
            float target = s.meshTargetMm, minSize = s.meshMinMm;
            ImGui::SetNextItemWidth(-FLT_MIN); ImGui::InputFloat("##target_size", &target, 0.0f, 0.0f, "%.4g");
            edited = edited || ImGui::IsItemDeactivatedAfterEdit();
            ImGui::SetNextItemWidth(-FLT_MIN); ImGui::InputFloat("##min_size", &minSize, 0.0f, 0.0f, "%.4g");
            edited = edited || ImGui::IsItemDeactivatedAfterEdit();
            if (edited) { s.meshTargetMm = std::max(0.0f, target); s.meshMinMm = std::max(0.0f, minSize); persist(st, s); }
            ImGui::Spacing();
            ImGui::TextUnformatted("3D algorithm");
            static const char* kAlgo[2] = {"Delaunay (robust)", "HXT (fast, parallel)"};
            int ai = (s.meshAlgo == 10) ? 1 : 0;
            ImGui::SetNextItemWidth(-FLT_MIN);
            if (ImGui::BeginCombo("##meshalgo", kAlgo[ai])) {
                for (int i = 0; i < 2; ++i)
                    if (ImGui::Selectable(kAlgo[i], ai == i)) { s.meshAlgo = i ? 10 : 1; persist(st, s); }
                ImGui::EndCombo();
            }
            ImGui::Unindent();
        }
        ImGui::Spacing();
    }

    bool busy = st.simClient.busy();
    if (busy) ImGui::BeginDisabled();
    if (ImGui::Button("Preview mesh", ImVec2(-FLT_MIN, 28*theme::S()))) previewMesh(st, s);
    if (busy) ImGui::EndDisabled();
    simTip("Mesh the part now and show node/element counts + quality, without running the solve.");

    if (s.nNodes > 0 || s.nElems > 0) {
        ImGui::Spacing();
        ImGui::Text("Nodes: %d", s.nNodes);
        ImGui::Text("Elements: %d", s.nElems);
    }
    if (s.meshHasQuality) {
        ImGui::Spacing();
        ImGui::SeparatorText("Mesh quality");
        ImGui::Text("Aspect ratio: %.2f avg, %.2f worst", s.meshAspectAvg, s.meshAspectMax);
        ImGui::Text("Min Jacobian: %.3f", s.meshJacobianMin);
        ImVec4 poorCol = s.meshPctPoor < 1.0f ? c5::accent() : s.meshPctPoor < 5.0f ? c5::warnCol() : c5::errCol();
        ImGui::TextColored(poorCol, "Poor elements: %.1f%%", s.meshPctPoor);
        if (s.meshAspectHist.size() == 10)
            ImGui::PlotHistogram("##aspecthist", s.meshAspectHist.data(), 10, 0, "aspect 1 -> 10",
                                 0.0f, FLT_MAX, ImVec2(-FLT_MIN, 40*theme::S()));
    }
}

void SimManager::drawFatigueForm(app::AppState& st, Study& s) {
    const float S = theme::S();
    UnitSys u = unitSys(st);
    const int mati = materialIndexByKey(s.materialKey);
    const double matSut = mati >= 0 ? materials()[mati].ultimate_mpa : 0.0;
    const double matSe = mati >= 0 ? materials()[mati].endurance_mpa : 0.0;

    ImGui::SeparatorText("Cyclic loading");
    static const char* kLT[4] = {"Fully reversed  (R = -1)", "Zero to max  (R = 0)", "Repeated  (custom R)", "Custom min/max factor"};
    int lt = (s.fatigueLoadType < 0 || s.fatigueLoadType > 3) ? 0 : s.fatigueLoadType;
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##flt", kLT[lt])) {
        for (int i = 0; i < 4; ++i) if (ImGui::Selectable(kLT[i], lt == i)) { s.fatigueLoadType = i; persist(st, s); }
        ImGui::EndCombo();
    }
    if (s.fatigueLoadType == 2) {
        ImGui::TextUnformatted("Load ratio R = min / max");
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::SliderFloat("##fR", &s.fatigueRatio, -1.0f, 0.95f, "%.2f");
        if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    } else if (s.fatigueLoadType == 3) {
        ImGui::TextUnformatted("Min / max load factor (x applied)");
        float mm[2] = {s.fatigueLmin, s.fatigueLmax};
        ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::DragFloat2("##fmm", mm, 0.01f, 0.0f, 0.0f, "%.2f x");
        if (ImGui::IsItemDeactivatedAfterEdit()) {
            s.fatigueLmin = std::min(mm[0], mm[1]); s.fatigueLmax = std::max(mm[0], mm[1]); persist(st, s);
        }
    }
    float lmin, lmax; fatigueLoadFactors(s, lmin, lmax);
    const float amp = (lmax - lmin) * 0.5f, mean = (lmax + lmin) * 0.5f;
    const float R = (std::fabs(lmax) > 1e-6f) ? (lmin / lmax) : -1.0f;
    ImGui::TextColored(c5::txtDim(), "Amplitude %.2fx | mean %.2fx | R %.2f  (x the applied-load stress)", amp, mean, R);
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float w = ImGui::GetContentRegionAvail().x, h = 46.0f * S;
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImGui::Dummy(ImVec2(w, h));
        const float midY = p0.y + h * 0.5f, x0 = p0.x + 24*S, x1 = p0.x + w - 6*S;
        const float span = std::max(1.0f, std::max(std::fabs(lmax), std::fabs(lmin)));
        auto Y = [&](float v) { return midY - (v / span) * (h * 0.42f); };
        ImVec4 inkV = c5::txt();
        auto ink = [&](float a) { return ImGui::ColorConvertFloat4ToU32(ImVec4(inkV.x, inkV.y, inkV.z, inkV.w * a)); };
        dl->AddLine(ImVec2(x0, Y(0)), ImVec2(x1, Y(0)), ink(0.45f), 1.0f);
        dl->AddLine(ImVec2(x0, Y(mean)), ImVec2(x1, Y(mean)), ImGui::ColorConvertFloat4ToU32(c5::warnCol()), 1.0f);
        ImU32 col = theme::panelview::sparkline();
        ImVec2 prev(x0, Y(mean));
        for (int i = 1; i <= 60; ++i) {
            float t = (float)i / 60.0f;
            float v = mean + amp * std::sin(t * 6.2831853f * 1.5f);
            ImVec2 cur(x0 + t * (x1 - x0), Y(v));
            dl->AddLine(prev, cur, col, 2.0f); prev = cur;
        }
        dl->AddText(ImVec2(p0.x, Y(lmax) - 6*S), ink(1.0f), "max");
        dl->AddText(ImVec2(p0.x, Y(lmin) - 6*S), ink(1.0f), "min");
    }

    ImGui::SeparatorText("Strength");
    if (matSut > 0)
        ImGui::TextColored(c5::txtDim(), "Material: Sut %.0f %s | Se' %.0f %s",
                           matSut * u.stressFromMpa, u.stress, matSe * u.stressFromMpa, u.stress);
    else {
        ImGui::PushStyleColor(ImGuiCol_Text, c5::warnCol());
        ImGui::TextWrapped("This material has no ultimate strength - set Sut (and Se') below, or pick a material that carries fatigue data.");
        ImGui::PopStyleColor();
    }
    auto strengthField = [&](const char* label, float& field, double matDefault) {
        float disp = (field > 0 ? field : (float)matDefault) * (float)u.stressFromMpa;
        ImGui::TextUnformatted(label);
        ImGui::SetNextItemWidth(-FLT_MIN);
        std::string id = std::string("##") + label;
        char sfmt[24]; std::snprintf(sfmt, sizeof(sfmt), "%%.4g %s", u.stress);
        ImGui::InputFloat(id.c_str(), &disp, 0, 0, sfmt);
        if (ImGui::IsItemDeactivatedAfterEdit()) { field = disp / (float)u.stressFromMpa; persist(st, s); }
    };
    strengthField((std::string("Ultimate Sut (") + u.stress + ")").c_str(), s.fatigueSut, matSut);
    strengthField((std::string("Endurance Se' (") + u.stress + ")").c_str(), s.fatigueSe, matSe);

    ImGui::SeparatorText("Endurance correction (Marin)");
    auto combo = [&](const char* lbl, const char* id, const char** items, int n, int& val) {
        int v = (val < 0 || val >= n) ? 0 : val;
        ImGui::TextUnformatted(lbl);
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::BeginCombo(id, items[v])) {
            for (int i = 0; i < n; ++i) if (ImGui::Selectable(items[i], v == i)) { val = i; persist(st, s); }
            ImGui::EndCombo();
        }
    };
    static const char* kSurf[4] = {"Ground", "Machined / cold-drawn", "Hot-rolled", "As-forged"};
    static const char* kCase[3] = {"Bending", "Axial", "Torsion"};
    combo("Surface finish", "##fsurf", kSurf, 4, s.fatigueSurface);
    combo("Loading type", "##fcase", kCase, 3, s.fatigueLoadCase);
    ImGui::TextUnformatted("Section size"); ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::DragFloat("##fsize", &s.fatigueSizeMm, 0.5f, 1.0f, 250.0f, "%.1f mm");
    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    ImGui::TextUnformatted("Temperature"); ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::DragFloat("##ftemp", &s.fatigueTempC, 1.0f, -50.0f, 500.0f, "%.0f C");
    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    ImGui::TextUnformatted("Design reliability (%)"); ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::SliderFloat("##frel", &s.fatigueReliability, 50.0f, 99.99f, "%.2f");
    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);

    const double Sut = s.fatigueSut > 0 ? s.fatigueSut : matSut;
    if (Sut > 0) {
        const double Seb = seBase(s, matSe, Sut);
        const double ka = marin_ka(Sut, s.fatigueSurface), kb = marin_kb(s.fatigueSizeMm, s.fatigueLoadCase),
                     kc = marin_kc(s.fatigueLoadCase), kd = marin_kd(s.fatigueTempC);
        const double SeC = ka * kb * kc * kd * Seb;
        ImGui::Spacing();
        ImGui::TextColored(c5::txtDim(), "ka %.2f  kb %.2f  kc %.2f  kd %.2f", ka, kb, kc, kd);
        ImGui::Text("Corrected Se = %.0f %s", SeC * u.stressFromMpa, u.stress);
    }
}

// Per-theory FoS/life table — data-equivalent replacement for host's Haigh + S-N charts (see
// sim_manager.h header note: a visual, not data, simplification).
void SimManager::drawFatigueResults(app::AppState& st, Study& s) {
    if (s.resultSummary.empty()) return;
    const auto& sm = s.resultSummary;
    const double SeC = sm.value("se_corrected_mpa", 0.0), Sut = sm.value("sut_mpa", 0.0);
    const double SeD = reliability_ke(s.fatigueReliability) * SeC;
    UnitSys u = unitSys(st);
    ImGui::SeparatorText("Fatigue theories");
    ImGui::TextColored(c5::txtDim(), "Design Se (reliability-adjusted): %.0f %s", SeD * u.stressFromMpa, u.stress);
    if (ImGui::BeginTable("##fattab", 4, ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_BordersInnerH)) {
        ImGui::TableSetupColumn("Theory"); ImGui::TableSetupColumn("sa_eq"); ImGui::TableSetupColumn("FoS"); ImGui::TableSetupColumn("Life (cycles)");
        ImGui::TableHeadersRow();
        for (int m = 0; m < 5; ++m) {
            double saeq = 0.0;
            if (sm.contains("theories") && sm["theories"].contains(kTheoryKeys[m]))
                saeq = sm["theories"][kTheoryKeys[m]].value("sa_eq_mpa", 0.0);
            const double fos = (saeq > 1e-9) ? SeD / saeq : 15.0;
            const double life = sn_life(saeq, SeD, Sut);
            ImGui::TableNextRow();
            ImGui::TableNextColumn(); ImGui::TextUnformatted(kTheoryNames[m]);
            ImGui::TableNextColumn(); ImGui::Text("%.1f %s", saeq * u.stressFromMpa, u.stress);
            ImGui::TableNextColumn();
            if (fos < 1.0) ImGui::TextColored(c5::errCol(), "%.2f", fos); else ImGui::Text("%.2f", fos);
            ImGui::TableNextColumn();
            if (life >= 1e6) ImGui::TextUnformatted("infinite (>1e6)"); else ImGui::Text("%.3g", life);
        }
        ImGui::EndTable();
    }
}

void SimManager::drawFlowForm(app::AppState& st, Study& s) {
    ImGui::TextUnformatted("Flow direction (base axis)");
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##dir", kDirLabels[(s.flowDir < 0 || s.flowDir > 5) ? 0 : s.flowDir])) {
        for (int i = 0; i < 6; ++i) if (ImGui::Selectable(kDirLabels[i], s.flowDir == i)) { s.flowDir = i; persist(st, s); }
        ImGui::EndCombo();
    }
    ImGui::TextUnformatted("Angle of attack"); ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::SliderFloat("##aoa", &s.flowAoA, -30.0f, 30.0f, "%.1f deg");
    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    ImGui::TextUnformatted("Sideslip"); ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::SliderFloat("##beta", &s.flowSideslip, -30.0f, 30.0f, "%.1f deg");
    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    ImGui::Spacing();
    ImGui::TextUnformatted("Speed"); ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##speed", &s.flowSpeed, 0, 0, "%.2f m/s");
    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);

    ImGui::TextUnformatted("Fluid");
    int fi = (s.flowFluid < 0 || s.flowFluid >= kNFluids) ? 0 : s.flowFluid;
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##fluid", kFluids[fi].label)) {
        for (int i = 0; i < kNFluids; ++i)
            if (ImGui::Selectable(kFluids[i].label, fi == i)) {
                s.flowFluid = i;
                if (i != kNFluids - 1) { s.flowDensity = kFluids[i].density; s.flowViscosity = kFluids[i].visc; }
                persist(st, s);
            }
        ImGui::EndCombo();
    }
    if (s.flowFluid == kNFluids - 1) {
        ImGui::TextUnformatted("Density (kg/m3)"); ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputFloat("##rho", &s.flowDensity, 0, 0, "%.3f");
        if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
        ImGui::TextUnformatted("Kinematic viscosity (m2/s)"); ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputFloat("##nu", &s.flowViscosity, 0, 0, "%.2e");
        if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    }

    ImGui::TextUnformatted("Surface roughness");
    int ri = (s.flowRoughness < 0 || s.flowRoughness >= kNRoughness) ? 0 : s.flowRoughness;
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##rough", kRoughness[ri].label)) {
        for (int i = 0; i < kNRoughness; ++i) if (ImGui::Selectable(kRoughness[i].label, ri == i)) { s.flowRoughness = i; persist(st, s); }
        ImGui::EndCombo();
    }
    if (s.flowRoughness == kNRoughness - 1) {
        ImGui::TextUnformatted("Roughness Ks (um)"); ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputFloat("##ks", &s.flowRoughnessUm, 0, 0, "%.1f");
        if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    }

    ImGui::TextUnformatted("Reference area (mm2, 0 = auto frontal)"); ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##refarea", &s.flowRefAreaMm2, 0, 0, "%.0f");
    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);

    ImGui::Spacing();
    ImGui::TextUnformatted("Resolution"); ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##preset", kPresetLabels[(s.flowPreset < 0 || s.flowPreset > 3) ? 0 : s.flowPreset])) {
        for (int i = 0; i < 4; ++i) if (ImGui::Selectable(kPresetLabels[i], s.flowPreset == i)) { s.flowPreset = i; persist(st, s); }
        ImGui::EndCombo();
    }

    const bool thermalStudy = (s.type == StudyType::Thermal);
    bool ofAvail = false; std::string ofVer;
    {
        nlohmann::json caps = st.simClient.caps();
        if (caps.is_object()) { ofAvail = caps.value("openfoam", false); ofVer = caps.value("openfoam_version", std::string()); }
    }
    if (!thermalStudy) {
        ImGui::Spacing();
        ImGui::TextUnformatted("Solver");
        if (!ofAvail && s.flowSolver == 1) { s.flowSolver = 0; persist(st, s); }
        static const char* kSolverLabels[2] = {"LBM (fast)", "OpenFOAM RANS (accurate)"};
        int svi = (s.flowSolver == 1 && ofAvail) ? 1 : 0;
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::BeginCombo("##solver", kSolverLabels[svi])) {
            if (ImGui::Selectable(kSolverLabels[0], svi == 0)) { s.flowSolver = 0; persist(st, s); }
            if (ofAvail) { if (ImGui::Selectable(kSolverLabels[1], svi == 1)) { s.flowSolver = 1; persist(st, s); } }
            else { ImGui::BeginDisabled(); ImGui::Selectable("OpenFOAM RANS (not provisioned)", false); ImGui::EndDisabled(); }
            ImGui::EndCombo();
        }
    } else {
        s.flowSolver = 1; s.flowThermal = true;
        if (!ofAvail) {
            ImGui::PushStyleColor(ImGuiCol_Text, c5::warnCol());
            ImGui::TextWrapped("Thermal studies need OpenFOAM, which isn't detected. Provision it with "
                               "`python -m tools.provision_openfoam --install`.");
            ImGui::PopStyleColor();
        }
    }

    bool showThermal = thermalStudy ? ofAvail : (s.flowSolver == 1 && ofAvail);
    if (!thermalStudy && s.flowSolver == 1 && ofAvail) {
        ImGui::Spacing();
        if (ImGui::Checkbox("Heat transfer (thermal)", &s.flowThermal)) persist(st, s);
    }
    if (showThermal && (thermalStudy || s.flowThermal)) {
        ImGui::Indent(10*theme::S());
        ImGui::TextUnformatted("Thermal model");
        static const char* kThermTypes[2] = {"Fluid (surface heat)", "Conjugate (solid + fluid)"};
        int tt = (s.flowThermalType == 1) ? 1 : 0;
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::BeginCombo("##thermtype", kThermTypes[tt])) {
            for (int i = 0; i < 2; ++i) if (ImGui::Selectable(kThermTypes[i], tt == i)) { s.flowThermalType = i; persist(st, s); }
            ImGui::EndCombo();
        }
        ImGui::TextUnformatted("Inlet/ambient temp (degC)"); ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputFloat("##inletT", &s.flowInletTempC, 0, 0, "%.1f");
        if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
        if (s.flowThermalType == 1) {
            ImGui::TextUnformatted("Solid conductivity (W/mK)"); ImGui::SetNextItemWidth(-FLT_MIN);
            ImGui::InputFloat("##kappa", &s.flowSolidKappa, 0, 0, "%.1f");
            if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
            ImGui::TextUnformatted("Dissipated power (W)"); ImGui::SetNextItemWidth(-FLT_MIN);
            ImGui::InputFloat("##chtP", &s.flowPowerW, 0, 0, "%.2f");
            if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
        } else {
            ImGui::TextUnformatted("Wall condition");
            static const char* kWallModes[3] = {"Fixed temperature", "Heat flux (W/m2)", "Total power (W)"};
            int wm = (s.flowWallMode < 0 || s.flowWallMode > 2) ? 2 : s.flowWallMode;
            ImGui::SetNextItemWidth(-FLT_MIN);
            if (ImGui::BeginCombo("##wallmode", kWallModes[wm])) {
                for (int i = 0; i < 3; ++i) if (ImGui::Selectable(kWallModes[i], wm == i)) { s.flowWallMode = i; persist(st, s); }
                ImGui::EndCombo();
            }
            if (wm == 0) {
                ImGui::TextUnformatted("Wall temperature (degC)"); ImGui::SetNextItemWidth(-FLT_MIN);
                ImGui::InputFloat("##wallT", &s.flowWallTempC, 0, 0, "%.1f");
                if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
            } else if (wm == 1) {
                ImGui::TextUnformatted("Heat flux (W/m2)"); ImGui::SetNextItemWidth(-FLT_MIN);
                ImGui::InputFloat("##wallQ", &s.flowWallFluxWm2, 0, 0, "%.1f");
                if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
            } else {
                ImGui::TextUnformatted("Total power (W)"); ImGui::SetNextItemWidth(-FLT_MIN);
                ImGui::InputFloat("##wallP", &s.flowWallPowerW, 0, 0, "%.2f");
                if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
            }
        }
        ImGui::Unindent(10*theme::S());
    }

    ImGui::Spacing();
    float dir[3]; flowIncomingDir(s, dir);
    float density, visc; flowFluidProps(s, density, visc);
    ImGui::TextColored(c5::txtDim(), "Incoming flow (base axis): (%.2f, %.2f, %.2f)", dir[0], dir[1], dir[2]);
    ImGui::TextColored(c5::txtDim(), "rho %.3g kg/m3  |  nu %.2e m2/s", density, visc);
}

void SimManager::drawResultControls(app::AppState& st, Study& s) {
    if (!s.hasResult) { ImGui::TextWrapped("No results yet. Set up the study and press Run."); return; }
    const UnitSys u = unitSys(st);
    char b[80];
    ImGui::SeparatorText("Results");
    ImGui::Spacing();

    // T-SIM3: advisor margin verdict (sim_advisor.postcheck vs margin_policy.json). FAIL is
    // loud (error colour, H2, bordered row) but never modal — the results stay fully usable.
    if (!s.advisorVerdict.empty()) {
        const ImVec4 vcol = (s.advisorVerdict == "PASS") ? c5::accent()
                          : (s.advisorVerdict == "WARN") ? c5::warnCol() : c5::errCol();
        ImGui::PushStyleColor(ImGuiCol_ChildBg, c5::glassBg());
        ImGui::PushStyleColor(ImGuiCol_Border, vcol);
        ImGui::BeginChild("##sim_verdict", ImVec2(-FLT_MIN, 0),
                          ImGuiChildFlags_Borders | ImGuiChildFlags_AutoResizeY |
                          ImGuiChildFlags_AlwaysUseWindowPadding);
        ImGui::PushFont(theme::font(theme::Font::H2));
        ImGui::TextColored(vcol, "Advisor verdict: %s", s.advisorVerdict.c_str());
        ImGui::PopFont();
        ImGui::SameLine();
        ImGui::TextColored(c5::txtDim(), "(%s, SF %.1f)", s.advisorMatClass.c_str(), s.advisorSf);
        simTip("Margin verdict from engine_oracle/sim/margin_policy.json via the sim advisor "
               "(max stress x SF vs yield/ultimate).");
        // Narration: the advisor's explanation lines (margin math, beam anchor, divergence).
        if (!s.advisorNarration.empty()) {
            std::istringstream nls(s.advisorNarration);
            std::string line;
            while (std::getline(nls, line)) {
                if (line.empty()) continue;
                // The narration's first line restates the verdict the H2 headline already
                // shows — skip it to keep the card tight.
                if (line.rfind("Advisor verdict", 0) == 0) continue;
                ImGui::PushStyleColor(ImGuiCol_Text, c5::txtDim());
                ImGui::TextWrapped("%s", asciiFold(line).c_str());
                ImGui::PopStyleColor();
            }
        }
        ImGui::EndChild();
        ImGui::PopStyleColor(2);
        ImGui::Spacing();
    }

    if (s.type == StudyType::Static || s.type == StudyType::Fatigue || isFlowFamily(s.type)) {
        const ResultField* arr = (s.type == StudyType::Static) ? kStaticFields
                                : (s.type == StudyType::Fatigue) ? kFatigueFields : kFlowFields;
        const int n = (s.type == StudyType::Static) ? kNStaticFields
                    : (s.type == StudyType::Fatigue) ? kNFatigueFields : kNFlowFields;
        ImGui::TextUnformatted("Plot");
        auto avail = [&](int i) { return !isFlowFamily(s.type) || flowFieldPresent(s.flowFields, arr[i].chunk); };
        int curIdx = 0;
        for (int i = 0; i < n; ++i) if (activeScalarField_ == arr[i].chunk) curIdx = i;
        if (!avail(curIdx)) for (int i = 0; i < n; ++i) if (avail(i)) { curIdx = i; break; }
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::BeginCombo("##field", arr[curIdx].label)) {
            for (int i = 0; i < n; ++i) {
                if (!avail(i)) continue;
                if (ImGui::Selectable(arr[i].label, i == curIdx)) { activeScalarField_ = arr[i].chunk; applyResultField(st, s); }
            }
            ImGui::EndCombo();
        }
        ImGui::Spacing();
    }

    auto row = [&](const char* k, const std::string& v, bool warn = false) {
        ImGui::TableNextRow();
        ImGui::TableNextColumn(); ImGui::TextUnformatted(k);
        ImGui::TableNextColumn();
        if (warn) ImGui::PushStyleColor(ImGuiCol_Text, c5::errCol());
        ImGui::TextUnformatted(v.c_str());
        if (warn) ImGui::PopStyleColor();
    };
    auto stressV = [&](const char* key) {
        double mpa = s.resultSummary.value(key, 0.0);
        std::snprintf(b, sizeof(b), "%.4g %s", mpa * u.stressFromMpa, u.stress);
        return std::string(b);
    };
    if (s.type != StudyType::Fatigue && ImGui::BeginTable("##restab", 2, ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_BordersInnerH)) {
        if (s.type == StudyType::Thermal) {
            // T-SIM1: steady-state conduction results row (max/min temperature).
            const auto& sm = s.resultSummary;
            std::snprintf(b, sizeof(b), "%.1f degC", sm.value("max_temp_c", s.maxTempC));
            row("Max temperature", b);
            std::snprintf(b, sizeof(b), "%.1f degC", sm.value("min_temp_c", s.minTempC));
            row("Min temperature", b);
            std::snprintf(b, sizeof(b), "%.1f W/m.K", s.thermalKwmk);
            row("Conductivity (k)", b);
        } else if (s.type == StudyType::Modal) {
            for (size_t i = 0; i < s.freqs.size(); ++i) {
                std::snprintf(b, sizeof(b), "%.2f Hz", s.freqs[i]);
                row(("Mode " + std::to_string(i + 1)).c_str(), b);
            }
            if (s.freqs.empty()) row("Frequencies", "(none)");
        } else if (isFlowFamily(s.type) && flowFieldPresent(s.flowFields, "temperature")) {
            std::snprintf(b, sizeof(b), "%.1f degC", s.flowInletTempC); row("Inlet / ambient", b);
            const char* lbl = (s.flowThermalType == 1) ? "Part temp (min)" : "Surface temp (min)";
            std::snprintf(b, sizeof(b), "%.1f degC", s.cpMin); row(lbl, b);
            std::snprintf(b, sizeof(b), "%.1f degC", s.cpMax);
            row((s.flowThermalType == 1) ? "Part temp (max)" : "Surface temp (max)", b);
            std::snprintf(b, sizeof(b), "%.0f", s.reynolds); row("Reynolds number", b);
        } else if (isFlowFamily(s.type)) {
            std::snprintf(b, sizeof(b), "%.4g N", s.dragN); row("Drag", b);
            std::snprintf(b, sizeof(b), "%.4g N", s.liftN); row("Lift", b);
            if (std::fabs(s.sideN) > 1e-9) { std::snprintf(b, sizeof(b), "%.4g N", s.sideN); row("Side force", b); }
            std::snprintf(b, sizeof(b), "%.4f", s.cd); row("Drag coefficient Cd", b);
            std::snprintf(b, sizeof(b), "%.4f", s.cl); row("Lift coefficient Cl", b);
            std::snprintf(b, sizeof(b), "%.2f", s.ld); row("Lift / drag (L/D)", b);
            std::snprintf(b, sizeof(b), "%.0f", s.reynolds); row("Reynolds number", b);
            std::snprintf(b, sizeof(b), "%.0f mm^2", s.refAreaMm2 > 0 ? s.refAreaMm2 : s.frontalMm2);
            row(s.flowRefAreaMm2 > 0 ? "Reference area" : "Frontal area", b);
        } else {
            const auto& sm = s.resultSummary;
            row("Max von Mises", stressV("max_von_mises_mpa"));
            if (sm.contains("max_tresca_mpa")) row("Max shear stress (Tresca)", stressV("max_tresca_mpa"));
            else if (sm.contains("max_shear_mpa")) row("Max shear", stressV("max_shear_mpa"));
            if (sm.contains("max_principal_mpa")) row("Max principal (P1)", stressV("max_principal_mpa"));
            if (sm.contains("min_principal_mpa")) row("Min principal (P3)", stressV("min_principal_mpa"));
            double mm = sm.value("max_disp_mm", 0.0);
            std::snprintf(b, sizeof(b), "%.4g %s", mm * u.lenFromMm, u.len);
            row("Max displacement", b);
            const char* fosKey = (s.criterion == FailureCriterion::Tresca) ? "min_fos_tresca"
                               : (s.criterion == FailureCriterion::MaxNormal) ? "min_fos_rankine" : "min_fos";
            const char* critName = (s.criterion == FailureCriterion::Tresca) ? "Tresca"
                                 : (s.criterion == FailureCriterion::MaxNormal) ? "max normal" : "von Mises";
            double fos = sm.value(fosKey, sm.value("min_fos", 0.0));
            std::snprintf(b, sizeof(b), "%.2f", fos);
            row((std::string("Min FoS (") + critName + ")").c_str(), b, fos > 0.0 && fos < 1.0);
        }
        ImGui::EndTable();
    }
    if (s.type == StudyType::Static || s.type == StudyType::Modal) {
        if (s.resultSummary.value("min_fos", 99.0) < 1.0) {
            ImGui::PushStyleColor(ImGuiCol_Text, c5::errCol());
            ImGui::TextWrapped("Factor of safety below 1 - the material yields under this load.");
            ImGui::PopStyleColor();
        }
    }
    if (s.type == StudyType::Fatigue) drawFatigueResults(st, s);
    if (!s.banner.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, c5::warnCol());
        ImGui::TextWrapped("Qualitative result: %s", s.banner.c_str());
        ImGui::PopStyleColor();
    }
    if (isFlowFamily(s.type) && !s.dragTrace.empty())
        ImGui::PlotLines("##drag", s.dragTrace.data(), (int)s.dragTrace.size(), 0, "Drag trace",
                         FLT_MAX, FLT_MAX, ImVec2(-FLT_MIN, 50*theme::S()));

    if (s.nNodes > 0 || s.nElems > 0 || s.nDof > 0 || !s.solverNotes.empty()) {
        ImGui::Spacing();
        if (ImGui::TreeNodeEx("Mesh & solver", ImGuiTreeNodeFlags_DefaultOpen)) {
            if (s.nNodes > 0) ImGui::Text("Nodes: %d", s.nNodes);
            if (s.nElems > 0) ImGui::Text("Elements: %d", s.nElems);
            if (s.nDof > 0) ImGui::Text("Equations (DOF): %d", s.nDof);
            for (const auto& n : s.solverNotes) ImGui::BulletText("%s", n.c_str());
            ImGui::TreePop();
        }
    }

    ImGui::Spacing(); ImGui::Separator();
    // T-SIM2: "Show contour" per-study toggle — persisted on the Study (s.contourOn) so it reflects
    // real state across frames AND is per-study (was a per-frame `true` local that never tracked the
    // toggle). Off hides BOTH the colormap pass and the legend bar so the part reads plainly.
    if (ImGui::Checkbox("Show contour", &s.contourOn)) {
        viewportSetColormapMode(s.contourOn);
        viewportState().legendOn = s.contourOn;
    }
    simTip("Colour-map the selected result field over the part, with a min/max legend. "
           "Turn off to see the plain shaded model.");
    if (!isFlowFamily(s.type)) {
        float curScale = viewportDeformScale();
        ImGui::Spacing();
        ImGui::Text("Deformation scale");
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::SliderFloat("##deform", &curScale, 0.0f, 5.0f, "%.3gx")) viewportSetDeformScale(curScale);
        if (ImGui::SmallButton("1x (true)")) viewportSetDeformScale(1.0f);
        ImGui::SameLine();
        if (ImGui::SmallButton("Off")) viewportSetDeformScale(0.0f);
    }
}

// T-SIM1: steady-state conduction inputs. Hot faces come from the Fixtures node (each held at
// the hot temperature); the cold-side boundary is set here + applied to the Load faces as a
// convection film, a heat flux, or a second fixed temperature (mirrors force/pressure choice).
void SimManager::drawThermalForm(app::AppState& st, Study& s) {
    ImGui::SeparatorText("Steady-state heat conduction");
    ImGui::TextDisabled("Fixtures = fixed-temperature (hot) faces. Loads = cold-side boundary.");
    ImGui::Spacing();

    // Conductivity readout from the cited table (auto from the selected material).
    const double k = thermalConductivityFor(s.materialKey);
    if (k > 0.0) {
        s.thermalKwmk = (float)k;
        ImGui::Text("Conductivity k = %.1f W/m.K", k);
        simTip("From engine/sim/thermal_conductivity.json (cited data table).");
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, c5::warnCol());
        ImGui::TextWrapped("Selected material has no tabulated thermal conductivity — pick a "
                           "material with a k value (Material node).");
        ImGui::PopStyleColor();
    }
    ImGui::Spacing();

    ImGui::TextUnformatted("Fixed (hot) face temperature (degC)"); ImGui::SetNextItemWidth(-FLT_MIN);
    ImGui::InputFloat("##thot", &s.thermalHotTempC, 0, 0, "%.1f");
    if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);

    ImGui::Spacing();
    ImGui::TextUnformatted("Cold-side boundary (on Load faces)");
    // Three mutually-exclusive modes: convection film, heat flux, or a fixed cold temperature.
    int mode = s.thermalUseColdFace ? 2 : (s.thermalUseFlux ? 1 : 0);
    static const char* kModes[3] = {"Convection film (h, T_ambient)", "Heat flux (W/m2)",
                                    "Fixed temperature (degC)"};
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::BeginCombo("##thmode", kModes[mode])) {
        for (int i = 0; i < 3; ++i)
            if (ImGui::Selectable(kModes[i], mode == i)) {
                s.thermalUseConvection = (i == 0);
                s.thermalUseFlux = (i == 1);
                s.thermalUseColdFace = (i == 2);
                persist(st, s);
            }
        ImGui::EndCombo();
    }
    if (mode == 0) {
        ImGui::TextUnformatted("Film coefficient h (W/m2.K)"); ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputFloat("##thh", &s.thermalConvH, 0, 0, "%.1f");
        if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
        ImGui::TextUnformatted("Ambient temperature (degC)"); ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputFloat("##thamb", &s.thermalAmbC, 0, 0, "%.1f");
        if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    } else if (mode == 1) {
        ImGui::TextUnformatted("Surface heat flux (W/m2, + into part)"); ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputFloat("##thflux", &s.thermalFluxWm2, 0, 0, "%.1f");
        if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    } else {
        ImGui::TextUnformatted("Cold face temperature (degC)"); ImGui::SetNextItemWidth(-FLT_MIN);
        ImGui::InputFloat("##thcold", &s.thermalColdTempC, 0, 0, "%.1f");
        if (ImGui::IsItemDeactivatedAfterEdit()) persist(st, s);
    }
}

// T-SIM3: precheck advisory card — aicad.advisory/1 items from sim_advisor.precheck rendered
// in the panel before/alongside dispatch. Severity colours: blocker=err, action=warn,
// info/hint=dim. Dismissable (advise, never veto); a dispatch block only happens when the
// user set AI_CAD_SIM_PRECHECK_BLOCK=1, and says so explicitly.
void SimManager::drawAdvisoryCard(app::AppState& st, Study& s) {
    (void)st;
    const bool mine = (precheckStudyId_ == s.id) && !s.id.empty();
    const bool haveItems = mine && precheckItems_.is_array() && !precheckItems_.empty();
    const bool haveNote = mine && !advisorNote_.empty();
    if (!haveItems && !haveNote) return;
    const float S = theme::S();
    ImGui::Spacing();
    ImGui::PushStyleColor(ImGuiCol_ChildBg, c5::glassBg());
    ImGui::PushStyleColor(ImGuiCol_Border, c5::glassBorder());
    ImGui::BeginChild("##sim_advisory", ImVec2(-FLT_MIN, 0),
                      ImGuiChildFlags_Borders | ImGuiChildFlags_AutoResizeY |
                      ImGuiChildFlags_AlwaysUseWindowPadding);
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextColored(c5::accent(), "Advisor precheck");
    ImGui::PopFont();
    ImGui::SameLine(ImGui::GetContentRegionMax().x - 18.f * S);
    if (ImGui::SmallButton("x##advdismiss")) {
        precheckItems_ = nlohmann::json::array();
        precheckStudyId_.clear();
        precheckBlocked_ = false;
        advisorNote_.clear();
    }
    simTip("Dismiss this advisory. The advisor advises; it never vetoes your run.");
    if (haveNote) ImGui::TextColored(c5::txtDim(), "%s", advisorNote_.c_str());
    if (haveItems) {
        for (int i = 0; i < (int)precheckItems_.size(); ++i) {
            const auto& it = precheckItems_[(size_t)i];
            const std::string sev = it.value("severity", std::string("info"));
            const ImVec4 col = (sev == "blocker") ? c5::errCol()
                             : (sev == "action")  ? c5::warnCol() : c5::txtDim();
            ImGui::PushID(i);
            ImGui::TextColored(col, "[%s]", sev.c_str());
            ImGui::SameLine();
            ImGui::TextWrapped("%s", asciiFold(it.value("finding", std::string())).c_str());
            if (it.contains("candidate_fixes") && it["candidate_fixes"].is_array() &&
                !it["candidate_fixes"].empty()) {
                const auto& fx = it["candidate_fixes"][0];
                const std::string hint = fx.contains("params") && fx["params"].is_object()
                                             ? fx["params"].value("hint", std::string())
                                             : std::string();
                if (!hint.empty()) {
                    ImGui::Indent(12.f * S);
                    ImGui::PushStyleColor(ImGuiCol_Text, c5::txtDim());
                    ImGui::TextWrapped("Fix: %s", asciiFold(hint).c_str());
                    ImGui::PopStyleColor();
                    ImGui::Unindent(12.f * S);
                }
            }
            ImGui::PopID();
        }
    }
    if (precheckBlocked_) {
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text, c5::errCol());
        ImGui::TextWrapped("Run blocked by precheck (AI_CAD_SIM_PRECHECK_BLOCK=1). "
                           "Resolve the blockers above, or unset the variable to run anyway.");
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();
    ImGui::PopStyleColor(2);
}

void SimManager::drawForm(app::AppState& st, Study& s) {
    const float S = theme::S();
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted(s.name.c_str());
    ImGui::PopFont();
    ImGui::SameLine();
    ImGui::TextDisabled("(%s)", studyTypeLabel(s.type));

    {
        static const char* kUnitItems[3] = {"Units: follow document", "Units: SI (MPa - N - mm)", "Units: US (psi - lbf - in)"};
        int ui_ = (unitsMode_ < 0 || unitsMode_ > 2) ? 0 : unitsMode_;
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::BeginCombo("##simunits", kUnitItems[ui_])) {
            for (int i = 0; i < 3; ++i)
                if (ImGui::Selectable(kUnitItems[i], ui_ == i)) {
                    unitsMode_ = i;
                    if (s.hasResult && resultShown_) applyResultField(st, s);  // re-label the legend
                }
            ImGui::EndCombo();
        }
    }
    ImGui::Spacing();

    switch (node_) {
        case SimNode::Results:
            drawResultControls(st, s);
            break;
        case SimNode::Material:
            // Thermal (conduction) picks a material for its conductivity, like the FEA studies.
            if (s.type == StudyType::Thermal) drawMaterialCombo(st, s);
            else if (!isFlowFamily(s.type)) drawMaterialCombo(st, s);
            else drawFlowForm(st, s);
            break;
        case SimNode::Fixtures:
            drawFixturesForm(st, s);
            break;
        case SimNode::Load:
            drawLoadsForm(st, s);
            break;
        case SimNode::Mesh:
            drawMeshForm(st, s);
            break;
        case SimNode::Study:
        default:
            if (s.type == StudyType::Thermal) { drawMaterialCombo(st, s); ImGui::Separator(); drawThermalForm(st, s); }
            else if (isFlowFamily(s.type)) drawFlowForm(st, s);
            else if (s.type == StudyType::Fatigue) { drawMaterialCombo(st, s); ImGui::Separator(); drawFatigueForm(st, s); }
            else drawMaterialCombo(st, s);
            break;
    }

    // T-SIM3: the material dialog + the precheck advisory card live at the form level so
    // they are reachable from every node of the selected study.
    drawMaterialDialog(st, s);
    drawAdvisoryCard(st, s);

    {
        ImGui::Spacing(); ImGui::Separator(); ImGui::Spacing();
        bool busy = st.simClient.busy();
        std::string blocker;
        bool ready = studyReady(s, blocker);
        const bool canRun = ready && !busy;
        if (!canRun) ImGui::BeginDisabled();
        if (ImGui::Button(busy ? "Running..." : "Run", ImVec2(-FLT_MIN, 30*S))) run(st, s);
        if (!canRun) ImGui::EndDisabled();
        if (!busy && !ready) simTip(blocker.c_str());
        if (busy) { ImGui::SameLine(); if (ImGui::SmallButton("Cancel")) st.simClient.cancel(); }
        if (!s.dragTrace.empty())
            ImGui::PlotLines("##drag", s.dragTrace.data(), (int)s.dragTrace.size(), 0, "Drag trace",
                             FLT_MAX, FLT_MAX, ImVec2(-FLT_MIN, 50*S));
        if (s.hasResult && node_ != SimNode::Results) {
            ImGui::Spacing();
            ImGui::TextWrapped("%s", s.summary.c_str());
        }
    }
}

SimManager& simManager() { static SimManager sm; return sm; }

}  // namespace ui
