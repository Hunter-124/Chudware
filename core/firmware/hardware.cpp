#include "firmware/hardware.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <stdexcept>

#include "firmware/private_storage.h"
#include "firmware/subprocess.h"

#include <cerrno>
#include <cstring>
#include <mutex>
#include <unordered_map>

#if !defined(_WIN32)
#include <fcntl.h>
#include <signal.h>
#include <sys/wait.h>
#if defined(__linux__)
#include <sys/syscall.h>
#endif
#include <termios.h>
#include <unistd.h>
#endif

namespace chudware::firmware {
namespace {

constexpr size_t kOutputCap = 32000;

std::string cap(const std::string& text) {
    if (text.size() <= kOutputCap) return text;
    return text.substr(0, kOutputCap) + "\n\xE2\x80\xA6[truncated]";
}

double elapsed_ms(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
}

FlashResult not_found(const std::string& tool, const std::vector<std::string>& cmd,
                      std::chrono::steady_clock::time_point t0) {
    FlashResult r;
    r.success = false;
    r.returncode = -1;
    r.cmd = cmd;
    r.output = "tool_not_found: '" + tool + "' is not installed / not on PATH";
    r.ms = elapsed_ms(t0);
    return r;
}

struct EmulatorWorkspace {
    std::filesystem::path directory = private_temp_directory("firmware-emulator");

    ~EmulatorWorkspace() {
        std::error_code ignored;
        std::filesystem::remove_all(directory, ignored);
    }

