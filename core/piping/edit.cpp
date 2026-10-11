#define _USE_MATH_DEFINES
#include "piping/edit.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <cstddef>
#include <stdexcept>
#include <string>
#include <vector>

#include "piping/catalog.h"
#include "piping/route.h"
#include "piping/schema.h"

namespace chudware::piping {
namespace {
using pyutil::ojson;
using V = std::array<double, 3>;
constexpr double eps = 1e-6;
V sub(V a, V b) { return {a[0]-b[0], a[1]-b[1], a[2]-b[2]}; }
V add(V a, V b) { return {a[0]+b[0], a[1]+b[1], a[2]+b[2]}; }
V mul(V a, double t) { return {a[0]*t, a[1]*t, a[2]*t}; }
double dot(V a, V b) { return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
double norm(V a) { return std::sqrt(dot(a,a)); }
double distance(V a, V b) { return norm(sub(a,b)); }
V unit(V a) { const double n = norm(a); return n > eps ? mul(a,1/n) : V{}; }
struct Refusal { std::string reason, hint; };
[[noreturn]] void refuse(std::string r, std::string h) { throw Refusal{std::move(r),std::move(h)}; }
V vec(const ojson& p) {
    if (!p.is_array() || p.size()!=3) refuse("invalid_params", "point must be [x,y,z]");
    V v{};
    for (int i=0;i<3;++i) {
        if (!p[i].is_number() || !std::isfinite(p[i].get<double>()))
            refuse("invalid_params", "coordinates must be finite numbers");
        v[i]=p[i].get<double>();
    }
    return v;
}
ojson point(V v) { return ojson::array({v[0],v[1],v[2]}); }
ojson simplify(const ojson& path) {
    if (!path.is_array() || path.size()<2) refuse("degenerate_path", "need at least two points");
    std::vector<V> pts;
    for (const auto& p:path) {
        V v=vec(p);
        if (!pts.empty() && distance(pts.back(),v)<=eps)
            refuse("degenerate_path", "duplicate vertices make a zero-length segment");
        if (pts.size()>1 && dot(unit(sub(pts.back(),pts[pts.size()-2])),unit(sub(v,pts.back())))>1-1e-9)
            pts.back()=v;
        else pts.push_back(v);
    }
    if (pts.size()<2 || distance(pts.front(),pts.back())<=eps)
        refuse("degenerate_path", "run endpoints must be distinct");
    ojson out=ojson::array();
    for (V p:pts) out.push_back(point(p));
    return out;
}
struct Projection { double distance=std::numeric_limits<double>::infinity(); V point{}; size_t segment=0; };
Projection project(V p,const ojson& path) {
    Projection best;
    for (size_t i=0;i+1<path.size();++i) {
        const V a=vec(path[i]), ab=sub(vec(path[i+1]),a);
        const double len2=dot(ab,ab);
        if (len2<=eps*eps) continue;
        const V q=add(a,mul(ab,std::clamp(dot(sub(p,a),ab)/len2,0.0,1.0)));
        const double d=distance(p,q);
        if (d<best.distance) best={d,q,i};
    }
    return best;
}
void record(ojson& list,const ojson& id) {
    if (std::find(list.begin(),list.end(),id)==list.end()) list.push_back(id);
}
struct Editor {
    ojson doc, created=ojson::array(), removed=ojson::array(), changed=ojson::array();
    double tol;
    size_t find(const char* list,const std::string& id) const {
        const auto& items=doc[list];
        for(size_t i=0;i<items.size();++i) if(items[i]["id"]==id) return i;
        refuse(std::string(list)=="runs" ? "unknown_run" : "unknown_fitting", "unknown id "+id);
    }
    void erase(const char* list,size_t i) {
        record(removed,doc[list][i]["id"]);
        doc[list].erase(doc[list].begin()+static_cast<std::ptrdiff_t>(i));
    }
    std::string fitting(const std::string& type,V at,const std::string& owner,const ojson& params) {
        const auto& run=doc["runs"][find("runs",owner)];
        const std::string id=next_id(doc,"ft");
        auto ft=new_fitting(id,type,run["nps"],point(at),run.value("schedule",std::string("40")),type,params);
        ft["run_id"]=owner;
        ft["material"]=run.value("material",ojson(nullptr));
        doc["fittings"].push_back(std::move(ft)); record(created,id);
        return id;
    }
    // Refuse a params key the fitting type does not define instead of silently storing it.
    // Null (delete) is always allowed; so is re-sending an already stored value unchanged,
    // which keeps whole-params round trips of older documents editable.
    static void check_param_keys(const std::string& type,const ojson& params,const ojson& current) {
        const auto keys=fitting_param_keys(type);
        std::string unknown;
        for(auto it=params.begin();it!=params.end();++it) {
            if(keys.count(it.key()) || it.value().is_null() ||
               (current.is_object() && current.contains(it.key()) && current[it.key()]==it.value())) continue;
            unknown+=(unknown.empty() ? "'" : ", '")+it.key()+"'";
        }
        if(unknown.empty()) return;
        std::string accepted;
        for(const auto& k:keys) accepted+=(accepted.empty() ? "" : ", ")+k;
        std::string hint=type+" fitting has no parameter "+unknown+"; accepted keys: "+accepted;
        if(type=="tee") hint+=". Per-path constant K: k_model_run / k_model_branch = {\"kind\":\"constant\",\"K\":<K>}";
        else hint+=". A fixed loss coefficient is k_model = {\"kind\":\"constant\",\"K\":<K>}";
        refuse("unknown_param",hint);
    }
    std::string radius(const ojson& run) const {
        const auto spec=nearest_pipe(run["nps"],run.value("schedule",std::string("40")));
        const double od=spec.value("outer_dia",60.3);
        const auto r=run.value("bend_radius",ojson(nullptr));
        return r.is_number() && r.get<double>()<1.25*od ? "SR" : "LR";
    }
    void spec(ojson& run,const ojson& op) {
        for(const char* key:{"nps","schedule","material","end_type","name","suppressed"})
            if(op.contains(key)) run[key]=op[key];
        const auto pipe=nearest_pipe(run["nps"],run.value("schedule",std::string("40")));
        if(!pipe.value("ok",false)) refuse("invalid_params","unsupported NPS/schedule");
        if(op.contains("bend_radius")) {
            const auto r=op["bend_radius"];
            if(r!="LR" && r!="SR") refuse("invalid_params","bend_radius must be LR or SR");
            const auto dims=fitting_dims("elbow",run["nps"].get<double>());
            const double lr=dims.value("center_to_end",1.5*pipe["outer_dia"].get<double>());
            run["bend_radius"]=(r=="SR" ? lr*2/3 : lr);
        }
        const auto errors=validate_run(run);
        if(!errors.empty()) refuse("invalid_params",errors.front());
        if(run.contains("suppressed") && !run["suppressed"].is_boolean())
            refuse("invalid_params","suppressed must be boolean");
    }
    std::string split(const std::string& id,V p) {
        const size_t ri=find("runs",id);
        auto original=doc["runs"][ri];
        const auto proj=project(p,original["path"]);
        if(proj.distance>tol) refuse("not_on_run","split point is not on the run");
        p=proj.point;
        if(distance(p,vec(original["path"].front()))<=eps || distance(p,vec(original["path"].back()))<=eps)
            refuse("degenerate_path","split point must be in the run interior");
        ojson near=ojson::array(),far=ojson::array();
        for(size_t i=0;i<=proj.segment;++i) near.push_back(original["path"][i]);
        if(distance(vec(near.back()),p)>eps) near.push_back(point(p));
        far.push_back(point(p));
        for(size_t i=proj.segment+1;i<original["path"].size();++i)
            if(distance(vec(far.back()),vec(original["path"][i]))>eps) far.push_back(original["path"][i]);
        if(near.size()<2 || far.size()<2) refuse("degenerate_path","split would create an empty run");
        const std::string new_id=next_id(doc,"r");
        original["id"]=new_id; original["path"]=far;
        doc["runs"][ri]["path"]=near; doc["runs"].push_back(original);
        record(changed,id); record(created,new_id);
        for(auto& ft:doc["fittings"]) {
            if(ft.value("run_id",ojson(nullptr))==id &&
               project(vec(ft["origin"]),far).distance<=eps &&
               distance(vec(ft["origin"]),p)>eps) {
                ft["run_id"]=new_id; record(changed,ft["id"]);
            }
            auto& params=ft["params"];
            if(ft["type"]=="tee" && distance(vec(ft["origin"]),p)>eps &&
               project(vec(ft["origin"]),far).distance<=eps) {
                if(params.value("branch_run",ojson(nullptr))==id) {
                    params["branch_run"]=new_id; record(changed,ft["id"]);
                }
                if(params.value("run_ids",ojson(nullptr)).is_array())
                    for(auto& r:params["run_ids"]) if(r==id) {
                        r=new_id; record(changed,ft["id"]);
                    }
            }
        }
        return new_id;
    }
    struct End { std::string id; V direction; bool start; };
    std::vector<End> ends(V p) const {
        std::vector<End> result;
        for(const auto& run:doc["runs"]) {
            const auto& path=run["path"];
            if(distance(p,vec(path.front()))<=eps)
                result.push_back({run["id"].get<std::string>(),unit(sub(vec(path[1]),p)),true});
            if(distance(p,vec(path.back()))<=eps)
                result.push_back({run["id"].get<std::string>(),unit(sub(vec(path[path.size()-2]),p)),false});
        }
        return result;
    }
    void junction(V p) {
        const auto connected=ends(p);
        if(connected.size()>3) refuse("node_full","a pipe node supports at most three runs");
        size_t existing=doc["fittings"].size();
        for(size_t i=0;i<doc["fittings"].size();++i) {
            const auto& ft=doc["fittings"][i];
            if((ft["type"]=="tee" || ft["type"]=="elbow") && distance(vec(ft["origin"]),p)<=eps) {
                existing=i; break;
            }
        }
        std::string type,owner; ojson params=ojson::object();
        if(connected.size()==3) {
            size_t a=0,b=1; double best=dot(connected[0].direction,connected[1].direction);
            for(size_t i=0;i<3;++i) for(size_t j=i+1;j<3;++j) {
                const double d=dot(connected[i].direction,connected[j].direction);
                if(d<best) {best=d;a=i;b=j;}
            }
            if(connected[a].start && !connected[b].start) std::swap(a,b);
            const size_t branch=3-a-b;
            type="tee"; owner=connected[branch].id;
            params={{"run_ids",ojson::array({connected[a].id,connected[b].id})},
                    {"branch_run",owner}};
        } else if(connected.size()==2) {
            const double angle=std::acos(std::clamp(-dot(connected[0].direction,connected[1].direction),-1.0,1.0))*180/M_PI;
            if(angle>1e-5 && angle<180-1e-5) {
                type="elbow"; owner=connected[1].id;
                params={{"angle",angle},{"radius",radius(doc["runs"][find("runs",owner)])}};
            }
        }
        if(type.empty()) {
            if(existing<doc["fittings"].size()) erase("fittings",existing);
            return;
        }
        if(existing==doc["fittings"].size()) {
            fitting(type,p,owner,params);
            existing=doc["fittings"].size()-1;
        }
        else {
            auto& ft=doc["fittings"][existing];
            if(ft["type"]==type) for(auto it=params.begin();it!=params.end();++it) ft["params"][it.key()]=it.value();
            else {ft["type"]=type;ft["params"]=params;}
            ft["run_id"]=owner; record(changed,ft["id"]);
        }
        if(type=="elbow") {
            const auto orient=elbow_orient(mul(connected[0].direction,-1),connected[1].direction);
            doc["fittings"][existing]["orient"]=ojson::array({orient[0],orient[1],orient[2]});
        }
    }
    void corners(const std::string& id) {
        const auto run=doc["runs"][find("runs",id)];
        const auto& path=run["path"];
        for(size_t i=1;i+1<path.size();++i) {
            const V a=unit(sub(vec(path[i]),vec(path[i-1]))), b=unit(sub(vec(path[i+1]),vec(path[i])));
            const double angle=std::acos(std::clamp(dot(a,b),-1.0,1.0))*180/M_PI;
            size_t fi=doc["fittings"].size();
            for(size_t j=0;j<doc["fittings"].size();++j)
                if(doc["fittings"][j]["type"]=="elbow" &&
                   distance(vec(doc["fittings"][j]["origin"]),vec(path[i]))<=eps) {fi=j;break;}
            if(angle<=1e-5) {if(fi<doc["fittings"].size()) erase("fittings",fi); continue;}
            if(angle>=180-1e-5) refuse("degenerate_path","a run cannot reverse at a vertex");
            if(fi==doc["fittings"].size()) {
                fitting("elbow",vec(path[i]),id,{{"angle",angle},{"radius",radius(run)}});
                fi=doc["fittings"].size()-1;
            } else {
                doc["fittings"][fi]["params"]["angle"]=angle;
                doc["fittings"][fi]["run_id"]=id; record(changed,doc["fittings"][fi]["id"]);
            }
            const auto orient=elbow_orient(a,b);
            doc["fittings"][fi]["orient"]=ojson::array({orient[0],orient[1],orient[2]});
        }
    }
    void merge_at(V p) {
        const auto c=ends(p);
        if(c.size()!=2 || dot(c[0].direction,c[1].direction)>-1+1e-9) {junction(p);return;}
        const auto r1=doc["runs"][find("runs",c[0].id)],r2=doc["runs"][find("runs",c[1].id)];
        for(const char* k:{"nps","schedule","material","bend_radius","end_type","bore_mm","suppressed"})
            if(r1.value(k,ojson(nullptr))!=r2.value(k,ojson(nullptr))) {junction(p);return;}
        auto left=r1["path"],right=r2["path"];
        if(c[0].start) std::reverse(left.begin(),left.end());
        if(!c[1].start) std::reverse(right.begin(),right.end());
        for(size_t i=1;i<right.size();++i) left.push_back(right[i]);
        doc["runs"][find("runs",c[0].id)]["path"]=simplify(left);
        for(auto& ft:doc["fittings"]) {
            if(ft.value("run_id",ojson(nullptr))==c[1].id) {
                ft["run_id"]=c[0].id; record(changed,ft["id"]);
            }
            auto& params=ft["params"];
            if(params.value("branch_run",ojson(nullptr))==c[1].id) {
                params["branch_run"]=c[0].id; record(changed,ft["id"]);
            }
            if(params.value("run_ids",ojson(nullptr)).is_array())
                for(auto& r:params["run_ids"]) if(r==c[1].id) {
                    r=c[0].id; record(changed,ft["id"]);
                }
        }
        record(changed,c[0].id); erase("runs",find("runs",c[1].id));
        for(size_t i=doc["fittings"].size();i-->0;)
            if((doc["fittings"][i]["type"]=="tee" || doc["fittings"][i]["type"]=="elbow") &&
                distance(vec(doc["fittings"][i]["origin"]),p)<=eps) erase("fittings",i);
    }
    void apply(const ojson& op) {
        if(!op.is_object() || !op.value("op",ojson(nullptr)).is_string())
            refuse("invalid_params","each operation requires op");
        if(op.contains("name") && !op["name"].is_string())
            refuse("invalid_params","name must be a string");
        if(op.contains("params") && !op["params"].is_object())
            refuse("invalid_params","params must be an object");
        const std::string kind=op["op"].get<std::string>();
        if(kind=="add_run") {
            auto path=simplify(op.value("path",ojson(nullptr)));
            auto base=doc["runs"].empty() ? new_pipe_run("r1",path,2,"40") : doc["runs"].front();
            base["path"]=path; base["id"]=next_id(doc,"r"); base["name"]=op.value("name",std::string("Run"));
            base["suppressed"]=false; base["bore_mm"]=nullptr;
            if(base.value("material",ojson(nullptr)).is_null()) base["material"]=doc.value("material",ojson("A106-B"));
            if(!op.contains("bend_radius")) {
                auto defaults=op; defaults["bend_radius"]=radius(base); spec(base,defaults);
            } else spec(base,op);
            for(size_t end:{size_t(0),path.size()-1}) {
                Projection nearest; std::string target;
                for(const auto& run:doc["runs"]) {
                    const auto q=project(vec(path[end]),run["path"]);
                    if(q.distance<nearest.distance) {nearest=q;target=run["id"].get<std::string>();}
                }
                if(!target.empty() && nearest.distance<=tol) {
                    path[end]=point(nearest.point);
                    const auto target_path=doc["runs"][find("runs",target)]["path"];
                    if(distance(nearest.point,vec(target_path.front()))>eps &&
                       distance(nearest.point,vec(target_path.back()))>eps) split(target,nearest.point);
                }
            }
            const std::string id=next_id(doc,"r"); base["id"]=id;
            base["path"]=simplify(path); doc["runs"].push_back(base); record(created,id);
            corners(id); junction(vec(base["path"].front())); junction(vec(base["path"].back()));
        } else if(kind=="split_run") {
            const auto id=op.at("run_id").get<std::string>();
            const auto far=split(id,vec(op.at("point")));
            junction(vec(doc["runs"][find("runs",far)]["path"].front()));
        } else if(kind=="add_fitting") {
            const std::string type=op.at("type").get<std::string>();
            if(type!="valve" && type!="orifice" && type!="custom" && type!="reducer" && type!="flange")
                refuse("invalid_params","add_fitting supports inline valve, orifice, custom, reducer, flange");
            check_param_keys(type,op.value("params",ojson::object()),ojson::object());
            const V at=vec(op.at("at")); Projection nearest; std::string id;
            if(op.contains("run_id")) {
                id=op["run_id"].get<std::string>(); nearest=project(at,doc["runs"][find("runs",id)]["path"]);
            } else for(const auto& run:doc["runs"]) {
                const auto q=project(at,run["path"]);
                if(q.distance<nearest.distance) {nearest=q;id=run["id"].get<std::string>();}
            }
            if(id.empty()) refuse("not_on_run","add a run before placing a fitting");
            const auto run=doc["runs"][find("runs",id)];
            const auto pipe=nearest_pipe(run["nps"],run.value("schedule",std::string("40")));
            if(nearest.distance>std::max(tol,pipe.value("outer_dia",tol))) refuse("not_on_run","fitting is too far from the run");
            if(type=="reducer") {
                if(!op.contains("nps2")) refuse("invalid_params","reducer requires nps2");
                const auto far=split(id,nearest.point);
                auto& downstream=doc["runs"][find("runs",far)];
                spec(downstream,{{"nps",op["nps2"]}});
                record(changed,far);
            }
            const auto fid=fitting(type,nearest.point,id,op.value("params",ojson::object()));
            auto& ft=doc["fittings"][find("fittings",fid)];
            for(const char* key:{"nps","nps2","name"}) if(op.contains(key)) ft[key]=op[key];
        } else if(kind=="update_fitting") {
            auto& ft=doc["fittings"][find("fittings",op.at("id").get<std::string>())];
            const auto previous_nps2=ft.value("nps2",ojson(nullptr));
            if(op.contains("params")) {
                if(!op["params"].is_object()) refuse("invalid_params","params must be an object");
                check_param_keys(ft["type"].get<std::string>(),op["params"],ft.value("params",ojson::object()));
                for(auto it=op["params"].begin();it!=op["params"].end();++it)
                    if(it.value().is_null()) ft["params"].erase(it.key()); else ft["params"][it.key()]=it.value();
            }
            if(ft["type"]=="elbow" && op.contains("params") && op["params"].contains("radius")) {
                const auto owner=ft.value("run_id",ojson(nullptr));
                if(owner.is_string()) {
                    auto& run=doc["runs"][find("runs",owner.get<std::string>())];
                    const auto radius_value=ft["params"].value("radius",ojson("LR"));
                    spec(run,{{"bend_radius",radius_value}});
                    record(changed,run["id"]);
                }
            }
            if(ft["type"]=="reducer" &&
               (op.contains("nps2") || (op.contains("params") && op["params"].contains("nps2")))) {
                const auto size=op.contains("nps2") ? op["nps2"] : op["params"]["nps2"];
                const auto catalog=nearest_pipe(size,ft.value("schedule",std::string("40")));
                if(!catalog.value("ok",false)) refuse("invalid_params","reducer outlet requires a catalog NPS");
                ft["nps2"]=size;
                ft["params"].erase("nps2");
                const auto owner=ft.value("run_id",ojson(nullptr));
                const V at=vec(ft["origin"]);
                for(auto& run:doc["runs"]) {
                    if(run["id"]==owner || run["nps"]!=previous_nps2) continue;
                    if(distance(vec(run["path"].front()),at)>eps &&
                       distance(vec(run["path"].back()),at)>eps) continue;
                    spec(run,{{"nps",size}});
                    record(changed,run["id"]);
                    for(auto& attached:doc["fittings"])
                        if(attached.value("run_id",ojson(nullptr))==run["id"] &&
                           attached["id"]!=ft["id"]) {
                            attached["nps"]=size; record(changed,attached["id"]);
                        }
                }
            }
            if(op.contains("name")) ft["name"]=op["name"];
            record(changed,ft["id"]);
        } else if(kind=="remove_fitting") erase("fittings",find("fittings",op.at("id").get<std::string>()));
        else if(kind=="remove_run") {
            const std::string id=op.at("id").get<std::string>();
            const auto run=doc["runs"][find("runs",id)];
            std::vector<V> junctions;
            for(const auto& ft:doc["fittings"]) if(ft["type"]=="tee" &&
                (distance(vec(ft["origin"]),vec(run["path"].front()))<=eps ||
                 distance(vec(ft["origin"]),vec(run["path"].back()))<=eps)) junctions.push_back(vec(ft["origin"]));
            for(size_t i=doc["fittings"].size();i-->0;) {
                const auto& ft=doc["fittings"][i];
                if(ft["type"]=="tee") continue;
                if(ft.value("run_id",ojson(nullptr))==id ||
                   (ft.value("run_id",ojson(nullptr)).is_null() && project(vec(ft["origin"]),run["path"]).distance<=eps))
                    erase("fittings",i);
            }
            erase("runs",find("runs",id));
            for(V p:junctions) merge_at(p);
            junction(vec(run["path"].front())); junction(vec(run["path"].back()));
        } else if(kind=="move_vertex") {
            const std::string id=op.at("run_id").get<std::string>(); const size_t ri=find("runs",id);
            if(!op.at("index").is_number_integer() || op["index"].get<long long>()<0 ||
                op["index"].get<size_t>()>=doc["runs"][ri]["path"].size()) refuse("invalid_params","vertex index out of range");
            const size_t index=op["index"].get<size_t>(); const V old=vec(doc["runs"][ri]["path"][index]),p=vec(op.at("point"));
            const bool endpoint=index==0 || index+1==doc["runs"][ri]["path"].size();
            std::vector<std::string> moved;
            for(auto& run:doc["runs"]) {
                bool touched=false;
                if(run["id"]==id) {run["path"][index]=point(p);touched=true;}
                else if(endpoint) {
                    if(distance(vec(run["path"].front()),old)<=tol) {run["path"].front()=point(p);touched=true;}
                    if(distance(vec(run["path"].back()),old)<=tol) {run["path"].back()=point(p);touched=true;}
                }
                if(touched) {record(changed,run["id"]);moved.push_back(run["id"].get<std::string>());}
            }
            for(auto& ft:doc["fittings"]) if(distance(vec(ft["origin"]),old)<=eps) {
                ft["origin"]=point(p); record(changed,ft["id"]);
            }
            for(const auto& rid:moved) {corners(rid); const auto& path=doc["runs"][find("runs",rid)]["path"];junction(vec(path.front()));junction(vec(path.back()));}
        } else if(kind=="set_run") {
            const std::string id=op.at("id").get<std::string>(); spec(doc["runs"][find("runs",id)],op); record(changed,id);
            for(auto& ft:doc["fittings"]) if(ft.value("run_id",ojson(nullptr))==id) {
                const auto& run=doc["runs"][find("runs",id)]; ft["nps"]=run["nps"];ft["schedule"]=run["schedule"];ft["material"]=run["material"];
                if(ft["type"]=="elbow") ft["params"]["radius"]=radius(run);
                record(changed,ft["id"]);
            }
        } else refuse("invalid_params","unknown edit operation "+kind);
        for(const auto& ft:doc["fittings"]) {
            const auto p=ft.value("params",ojson::object());
            if(ft["type"]=="custom" && !p.contains("k_model")) refuse("invalid_k_model","custom fitting requires k_model");
            for(const char* key:{"k_model","k_model_run","k_model_branch"})
                if(p.contains(key)) {
                    const auto errors=validate_k_model(p[key],key);
                    if(!errors.empty()) refuse("invalid_k_model",errors.front());
                }
            if(ft["type"]=="orifice" && p.value("beta",ojson(nullptr)).is_null()) {
                const auto pipe=nearest_pipe(ft["nps"],ft.value("schedule",std::string("40")));
                if(p.value("bore_mm",ojson(nullptr)).is_number() && p["bore_mm"].get<double>()>=pipe.value("inner_dia",0.0))
                    refuse("invalid_params","orifice bore must be smaller than pipe ID");
            }
        }
        const auto errors=validate_piping(doc);
        if(!errors.empty()) refuse("invalid_params",errors.front());
    }
};
}  // namespace

pyutil::ojson piping_edit(const pyutil::ojson& params) {
    size_t index=0;
    auto failure=[&](const std::string& reason,const std::string& hint) {
        return ojson{{"ok",false},{"reason",reason},{"hint",hint},{"op_index",index}};
    };
    try {
        if(!params.is_object() || !params.value("ops",ojson(nullptr)).is_array())
            return failure("invalid_params","ops must be an array");
        const auto tolerance=params.value("tol_mm",ojson(1.0));
        if(!tolerance.is_number() || !std::isfinite(tolerance.get<double>()) || tolerance.get<double>()<0)
            return failure("invalid_params","tol_mm must be finite and nonnegative");
        auto doc=params.value("piping",ojson(nullptr)); if(doc.is_null()) doc=new_piping();
        const auto errors=validate_piping(doc);
        if(!errors.empty()) return failure("invalid_params",errors.front());
        Editor editor{std::move(doc),ojson::array(),ojson::array(),ojson::array(),tolerance.get<double>()};
        for(const auto& op:params["ops"]) {editor.apply(op);++index;}
        return {{"ok",true},{"piping",std::move(editor.doc)},{"created",std::move(editor.created)},
                {"removed",std::move(editor.removed)},{"changed",std::move(editor.changed)},{"warnings",ojson::array()}};
    } catch(const Refusal& e) {return failure(e.reason,e.hint);}
      catch(const nlohmann::json::exception&) {return failure("invalid_params","operation has missing fields or incorrect types");}
}
}  // namespace chudware::piping
