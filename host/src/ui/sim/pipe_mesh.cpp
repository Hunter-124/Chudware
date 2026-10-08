#include "pipe_mesh.h"
#include "../piping_panel.h"
#include "../../ui_theme.h"
#include "../../result_ramp.h"
#include <algorithm>
#include <array>
#include <limits>
#include <unordered_map>

namespace ui::pipescene {
namespace {
constexpr float pi=3.14159265358979323846f;
constexpr int sides=16;
using json=nlohmann::json;
const json& array(const json& j,const char* key) {
    static const json empty=json::array();
    auto it=j.find(key); return it!=j.end() && it->is_array() ? *it : empty;
}
float number(const json& j,const char* key,float fallback=0) {
    auto it=j.find(key); return it!=j.end() && it->is_number() ? it->get<float>() : fallback;
}
std::string text(const json& j,const char* key,const char* fallback="") {
    auto it=j.find(key); return it!=j.end() && it->is_string() ? it->get<std::string>() : fallback;
}
bool point(const json& j,Point& p) {
    if (!j.is_array() || j.size()!=3) return false;
    for (const auto& x:j) if (!x.is_number()) return false;
    p={j[0].get<float>(),j[1].get<float>(),j[2].get<float>()};
    return std::isfinite(p.x)&&std::isfinite(p.y)&&std::isfinite(p.z);
}
void put(std::vector<float>& v,Point p) { v.insert(v.end(),{p.x,p.y,p.z}); }
void triangle(Soup& m,Point a,Point b,Point c,Point na,Point nb,Point nc) {
    put(m.xyz,a); put(m.xyz,b); put(m.xyz,c);
    put(m.nrm,na); put(m.nrm,nb); put(m.nrm,nc);
}
Point perpendicular(Point axis) {
    return unit(cross(axis,std::fabs(axis.z)<0.9f ? Point{0,0,1} : Point{0,1,0}));
}
void frustum(Soup& m,Point a,Point b,float ra,float rb,bool caps=true) {
    if (length(b-a)<1e-5f) return;
    const Point axis=unit(b-a),u=perpendicular(axis),v=cross(axis,u);
    const float slope=(ra-rb)/length(b-a);
    for (int i=0;i<sides;++i) {
        const float t=2*pi*i/sides,s=2*pi*(i+1)/sides;
        const Point r=u*std::cos(t)+v*std::sin(t),q=u*std::cos(s)+v*std::sin(s);
        const Point na=unit(r+axis*slope),nb=unit(q+axis*slope);
        triangle(m,a+r*ra,a+q*ra,b+q*rb,na,nb,nb);
        triangle(m,a+r*ra,b+q*rb,b+r*rb,na,nb,na);
        if (caps) {
            triangle(m,a,a+q*ra,a+r*ra,axis*(-1),axis*(-1),axis*(-1));
            triangle(m,b,b+r*rb,b+q*rb,axis,axis,axis);
        }
    }
}
void cylinder(Soup& m,Point o,Point axis,float halfLength,float radius) {
    frustum(m,o-axis*halfLength,o+axis*halfLength,radius,radius);
}
void ring(Soup& m,Point o,Point axis,float major,float minor) {
    const Point u=perpendicular(axis),v=cross(axis,u);
    for (int i=0;i<sides;++i) for (int j=0;j<sides;++j) {
        Point p[4],n[4];
        const int is[4]={i,i+1,i+1,i},js[4]={j,j,j+1,j+1};
        for(int k=0;k<4;++k) {
            const float a=2*pi*is[k]/sides,b=2*pi*js[k]/sides;
            const Point radial=u*std::cos(a)+v*std::sin(a);
            n[k]=radial*std::cos(b)+axis*std::sin(b);
            p[k]=o+radial*major+n[k]*minor;
        }
        triangle(m,p[0],p[1],p[2],n[0],n[1],n[2]);
        triangle(m,p[0],p[2],p[3],n[0],n[2],n[3]);
    }
}
void plate(Soup& m,Point o,Point axis,float outer,float inner,float half) {
    const Point u=perpendicular(axis),v=cross(axis,u);
    for(int i=0;i<sides;++i) {
        const Point r=u*std::cos(2*pi*i/sides)+v*std::sin(2*pi*i/sides);
        const Point q=u*std::cos(2*pi*(i+1)/sides)+v*std::sin(2*pi*(i+1)/sides);
        for(int sign:{-1,1}) {
            Point c=o+axis*(half*sign),n=axis*float(sign);
            Point p[4]={c+r*inner,c+r*outer,c+q*outer,c+q*inner};
            if(sign>0) { triangle(m,p[0],p[1],p[2],n,n,n); triangle(m,p[0],p[2],p[3],n,n,n); }
            else { triangle(m,p[0],p[2],p[1],n,n,n); triangle(m,p[0],p[3],p[2],n,n,n); }
        }
        triangle(m,o-axis*half+r*outer,o-axis*half+q*outer,o+axis*half+q*outer,r,q,q);
        triangle(m,o-axis*half+r*outer,o+axis*half+q*outer,o+axis*half+r*outer,r,q,r);
    }
}
void roundedBlock(Soup& m,Point o,Point axis,Point up,float d) {
    // Rounded-square cross-section, swept along the pipe: the extra radial harmonics flatten
    // the sides without adding an unlit box or a second topology representation.
    const Point u=unit(up-axis*dot(up,axis)),v=cross(axis,u);
    for(int i=0;i<sides;++i) {
        const float a=2*pi*i/sides,b=2*pi*(i+1)/sides;
        auto radial=[&](float t) { return (u*std::cos(t)+v*std::sin(t))*(d*(0.74f-0.08f*std::cos(4*t))); };
        const Point r=radial(a),q=radial(b),na=unit(r),nb=unit(q);
        const Point x=o-axis*(d*0.6f),y=o+axis*(d*0.6f);
        triangle(m,x+r,x+q,y+q,na,nb,nb); triangle(m,x+r,y+q,y+r,na,nb,na);
        triangle(m,x,x+q,x+r,axis*(-1),axis*(-1),axis*(-1));
        triangle(m,y,y+r,y+q,axis,axis,axis);
    }
}
float segmentDistance(Point p,Point a,Point b,Point& nearest) {
    const Point d=b-a; const float dd=dot(d,d);
    nearest=a+d*(dd>1e-10f ? std::clamp(dot(p-a,d)/dd,0.0f,1.0f) : 0);
    return length(p-nearest);
}
}

float outerDiameter(double nps) {
    // ASME B36.10M OD (mm); schedule changes the bore, never this outside diameter.
    static constexpr double sizes[]={0.125,0.25,0.375,0.5,0.75,1,1.25,1.5,2,2.5,3,3.5,4,5,6,8,10,12,14,16,18,20,22,24,26,28,30,32,34,36,38,40,42,44,46,48};
    static constexpr float od[]={10.3f,13.7f,17.1f,21.3f,26.7f,33.4f,42.2f,48.3f,60.3f,73.0f,88.9f,101.6f,114.3f,141.3f,168.3f,219.1f,273.0f,323.9f,355.6f,406.4f,457.2f,508.0f,558.8f,609.6f,660.4f,711.2f,762.0f,812.8f,863.6f,914.4f,965.2f,1016.0f,1066.8f,1117.6f,1168.4f,1219.2f};
    std::size_t best=0;
    for(std::size_t i=1;i<std::size(sizes);++i) if(std::fabs(sizes[i]-nps)<std::fabs(sizes[best]-nps)) best=i;
    return od[best];
}
void appendRun(Soup& mesh,const std::vector<Point>& path,float od,float bendRadius,std::vector<Point>* rounded) {
    if(path.size()<2) return;
    std::vector<Point> local;
    auto& pts=rounded ? *rounded : local;
    pts.clear(); pts.reserve(path.size()*9); pts.push_back(path.front());
    auto desiredTrim=[&](std::size_t i) {
        if(i==0 || i+1>=path.size()) return 0.0f;
        const float c=std::clamp(dot(unit(path[i]-path[i-1]),unit(path[i+1]-path[i])),-1.0f,1.0f);
        return c>-0.999999f ? bendRadius*std::sqrt(std::max(0.0f,(1-c)/(1+c))) : 0.0f;
    };
    for(std::size_t i=1;i+1<path.size();++i) {
        const Point in=unit(path[i]-path[i-1]),out=unit(path[i+1]-path[i]);
        const float cosine=std::clamp(dot(in,out),-1.0f,1.0f),angle=std::acos(cosine);
        if(angle<1e-4f || angle>pi-1e-3f) { pts.push_back(path[i]); continue; }
        const float desired=desiredTrim(i);
        const float left=length(path[i]-path[i-1]),right=length(path[i+1]-path[i]);
        const float leftSum=desired+desiredTrim(i-1),rightSum=desired+desiredTrim(i+1);
        const float trim=std::min({desired,leftSum>left?left*0.98f*desired/leftSum:desired,
                                          rightSum>right?right*0.98f*desired/rightSum:desired});
        const float radius=trim/std::tan(angle*0.5f);
        const Point normal=unit(cross(in,out)),side=unit(cross(normal,in));
        const Point a=path[i]-in*trim,center=a+side*radius,r0=a-center;
        const int steps=std::max(2,int(std::ceil(angle/(pi/16))));
        for(int s=0;s<=steps;++s) {
            const float theta=angle*s/steps;
            pts.push_back(center+r0*std::cos(theta)+cross(normal,r0)*std::sin(theta));
        }
    }
    pts.push_back(path.back());
    // Parallel-transport the ring basis; no twist seams between straights and torus bends.
    std::array<Point,sides> previous{},previousNormals{};
    Point u=perpendicular(unit(pts[1]-pts[0]));
    for(std::size_t k=0;k<pts.size();++k) {
        const Point tangent=unit(k==0 ? pts[1]-pts[0] : k+1==pts.size() ? pts[k]-pts[k-1] : unit(pts[k]-pts[k-1])+unit(pts[k+1]-pts[k]));
        u=unit(u-tangent*dot(u,tangent)); const Point v=cross(tangent,u);
        std::array<Point,sides> current{},normals{};
        for(int j=0;j<sides;++j) { normals[j]=u*std::cos(2*pi*j/sides)+v*std::sin(2*pi*j/sides); current[j]=pts[k]+normals[j]*(od*0.5f); }
        for(int j=0;j<sides;++j) {
            const int q=(j+1)%sides;
            if(k>0) {
                triangle(mesh,previous[j],previous[q],current[q],previousNormals[j],previousNormals[q],normals[q]);
                triangle(mesh,previous[j],current[q],current[j],previousNormals[j],normals[q],normals[j]);
            }
            if(k==0) triangle(mesh,pts[k],current[q],current[j],tangent*(-1),tangent*(-1),tangent*(-1));
            if(k+1==pts.size()) triangle(mesh,pts[k],current[j],current[q],tangent,tangent,tangent);
        }
        previous=current; previousNormals=normals;
    }
}
void appendFitting(Soup& mesh,const FittingGeometry& f) {
    const float d=f.od;
    const Point o=f.origin,a=unit(f.axis),up=unit(f.branch-a*dot(f.branch,a));
    if(f.type=="elbow") {
        if(f.bendCovered) return;
        const float angle=number(f.params,"angle",90)*pi/180;
        const float radius=d*(text(f.params,"radius","LR")=="SR"?1:1.5f);
        const Point n=unit(cross(a,up)),side=unit(cross(n,a));
        const Point start=o-a*(radius*std::tan(angle*0.5f)),center=start+side*radius;
        const Point radial=(start-center)*(1/radius);
        std::array<Point,sides> prev{},prevN{};
        const int steps=std::max(2,int(std::ceil(angle/(pi/16))));
        for(int k=0;k<=steps;++k) {
            const float t=angle*k/steps;
            const Point r=radial*std::cos(t)+cross(n,radial)*std::sin(t),c=center+r*radius;
            std::array<Point,sides> cur{},curN{};
            for(int j=0;j<sides;++j) {
                const float s=2*pi*j/sides;
                curN[j]=r*std::cos(s)-n*std::sin(s); cur[j]=c+curN[j]*(d*0.5f);
            }
            if(k>0) for(int j=0;j<sides;++j) {
                const int q=(j+1)%sides;
                triangle(mesh,prev[j],prev[q],cur[q],prevN[j],prevN[q],curN[q]);
                triangle(mesh,prev[j],cur[q],cur[j],prevN[j],curN[q],curN[j]);
            }
            prev=cur; prevN=curN;
        }
    } else if(f.type=="tee") {
        cylinder(mesh,o,a,d*0.8f,d*0.54f);
        cylinder(mesh,o+up*(d*0.45f),up,d*0.45f,f.od2*0.5f);
        cylinder(mesh,o+up*(d*0.82f),up,d*0.08f,f.od2*0.65f);
    } else if(f.type=="reducer") {
        frustum(mesh,o-a*(d*0.65f),o+a*(d*0.65f),d*0.53f,f.od2*0.53f);
    } else if(f.type=="flange") {
        plate(mesh,o,a,d*0.95f,d*0.43f,d*0.12f);
        cylinder(mesh,o,a,d*0.3f,d*0.57f);
    } else if(f.type=="orifice") {
        plate(mesh,o,a,d*0.83f,d*0.35f,d*0.045f);
        cylinder(mesh,o+up*(d*0.86f),up,d*0.18f,d*0.12f);
    } else if(f.type=="custom") {
        roundedBlock(mesh,o,a,up,d); plate(mesh,o,a,d*0.92f,d*0.5f,d*0.08f);
    } else if(f.type=="valve") {
        const std::string type=text(f.params,"valve_type","gate");
        const bool quarter=type=="ball" || type=="plug" || type=="butterfly";
        const bool check=type=="check_swing" || type=="check_lift";
        if(type=="butterfly") cylinder(mesh,o,a,d*0.2f,d*0.78f);
        else if(type=="globe" || type=="ball") {
            frustum(mesh,o-a*(d*0.65f),o,d*0.53f,d*0.82f);
            frustum(mesh,o,o+a*(d*0.65f),d*0.82f,d*0.53f);
        } else roundedBlock(mesh,o,a,up,d*0.9f);
        if(type=="angle") cylinder(mesh,o-up*(d*0.5f),up,d*0.5f,d*0.5f);
        for(float s:{-1.0f,1.0f}) plate(mesh,o+a*(d*0.65f*s),a,d*0.72f,d*0.43f,d*0.08f);
        const float bonnetHeight=type=="check_swing"?0.8f:type=="check_lift"?1.3f:1.2f;
        frustum(mesh,o+up*(d*0.45f),o+up*(d*bonnetHeight),d*0.36f,d*0.22f);
        if(!check) {
            cylinder(mesh,o+up*(d*1.4f),up,d*0.25f,d*0.075f);
            const Point wheel=o+up*(d*1.67f);
            if(quarter) cylinder(mesh,wheel+a*(d*0.4f),a,d*0.5f,d*0.075f);
            else {
                ring(mesh,wheel,up,d*0.52f,d*0.06f);
                cylinder(mesh,wheel,a,d*0.52f,d*0.035f);
                cylinder(mesh,wheel,cross(up,a),d*0.52f,d*0.035f);
            }
        }
    }
}

void MeshCache::update(PipingPanel& panel) {
    bool geometryChanged=owner!=&panel || networkRevision!=panel.networkRevision();
    if(geometryChanged) {
        owner=&panel; networkRevision=panel.networkRevision(); runs.clear(); fittings.clear(); mesh.clear(); centerline.clear();
        hoverRun.clear(); hoverFitting.clear();
        for(const auto& r:array(panel.network(),"runs")) {
            if(r.value("suppressed",false)) continue;
            RunGeometry g; g.id=text(r,"id"); g.od=outerDiameter(number(r,"nps",2));
            g.bendRadius=number(r,"bend_radius",g.od*(text(r,"bend_radius")=="SR"?1:1.5f));
            for(const auto& p:array(r,"path")) { Point v; if(point(p,v) && (g.path.empty() || length(v-g.path.back())>1e-5f)) g.path.push_back(v); }
            if(g.path.size()<2) continue;
            g.first=mesh.xyz.size(); appendRun(mesh,g.path,g.od,g.bendRadius,&g.rounded); g.end=mesh.xyz.size();
            for(std::size_t k=1;k<g.rounded.size();++k) { put(centerline,g.rounded[k-1]); put(centerline,g.rounded[k]); }
            runs.push_back(std::move(g));
        }
        for(const auto& f:array(panel.network(),"fittings")) {
            if(f.value("suppressed",false)) continue;
            FittingGeometry g; g.id=text(f,"id"); g.type=text(f,"type"); g.runId=text(f,"run_id");
            auto o=f.find("origin"); if(o==f.end() || !point(*o,g.origin)) continue;
            g.od=outerDiameter(number(f,"nps",2)); g.od2=outerDiameter(number(f,"nps2",number(f,"nps",2)));
            g.params=f.value("params",json::object());
            if(g.type=="tee" && g.params.contains("branch_nps"))
                g.od2=outerDiameter(number(g.params,"branch_nps",number(f,"nps",2)));
            const RunGeometry* attached=nullptr; float best=std::numeric_limits<float>::max();
            for(const auto& r:runs) {
                if(!g.runId.empty() && r.id!=g.runId) continue;
                for(std::size_t k=1;k<r.path.size();++k) {
                    Point q; const float dist=segmentDistance(g.origin,r.path[k-1],r.path[k],q);
                    if(dist<best) { best=dist; g.axis=unit(r.path[k]-r.path[k-1]); attached=&r; }
                }
            }
            if(!attached && !g.runId.empty()) continue; // suppressed/deleted parent
            if(attached) { g.runId=attached->id; g.od=attached->od; }
            g.branch=std::fabs(g.axis.z)<0.9f ? Point{0,0,1} : Point{0,1,0};
            if(g.type=="elbow" && attached) {
                for(std::size_t k=1;k+1<attached->path.size();++k)
                    if(length(attached->path[k]-g.origin)<1e-3f) g.bendCovered=true;
                if(!g.bendCovered) {
                    Point incident[2]; int count=0;
                    for(const auto& r:runs) {
                        if(count==2) break;
                        if(length(r.path.front()-g.origin)<1e-3f) incident[count++]=unit(r.path[1]-r.path.front());
                        else if(length(r.path.back()-g.origin)<1e-3f) incident[count++]=unit(r.path[r.path.size()-2]-r.path.back());
                    }
                    if(count==2) { g.axis=incident[0]*(-1); g.branch=incident[1]; }
                }
            }
            if(g.type=="tee") {
                const std::string branch=text(g.params,"branch_run");
                for(const auto& r:runs) if(r.id==branch && r.path.size()>1) {
                    g.branch=length(r.path.front()-g.origin)<length(r.path.back()-g.origin) ? unit(r.path[1]-r.path.front()) : unit(r.path[r.path.size()-2]-r.path.back());
                    g.od2=r.od;
                }
            }
            g.first=mesh.xyz.size(); appendFitting(mesh,g); g.end=mesh.xyz.size(); fittings.push_back(std::move(g));
        }
        rgb.resize(mesh.xyz.size()); pressureFraction.resize(mesh.xyz.size()/3);
        auto fractions=[&](std::size_t first,std::size_t end,const RunGeometry& run) {
            for(std::size_t i=first;i<end;i+=3) {
                const Point p{mesh.xyz[i],mesh.xyz[i+1],mesh.xyz[i+2]};
                float dist=std::numeric_limits<float>::max(),travel=0,bestTravel=0;
                for(std::size_t k=1;k<run.rounded.size();++k) {
                    Point q; const float d=segmentDistance(p,run.rounded[k-1],run.rounded[k],q);
                    if(d<dist) { dist=d; bestTravel=travel+length(q-run.rounded[k-1]); }
                    travel+=length(run.rounded[k]-run.rounded[k-1]);
                }
                pressureFraction[i/3]=travel>0?bestTravel/travel:0;
            }
        };
        for(const auto& r:runs) fractions(r.first,r.end,r);
        for(const auto& f:fittings)
            for(const auto& r:runs) if(r.id==f.runId) { fractions(f.first,f.end,r); break; }
        ++revision; ++lineRevision;
    }
    const auto& links=array(panel.solveResult(),"links"); const auto& nodes=array(panel.solveResult(),"nodes");
    const void* linkIdentity=links.get_ptr<const json::array_t*>();
    const void* nodeIdentity=nodes.get_ptr<const json::array_t*>();
    const bool solveChanged=geometryChanged || solveLinks!=linkIdentity || solveNodes!=nodeIdentity;
    if(solveChanged) {
        solveLinks=linkIdentity; solveNodes=nodeIdentity;
        std::unordered_map<std::string,float> pressure;
        for(const auto& n:nodes) pressure.emplace(text(n,"id"),number(n,"pressure_kpa"));
        for(auto& r:runs) {
            r.solved=false;
            for(const auto& l:links) if(text(l,"id")==r.id) {
                r.velocity=std::fabs(number(l,"velocity_ms"));
                auto a=pressure.find(text(l,"from")),b=pressure.find(text(l,"to"));
                r.pressure0=a!=pressure.end()?a->second:0; r.pressure1=b!=pressure.end()?b->second:0;
                r.solved=true; break;
            }
        }
    }
    const auto& pal=ui::palette;
    const float* roles[]={pal.partDefault,pal.selectionHighlight,pal.hoverHighlight};
    bool paletteChanged=false;
    for(int r=0;r<3;++r) for(int c=0;c<3;++c) if(paletteKey[r*3+c]!=roles[r][c]) { paletteKey[r*3+c]=roles[r][c]; paletteChanged=true; }
    const std::string sr=panel.selectedRunId(); const auto& sf=panel.selectedFittingId();
    // Hover changes are tracked against the last colours, without re-tessellating the network.
    bool colorsChanged=geometryChanged || solveChanged || paletteChanged || colorBy!=panel.colorBy() || selectedRun!=sr || selectedFitting!=sf || lastHoverRun!=hoverRun || lastHoverFitting!=hoverFitting;
    if(!colorsChanged) return;
    colorBy=panel.colorBy(); selectedRun=sr; selectedFitting=sf; lastHoverRun=hoverRun; lastHoverFitting=hoverFitting;
    float low=std::numeric_limits<float>::max(),high=-low;
    for(const auto& r:runs) if(r.solved) {
        if(colorBy==1) { low=std::min({low,r.pressure0,r.pressure1}); high=std::max({high,r.pressure0,r.pressure1}); }
        else { low=std::min(low,r.velocity); high=std::max(high,r.velocity); }
    }
    auto colour=[&](std::size_t first,std::size_t end,const RunGeometry* run,const float* highlight) {
        float c[3];
        for(std::size_t i=first;i<end;i+=3) {
            std::copy_n(highlight?highlight:pal.partDefault,3,c);
            if(!highlight && colorBy>0 && run && run->solved) {
                float value=run->velocity;
                if(colorBy==1)
                    value=run->pressure0+(run->pressure1-run->pressure0)*pressureFraction[i/3];
                result_ramp::sample(high>low ? (value-low)/(high-low) : 0.5f,c);
            }
            std::copy_n(c,3,rgb.begin()+i);
        }
    };
    for(const auto& r:runs) colour(r.first,r.end,&r,r.id==selectedRun ? pal.selectionHighlight : r.id==hoverRun ? pal.hoverHighlight : nullptr);
    for(const auto& f:fittings) {
        const RunGeometry* r=nullptr; for(const auto& g:runs) if(g.id==f.runId) {r=&g; break;}
        colour(f.first,f.end,r,f.id==selectedFitting?pal.selectionHighlight:f.id==hoverFitting?pal.hoverHighlight:nullptr);
        if(f.bendCovered && r && (f.id==selectedFitting || f.id==hoverFitting)) {
            const float* c=f.id==selectedFitting?pal.selectionHighlight:pal.hoverHighlight;
            for(std::size_t i=r->first;i<r->end;i+=3) {
                const Point p{mesh.xyz[i],mesh.xyz[i+1],mesh.xyz[i+2]};
                if(length(p-f.origin)<r->bendRadius+r->od) std::copy_n(c,3,rgb.begin()+i);
            }
        }
    }
    ++revision;
}
void MeshCache::collect(simscene::Scene& out) const {
    if(!mesh.xyz.empty()) {
        simscene::LitBatch b; b.key=0x1000; b.xyz=&mesh.xyz; b.nrm=&mesh.nrm; b.rgb=&rgb; b.revision=revision;
        std::copy_n(ui::palette.partDefault,4,b.rgba); b.rgba[3]=1; out.lit.push_back(b);
    }
    if(!centerline.empty()) {
        simscene::LineBatch b; b.key=0x1001; b.xyz=&centerline; b.width=1; b.revision=lineRevision;
        std::copy_n(ui::palette.sketchDimLine,4,b.rgba); b.rgba[3]*=0.35f; out.lines.push_back(b);
    }
}
Hit hitRun(const MeshCache& mesh,const simscene::ViewportCtx& vp,ImVec2 mouse,float threshold,bool vertices,bool rounded) {
    Hit hit; hit.distance=threshold;
    float ro[3],rd[3]; simscene::screenRay(vp,mouse,ro,rd);
    const Point origin{ro[0],ro[1],ro[2]},ray{rd[0],rd[1],rd[2]};
    for(int r=0;r<int(mesh.runs.size());++r) {
        const auto& path=rounded?mesh.runs[r].rounded:mesh.runs[r].path;
        for(std::size_t k=0;k<path.size();++k) {
            ImVec2 a; if(!project(vp,path[k],a)) continue;
            if(vertices) {
                const float d=std::hypot(mouse.x-a.x,mouse.y-a.y);
                if(d<hit.distance) { hit.run=r; hit.point=path[k]; hit.distance=d; }
            } else if(k+1<path.size()) {
                ImVec2 b; if(!project(vp,path[k+1],b)) continue;
                const float dx=b.x-a.x,dy=b.y-a.y,den=dx*dx+dy*dy;
                const float t=den>1e-7f?std::clamp(((mouse.x-a.x)*dx+(mouse.y-a.y)*dy)/den,0.0f,1.0f):0;
                const float d=std::hypot(mouse.x-(a.x+dx*t),mouse.y-(a.y+dy*t));
                if(d<hit.distance) {
                    // Closest ray/segment point, rather than linear screen interpolation (which
                    // is wrong in perspective). The screen metric is only the picking tolerance.
                    const Point delta=path[k+1]-path[k],w=path[k]-origin;
                    const float aa=dot(delta,delta),bb=dot(delta,ray),dd=dot(delta,w),ee=dot(ray,w),det=aa-bb*bb;
                    const float along=det>1e-8f?std::clamp((bb*ee-dd)/det,0.0f,1.0f):t;
                    hit.run=r; hit.point=path[k]+delta*along; hit.distance=d;
                }
            }
        }
    }
    return hit;
}
Hit hitFitting(const MeshCache& mesh,const simscene::ViewportCtx& vp,ImVec2 mouse,float threshold) {
    Hit hit; hit.distance=threshold;
    for(int f=0;f<int(mesh.fittings.size());++f) {
        ImVec2 s; if(!project(vp,mesh.fittings[f].origin,s)) continue;
        const float d=std::hypot(mouse.x-s.x,mouse.y-s.y);
        if(d<hit.distance) { hit.fitting=f; hit.point=mesh.fittings[f].origin; hit.distance=d; }
    }
    return hit;
}
} // namespace ui::pipescene
