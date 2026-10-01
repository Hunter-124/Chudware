#include "splat_renderer.h"
#include "gl_compat.h"
#include "ui/viewport/glsl_compat.h"
#include "device/numeric_parse.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <limits>
#include <numeric>
#include <sstream>
#include <string_view>

namespace render {
namespace {
constexpr size_t kMaxBytes = size_t(2) * 1024 * 1024 * 1024;
constexpr size_t kMaxVertices = 2000000;
constexpr float kC0 = 0.28209479177387814f;
enum class Type { I8, U8, I16, U16, I32, U32, F32, F64 };
struct Property { Type type; size_t width; int slot; };

bool space(char c) { return c == ' ' || c == '\t' || c == '\r' || c == '\n'; }
std::string_view token(std::string_view& text) {
    while (!text.empty() && space(text.front())) text.remove_prefix(1);
    size_t n = 0;
    while (n < text.size() && !space(text[n])) ++n;
    auto result = text.substr(0, n);
    text.remove_prefix(n);
    return result;
}
bool primitive(const std::string& name, Type& type, size_t& width) {
    static const char* names[][2] = {{"char","int8"},{"uchar","uint8"},
        {"short","int16"},{"ushort","uint16"},{"int","int32"},{"uint","uint32"},
        {"float","float32"},{"double","float64"}};
    static const size_t widths[] = {1,1,2,2,4,4,4,8};
    for (int i = 0; i < 8; ++i) if (name == names[i][0] || name == names[i][1]) {
        type = static_cast<Type>(i); width = widths[i]; return true;
    }
    return false;
}
int slotFor(const std::string& name) {
    static const char* names[] = {"x","y","z","nx","ny","nz","opacity",
        "scale_0","scale_1","scale_2","rot_0","rot_1","rot_2","rot_3",
        "f_dc_0","f_dc_1","f_dc_2"};
    for (int i = 0; i < 17; ++i) if (name == names[i]) return i;
    if (name.compare(0, 7, "f_rest_") == 0) {
        int n = -1;
        auto r = std::from_chars(name.data() + 7, name.data() + name.size(), n);
        if (r.ec != std::errc() || r.ptr != name.data() + name.size() || n < 0 || n >= 45)
            return -2;
        return 17 + n;
    }
    return -1;
}
bool scalar(const char*& p, const char* end, bool binary, const Property& prop, double& value) {
    if (binary) {
        if (size_t(end - p) < prop.width) return false;
        uint64_t bits = 0;
        for (size_t i = 0; i < prop.width; ++i)
            bits |= uint64_t(static_cast<unsigned char>(p[i])) << (8 * i);
        p += prop.width;
        switch (prop.type) {
        case Type::I8: value = static_cast<int8_t>(bits); break;
        case Type::U8: value = static_cast<uint8_t>(bits); break;
        case Type::I16: value = static_cast<int16_t>(bits); break;
        case Type::U16: value = static_cast<uint16_t>(bits); break;
        case Type::I32: value = static_cast<int32_t>(bits); break;
        case Type::U32: value = static_cast<uint32_t>(bits); break;
        case Type::F32: { uint32_t b = uint32_t(bits); float f; std::memcpy(&f, &b, 4); value = f; break; }
        case Type::F64: std::memcpy(&value, &bits, 8); break;
        }
    } else {
        while (p != end && space(*p)) ++p;
        const char* start = p;
        while (p != end && !space(*p)) ++p;
        if (p == start || p - start > 128) return false;
        // Not std::from_chars: Apple's libc++ deletes the floating-point overloads.
        if (!chudware::device::parseFiniteDouble(std::string_view(start, size_t(p - start)), value))
            return false;
        static const double lo[] = {-128,0,-32768,0,-2147483648.0,0,
            -double(std::numeric_limits<float>::max()),-std::numeric_limits<double>::max()};
        static const double hi[] = {127,255,32767,65535,2147483647.0,4294967295.0,
            double(std::numeric_limits<float>::max()),std::numeric_limits<double>::max()};
        const int t = int(prop.type);
        if (value < lo[t] || value > hi[t] || (t < 6 && std::floor(value) != value)) return false;
    }
    return std::isfinite(value);
}
bool decode(const char*& p, const char* end, bool binary, const std::vector<Property>& props,
            int degree, int restCount, Gaussian& g) {
    std::array<double, 62> raw{};
    raw[5] = 1.0;
    for (const auto& prop : props) {
        double v;
        if (!scalar(p, end, binary, prop, v)) return false;
        if (prop.slot >= 0) {
            if (std::fabs(v) > std::numeric_limits<float>::max()) return false;
            raw[size_t(prop.slot)] = v;
        }
    }
    g.pos = {float(raw[0]),float(raw[1]),float(raw[2])};
    // Keep covariance and perspective products representable without discarding normal scenes.
    if (std::max({std::fabs(raw[0]),std::fabs(raw[1]),std::fabs(raw[2])}) > 1e15) return false;
    g.normal = {float(raw[3]),float(raw[4]),float(raw[5])};
    g.opacity = float(raw[6] >= 0 ? 1.0 / (1.0 + std::exp(-raw[6]))
                                            : std::exp(raw[6]) / (1.0 + std::exp(raw[6])));
    float scales[3];
    for (int i = 0; i < 3; ++i) {
        const double s = std::exp(raw[size_t(7+i)]);
        if (!std::isfinite(s) || s < 1e-15 || s > 1e15) return false;
        scales[i] = float(s);
        g.color[i] = float(0.5 + kC0 * raw[size_t(14+i)]);
    }
    g.scale = {scales[0],scales[1],scales[2]};
    double norm = 0;
    for (int i = 0; i < 4; ++i) norm += raw[size_t(10+i)] * raw[size_t(10+i)];
    if (!(norm > 1e-30) || !std::isfinite(norm)) return false;
    norm = std::sqrt(norm);
    for (int i = 0; i < 4; ++i) g.rot[i] = float(raw[size_t(10+i)] / norm);
    g.shDegree = degree;
    const int perChannel = restCount / 3;
    for (int c = 0; c < 3; ++c)
        for (int i = 0; i < perChannel; ++i)
            g.sh[c*15+i] = float(raw[size_t(17+c*perChannel+i)]);
    return true;
}

bool parse(const char* data, size_t len, SplatCloud& out, std::string& err) {
    auto fail = [&](const char* reason) { err = reason; return false; };
    if (!data || !len || len > kMaxBytes) return fail("empty or oversized splat buffer");
    if (len < 4 || std::memcmp(data,"ply",3) != 0 ||
        (data[3] != '\n' && !(len >= 5 && data[3] == '\r' && data[4] == '\n')))
        return fail("missing PLY magic");
    size_t offset = data[3] == '\n' ? 4 : 5, count = 0, stride = 0;
    bool formatSeen = false, vertexSeen = false, ended = false, binary = false;
    std::vector<Property> props;
    std::vector<std::string> names;
    std::array<bool,62> present{};
    while (offset < len && offset < 65536) {
        size_t lineEnd = offset;
        while (lineEnd < len && data[lineEnd] != '\n' && lineEnd - offset <= 4096) ++lineEnd;
        if (lineEnd == len || lineEnd - offset > 4096) return fail("unterminated or oversized PLY header line");
        std::string_view line(data + offset, lineEnd - offset);
        offset = lineEnd + 1;
        auto keyword = token(line);
        if (keyword == "comment" || keyword == "obj_info") continue;
        if (keyword == "format") {
            auto f = token(line), version = token(line);
            if (formatSeen || vertexSeen || version != "1.0" || !token(line).empty() ||
                (f != "ascii" && f != "binary_little_endian")) return fail("unsupported or duplicate PLY format");
            binary = f == "binary_little_endian"; formatSeen = true;
        } else if (keyword == "element") {
            auto name = token(line), number = token(line);
            auto r = std::from_chars(number.data(), number.data() + number.size(), count);
            if (!formatSeen || vertexSeen || name != "vertex" || number.empty() ||
                r.ec != std::errc() || r.ptr != number.data()+number.size() || !token(line).empty() ||
                !count || count > kMaxVertices) return fail("expected one bounded nonempty vertex element");
            vertexSeen = true;
        } else if (keyword == "property") {
            const std::string type(token(line)), name(token(line));
            Property prop{};
            if (!vertexSeen || name.empty() || !token(line).empty() || props.size() >= 128 ||
                !primitive(type, prop.type, prop.width)) return fail("unsupported scalar PLY property");
            if (std::find(names.begin(),names.end(),name) != names.end()) return fail("duplicate PLY property");
            prop.slot = slotFor(name);
            if (prop.slot == -2) return fail("invalid SH coefficient name");
            if (prop.slot >= 0) {
                if (present[size_t(prop.slot)]) return fail("duplicate Gaussian coefficient");
                if (prop.type != Type::F32 && prop.type != Type::F64) return fail("Gaussian properties must be floating point");
                present[size_t(prop.slot)] = true;
            }
            names.push_back(name); props.push_back(prop); stride += prop.width;
        } else if (keyword == "end_header") {
            if (!token(line).empty()) return fail("invalid end_header");
            ended = true; break;
        } else return fail("invalid PLY header directive");
    }
    if (!ended || !formatSeen || !vertexSeen || offset > 65536) return fail("incomplete PLY header");
    for (int i = 0; i < 17; ++i)
        if ((i < 3 || i > 5) && !present[size_t(i)]) return fail("missing required Gaussian property");
    int rest = 0;
    for (int i = 17; i < 62; ++i) if (present[size_t(i)]) ++rest;
    if (rest != 0 && rest != 9 && rest != 24 && rest != 45) return fail("incomplete SH degree (expected 0, 9, 24 or 45 rest coefficients)");
    for (int i = 0; i < rest; ++i) if (!present[size_t(17+i)]) return fail("noncontiguous SH coefficients");
    const int degree = rest == 45 ? 3 : rest == 24 ? 2 : rest == 9 ? 1 : 0;
    if ((binary && count * stride != len - offset) ||
        (!binary && count > (len - offset) / (props.size() * 2 - 1)))
        return fail("PLY payload size does not match vertex count");
    // Validate the entire payload and activated parameters before allocating a potentially large cloud.
    for (int pass = 0; pass < 2; ++pass) {
        const char* p = data + offset;
        if (pass) out.gaussians.reserve(count);
        for (size_t i = 0; i < count; ++i) {
            Gaussian g;
            if (!decode(p, data + len, binary, props, degree, rest, g))
                return fail("invalid, nonfinite, out-of-range or truncated Gaussian data");
            if (pass) out.gaussians.push_back(g);
        }
        if (!binary) while (p < data + len && space(*p)) ++p;
        if (p != data + len) return fail("unexpected trailing PLY data");
    }
    return true;
}
} // namespace

bool parseSplatPly(const char* data, size_t len, SplatCloud& out, std::string& err) {
    out.gaussians.clear(); err.clear();
    try {
        if (parse(data,len,out,err)) return true;
    } catch (const std::exception& e) { err = std::string("cannot parse splat: ") + e.what(); }
    out.gaussians.clear();
    return false;
}
bool loadSplatPly(const std::string& path, SplatCloud& out, std::string& err) {
    out.gaussians.clear(); err.clear();
    try {
        std::ifstream file(path, std::ios::binary | std::ios::ate);
        if (!file) { err = "cannot open " + path; return false; }
        const auto bytes = file.tellg();
        if (bytes <= 0 || uint64_t(bytes) > kMaxBytes) { err = "empty or oversized splat file"; return false; }
        std::string buffer(size_t(bytes), '\0');
        file.seekg(0);
        if (!file.read(buffer.data(), std::streamsize(buffer.size()))) { err = "cannot read complete splat file"; return false; }
        return parseSplatPly(buffer.data(),buffer.size(),out,err);
    } catch (const std::exception& e) { err = std::string("cannot load splat: ") + e.what(); return false; }
}

std::vector<uint32_t> depthSortIndices(const SplatCloud& cloud, const Vec3& eye, const Vec3& viewDir) {
    std::vector<uint32_t> indices(cloud.size());
    std::vector<double> depths(cloud.size());
    const Vec3 dir = normalize(viewDir);
    for (size_t i = 0; i < cloud.size(); ++i) {
        const auto& p = cloud.gaussians[i].pos;
        depths[i] = (double(p.x)-eye.x)*dir.x + (double(p.y)-eye.y)*dir.y + (double(p.z)-eye.z)*dir.z;
    }
    std::iota(indices.begin(),indices.end(),0u);
    std::sort(indices.begin(),indices.end(),[&](uint32_t a, uint32_t b) {
        return depths[a] == depths[b] ? a < b : depths[a] > depths[b];
    });
    return indices;
}

void gaussianCovariance(const Gaussian& g, float covariance[6]) {
    const float w=g.rot[0], x=g.rot[1], y=g.rot[2], z=g.rot[3];
    const Vec3 a{(1-2*(y*y+z*z))*g.scale.x, 2*(x*y+w*z)*g.scale.x, 2*(x*z-w*y)*g.scale.x};
    const Vec3 b{2*(x*y-w*z)*g.scale.y, (1-2*(x*x+z*z))*g.scale.y, 2*(y*z+w*x)*g.scale.y};
    const Vec3 c{2*(x*z+w*y)*g.scale.z, 2*(y*z-w*x)*g.scale.z, (1-2*(x*x+y*y))*g.scale.z};
    covariance[0]=a.x*a.x+b.x*b.x+c.x*c.x;
    covariance[1]=a.x*a.y+b.x*b.y+c.x*c.y;
    covariance[2]=a.x*a.z+b.x*b.z+c.x*c.z;
    covariance[3]=a.y*a.y+b.y*b.y+c.y*c.y;
    covariance[4]=a.y*a.z+b.y*b.z+c.y*c.z;
    covariance[5]=a.z*a.z+b.z*b.z+c.z*c.z;
}

namespace {
const char* kVert = R"(#version 330 core
layout(location=0) in vec2 aCorner;
layout(location=1) in uint iIndex;
uniform mat4 uMVP;
uniform vec3 uEye;
uniform vec2 uViewport;
uniform sampler2D uData;
uniform int uTextureWidth;
out vec2 vUV;
out vec3 vColor;
out float vOpacity;
vec4 dataAt(int n) {
    int p = int(iIndex)*16+n;
    return texelFetch(uData,ivec2(p%uTextureWidth,p/uTextureWidth),0);
}
float coefficient(int channel, int i) {
    int n = channel*15+i;
    return dataAt(4+n/4)[n%4];
}
void main() {
    vec4 centerAlpha=dataAt(0), ca=dataAt(1), cb=dataAt(2), cd=dataAt(3);
    vec4 clip=uMVP*vec4(centerAlpha.xyz,1);
    vUV=aCorner*3.0;
    vColor=vec3(cb.zw,cd.x);
    vOpacity=centerAlpha.w;
    gl_Position=vec4(2,2,2,1);
    if (clip.w <= 1e-6 || clip.z < -clip.w || clip.z > clip.w) return;
    mat3 covariance=mat3(ca.x,ca.y,ca.z, ca.y,ca.w,cb.x, ca.z,cb.x,cb.y);
    vec3 rowW=vec3(uMVP[0][3],uMVP[1][3],uMVP[2][3]);
    // Match Brush's 15% image-frustum margin when differentiating projection.
    // Unbounded offscreen rays otherwise inflate depth-elongated splats over the entire view.
    vec2 ndc=clamp(clip.xy/clip.w,vec2(-1.3),vec2(1.3));
    vec3 jx=(vec3(uMVP[0][0],uMVP[1][0],uMVP[2][0])-ndc.x*rowW)/clip.w*uViewport.x*0.5;
    vec3 jy=(vec3(uMVP[0][1],uMVP[1][1],uMVP[2][1])-ndc.y*rowW)/clip.w*uViewport.y*0.5;
    float xx=dot(jx,covariance*jx)+0.3;
    float xy=dot(jx,covariance*jy);
    float yy=dot(jy,covariance*jy)+0.3;
    float middle=0.5*(xx+yy);
    float radius=length(vec2(0.5*(xx-yy),xy));
    float l1=max(middle+radius,0.3), l2=max(middle-radius,0.3);
    vec2 major=vec2(1,0);
    if (abs(xy)>1e-6) major=normalize(vec2(xy,l1-xx));
    else if (yy>xx) major=vec2(0,1);
    vec2 minor=vec2(-major.y,major.x);
    vec2 pixelOffset=3.0*(aCorner.x*sqrt(l1)*major+aCorner.y*sqrt(l2)*minor);
    gl_Position=clip;
    gl_Position.xy+=pixelOffset*2.0/uViewport*clip.w;
    vec3 delta=centerAlpha.xyz-uEye;
    vec3 d=delta/max(length(delta),1e-12);
    float x=d.x,y=d.y,z=d.z, xxD=x*x,yyD=y*y,zz=z*z;
    float basis[15];
    basis[0]=-0.4886025119*y; basis[1]=0.4886025119*z; basis[2]=-0.4886025119*x;
    basis[3]=1.0925484306*x*y; basis[4]=-1.0925484306*y*z;
    basis[5]=0.3153915653*(2.0*zz-xxD-yyD);
    basis[6]=-1.0925484306*x*z; basis[7]=0.5462742153*(xxD-yyD);
    basis[8]=-0.5900435899*y*(3.0*xxD-yyD);
    basis[9]=2.8906114426*x*y*z;
    basis[10]=-0.4570457995*y*(4.0*zz-xxD-yyD);
    basis[11]=0.3731763326*z*(2.0*zz-3.0*xxD-3.0*yyD);
    basis[12]=-0.4570457995*x*(4.0*zz-xxD-yyD);
    basis[13]=1.4453057213*z*(xxD-yyD);
    basis[14]=-0.5900435899*x*(xxD-3.0*yyD);
    int degree=int(cd.y), n=degree==3?15:(degree==2?8:(degree==1?3:0));
    for (int i=0;i<n;++i)
        vColor+=basis[i]*vec3(coefficient(0,i),coefficient(1,i),coefficient(2,i));
    vColor=max(vColor,vec3(0));
})";
const char* kFrag = R"(#version 330 core
in vec2 vUV;
in vec3 vColor;
in float vOpacity;
out vec4 frag;
void main() {
    float r2=dot(vUV,vUV);
    if (r2>9.0) discard;
    float a=min(0.99,exp(-0.5*r2)*vOpacity);
    if (a<1.0/255.0) discard;
    frag=vec4(vColor,a);
})";

