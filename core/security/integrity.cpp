#include "security/integrity.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <set>
#include <sstream>

#include <nlohmann/json.hpp>
#include "security/release_keys.h"
extern "C" {
#include "util/tweetnacl.h"
}
#if defined(_WIN32)
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace chudware::security {
namespace fs = std::filesystem;
using json = nlohmann::json;
namespace {
constexpr std::string_view kSchema = "chudware-payload-v1";
IntegrityResult fail(IntegrityReason r, std::string d, fs::path root = {}) { return {false,r,std::move(root),std::move(d),{}}; }
bool readFile(const fs::path& p,std::string& out,std::size_t cap){std::ifstream f(p,std::ios::binary);if(!f)return false;f.seekg(0,std::ios::end);auto n=f.tellg();if(n<0||static_cast<std::uintmax_t>(n)>cap)return false;out.resize(static_cast<std::size_t>(n));f.seekg(0);return out.empty()||static_cast<bool>(f.read(out.data(),n));}
int b64(unsigned char c){if(c>='A'&&c<='Z')return c-'A';if(c>='a'&&c<='z')return c-'a'+26;if(c>='0'&&c<='9')return c-'0'+52;if(c=='+')return 62;if(c=='/')return 63;return-1;}
bool decode64(std::string_view in,std::vector<unsigned char>& out){while(!in.empty()&&(in.back()=='\n'||in.back()=='\r'))in.remove_suffix(1);if(in.empty()||in.size()%4)return false;out.clear();unsigned acc=0;int bits=0;for(std::size_t i=0;i<in.size();++i){unsigned char c=in[i];if(c=='='){if(i+2<in.size())return false;continue;}int v=b64(c);if(v<0)return false;acc=(acc<<6)|static_cast<unsigned>(v);bits+=6;if(bits>=8){bits-=8;out.push_back(static_cast<unsigned char>((acc>>bits)&255));}}return out.size()==64;}
bool exact(const json&o,std::initializer_list<const char*> ks){if(!o.is_object()||o.size()!=ks.size())return false;for(auto k:ks)if(!o.contains(k))return false;return true;}
bool safePath(std::string_view s){if(s.empty()||s.size()>4096||s.front()=='/'||s.front()=='\\'||s.find('\\')!=s.npos||s.find('\0')!=s.npos)return false;fs::path p{std::string(s)};if(p.is_absolute()||p.has_root_name())return false;for(const auto&c:p)if(c=="."||c==".."||c.empty())return false;return p.generic_string()==s;}

class Sha256 { public:void add(const unsigned char*p,std::size_t n){total_+=n;append(p,n);}std::array<unsigned char,32> finish(){auto bits=total_*8;unsigned char one=0x80,pad=0;append(&one,1);while(used_!=56)append(&pad,1);unsigned char len[8];for(int i=0;i<8;++i)len[7-i]=static_cast<unsigned char>(bits>>(i*8));append(len,8);std::array<unsigned char,32>o{};for(int i=0;i<8;++i)for(int j=0;j<4;++j)o[i*4+j]=static_cast<unsigned char>(h_[i]>>(24-j*8));return o;}private:static std::uint32_t r(std::uint32_t x,int n){return(x>>n)|(x<<(32-n));}void append(const unsigned char*p,std::size_t n){while(n--){buf_[used_++]=*p++;if(used_==64){block(buf_.data());used_=0;}}}void block(const unsigned char*p){static const std::uint32_t K[64]={0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};std::uint32_t w[64];for(int i=0;i<16;++i)w[i]=(static_cast<std::uint32_t>(p[i*4])<<24)|(static_cast<std::uint32_t>(p[i*4+1])<<16)|(static_cast<std::uint32_t>(p[i*4+2])<<8)|p[i*4+3];for(int i=16;i<64;++i)w[i]=w[i-16]+(r(w[i-15],7)^r(w[i-15],18)^(w[i-15]>>3))+w[i-7]+(r(w[i-2],17)^r(w[i-2],19)^(w[i-2]>>10));auto a=h_[0],b=h_[1],c=h_[2],d=h_[3],e=h_[4],f=h_[5],g=h_[6],z=h_[7];for(int i=0;i<64;++i){auto t1=z+(r(e,6)^r(e,11)^r(e,25))+((e&f)^(~e&g))+K[i]+w[i];auto t2=(r(a,2)^r(a,13)^r(a,22))+((a&b)^(a&c)^(b&c));z=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;}h_[0]+=a;h_[1]+=b;h_[2]+=c;h_[3]+=d;h_[4]+=e;h_[5]+=f;h_[6]+=g;h_[7]+=z;}std::uint64_t total_=0;std::size_t used_=0;std::array<unsigned char,64>buf_{};std::uint32_t h_[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};};
std::string hex(const std::array<unsigned char,32>&d){std::ostringstream s;for(auto b:d)s<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<int>(b);return s.str();}
bool selected(std::string_view kind, VerifyMode mode, std::string_view path,
              const fs::path& executable, std::size_t resourceIndex) {
    if (mode == VerifyMode::Full) return true;
    if (kind == "resource") return mode == VerifyMode::Startup && resourceIndex % 16 == 0;
    if (mode == VerifyMode::Startup)
        return kind == "executable" || kind == "library" || kind == "worker" || kind == "policy";
    if (kind == "worker" || kind == "policy" || kind == "library") return true;
    return kind == "executable" &&
           (executable.empty() || fs::path(std::string(path)).filename() == executable.filename());
}
}

fs::path installedPayloadRoot(const fs::path& executable){fs::path p=executable.lexically_normal();
#if defined(__APPLE__)
// Walk up to the enclosing .app. Stop when parent_path() stops changing: for an
// absolute path it converges on "/" and NEVER becomes empty, so the obvious
// `q=q.parent_path()` loop spins forever for any executable that is not inside
// a bundle -- every unbundled layout, and the reason host test_security burned
// its entire ctest timeout on macOS with no output.
for(fs::path q=p.parent_path();!q.empty();){if(q.extension()==".app")return q;fs::path next=q.parent_path();if(next==q)break;q=std::move(next);}
#elif defined(__linux__)
auto q=p.parent_path();if(q.filename()=="bin"&&q.parent_path().filename()=="usr")return q.parent_path().parent_path();
#endif
return p.parent_path();}

FileVerdict verifyManifestEntry(const fs::path& root,std::string_view rel,std::uintmax_t expectedSize,std::string_view expectedHash,std::uint32_t expectedMode){FileVerdict v{false,IntegrityReason::IoError,std::string(rel),{}};if(!safePath(rel)||expectedHash.size()!=64||!std::all_of(expectedHash.begin(),expectedHash.end(),[](unsigned char c){return std::isdigit(c)||(c>='a'&&c<='f');})){v.reason=IntegrityReason::UnsafePath;v.detail="unsafe path or hash";return v;}auto path=root/fs::path(std::string(rel));
#if defined(_WIN32)
HANDLE fd=CreateFileW(path.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL|FILE_FLAG_OPEN_REPARSE_POINT,nullptr);if(fd==INVALID_HANDLE_VALUE){v.reason=IntegrityReason::MissingFile;v.detail="open failed";return v;}BY_HANDLE_FILE_INFORMATION st{};if(!GetFileInformationByHandle(fd,&st)||(st.dwFileAttributes&(FILE_ATTRIBUTE_REPARSE_POINT|FILE_ATTRIBUTE_DIRECTORY))){CloseHandle(fd);v.reason=IntegrityReason::SymlinkRejected;v.detail="not regular non-reparse file";return v;}std::uintmax_t size=(static_cast<std::uintmax_t>(st.nFileSizeHigh)<<32)|st.nFileSizeLow;Sha256 sha;unsigned char buf[65536];DWORD n=0;while(ReadFile(fd,buf,sizeof buf,&n,nullptr)&&n)sha.add(buf,n);CloseHandle(fd);
#else
int fd=::open(path.c_str(),O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0){v.reason=errno==ELOOP?IntegrityReason::SymlinkRejected:IntegrityReason::MissingFile;v.detail=std::strerror(errno);return v;}struct stat st{};if(::fstat(fd,&st)||!S_ISREG(st.st_mode)){::close(fd);v.reason=IntegrityReason::NotRegularFile;v.detail="not regular";return v;}std::uintmax_t size=st.st_size;if(expectedMode&&(static_cast<std::uint32_t>(st.st_mode)&0777)!=expectedMode){::close(fd);v.reason=IntegrityReason::ModeMismatch;v.detail="mode mismatch";return v;}Sha256 sha;unsigned char buf[65536];for(;;){ssize_t n=::read(fd,buf,sizeof buf);if(n>0)sha.add(buf,n);else if(n==0)break;else if(errno!=EINTR){::close(fd);v.reason=IntegrityReason::IoError;v.detail=std::strerror(errno);return v;}}::close(fd);
#endif
if(size!=expectedSize){v.reason=IntegrityReason::SizeMismatch;v.detail="size mismatch";return v;}if(hex(sha.finish())!=expectedHash){v.reason=IntegrityReason::HashMismatch;v.detail="sha256 mismatch";return v;}v.ok=true;v.reason=IntegrityReason::Ok;return v;}

FileVerdict verifyInstalledManifestEntry(const fs::path& executable,
                                         std::string_view relativePath,
                                         std::string_view expectedKind) {
    FileVerdict out{false, IntegrityReason::IoError, std::string(relativePath), {}};
#ifdef CHUDWARE_DEVELOPER_BUILD
    out.ok = true;
    out.reason = IntegrityReason::DeveloperBuild;
    out.detail = "developer build";
    return out;
#else
    const fs::path root = installedPayloadRoot(executable);
    std::string bytes;
    std::string signature;
    if (!readFile(root / "payload-manifest.v1.json", bytes, 16 * 1024 * 1024)) {
        out.reason = IntegrityReason::ManifestMissing;
        out.detail = "manifest unreadable";
        return out;
    }
    if (!readFile(root / "payload-manifest.v1.sig", signature, 256)) {
        out.reason = IntegrityReason::SignatureMissing;
        out.detail = "signature unreadable";
        return out;
    }
    const auto authenticated =
        verifyPayloadManifestBytes(root, bytes, signature, VerifyMode::Startup, executable);
    if (!authenticated.ok) {
        out.reason = authenticated.reason;
        out.detail = authenticated.detail;
        return out;
    }
    const json manifest = json::parse(bytes, nullptr, false);
    const json* match = nullptr;
    for (const auto& row : manifest["files"]) {
        if (row["path"].get<std::string>() != relativePath) continue;
        if (match) {
            out.reason = IntegrityReason::InvalidSchema;
            out.detail = "duplicate manifest row";
            return out;
        }
        match = &row;
    }
    if (!match) {
        out.reason = IntegrityReason::MissingFile;
        out.detail = "path is not manifest-owned";
        return out;
    }
    if ((*match)["kind"].get<std::string>() != expectedKind) {
        out.reason = IntegrityReason::InvalidSchema;
        out.detail = "manifest kind mismatch";
        return out;
    }
    return verifyManifestEntry(root,
                               relativePath,
                               (*match)["size"].get<std::uintmax_t>(),
                               (*match)["sha256"].get<std::string>(),
                               (*match)["mode"].get<std::uint32_t>());
#endif
}

namespace {
template <typename VerifyHeld>
FileVerdict verifyInstalledManifestHeld(const fs::path& executable,
                                        std::string_view relativePath,
                                        std::string_view expectedKind,
                                        VerifyHeld&& verifyHeld) {
    FileVerdict out{false, IntegrityReason::IoError, std::string(relativePath), {}};
#ifdef CHUDWARE_DEVELOPER_BUILD
    out.ok = true;
    out.reason = IntegrityReason::DeveloperBuild;
    return out;
#else
    const fs::path root = installedPayloadRoot(executable);
    std::string bytes, signature;
    if (!readFile(root / "payload-manifest.v1.json", bytes, 16 * 1024 * 1024)) {
        out.reason = IntegrityReason::ManifestMissing; out.detail = "manifest unreadable"; return out;
    }
    if (!readFile(root / "payload-manifest.v1.sig", signature, 256)) {
        out.reason = IntegrityReason::SignatureMissing; out.detail = "signature unreadable"; return out;
    }
    const auto authenticated = verifyPayloadManifestBytes(root, bytes, signature,
                                                           VerifyMode::Startup, executable);
    if (!authenticated.ok) {
        out.reason = authenticated.reason; out.detail = authenticated.detail; return out;
    }
    const json manifest = json::parse(bytes);
    const json* match = nullptr;
    for (const auto& row : manifest["files"]) {
        if (row["path"].get<std::string>() == relativePath) {
            if (match) { out.reason = IntegrityReason::InvalidSchema; out.detail = "duplicate manifest row"; return out; }
            match = &row;
        }
    }
    if (!match) { out.reason = IntegrityReason::MissingFile; out.detail = "path is not manifest-owned"; return out; }
    if ((*match)["kind"].get<std::string>() != expectedKind) {
        out.reason = IntegrityReason::InvalidSchema; out.detail = "manifest kind mismatch"; return out;
    }
    return verifyHeld((*match)["size"].get<std::uintmax_t>(),
                      (*match)["sha256"].get<std::string>(),
                      (*match)["mode"].get<std::uint32_t>());
#endif
}
}

#if defined(_WIN32)
FileVerdict verifyInstalledManifestHandle(const fs::path& executable,
                                          std::string_view relativePath,
                                          std::string_view expectedKind,
                                          void* fileHandle) {
    return verifyInstalledManifestHeld(executable, relativePath, expectedKind,
        [&](std::uintmax_t expectedSize, const std::string& expectedHash, std::uint32_t) {
            FileVerdict v{false, IntegrityReason::IoError, std::string(relativePath), {}};
            HANDLE h = static_cast<HANDLE>(fileHandle); BY_HANDLE_FILE_INFORMATION st{};
            if (!h || h == INVALID_HANDLE_VALUE || !GetFileInformationByHandle(h, &st) ||
                (st.dwFileAttributes & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY))) {
                v.reason = IntegrityReason::SymlinkRejected; v.detail = "not regular non-reparse file"; return v;
            }
            LARGE_INTEGER zero{}; SetFilePointerEx(h, zero, nullptr, FILE_BEGIN);
            Sha256 sha; unsigned char buf[65536]; DWORD n = 0;
            while (ReadFile(h, buf, sizeof buf, &n, nullptr) && n) sha.add(buf, n);
            const std::uintmax_t size = (static_cast<std::uintmax_t>(st.nFileSizeHigh) << 32) | st.nFileSizeLow;
            if (size != expectedSize) { v.reason = IntegrityReason::SizeMismatch; v.detail = "size mismatch"; return v; }
            if (hex(sha.finish()) != expectedHash) { v.reason = IntegrityReason::HashMismatch; v.detail = "sha256 mismatch"; return v; }
            v.ok = true; v.reason = IntegrityReason::Ok; return v;
        });
}
#else
FileVerdict verifyInstalledManifestFd(const fs::path& executable,
                                      std::string_view relativePath,
                                      std::string_view expectedKind,
                                      int fd) {
    return verifyInstalledManifestHeld(executable, relativePath, expectedKind,
        [&](std::uintmax_t expectedSize, const std::string& expectedHash, std::uint32_t expectedMode) {
            FileVerdict v{false, IntegrityReason::IoError, std::string(relativePath), {}};
            struct stat st{};
            if (fd < 0 || ::fstat(fd, &st) || !S_ISREG(st.st_mode)) { v.reason = IntegrityReason::NotRegularFile; return v; }
            if (expectedMode && (static_cast<std::uint32_t>(st.st_mode) & 0777) != expectedMode) { v.reason = IntegrityReason::ModeMismatch; return v; }
            if (::lseek(fd, 0, SEEK_SET) < 0) { v.reason = IntegrityReason::IoError; return v; }
            Sha256 sha; unsigned char buf[65536];
            for (;;) { ssize_t n = ::read(fd, buf, sizeof buf); if (n > 0) sha.add(buf, n); else if (!n) break; else if (errno != EINTR) { v.reason = IntegrityReason::IoError; return v; } }
            if (static_cast<std::uintmax_t>(st.st_size) != expectedSize) { v.reason = IntegrityReason::SizeMismatch; return v; }
            if (hex(sha.finish()) != expectedHash) { v.reason = IntegrityReason::HashMismatch; return v; }
            v.ok = true; v.reason = IntegrityReason::Ok; return v;
        });
}
#endif

