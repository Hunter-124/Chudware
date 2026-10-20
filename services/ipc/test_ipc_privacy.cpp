// Standalone regressions, compiled by services/mcp/test_hostd_privacy.py.
#include "ipc_private.h"
#include "ipc_transport.h"
#include "ipc_discovery.h"
#include "ipc_windows_local.h"
#include "../../core/hostd/hostd_log.h"

#include <cassert>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <thread>
#include <chrono>

#ifndef _WIN32
#include <sys/socket.h>
#include <sys/un.h>
#endif

namespace fs = std::filesystem;
using namespace chudware_ipc;

#ifdef _WIN32
static void assertPrivateAcl(const fs::path& path) {
    PSID owner = nullptr;
    PACL acl = nullptr;
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    assert(GetNamedSecurityInfoW(const_cast<wchar_t*>(path.c_str()), SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION, &owner, nullptr,
        &acl, nullptr, &descriptor) == ERROR_SUCCESS);
    assert(acl && owner);
    SECURITY_DESCRIPTOR_CONTROL control = 0;
    DWORD revision = 0;
    assert(GetSecurityDescriptorControl(descriptor, &control, &revision));
    assert(control & SE_DACL_PROTECTED);
    unsigned char systemBuffer[SECURITY_MAX_SID_SIZE];
    DWORD systemSize = sizeof systemBuffer;
    assert(CreateWellKnownSid(WinLocalSystemSid, nullptr, systemBuffer, &systemSize));
    assert(acl->AceCount == 2);
    bool foundOwner = false, foundSystem = false;
    for (DWORD i = 0; i < acl->AceCount; ++i) {
        void* raw = nullptr;
        assert(GetAce(acl, i, &raw));
        auto* ace = static_cast<ACCESS_ALLOWED_ACE*>(raw);
        assert(ace->Header.AceType == ACCESS_ALLOWED_ACE_TYPE);
        PSID sid = &ace->SidStart;
        if (EqualSid(sid, owner)) foundOwner = true;
        else { assert(EqualSid(sid, systemBuffer)); foundSystem = true; }
    }
    assert(foundOwner && foundSystem);
    LocalFree(descriptor);
}
static std::wstring ownerSidText(const fs::path& path) {
    PSID owner = nullptr;
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    assert(GetNamedSecurityInfoW(const_cast<wchar_t*>(path.c_str()), SE_FILE_OBJECT,
        OWNER_SECURITY_INFORMATION, &owner, nullptr, nullptr, nullptr, &descriptor) == ERROR_SUCCESS);
    LPWSTR text = nullptr;
    assert(ConvertSidToStringSidW(owner, &text));
    const std::wstring result = text;
    LocalFree(text);
    LocalFree(descriptor);
    return result;
}

#else
static void assertMode(const fs::path& path, mode_t mode) {
    struct stat st{};
    assert(::lstat(path.c_str(), &st) == 0);
    assert((st.st_mode & 0777) == mode);
    assert(st.st_uid == ::geteuid());
}
#endif