    std::string copy_input(const std::string& source, const char* name, size_t budget) {
        const auto destination = directory / name;
        private_storage_detail::atomic_write(
            destination, private_storage_detail::read_file(source, budget));
        return destination.string();
    }
};

// hardware._run equivalent: returns (returncode, combined capped output).
FlashResult run_flash(const std::vector<std::string>& cmd, double timeout_s,
                      std::chrono::steady_clock::time_point t0) {
    FlashResult r;
    r.cmd = cmd;
    RunResult rr = run_capture(cmd, "", timeout_s);
    if (rr.not_found) {
        r.returncode = -1;
        r.output = "tool_not_found: executable '" + cmd[0] + "' not on PATH";
    } else if (rr.timed_out) {
        r.returncode = -1;
        r.output = "timeout: '" + cmd[0] + "' exceeded " +
                   std::to_string(static_cast<long long>(timeout_s)) + "s";
    } else {
        r.returncode = rr.returncode;
        r.output = cap(rr.stdout_text + rr.stderr_text);
    }
    r.success = (r.returncode == 0);
    r.ms = elapsed_ms(t0);
    return r;
}

// Bounded free-run of an emulator that never self-exits (simavr/renode): a wall-clock
// timeout at `run_s` is the EXPECTED stop, not a failure.
FlashResult run_bounded_emulate(const std::vector<std::string>& cmd, const std::string& cwd,
                                double run_s, std::chrono::steady_clock::time_point t0) {
    FlashResult r;
    r.cmd = cmd;
    RunResult rr = run_capture(cmd, cwd, run_s, true, kOutputCap);
    if (rr.not_found) {
        r.returncode = -1;
        r.success = false;
        r.output = "tool_not_found: executable '" + cmd[0] + "' not on PATH";
    } else if (rr.output_limit_exceeded) {
        r.returncode = -1;
        r.success = false;
        r.output = "output_limit_exceeded: emulator output exceeded its capture budget\n" +
                   cap(rr.stdout_text + rr.stderr_text);
    } else if (rr.timed_out) {
        r.returncode = 0;
        r.success = true;
        r.output = cap(rr.stdout_text + rr.stderr_text) + "\n[stopped after " +
                   std::to_string(run_s) + "s run window]";
    } else {
        r.returncode = rr.returncode;
        r.success = (rr.returncode == 0);
        r.output = cap(rr.stdout_text + rr.stderr_text);
    }
    r.ms = elapsed_ms(t0);
    return r;
}

#if !defined(_WIN32)
// Launches `argv` as a detached background child (stdio -> /dev/null, own session), never
// waited on by the parent except via stop_background(). Returns the child pid, or -1 on a
// fork() failure.
pid_t spawn_background(const std::vector<std::string>& argv, const std::string& cwd,
                       bool minimal_env) {
    const std::string executable = std::filesystem::absolute(which(argv[0])).string();
    std::vector<char*> cargv;
    cargv.reserve(argv.size() + 1);
    for (const auto& s : argv) cargv.push_back(const_cast<char*>(s.c_str()));
    cargv.push_back(nullptr);
    std::vector<std::string> environment;
    std::vector<char*> envp;
    if (minimal_env) {
        const char* path = std::getenv("PATH");
        environment = {"PATH=" + std::string(path ? path : ""),
                       "HOME=" + cwd, "TMPDIR=" + cwd};
        for (auto& value : environment) envp.push_back(value.data());
        envp.push_back(nullptr);
    }
    const long descriptor_limit = minimal_env ? ::sysconf(_SC_OPEN_MAX) : 0;
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0) {
        int devnull = ::open("/dev/null", O_RDWR);
        if (devnull < 0) _exit(127);
        if (::dup2(devnull, STDIN_FILENO) < 0 ||
            ::dup2(devnull, STDOUT_FILENO) < 0 ||
            ::dup2(devnull, STDERR_FILENO) < 0) _exit(127);
        if (devnull > STDERR_FILENO) ::close(devnull);
        if (minimal_env) {
            bool descriptors_closed = false;
#if defined(__linux__) && defined(SYS_close_range)
            descriptors_closed = (::syscall(SYS_close_range, 3u, ~0u, 0u) == 0);
#endif
            if (!descriptors_closed) {
                if (descriptor_limit < 0) _exit(127);
                for (long fd = 3; fd < descriptor_limit; ++fd) ::close(static_cast<int>(fd));
            }
        }
        if (!cwd.empty() && ::chdir(cwd.c_str()) != 0) _exit(127);
        if (::setsid() < 0) _exit(127);
        if (minimal_env)
            ::execve(executable.c_str(), cargv.data(), envp.data());
        else
            ::execvp(cargv[0], cargv.data());
        _exit(127);
    }
    return pid;
}

// SIGTERM, then SIGKILL after ~1s grace, reaping either way. No-op for pid <= 0.
void stop_background(pid_t pid) {
    if (pid <= 0) return;
    ::kill(pid, SIGTERM);
    ::kill(-pid, SIGTERM);
    int status = 0;
    for (int i = 0; i < 20; ++i) {
        const pid_t waited = ::waitpid(pid, &status, WNOHANG);
        if (waited == pid || (waited < 0 && errno == ECHILD)) {
            ::kill(-pid, SIGKILL);
            return;
        }
        ::usleep(50000);
    }
    ::kill(pid, SIGKILL);
    ::kill(-pid, SIGKILL);
    while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
}

struct BackgroundChild {
    pid_t pid = -1;
    ~BackgroundChild() { stop_background(pid); }
};

speed_t baud_to_speed(int baud) {
    switch (baud) {
        case 300: return B300;
        case 1200: return B1200;
        case 2400: return B2400;
        case 4800: return B4800;
        case 9600: return B9600;
        case 19200: return B19200;
        case 38400: return B38400;
        case 57600: return B57600;
        case 115200: return B115200;
        case 230400: return B230400;
#ifdef B460800
        case 460800: return B460800;
#endif
#ifdef B921600
        case 921600: return B921600;
#endif
        default: return B115200;
    }
}

// Live serial handle registry (process-local; valid for the hosting process's lifetime).
struct SerialHandleImpl {
    int fd = -1;
    std::string port;
    int baud = 0;
};
std::mutex g_serial_mutex;
std::unordered_map<int, SerialHandleImpl> g_serial_handles;
int g_next_serial_handle = 1;
#endif  // !_WIN32

}  // namespace

