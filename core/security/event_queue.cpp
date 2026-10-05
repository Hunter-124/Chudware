#include "security/event_queue.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <mutex>
#include <random>
#include <sstream>
#include <thread>

#include <miniz.h>
#include <nlohmann/json.hpp>

#include "util/device_identity.h"
#include "util/entitlement.h"
extern "C" {
#include "util/tweetnacl.h"
}
#include "util/secret_store.h"
#include "util/time_local.h"

#if defined(_WIN32)
#  include <windows.h>
#else
#  include <fcntl.h>
#  include <sys/file.h>
#  include <unistd.h>
#endif

namespace chudware::security {
namespace {
using json = nlohmann::json;
namespace fs = std::filesystem;
constexpr std::string_view kStateAccount = "security-event-counters-v1";

fs::path queuePath(std::string_view dir) { return fs::path(dir) / "security" / "events.v1"; }
fs::path lockPath(std::string_view dir) { return fs::path(dir) / "security" / "events.v1.lock"; }

class FileLock {
public:
    explicit FileLock(const fs::path& path) {
        processMutex().lock(); ownsMutex_ = true;
        std::error_code ec; fs::create_directories(path.parent_path(), ec);
#if defined(_WIN32)
        handle_ = CreateFileW(path.wstring().c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                              OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
#else
        fd_ = ::open(path.c_str(), O_CREAT | O_RDWR, 0600);
        if (fd_ >= 0 && flock(fd_, LOCK_EX) != 0) { ::close(fd_); fd_ = -1; }
#endif
    }
    ~FileLock() {
#if defined(_WIN32)
        if (handle_ != INVALID_HANDLE_VALUE) CloseHandle(handle_);
#else
        if (fd_ >= 0) { flock(fd_, LOCK_UN); ::close(fd_); }
#endif
        if (ownsMutex_) processMutex().unlock();
    }
    explicit operator bool() const {
#if defined(_WIN32)
        return handle_ != INVALID_HANDLE_VALUE;
#else
        return fd_ >= 0;
#endif
    }
private:
    static std::mutex& processMutex(){static std::mutex value;return value;}
    bool ownsMutex_ = false;
#if defined(_WIN32)
    HANDLE handle_ = INVALID_HANDLE_VALUE;
#else
    int fd_ = -1;
#endif
};

struct Counters { std::uint64_t next = 1, ack = 0; std::string pendingId; std::uint64_t pendingSequence = 0; std::size_t pendingCount = 0; std::string pendingBody; std::string subject; };

bool loadCounters(std::string_view dir, Counters& out) {
    std::vector<unsigned char> bytes;
    if (!secret_store::load(device_identity::kService, kStateAccount, bytes, dir)) return true;
    try {
        const json j = json::parse(bytes.begin(), bytes.end());
        out.next = j.at("next_sequence").get<std::uint64_t>();
        out.ack = j.at("accepted_through").get<std::uint64_t>();
        out.pendingId = j.value("pending_batch_id", "");
        out.pendingSequence = j.value("pending_sequence", 0ULL);
        out.pendingCount = j.value("pending_count", std::size_t{0});
        out.pendingBody = j.value("pending_body", "");
        out.subject = j.value("subject", "");
        return out.next > 0 && out.ack < out.next;
    } catch (...) { return false; }
}
bool storeCounters(std::string_view dir, const Counters& c) {
    const std::string text = json{{"next_sequence",c.next},{"accepted_through",c.ack},
      {"pending_batch_id",c.pendingId},{"pending_sequence",c.pendingSequence},
      {"pending_count",c.pendingCount},{"pending_body",c.pendingBody},{"subject",c.subject}}.dump();
    return secret_store::store(device_identity::kService, kStateAccount,
        std::vector<unsigned char>(text.begin(), text.end()), dir);
}
// State written before subjects were recorded: a pending batch still names its subject.
void adoptLegacySubject(Counters& c) {
    if (!c.subject.empty() || c.pendingBody.empty()) return;
    try { c.subject = json::parse(c.pendingBody).value("subject", ""); } catch (...) {}
}

bool readRows(const fs::path& path, std::vector<json>& rows, bool& corrupt) {
    corrupt = false; std::ifstream in(path, std::ios::binary); if (!in) return true;
    std::string line;
    while (std::getline(in, line)) {
        if (line.empty()) continue;
        try { json row=json::parse(line); if (!row.is_object() || !row["reason"].is_string() ||
              !row["occurred_at"].is_string() || !row["score"].is_number_unsigned()) throw 0;
              rows.push_back(std::move(row)); }
        catch (...) { corrupt = true; }
    }
    return true;
}
std::size_t encodedBytes(const std::vector<json>& rows) {
    std::size_t n=0; for (const auto& r:rows) n += r.dump().size()+1; return n;
}
bool writeRows(const fs::path& path, const std::vector<json>& rows) {
    std::error_code ec; fs::create_directories(path.parent_path(),ec); if(ec)return false;
    const fs::path tmp=path.string()+".tmp."+std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());
    { std::ofstream out(tmp,std::ios::binary|std::ios::trunc); if(!out)return false;
      for(const auto&r:rows) out<<r.dump()<<'\n'; out.flush(); if(!out)return false; }
#if !defined(_WIN32)
    fs::permissions(tmp,fs::perms::owner_read|fs::perms::owner_write,fs::perm_options::replace,ec); if(ec)return false;
#endif
    fs::rename(tmp,path,ec); if(ec){ fs::remove(tmp); return false; } return true;
}

json wireEvent(const json& row) {
    return {{"reason", row.at("reason")}, {"occurred_at", row.at("occurred_at")}};
}
std::string batchId() {
    std::array<unsigned char,16> bytes{};
    std::random_device random;
    for (auto& byte : bytes) byte = static_cast<unsigned char>(random());
    bytes[6] = static_cast<unsigned char>((bytes[6] & 0x0fU) | 0x40U);
    bytes[8] = static_cast<unsigned char>((bytes[8] & 0x3fU) | 0x80U);
    std::ostringstream out;
    out << std::hex << std::setfill('0');
    for (std::size_t i = 0; i < bytes.size(); ++i) {
        if (i == 4 || i == 6 || i == 8 || i == 10) out << '-';
        out << std::setw(2) << static_cast<unsigned>(bytes[i]);
    }
    return out.str();
}

std::string rfc3339Utc(std::int64_t seconds) {
    const std::time_t value = static_cast<std::time_t>(seconds);
    std::tm utc{};
    if (!chudware::util::gmTime(value, utc)) return {};
    std::ostringstream out;
    out << std::put_time(&utc, "%Y-%m-%dT%H:%M:%SZ");
    return out.str();
}
}


bool verifySecurityAcknowledgement(std::string_view body,std::string_view batchId,std::string_view subject,std::string_view policy,std::uint64_t sequence,std::string* enforcementState,std::string* statusOut){
    try {
        json value=json::parse(body);
        static const std::array<std::string_view,8> fields={"status","batch_id","subject","sequence","policy_id","signature","issued_at","enforcement_state"};
        if(!value.is_object()||value.size()!=fields.size())return false;
        for(auto f:fields)if(!value.contains(std::string(f)))return false;
        const std::string status=value.at("status").get<std::string>();
        if(status!="acknowledged"&&status!="contained"&&status!="reauthorize")return false;
        // Typed gets: MSVC (C2666) rejects the implicit-conversion soup of
        // nlohmann::json::operator!= against string_view/uint64_t.
        const std::uint64_t acked=value.at("sequence").get<std::uint64_t>();
        if(value.at("batch_id").get<std::string>()!=batchId||
           value.at("subject").get<std::string>()!=subject||
           value.at("policy_id").get<std::string>()!=policy||
           (status=="reauthorize"?acked<sequence:acked!=sequence)||
           !value.at("enforcement_state").is_object())return false;
        const std::string issuedAt=value.at("issued_at").get<std::string>();
        if(issuedAt.size()!=20||issuedAt[4]!='-'||issuedAt[7]!='-'||issuedAt[10]!='T'||issuedAt[13]!=':'||issuedAt[16]!=':'||issuedAt[19]!='Z')return false;
        std::vector<unsigned char> signature;
        const std::string encoded=value.at("signature").get<std::string>();
        auto cv=[](unsigned char c){if(c>='A'&&c<='Z')return int(c-'A');if(c>='a'&&c<='z')return int(c-'a'+26);if(c>='0'&&c<='9')return int(c-'0'+52);if(c=='-')return 62;if(c=='_')return 63;return -1;};
        unsigned acc=0;int bits=0;for(unsigned char c:encoded){int v=cv(c);if(v<0)return false;acc=(acc<<6)|unsigned(v);bits+=6;if(bits>=8){bits-=8;signature.push_back((acc>>bits)&255);}}
        if(signature.size()!=crypto_sign_BYTES)return false; value.erase("signature"); const std::string canonical=value.dump();
        std::vector<unsigned char> signedMessage(signature);signedMessage.insert(signedMessage.end(),canonical.begin(),canonical.end());std::vector<unsigned char> opened(signedMessage.size());unsigned long long n=0;
        if(crypto_sign_open(opened.data(),&n,signedMessage.data(),signedMessage.size(),entitlement::kEmbeddedPublicKey.data())!=0||n!=canonical.size())return false;
        if(enforcementState)*enforcementState=value.at("enforcement_state").dump();
        if(statusOut)*statusOut=status;
        return true;
    }catch(...){return false;}
}
unsigned securityEventScore(std::string_view r) noexcept {
    if(r=="manifest_signature_invalid")return 100;
    if(r=="protected_file_hash_mismatch")return 80;
    if(r=="protected_module_unlisted")return 50;
    if(r=="debugger_attached")return 35;
    if(r=="trace_environment_present")return 20;
    if(r=="entitlement_signature_invalid"||r=="heartbeat")return 0;
    return 0;
}

bool gzipSecurityPayload(std::string_view input,std::string& output){
    mz_stream stream{};
    if(mz_deflateInit2(&stream,MZ_BEST_COMPRESSION,MZ_DEFLATED,-MZ_DEFAULT_WINDOW_BITS,8,MZ_DEFAULT_STRATEGY)!=MZ_OK)return false;
    std::vector<unsigned char> deflated(mz_compressBound(static_cast<mz_ulong>(input.size())));
    stream.next_in=reinterpret_cast<const unsigned char*>(input.data());stream.avail_in=static_cast<unsigned>(input.size());stream.next_out=deflated.data();stream.avail_out=static_cast<unsigned>(deflated.size());
    const int result=mz_deflate(&stream,MZ_FINISH);const std::size_t size=stream.total_out;mz_deflateEnd(&stream);if(result!=MZ_STREAM_END)return false;
    output.assign("\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff",10);output.append(reinterpret_cast<const char*>(deflated.data()),size);
    const mz_ulong crc=mz_crc32(MZ_CRC32_INIT,reinterpret_cast<const unsigned char*>(input.data()),input.size());
    auto append32=[&](std::uint32_t v){for(int i=0;i<4;++i)output.push_back(static_cast<char>((v>>(8*i))&255));};append32(static_cast<std::uint32_t>(crc));append32(static_cast<std::uint32_t>(input.size()));
    return output.size()<=kSecurityBatchMaxBytes;
}
bool approvedSecurityReason(std::string_view r) noexcept {
    return r=="manifest_signature_invalid"||r=="protected_file_hash_mismatch"||
      r=="protected_module_unlisted"||r=="debugger_attached"||
      r=="trace_environment_present"||r=="entitlement_signature_invalid"||
      r=="heartbeat";
}

EventQueue::EventQueue(std::string dir):dataDir_(std::move(dir)){}
bool EventQueue::append(const SecurityEvent& event,std::string* error) {
    if(!approvedSecurityReason(event.reason)||event.score!=securityEventScore(event.reason)||event.occurredAt<=0){if(error)*error="invalid security event";return false;}
    FileLock lock(lockPath(dataDir_)); if(!lock){if(error)*error="queue lock failed";return false;}
    std::vector<json> rows; bool corrupt=false; readRows(queuePath(dataDir_),rows,corrupt);
    if(corrupt){
        std::error_code ec; fs::copy_file(queuePath(dataDir_),queuePath(dataDir_).string()+".corrupt",fs::copy_options::overwrite_existing,ec);
    }
    const std::string occurredAt = rfc3339Utc(event.occurredAt);
    if (occurredAt.empty()) { if(error)*error="invalid security event timestamp"; return false; }
    rows.push_back({{"reason",event.reason},{"occurred_at",occurredAt},{"score",event.score}});
    while(rows.size()>kSecurityQueueMaxEvents||encodedBytes(rows)>kSecurityQueueMaxBytes){
        auto it=std::find_if(rows.begin(),rows.end(),[](const json&r){return r.value("score",0U)==0;});
        if(it==rows.end()){if(event.score>0){rows.pop_back();if(error)*error="nonzero evidence queue full";return false;} rows.pop_back();break;}
        rows.erase(it);
    }
    if(!writeRows(queuePath(dataDir_),rows)){if(error)*error="queue write failed";return false;} return true;
}
SecurityBatch EventQueue::prepareBatch(std::string_view subject,std::string_view policy,std::string_view version,std::string_view platform,std::int64_t now,std::string* error){
    FileLock lock(lockPath(dataDir_)); if(!lock){if(error)*error="queue lock failed";return{};}
    std::vector<json> rows;bool corrupt=false;readRows(queuePath(dataDir_),rows,corrupt);
    Counters c;if(!loadCounters(dataDir_,c)){if(error)*error="counter state corrupt";return{};}
    adoptLegacySubject(c);
    if(c.subject!=subject){
        // Legacy state with nothing pending adopts the current subject as-is: it is in step
        // with the gateway. A DIFFERENT subject is a new authorization the gateway numbers
        // from zero, so a sequence carried over from the old one would never be accepted.
        if(!c.subject.empty())c=Counters{};
        c.subject=std::string(subject);
        if(!storeCounters(dataDir_,c)){if(error)*error="counter state write failed";return{};}
    }
    if(c.pendingSequence && !c.pendingBody.empty()) return {c.pendingId,c.pendingSequence,c.pendingBody,c.pendingCount,c.pendingCount==0};
    std::vector<json> selected; std::size_t bytes=0;
    for(const auto&r:rows){json wire=wireEvent(r);const auto n=wire.dump().size();if(selected.size()==kSecurityBatchMaxEvents||bytes+n>kSecurityBatchMaxBytes/2)break;selected.push_back(std::move(wire));bytes+=n;}
    const bool heartbeat=selected.empty();
    if(heartbeat) {
        const std::string occurredAt = rfc3339Utc(now);
        if (occurredAt.empty()) { if(error)*error="invalid security event timestamp"; return{}; }
        selected.push_back({{"reason","heartbeat"},{"occurred_at",occurredAt}});
    }
    const std::uint64_t seq=c.pendingSequence?c.pendingSequence:c.next;
    const std::string id=c.pendingId.empty()?batchId():c.pendingId;
    json unsignedBatch={{"batch_id",id},{"subject",subject},{"sequence",seq},{"policy_id",policy},{"app_version",version},{"platform",platform},{"events",selected}};
    json signedBatch=unsignedBatch; if(!device_identity::signSecurityEnvelope(signedBatch,dataDir_,error))return{};
    const std::string body=signedBatch.dump(); if(body.size()>kSecurityBatchMaxBytes){if(error)*error="security batch too large";return{};}
    if(!c.pendingSequence){c.pendingSequence=seq;c.pendingId=id;c.pendingCount=heartbeat?0:selected.size();c.pendingBody=body;if(!storeCounters(dataDir_,c)){if(error)*error="counter state write failed";return{};}}
    return{id,seq,body,heartbeat?0:selected.size(),heartbeat};
}
bool EventQueue::accept(std::string_view id,std::uint64_t through,std::string* error){
    FileLock lock(lockPath(dataDir_));if(!lock)return false; Counters c;if(!loadCounters(dataDir_,c)||id!=c.pendingId||through<c.ack||through>c.pendingSequence){if(error)*error="invalid acknowledgement";return false;}
    std::vector<json> rows;bool corrupt=false;readRows(queuePath(dataDir_),rows,corrupt);
    if(through==c.pendingSequence){const std::size_t count=std::min(c.pendingCount,rows.size()); rows.erase(rows.begin(),rows.begin()+count); c.ack=through;c.next=through==UINT64_MAX?UINT64_MAX:through+1;c.pendingId.clear();c.pendingSequence=0;c.pendingCount=0;c.pendingBody.clear();}
    if(!writeRows(queuePath(dataDir_),rows)||!storeCounters(dataDir_,c)){if(error)*error="ack persistence failed";return false;}return true;
}
std::uint64_t EventQueue::acceptedThrough(std::string_view subject)const{FileLock lock(lockPath(dataDir_));Counters c;if(!lock||!loadCounters(dataDir_,c))return 0;adoptLegacySubject(c);return c.subject.empty()||c.subject==subject?c.ack:0;}
std::size_t EventQueue::size()const{FileLock lock(lockPath(dataDir_));std::vector<json>r;bool c=false;if(lock)readRows(queuePath(dataDir_),r,c);return r.size();}
bool SecurityReporter::report(std::string_view reason,std::int64_t at,std::string* error){return queue_.append({std::string(reason),at,securityEventScore(reason)},error);}
bool SecurityReporter::heartbeat(std::int64_t at,std::string* error){return report("heartbeat",at,error);}

} // namespace chudware::security
