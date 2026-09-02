#include "security/integrity.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <atomic>
#include <set>
#include <thread>
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

// SHA-256 is the whole cost of payload verification (~835 MB on a packaged install,
// 437 MB of it a single libcef.so that no amount of cross-file threading can split).
// x86-64 SHA-NI does the same compression an order of magnitude faster, so use it
// when the CPU reports it and keep the portable scalar path as the fallback.
#if defined(__x86_64__) && (defined(__GNUC__) || defined(__clang__))
#define CHUDWARE_SHA256_NI 1
#include <immintrin.h>
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

#ifdef CHUDWARE_SHA256_NI
// Intel SHA extensions: compress `blocks` 64-byte blocks into the standard
// {a,b,c,d,e,f,g,h} state. Same layout and same result as the scalar block()
// below — pinned by kShaNiMatchesScalar in the self-test at the bottom of this
// file, which also compares against published vectors.
__attribute__((target("sha,sse4.1,ssse3")))
void sha256NiBlocks(std::uint32_t* h, const unsigned char* p, std::size_t blocks) {
    const __m128i mask = _mm_set_epi64x(0x0c0d0e0f08090a0bLL, 0x0405060700010203LL);
    __m128i tmp = _mm_loadu_si128(reinterpret_cast<const __m128i*>(h));
    __m128i state1 = _mm_loadu_si128(reinterpret_cast<const __m128i*>(h + 4));
    tmp = _mm_shuffle_epi32(tmp, 0xB1);
    state1 = _mm_shuffle_epi32(state1, 0x1B);
    __m128i state0 = _mm_alignr_epi8(tmp, state1, 8);
    state1 = _mm_blend_epi16(state1, tmp, 0xF0);

    while (blocks--) {
        const __m128i abef = state0;
        const __m128i cdgh = state1;
        __m128i m0 = _mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(p)), mask);
        __m128i m1 = _mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(p + 16)), mask);
        __m128i m2 = _mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(p + 32)), mask);
        __m128i m3 = _mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(p + 48)), mask);
        __m128i msg;

        // Rounds 0-3 / 4-7 / 8-11: schedule words are still raw message words.
        msg = _mm_add_epi32(m0, _mm_set_epi64x(static_cast<long long>(0xE9B5DBA5B5C0FBCFULL), static_cast<long long>(0x71374491428A2F98ULL)));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        state0 = _mm_sha256rnds2_epu32(state0, state1, _mm_shuffle_epi32(msg, 0x0E));

        msg = _mm_add_epi32(m1, _mm_set_epi64x(static_cast<long long>(0xAB1C5ED5923F82A4ULL), static_cast<long long>(0x59F111F13956C25BULL)));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        state0 = _mm_sha256rnds2_epu32(state0, state1, _mm_shuffle_epi32(msg, 0x0E));
        m0 = _mm_sha256msg1_epu32(m0, m1);

        msg = _mm_add_epi32(m2, _mm_set_epi64x(static_cast<long long>(0x550C7DC3243185BEULL), static_cast<long long>(0x12835B01D807AA98ULL)));
        state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
        state0 = _mm_sha256rnds2_epu32(state0, state1, _mm_shuffle_epi32(msg, 0x0E));
        m1 = _mm_sha256msg1_epu32(m1, m2);

        // Rounds 12-15 onward: each group also extends the schedule (msg2) for the
        // group four ahead, exactly like w[i] = w[i-16] + s0 + w[i-7] + s1 scalar-side.
        static const unsigned long long kRound[13][2] = {
            {0x80DEB1FE72BE5D74LL, 0xC19BF1749BDC06A7LL},
            {0xEFBE4786E49B69C1LL, 0x240CA1CC0FC19DC6LL},
            {0x4A7484AA2DE92C6FLL, 0x76F988DA5CB0A9DCLL},
            {0xA831C66D983E5152LL, 0xBF597FC7B00327C8LL},
            {0xD5A79147C6E00BF3LL, 0x1429296706CA6351LL},
            {0x2E1B213827B70A85LL, 0x53380D134D2C6DFCLL},
            {0x766A0ABB650A7354LL, 0x92722C8581C2C92ELL},
            {0xA81A664BA2BFE8A1LL, 0xC76C51A3C24B8B70LL},
            {0xD6990624D192E819LL, 0x106AA070F40E3585LL},
            {0x1E376C0819A4C116LL, 0x34B0BCB52748774CLL},
            {0x4ED8AA4A391C0CB3LL, 0x682E6FF35B9CCA4FLL},
            {0x78A5636F748F82EELL, 0x8CC7020884C87814LL},
            {0xA4506CEB90BEFFFALL, 0xC67178F2BEF9A3F7LL},
        };
        __m128i* const w[4] = {&m0, &m1, &m2, &m3};
        for (int g = 3; g <= 15; ++g) {
            const int j = g & 3;
            __m128i& cur = *w[j];
            __m128i& nxt = *w[(j + 1) & 3];
            __m128i& prv = *w[(j + 3) & 3];
            msg = _mm_add_epi32(cur, _mm_set_epi64x(static_cast<long long>(kRound[g - 3][1]),
                                                   static_cast<long long>(kRound[g - 3][0])));
            state1 = _mm_sha256rnds2_epu32(state1, state0, msg);
            if (g < 15) {
                nxt = _mm_add_epi32(nxt, _mm_alignr_epi8(cur, prv, 4));
                nxt = _mm_sha256msg2_epu32(nxt, cur);
            }
            state0 = _mm_sha256rnds2_epu32(state0, state1, _mm_shuffle_epi32(msg, 0x0E));
            if (g < 13) prv = _mm_sha256msg1_epu32(prv, cur);
        }

        state0 = _mm_add_epi32(state0, abef);
        state1 = _mm_add_epi32(state1, cdgh);
        p += 64;
    }

    tmp = _mm_shuffle_epi32(state0, 0x1B);
    state1 = _mm_shuffle_epi32(state1, 0xB1);
    _mm_storeu_si128(reinterpret_cast<__m128i*>(h), _mm_blend_epi16(tmp, state1, 0xF0));
    _mm_storeu_si128(reinterpret_cast<__m128i*>(h + 4), _mm_alignr_epi8(state1, tmp, 8));
}