nlohmann::json FlashResult::to_dict() const {
    return {
        {"success", success},
        {"output", output},
        {"returncode", returncode},
        {"cmd", cmd},
        {"ms", std::round(ms * 100.0) / 100.0},
    };
}

FlashResult probe_rs_flash(const std::string& elf_path, const std::string& chip,
                           double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {"probe-rs", "download", "--chip", chip, elf_path};
    if (which("probe-rs").empty()) return not_found("probe-rs", cmd, t0);
    return run_flash(cmd, timeout_s, t0);
}

FlashResult probe_rs_reset(const std::string& chip, double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {"probe-rs", "reset", "--chip", chip};
    if (which("probe-rs").empty()) return not_found("probe-rs", cmd, t0);
    return run_flash(cmd, timeout_s, t0);
}

FlashResult probe_rs_halt(const std::string& chip, double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {"probe-rs", "reset", "--chip", chip, "--halt"};
    if (which("probe-rs").empty()) return not_found("probe-rs", cmd, t0);
    return run_flash(cmd, timeout_s, t0);
}

std::vector<uint8_t> probe_rs_read_memory(long long address, long long length,
                                          const std::string& chip, double timeout_s) {
    if (which("probe-rs").empty()) {
        throw std::runtime_error("tool_not_found: 'probe-rs' is not installed / not on PATH");
    }
    char addr_buf[32];
    std::snprintf(addr_buf, sizeof(addr_buf), "0x%llx", address);
    std::vector<std::string> cmd = {"probe-rs", "read", "--chip", chip, addr_buf,
                                    std::to_string(length)};
    RunResult rr = run_capture(cmd, "", timeout_s);
    if (rr.returncode != 0) {
        throw std::runtime_error("probe-rs read failed (rc=" + std::to_string(rr.returncode) +
                                 "): " + cap(rr.stdout_text + rr.stderr_text));
    }
    // probe-rs outputs hex bytes; parse them back to raw bytes. First try the
    // whitespace-stripped blob; on failure fall back to hex-chars-only.
    std::string output = rr.stdout_text + rr.stderr_text;
    auto from_hex = [](const std::string& h) -> std::vector<uint8_t> {
        std::vector<uint8_t> out;
        if (h.size() % 2) return {};
        out.reserve(h.size() / 2);
        for (size_t i = 0; i + 1 < h.size(); i += 2) {
            auto nib = [](char c) -> int {
                if (c >= '0' && c <= '9') return c - '0';
                if (c >= 'a' && c <= 'f') return c - 'a' + 10;
                if (c >= 'A' && c <= 'F') return c - 'A' + 10;
                return -1;
            };
            int hi = nib(h[i]), lo = nib(h[i + 1]);
            if (hi < 0 || lo < 0) return {};
            out.push_back(static_cast<uint8_t>((hi << 4) | lo));
        }
        return out;
    };
    std::string squeezed;
    for (char c : output)
        if (!std::isspace(static_cast<unsigned char>(c))) squeezed += c;
    std::vector<uint8_t> data = from_hex(squeezed);
    if (data.empty() && !squeezed.empty()) {
        std::string hex_only;
        for (char c : output)
            if (std::isxdigit(static_cast<unsigned char>(c))) hex_only += c;
        if (hex_only.size() % 2) hex_only.pop_back();
        data = from_hex(hex_only);
    }
    return data;
}

FlashResult esptool_flash(const std::string& bin_path, const std::string& port, int baud,
                          const std::string& flash_offset, double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {
        "esptool.py", "--port", port, "--baud", std::to_string(baud),
        "write_flash", flash_offset, bin_path,
    };
    if (which("esptool.py").empty()) return not_found("esptool.py", cmd, t0);
    return run_flash(cmd, timeout_s, t0);
}

