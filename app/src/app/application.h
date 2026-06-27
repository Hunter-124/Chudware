#pragma once
#include <string>
#include "viewport/scene.h"
struct GLFWwindow;

namespace app {
struct AppState;

class Application {
public:
    bool init(GLFWwindow* window, const std::string& resourcesDir, float scale);
    void frame();          // build one UI frame (call between ImGui NewFrame and Render)
    void shutdown();
    AppState& state();
private:
    void buildDockLayout(unsigned int dockspaceId);
    bool layoutBuilt_ = false;
    viewport::Scene scene_;
};

} // namespace app
