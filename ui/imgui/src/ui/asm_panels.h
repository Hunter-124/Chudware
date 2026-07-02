// Assembly UI panels (C20b) — SolidWorks-style assembly workflow on the C20 engine.
// Ported from host/src/ui/asm_panels.h (C5).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace app { struct AppState; }

namespace ui {

using AppState = app::AppState;

enum class MateSurfKind { None, Plane, Cylinder, Other };

class AsmPanels {
public:
    bool handleCommand(AppState& st, const std::string& cmdId);
    void draw(AppState& st);
    bool routeAsmPick(const nlohmann::json& selector, const std::string& component, int pickKind);
    bool routeAsmPatternPick(const std::string& component);
    bool mateFormActive() const { return mateOpen_; }
    bool patternFormActive() const { return patternOpen_; }

    void openMateForm()     { beginMateForm(); }
    void presetMateTypeForShot(const std::string& type);
    void openInterference(AppState& st) { handleCommand(st, "asm_interference"); }
    bool insertComponent(AppState& st, const std::string& path,
                         const nlohmann::json& transform, std::string& aicmOut);
    bool addMate(AppState& st, const std::string& type, const std::string& align,
                 const nlohmann::json& refs, std::string& aicmOut);
    bool setExplode(AppState& st, bool active, float factor);
    void closeInsert()  { insertOpen_ = false; }
    void closeExplode() { explodeOpen_ = false; }
    void closeMateForm();

private:
    void beginMateForm();
    void beginPatternForm();
    void drawInsert(AppState& st);
    void drawPartsLibrary(AppState& st);
    bool insertCatalogPart(AppState& st, const std::string& family, const std::string& size,
                           double length_mm, std::string& aicmOut);
    void drawMateForm(AppState& st);
    void drawPatternForm(AppState& st);
    void drawExplode(AppState& st);
    void drawInterference(AppState& st);
    void drawBom(AppState& st);
    bool addPattern(AppState& st, std::string& aicmOut, std::string& errorOut);

    void refreshInterference(AppState& st);
    void refreshBom(AppState& st);
    const char* suggestedMate() const;
    std::string effectiveMateType() const;
    static bool typeNeedsThirdRef(const std::string& t);

    bool insertOpen_ = false, insertFocus_ = false;
    std::vector<char> insertPath_{std::vector<char>(512, 0)};

    bool partsLibOpen_ = false, partsLibFocus_ = false;
    bool partsCatalogFetched_ = false;
    nlohmann::json partsCatalog_;
    std::string partsCatalogError_;
    int partsSelFamily_ = -1;
    int partsSelSize_ = 0;
    std::vector<char> partsLenBuf_{std::vector<char>(32, 0)};
    std::vector<char> partsFilter_{std::vector<char>(64, 0)};
    std::string partsNote_;

    bool mateOpen_ = false, mateFocus_ = false;
    int  activeWell_ = 0;
    struct MatePick {
        bool has = false;
        nlohmann::json selector;
        std::string component;
        std::string label;
        MateSurfKind surf = MateSurfKind::None;
    } picks_[3];
    int  mateType_ = 0;
    std::string selectedMateType_;
    bool aligned_ = true;
    std::vector<char> dimBuf_{std::vector<char>(64, 0)};
    std::vector<char> minBuf_{std::vector<char>(64, 0)};
    std::vector<char> maxBuf_{std::vector<char>(64, 0)};
    std::vector<char> ratioBuf_{std::vector<char>(64, 0)};
    std::vector<char> pitchBuf_{std::vector<char>(64, 0)};
    std::vector<char> pitchRadBuf_{std::vector<char>(64, 0)};
    std::string mateError_;

    bool patternOpen_ = false, patternFocus_ = false;
    int  patternKind_ = 0;
    int  patternDir_  = 0;
    std::vector<char> patternCountBuf_{std::vector<char>(32, 0)};
    std::vector<char> patternSpacingBuf_{std::vector<char>(32, 0)};
    std::vector<char> patternAngleBuf_{std::vector<char>(32, 0)};
    bool patternEqual_ = true;
    std::vector<std::string> patternComps_;
    std::string patternError_;

    bool explodeOpen_ = false, explodeFocus_ = false;
    bool explodeActive_ = false;
    float explodeFactor_ = 0.0f;

    bool interferenceOpen_ = false, interferenceFocus_ = false;
    nlohmann::json interferenceResult_;
    std::string interferenceError_;
    bool interferencePending_ = false;
    int  interferenceGen_ = 0;

    bool bomOpen_ = false, bomFocus_ = false;
    struct BomRow { std::string name, source; int qty = 0; double massG = 0; double volMm3 = 0; bool haveMass = false; };
    std::vector<BomRow> bomRows_;
    std::vector<char> bomCsvPath_{std::vector<char>(512, 0)};
    std::string bomNote_;
    int bomPending_ = 0;
    int bomGen_ = 0;
};

}  // namespace ui