FlashResult avrdude_flash(const std::string& hex_path, const std::string& mcu,
                          const std::string& programmer, const std::string& port, int baud,
                          double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {"avrdude", "-c", programmer.empty() ? "arduino" : programmer,
                                    "-p", mcu};
    if (!port.empty()) {
        cmd.push_back("-P");
        cmd.push_back(port);
    }
    if (baud > 0) {
        cmd.push_back("-b");
        cmd.push_back(std::to_string(baud));
    }
    cmd.push_back("-U");
    cmd.push_back("flash:w:" + hex_path + ":i");
    if (which("avrdude").empty()) return not_found("avrdude", cmd, t0);
    return run_flash(cmd, timeout_s, t0);
}

#if defined(_WIN32)

static FlashResult gdb_mi_session_in_directory(
    const std::vector<std::string>&, const std::string&,
    const std::vector<std::string>&, double, const std::string&, double,
    const std::string&, bool) {
    FlashResult r;
    r.success = false;
    r.returncode = -1;
    r.output = "unsupported_platform: gdb/MI background-process debug driving is POSIX-only in this build";
    return r;
}

int serial_open(const std::string&, int, std::string& err) {
    err = "unsupported_platform: live serial monitor is POSIX-only in this build";
    return -1;
}
std::string serial_read(int, size_t) {
    throw std::runtime_error("unsupported_platform: live serial monitor is POSIX-only in this build");
}
size_t serial_write(int, const std::string&) {
    throw std::runtime_error("unsupported_platform: live serial monitor is POSIX-only in this build");
}
bool serial_close(int) { return false; }

#else  // POSIX

static FlashResult gdb_mi_session_in_directory(
    const std::vector<std::string>& server_argv, const std::string& remote_target,
    const std::vector<std::string>& mi_commands, double timeout_s,
    const std::string& gdb_binary, double startup_delay_s,
    const std::string& cwd, bool minimal_env) {
    auto t0 = std::chrono::steady_clock::now();
    if (!server_argv.empty() && which(server_argv[0]).empty())
        return not_found(server_argv[0], server_argv, t0);
    const auto gdb_executable = which(gdb_binary);
    if (gdb_executable.empty())
        return not_found(gdb_binary, {gdb_binary, "--batch", "--interpreter=mi2"}, t0);

    BackgroundChild server;
    if (!server_argv.empty()) {
        server.pid = spawn_background(server_argv, cwd, minimal_env);
        if (server.pid < 0) {
            FlashResult r;
            r.success = false;
            r.returncode = -1;
            r.cmd = server_argv;
            r.output = "spawn_failed: could not fork '" + server_argv[0] + "'";
            r.ms = elapsed_ms(t0);
            return r;
        }
        ::usleep(static_cast<useconds_t>(std::max(0.0, startup_delay_s) * 1e6));
    }

    std::vector<std::string> gdb_argv = {
        minimal_env ? std::filesystem::absolute(gdb_executable).string() : gdb_binary,
        "--batch", "--interpreter=mi2"};
    if (minimal_env) gdb_argv.push_back("--nx");
    gdb_argv.push_back("-ex");
    gdb_argv.push_back("target remote " + remote_target);
    for (const auto& c : mi_commands) {
        gdb_argv.push_back("-ex");
        gdb_argv.push_back(c);
    }
    gdb_argv.push_back("-ex");
    gdb_argv.push_back("detach");
    gdb_argv.push_back("-ex");
    gdb_argv.push_back("quit");

    RunResult rr = run_capture(gdb_argv, cwd, timeout_s, minimal_env, kOutputCap);

    FlashResult r;
    r.cmd = gdb_argv;
    if (rr.not_found) {
        r.returncode = -1;
        r.output = "tool_not_found: executable '" + gdb_argv[0] + "' not on PATH";
    } else if (rr.output_limit_exceeded) {
        r.returncode = -1;
        r.output = "output_limit_exceeded: GDB output exceeded its capture budget\n" +
                   cap(rr.stdout_text + rr.stderr_text);
    } else if (rr.timed_out) {
        r.returncode = -1;
        r.output = "timeout: '" + gdb_argv[0] + "' exceeded " +
                   std::to_string(static_cast<long long>(timeout_s)) + "s";
    } else {
        r.returncode = rr.returncode;
        r.output = cap(rr.stdout_text + rr.stderr_text);
    }
    r.success = (r.returncode == 0);
    r.ms = elapsed_ms(t0);
    return r;
}

