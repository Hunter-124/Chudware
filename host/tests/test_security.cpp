#include "security/integrity.h"
#include "security/release_keys.h"
#include <cstdlib>
#include <string>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
namespace fs = std::filesystem;
using namespace chudware::security;
static void require(bool v,const char*m){if(!v){std::cerr<<"FAILED: "<<m<<'\n';std::exit(1);}}
static void put(const fs::path&p,std::string_view s){fs::create_directories(p.parent_path());std::ofstream(p,std::ios::binary).write(s.data(),s.size());}
// Flushed stage markers so a hang or crash names the section it stopped in.
static void stage(const char*name){std::cerr<<"stage: "<<name<<std::endl;}
// Process-unique fixture root so concurrent or orphaned runs never share files.
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
fs::remove_all(root);std::cout<<"security tests passed\n";}