IntegrityResult verifyPayloadManifestBytes(const fs::path& root,std::string_view manifestBytes,std::string_view signatureBytes,VerifyMode mode,const fs::path& executable){std::string bytes(manifestBytes),sigText(signatureBytes);if(root.empty())return fail(IntegrityReason::RootNotFound,"payload root unavailable");json j=json::parse(bytes,nullptr,false);if(j.is_discarded())return fail(IntegrityReason::InvalidJson,"manifest is not JSON",root);if(j.dump(-1,' ',false,json::error_handler_t::strict)!=bytes)return fail(IntegrityReason::NonCanonicalManifest,"manifest bytes are not canonical",root);if(!exact(j,{"schema","product","version","platform","arch","created_at","key_id","files"})||!j["schema"].is_string()||j["schema"].get<std::string_view>()!=kSchema||!j["product"].is_string()||j["product"].get<std::string_view>()!="Chudware"||!j["key_id"].is_string()||!j["files"].is_array())return fail(IntegrityReason::InvalidSchema,"manifest schema invalid",root);std::string keyId=j["key_id"];auto key=release::findKey(keyId);if(!key)return fail(IntegrityReason::UnknownKey,"release key untrusted",root);std::vector<unsigned char> sig;if(!decode64(sigText,sig))return fail(IntegrityReason::InvalidSignature,"signature encoding invalid",root);std::string pre(kSchema);pre.push_back('\0');pre+=keyId;pre.push_back('\0');pre+=bytes;std::vector<unsigned char> sm(64+pre.size()),opened(sm.size());std::memcpy(sm.data(),sig.data(),64);std::memcpy(sm.data()+64,pre.data(),pre.size());unsigned long long n=0;if(crypto_sign_open(opened.data(),&n,sm.data(),sm.size(),key->publicKey.data())||n!=pre.size()||std::memcmp(opened.data(),pre.data(),pre.size()))return fail(IntegrityReason::InvalidSignature,"manifest signature invalid",root);IntegrityResult result{true,IntegrityReason::Ok,root,{},{} };std::string previous;std::size_t ri=0;for(const auto&e:j["files"]){if(!exact(e,{"path","size","sha256","mode","kind"})||!e["path"].is_string()||!e["size"].is_number_unsigned()||!e["sha256"].is_string()||!e["mode"].is_number_unsigned()||!e["kind"].is_string())return fail(IntegrityReason::InvalidSchema,"file entry invalid",root);std::string p=e["path"],kind=e["kind"];if(!safePath(p)||(!previous.empty()&&p<=previous))return fail(IntegrityReason::UnsafePath,"paths unsafe, duplicated, or unsorted",root);previous=p;if(kind!="executable"&&kind!="library"&&kind!="worker"&&kind!="policy"&&kind!="resource")return fail(IntegrityReason::InvalidSchema,"unknown kind",root);auto i=ri;if(kind=="resource")++ri;if(!selected(kind,mode,p,executable,i))continue;auto v=verifyManifestEntry(root,p,e["size"].get<std::uintmax_t>(),e["sha256"].get<std::string>(),e["mode"].get<std::uint32_t>());result.files.push_back(v);if(!v.ok){result.ok=false;result.reason=v.reason;result.detail=v.path+": "+v.detail;return result;}}return result;}
IntegrityResult verifyPayloadManifest(const fs::path& root,const fs::path& manifest,const fs::path& signature,VerifyMode mode,const fs::path& executable){std::string bytes,sigText;if(root.empty())return fail(IntegrityReason::RootNotFound,"payload root unavailable");if(!readFile(manifest,bytes,16*1024*1024))return fail(IntegrityReason::ManifestMissing,"manifest unreadable",root);if(!readFile(signature,sigText,256))return fail(IntegrityReason::SignatureMissing,"signature unreadable",root);return verifyPayloadManifestBytes(root,bytes,sigText,mode,executable);}
IntegrityResult verifyInstalledPayload(const fs::path& exe,VerifyMode mode){
#ifdef CHUDWARE_DEVELOPER_BUILD
return {true,IntegrityReason::DeveloperBuild,installedPayloadRoot(exe),"developer build",{}};
#else
auto root=installedPayloadRoot(exe);return verifyPayloadManifest(root,root/"payload-manifest.v1.json",root/"payload-manifest.v1.sig",mode,exe);
#endif
}
const char* integrityReasonName(IntegrityReason r)noexcept{switch(r){case IntegrityReason::Ok:return"ok";case IntegrityReason::DeveloperBuild:return"developer_build";case IntegrityReason::RootNotFound:return"root_not_found";case IntegrityReason::ManifestMissing:return"manifest_missing";case IntegrityReason::SignatureMissing:return"signature_missing";case IntegrityReason::IoError:return"io_error";case IntegrityReason::InvalidJson:return"invalid_json";case IntegrityReason::NonCanonicalManifest:return"noncanonical_manifest";case IntegrityReason::InvalidSchema:return"invalid_schema";case IntegrityReason::UnknownKey:return"unknown_key";case IntegrityReason::InvalidSignature:return"invalid_signature";case IntegrityReason::UnsafePath:return"unsafe_path";case IntegrityReason::MissingFile:return"missing_file";case IntegrityReason::NotRegularFile:return"not_regular_file";case IntegrityReason::SymlinkRejected:return"symlink_rejected";case IntegrityReason::SizeMismatch:return"size_mismatch";case IntegrityReason::HashMismatch:return"hash_mismatch";case IntegrityReason::ModeMismatch:return"mode_mismatch";}return"unknown";}
} // namespace chudware::security
