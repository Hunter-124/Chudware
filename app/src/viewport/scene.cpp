#include "viewport/scene.h"
#include <glad/gl.h>
#include "src/render/aicm_loader.h"   // reused pure-CPU AICM parser (-I ${HOST})
#include <cmath>
#include <cstdio>
#include <cstring>
#include <algorithm>
#include <vector>

namespace viewport {
namespace {

// ---- tiny column-major mat4 / vec3 math (OpenGL convention) ----
struct M4 { float m[16]; };
static M4 identity(){ M4 r{}; r.m[0]=r.m[5]=r.m[10]=r.m[15]=1; return r; }
static M4 mul(const M4&a,const M4&b){ M4 r{};
    for(int c=0;c<4;c++)for(int row=0;row<4;row++){ float s=0; for(int k=0;k<4;k++) s+=a.m[k*4+row]*b.m[c*4+k]; r.m[c*4+row]=s; } return r; }
static void cross(const float*a,const float*b,float*o){ o[0]=a[1]*b[2]-a[2]*b[1]; o[1]=a[2]*b[0]-a[0]*b[2]; o[2]=a[0]*b[1]-a[1]*b[0]; }
static float dot3(const float*a,const float*b){ return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
static void norm3(float*v){ float l=std::sqrt(dot3(v,v)); if(l>1e-9f){ v[0]/=l; v[1]/=l; v[2]/=l; } }
static M4 perspective(float fovyDeg,float aspect,float n,float f){
    M4 r{}; float t=std::tan(fovyDeg*0.5f*3.14159265f/180.f);
    r.m[0]=1.f/(aspect*t); r.m[5]=1.f/t; r.m[10]=-(f+n)/(f-n); r.m[11]=-1.f; r.m[14]=-(2*f*n)/(f-n); return r; }
static M4 lookAt(const float*eye,const float*c,const float*up){
    float f[3]={c[0]-eye[0],c[1]-eye[1],c[2]-eye[2]}; norm3(f);
    float s[3]; cross(f,up,s); norm3(s);
    float u[3]; cross(s,f,u);
    M4 r=identity();
    r.m[0]=s[0]; r.m[4]=s[1]; r.m[8]=s[2];
    r.m[1]=u[0]; r.m[5]=u[1]; r.m[9]=u[2];
    r.m[2]=-f[0];r.m[6]=-f[1];r.m[10]=-f[2];
    r.m[12]=-dot3(s,eye); r.m[13]=-dot3(u,eye); r.m[14]=dot3(f,eye);
    return r; }

static unsigned int compile(unsigned int type,const char*src,const char* tag){
    unsigned int s=glCreateShader(type); glShaderSource(s,1,&src,nullptr); glCompileShader(s);
    int ok=0; glGetShaderiv(s,GL_COMPILE_STATUS,&ok);
    if(!ok){ char log[1024]; glGetShaderInfoLog(s,1024,nullptr,log); std::fprintf(stderr,"[scene] %s shader: %s\n",tag,log); }
    return s; }
static unsigned int link(const char*vs,const char*fs,const char* tag){
    unsigned int v=compile(GL_VERTEX_SHADER,vs,tag), f=compile(GL_FRAGMENT_SHADER,fs,tag);
    unsigned int p=glCreateProgram(); glAttachShader(p,v); glAttachShader(p,f); glLinkProgram(p);
    int ok=0; glGetProgramiv(p,GL_LINK_STATUS,&ok);
    if(!ok){ char log[1024]; glGetProgramInfoLog(p,1024,nullptr,log); std::fprintf(stderr,"[scene] %s link: %s\n",tag,log); }
    glDeleteShader(v); glDeleteShader(f); return p; }

const char* kMeshVS = R"(#version 330 core
layout(location=0) in vec3 aPos; layout(location=1) in vec3 aNrm;
uniform mat4 uMVP; uniform mat4 uModel; uniform mat3 uNormal;
out vec3 vN; out vec3 vW;
void main(){ vW=(uModel*vec4(aPos,1.0)).xyz; vN=normalize(uNormal*aNrm); gl_Position=uMVP*vec4(aPos,1.0); })";

const char* kMeshFS = R"(#version 330 core
in vec3 vN; in vec3 vW; out vec4 frag;
uniform vec3 uColor; uniform vec3 uCam; uniform float uRough; uniform float uMetal;
void main(){
  vec3 N=normalize(vN); if(!gl_FrontFacing) N=-N;
  vec3 V=normalize(uCam-vW);
  vec3 L1=normalize(vec3(0.35,0.45,0.82));
  vec3 L2=normalize(vec3(-0.7,-0.35,0.45));
  float k1=max(dot(N,L1),0.0);
  float k2=max(dot(N,L2),0.0)*0.45;
  float hemi=0.5+0.5*N.z;
  vec3 amb=mix(vec3(0.16,0.18,0.22), vec3(0.34,0.37,0.42), hemi);
  vec3 base=uColor;
  vec3 diff=base*(amb + k1*vec3(1.0,0.97,0.92) + k2*vec3(0.45,0.55,0.75));
  vec3 H=normalize(L1+V);
  float sh=mix(14.0,96.0,1.0-uRough);
  float spec=pow(max(dot(N,H),0.0),sh)*(0.25+0.75*(1.0-uRough));
  float fres=pow(1.0-max(dot(N,V),0.0),3.0)*0.4;
  vec3 col=diff + spec*mix(vec3(1.0),base,uMetal) + fres*vec3(0.45,0.55,0.78);
  col=pow(col, vec3(0.9));
  frag=vec4(col,1.0);
})";

const char* kEdgeVS = R"(#version 330 core
layout(location=0) in vec3 aPos; uniform mat4 uMVP;
void main(){ gl_Position=uMVP*vec4(aPos,1.0); })";
const char* kEdgeFS = R"(#version 330 core
out vec4 frag; uniform vec3 uColor; uniform float uA;
void main(){ frag=vec4(uColor,uA); })";

