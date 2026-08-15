#pragma once

struct AppState;

namespace ui {

void plmDrawVersionsPage(AppState& st);
bool plmVersionsAvailable();
void plmVersionsOnProjectChanged();

}  // namespace ui
