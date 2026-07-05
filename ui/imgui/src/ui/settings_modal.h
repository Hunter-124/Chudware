// Settings + About modals and persisted app settings (config.json via platform/paths).
#pragma once

#include <set>
#include <string>
#include <vector>

namespace app { struct AppState; }

namespace ui {

enum class Units { MMGS = 0, IPS = 1 };

struct AppSettings {
    int provider = 4;
    char model[512] = "gemini-2.5-flash";
    char apiKey[256] = "";
    char geminiKey[256] = "";
    char anthropicKey[256] = "";
    char openaiKey[256] = "";
    char cloudKey[256] = "";
    char reasoningEffort[16] = "high";
    char baseUrl[256] = "http://localhost:1234/v1";
    bool vision = true;
    bool offlineMode = false;
    bool planMode = true;
    int maxIters = 0;
    int maxTokens = 0;
    int contextWindow = 0;
    std::string systemPrompt;
    std::string theme = "dark";
    Units units = Units::MMGS;
    std::string workspaceDir;
    std::vector<std::string> recentFiles;
    std::set<std::string> hiddenModels;
    std::set<std::string> disabledProviders;
    bool expVisionReview = false;
    char expVisionModel[512] = "qwen2.5-vl-7b-instruct";
    char expVisionBaseUrl[256] = "http://localhost:1234/v1";
    char expVisionKey[256] = "";
    bool expCadSequenceMode = false;
};

struct SettingsUiState {
    bool openSettings = false;
    bool showAbout = false;
    int  openSettingsTab = -1;
    bool showProfilerWindow = false;
    bool profilerRunning = false;
    std::string pendingPromptRole;
    bool wantSettings = false;
    bool openComposerModelDropdown = false; // T-G22: shot trigger — open composer model popup
    bool openPermissionsPopup = false;      // T-G22: shot trigger — open Permissions popup
};

struct SettingsEntry {
    const char* label;
    const char* tab;
    int         tabIndex;
};

extern const SettingsEntry kSettingsEntries[];
extern const int           kSettingsEntryCount;

AppSettings&    appSettings();
SettingsUiState& settingsUi();
void loadAppSettings();
void saveAppSettings(const AppSettings& s);

void drawSettingsModal(app::AppState& st, bool busy);

}  // namespace ui