const char* kBgVS = R"(#version 330 core
out vec2 vUv; const vec2 P[3]=vec2[3](vec2(-1,-1),vec2(3,-1),vec2(-1,3));
void main(){ vec2 p=P[gl_VertexID]; vUv=p*0.5+0.5; gl_Position=vec4(p,0.999,1.0); })";
const char* kBgFS = R"(#version 330 core
in vec2 vUv; out vec4 frag; uniform vec3 uTop; uniform vec3 uBot;
void main(){ frag=vec4(mix(uBot,uTop,vUv.y),1.0); })";

} // namespace

bool Scene::init() {
    prog_   = link(kMeshVS,kMeshFS,"mesh");
    edgeProg_= link(kEdgeVS,kEdgeFS,"edge");
    idProg_ = link(kEdgeVS,kEdgeFS,"id");   // flat color = exact pick id (no AA/blend)
    bgProg_ = link(kBgVS,kBgFS,"bg");
    glGenVertexArrays(1,&bgVao_);
    buildFbo();
    return prog_!=0;
}

void Scene::shutdown(){ releaseBodies(); releaseFbo();
    if(prog_) glDeleteProgram(prog_); if(edgeProg_) glDeleteProgram(edgeProg_);
    if(idProg_) glDeleteProgram(idProg_);
    if(bgProg_) glDeleteProgram(bgProg_); if(bgVao_) glDeleteVertexArrays(1,&bgVao_); }

void Scene::releaseFbo(){
    if(msColor_) glDeleteRenderbuffers(1,&msColor_); if(msDepth_) glDeleteRenderbuffers(1,&msDepth_);
    if(msFbo_) glDeleteFramebuffers(1,&msFbo_);
    if(resolveTex_) glDeleteTextures(1,&resolveTex_); if(resolveFbo_) glDeleteFramebuffers(1,&resolveFbo_);
    if(idTex_) glDeleteTextures(1,&idTex_); if(idDepth_) glDeleteRenderbuffers(1,&idDepth_); if(idFbo_) glDeleteFramebuffers(1,&idFbo_);
    msColor_=msDepth_=msFbo_=resolveTex_=resolveFbo_=idTex_=idDepth_=idFbo_=0;
}

