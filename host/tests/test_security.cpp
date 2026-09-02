#include "security/integrity.h"
#include "security/containment.h"
#include "security/event_queue.h"
#include "security/runtime_guard.h"
#include "security/release_keys.h"
#include "util/device_identity.h"
#include "util/secret_store.h"
extern "C" {
#include "util/tweetnacl.h"
}
#include <nlohmann/json.hpp>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
namespace fs = std::filesystem;
using namespace chudware::security;
static void require(bool v,const char*m){if(!v){std::cerr<<"FAILED: "<<m<<'\n';std::exit(1);}}
static void put(const fs::path&p,std::string_view s){fs::create_directories(p.parent_path());std::ofstream(p,std::ios::binary).write(s.data(),s.size());}
// Flushed stage markers: the production-only sections below take file locks, so a
// hang has to name where it stopped. macOS x86_64 sat here for a whole ctest
// timeout printing nothing, which said nothing about which call blocked.
static void stage(const char*name){std::cerr<<"stage: "<<name<<std::endl;}
// Process-unique fixture root. A fixed path plus the blocking flock the
// EventQueue takes means one orphaned run (a cancelled CI job, a killed
// debugger) makes every later run wait forever on a lock nobody will release.
int main(){const fs::path root=fs::temp_directory_path()/("chudware-security-test-"+std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));fs::remove_all(root);fs::create_directories(root);stage("integrity");put(root/"worker/chudware-worker","worker");
#ifndef _WIN32
fs::permissions(root/"worker/chudware-worker",fs::perms::owner_read|fs::perms::owner_write,fs::perm_options::replace);
#endif
auto ok=verifyManifestEntry(root,"worker/chudware-worker",6,"87eba76e7f3164534045ba922e7770fb58bbd14ad732bbf5ba6f11cc56989e6e",
#ifndef _WIN32
0600
#else
0
#endif
);if(!ok.ok)std::cerr<<ok.detail<<" reason="<<integrityReasonName(ok.reason)<<'\n';require(ok.ok,"matching worker bytes accepted");put(root/"worker/chudware-worker","changed");require(verifyManifestEntry(root,"worker/chudware-worker",7,"87eba76e7f3164534045ba922e7770fb58bbd14ad732bbf5ba6f11cc56989e6e").reason==IntegrityReason::HashMismatch,"changed worker rejected");require(verifyManifestEntry(root,"../outside",0,std::string(64,'0')).reason==IntegrityReason::UnsafePath,"parent traversal rejected");require(verifyManifestEntry(root,"/absolute",0,std::string(64,'0')).reason==IntegrityReason::UnsafePath,"absolute path rejected");
#ifndef _WIN32
fs::create_symlink(root/"worker/chudware-worker",root/"worker/link");require(verifyManifestEntry(root,"worker/link",7,std::string(64,'0')).reason==IntegrityReason::SymlinkRejected,"symlink rejected");
#endif
stage("sha256-boundaries");// verifyManifestEntry hashes payload files up to 437 MB, so SHA-256 must be right on
// every block boundary AND across the 64 KiB read chunk, not just for short strings.
// The digests come from an independent implementation (coreutils sha256sum / hashlib);
// on x86-64 these also exercise the SHA-NI path, which must agree with the scalar one.
struct ShaVector{std::size_t size;const char*digest;};
static const ShaVector kShaVectors[]={{0,"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"},{1,"084fed08b978af4d7d196a7446a86b58009e636b611db16211b65a9aadff29c5"},{55,"e7313d333c272e639f790978283f9eb392e843d0f29b7016828bb1daa4aac70b"},{56,"4324d65f3c103567f5589c710bc08f8523f929a9272e3af36fc968e52abc6c27"},{57,"35df609437dcfea3279283ab79fd554e2bf78f8f7ae2de532d8ee300b09e8f73"},{63,"81c80242132f230c3bd41b3e63bbcff16107339549214a99614ff26664625055"},{64,"39e3d7b6b5d075d37d053ad89b24b41bef4f3c29760c84447cab3f3be1882241"},{65,"aacca6ff74fdbb296d165a45cecfa04e5127bc008770fbbdd48006f2d2fae95e"},{119,"9ce7368e4daf32341631b492e80359dc9f594b48453cd0dd5bf0b19279cc177e"},{128,"d2742f1f4ac6bb7ca2b239ee18402ba8b3f9f8e652d2a72973c2b9ba11c08cf6"},{129,"307f8fc2c1622b92762e818d39a185d4d667ad49a4b07ceae1f4afa008a93ec4"},{1023,"0978ee87031e207b01d2b755b5bd45956d991339b7455cd99251c30d2938840d"},{1024,"e9183d9a79aad8a047b8e67981210d50b01fc75b1edba5bc32ba3d3ec4d5056d"},{65535,"feaacf5dfeada48ff99357abd0998dd8b350c8b0603a81f573cf3ea577885f99"},{65536,"510b126e1d4ced49107fe4ab03ee54cb1c8e4caf6064e1dd29c48d4a3e74c38b"},{65537,"ad8b370d36508e55e3c9cd44667a6e36e35955d0ff9f8fe59805bb18c2db5dd8"},{65599,"5f8e1c35e118a967b1173d0c0403f861ee2abbc16b751dc54713efb2fbf728c5"},{200000,"ec0ebf98b6f2954bf0f7b839402b1ba245996c39d18e155414e91a2b4353c157"}};
for(const auto&v:kShaVectors){std::string bytes(v.size,'\0');for(std::size_t i=0;i<v.size;++i)bytes[i]=static_cast<char>((i*7+3)&0xFF);const fs::path p=root/"sha"/("v"+std::to_string(v.size)+".bin");put(p,bytes);const auto verdict=verifyManifestEntry(root,("sha/v"+std::to_string(v.size)+".bin"),v.size,v.digest);if(!verdict.ok)std::cerr<<"size="<<v.size<<" reason="<<integrityReasonName(verdict.reason)<<'\n';require(verdict.ok,"sha256 boundary vector");// One flipped bit anywhere must still fail: a fast path that skips blocks would pass above.
if(v.size){std::string flipped=bytes;flipped[v.size/2]=static_cast<char>(flipped[v.size/2]^0x01);put(p,flipped);require(verifyManifestEntry(root,("sha/v"+std::to_string(v.size)+".bin"),v.size,v.digest).reason==IntegrityReason::HashMismatch,"sha256 detects a single flipped bit");}}
require(verifyPayloadManifestBytes(root,"{\"product\":\"Chudware\"}\n","AAAA",VerifyMode::ManifestOnly).reason==IntegrityReason::NonCanonicalManifest,"ManifestOnly still authenticates the manifest");require(verifyPayloadManifestBytes(root,"{}","AAAA",VerifyMode::ManifestOnly).reason==IntegrityReason::InvalidSchema,"ManifestOnly still enforces the manifest schema");
require(chudware::security::release::findKey("release-v1")!=nullptr,"release key lookup");require(chudware::security::release::findKey("unknown")==nullptr,"unknown key rejected");
#if defined(__linux__)
require(installedPayloadRoot("/tmp/AppDir/usr/bin/chudware")==fs::path("/tmp/AppDir"),"linux AppDir root");
#else
require(installedPayloadRoot(root/"chudware")==root,"executable parent root");
#endif
put(root/"manifest.json","{\"product\":\"Chudware\"}\n");put(root/"manifest.sig","AAAA\n");require(verifyPayloadManifest(root,root/"manifest.json",root/"manifest.sig",VerifyMode::Full).reason==IntegrityReason::NonCanonicalManifest,"noncanonical manifest rejected");
stage("device-identity");const fs::path identityRoot=root/"identity";const auto first=chudware::device_identity::ensureForAuthorization(identityRoot.string());require(first.status==chudware::device_identity::KeyStatus::Ready,"device identity generated");require(first.publicKey.size()==43,"public key is unpadded base64url");const auto second=chudware::device_identity::ensureForAuthorization(identityRoot.string());require(second.status==chudware::device_identity::KeyStatus::Ready&&second.publicKey==first.publicKey,"device identity reused");
nlohmann::json envelope={{"token","lease"},{"request_nonce","nonce"},{"sequence",1u},{"app_version","1.2.3"},{"platform","linux"},{"integrity_digest","digest"},{"last_acked_event_sequence",0u},{"customer_receipt",nullptr}};require(chudware::device_identity::signRefreshEnvelope(envelope,identityRoot.string()),"refresh envelope signed");require(envelope.size()==9&&envelope.contains("signature"),"signature is sole added field");
auto decode=[](std::string_view text){std::vector<unsigned char> out;unsigned acc=0;int bits=0;for(unsigned char c:text){int v=c>='A'&&c<='Z'?c-'A':c>='a'&&c<='z'?c-'a'+26:c>='0'&&c<='9'?c-'0'+52:c=='-'?62:c=='_'?63:-1;if(v<0)return std::vector<unsigned char>{};acc=(acc<<6)|static_cast<unsigned>(v);bits+=6;if(bits>=8){bits-=8;out.push_back(static_cast<unsigned char>((acc>>bits)&255));}}return out;};auto pk=decode(first.publicKey);auto sig=decode(envelope["signature"].get<std::string>());envelope.erase("signature");const std::string canonical=envelope.dump();std::vector<unsigned char> sm(sig);sm.insert(sm.end(),canonical.begin(),canonical.end());std::vector<unsigned char> opened(sm.size());unsigned long long openedLen=0;require(pk.size()==crypto_sign_PUBLICKEYBYTES&&sig.size()==crypto_sign_BYTES&&crypto_sign_open(opened.data(),&openedLen,sm.data(),sm.size(),pk.data())==0&&openedLen==canonical.size(),"refresh signature verifies against device public key");
stage("secret-store");std::vector<unsigned char> corrupt(crypto_sign_SECRETKEYBYTES,0);require(chudware::secret_store::store(chudware::device_identity::kService,chudware::device_identity::kAccount,corrupt,identityRoot.string()),"corrupt key fixture stored");require(chudware::device_identity::load(identityRoot.string()).status==chudware::device_identity::KeyStatus::Corrupt,"corrupt key rejected outside authorization");require(!chudware::device_identity::signRefreshEnvelope(envelope,identityRoot.string()),"corrupt key cannot sign");const auto rotated=chudware::device_identity::ensureForAuthorization(identityRoot.string());require(rotated.status==chudware::device_identity::KeyStatus::Ready&&rotated.publicKey!=first.publicKey,"explicit authorization rotates corrupt key");
unsigned char policyPkRaw[crypto_sign_PUBLICKEYBYTES],policySk[crypto_sign_SECRETKEYBYTES];require(crypto_sign_keypair(policyPkRaw,policySk)==0,"policy test key generated");std::array<unsigned char,32> policyPk{};std::copy(policyPkRaw,policyPkRaw+32,policyPk.begin());auto encodePolicy=[](const unsigned char*data,std::size_t size){static constexpr char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";std::string out;unsigned acc=0;int bits=0;for(std::size_t i=0;i<size;++i){acc=(acc<<8)|data[i];bits+=8;while(bits>=6){bits-=6;out.push_back(alphabet[(acc>>bits)&63]);}}if(bits)out.push_back(alphabet[(acc<<(6-bits))&63]);return out;};auto signPolicy=[&](nlohmann::json value){const std::string canonical=value.dump();std::vector<unsigned char> signedMessage(crypto_sign_BYTES+canonical.size());unsigned long long n=0;require(crypto_sign(signedMessage.data(),&n,reinterpret_cast<const unsigned char*>(canonical.data()),canonical.size(),policySk)==0,"policy signed");value["signature"]=encodePolicy(signedMessage.data(),crypto_sign_BYTES);return value;};nlohmann::json standardPolicy=signPolicy({{"id","standard"},{"subject","subject-1"},{"email","test@example.com"},{"entitlement_nonce","ent"},{"request_nonce","request"},{"issued_at",1700000000},{"expires_at",1800000000},{"telemetry_mode","standard"}});chudware::entitlement::SecurityPolicy parsedPolicy;require(chudware::entitlement::verifySecurityPolicy(standardPolicy.dump(),policyPk,"subject-1",parsedPolicy)&&parsedPolicy.id=="standard"&&parsedPolicy.customerEndpoint.empty(),"standard signed policy accepted without endpoint");nlohmann::json customerPolicy=signPolicy({{"id","customer_endpoint"},{"subject","subject-1"},{"email","test@example.com"},{"entitlement_nonce","ent"},{"request_nonce","request"},{"issued_at",1700000000},{"expires_at",1800000000},{"telemetry_mode","customer_endpoint"},{"customer_endpoint","https://security.example/events"}});require(chudware::entitlement::verifySecurityPolicy(customerPolicy.dump(),policyPk,"subject-1",parsedPolicy)&&parsedPolicy.customerEndpoint=="https://security.example/events","signed customer endpoint accepted");customerPolicy["customer_endpoint"]="http://security.example/events";require(!chudware::entitlement::verifySecurityPolicy(customerPolicy.dump(),policyPk,"subject-1",parsedPolicy),"non-HTTPS customer endpoint rejected");customerPolicy=signPolicy({{"id","customer_endpoint"},{"subject","subject-1"},{"email","test@example.com"},{"entitlement_nonce","ent"},{"request_nonce","request"},{"issued_at",1700000000},{"expires_at",1800000000},{"telemetry_mode","customer_endpoint"},{"customer_endpoint","https://security.example/events"}});customerPolicy["customer_endpoint"]="https://evil.example/events";require(!chudware::entitlement::verifySecurityPolicy(customerPolicy.dump(),policyPk,"subject-1",parsedPolicy),"unsigned endpoint override rejected");
unsigned char containmentPk[crypto_sign_PUBLICKEYBYTES],containmentSk[crypto_sign_SECRETKEYBYTES];require(crypto_sign_keypair(containmentPk,containmentSk)==0,"containment test key generated");std::array<unsigned char,32> containmentPublic{};std::copy(containmentPk,containmentPk+32,containmentPublic.begin());auto encode=[](const unsigned char*data,std::size_t size){static constexpr char alphabet[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";std::string out;unsigned acc=0;int bits=0;for(std::size_t i=0;i<size;++i){acc=(acc<<8)|data[i];bits+=8;while(bits>=6){bits-=6;out.push_back(alphabet[(acc>>bits)&63]);}}if(bits)out.push_back(alphabet[(acc<<(6-bits))&63]);return out;};auto signState=[&](nlohmann::json value){const std::string canonical=value.dump();std::vector<unsigned char> signedBytes(canonical.size()+crypto_sign_BYTES);unsigned long long signedLen=0;require(crypto_sign(signedBytes.data(),&signedLen,reinterpret_cast<const unsigned char*>(canonical.data()),canonical.size(),containmentSk)==0,"containment state signed");value["signature"]=encode(signedBytes.data(),crypto_sign_BYTES);return value.dump();};constexpr std::int64_t containmentNow=1800000000;nlohmann::json activeState={{"subject","account-1"},{"state","active"},{"issued_at",containmentNow-10},{"expires_at",containmentNow+10},{"reason_codes",nlohmann::json::array()},{"action_id","action-1"},{"nonce","nonce-1"}};const std::string signedActive=signState(activeState);require(verifyEnforcementState(signedActive,"account-1",containmentNow,containmentPublic).active(),"valid signed active containment state accepted");auto tampered=nlohmann::json::parse(signedActive);tampered["subject"]="account-2";require(verifyEnforcementState(tampered.dump(),"account-2",containmentNow,containmentPublic).status==EnforcementStatus::InvalidSignature,"tampered containment signature rejected");auto extra=nlohmann::json::parse(signedActive);extra["extra"]=true;require(verifyEnforcementState(extra.dump(),"account-1",containmentNow,containmentPublic).status==EnforcementStatus::Malformed,"extra containment schema field rejected");require(verifyEnforcementState(signedActive,"other-account",containmentNow,containmentPublic).status==EnforcementStatus::SubjectMismatch,"containment subject mismatch rejected");require(verifyEnforcementState(signedActive,"account-1",containmentNow+10,containmentPublic).status==EnforcementStatus::Expired,"containment expiry boundary rejected");auto containedState=activeState;containedState["state"]="contained";containedState["reason_codes"]={"debugger_attached"};require(verifyEnforcementState(signState(containedState),"account-1",containmentNow,containmentPublic).status==EnforcementStatus::Contained,"signed contained state enforced");auto badReason=containedState;badReason["reason_codes"]={"local_override"};require(verifyEnforcementState(signState(badReason),"account-1",containmentNow,containmentPublic).status==EnforcementStatus::Malformed,"unapproved reason rejected");const fs::path containmentRoot=root/"containment";require(persistEnforcementState(signedActive,containmentRoot.string()),"containment state persisted");require(loadEnforcementState(containmentRoot.string(),"account-1",containmentNow,containmentPublic).active(),"persisted containment state verified on load");require(loadEnforcementState((root/"missing-containment").string(),"account-1",containmentNow,containmentPublic).status==EnforcementStatus::Missing,"missing containment state is non-active");
#ifndef CHUDWARE_DEVELOPER_BUILD
stage("runtime-guard");RuntimeGuardProbes debuggerProbes{[] { return true; }, [] { return false; }, [] { return std::vector<LoadedModule>{}; }};
auto debuggerSnapshot=inspectRuntime(SecurityCheckpoint::BeforeMutation,root,{},debuggerProbes);require(debuggerSnapshot.reasonCodes==std::vector<std::string>{"debugger_attached"},"debugger reason is bounded");require(debuggerSnapshot.vmContainerScore==0,"VM/container context is not scored");
RuntimeGuardProbes traceProbes{[] { return false; }, [] { return true; }, [] { return std::vector<LoadedModule>{}; }};
auto traceSnapshot=inspectRuntime(SecurityCheckpoint::BeforeAi,root,{},traceProbes);require(traceSnapshot.reasonCodes==std::vector<std::string>{"trace_environment_present"},"trace environment reason is bounded");
std::vector<FileVerdict> moduleFiles{{true,IntegrityReason::Ok,"lib/libchudware-owned.so",{}}};
RuntimeGuardProbes mismatchProbes{[] { return false; }, [] { return false; }, [&] { return std::vector<LoadedModule>{{root/"lib/libchudware-owned.so",true}}; }};
moduleFiles[0].ok=false;moduleFiles[0].reason=IntegrityReason::HashMismatch;
auto mismatchSnapshot=inspectRuntime(SecurityCheckpoint::BeforeWorkerSpawn,root,moduleFiles,mismatchProbes);require(mismatchSnapshot.reasonCodes==std::vector<std::string>{"protected_file_hash_mismatch"},"loaded protected module mismatch detected");
moduleFiles[0].ok=true;moduleFiles[0].reason=IntegrityReason::Ok;
RuntimeGuardProbes unlistedProbes{[] { return false; }, [] { return false; }, [&] { return std::vector<LoadedModule>{{root/"lib/libchudware-extra.so",true}}; }};
auto unlistedSnapshot=inspectRuntime(SecurityCheckpoint::Startup,root,moduleFiles,unlistedProbes);require(unlistedSnapshot.reasonCodes==std::vector<std::string>{"protected_module_unlisted"},"unlisted loaded Chudware module detected");
#endif
#ifndef CHUDWARE_DEVELOPER_BUILD
stage("event-queue");const fs::path queuedRoot = root / "queued-runtime";
fs::create_directories(queuedRoot);
auto queuedDecision = inspectAndPersistRuntime(
    SecurityCheckpoint::BeforeAi, root, {}, queuedRoot.string(), debuggerProbes, 1800000000);
require(!queuedDecision.allowed() && queuedDecision.denialReason == "debugger_attached",
        "runtime denial preserves bounded reason after persistence");
EventQueue queuedEvents(queuedRoot.string());
require(queuedEvents.size() == 1, "runtime reason queued before denial");

stage("event-queue-fill");const fs::path fullRoot = root / "full-runtime";
fs::create_directories(fullRoot);
EventQueue fullEvents(fullRoot.string());
for (std::size_t i = 0; i < kSecurityQueueMaxEvents; ++i)
    require(fullEvents.append({"manifest_signature_invalid", 1800000000 +
                                static_cast<std::int64_t>(i), 100}),
            "nonzero queue fixture filled");
auto failedDecision = inspectAndPersistRuntime(
    SecurityCheckpoint::BeforeWorkerSpawn, root, {}, fullRoot.string(),
    debuggerProbes, 1800002000);
require(!failedDecision.allowed() &&
            failedDecision.denialReason == kRuntimeEvidencePersistenceFailed,
        "queue failure remains a bounded denial");
require(fullEvents.size() == kSecurityQueueMaxEvents,
        "queue failure does not discard prior nonzero evidence");
#endif
fs::remove_all(root);std::cout<<"security tests passed\n";}