bool shaNiUsable() {
    static const bool ok = __builtin_cpu_supports("sha") &&
                           __builtin_cpu_supports("sse4.1") &&
                           __builtin_cpu_supports("ssse3");
    return ok;
}
#endif  // CHUDWARE_SHA256_NI

class Sha256 { public:void add(const unsigned char*p,std::size_t n){total_+=n;append(p,n);}std::array<unsigned char,32> finish(){auto bits=total_*8;unsigned char one=0x80,pad=0;append(&one,1);while(used_!=56)append(&pad,1);unsigned char len[8];for(int i=0;i<8;++i)len[7-i]=static_cast<unsigned char>(bits>>(i*8));append(len,8);std::array<unsigned char,32>o{};for(int i=0;i<8;++i)for(int j=0;j<4;++j)o[i*4+j]=static_cast<unsigned char>(h_[i]>>(24-j*8));return o;}private:static std::uint32_t r(std::uint32_t x,int n){return(x>>n)|(x<<(32-n));}void append(const unsigned char*p,std::size_t n){if(used_){std::size_t take=std::min(n,std::size_t(64)-used_);std::memcpy(buf_.data()+used_,p,take);used_+=take;p+=take;n-=take;if(used_==64){compress(buf_.data(),1);used_=0;}}if(n>=64){const std::size_t whole=n/64;compress(p,whole);p+=whole*64;n-=whole*64;}if(n){std::memcpy(buf_.data()+used_,p,n);used_+=n;}}void compress(const unsigned char*p,std::size_t blocks){
#ifdef CHUDWARE_SHA256_NI
if(shaNiUsable()){sha256NiBlocks(h_,p,blocks);return;}
#endif
for(std::size_t i=0;i<blocks;++i)block(p+i*64);}void block(const unsigned char*p){static const std::uint32_t K[64]={0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};std::uint32_t w[64];for(int i=0;i<16;++i)w[i]=(static_cast<std::uint32_t>(p[i*4])<<24)|(static_cast<std::uint32_t>(p[i*4+1])<<16)|(static_cast<std::uint32_t>(p[i*4+2])<<8)|p[i*4+3];for(int i=16;i<64;++i)w[i]=w[i-16]+(r(w[i-15],7)^r(w[i-15],18)^(w[i-15]>>3))+w[i-7]+(r(w[i-2],17)^r(w[i-2],19)^(w[i-2]>>10));auto a=h_[0],b=h_[1],c=h_[2],d=h_[3],e=h_[4],f=h_[5],g=h_[6],z=h_[7];for(int i=0;i<64;++i){auto t1=z+(r(e,6)^r(e,11)^r(e,25))+((e&f)^(~e&g))+K[i]+w[i];auto t2=(r(a,2)^r(a,13)^r(a,22))+((a&b)^(a&c)^(b&c));z=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;}h_[0]+=a;h_[1]+=b;h_[2]+=c;h_[3]+=d;h_[4]+=e;h_[5]+=f;h_[6]+=g;h_[7]+=z;}std::uint64_t total_=0;std::size_t used_=0;std::array<unsigned char,64>buf_{};std::uint32_t h_[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};};
std::string hex(const std::array<unsigned char,32>&d){std::ostringstream s;for(auto b:d)s<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<int>(b);return s.str();}
bool selected(std::string_view kind, VerifyMode mode, std::string_view path,
              const fs::path& executable, std::size_t resourceIndex) {
    if (mode == VerifyMode::Full) return true;
    // Hash nothing: the caller verifies exactly one named row right after this,
    // so re-hashing the whole payload here is pure waste.
    if (mode == VerifyMode::ManifestOnly) return false;
    if (kind == "resource") return mode == VerifyMode::Startup && resourceIndex % 16 == 0;
    if (mode == VerifyMode::Startup)
        return kind == "executable" || kind == "library" || kind == "worker" || kind == "policy";
    if (kind == "worker" || kind == "policy" || kind == "library") return true;
    return kind == "executable" &&
           (executable.empty() || fs::path(std::string(path)).filename() == executable.filename());
}