void Scene::buildFbo(){
    releaseFbo();
    int samples = samples_;
    glGenFramebuffers(1,&msFbo_); glBindFramebuffer(GL_FRAMEBUFFER,msFbo_);
    glGenRenderbuffers(1,&msColor_); glBindRenderbuffer(GL_RENDERBUFFER,msColor_);
    glRenderbufferStorageMultisample(GL_RENDERBUFFER,samples,GL_RGBA8,w_,h_);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_RENDERBUFFER,msColor_);
    glGenRenderbuffers(1,&msDepth_); glBindRenderbuffer(GL_RENDERBUFFER,msDepth_);
    glRenderbufferStorageMultisample(GL_RENDERBUFFER,samples,GL_DEPTH24_STENCIL8,w_,h_);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER,GL_DEPTH_STENCIL_ATTACHMENT,GL_RENDERBUFFER,msDepth_);
    if(glCheckFramebufferStatus(GL_FRAMEBUFFER)!=GL_FRAMEBUFFER_COMPLETE){
        // fall back to no MSAA
        samples_=1; if(samples!=1){ glBindFramebuffer(GL_FRAMEBUFFER,0); buildFbo(); return; }
    }
    glGenFramebuffers(1,&resolveFbo_); glBindFramebuffer(GL_FRAMEBUFFER,resolveFbo_);
    glGenTextures(1,&resolveTex_); glBindTexture(GL_TEXTURE_2D,resolveTex_);
    glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA8,w_,h_,0,GL_RGBA,GL_UNSIGNED_BYTE,nullptr);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,resolveTex_,0);

    // id-pass FBO (single-sample, exact colors for picking)
    glGenFramebuffers(1,&idFbo_); glBindFramebuffer(GL_FRAMEBUFFER,idFbo_);
    glGenTextures(1,&idTex_); glBindTexture(GL_TEXTURE_2D,idTex_);
    glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA8,w_,h_,0,GL_RGBA,GL_UNSIGNED_BYTE,nullptr);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_NEAREST);
    glFramebufferTexture2D(GL_FRAMEBUFFER,GL_COLOR_ATTACHMENT0,GL_TEXTURE_2D,idTex_,0);
    glGenRenderbuffers(1,&idDepth_); glBindRenderbuffer(GL_RENDERBUFFER,idDepth_);
    glRenderbufferStorage(GL_RENDERBUFFER,GL_DEPTH_COMPONENT24,w_,h_);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER,GL_DEPTH_ATTACHMENT,GL_RENDERBUFFER,idDepth_);

    glBindFramebuffer(GL_FRAMEBUFFER,0);
}

void Scene::resize(int w,int h){
    if(w<16)w=16; if(h<16)h=16;
    if(w==w_&&h==h_) return; w_=w; h_=h; buildFbo();
}

void Scene::releaseBodies(){
    for(auto&b:bodies_){
        if(b.vbo)glDeleteBuffers(1,&b.vbo); if(b.ebo)glDeleteBuffers(1,&b.ebo); if(b.vao)glDeleteVertexArrays(1,&b.vao);
        if(b.evbo)glDeleteBuffers(1,&b.evbo); if(b.evao)glDeleteVertexArrays(1,&b.evao);
    }
    bodies_.clear();
}
void Scene::clearMesh(){ releaseBodies(); }

