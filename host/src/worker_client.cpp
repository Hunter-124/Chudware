#include "worker_client.h"

#include <windows.h>

#include <cstdlib>
#include <string>

#include "bootstrap.h"

using nlohmann::json;

namespace {
std::wstring widen(const std::string& s) {
    if (s.empty()) return L"";
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring w(n, 0);
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
    return w;
}
bool fileExists(const std::string& p) {
    DWORD a = GetFileAttributesA(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}
std::string win32Error(DWORD code) {
    char* msg = nullptr;
    DWORD n = FormatMessageA(
        FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
        nullptr, code, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), (char*)&msg, 0, nullptr);
    std::string s = (n && msg) ? std::string(msg, n) : ("error " + std::to_string(code));
    if (msg) LocalFree(msg);
    while (!s.empty() && (s.back() == '\n' || s.back() == '\r' || s.back() == '.')) s.pop_back();
    return s;
}
}  // namespace

WorkerClient::~WorkerClient() { stop(); }

bool WorkerClient::start(std::string& err) {
    // Resolve via the shared bootstrap logic so the worker, runtimeReady(), and the GUI
    // all agree on which python + app dir to use.
    std::string python = runtimePython();
    std::string repo = appDir();
    readBuf_.clear();  // fresh process => fresh stream (matters when restarting after a repair)

    // Fail with a clear, actionable message *before* CreateProcess so a missing runtime
    // never surfaces as a cryptic "CreateProcess failed".
    if (!fileExists(python)) {
        err = "Python runtime not installed.\nExpected: " + python +
              "\nUse AI-CAD \xe2\x96\xb8 Repair runtime to install it.";
        return false;
    }

    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof(sa);
    sa.bInheritHandle = TRUE;

    HANDLE outRd = nullptr, outWr = nullptr, inRd = nullptr, inWr = nullptr;
    if (!CreatePipe(&outRd, &outWr, &sa, 0) || !CreatePipe(&inRd, &inWr, &sa, 0)) {
        err = "CreatePipe failed";
        return false;
    }
    // The parent's ends must not be inherited by the child.
    SetHandleInformation(outRd, HANDLE_FLAG_INHERIT, 0);
    SetHandleInformation(inWr, HANDLE_FLAG_INHERIT, 0);

    STARTUPINFOW si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = inRd;
    si.hStdOutput = outWr;
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);  // worker diagnostics -> host console

    std::wstring cmd = L"\"" + widen(python) + L"\" -m engine.worker";
    std::wstring cwd = widen(repo);
    std::vector<wchar_t> cmdbuf(cmd.begin(), cmd.end());
    cmdbuf.push_back(0);

    PROCESS_INFORMATION pi{};
    BOOL ok = CreateProcessW(nullptr, cmdbuf.data(), nullptr, nullptr, TRUE, CREATE_NO_WINDOW,
                             nullptr, cwd.empty() ? nullptr : cwd.c_str(), &si, &pi);
    DWORD gle = GetLastError();
    CloseHandle(outWr);
    CloseHandle(inRd);
    if (!ok) {
        err = "Could not launch the Python runtime (" + win32Error(gle) + ").\n" + python +
              "\nUse AI-CAD \xe2\x96\xb8 Repair runtime to reinstall it.";
        CloseHandle(outRd);
        CloseHandle(inWr);
        return false;
    }
    CloseHandle(pi.hThread);
    process_ = pi.hProcess;
    childStdoutRd_ = outRd;
    childStdinWr_ = inWr;
    running_ = true;
    return true;
}

void WorkerClient::stop() {
    if (childStdinWr_) {
        DWORD wrote = 0;
        const char* bye = "{\"id\":0,\"method\":\"shutdown\"}\n";
        WriteFile(childStdinWr_, bye, (DWORD)strlen(bye), &wrote, nullptr);
        CloseHandle(childStdinWr_);
        childStdinWr_ = nullptr;
    }
    if (process_) {
        WaitForSingleObject(process_, 2000);
        TerminateProcess(process_, 0);
        CloseHandle(process_);
        process_ = nullptr;
    }
    if (childStdoutRd_) {
        CloseHandle(childStdoutRd_);
        childStdoutRd_ = nullptr;
    }
    running_ = false;
}

bool WorkerClient::readLine(std::string& line, int timeoutMs) {
    ULONGLONG start = GetTickCount64();
    for (;;) {
        size_t nl = readBuf_.find('\n');
        if (nl != std::string::npos) {
            line = readBuf_.substr(0, nl);
            readBuf_.erase(0, nl + 1);
            return true;
        }
        if ((int)(GetTickCount64() - start) > timeoutMs) return false;

        DWORD avail = 0;
        if (!PeekNamedPipe(childStdoutRd_, nullptr, 0, nullptr, &avail, nullptr)) return false;
        if (avail == 0) {
            if (WaitForSingleObject(process_, 0) == WAIT_OBJECT_0) {
                // process exited; drain whatever is buffered one last time
                if (!PeekNamedPipe(childStdoutRd_, nullptr, 0, nullptr, &avail, nullptr) ||
                    avail == 0)
                    return false;
            } else {
                Sleep(5);
                continue;
            }
        }
        std::string chunk(avail, 0);
        DWORD got = 0;
        if (!ReadFile(childStdoutRd_, chunk.data(), avail, &got, nullptr) || got == 0)
            return false;
        readBuf_.append(chunk.data(), got);
    }
}

bool WorkerClient::call(const std::string& method, const json& params, json& result,
                        std::string& err, int timeoutMs, const EventFn& onEvent) {
    std::lock_guard<std::mutex> lk(callMtx_);
    if (!running_) {
        err = "worker not started";
        return false;
    }
    int id = nextId_++;
    json req = {{"id", id}, {"method", method}, {"params", params}};
    std::string line = req.dump() + "\n";
    DWORD wrote = 0;
    if (!WriteFile(childStdinWr_, line.data(), (DWORD)line.size(), &wrote, nullptr)) {
        err = "write failed";
        return false;
    }
    for (;;) {
        std::string resp;
        if (!readLine(resp, timeoutMs)) {
            err = "timeout/closed waiting for response";
            return false;
        }
        if (resp.empty()) continue;
        json j;
        try {
            j = json::parse(resp);
        } catch (...) {
            continue;  // ignore non-JSON noise on the channel
        }
        if (!j.contains("id") || j["id"].is_null() || j["id"].get<int>() != id) continue;
        // Streamed progress notification (no "ok"): deliver and keep waiting.
        if (j.contains("event") && !j.contains("ok")) {
            if (onEvent) onEvent(j.value("data", json::object()));
            continue;
        }
        if (j.value("ok", false)) {
            result = j.value("result", json::object());
            return true;
        }
        err = j.value("error", "unknown rpc error");
        return false;
    }
}
