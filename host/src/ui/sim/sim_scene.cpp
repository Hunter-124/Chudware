// sim/sim_scene.cpp — dispatcher for the viewport <-> simulation-panel scene seam (sim_scene.h).
#include "sim_scene.h"

#include <cmath>

#include "../../core/app_state.h"
#include "../sim_manager.h"

namespace ui::simscene {

namespace {

bool invert4x4(const float* m, float* inv) {
    inv[0]  =  m[5]*m[10]*m[15] - m[5]*m[11]*m[14] - m[9]*m[6]*m[15] + m[9]*m[7]*m[14] + m[13]*m[6]*m[11] - m[13]*m[7]*m[10];
    inv[4]  = -m[4]*m[10]*m[15] + m[4]*m[11]*m[14] + m[8]*m[6]*m[15] - m[8]*m[7]*m[14] - m[12]*m[6]*m[11] + m[12]*m[7]*m[10];
    inv[8]  =  m[4]*m[9]*m[15]  - m[4]*m[11]*m[13] - m[8]*m[5]*m[15] + m[8]*m[7]*m[13] + m[12]*m[5]*m[11] - m[12]*m[7]*m[9];
    inv[12] = -m[4]*m[9]*m[14]  + m[4]*m[10]*m[13] + m[8]*m[5]*m[14] - m[8]*m[6]*m[13] - m[12]*m[5]*m[10] + m[12]*m[6]*m[9];
    inv[1]  = -m[1]*m[10]*m[15] + m[1]*m[11]*m[14] + m[9]*m[2]*m[15] - m[9]*m[3]*m[14] - m[13]*m[2]*m[11] + m[13]*m[3]*m[10];
    inv[5]  =  m[0]*m[10]*m[15] - m[0]*m[11]*m[14] - m[8]*m[2]*m[15] + m[8]*m[3]*m[14] + m[12]*m[2]*m[11] - m[12]*m[3]*m[10];
    inv[9]  = -m[0]*m[9]*m[15]  + m[0]*m[11]*m[13] + m[8]*m[1]*m[15] - m[8]*m[3]*m[13] - m[12]*m[1]*m[11] + m[12]*m[3]*m[9];
    inv[13] =  m[0]*m[9]*m[14]  - m[0]*m[10]*m[13] - m[8]*m[1]*m[14] + m[8]*m[2]*m[13] + m[12]*m[1]*m[10] - m[12]*m[2]*m[9];
    inv[2]  =  m[1]*m[6]*m[15]  - m[1]*m[7]*m[14]  - m[5]*m[2]*m[15] + m[5]*m[3]*m[14] + m[13]*m[2]*m[7]  - m[13]*m[3]*m[6];
    inv[6]  = -m[0]*m[6]*m[15]  + m[0]*m[7]*m[14]  + m[4]*m[2]*m[15] - m[4]*m[3]*m[14] - m[12]*m[2]*m[7]  + m[12]*m[3]*m[6];
    inv[10] =  m[0]*m[5]*m[15]  - m[0]*m[7]*m[13]  - m[4]*m[1]*m[15] + m[4]*m[3]*m[13] + m[12]*m[1]*m[7]  - m[12]*m[3]*m[5];
    inv[14] = -m[0]*m[5]*m[14]  + m[0]*m[6]*m[13]  + m[4]*m[1]*m[14] - m[4]*m[2]*m[13] - m[12]*m[1]*m[6]  + m[12]*m[2]*m[5];
    inv[3]  = -m[1]*m[6]*m[11]  + m[1]*m[7]*m[10]  + m[5]*m[2]*m[11] - m[5]*m[3]*m[10] - m[9]*m[2]*m[7]   + m[9]*m[3]*m[6];
    inv[7]  =  m[0]*m[6]*m[11]  - m[0]*m[7]*m[10]  - m[4]*m[2]*m[11] + m[4]*m[3]*m[10] + m[8]*m[2]*m[7]   - m[8]*m[3]*m[6];
    inv[11] = -m[0]*m[5]*m[11]  + m[0]*m[7]*m[9]   + m[4]*m[1]*m[11] - m[4]*m[3]*m[9]  - m[8]*m[1]*m[7]   + m[8]*m[3]*m[5];
    inv[15] =  m[0]*m[5]*m[10]  - m[0]*m[6]*m[9]   - m[4]*m[1]*m[10] + m[4]*m[2]*m[9]  + m[8]*m[1]*m[6]   - m[8]*m[2]*m[5];
    float det = m[0]*inv[0] + m[1]*inv[4] + m[2]*inv[8] + m[3]*inv[12];
    if (std::fabs(det) < 1e-20f) return false;
    det = 1.0f / det;
    for (int i = 0; i < 16; ++i) inv[i] *= det;
    return true;
}

enum class Owner { None, Piping, Hydraulics };

Owner owner(AppState& st) {
    if (st.activeWorkspace != AppState::Workspace::Simulate || !st.simManager) return Owner::None;
    if (st.simManager->piping().isOpen()) return Owner::Piping;
    if (st.simManager->hydraulic().isOpen()) return Owner::Hydraulics;
    return Owner::None;
}

}  // namespace

void screenRay(const ViewportCtx& vp, ImVec2 mouse, float orig[3], float dir[3]) {
    orig[0] = vp.cam.eye.x; orig[1] = vp.cam.eye.y; orig[2] = vp.cam.eye.z;
    dir[0] = -vp.cam.dir.x; dir[1] = -vp.cam.dir.y; dir[2] = -vp.cam.dir.z;
    float inv[16];
    if (vp.vw <= 0 || vp.vh <= 0 || !invert4x4(vp.cam.mvp.m, inv)) return;
    const float ndcX = ((mouse.x - vp.imgMin.x) / (float)vp.vw) * 2.0f - 1.0f;
    const float ndcY = 1.0f - ((mouse.y - vp.imgMin.y) / (float)vp.vh) * 2.0f;
    auto unproj = [&](float ndcZ, float o[3]) -> bool {
        const float cl[4] = {ndcX, ndcY, ndcZ, 1.0f};
        float w[4];
        for (int r = 0; r < 4; ++r)
            w[r] = inv[0*4+r]*cl[0] + inv[1*4+r]*cl[1] + inv[2*4+r]*cl[2] + inv[3*4+r]*cl[3];
        if (std::fabs(w[3]) < 1e-12f) return false;
        o[0] = w[0] / w[3]; o[1] = w[1] / w[3]; o[2] = w[2] / w[3];
        return true;
    };
    float pn[3], pf[3];
    if (!unproj(-1.0f, pn) || !unproj(1.0f, pf)) return;
    const float d[3] = {pf[0] - pn[0], pf[1] - pn[1], pf[2] - pn[2]};
    const float len = std::sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]);
    if (len < 1e-9f) return;
    for (int k = 0; k < 3; ++k) {
        orig[k] = pn[k];
        dir[k] = d[k] / len;
    }
}

