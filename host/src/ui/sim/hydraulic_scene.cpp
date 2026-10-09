// Hydraulic physical layout: cached shaded glyphs, orthogonal hoses and plane placement.
#include "sim_scene.h"
#include "imgui_internal.h"
#include "../widgets/kit.h"

#include <algorithm>
#include <array>
#include <cfloat>
#include <cmath>
#include <map>
#include <string>

#include "../../core/app_state.h"
#include "../../result_ramp.h"
#include "../../ui_theme.h"
#include "../metrics.h"
#include "../sim_manager.h"
#include "../legend.h"
#include "../property_manager.h"
#include "../sketch_mode.h"
#include "../sketch3d_mode.h"
#include "sim_common.h"

namespace ui {
void HydraulicPanel::componentEnvelope3d(const nlohmann::json& component, float lo[3], float hi[3]) {
    // Keep the placement envelope beside the glyph renderer. Reserve full travel rather than
    // the current simulated extension so re-running a circuit cannot overlap default glyphs.
    const auto& spec = sim::jobj(component, "params");
    const auto type = sim::jstr(component, "type");
    std::array<float, 3> lower{-50, -35, -35}, upper{50, 35, 35};
    if (type == "cylinder_linear") {
        const float bore = std::max(8.0f, (float)sim::jnum(spec, "bore_mm", 63));
        const float rod = std::max(4.0f, (float)sim::jnum(spec, "rod_mm", bore * 0.55f));
        const float stroke = std::max(1.0f, (float)sim::jnum(spec, "stroke_mm", 300));
        const float radius = std::max(bore * 0.5f + 10, rod * 0.5f);
        lower = {-(stroke + 80) * 0.5f - 9, -radius, -radius};
        upper = {(stroke + 80) * 0.5f + stroke + 30, radius, radius};
    } else if (type == "pump" || type == "motor_hydraulic" || type == "cylinder_rotary") {
        const float scale = std::cbrt(std::max(1.0, sim::jnum(spec, "displacement_cc_rev", 16)) / 16.0);
        lower = {-50 * scale, -35 * scale, (type == "pump" ? -41 : -35) * scale};
        upper = {(type == "pump" ? 160 : 85) * scale, 35 * scale, 35 * scale};
    } else if (type == "reservoir") {
        const float edge = std::cbrt(std::max(0.1, sim::jnum(spec, "volume_l", 40)) * 1e6);
        lower = {-edge * 0.5f, -edge * 0.325f, -edge * 0.35f};
        upper = {edge * 0.5f, edge * 0.325f, edge * 0.35f + 20};
    } else if (type == "accumulator") {
        const float radius = std::cbrt(std::max(0.01, sim::jnum(spec, "volume_l", 1)) * 1e6 / 12.5663706144);
        lower = {-radius, -radius, -2 * radius};
        upper = {radius, radius, 2 * radius};
    } else if (type.find("valve") != std::string::npos) {
        lower[0] = -70; upper[0] = 70;
    }
    // Ports protrude 12 mm and have 6 mm radius.
    for (int k = 0; k < 3; ++k) { lower[k] -= 18; upper[k] += 18; lo[k] = FLT_MAX; hi[k] = -FLT_MAX; }
    const float angle = (float)sim::jnum(component, "angle") * 0.01745329252f;
    const float cs = std::cos(angle), sn = std::sin(angle);
    for (int corner = 0; corner < 8; ++corner) {
        const float x = (corner & 1) ? upper[0] : lower[0];
        const float y = (corner & 2) ? upper[1] : lower[1];
        const float v[3] = {cs*x - sn*y, sn*x + cs*y, (corner & 4) ? upper[2] : lower[2]};
        for (int k = 0; k < 3; ++k) { lo[k] = std::min(lo[k], v[k]); hi[k] = std::max(hi[k], v[k]); }
    }
}
} // namespace ui

