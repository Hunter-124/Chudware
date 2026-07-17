// PLM Project Hub — CAD project graph tab (contracts/cad_graph.md).
//
// Renders the node/edge web from cad_graph_get for the active project folder.
// Own state is file-static in graph_tab.cpp; the hub only registers the page and
// asks whether the tab is available (degrades to hidden on unknown_method).
#pragma once

#include <string>

struct AppState;

namespace ui {

// False after the worker reports unknown_method / not_implemented for cad_graph_*.
// The hub rail skips the Graph button when this is false.
bool plmGraphAvailable();

// Drop caches when the active project folder changes (called from the hub shell).
void plmGraphOnProjectChanged();

// Page body for PlmPage::Graph.
void plmDrawGraphPage(AppState& st);

// Pre-fill the graph search with a doc's file stem and jump focus there — the
// FeatureManager "show this part's links" entry point.
void plmGraphFocus(const std::string& query);

}  // namespace ui