bool Scene::loadAicm(const std::string& path){
    if(path.empty()) return false;
    render::DocMesh dm; std::string err;
    if(!render::loadAicmFile(path,dm,err)){ lastError=err; return false; }
    releaseBodies();
    pickMap_.clear(); edgeMap_.clear(); selected_.clear(); hasHover_=false;
    float lo[3]={1e30f,1e30f,1e30f}, hi[3]={-1e30f,-1e30f,-1e30f};
    for(const auto& bm: dm.bodies){
        if(bm.n_tris==0) continue;
        GBody g; g.color[0]=bm.color[0]; g.color[1]=bm.color[1]; g.color[2]=bm.color[2];
        g.metallic=bm.metallic; g.roughness=bm.roughness;
        // interleave pos+nrm
        std::vector<float> inter; inter.reserve(bm.n_verts*6);
        for(uint32_t i=0;i<bm.n_verts;i++){
            float x=bm.pos[i*3],y=bm.pos[i*3+1],z=bm.pos[i*3+2];
            inter.push_back(x);inter.push_back(y);inter.push_back(z);
            inter.push_back(bm.nrm[i*3]);inter.push_back(bm.nrm[i*3+1]);inter.push_back(bm.nrm[i*3+2]);
            if(x<lo[0])lo[0]=x; if(y<lo[1])lo[1]=y; if(z<lo[2])lo[2]=z;
            if(x>hi[0])hi[0]=x; if(y>hi[1])hi[1]=y; if(z>hi[2])hi[2]=z;
        }
        glGenVertexArrays(1,&g.vao); glBindVertexArray(g.vao);
        glGenBuffers(1,&g.vbo); glBindBuffer(GL_ARRAY_BUFFER,g.vbo);
        glBufferData(GL_ARRAY_BUFFER,inter.size()*sizeof(float),inter.data(),GL_STATIC_DRAW);
        glGenBuffers(1,&g.ebo); glBindBuffer(GL_ELEMENT_ARRAY_BUFFER,g.ebo);
        glBufferData(GL_ELEMENT_ARRAY_BUFFER,bm.idx.size()*sizeof(uint32_t),bm.idx.data(),GL_STATIC_DRAW);
        glEnableVertexAttribArray(0); glVertexAttribPointer(0,3,GL_FLOAT,GL_FALSE,6*sizeof(float),(void*)0);
        glEnableVertexAttribArray(1); glVertexAttribPointer(1,3,GL_FLOAT,GL_FALSE,6*sizeof(float),(void*)(3*sizeof(float)));
        g.nIdx=(int)bm.idx.size();
        // picking metadata: one pick id per face range (render order == pickMap_ order)
        g.body_id = bm.body_id;
        for(const auto& fr : bm.faces){
            g.faces.push_back(FacePart{fr.face_id, fr.first_tri, fr.tri_count});
            pickMap_.push_back(PickRef{bm.body_id, fr.face_id, 0});
        }
        // edges: expand each segment polyline into GL_LINES pairs; record per-edge vertex ranges
        std::vector<float> el;
        for(const auto& seg: bm.edges){
            int vtxStart=(int)(el.size()/3);
            for(uint32_t k=0;k+1<seg.pt_count;k++){
                uint32_t a=seg.first_pt+k, b=seg.first_pt+k+1;
                if((b)*3+2 >= bm.edge_pts.size()) break;
                el.push_back(bm.edge_pts[a*3]);el.push_back(bm.edge_pts[a*3+1]);el.push_back(bm.edge_pts[a*3+2]);
                el.push_back(bm.edge_pts[b*3]);el.push_back(bm.edge_pts[b*3+1]);el.push_back(bm.edge_pts[b*3+2]);
            }
            int vtxCount=(int)(el.size()/3)-vtxStart;
            if(vtxCount>0){
                g.edgeParts.push_back(EdgePart{seg.edge_id, vtxStart, vtxCount});
                edgeMap_.push_back(PickRef{bm.body_id, seg.edge_id, 1});
            }
        }
        if(!el.empty()){
            glGenVertexArrays(1,&g.evao); glBindVertexArray(g.evao);
            glGenBuffers(1,&g.evbo); glBindBuffer(GL_ARRAY_BUFFER,g.evbo);
            glBufferData(GL_ARRAY_BUFFER,el.size()*sizeof(float),el.data(),GL_STATIC_DRAW);
            glEnableVertexAttribArray(0); glVertexAttribPointer(0,3,GL_FLOAT,GL_FALSE,3*sizeof(float),(void*)0);
            g.nEdgeVerts=(int)(el.size()/3);
        }
        glBindVertexArray(0);
        bodies_.push_back(g);
    }
    if(lo[0]<=hi[0]){
        for(int i=0;i<3;i++) center_[i]=(lo[i]+hi[i])*0.5f;
        float dx=hi[0]-lo[0],dy=hi[1]-lo[1],dz=hi[2]-lo[2];
        radius_=0.5f*std::sqrt(dx*dx+dy*dy+dz*dz); if(radius_<1e-3f)radius_=1.f;
    }
    fitView();
    return !bodies_.empty();
}