struct PendingRow {
    std::string path;
    std::uintmax_t size = 0;
    std::string sha256;
    std::uint32_t mode = 0;
};

// Hash the selected manifest rows across cores. Verdicts are written by index, so
// the caller still reports the FIRST failure in manifest order no matter how the
// threads interleave. Startup mode covers ~800 MB on a packaged install; hashing it
// on one thread cost ~2.7 s before hostd bound its socket or drew its tray icon.
std::vector<FileVerdict> hashRows(const fs::path& root, const std::vector<PendingRow>& rows) {
    std::vector<FileVerdict> out(rows.size());
    const auto hashOne = [&](std::size_t i) {
        out[i] = verifyManifestEntry(root, rows[i].path, rows[i].size, rows[i].sha256, rows[i].mode);
    };
    unsigned hw = std::thread::hardware_concurrency();
    if (hw == 0) hw = 1;
    const std::size_t workers = std::min<std::size_t>(rows.size(), std::min(hw, 8u));
    if (workers <= 1) {
        for (std::size_t i = 0; i < rows.size(); ++i) hashOne(i);
        return out;
    }
    std::atomic<std::size_t> next{0};
    const auto drain = [&] {
        for (;;) {
            const std::size_t i = next.fetch_add(1, std::memory_order_relaxed);
            if (i >= rows.size()) return;
            hashOne(i);
        }
    };
    std::vector<std::thread> pool;
    pool.reserve(workers - 1);
    for (std::size_t t = 1; t < workers; ++t) pool.emplace_back(drain);
    drain();
    for (auto& t : pool) t.join();
    return out;
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
        verifyPayloadManifestBytes(root, bytes, signature, VerifyMode::ManifestOnly, executable);
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
                                                           VerifyMode::ManifestOnly, executable);
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