int main(int argc, char** argv) {
#ifdef _WIN32
    if (argc == 3 && std::string(argv[1]) == "--lock-child") {
        SingleInstance child;
        std::string err;
        if (!child.acquire(argv[2], err)) return 2;
        Sleep(INFINITE);  // Only the fixture's parent terminates this process.
        return 3;
    }
#endif
    assert(argc == 2);
    const fs::path root = fs::u8path(argv[1]);
#ifndef _WIN32
    ::umask(0022);
#endif
    const auto state = root / "state";
    const auto logs = state / "logs";
    assert(privateDir(state.u8string()));
    assert(privateDir(logs.u8string()));
    assert(privateWrite((logs / "secret.json").u8string(), "confidential-fixture"));
    assert(privateCopy((logs / "secret.json").u8string(), (logs / "copy.json").u8string()));
    assert(hostd::log().open(logs.u8string()));
    hostd::log().line("%s", std::string(5 * 1024 * 1024, 'x').c_str());
    hostd::log().line("private-after-rotation");
#ifdef _WIN32
    assertPrivateAcl(state);
    assertPrivateAcl(logs);
    assertPrivateAcl(logs / "secret.json");
    assertPrivateAcl(logs / "copy.json");
    assert(_putenv_s("APPDATA", root.u8string().c_str()) == 0);
    const auto key = fnv1aHex(root.u8string());
    // Squat only a unique fixture's RETIRED public name: it cannot affect the
    // new private file singleton, including collisions with other object types.
    const auto oldName = L"Global\\chudware-hostd-" + ownerSidText(state) + L"-" +
                         std::wstring(key.begin(), key.end());
    HANDLE squat = CreateMutexW(nullptr, FALSE, oldName.c_str());
    assert(squat);
    assertPrivateAcl(logs / "hostd.log");
    assertPrivateAcl(logs / "hostd.log.1");
    const auto path = lockPath(key);
    assert(fs::u8path(path).parent_path() == root / "Chudware");
    assert(path.rfind("Global\\", 0) != 0 && path.rfind("Local\\", 0) != 0);
    std::string err;
    SingleInstance first, second;
    assert(!privateLockHeld(path));
    assert(first.acquire(path, err));
    assert(privateLockHeld(path));
    assertPrivateAcl(fs::u8path(path));
    // A different OS identity cannot even read/open the singleton file. This
    // uses Windows' real anonymous impersonation token, not a source assertion.
    assert(ImpersonateAnonymousToken(GetCurrentThread()));
    HANDLE foreign = CreateFileW(fs::u8path(path).c_str(), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING, 0, nullptr);
    const DWORD foreignError = GetLastError();
    assert(RevertToSelf());
    assert(foreign == INVALID_HANDLE_VALUE && foreignError == ERROR_ACCESS_DENIED);
    assert(!second.acquire(path, err));
    first.release();
    assert(second.acquire(path, err));
    second.release();
    assert(!privateLockHeld(path));
    // A crash releases the OS handle claim; the diagnostic file is not authority.
    std::wstring executable(32768, L'\0');
    const DWORD executableSize = GetModuleFileNameW(nullptr, executable.data(),
                                                   static_cast<DWORD>(executable.size()));
    assert(executableSize && executableSize < executable.size());
    executable.resize(executableSize);
    std::wstring command = L"\"" + executable + L"\" --lock-child \"" +
                           fs::u8path(path).wstring() + L"\"";
    STARTUPINFOW startup{};
    startup.cb = sizeof startup;
    PROCESS_INFORMATION child{};
    assert(CreateProcessW(executable.c_str(), command.data(), nullptr, nullptr, FALSE,
                          CREATE_NO_WINDOW, nullptr, nullptr, &startup, &child));
    CloseHandle(child.hThread);
    const auto claimDeadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
    while (!privateLockHeld(path) && std::chrono::steady_clock::now() < claimDeadline)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    const bool childClaimed = privateLockHeld(path);
    const bool parentRefused = !second.acquire(path, err);
    assert(TerminateProcess(child.hProcess, 0));
    assert(WaitForSingleObject(child.hProcess, 3000) == WAIT_OBJECT_0);
    CloseHandle(child.hProcess);
    assert(childClaimed && parentRefused);
    assert(second.acquire(path, err));
    second.release();
    assert(!privateLockHeld(path));

    // Alias attacks never truncate the target or turn file existence into a claim.
    const auto original = root / "singleton-target.txt";
    const auto alias = root / "Chudware" / "alias.lock";
    assert(privateWrite(original.u8string(), "must-survive"));
    assert(CreateHardLinkW(alias.c_str(), original.c_str(), nullptr));
    assert(!first.acquire(alias.u8string(), err));
    std::string originalText;
    { std::ifstream input(original); input >> originalText; }
    assert(originalText == "must-survive");
    CloseHandle(squat);
    const auto gui = guiLockPath(key);
    assert(gui != path && fs::u8path(gui).parent_path() == root / "Chudware");
    assert(first.acquire(gui, err));
    assert(!second.acquire(gui, err));
    first.release();
    assert(!privateLockHeld(gui));
    const std::string activation = "Local\\chudware-gui-activate-" + fnv1aHex(gui);
    const std::wstring activationName(activation.begin(), activation.end());
    HANDLE event = privateActivationEvent(activationName, true);
    assert(event && ownerPrivateEvent(event));
    HANDLE wake = privateActivationEvent(activationName, false);
    assert(wake && SetEvent(wake));
    assert(WaitForSingleObject(event, 1000) == WAIT_OBJECT_0);
    assert(WaitForSingleObject(event, 0) == WAIT_TIMEOUT);  // auto-reset
    assert(ImpersonateAnonymousToken(GetCurrentThread()));
    HANDLE foreignWake = OpenEventW(EVENT_MODIFY_STATE, FALSE, activationName.c_str());
    const DWORD wakeError = GetLastError();
    assert(RevertToSelf());
    assert(!foreignWake && wakeError == ERROR_ACCESS_DENIED);
    CloseHandle(wake);
    CloseHandle(event);
    // A squatted, permissive event owned even by us is not a private wake lane.
    PSECURITY_DESCRIPTOR broad = nullptr;
    assert(ConvertStringSecurityDescriptorToSecurityDescriptorW(
        L"D:P(A;;GA;;;WD)", SDDL_REVISION_1, &broad, nullptr));
    SECURITY_ATTRIBUTES broadAttributes{sizeof(SECURITY_ATTRIBUTES), broad, FALSE};
    HANDLE squattedEvent = CreateEventW(&broadAttributes, FALSE, FALSE, activationName.c_str());
    LocalFree(broad);
    assert(squattedEvent);
    assert(!privateActivationEvent(activationName, true));
    assert(!privateActivationEvent(activationName, false));
    assert(first.acquire(gui, err));
    assert(privateLockHeld(gui));
    assert(!second.acquire(gui, err));  // foreign wake object never bypasses lock
    first.release();
    assert(!privateLockHeld(gui));
    CloseHandle(squattedEvent);
    // Wrong kernel-object type is unavailable, never singleton authority.
    HANDLE squattedMutex = CreateMutexW(nullptr, FALSE, activationName.c_str());
    assert(squattedMutex && !privateActivationEvent(activationName, true));
    assert(first.acquire(gui, err));
    assert(!second.acquire(gui, err));
    first.release();
    CloseHandle(squattedMutex);

    // Real native pipe exchanges: reply framing, byte cap and absolute deadline.
    const std::string pipeName = "\\\\.\\pipe\\chudware-control-fixture-" + key;
    IpcListener listener;
    assert(listener.listen(pipeName, err));
    IpcListener duplicate;
    assert(!duplicate.listen(pipeName, err));  // first-instance anti-squatting
    std::thread peer([&] {
        auto conn = listener.accept(2000);
        assert(conn.valid());
        std::string request;
        assert(conn.readLine(request, 1000) && request == "status");
        assert(conn.writeLine("{\"ok\":true,\"result\":{}}"));
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    });
    std::string response;
    assert(exchangeLocalPipe(pipeName, "status\n", response, 1024, 1000, err));
    assert(response == "{\"ok\":true,\"result\":{}}");
    peer.join();
    std::thread oversizedPeer([&] {
        auto conn = listener.accept(2000);
        assert(conn.valid());
        std::string request;
        assert(conn.readLine(request, 1000));
        assert(conn.writeLine(std::string(512, 'x')));
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    });
    assert(!exchangeLocalPipe(pipeName, "status\n", response, 256, 1000, err));
    assert(err == "worker control response exceeded limit");
    oversizedPeer.join();
    std::thread silentPeer([&] {
        auto conn = listener.accept(2000);
        assert(conn.valid());
        std::string request;
        assert(conn.readLine(request, 1000));
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
    });
    const auto started = std::chrono::steady_clock::now();
    assert(!exchangeLocalPipe(pipeName, "status\n", response, 1024, 100, err));
    assert(err == "worker control request timed out");
    assert(std::chrono::steady_clock::now() - started < std::chrono::seconds(1));
    silentPeer.join();
    std::thread closedPeer([&] {
        auto conn = listener.accept(2000);
        assert(conn.valid());
        std::string request;
        assert(conn.readLine(request, 1000));
        // An actual close, not a silent open pipe, must surface as EOF promptly.
    });
    const auto closeStarted = std::chrono::steady_clock::now();
    assert(!exchangeLocalPipe(pipeName, "status\n", response, 1024, 2000, err));
    assert(err == "worker control pipe closed");
    assert(std::chrono::steady_clock::now() - closeStarted < std::chrono::seconds(1));
    closedPeer.join();

    // Raw native peers exercise partial replies and write backpressure without
    // relying on the daemon transport's newline-writing convenience method.
    const auto rawName = fs::u8path(pipeName + "-raw");
    local_pipe_detail::Handle raw(CreateNamedPipeW(rawName.c_str(),
        PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
        PIPE_TYPE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        1, 4096, 4096, 0, &privateSecurity().attributes));
    assert(raw.get() != INVALID_HANDLE_VALUE);
    std::thread dripPeer([&] {
        const BOOL connected = ConnectNamedPipe(raw.get(), nullptr);
        assert(connected || GetLastError() == ERROR_PIPE_CONNECTED);
        for (int i = 0; i < 8; ++i) {
            DWORD count = 0;
            if (!WriteFile(raw.get(), "x", 1, &count, nullptr)) break;
            std::this_thread::sleep_for(std::chrono::milliseconds(40));
        }
    });
    const auto dripStarted = std::chrono::steady_clock::now();
    assert(!exchangeLocalPipe(pipeName + "-raw", "status\n", response, 1024, 120, err));
    assert(err == "worker control request timed out");
    assert(std::chrono::steady_clock::now() - dripStarted < std::chrono::seconds(1));
    dripPeer.join();
    assert(DisconnectNamedPipe(raw.get()));

    std::thread nonreader([&] {
        const BOOL connected = ConnectNamedPipe(raw.get(), nullptr);
        assert(connected || GetLastError() == ERROR_PIPE_CONNECTED);
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
    });
    const auto writeStarted = std::chrono::steady_clock::now();
    assert(!exchangeLocalPipe(pipeName + "-raw", std::string(512 * 1024, 'x') + "\n",
                             response, 1024, 100, err));
    assert(err == "worker control request timed out");
    assert(std::chrono::steady_clock::now() - writeStarted < std::chrono::seconds(1));
    nonreader.join();
    assert(DisconnectNamedPipe(raw.get()));
    // Disconnect alone does not make a reused instance available. Re-arm the
    // server and wait for real admission before occupying its only client slot.
    std::thread occupiedPeer([&] {
        const BOOL connected = ConnectNamedPipe(raw.get(), nullptr);
        assert(connected || GetLastError() == ERROR_PIPE_CONNECTED);
    });
    assert(WaitNamedPipeW(rawName.c_str(), 1000));

    local_pipe_detail::Handle occupied(CreateFileW(rawName.c_str(), GENERIC_READ | GENERIC_WRITE,
        0, nullptr, OPEN_EXISTING, FILE_FLAG_OVERLAPPED |
        SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, nullptr));
    assert(occupied.get() != INVALID_HANDLE_VALUE);
    occupiedPeer.join();
    const auto busyStarted = std::chrono::steady_clock::now();
    assert(!exchangeLocalPipe(pipeName + "-raw", "status\n", response, 1024, 100, err));
    assert(err == "worker control request timed out");
    assert(std::chrono::steady_clock::now() - busyStarted < std::chrono::seconds(1));

    // The native listener must recycle a client lost before ConnectNamedPipe.
    auto abandoned = connectTo(pipeName, err);
    assert(abandoned.valid());
    abandoned.close();
    auto gone = listener.accept(5);
    if (gone.valid()) {
        std::string ignored;
        assert(!gone.readLine(ignored, 100) && gone.eof());
        gone.close();
    }
    std::thread recoveredPeer([&] {
        auto conn = listener.accept(2000);
        assert(conn.valid());
        std::string request;
        assert(conn.readLine(request, 1000));
        assert(conn.writeLine("recovered"));
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    });
    assert(exchangeLocalPipe(pipeName, "status\n", response, 1024, 1000, err));
    assert(response == "recovered");
    recoveredPeer.join();

    // accept(timeout) leaves an OVERLAPPED connect alive. close must drain it
    // before freeing its event/heap storage, then allow exclusive-name reuse.
    for (int i = 0; i < 8; ++i) {
        assert(!listener.accept(1).valid());
        const auto cleanupStarted = std::chrono::steady_clock::now();
        listener.close();
        assert(std::chrono::steady_clock::now() - cleanupStarted < std::chrono::seconds(1));
        assert(listener.listen(pipeName, err));
    }
    listener.close();
    IpcListener unsupported;
    assert(!unsupported.adoptListeningFd(3, err));
#else
    assertMode(state, 0700);
    assertMode(logs, 0700);
    assertMode(logs / "secret.json", 0600);
    assertMode(logs / "copy.json", 0600);
    assertMode(logs / "hostd.log", 0600);
    assertMode(logs / "hostd.log.1", 0600);
    assert(::setenv("XDG_DATA_HOME", root.c_str(), 1) == 0);
    assert(stateDir() == (root / "Chudware").string());
    assertMode(root / "Chudware", 0700);
    const auto outside = root / "outside.json";
    { std::ofstream out(outside); out << "must-survive"; }
    fs::create_symlink(outside, logs / "link.json");
    assert(!privateWrite((logs / "link.json").string(), "bad"));
    fs::create_hard_link(outside, logs / "hard.json");
    assert(!privateWrite((logs / "hard.json").string(), "bad"));
    std::string original;
    { std::ifstream in(outside); in >> original; }
    assert(original == "must-survive");
    fs::create_directory_symlink(logs, root / "linked-dir");
    assert(!privateDir((root / "linked-dir").string()));

    const std::string endpoint = (root / "activated.sock").string();
    assert(endpoint.size() < sizeof(sockaddr_un{}.sun_path));
    int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    assert(fd >= 0);
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    std::strcpy(address.sun_path, endpoint.c_str());
    assert(::bind(fd, reinterpret_cast<sockaddr*>(&address), sizeof address) == 0);
    assert(::listen(fd, 4) == 0);
    std::string err;
    {
        IpcListener adopted;
        assert(adopted.adoptListeningFd(fd, err));
        assert(::fcntl(fd, F_GETFD) & FD_CLOEXEC);
        auto client = connectTo(endpoint, err);
        assert(client.valid());
        auto peer = adopted.accept(1000);
        assert(peer.valid());
        assert(client.writeLine("socket-activation"));
        std::string line;
        assert(peer.readLine(line, 1000) && line == "socket-activation");
        assert(adopted.releasePath().empty());
        IpcListener moved = std::move(adopted);
        assert(moved.valid() && !adopted.valid());
    }
    assert(fs::exists(endpoint));  // close/destruction must NOT unlink activation
    assert(::fcntl(fd, F_GETFD) == -1 && errno == EBADF);
    IpcListener rejected;
    fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    assert(!rejected.adoptListeningFd(fd, err));  // socket, but not listening
    assert(::fcntl(fd, F_GETFD) == -1 && errno == EBADF);
    fd = ::open(outside.c_str(), O_RDONLY);
    assert(!rejected.adoptListeningFd(fd, err));
    assert(::fcntl(fd, F_GETFD) == -1 && errno == EBADF);
    fd = ::socket(AF_INET, SOCK_STREAM, 0);
    assert(!rejected.adoptListeningFd(fd, err));
    assert(::fcntl(fd, F_GETFD) == -1 && errno == EBADF);
#endif
    return 0;
}