void Scene::fitView(){
    target_[0]=center_[0]; target_[1]=center_[1]; target_[2]=center_[2];
    dist_ = radius_ / std::sin(fov_*0.5f*3.14159265f/180.f) * 1.25f;
}
void Scene::homeView(){ az_=-0.9f; el_=0.5f; fitView(); }
void Scene::orbit(float dAz,float dEl){ az_+=dAz; el_+=dEl;
    const float lim=1.5533f; if(el_>lim)el_=lim; if(el_<-lim)el_=-lim; }
void Scene::zoom(float steps){ dist_*=std::pow(0.9f,steps); float mn=radius_*0.05f,mx=radius_*60.f; if(dist_<mn)dist_=mn; if(dist_>mx)dist_=mx; }
void Scene::pan(float dxPx,float dyPx){
    float scale=dist_*std::tan(fov_*0.5f*3.14159265f/180.f)/(0.5f*h_);
    // camera right/up in world
    float dir[3]={std::cos(el_)*std::cos(az_),std::cos(el_)*std::sin(az_),std::sin(el_)};
    float up0[3]={0,0,1}; float right[3]; cross(dir,up0,right); norm3(right);
    float up[3]; cross(right,dir,up); norm3(up);
    for(int i=0;i<3;i++) target_[i]+= (-dxPx*scale)*right[i] + (dyPx*scale)*up[i];
}