IntegrityResult verifyPayloadManifestBytes(const fs::path& root,std::string_view manifestBytes,std::string_view signatureBytes,VerifyMode mode,const fs::path& executable){std::string bytes(manifestBytes),sigText(signatureBytes);if(root.empty())return fail(IntegrityReason::RootNotFound,"payload root unavailable");json j=json::parse(bytes,nullptr,false);if(j.is_discarded())return fail(IntegrityReason::InvalidJson,"manifest is not JSON",root);if(j.dump(-1,' ',false,json::error_handler_t::strict)!=bytes)return fail(IntegrityReason::NonCanonicalManifest,"manifest bytes are not canonical",root);if(!exact(j,{"schema","product","version","platform","arch","created_at","key_id","files"})||!j["schema"].is_string()||j["schema"].get<std::string_view>()!=kSchema||!j["product"].is_string()||j["product"].get<std::string_view>()!="Chudware"||!j["key_id"].is_string()||!j["files"].is_array())return fail(IntegrityReason::InvalidSchema,"manifest schema invalid",root);std::string keyId=j["key_id"];auto key=release::findKey(keyId);if(!key)return fail(IntegrityReason::UnknownKey,"release key untrusted",root);std::vector<unsigned char> sig;if(!decode64(sigText,sig))return fail(IntegrityReason::InvalidSignature,"signature encoding invalid",root);std::string pre(kSchema);pre.push_back('\0');pre+=keyId;pre.push_back('\0');pre+=bytes;std::vector<unsigned char> sm(64+pre.size()),opened(sm.size());std::memcpy(sm.data(),sig.data(),64);std::memcpy(sm.data()+64,pre.data(),pre.size());unsigned long long n=0;if(crypto_sign_open(opened.data(),&n,sm.data(),sm.size(),key->publicKey.data())||n!=pre.size()||std::memcmp(opened.data(),pre.data(),pre.size()))return fail(IntegrityReason::InvalidSignature,"manifest signature invalid",root);IntegrityResult result{true,IntegrityReason::Ok,root,{},{} };std::string previous;std::size_t ri=0;std::vector<PendingRow> rows;for(const auto&e:j["files"]){if(!exact(e,{"path","size","sha256","mode","kind"})||!e["path"].is_string()||!e["size"].is_number_unsigned()||!e["sha256"].is_string()||!e["mode"].is_number_unsigned()||!e["kind"].is_string())return fail(IntegrityReason::InvalidSchema,"file entry invalid",root);std::string p=e["path"],kind=e["kind"];if(!safePath(p)||(!previous.empty()&&p<=previous))return fail(IntegrityReason::UnsafePath,"paths unsafe, duplicated, or unsorted",root);previous=p;if(kind!="executable"&&kind!="library"&&kind!="worker"&&kind!="policy"&&kind!="resource")return fail(IntegrityReason::InvalidSchema,"unknown kind",root);auto i=ri;if(kind=="resource")++ri;if(!selected(kind,mode,p,executable,i))continue;rows.push_back({p,e["size"].get<std::uintmax_t>(),e["sha256"].get<std::string>(),e["mode"].get<std::uint32_t>()});}// Schema/order validation above stays strictly sequential; only the hashing fans out.
for(const auto&v:hashRows(root,rows)){result.files.push_back(v);if(!v.ok){result.ok=false;result.reason=v.reason;result.detail=v.path+": "+v.detail;return result;}}return result;}
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
