#pragma once
struct AppState;
namespace ui {
void pollCurrentRun(AppState& st);
int currentRunLiveCount();
void drawCurrentRunView(AppState& st);
}  // namespace ui
