// SolidWorks-style left manager pane: Explorer / FeatureManager / PropertyManager /
// ConfigurationManager hosted as vertical icon tabs in one docked window.
#pragma once

#include <functional>
#include <string>

struct AppState;

namespace ui {

class ConfigPanel;
class PropertyManager;

void drawLeftPane(AppState& st, PropertyManager& propertyManager, ConfigPanel& configPanel,
                  const std::function<void(const std::string& featureId)>& editCallback);

}  // namespace ui