GLuint compile(GLenum type, const char* source) {
    GLuint shader=glCreateShader(type);
#ifdef CHUDWARE_GLES3
    const std::string rewritten=shaderSrc(source);
    const char* p=rewritten.c_str();
#else
    const char* p=shaderSrc(source);
#endif
    glShaderSource(shader,1,&p,nullptr); glCompileShader(shader);
    GLint ok=0; glGetShaderiv(shader,GL_COMPILE_STATUS,&ok);
    if (!ok) {
        char log[2048]; glGetShaderInfoLog(shader,sizeof(log),nullptr,log);
        std::fprintf(stderr,"splat shader: %s\n",log);
        glDeleteShader(shader); return 0;
    }
    return shader;
}
// Covers lazy creation/upload as well as draw; never leaks state into the CAD/ImGui renderer.
struct GLState {
    GLint program,vao,buffer,active,texture,sampler,srcRGB,dstRGB,srcAlpha,dstAlpha,eqRGB,eqAlpha,depthFunc;
    GLboolean blend,depth,cull,depthMask;
    GLState() {
        glGetIntegerv(GL_CURRENT_PROGRAM,&program); glGetIntegerv(GL_VERTEX_ARRAY_BINDING,&vao);
        glGetIntegerv(GL_ARRAY_BUFFER_BINDING,&buffer); glGetIntegerv(GL_ACTIVE_TEXTURE,&active);
        glActiveTexture(GL_TEXTURE0); glGetIntegerv(GL_TEXTURE_BINDING_2D,&texture);
        glGetIntegerv(GL_SAMPLER_BINDING,&sampler); glBindSampler(0,0);
        glGetIntegerv(GL_BLEND_SRC_RGB,&srcRGB); glGetIntegerv(GL_BLEND_DST_RGB,&dstRGB);
        glGetIntegerv(GL_BLEND_SRC_ALPHA,&srcAlpha); glGetIntegerv(GL_BLEND_DST_ALPHA,&dstAlpha);
        glGetIntegerv(GL_BLEND_EQUATION_RGB,&eqRGB); glGetIntegerv(GL_BLEND_EQUATION_ALPHA,&eqAlpha);
        glGetIntegerv(GL_DEPTH_FUNC,&depthFunc);
        blend=glIsEnabled(GL_BLEND); depth=glIsEnabled(GL_DEPTH_TEST); cull=glIsEnabled(GL_CULL_FACE);
        glGetBooleanv(GL_DEPTH_WRITEMASK,&depthMask);
    }
    ~GLState() {
        glUseProgram(program); glBindVertexArray(vao); glBindBuffer(GL_ARRAY_BUFFER,buffer);
        glBindTexture(GL_TEXTURE_2D,texture); glBindSampler(0,sampler); glActiveTexture(active);
        glBlendFuncSeparate(srcRGB,dstRGB,srcAlpha,dstAlpha); glBlendEquationSeparate(eqRGB,eqAlpha);
        glDepthFunc(depthFunc); glDepthMask(depthMask);
        if (blend) glEnable(GL_BLEND); else glDisable(GL_BLEND);
        if (depth) glEnable(GL_DEPTH_TEST); else glDisable(GL_DEPTH_TEST);
        if (cull) glEnable(GL_CULL_FACE); else glDisable(GL_CULL_FACE);
    }
};
} // namespace