namespace ui::hydscene {
namespace {
using sim::jarr;
using sim::jnum;
using sim::jobj;
using sim::jstr;
using V = std::array<float, 3>;
V add(V a, V b) { return {a[0]+b[0], a[1]+b[1], a[2]+b[2]}; }
V sub(V a, V b) { return {a[0]-b[0], a[1]-b[1], a[2]-b[2]}; }
V mul(V a, float s) { return {a[0]*s, a[1]*s, a[2]*s}; }
float dot(V a, V b) { return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
V cross(V a, V b) { return {a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]}; }
V unit(V a) { return mul(a, 1.0f/std::max(1e-9f, std::sqrt(dot(a,a)))); }
struct Geometry {
    std::vector<float> xyz, nrm, rgb;
    void clear() { xyz.clear(); nrm.clear(); rgb.clear(); }
    void vertex(V p, V n, const float* color) {
        xyz.insert(xyz.end(),p.begin(),p.end()); nrm.insert(nrm.end(),n.begin(),n.end());
        rgb.insert(rgb.end(), color, color+3);
    }
    void tri(V a,V b,V c,const float* color) {
        const V n=unit(cross(sub(b,a),sub(c,a)));
        vertex(a,n,color); vertex(b,n,color); vertex(c,n,color);
    }
};
void box(Geometry& g, V center, V size, const float* color) {
    V v[8];
    for (int i=0;i<8;++i) for (int k=0;k<3;++k)
        v[i][k]=center[k]+((i&(1<<k))?0.5f:-0.5f)*size[k];
    static constexpr int faces[6][4]={{0,4,6,2},{1,3,7,5},{0,1,5,4},{2,6,7,3},{0,2,3,1},{4,5,7,6}};
    for (const auto& f:faces) { g.tri(v[f[0]],v[f[1]],v[f[2]],color); g.tri(v[f[0]],v[f[2]],v[f[3]],color); }
}
void tube(Geometry& g, V a,V b,float r,const float* color) {
    if (dot(sub(b,a),sub(b,a))<1e-8f) return;
    const V axis=unit(sub(b,a));
    const V u=unit(cross(axis,std::fabs(axis[2])<0.9f?V{0,0,1}:V{0,1,0}));
    const V v=cross(axis,u);
    constexpr int sides=16;
    for(int i=0;i<sides;++i) {
        const float t0=6.28318530718f*i/sides,t1=6.28318530718f*(i+1)/sides;
        const V n0=add(mul(u,std::cos(t0)),mul(v,std::sin(t0)));
        const V n1=add(mul(u,std::cos(t1)),mul(v,std::sin(t1)));
        const V a0=add(a,mul(n0,r)),a1=add(a,mul(n1,r)),b0=add(b,mul(n0,r)),b1=add(b,mul(n1,r));
        g.vertex(a0,n0,color);g.vertex(a1,n1,color);g.vertex(b1,n1,color);
        g.vertex(a0,n0,color);g.vertex(b1,n1,color);g.vertex(b0,n0,color);
        g.tri(a,a1,a0,color);g.tri(b,b0,b1,color);
    }
}
void sphere(Geometry& g,V c,float r,const float* color) {
    auto at=[&](int row,int col) {
        const float lat=3.14159265359f*row/8,lon=6.28318530718f*col/16;
        return V{std::sin(lat)*std::cos(lon),std::sin(lat)*std::sin(lon),std::cos(lat)};
    };
    for(int i=0;i<8;++i) for(int j=0;j<16;++j) {
        const V a=at(i,j),b=at(i+1,j),d=at(i,j+1),e=at(i+1,j+1);
        for (const V& n:{a,b,e,a,e,d}) g.vertex(add(c,mul(n,r)),n,color);
    }
}
struct Glyph { std::string id,name; V position,lo,hi; std::map<std::string,V> ports; };
struct Cache {
    HydraulicPanel* owner=nullptr;
    std::uint64_t circuit=~std::uint64_t{0},layout=0,result=0,revision=0;
    std::string selected;
    int selectedIndex = -1;
    std::array<float, 9> colors{};
    Geometry bodies,hoses;
    std::vector<Glyph> glyphs;
    float peak=0;
    bool pressureColored = false;
} cache;
struct PointerGesture {
    HydraulicPanel* owner = nullptr;
    bool pressed = false, dragged = false, doubleClick = false;
} pointer;
HydraulicPanel* panel(AppState& st) {
    return st.simManager ? &st.simManager->hydraulic() : nullptr;
}
void rebuild(HydraulicPanel& p) {
    cache.owner=&p;cache.circuit=p.circuitRevision();cache.layout=p.layoutRevision();cache.result=p.resultRevision();
    cache.selected=p.selectedComponentId();++cache.revision;
    cache.selectedIndex = p.selectedComponentIndex();
    std::copy(ui::palette.accent, ui::palette.accent+3, cache.colors.begin());
    std::copy(ui::palette.accent2, ui::palette.accent2+3, cache.colors.begin()+3);
    std::copy(ui::palette.textDim, ui::palette.textDim+3, cache.colors.begin()+6);
    cache.bodies.clear();cache.hoses.clear();cache.glyphs.clear();cache.peak=0;
    const auto& nodes=jobj(p.simulateResult(),"nodes");
    cache.pressureColored = !nodes.empty();
    std::map<std::string,float> nodePeaks,memberPeaks;
    for(auto it=nodes.begin();it!=nodes.end();++it) {
        float peak=0;for(const auto& n:jarr(*it,"pressure_bar")) if(n.is_number()) peak=std::max(peak,n.get<float>());
        nodePeaks[it.key()]=peak;cache.peak=std::max(cache.peak,peak);
        for(const auto& member:jarr(*it,"members")) if(member.is_string()) memberPeaks[member.get<std::string>()]=peak;
    }
    for(const auto& c:jarr(p.circuit(),"components")) {
        Glyph glyph;glyph.id=jstr(c,"id");glyph.name=jstr(c,"name",jstr(c,"type"));p.componentPosition3d(c,glyph.position.data());
        const auto& spec=jobj(c,"params");const auto type=jstr(c,"type");
        const float* color=glyph.id==cache.selected?ui::palette.accent:ui::palette.accent2;
        const float* metal=ui::palette.textDim;
        const V origin=glyph.position;const std::size_t first=cache.bodies.xyz.size();
        V size{100,70,70};
        if(type=="cylinder_linear") {
            const float bore=std::max(8.0f,(float)jnum(spec,"bore_mm",63));
            const float rod=std::max(4.0f,(float)jnum(spec,"rod_mm",bore*0.55f));
            const float stroke=std::max(1.0f,(float)jnum(spec,"stroke_mm",300));
            const auto& series=jarr(jobj(jobj(jobj(p.simulateResult(),"components"),glyph.id.c_str()),"series"),"position_mm");
            float extension=0; if(!series.empty()&&series.back().is_number()) extension=series.back().get<float>();
            extension=std::clamp(extension,0.0f,stroke);
            size={stroke+80,bore+20,bore+20};
            tube(cache.bodies,add(origin,V{-size[0]/2,0,0}),add(origin,V{size[0]/2,0,0}),bore*0.5f+8,color);
            tube(cache.bodies,origin,add(origin,V{size[0]/2+extension+30,0,0}),rod*0.5f,metal);
            box(cache.bodies,add(origin,V{-size[0]/2,0,0}),V{18,size[1],size[2]},metal);
            box(cache.bodies,add(origin,V{size[0]/2,0,0}),V{18,size[1],size[2]},metal);
        } else if(type=="pump"||type=="motor_hydraulic"||type=="cylinder_rotary") {
            const float scale=std::cbrt(std::max(1.0,(double)jnum(spec,"displacement_cc_rev",16))/16.0);
            size={100*scale,70*scale,70*scale};box(cache.bodies,origin,size,color);
            tube(cache.bodies,add(origin,V{size[0]/2,0,0}),add(origin,V{size[0]/2+(type=="pump"?110:35)*scale,0,0}),30*scale,metal);
            if(type=="pump") box(cache.bodies,add(origin,V{size[0]/2+55*scale,0,-35*scale}),V{85*scale,60*scale,12*scale},metal);
        } else if(type=="reservoir") {
            const float edge=std::cbrt(std::max(0.1,jnum(spec,"volume_l",40))*1e6);
            size={edge,edge*0.65f,edge*0.7f};box(cache.bodies,origin,size,color);
            tube(cache.bodies,add(origin,V{0,0,size[2]/2}),add(origin,V{0,0,size[2]/2+20}),12,metal);
        } else if(type=="accumulator") {
            const float radius=std::cbrt(std::max(0.01,jnum(spec,"volume_l",1))*1e6/12.5663706144);
            size={radius*2,radius*2,radius*4};
            tube(cache.bodies,add(origin,V{0,0,-radius}),add(origin,V{0,0,radius}),radius,color);
            sphere(cache.bodies,add(origin,V{0,0,-radius}),radius,color);sphere(cache.bodies,add(origin,V{0,0,radius}),radius,color);
        } else {
            box(cache.bodies,origin,size,color);
            if(type.find("valve")!=std::string::npos) {
                tube(cache.bodies,add(origin,V{-70,0,0}),add(origin,V{-50,0,0}),22,metal);
                tube(cache.bodies,add(origin,V{50,0,0}),add(origin,V{70,0,0}),22,metal);
            }
        }
        auto port=[&](const std::string& name) {
            if(glyph.ports.count(name)) return;
            V offset{0,0,size[2]/2};
            if(name=="A"||name=="P"||name=="IN") offset={-size[0]*0.3f,size[1]/2,0};
            else if(name=="B"||name=="OUT") offset={size[0]*0.3f,size[1]/2,0};
            else if(name=="T"||name=="S") offset={-size[0]*0.3f,-size[1]/2,0};
            else if(name=="T2") offset={0,-size[1]/2,0};
            else if(name=="T3"||name=="L") offset={size[0]*0.3f,-size[1]/2,0};
            const V anchor=add(origin,offset);glyph.ports[name]=anchor;
            tube(cache.bodies,anchor,add(anchor,mul(unit(offset),12)),6,metal);
        };
        for(const auto& portJson:jarr(c,"ports")) port(jstr(portJson,"id"));
        for(const auto& h:jarr(p.circuit(),"hoses")) for(const auto& e:jarr(h,"connections"))
            if(jstr(e,"component_id")==glyph.id) port(jstr(e,"port"));
        // Native component angle is degrees; rotate the glyph and its ports together.
        const float angle = (float)jnum(c, "angle") * 0.01745329252f;
        const float cs = std::cos(angle), sn = std::sin(angle);
        auto rotate = [&](V v) { return V{cs*v[0]-sn*v[1], sn*v[0]+cs*v[1], v[2]}; };
        glyph.lo = {FLT_MAX, FLT_MAX, FLT_MAX};
        glyph.hi = {-FLT_MAX, -FLT_MAX, -FLT_MAX};
        for (std::size_t i = first; i < cache.bodies.xyz.size(); i += 3) {
            const V at = add(origin, rotate(sub(V{cache.bodies.xyz[i], cache.bodies.xyz[i+1],
                                                   cache.bodies.xyz[i+2]}, origin)));
            const V normal = rotate(V{cache.bodies.nrm[i], cache.bodies.nrm[i+1], cache.bodies.nrm[i+2]});
            for (int k = 0; k < 3; ++k) {
                cache.bodies.xyz[i+k] = at[k];
                cache.bodies.nrm[i+k] = normal[k];
                glyph.lo[k] = std::min(glyph.lo[k], at[k]);
                glyph.hi[k] = std::max(glyph.hi[k], at[k]);
            }
        }
        for (auto& entry : glyph.ports) entry.second = add(origin, rotate(sub(entry.second, origin)));
        cache.glyphs.push_back(std::move(glyph));
    }
    for(const auto& hose:jarr(p.circuit(),"hoses")) {
        std::vector<V> ends;float pressure=nodePeaks[jstr(hose,"id")];
        for(const auto& e:jarr(hose,"connections")) {
            const auto id=jstr(e,"component_id"),port=jstr(e,"port");pressure=std::max(pressure,memberPeaks[id+"."+port]);
            for(const auto& g:cache.glyphs) if(g.id==id) {const auto it=g.ports.find(port);if(it!=g.ports.end()) ends.push_back(it->second);break;}
        }
        if(ends.size()<2) continue;
        float rgb[3];std::copy(ui::palette.textDim,ui::palette.textDim+3,rgb);
        if(!nodes.empty()) result_ramp::sample(cache.peak>0?pressure/cache.peak:0,rgb);
        const float radius=std::max(1.0f,(float)jnum(hose,"id_mm",jnum(jobj(hose,"params"),"id_mm",12.7))*0.5f+2.0f);
        V hub{};for(V e:ends)hub=add(hub,mul(e,1.0f/(float)ends.size()));hub[2]+=45;
        for(V e:ends) {
            const V lift{e[0],e[1],hub[2]},bend{hub[0],e[1],hub[2]};
            tube(cache.hoses,e,lift,radius,rgb);tube(cache.hoses,lift,bend,radius,rgb);tube(cache.hoses,bend,hub,radius,rgb);
            sphere(cache.hoses,lift,radius,rgb);sphere(cache.hoses,bend,radius,rgb);
        }
        sphere(cache.hoses,hub,radius,rgb);
    }
}
void ensure(HydraulicPanel& p) {
    const bool themeChanged =
        !std::equal(cache.colors.begin(), cache.colors.begin()+3, ui::palette.accent) ||
        !std::equal(cache.colors.begin()+3, cache.colors.begin()+6, ui::palette.accent2) ||
        !std::equal(cache.colors.begin()+6, cache.colors.end(), ui::palette.textDim);
    if (cache.owner != &p || cache.circuit != p.circuitRevision() ||
        cache.layout != p.layoutRevision() || cache.result != p.resultRevision() ||
        cache.selectedIndex != p.selectedComponentIndex() || themeChanged)
        rebuild(p);
}
}

bool active(AppState& st) {
    const auto* p=panel(st);
    return st.activeWorkspace==AppState::Workspace::Simulate && p && p->isOpen() && p->centralView()==HydraulicPanel::CentralView::View3D;
}
void collect(AppState& st,simscene::Scene& out) {
    if(!active(st))return;auto& p=*panel(st);ensure(p);
    auto publish=[&](std::uint64_t key,Geometry& g) {
        if(g.xyz.empty())return;simscene::LitBatch b;b.key=key;b.xyz=&g.xyz;b.nrm=&g.nrm;b.rgb=&g.rgb;b.revision=cache.revision;
        std::copy(ui::palette.accent2,ui::palette.accent2+4,b.rgba);out.lit.push_back(b);
    };
    publish(0x2000,cache.bodies);publish(0x2001,cache.hoses);
    if(!cache.bodies.xyz.empty()&&p.takeFrame3d()) {
        for(int k=0;k<3;++k){st.zoomToBBoxMin[k]=FLT_MAX;st.zoomToBBoxMax[k]=-FLT_MAX;}
        for(const auto* g:{&cache.bodies,&cache.hoses}) for(std::size_t i=0;i<g->xyz.size();i+=3) for(int k=0;k<3;++k) {
            st.zoomToBBoxMin[k]=std::min(st.zoomToBBoxMin[k],g->xyz[i+k]-30);st.zoomToBBoxMax[k]=std::max(st.zoomToBBoxMax[k],g->xyz[i+k]+30);
        }
        st.zoomToBBoxRequested=true;
    }
}
bool toolArmed(AppState& st) {return active(st)&&panel(st)->placing3d();}
bool onPlaneChip(AppState& st,simscene::Plane plane) {
    if(!toolArmed(st))return false;panel(st)->placementPlane=plane;return true;
}
bool handleViewport(AppState& st,const simscene::ViewportCtx& vp,ImDrawList* dl) {
    if (!active(st)) { pointer = {}; return false; }
    auto& p = *panel(st);
    ensure(p);
    if (cache.pressureColored && !cache.hoses.xyz.empty() && dl) {
        // Exactly the hose ramp normalization: blue at zero, red at the largest node peak (in
        // bar). The legend shows it in the Simulation pane's units (psi when US customary; the
        // conversion is linear, so the ramp is unchanged). The shared legend owns tick
        // formatting and framing reservation.
        const sim::Disp bar = sim::disp("bar", SimManager::unitSys(st).us);
        const bool previousOn = st.legendOn;
        const auto previousMin = st.legendMin, previousMax = st.legendMax;
        std::string title = "Peak pressure", units = bar.unit;
        title.swap(st.legendTitle);
        units.swap(st.legendUnits);
        st.legendOn = true;
        st.legendMin = (float)sim::toDisplay(0.0, bar);
        st.legendMax = (float)sim::toDisplay(cache.peak, bar);
        dl->PushClipRect(vp.imgMin, vp.imgMax, true);
        ui::drawLegend(st, dl, vp.imgMin, vp.imgMax);
        dl->PopClipRect();
        st.legendOn = previousOn;
        st.legendMin = previousMin;
        st.legendMax = previousMax;
        title.swap(st.legendTitle);
        units.swap(st.legendUnits);
    }
    if (pointer.owner != &p) { pointer = {}; pointer.owner = &p; }
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && vp.hovered && !vp.navigating) {
        pointer.pressed = true;
        pointer.dragged = false;
        pointer.doubleClick = ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
    }
    if (pointer.pressed && (vp.navigating || ImGui::IsMouseDragging(ImGuiMouseButton_Left)))
        pointer.dragged = true;
    const bool released = ImGui::IsMouseReleased(ImGuiMouseButton_Left);
    const bool click = released && pointer.pressed && !pointer.dragged && vp.hovered && !vp.navigating;
    const bool doubleClick = click && pointer.doubleClick;
    if (released) pointer.pressed = false;
    if (!vp.hovered || vp.navigating) return false;
    const auto& io = ImGui::GetIO();
    const bool keys = !io.WantTextInput && !ImGui::IsAnyItemActive() &&
        !(st.propertyManager && st.propertyManager->active()) &&
        !(st.sketchMode && st.sketchMode->active()) &&
        !(st.sketch3dMode && st.sketch3dMode->active()) && !st.sketchPlanePickActive;
    const ImGuiID keyOwner = ImGui::GetID("hyd_scene_edit_keys");
    const auto pressed = [&](ImGuiKey key) {
        if (!ImGui::IsKeyPressed(key, ImGuiInputFlags_None, keyOwner)) return false;
        ImGui::SetKeyOwner(key, keyOwner, ImGuiInputFlags_LockThisFrame);
        return true;
    };
    if (keys) {
        if ((io.KeyCtrl || io.KeySuper) && pressed(ImGuiKey_Z)) {
            pointer.pressed = false;
            p.armPlacement3d(false);
            if (io.KeyShift) p.redo(st); else p.undo(st);
            return true;
        }
        if ((io.KeyCtrl || io.KeySuper) && pressed(ImGuiKey_Y)) {
            pointer.pressed = false;
            p.armPlacement3d(false);
            p.redo(st);
            return true;
        }
        if (pressed(ImGuiKey_Delete) || pressed(ImGuiKey_Backspace)) {
            pointer.pressed = false;
            p.armPlacement3d(false);
            p.deleteSelection(st);
            return true;
        }
    }
    const auto mouse = ImGui::GetIO().MousePos;
    float ro[3], rd[3]; simscene::screenRay(vp, mouse, ro, rd);
    if(p.placing3d()) {
        if (keys && (pressed(ImGuiKey_Escape) || pressed(ImGuiKey_Enter))) {
            pointer.pressed = false;
            p.armPlacement3d(false);
            return true;
        }
        if (keys && pressed(ImGuiKey_Tab))
            p.placementPlane = (simscene::Plane)(((int)p.placementPlane + 1) % 3);
        const int normal=p.placementPlane==simscene::Plane::XY?2:p.placementPlane==simscene::Plane::XZ?1:0;
        if (!std::isfinite(p.placementOffsetMm) || std::fabs(rd[normal]) < 1e-7f) return click;
        const float t = ((float)p.placementOffsetMm - ro[normal]) / rd[normal];
        if (!std::isfinite(t) || t < 0) return click;
        V position;
        for (int k = 0; k < 3; ++k) {
            position[k] = ro[k] + t * rd[k];
            if (k != normal && p.placementSnap && p.placementGridMm > 0 && std::isfinite(p.placementGridMm))
                position[k] = (float)(std::round(position[k] / p.placementGridMm) * p.placementGridMm);
        }
        position[normal] = (float)p.placementOffsetMm;
        if(p.placementSnap && std::isfinite(p.placementGridMm) && p.placementGridMm > 0) {
            const int a=normal==0?1:0,b=normal==2?1:2;
            for(int i=-8;i<=8;++i) for(int dir=0;dir<2;++dir) {
                V x=position,y=position;
                const int fixed=dir?a:b,varying=dir?b:a;
                x[fixed]+=(float)(i*p.placementGridMm);y[fixed]=x[fixed];
                x[varying]-=(float)(8*p.placementGridMm);y[varying]+=(float)(8*p.placementGridMm);
                ImVec2 sx,sy;if(simscene::worldToScreen(vp,x.data(),sx)&&simscene::worldToScreen(vp,y.data(),sy))
                    dl->AddLine(sx,sy,ui::Palette::toU32(ui::palette.textDim),metrics::stroke(0.5f));
            }
        }
        ImVec2 screen;if(simscene::worldToScreen(vp,position.data(),screen)) {
            dl->AddCircle(screen,metrics::px(7),ui::Palette::toU32(ui::palette.accent),0,metrics::stroke(2));
        }
        if(click){p.moveComponent3d(st,p.selectedComponentId(),position.data());return true;}
        return false;
    }
    const Glyph* hit=nullptr;float nearest=FLT_MAX;
    for(const auto& g:cache.glyphs) {
        float nearT=0,farT=FLT_MAX;
        for(int k=0;k<3;++k) {
            if(std::fabs(rd[k])<1e-8f){if(ro[k]<g.lo[k]||ro[k]>g.hi[k])farT=-1;continue;}
            float a=(g.lo[k]-ro[k])/rd[k],b=(g.hi[k]-ro[k])/rd[k];if(a>b)std::swap(a,b);
            nearT=std::max(nearT,a);farT=std::min(farT,b);
        }
        if(farT>=nearT&&nearT<nearest){nearest=nearT;hit=&g;}
    }
    if(!hit)return false;
    ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    if (!ImGui::IsMouseDown(ImGuiMouseButton_Left))
        kit::tooltip((hit->name + "\nClick to edit in the side panel.\nDouble-click to place in 3D.").c_str());
    if (click) {
        p.selectComponent3d(hit->id);
        if (doubleClick) p.armPlacement3d(true);
        return true;
    }
    return false;
}
} // namespace ui::hydscene