int serial_open(const std::string& port, int baud, std::string& err) {
    int fd = ::open(port.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK);
    if (fd < 0) {
        err = std::string("open_failed: ") + std::strerror(errno);
        return -1;
    }
    termios tio{};
    if (::tcgetattr(fd, &tio) != 0) {
        err = std::string("tcgetattr_failed: ") + std::strerror(errno);
        ::close(fd);
        return -1;
    }
    ::cfmakeraw(&tio);
    speed_t speed = baud_to_speed(baud > 0 ? baud : 115200);
    ::cfsetispeed(&tio, speed);
    ::cfsetospeed(&tio, speed);
    tio.c_cflag |= (CLOCAL | CREAD);
    tio.c_cflag &= ~PARENB;
    tio.c_cflag &= ~CSTOPB;
    tio.c_cflag &= ~CSIZE;
    tio.c_cflag |= CS8;
    tio.c_cc[VMIN] = 0;
    tio.c_cc[VTIME] = 0;
    if (::tcsetattr(fd, TCSANOW, &tio) != 0) {
        err = std::string("tcsetattr_failed: ") + std::strerror(errno);
        ::close(fd);
        return -1;
    }
    ::tcflush(fd, TCIOFLUSH);

    std::lock_guard<std::mutex> lock(g_serial_mutex);
    int handle = g_next_serial_handle++;
    g_serial_handles[handle] = SerialHandleImpl{fd, port, baud > 0 ? baud : 115200};
    return handle;
}

std::string serial_read(int handle, size_t max_bytes) {
    int fd;
    {
        std::lock_guard<std::mutex> lock(g_serial_mutex);
        auto it = g_serial_handles.find(handle);
        if (it == g_serial_handles.end())
            throw std::runtime_error("unknown_handle: serial handle " + std::to_string(handle) +
                                     " is not open");
        fd = it->second.fd;
    }
    std::string out;
    out.resize(max_bytes);
    ssize_t n = ::read(fd, out.data(), max_bytes);
    if (n < 0) {
        if (errno == EAGAIN || errno == EWOULDBLOCK) return "";
        throw std::runtime_error(std::string("read_failed: ") + std::strerror(errno));
    }
    out.resize(static_cast<size_t>(n));
    return out;
}

size_t serial_write(int handle, const std::string& data) {
    int fd;
    {
        std::lock_guard<std::mutex> lock(g_serial_mutex);
        auto it = g_serial_handles.find(handle);
        if (it == g_serial_handles.end())
            throw std::runtime_error("unknown_handle: serial handle " + std::to_string(handle) +
                                     " is not open");
        fd = it->second.fd;
    }
    ssize_t n = ::write(fd, data.data(), data.size());
    if (n < 0) throw std::runtime_error(std::string("write_failed: ") + std::strerror(errno));
    return static_cast<size_t>(n);
}

bool serial_close(int handle) {
    std::lock_guard<std::mutex> lock(g_serial_mutex);
    auto it = g_serial_handles.find(handle);
    if (it == g_serial_handles.end()) return false;
    ::close(it->second.fd);
    g_serial_handles.erase(it);
    return true;
}

#endif  // !_WIN32

FlashResult gdb_mi_session(const std::vector<std::string>& server_argv,
                           const std::string& remote_target,
                           const std::vector<std::string>& mi_commands, double timeout_s,
                           const std::string& gdb_binary, double startup_delay_s) {
    return gdb_mi_session_in_directory(server_argv, remote_target, mi_commands, timeout_s,
                                       gdb_binary, startup_delay_s, "", false);
}

FlashResult openocd_gdb_mi(const std::string& openocd_cfg, const std::vector<std::string>& mi_commands,
                           double timeout_s, const std::string& gdb_binary) {
    return gdb_mi_session({"openocd", "-f", openocd_cfg}, "localhost:3333", mi_commands, timeout_s,
                          gdb_binary, 0.4);
}