SplatRenderer::~SplatRenderer() {
#ifndef CHUDWARE_GLES3
    if (sort_.valid()) sort_.wait();
#endif
    destroy();
}
void SplatRenderer::destroy() {
    if (quadVbo_) glDeleteBuffers(1,&quadVbo_);
    if (orderVbo_) glDeleteBuffers(1,&orderVbo_);
    if (dataTex_) glDeleteTextures(1,&dataTex_);
    if (vao_) glDeleteVertexArrays(1,&vao_);
    if (prog_) glDeleteProgram(prog_);
    quadVbo_=orderVbo_=dataTex_=vao_=prog_=0; glReady_=false;
}
bool SplatRenderer::load(const std::string& path, std::string& err) {
    SplatCloud cloud;
    if (!loadSplatPly(path,cloud,err)) return false;
    setCloud(std::move(cloud)); return true;
}
void SplatRenderer::setCloud(SplatCloud cloud) {
#ifndef CHUDWARE_GLES3
    // A sort only reads cloud_; join before replacing its backing storage. Discard old orders.
    if (sort_.valid()) { sort_.wait(); sort_ = {}; }
#endif
    cloud_=std::move(cloud); dataDirty_=true; instCount_=0;
    lastEye_={1e30f,1e30f,1e30f}; order_.clear();
    min_={std::numeric_limits<float>::max(),std::numeric_limits<float>::max(),std::numeric_limits<float>::max()};
    max_={-min_.x,-min_.y,-min_.z};
    for (const auto& g : cloud_.gaussians) {
        float c[6]; gaussianCovariance(g,c);
        const Vec3 extent{3*std::sqrt(c[0]),3*std::sqrt(c[3]),3*std::sqrt(c[5])};
        min_.x=std::min(min_.x,g.pos.x-extent.x); min_.y=std::min(min_.y,g.pos.y-extent.y); min_.z=std::min(min_.z,g.pos.z-extent.z);
        max_.x=std::max(max_.x,g.pos.x+extent.x); max_.y=std::max(max_.y,g.pos.y+extent.y); max_.z=std::max(max_.z,g.pos.z+extent.z);
    }
}
bool SplatRenderer::bounds(Vec3& min, Vec3& max) const {
    if (cloud_.empty()) return false;
    min=min_; max=max_; return true;
}
bool SplatRenderer::ensureGL(std::string& err) {
    if (glReady_) return true;
    if (glFailed_) { err="splat GL initialization failed"; return false; }
    GLuint v=compile(GL_VERTEX_SHADER,kVert), f=compile(GL_FRAGMENT_SHADER,kFrag);
    if (!v || !f) {
        if (v) glDeleteShader(v);
        if (f) glDeleteShader(f);
        glFailed_=true; err="splat shader compilation failed"; return false;
    }
    prog_=glCreateProgram(); glAttachShader(prog_,v); glAttachShader(prog_,f); glLinkProgram(prog_);
    glDeleteShader(v); glDeleteShader(f);
    GLint linked=0; glGetProgramiv(prog_,GL_LINK_STATUS,&linked);
    if (!linked) {
        char log[2048]; glGetProgramInfoLog(prog_,sizeof(log),nullptr,log);
        err=std::string("splat shader link failed: ")+log;
        glDeleteProgram(prog_); prog_=0; glFailed_=true; return false;
    }
    uMVP_=glGetUniformLocation(prog_,"uMVP"); uEye_=glGetUniformLocation(prog_,"uEye");
    uViewport_=glGetUniformLocation(prog_,"uViewport"); uData_=glGetUniformLocation(prog_,"uData");
    uTextureWidth_=glGetUniformLocation(prog_,"uTextureWidth");
    glGenVertexArrays(1,&vao_); glBindVertexArray(vao_);
    const float quad[]={-1,-1,1,-1,-1,1,1,1};
    glGenBuffers(1,&quadVbo_); glBindBuffer(GL_ARRAY_BUFFER,quadVbo_);
    glBufferData(GL_ARRAY_BUFFER,sizeof(quad),quad,GL_STATIC_DRAW);
    glEnableVertexAttribArray(0); glVertexAttribPointer(0,2,GL_FLOAT,GL_FALSE,2*sizeof(float),nullptr);
    glGenBuffers(1,&orderVbo_); glBindBuffer(GL_ARRAY_BUFFER,orderVbo_);
    glEnableVertexAttribArray(1); glVertexAttribIPointer(1,1,GL_UNSIGNED_INT,sizeof(uint32_t),nullptr);
    glVertexAttribDivisor(1,1); glGenTextures(1,&dataTex_);
    glReady_=true; return true;
}
bool SplatRenderer::uploadCloud(std::string& err) {
    GLint limit=0; glGetIntegerv(GL_MAX_TEXTURE_SIZE,&limit);
    textureWidth_=std::min(limit,4096); textureWidth_-=textureWidth_%16;
    if (textureWidth_ < 16 || cloud_.size()*16 > size_t(textureWidth_)*size_t(limit)) {
        err="Gaussian cloud exceeds GPU texture capacity"; return false;
    }
    const size_t height=(cloud_.size()*16+size_t(textureWidth_)-1)/size_t(textureWidth_);
    std::vector<float> texels(size_t(textureWidth_)*height*4,0);
    for (size_t i=0;i<cloud_.size();++i) {
        const auto& g=cloud_.gaussians[i]; float* p=texels.data()+i*64;
        p[0]=g.pos.x; p[1]=g.pos.y; p[2]=g.pos.z; p[3]=g.opacity;
        gaussianCovariance(g,p+4);
        p[10]=g.color[0]; p[11]=g.color[1]; p[12]=g.color[2]; p[13]=float(g.shDegree);
        std::copy(g.sh,g.sh+45,p+16);
    }
    glBindTexture(GL_TEXTURE_2D,dataTex_);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_CLAMP_TO_EDGE);
    // Pixel-unpack state may belong to an unrelated uploader (including a bound PBO).
    GLint pbo,alignment,rowLength,skipRows,skipPixels;
    glGetIntegerv(GL_PIXEL_UNPACK_BUFFER_BINDING,&pbo); glBindBuffer(GL_PIXEL_UNPACK_BUFFER,0);
    glGetIntegerv(GL_UNPACK_ALIGNMENT,&alignment); glGetIntegerv(GL_UNPACK_ROW_LENGTH,&rowLength);
    glGetIntegerv(GL_UNPACK_SKIP_ROWS,&skipRows); glGetIntegerv(GL_UNPACK_SKIP_PIXELS,&skipPixels);
    glPixelStorei(GL_UNPACK_ALIGNMENT,4); glPixelStorei(GL_UNPACK_ROW_LENGTH,0);
    glPixelStorei(GL_UNPACK_SKIP_ROWS,0); glPixelStorei(GL_UNPACK_SKIP_PIXELS,0);
    glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA32F,textureWidth_,GLsizei(height),0,GL_RGBA,GL_FLOAT,texels.data());
    glPixelStorei(GL_UNPACK_ALIGNMENT,alignment); glPixelStorei(GL_UNPACK_ROW_LENGTH,rowLength);
    glPixelStorei(GL_UNPACK_SKIP_ROWS,skipRows); glPixelStorei(GL_UNPACK_SKIP_PIXELS,skipPixels);
    glBindBuffer(GL_PIXEL_UNPACK_BUFFER,pbo);
    dataDirty_=false; return true;
}
void SplatRenderer::uploadOrder(const std::vector<uint32_t>& order) {
    glBindBuffer(GL_ARRAY_BUFFER,orderVbo_);
    glBufferData(GL_ARRAY_BUFFER,GLsizeiptr(order.size()*sizeof(uint32_t)),order.data(),GL_DYNAMIC_DRAW);
    instCount_=int(order.size());
}
void SplatRenderer::draw(const Renderer::CameraXform& cam) {
    if (cloud_.empty()) return;
    GLState restore;
    std::string err;
    if (!ensureGL(err)) { if (!err.empty()) std::fprintf(stderr,"%s\n",err.c_str()); return; }
    if (dataDirty_ && !uploadCloud(err)) { std::fprintf(stderr,"%s\n",err.c_str()); return; }
    const Vec3 dir=normalize(cam.center-cam.eye);
#ifndef CHUDWARE_GLES3
    if (sort_.valid() && sort_.wait_for(std::chrono::seconds(0)) == std::future_status::ready) {
        try {
            order_=sort_.get(); uploadOrder(order_);
            lastEye_=sortingEye_; lastDir_=sortingDir_;
        } catch (const std::exception& error) {
            std::fprintf(stderr,"splat depth sort failed: %s\n",error.what());
        }
    }
#endif
    const bool changed=cam.eye.x!=lastEye_.x || cam.eye.y!=lastEye_.y || cam.eye.z!=lastEye_.z ||
        dir.x!=lastDir_.x || dir.y!=lastDir_.y || dir.z!=lastDir_.z;
    if (changed || instCount_!=int(cloud_.size())) {
        bool deferred=false;
#ifndef CHUDWARE_GLES3
        if (instCount_==int(cloud_.size()) && cloud_.size()>10000) {
            deferred=true;
            // At most one worker; orbiting coalesces to the latest camera on its completion.
            // Initial order remains synchronous so a new cloud never flashes unsorted.
            if (!sort_.valid()) {
                sortingEye_=cam.eye; sortingDir_=dir;
                const Vec3 eye=cam.eye;
                sort_=std::async(std::launch::async,[this,eye,dir] {
                    return depthSortIndices(cloud_,eye,dir);
                });
            }
        }
#endif
        if (!deferred) {
            order_=depthSortIndices(cloud_,cam.eye,dir); uploadOrder(order_);
            lastEye_=cam.eye; lastDir_=dir;
        }
    }
    GLint viewport[4]; glGetIntegerv(GL_VIEWPORT,viewport);
    if (viewport[2]<=0 || viewport[3]<=0) return;
    glEnable(GL_BLEND); glBlendEquationSeparate(GL_FUNC_ADD,GL_FUNC_ADD);
    glBlendFuncSeparate(GL_SRC_ALPHA,GL_ONE_MINUS_SRC_ALPHA,GL_ONE,GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_DEPTH_TEST); glDepthFunc(GL_LEQUAL); glDepthMask(GL_FALSE); glDisable(GL_CULL_FACE);
    glUseProgram(prog_); glUniformMatrix4fv(uMVP_,1,GL_FALSE,cam.mvp.m);
    glUniform3f(uEye_,cam.eye.x,cam.eye.y,cam.eye.z);
    glUniform2f(uViewport_,float(viewport[2]),float(viewport[3]));
    glUniform1i(uData_,0); glUniform1i(uTextureWidth_,textureWidth_);
    glBindTexture(GL_TEXTURE_2D,dataTex_); glBindVertexArray(vao_);
    glDrawArraysInstanced(GL_TRIANGLE_STRIP,0,4,instCount_);
}
} // namespace render