void Scene::render(){
    glBindFramebuffer(GL_FRAMEBUFFER,msFbo_);
    glViewport(0,0,w_,h_);
    glDisable(GL_BLEND);
    // background gradient (no depth)
    glDisable(GL_DEPTH_TEST); glDepthMask(GL_FALSE);
    glUseProgram(bgProg_); glBindVertexArray(bgVao_);
    glUniform3f(glGetUniformLocation(bgProg_,"uTop"),0.17f,0.20f,0.25f);
    glUniform3f(glGetUniformLocation(bgProg_,"uBot"),0.07f,0.09f,0.12f);
    glDrawArrays(GL_TRIANGLES,0,3);
    glDepthMask(GL_TRUE); glEnable(GL_DEPTH_TEST); glClear(GL_DEPTH_BUFFER_BIT);

    // camera
    float dir[3]={std::cos(el_)*std::cos(az_),std::cos(el_)*std::sin(az_),std::sin(el_)};
    float eye[3]={target_[0]+dir[0]*dist_,target_[1]+dir[1]*dist_,target_[2]+dir[2]*dist_};
    float up[3]={0,0,1};
    float aspect=(float)w_/(float)(h_>0?h_:1);
    M4 proj=perspective(fov_,aspect,radius_*0.02f+0.01f,dist_+radius_*8.f);
    M4 view=lookAt(eye,target_,up);
    M4 model=identity();
    M4 mvp=mul(proj,view);

    if(!bodies_.empty()){
        bool wire = (style_==2);
        if(wire) glPolygonMode(GL_FRONT_AND_BACK,GL_LINE);
        glEnable(GL_CULL_FACE); glCullFace(GL_BACK);
        glUseProgram(prog_);
        glUniformMatrix4fv(glGetUniformLocation(prog_,"uMVP"),1,GL_FALSE,mvp.m);
        glUniformMatrix4fv(glGetUniformLocation(prog_,"uModel"),1,GL_FALSE,model.m);
        float nrm[9]={1,0,0,0,1,0,0,0,1};
        glUniformMatrix3fv(glGetUniformLocation(prog_,"uNormal"),1,GL_FALSE,nrm);
        glUniform3fv(glGetUniformLocation(prog_,"uCam"),1,eye);
        for(const auto&b:bodies_){
            glUniform3fv(glGetUniformLocation(prog_,"uColor"),1,b.color);
            glUniform1f(glGetUniformLocation(prog_,"uRough"),b.roughness);
            glUniform1f(glGetUniformLocation(prog_,"uMetal"),b.metallic);
            glBindVertexArray(b.vao);
            glDrawElements(GL_TRIANGLES,b.nIdx,GL_UNSIGNED_INT,0);
        }
        if(wire) glPolygonMode(GL_FRONT_AND_BACK,GL_FILL);
        glDisable(GL_CULL_FACE);

        // edges
        if(style_!=2){
            glEnable(GL_POLYGON_OFFSET_LINE);
            glUseProgram(edgeProg_);
            glUniformMatrix4fv(glGetUniformLocation(edgeProg_,"uMVP"),1,GL_FALSE,mvp.m);
            glUniform3f(glGetUniformLocation(edgeProg_,"uColor"),0.06f,0.08f,0.11f);
            glUniform1f(glGetUniformLocation(edgeProg_,"uA"),1.0f);
            glLineWidth(1.2f);
            glEnable(GL_DEPTH_TEST); glDepthFunc(GL_LEQUAL);
            for(const auto&b:bodies_) if(b.evao && (style_==1)){
                glBindVertexArray(b.evao); glDrawArrays(GL_LINES,0,b.nEdgeVerts);
            }
            glDepthFunc(GL_LESS);
        }

        // selection + hover face highlight (translucent overlay)
        if(!selected_.empty() || hasHover_){
            glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA,GL_ONE_MINUS_SRC_ALPHA);
            glEnable(GL_POLYGON_OFFSET_FILL); glPolygonOffset(-1.f,-1.f);
            glDepthFunc(GL_LEQUAL);
            glUseProgram(edgeProg_);
            glUniformMatrix4fv(glGetUniformLocation(edgeProg_,"uMVP"),1,GL_FALSE,mvp.m);
            int uCol=glGetUniformLocation(edgeProg_,"uColor"), uAl=glGetUniformLocation(edgeProg_,"uA");
            auto drawRef=[&](const PickRef& pr,float cr,float cg,float cb,float ca){
                glUniform3f(uCol,cr,cg,cb); glUniform1f(uAl,ca);
                for(const auto&b:bodies_){ if(b.body_id!=pr.body_id) continue;
                    if(pr.kind==0){
                        for(const auto&f:b.faces){ if(f.face_id!=pr.topo_id) continue;
                            glBindVertexArray(b.vao);
                            glDrawElements(GL_TRIANGLES,f.tri_count*3,GL_UNSIGNED_INT,
                                           (void*)(size_t)(f.first_tri*3*sizeof(unsigned int)));
                        }
                    } else if(b.evao){
                        glLineWidth(3.5f);
                        for(const auto&ep:b.edgeParts){ if(ep.edge_id!=pr.topo_id) continue;
                            glBindVertexArray(b.evao); glDrawArrays(GL_LINES, ep.vtxStart, ep.vtxCount);
                        }
                    }
                }
            };
            for(const auto& sref:selected_)
                drawRef(sref, sref.kind==1?0.25f:0.30f, sref.kind==1?0.62f:0.55f, 1.0f, sref.kind==1?0.95f:0.42f);
            if(hasHover_)
                drawRef(hovered_, 0.55f,0.80f,1.0f, hovered_.kind==1?0.85f:0.26f);
            glDisable(GL_POLYGON_OFFSET_FILL); glDisable(GL_BLEND); glDepthFunc(GL_LESS); glLineWidth(1.0f);
        }
    }
    glBindVertexArray(0);

    // resolve MSAA -> texture
    glBindFramebuffer(GL_READ_FRAMEBUFFER,msFbo_);
    glBindFramebuffer(GL_DRAW_FRAMEBUFFER,resolveFbo_);
    glBlitFramebuffer(0,0,w_,h_,0,0,w_,h_,GL_COLOR_BUFFER_BIT,GL_NEAREST);
    glBindFramebuffer(GL_FRAMEBUFFER,0);
}