FlashResult openocd_gdb_reset(const std::string& openocd_cfg, bool halt, double timeout_s) {
    return openocd_gdb_mi(openocd_cfg, {std::string("monitor reset ") + (halt ? "halt" : "run")},
                          timeout_s);
}

FlashResult openocd_gdb_halt(const std::string& openocd_cfg, double timeout_s) {
    return openocd_gdb_mi(openocd_cfg, {"-exec-interrupt", "monitor halt"}, timeout_s);
}

FlashResult openocd_gdb_step(const std::string& openocd_cfg, bool step_over, double timeout_s) {
    return openocd_gdb_mi(
        openocd_cfg, {step_over ? "-exec-next-instruction" : "-exec-step-instruction"}, timeout_s);
}

FlashResult openocd_gdb_set_breakpoint(const std::string& openocd_cfg, const std::string& address,
                                       double timeout_s) {
    return openocd_gdb_mi(openocd_cfg, {"-break-insert *" + address}, timeout_s);
}

FlashResult openocd_gdb_read_mem(const std::string& openocd_cfg, const std::string& address,
                                 long long length, double timeout_s) {
    return openocd_gdb_mi(
        openocd_cfg, {"-data-read-memory-bytes " + address + " " + std::to_string(length)}, timeout_s);
}

FlashResult simavr_run(const std::string& elf_path, const std::string& mcu, double run_s,
                       int gdb_port, const std::vector<std::string>& gdb_commands, double timeout_s,
                       const std::string& gdb_binary) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {"simavr", "-m", mcu, elf_path};
    const auto executable = which("simavr");
    if (executable.empty()) return not_found("simavr", cmd, t0);
    try {
        EmulatorWorkspace workspace;
        cmd[0] = std::filesystem::absolute(executable).string();
        cmd.back() = workspace.copy_input(elf_path, "firmware.elf", 64 * 1024 * 1024);
        if (!gdb_commands.empty()) {
            cmd.insert(cmd.end() - 1, "-g");
            return gdb_mi_session_in_directory(
                cmd, "localhost:" + std::to_string(gdb_port), gdb_commands,
                timeout_s, gdb_binary, 0.4, workspace.directory.string(), true);
        }
        return run_bounded_emulate(cmd, workspace.directory.string(), run_s, t0);
    } catch (const std::exception& exc) {
        FlashResult result;
        result.returncode = -1;
        result.cmd = cmd;
        result.output = "emulator_input_refused: " + std::string(exc.what());
        result.ms = elapsed_ms(t0);
        return result;
    }
}

FlashResult renode_run(const std::string& resc_path, double run_s, int gdb_port,
                       const std::vector<std::string>& gdb_commands, double timeout_s,
                       const std::string& gdb_binary) {
    auto t0 = std::chrono::steady_clock::now();
    std::vector<std::string> cmd = {"renode", "--disable-xwt", "--console", resc_path};
    const auto executable = which("renode");
    if (executable.empty()) return not_found("renode", cmd, t0);
    try {
        EmulatorWorkspace workspace;
        cmd[0] = std::filesystem::absolute(executable).string();
        cmd.back() = workspace.copy_input(resc_path, "scenario.resc", 4 * 1024 * 1024);
        if (!gdb_commands.empty()) {
            return gdb_mi_session_in_directory(
                cmd, "localhost:" + std::to_string(gdb_port), gdb_commands,
                timeout_s, gdb_binary, 1.5, workspace.directory.string(), true);
        }
        return run_bounded_emulate(cmd, workspace.directory.string(), run_s, t0);
    } catch (const std::exception& exc) {
        FlashResult result;
        result.returncode = -1;
        result.cmd = cmd;
        result.output = "emulator_input_refused: " + std::string(exc.what());
        result.ms = elapsed_ms(t0);
        return result;
    }
}

}  // namespace chudware::firmware