bool worldToScreen(const ViewportCtx& vp, const float p[3], ImVec2& out) {
    const float* m = vp.cam.mvp.m;  // column-major
    const float q[4] = {p[0], p[1], p[2], 1.0f};
    float c[4];
    for (int r = 0; r < 4; ++r)
        c[r] = m[0 * 4 + r] * q[0] + m[1 * 4 + r] * q[1] + m[2 * 4 + r] * q[2] + m[3 * 4 + r] * q[3];
    if (c[3] <= 1e-6f) return false;
    out.x = vp.imgMin.x + (c[0] / c[3] * 0.5f + 0.5f) * (float)vp.vw;
    out.y = vp.imgMin.y + (1.0f - (c[1] / c[3] * 0.5f + 0.5f)) * (float)vp.vh;
    return true;
}

bool active(AppState& st) {
    switch (owner(st)) {
    case Owner::Piping: return pipescene::active(st);
    case Owner::Hydraulics: return hydscene::active(st);
    default: return false;
    }
}

void collect(AppState& st, Scene& out) {
    switch (owner(st)) {
    case Owner::Piping: pipescene::collect(st, out); break;
    case Owner::Hydraulics: hydscene::collect(st, out); break;
    default: break;
    }
}

bool toolArmed(AppState& st) {
    switch (owner(st)) {
    case Owner::Piping: return pipescene::toolArmed(st);
    case Owner::Hydraulics: return hydscene::toolArmed(st);
    default: return false;
    }
}

bool handleViewport(AppState& st, const ViewportCtx& vp, ImDrawList* dl) {
    switch (owner(st)) {
    case Owner::Piping: return pipescene::handleViewport(st, vp, dl);
    case Owner::Hydraulics: return hydscene::handleViewport(st, vp, dl);
    default: return false;
    }
}

bool onPlaneChip(AppState& st, Plane plane) {
    switch (owner(st)) {
    case Owner::Piping: return pipescene::onPlaneChip(st, plane);
    case Owner::Hydraulics: return hydscene::onPlaneChip(st, plane);
    default: return false;
    }
}

}  // namespace ui::simscene