// shared camera mvp for the id passes
static M4 idMvp(float az,float el,float dist,const float* tgt,float fov,float rad,int w,int h){
    float dir[3]={std::cos(el)*std::cos(az),std::cos(el)*std::sin(az),std::sin(el)};
    float eye[3]={tgt[0]+dir[0]*dist,tgt[1]+dir[1]*dist,tgt[2]+dir[2]*dist};
    float up[3]={0,0,1};
    float aspect=(float)w/(float)(h>0?h:1);
    return mul(perspective(fov,aspect,rad*0.02f+0.01f,dist+rad*8.f), lookAt(eye,tgt,up));
}

void Scene::renderIdPassFaces(){
    if(idFbo_==0 || bodies_.empty()) return;
    glBindFramebuffer(GL_FRAMEBUFFER,idFbo_); glViewport(0,0,w_,h_);
    glDisable(GL_BLEND);
    glClearColor(0,0,0,1); glClear(GL_COLOR_BUFFER_BIT|GL_DEPTH_BUFFER_BIT);
    glEnable(GL_DEPTH_TEST); glDepthFunc(GL_LESS);
    M4 mvp=idMvp(az_,el_,dist_,target_,fov_,radius_,w_,h_);
    glUseProgram(idProg_);
    glUniformMatrix4fv(glGetUniformLocation(idProg_,"uMVP"),1,GL_FALSE,mvp.m);
    glUniform1f(glGetUniformLocation(idProg_,"uA"),1.0f);
    glEnable(GL_CULL_FACE); glCullFace(GL_BACK);
    uint32_t idx=0;
    for(const auto& b:bodies_){
        glBindVertexArray(b.vao);
        for(const auto& f:b.faces){
            ++idx;
            glUniform3f(glGetUniformLocation(idProg_,"uColor"),
                        (idx&0xFF)/255.f, ((idx>>8)&0xFF)/255.f, ((idx>>16)&0xFF)/255.f);
            glDrawElements(GL_TRIANGLES,f.tri_count*3,GL_UNSIGNED_INT,
                           (void*)(size_t)(f.first_tri*3*sizeof(unsigned int)));
        }
    }
    glDisable(GL_CULL_FACE); glBindVertexArray(0); glBindFramebuffer(GL_FRAMEBUFFER,0);
}

void Scene::renderIdPassEdges(){
    if(idFbo_==0 || bodies_.empty()) return;
    glBindFramebuffer(GL_FRAMEBUFFER,idFbo_); glViewport(0,0,w_,h_);
    glDisable(GL_BLEND);
    glClearColor(0,0,0,1); glClear(GL_COLOR_BUFFER_BIT|GL_DEPTH_BUFFER_BIT);
    glEnable(GL_DEPTH_TEST);
    M4 mvp=idMvp(az_,el_,dist_,target_,fov_,radius_,w_,h_);
    glUseProgram(idProg_);
    glUniformMatrix4fv(glGetUniformLocation(idProg_,"uMVP"),1,GL_FALSE,mvp.m);
    glUniform1f(glGetUniformLocation(idProg_,"uA"),1.0f);
    // depth-only face prepass so only FRONT (visible) edges get picked
    glDepthFunc(GL_LESS); glColorMask(GL_FALSE,GL_FALSE,GL_FALSE,GL_FALSE);
    glEnable(GL_CULL_FACE); glCullFace(GL_BACK);
    for(const auto& b:bodies_){ glBindVertexArray(b.vao); glDrawElements(GL_TRIANGLES,b.nIdx,GL_UNSIGNED_INT,0); }
    glDisable(GL_CULL_FACE);
    glColorMask(GL_TRUE,GL_TRUE,GL_TRUE,GL_TRUE); glDepthFunc(GL_LEQUAL);
    glLineWidth(4.5f);
    uint32_t idx=0;
    for(const auto& b:bodies_){
        if(!b.evao) { idx += (uint32_t)b.edgeParts.size(); continue; }
        glBindVertexArray(b.evao);
        for(const auto& ep:b.edgeParts){
            ++idx;
            glUniform3f(glGetUniformLocation(idProg_,"uColor"),
                        (idx&0xFF)/255.f, ((idx>>8)&0xFF)/255.f, ((idx>>16)&0xFF)/255.f);
            glDrawArrays(GL_LINES, ep.vtxStart, ep.vtxCount);
        }
    }
    glDepthFunc(GL_LESS); glBindVertexArray(0); glBindFramebuffer(GL_FRAMEBUFFER,0);
}

