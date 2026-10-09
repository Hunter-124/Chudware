#include "pipe_sketch.h"
#include "../../core/app_state.h"
#include "../../ui_theme.h"
#include "../cursors.h"
#include "../viewport_panel.h"
#include "../cam/cam_panel.h"
#include <algorithm>
#include <cstdio>
#include <cstdlib>

namespace ui::pipescene {
namespace {
Point normal(int plane) { return plane==1?Point{0,1,0}:plane==2?Point{1,0,0}:Point{0,0,1}; }
void basis(int plane,Point& u,Point& v) {
    u=plane==2?Point{0,1,0}:Point{1,0,0};
    v=plane==0?Point{0,1,0}:Point{0,0,1};
}
bool same(Point a,Point b) { return a.x==b.x && a.y==b.y && a.z==b.z; }
float screenDistance(ImVec2 a,ImVec2 b) { return std::hypot(a.x-b.x,a.y-b.y); }
float specNumber(const nlohmann::json& j,const char* key,float fallback) {
    auto it=j.find(key); return it!=j.end() && it->is_number()?it->get<float>():fallback;
}
void line(std::vector<float>& v,Point a,Point b) { v.insert(v.end(),{a.x,a.y,a.z,b.x,b.y,b.z}); }
Point axis(int index) { return index==0?Point{1,0,0}:index==1?Point{0,1,0}:Point{0,0,1}; }
int normalAxis(int plane) { return plane==0?2:plane==1?1:0; }
bool closestAxis(Point origin,Point ray,Point anchor,Point direction,Point& out) {
    const Point w=anchor-origin; const float b=dot(direction,ray),den=1-b*b;
    // An axis pointing straight at the camera has no useful screen-space inference line.
    if(den<1e-5f) return false;
    const float s=(b*dot(ray,w)-dot(direction,w))/den;
    if(dot(ray,w)+b*s<0 || !std::isfinite(s)) return false;
    out=anchor+direction*s; return true;
}
float worldPerPixel(const simscene::ViewportCtx& vp,Point at) {
    float height=vp.cam.halfHeight;
    if(!vp.cam.orthographic && vp.cam.dist>1e-6f) {
        const Point eye{vp.cam.eye.x,vp.cam.eye.y,vp.cam.eye.z};
        const Point direction{vp.cam.dir.x,vp.cam.dir.y,vp.cam.dir.z};
        height*=std::max(dot(eye-at,direction)/vp.cam.dist,1e-3f);
    }
    return std::max(2*height/std::max(vp.vh,1),1e-6f);
}
void patchTriangle(Soup& mesh,Point a,Point b,Point c,Point n) {
    mesh.xyz.insert(mesh.xyz.end(),{a.x,a.y,a.z,b.x,b.y,b.z,c.x,c.y,c.z});
    for(int i=0;i<3;++i) mesh.nrm.insert(mesh.nrm.end(),{n.x,n.y,n.z});
}
void dashedLine(ImDrawList* dl,ImVec2 a,ImVec2 b,ImU32 colour,float scale) {
    const float span=screenDistance(a,b);
    if(span<1e-3f) return;
    const float stride=10*scale;
    const int count=std::min(int(std::ceil(span/stride)),256);
    for(int i=0;i<count;++i) {
        const float t0=i*stride/span,t1=std::min((i*stride+6*scale)/span,1.0f);
        dl->AddLine({a.x+(b.x-a.x)*t0,a.y+(b.y-a.y)*t0},
                    {a.x+(b.x-a.x)*t1,a.y+(b.y-a.y)*t1},colour,scale);
    }
}
Point gridInterior(const PipingPanel::DrawSettings& settings,const MeshCache& mesh,const Hit& hit) {
    if(!settings.snapGrid || settings.gridMm<=0 || hit.run<0) return hit.point;
    const auto& path=mesh.runs[hit.run].path;
    std::size_t segment=0; float best=1e30f;
    for(std::size_t i=1;i<path.size();++i) {
        const Point d=path[i]-path[i-1]; const float dd=dot(d,d);
        if(dd<1e-10f) continue;
        const float t=std::clamp(dot(hit.point-path[i-1],d)/dd,0.0f,1.0f);
        const float distance=length(hit.point-(path[i-1]+d*t));
        if(distance<best) { best=distance; segment=i; }
    }
    if(segment==0) return hit.point;
    const Point a=path[segment-1],d=path[segment]-a;
    const float span=length(d),rawT=dot(hit.point-a,d)/dot(d,d);
    if(rawT<=0 || rawT>=1) return hit.point;
    const Point direction=d*(1/span); const float pitch=float(settings.gridMm);
    float t;
    const Point axes[]={{1,0,0},{0,1,0},{0,0,1}};
    int axis=-1;
    for(int i=0;i<3;++i) if(std::fabs(dot(direction,axes[i]))>1-1e-6f) { axis=i; break; }
    if(axis>=0) {
        const float coordinate=std::round(dot(hit.point,axes[axis])/pitch)*pitch;
        t=(coordinate-dot(a,axes[axis]))/dot(d,axes[axis]);
    } else {
        t=std::round(rawT*span/pitch)*pitch/span;
    }
    t=std::clamp(t,0.0f,1.0f);
    // Never turn an interior snap into an endpoint/junction with different connectivity.
    return t>0 && t<1 ? a+d*t : hit.point;
}
ImU32 ink(const float* colour,float alpha=1) {
    float c[4]; std::copy_n(colour,4,c); c[3]*=alpha; return ui::palette.toU32(c);
}
}
void SketchTool::clearPolyline(PipingPanel* panel) {
    points_.clear(); numeric_=false; numberLength_=0; number_[0]='\0';
    haveCursor_=false; snap_=Snap::None; axis_=-1; runOd_=runBend_=0;
    pendingFinish_=false; pendingRunOps_=nullptr; pendingFittingOps_=nullptr;
    if(panel) panel->drawSettings().autoPlane=true;
    if(!ghost_.xyz.empty()) { ghost_.clear(); ++ghostRevision_; }
    previewPath_.clear();
}
void SketchTool::reset() {
    clearPolyline(); previousTool_=PipingPanel::Tool::None;
    pressed_=dragged_=doubleClick_=swallowRelease_=false;
    if(gridPlane_>=0) {
        for(auto& grid:grid_) grid.clear();
        for(auto& patch:planePatch_) patch.clear();
        ++gridRevision_;
    }
    activePlane_=0; explicitPlane_=-1; gridPlane_=-1;
    previewFitting_.type.clear(); previewFitting_.params=nullptr; fittingOd_=0;
}
void SketchTool::tick(AppState& st,PipingPanel& panel) {
    if(panel.tool()!=previousTool_) {
        if(previousTool_!=PipingPanel::Tool::None) panel.drawSettings().autoPlane=true;
        reset(); previousTool_=panel.tool();
    }
    // Input is handled after collection. Let a queued Escape reach cancellation before
    // auto-submitting an edit that became available on this same frame.
    if((!pendingFinish_ && pendingFittingOps_.is_null()) || panel.busy() || panel.editPending() ||
       ImGui::IsKeyPressed(ImGuiKey_Escape))
        return;
    if(pendingFinish_) {
        if(panel.submitEdit(st,pendingRunOps_,"Run drawn")) clearPolyline(&panel);
    } else if(panel.submitEdit(st,pendingFittingOps_,"Fitting inserted")) {
        pendingFittingOps_=nullptr;
        if(!ghost_.xyz.empty()) { ghost_.clear(); ++ghostRevision_; }
        fittingOd_=0;
    }
}
bool SketchTool::onPlane(PipingPanel& panel,simscene::Plane plane) {
    if(panel.tool()==PipingPanel::Tool::None) return false;
    auto& settings=panel.drawSettings();
    settings.plane=int(plane); settings.autoPlane=false;
    explicitPlane_=activePlane_=settings.plane;
    // The viewport plane chip is pressed after our handler. Its release must not become
    // another point in the pipe polyline, even though it lies inside the viewport image.
    pressed_=false; swallowRelease_=true;
    return true;
}
void SketchTool::updatePlane(PipingPanel& panel,const simscene::ViewportCtx& vp) {
    auto& settings=panel.drawSettings();
    // External explicit-plane changes (including the side panel) never restart the chain.
    if(explicitPlane_>=0 && explicitPlane_!=settings.plane) settings.autoPlane=false;
    explicitPlane_=settings.plane;
    activePlane_=settings.plane;
    if(!settings.autoPlane || points_.empty()) return;
    const Point facing{vp.cam.dir.x,vp.cam.dir.y,vp.cam.dir.z};
    float best=-1;
    for(int plane=0;plane<3;++plane) {
        const float score=std::fabs(dot(normal(plane),facing));
        if(score>best) { best=score; activePlane_=plane; }
    }
}
bool SketchTool::cursorWorld(PipingPanel& panel,const MeshCache& mesh,
                              const simscene::ViewportCtx& vp,Point& out) {
    const auto& settings=panel.drawSettings(); const auto& io=ImGui::GetIO();
    const float threshold=10.0f*ui::uiScale;
    snap_=Snap::None; axis_=-1;
    float ro[3],rd[3]; simscene::screenRay(vp,io.MousePos,ro,rd);
    const Point origin{ro[0],ro[1],ro[2]},ray{rd[0],rd[1],rd[2]},n=normal(activePlane_);
    const Point anchor=points_.empty()?n*float(settings.planeOffsetMm):points_.back();
    const bool vertical=io.KeyShift && !points_.empty();
    // Own handles and pipe geometry remain exact snaps, even outside the working plane.
    // Shift only admits snaps on its explicit normal line.
    if(!points_.empty()) {
        for(int i=0;i<2;++i) {
            const Point p=i==0?points_.front():points_.back(); ImVec2 s;
            if(project(vp,p,s) && screenDistance(s,io.MousePos)<threshold &&
               (!vertical || length(cross(p-anchor,n))<1e-3f)) {
                out=p; snap_=i==0?Snap::Start:Snap::Last; return true;
            }
        }
    }
    if(settings.snapPipes) {
        const Hit vertex=hitRun(mesh,vp,io.MousePos,threshold,true);
        if(vertex.run>=0 && (!vertical || length(cross(vertex.point-anchor,n))<1e-3f)) {
            out=vertex.point; snap_=Snap::Vertex; return true;
        }
        const Hit interior=hitRun(mesh,vp,io.MousePos,threshold);
        if(interior.run>=0 && (!vertical || length(cross(interior.point-anchor,n))<1e-3f)) {
            const Point snapped=gridInterior(settings,mesh,interior);
            out=!vertical || length(cross(snapped-anchor,n))<1e-3f ? snapped : interior.point;
            snap_=Snap::Interior; return true;
        }
    }
    bool havePoint=false;
    if(vertical) {
        if(!closestAxis(origin,ray,anchor,n,out)) return false;
        snap_=Snap::Vertical; axis_=normalAxis(activePlane_); havePoint=true;
    } else {
        const float den=dot(n,ray);
        if(std::fabs(den)>1e-6f) {
            const float t=dot(n,anchor-origin)/den;
            if(t>=0) { out=origin+ray*t; havePoint=true; }
        }
        if(settings.snapAxis && !points_.empty()) {
            float best=8.0f*ui::uiScale;
            for(int i=0;i<3;++i) {
                Point q; ImVec2 sq;
                // Use ray-line closest approach, not a projection of the plane hit. That
                // also admits the normal axis, so a riser/drop needs no modifier.
                if(!closestAxis(origin,ray,anchor,axis(i),q) || !project(vp,q,sq)) continue;
                const float distance=screenDistance(sq,io.MousePos);
                if(distance<best) {
                    best=distance; out=q; snap_=Snap::Axis; axis_=i; havePoint=true;
                }
            }
        }
    }
    if(!havePoint) return false;
    if(settings.snapGrid && settings.gridMm>0) {
        const float pitch=float(settings.gridMm);
        if(vertical || snap_==Snap::Axis) {
            const Point direction=axis(axis_);
            out=anchor+direction*(std::round(dot(out,direction)/pitch)*pitch-dot(anchor,direction));
        } else {
            Point u,v; basis(activePlane_,u,v);
            out=anchor+u*(std::round(dot(out,u)/pitch)*pitch-dot(anchor,u))
                      +v*(std::round(dot(out,v)/pitch)*pitch-dot(anchor,v));
            snap_=Snap::Grid;
        }
    }
    return true;
}
bool SketchTool::finish(AppState& st,PipingPanel& panel) {
    if(points_.size()<2) { clearPolyline(&panel); return false; }
    if(!pendingFinish_) {
        nlohmann::json op=panel.newRunSpec(); op["op"]="add_run";
        auto path=nlohmann::json::array(); for(const auto& p:points_) path.push_back({p.x,p.y,p.z});
        op["path"]=std::move(path);
        pendingRunOps_=nlohmann::json::array({std::move(op)});
        pendingFinish_=true;
        numeric_=false; haveCursor_=false;
    }
    if(panel.busy() || panel.editPending() ||
       !panel.submitEdit(st,pendingRunOps_,"Run drawn")) return false;
    clearPolyline(&panel); return true;
}
void SketchTool::updatePreview(PipingPanel& panel,const MeshCache& mesh,const Hit* hit,bool show) {
    if(!show) {
        if(!ghost_.xyz.empty()) { ghost_.clear(); ++ghostRevision_; }
        previewPath_.clear(); fittingOd_=0; return;
    }
    if(panel.tool()==PipingPanel::Tool::DrawRun) {
        const bool append=haveCursor_ && !points_.empty() && length(cursor_-points_.back())>1e-4f;
        const std::size_t count=points_.size()+(append?1:0);
        const double nps=panel.newRunNps(); const bool shortRadius=panel.newRunShortRadius();
        const bool specChanged=runOd_<=0 || runNps_!=nps || runShortRadius_!=shortRadius;
        const float od=specChanged?outerDiameter(nps):runOd_;
        const float bend=specChanged?od*(shortRadius?1:1.5f):runBend_;
        runNps_=nps; runShortRadius_=shortRadius;
        bool prefixChanged=points_.size()>previewPath_.size();
        if(!prefixChanged)
            for(std::size_t i=0;i<points_.size();++i)
                if(!same(points_[i],previewPath_[i])) { prefixChanged=true; break; }
        const bool cursorChanged=append && (previewPath_.empty() || !same(cursor_,previewPath_.back()));
        if(!prefixChanged && !cursorChanged && count==previewPath_.size() &&
           runOd_==od && runBend_==bend) return;
        // POD panel accessors keep live spec changes allocation-free; only the final edit
        // constructs the canonical JSON run specification.
        runOd_=od; runBend_=bend;
        previewPath_.resize(count);
        if(prefixChanged) std::copy(points_.begin(),points_.end(),previewPath_.begin());
        if(append) previewPath_.back()=cursor_;
        const bool hadGeometry=!ghost_.xyz.empty();
        ghost_.clear();
        appendRun(ghost_,previewPath_,runOd_,runBend_,&previewRounded_);
        if(hadGeometry || !ghost_.xyz.empty()) ++ghostRevision_;
    } else if(hit && hit->run>=0) {
        const auto& r=mesh.runs[hit->run]; Point axis{1,0,0}; float best=1e30f;
        for(std::size_t i=1;i<r.path.size();++i) {
            const Point d=r.path[i]-r.path[i-1]; const float t=std::clamp(dot(hit->point-r.path[i-1],d)/dot(d,d),0.0f,1.0f);
            const float distance=length(hit->point-(r.path[i-1]+d*t));
            if(distance<best) { best=distance; axis=unit(d); }
        }
        const auto& settings=panel.drawSettings();
        const bool settingsChanged=previewFitting_.type!=settings.insertType ||
                                   previewFitting_.params!=settings.insertParams;
        if(!settingsChanged && fittingOd_==r.od && same(previewFitting_.origin,hit->point) &&
           same(previewFitting_.axis,axis)) return;
        if(settingsChanged) {
            previewFitting_.type=settings.insertType;
            previewFitting_.params=settings.insertParams;
        }
        fittingOd_=r.od;
        auto& f=previewFitting_; f.origin=hit->point; f.axis=axis;
        f.branch=std::fabs(axis.z)<0.9f?Point{0,0,1}:Point{0,1,0}; f.od=f.od2=r.od;
        if(f.type=="reducer") f.od2=outerDiameter(specNumber(settings.insertParams,"nps2",2));
        ghost_.clear(); appendFitting(ghost_,f); ++ghostRevision_;
    }
}
void SketchTool::updateGrid(PipingPanel& panel,const simscene::ViewportCtx& vp,bool show) {
    if(!show || (points_.empty() && !haveCursor_)) {
        if(gridPlane_>=0) {
            for(auto& grid:grid_) grid.clear();
            for(auto& patch:planePatch_) patch.clear();
            ++gridRevision_; gridPlane_=-1;
        }
        return;
    }
    const auto& settings=panel.drawSettings();
    Point u,v; basis(activePlane_,u,v); const Point n=cross(u,v);
    const Point center=points_.empty()?cursor_:points_.back();
    const float pixel=worldPerPixel(vp,center),extent=pixel*140*ui::uiScale;
    float pitch=float(settings.gridMm);
    if(!(pitch>0) || !std::isfinite(pitch)) pitch=pixel*24*ui::uiScale;
    // Keep the visual grid readable and bounded when zoomed far out; snapping still uses
    // the original pitch, not this display-only multiple.
    while(pitch<pixel*20*ui::uiScale) pitch*=2;
    if(gridPlane_==activePlane_ && gridPitch_==pitch && gridExtent_==extent &&
       same(gridCenter_,center)) return;
    gridPlane_=activePlane_; gridPitch_=pitch; gridExtent_=extent; gridCenter_=center;
    constexpr float signs[4][2]={{-1,-1},{1,-1},{1,1},{-1,1}};
    for(std::size_t layer=0;layer<planePatch_.size();++layer) {
        auto& patch=planePatch_[layer]; patch.clear();
        patch.xyz.reserve(72); patch.nrm.reserve(72);
        const float inner=extent*float(layer)/planePatch_.size();
        const float outer=extent*float(layer+1)/planePatch_.size();
        Point a[4],b[4];
        for(int corner=0;corner<4;++corner) {
            a[corner]=center+u*(signs[corner][0]*inner)+v*(signs[corner][1]*inner);
            b[corner]=center+u*(signs[corner][0]*outer)+v*(signs[corner][1]*outer);
        }
        if(layer==0) {
            patchTriangle(patch,b[0],b[1],b[2],n);
            patchTriangle(patch,b[0],b[2],b[3],n);
        } else for(int corner=0;corner<4;++corner) {
            const int next=(corner+1)%4;
            patchTriangle(patch,a[corner],b[corner],b[next],n);
            patchTriangle(patch,a[corner],b[next],a[next],n);
        }
    }
    for(auto& grid:grid_) { grid.clear(); grid.reserve(96*6); }
    constexpr float cuts[]={-1,-0.65f,-0.3f,0.3f,0.65f,1};
    constexpr int bands[]={2,1,0,1,2};
    for(int direction=0;direction<2;++direction) {
        const Point fixed=direction?u:v,varying=direction?v:u;
        const float coordinate=dot(center,fixed);
        const float first=std::ceil((coordinate-extent)/pitch)*pitch;
        for(int i=0;i<16;++i) {
            const float offset=first+i*pitch-coordinate;
            if(offset>extent) break;
            const float fraction=std::fabs(offset)/extent;
            const int band=fraction>0.65f?2:fraction>0.3f?1:0;
            for(int segment=0;segment<5;++segment) {
                line(grid_[std::max(band,bands[segment])],
                     center+fixed*offset+varying*(cuts[segment]*extent),
                     center+fixed*offset+varying*(cuts[segment+1]*extent));
            }
        }
    }
    ++gridRevision_;
}
void SketchTool::collect(simscene::Scene& out) const {
    if(!ghost_.xyz.empty()) {
        simscene::LitBatch b; b.key=0x1010; b.xyz=&ghost_.xyz; b.nrm=&ghost_.nrm;
        b.revision=ghostRevision_; b.bounds=false;
        std::copy_n(ui::palette.accent,4,b.rgba); b.rgba[3]*=0.28f; out.lit.push_back(b);
    }
    const float* planeColour=activePlane_==0?ui::palette.axisZ:
                             activePlane_==1?ui::palette.axisY:ui::palette.axisX;
    constexpr float patchAlpha[]={0.075f,0.05f,0.03f,0.012f};
    for(std::size_t layer=0;layer<planePatch_.size();++layer) {
        const auto& patch=planePatch_[layer]; if(patch.xyz.empty()) continue;
        simscene::LitBatch b; b.key=0x1012+layer; b.xyz=&patch.xyz; b.nrm=&patch.nrm;
        b.revision=gridRevision_; b.bounds=false;
        std::copy_n(planeColour,4,b.rgba); b.rgba[3]*=patchAlpha[layer]; out.lit.push_back(b);
    }
    constexpr std::uint64_t gridKeys[]={0x1011,0x1016,0x1017};
    constexpr float gridAlpha[]={0.75f,0.45f,0.18f};
    for(std::size_t layer=0;layer<grid_.size();++layer) {
        if(grid_[layer].empty()) continue;
        simscene::LineBatch b; b.key=gridKeys[layer]; b.xyz=&grid_[layer];
        b.revision=gridRevision_; b.width=1; b.overSurface=false; b.bounds=false;
        std::copy_n(ui::palette.sketchGrid,4,b.rgba); b.rgba[3]*=gridAlpha[layer]; out.lines.push_back(b);
    }
}
void SketchTool::drawHud(PipingPanel& panel,const simscene::ViewportCtx& vp,ImDrawList* dl) const {
    if(!dl) return;
    const auto& pal=ui::palette; const float scale=ui::uiScale;
    dl->PushClipRect(vp.imgMin,vp.imgMax,true);
    for(std::size_t i=0;i<points_.size();++i) {
        ImVec2 s; if(!project(vp,points_[i],s)) continue;
        dl->AddCircleFilled(s,3.5f*scale,ink(pal.accent));
        if(i>0) { ImVec2 a; if(project(vp,points_[i-1],a)) dl->AddLine(a,s,ink(pal.accent,0.7f),2.0f*scale); }
    }
    ImVec2 cursorScreen;
    if(haveCursor_ && project(vp,cursor_,cursorScreen)) {
        const float r=6.0f*scale;
        const float* axisColour=axis_==0?pal.axisX:axis_==1?pal.axisY:pal.axisZ;
        const ImU32 color=ink(snap_==Snap::Axis || snap_==Snap::Vertical?axisColour:
                              snap_==Snap::None?pal.accent:pal.sketchInfer);
        if(snap_==Snap::Vertex || snap_==Snap::Start || snap_==Snap::Last) {
            dl->AddRect({cursorScreen.x-r,cursorScreen.y-r},{cursorScreen.x+r,cursorScreen.y+r},color,0,0,1.5f*scale);
            if(snap_==Snap::Start)
                dl->AddRect({cursorScreen.x-r-3*scale,cursorScreen.y-r-3*scale},
                            {cursorScreen.x+r+3*scale,cursorScreen.y+r+3*scale},color,0,0,scale);
            if(snap_==Snap::Last)
                dl->AddLine({cursorScreen.x-r,cursorScreen.y-r},{cursorScreen.x+r,cursorScreen.y+r},color,scale);
        } else if(snap_==Snap::Interior) {
            dl->AddTriangle({cursorScreen.x,cursorScreen.y-r},{cursorScreen.x+r,cursorScreen.y+r},{cursorScreen.x-r,cursorScreen.y+r},color,1.5f*scale);
        } else if(snap_==Snap::Grid) {
            dl->AddLine({cursorScreen.x-r,cursorScreen.y},{cursorScreen.x+r,cursorScreen.y},color,1.5f*scale);
            dl->AddLine({cursorScreen.x,cursorScreen.y-r},{cursorScreen.x,cursorScreen.y+r},color,1.5f*scale);
        } else if(snap_==Snap::Axis) {
            dl->AddQuad({cursorScreen.x,cursorScreen.y-r},{cursorScreen.x+r,cursorScreen.y},
                        {cursorScreen.x,cursorScreen.y+r},{cursorScreen.x-r,cursorScreen.y},color,1.5f*scale);
        } else {
            dl->AddCircle(cursorScreen,r,color,16,1.5f*scale);
            if(snap_==Snap::Vertical) dl->AddCircle(cursorScreen,r+3*scale,color,16,scale);
        }
        if(!points_.empty()) {
            ImVec2 a; if(project(vp,points_.back(),a)) {
                dl->AddLine(a,cursorScreen,color,1.5f*scale);
                const Point delta=cursor_-points_.back();
                // Lengths in the panel's display unit (mm, or in when US customary).
                const float unitMm=float(panel.lengthUnitMm());
                const char* lu=panel.lengthUnit();
                const int prec=unitMm>1.0f?2:1;
                char label[128]; std::snprintf(label,sizeof(label),"%.*f %s%s",prec,length(delta)/unitMm,lu,
                    snap_==Snap::Interior?" · Tee":snap_==Snap::Vertical?" · Normal lock":"");
                const float lineHeight=ImGui::GetTextLineHeight();
                dl->AddText({cursorScreen.x+12.0f*scale,cursorScreen.y-2*lineHeight-6*scale},ink(pal.sketchInk),label);
                std::snprintf(label,sizeof(label),"dx %+.*f  dy %+.*f  dz %+.*f %s",prec,delta.x/unitMm,
                              prec,delta.y/unitMm,prec,delta.z/unitMm,lu);
                dl->AddText({cursorScreen.x+12.0f*scale,cursorScreen.y-lineHeight-4*scale},ink(pal.sketchInk,0.8f),label);
            }
            if(snap_==Snap::Axis || snap_==Snap::Vertical) {
                const Point direction=axis(axis_);
                const float span=worldPerPixel(vp,points_.back())*200*scale;
                ImVec2 a,b;
                if(project(vp,points_.back()-direction*span,a) && project(vp,points_.back()+direction*span,b))
                    dashedLine(dl,a,b,ink(axisColour,0.65f),scale);
                const char* names[]={"Along X","Along Y","Along Z"};
                dl->AddText({cursorScreen.x+12*scale,cursorScreen.y+4*scale},ink(axisColour),names[axis_]);
            }
        }
        if(numeric_) {
            char label[96]; std::snprintf(label,sizeof(label),"Length: %s %s · Enter to place",number_.data(),panel.lengthUnit());
            const ImVec2 size=ImGui::CalcTextSize(label),p{cursorScreen.x+12.0f*scale,cursorScreen.y+ImGui::GetTextLineHeight()+8*scale};
            dl->AddRectFilled({p.x-6.0f*scale,p.y-4.0f*scale},{p.x+size.x+6.0f*scale,p.y+size.y+4.0f*scale},ink(pal.overlayBg),4.0f*scale);
            dl->AddText(p,ink(pal.overlayText),label);
        }
    }
    const char* action=pendingFinish_ ?
        "Saving previous edit… run will be added · Esc cancel" :
        !pendingFittingOps_.is_null() ?
        "Saving previous edit… fitting will be added · Esc cancel" :
        panel.tool()==PipingPanel::Tool::DrawRun ?
        "Click points · Enter/double-click finish · Backspace undo · Tab plane/Auto · Shift normal · Esc cancel" :
        "Click a pipe to insert a fitting · Esc to leave the tool";
    const char* planes[]={"XY","XZ","YZ"};
    char hint[320];
    std::snprintf(hint,sizeof(hint),"%s · %s | %s",planes[activePlane_],
                  panel.drawSettings().autoPlane?"Auto":"Locked",action);
    ImVec2 clearMin=vp.imgMin,clearMax=vp.imgMax;
    ui::viewportClearRect(clearMin,clearMax);
    const float available=std::max(clearMax.x-clearMin.x-24.0f*scale,1.0f);
    const float fontSize=std::min(ImGui::GetFontSize(),available/std::max(ImGui::CalcTextSize(hint).x,1.0f)*ImGui::GetFontSize());
    // The shared kit chip owns ImGui layout, whereas this hook owns only the supplied draw list;
    // its label is painted directly here so it cannot create an invisible input item over CAD.
    const float hintWidth=ImGui::CalcTextSize(hint).x*fontSize/ImGui::GetFontSize();
    const ImVec2 hintMin{(clearMin.x+clearMax.x-hintWidth)*0.5f,
                         clearMax.y-ImGui::GetTextLineHeight()-14.0f*scale};
    dl->AddText(ImGui::GetFont(),fontSize,hintMin,ink(pal.sketchInk),hint);
    ui::publishViewportHudRect(hintMin.x-6.0f*scale,hintMin.y-6.0f*scale,
                              hintMin.x+hintWidth+6.0f*scale,hintMin.y+fontSize+6.0f*scale);
    dl->PopClipRect();
}
bool SketchTool::handle(AppState& st,PipingPanel& panel,MeshCache& mesh,const simscene::ViewportCtx& vp,ImDrawList* dl) {
    const auto tool=panel.tool();
    if(tool!=previousTool_) {
        if(previousTool_!=PipingPanel::Tool::None) panel.drawSettings().autoPlane=true;
        reset(); previousTool_=tool;
    }
    const auto& io=ImGui::GetIO();
    bool consumed=false;
    if(ImGui::IsMouseClicked(ImGuiMouseButton_Left) && vp.hovered && !vp.navigating) {
        pressed_=true; dragged_=false; doubleClick_=ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left);
    }
    if(pressed_ && (vp.navigating || ImGui::IsMouseDragging(ImGuiMouseButton_Left))) dragged_=true;
    const bool released=ImGui::IsMouseReleased(ImGuiMouseButton_Left);
    const bool click=released && pressed_ && !dragged_ && vp.hovered && !vp.navigating && !swallowRelease_;
    if(released) { pressed_=false; swallowRelease_=false; }
    if(tool==PipingPanel::Tool::None) {
        const Hit fit=vp.hovered&&!vp.navigating?hitFitting(mesh,vp,io.MousePos,12.0f*ui::uiScale):Hit{};
        const Hit run=vp.hovered&&!vp.navigating?hitRun(mesh,vp,io.MousePos,10.0f*ui::uiScale,false,true):Hit{};
        mesh.hoverFitting=fit.fitting>=0?mesh.fittings[fit.fitting].id:std::string();
        mesh.hoverRun=fit.fitting<0 && run.run>=0?mesh.runs[run.run].id:std::string();
        if(click && fit.fitting>=0) { panel.selectFitting(st,mesh.fittings[fit.fitting].id); consumed=true; }
        else if(click && run.run>=0) { panel.selectRun(st,mesh.runs[run.run].id); consumed=true; }
        return consumed;
    }
    mesh.hoverRun.clear(); mesh.hoverFitting.clear();
    Hit fittingHit;
    haveCursor_=false;
    // XY -> XZ -> YZ -> Auto -> XY. Auto is separate from the explicit plane index:
    // from an explicit/default XY, one Tab still selects XZ (including pipedraw).
    // A live chain keeps the shortcut when a plane chip/panel has moved focus off the image.
    if(tool==PipingPanel::Tool::DrawRun && !pendingFinish_ && !io.WantTextInput &&
       !ImGui::IsAnyItemActive() && !ImGui::IsPopupOpen("",ImGuiPopupFlags_AnyPopupId) &&
       !vp.navigating && (vp.hovered || !points_.empty()) && ImGui::IsKeyPressed(ImGuiKey_Tab)) {
        auto& settings=panel.drawSettings();
        if(!settings.autoPlane && settings.plane==2) settings.autoPlane=true;
        else { settings.plane=(settings.plane+1)%3; settings.autoPlane=false; }
        explicitPlane_=settings.plane;
    }
    updatePlane(panel,vp);
    if(vp.hovered && !vp.navigating) {
        st.requestCursor(ui::CursorKind::Crosshair);
        if(ImGui::IsKeyPressed(ImGuiKey_Escape)) {
            const bool cancelRun=!points_.empty() || numeric_ || !pendingFittingOps_.is_null();
            clearPolyline(&panel);
            if(!cancelRun) panel.setTool(st,PipingPanel::Tool::None);
            pressed_=false; swallowRelease_=true;
        } else if(tool==PipingPanel::Tool::DrawRun && !pendingFinish_) {
            if(!io.WantTextInput && !numeric_ && ImGui::IsKeyPressed(ImGuiKey_Backspace) && !points_.empty()) {
                points_.pop_back(); updatePlane(panel,vp);
            }
            haveCursor_=cursorWorld(panel,mesh,vp,cursor_);
            if(!io.WantTextInput && !points_.empty() && haveCursor_ && (numeric_ || length(cursor_-points_.back())>1e-4f)) {
                for(int i=0;i<io.InputQueueCharacters.Size;++i) {
                    const ImWchar c=io.InputQueueCharacters[i];
                    if((c>='0' && c<='9') || (c=='.' && numeric_)) {
                        if(!numeric_) { numeric_=true; numberLength_=0; number_[0]='\0'; numericDirection_=unit(cursor_-points_.back()); }
                        if(numberLength_+1<number_.size()) { number_[numberLength_++]=char(c); number_[numberLength_]='\0'; }
                    }
                }
            }
            if(numeric_) {
                if(ImGui::IsKeyPressed(ImGuiKey_Backspace) && numberLength_>0) { number_[--numberLength_]='\0'; }
                char* end=nullptr; const float value=std::strtof(number_.data(),&end);
                const bool valid=end && end!=number_.data() && *end=='\0' && std::isfinite(value) && value>0;
                // The typed number is in the panel's display length unit (mm, or in when US).
                if(valid && !points_.empty()) { cursor_=points_.back()+numericDirection_*(value*float(panel.lengthUnitMm())); haveCursor_=true; snap_=Snap::None; }
                if(ImGui::IsKeyPressed(ImGuiKey_Enter) && valid) {
                    points_.push_back(cursor_); numeric_=false; numberLength_=0; number_[0]='\0';
                }
            } else {
                const bool finishKey=ImGui::IsKeyPressed(ImGuiKey_Enter) || ImGui::IsMouseClicked(ImGuiMouseButton_Right);
                if(finishKey) finish(st,panel);
                else if(click && haveCursor_) {
                    consumed=true;
                    if(!points_.empty() && length(cursor_-points_.back())<1e-4f) finish(st,panel);
                    else if(points_.size()>=2 && length(cursor_-points_.front())<1e-4f) {
                        points_.push_back(points_.front()); finish(st,panel);
                    } else {
                        if(points_.empty() || length(cursor_-points_.back())>1e-4f) points_.push_back(cursor_);
                        if(doubleClick_) finish(st,panel);
                    }
                }
            }
        } else if(tool==PipingPanel::Tool::InsertFitting && pendingFittingOps_.is_null()) {
            fittingHit=hitRun(mesh,vp,io.MousePos,12.0f*ui::uiScale);
            if(fittingHit.run>=0) fittingHit.point=gridInterior(panel.drawSettings(),mesh,fittingHit);
            if(fittingHit.run>=0) { cursor_=fittingHit.point; haveCursor_=true; snap_=Snap::Interior; }
            if(click) {
                consumed=true;
                if(fittingHit.run>=0) {
                    const auto& s=panel.drawSettings(); const Point p=fittingHit.point;
                    nlohmann::json op={{"op","add_fitting"},{"type",s.insertType},{"at",{p.x,p.y,p.z}},{"run_id",mesh.runs[fittingHit.run].id},{"params",s.insertParams}};
                    if(s.insertType=="reducer" && s.insertParams.contains("nps2")) {
                        op["nps2"]=s.insertParams["nps2"]; op["params"].erase("nps2");
                    }
                    auto ops=nlohmann::json::array({std::move(op)});
                    if(panel.busy() || panel.editPending() || !panel.submitEdit(st,ops,"Fitting inserted")) {
                        updatePreview(panel,mesh,&fittingHit,true);
                        pendingFittingOps_=std::move(ops);
                    }
                }
            }
        }
    }
    if(pendingFittingOps_.is_null())
        updatePreview(panel,mesh,&fittingHit,panel.tool()!=PipingPanel::Tool::None && (tool==PipingPanel::Tool::DrawRun ? !points_.empty() : haveCursor_));
    updatePlane(panel,vp);
    updateGrid(panel,vp,tool==PipingPanel::Tool::DrawRun && !pendingFinish_ &&
               (vp.hovered || !points_.empty()) && panel.tool()!=PipingPanel::Tool::None);
    if(panel.tool()!=PipingPanel::Tool::None) drawHud(panel,vp,dl);
    return consumed || (click && tool!=PipingPanel::Tool::None);
}
} // namespace ui::pipescene