bool Scene::pickAt(int px,int py,PickRef& out){
    if(px<0||py<0||px>=w_||py>=h_||bodies_.empty()) return false;
    glPixelStorei(GL_PACK_ALIGNMENT,1);
    if(pickKind_==1){  // EDGES: search a small window for the nearest edge pixel
        renderIdPassEdges();
        glBindFramebuffer(GL_FRAMEBUFFER,idFbo_); glReadBuffer(GL_COLOR_ATTACHMENT0);
        const int R=6;
        int cxFbo=px, cyFbo=h_-1-py;
        int x0=std::max(0,cxFbo-R), x1=std::min(w_-1,cxFbo+R);
        int y0=std::max(0,cyFbo-R), y1=std::min(h_-1,cyFbo+R);
        int rw=x1-x0+1, rh=y1-y0+1;
        if(rw<=0||rh<=0){ glBindFramebuffer(GL_FRAMEBUFFER,0); return false; }
        std::vector<unsigned char> buf((size_t)rw*rh*4, 0);
        glReadPixels(x0,y0,rw,rh,GL_RGBA,GL_UNSIGNED_BYTE,buf.data());
        glBindFramebuffer(GL_FRAMEBUFFER,0);
        int ci=cxFbo-x0, cj=cyFbo-y0; uint32_t bestIdx=0; float bestD=1e18f;
        for(int j=0;j<rh;j++) for(int i=0;i<rw;i++){
            size_t o=((size_t)j*rw+i)*4; uint32_t idx=buf[o]|((uint32_t)buf[o+1]<<8)|((uint32_t)buf[o+2]<<16);
            if(idx==0) continue;
            float d=(float)((i-ci)*(i-ci)+(j-cj)*(j-cj));
            if(d<bestD){ bestD=d; bestIdx=idx; }
        }
        if(bestIdx==0 || bestIdx>edgeMap_.size()) return false;
        out=edgeMap_[bestIdx-1]; return true;
    }
    // FACES: exact center pixel
    renderIdPassFaces();
    glBindFramebuffer(GL_FRAMEBUFFER,idFbo_); glReadBuffer(GL_COLOR_ATTACHMENT0);
    unsigned char c[4]={0,0,0,0};
    glReadPixels(px, h_-1-py, 1,1, GL_RGBA, GL_UNSIGNED_BYTE, c);
    glBindFramebuffer(GL_FRAMEBUFFER,0);
    uint32_t idx=(uint32_t)c[0]|((uint32_t)c[1]<<8)|((uint32_t)c[2]<<16);
    if(idx==0 || idx>pickMap_.size()) return false;
    out=pickMap_[idx-1]; return true;
}

void Scene::setHovered(const PickRef* r){ if(r){ hovered_=*r; hasHover_=true; } else hasHover_=false; }
void Scene::clearSelection(){ selected_.clear(); }
void Scene::toggleSelect(const PickRef& r){
    for(size_t i=0;i<selected_.size();++i) if(selected_[i]==r){ selected_.erase(selected_.begin()+i); return; }
    selected_.push_back(r);
}

} // namespace viewport
