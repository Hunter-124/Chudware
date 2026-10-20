#include "manufacturing/gateway/transport_serial.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cerrno>
#include <cctype>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <string_view>
#include <thread>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <setupapi.h>
#else
#include <fcntl.h>
#include <poll.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <sys/stat.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/sysmacros.h>
#endif
#endif

namespace chudware::mfg::gateway {
namespace {

bool virtualSerialAddress(std::string_view address) {
    if (address.starts_with("/dev/pts/") || address.starts_with("pty:") ||
        address.starts_with("loopback:") || address.starts_with("replay:")) return true;
#ifdef __APPLE__
    // Darwin PTY slaves are /dev/ttysNNN (real serial devices use /dev/tty.* or
    // /dev/cu.*). Treating a test/emulator PTY as probed physical evidence
    // would let a caller's endpoint_is_physical flag overstate its provenance.
    if (address.starts_with("/dev/ttys")) return true;
#endif
    std::string lowered(address);
    std::transform(lowered.begin(), lowered.end(), lowered.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return lowered.find("fake") != std::string::npos || lowered.find("loopback") != std::string::npos ||
           lowered.find("replay") != std::string::npos;
}

std::optional<std::uint32_t> parseBaud(std::string_view value) {
    std::uint32_t baud = 0;
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), baud);
    if (error != std::errc{} || end != value.data() + value.size() || baud == 0) return std::nullopt;
    return baud;
}

std::string bytesToString(const std::vector<std::byte>& bytes) {
    return std::string(reinterpret_cast<const char*>(bytes.data()), bytes.size());
}

#ifdef _WIN32
std::wstring widenAscii(std::string_view text) {
    return std::wstring(text.begin(), text.end());
}

std::string narrowAscii(std::wstring_view text) {
    std::string value;
    value.reserve(text.size());
    for (wchar_t ch : text) value.push_back(ch <= 0x7f ? static_cast<char>(ch) : '?');
    return value;
}

std::string windowsMessage(DWORD error) {
    return "Windows error " + std::to_string(error);
}

bool windowsDeviceLost(DWORD error) {
    return error == ERROR_ACCESS_DENIED || error == ERROR_DEVICE_NOT_CONNECTED ||
           error == ERROR_INVALID_HANDLE || error == ERROR_GEN_FAILURE ||
           error == ERROR_OPERATION_ABORTED || error == ERROR_BAD_COMMAND;
}
#else
std::optional<speed_t> termiosBaud(std::uint32_t baud) {
    switch (baud) {
        case 1200: return B1200;
        case 2400: return B2400;
        case 4800: return B4800;
        case 9600: return B9600;
        case 19200: return B19200;
        case 38400: return B38400;
        case 57600: return B57600;
        case 115200: return B115200;
#ifdef B230400
        case 230400: return B230400;
#endif
#ifdef B460800
        case 460800: return B460800;
#endif
#ifdef B921600
        case 921600: return B921600;
#endif
        default: return std::nullopt;
    }
}

bool pathWithin(const std::filesystem::path& path, const std::filesystem::path& root) {
    std::error_code error;
    const auto absolute_path = std::filesystem::absolute(path, error).lexically_normal();
    if (error) return false;
    const auto absolute_root = std::filesystem::absolute(root, error).lexically_normal();
    if (error) return false;
    auto path_part = absolute_path.begin();
    for (auto root_part = absolute_root.begin(); root_part != absolute_root.end(); ++root_part) {
        if (path_part == absolute_path.end() || *path_part != *root_part) return false;
        ++path_part;
    }
    return true;
}

// Resolve only commissioned by-id aliases. Every component of the resulting
// device path is opened without following symlinks, not just the final node.
int openPosixSerialTarget(const std::filesystem::path& target) {
    const auto absolute = std::filesystem::absolute(target).lexically_normal();
    int directory = ::open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directory < 0) return -1;
    for (auto part = ++absolute.begin(); part != absolute.end(); ++part) {
        auto next = part;
        ++next;
        const int flags = next == absolute.end()
                              ? O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW
                              : O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW;
        const int opened = ::openat(directory, part->c_str(), flags);
        const int error = errno;
        ::close(directory);
        if (opened < 0) {
            errno = error;
            return -1;
        }
        directory = opened;
    }
    return directory;
}

bool resolvePosixSerialTarget(const std::filesystem::path& address,
                              const SerialTransportOptions& options,
                              std::filesystem::path& target, std::string& why) {
    std::error_code error;
    const auto status = std::filesystem::symlink_status(address, error);
    if (error) {
        why = "serial path inspection failed: " + error.message();
        return false;
    }
    if (!std::filesystem::is_symlink(status)) {
        target = address;
        return true;
    }
    if (!pathWithin(address, options.posix_by_id_root)) {
        why = "serial symlink refused: path is outside the commissioned by-id directory";
        return false;
    }
    target = std::filesystem::canonical(address, error);
    if (error) {
        why = "serial by-id resolution refused: " + error.message();
        return false;
    }
    return true;
}

std::optional<std::string> readSysfsAttribute(const std::filesystem::path& path) {
    std::ifstream input(path);
    if (!input.is_open()) return std::nullopt;
    std::string value;
    std::getline(input, value);
    while (!value.empty() && std::isspace(static_cast<unsigned char>(value.back())))
        value.pop_back();
    const auto begin = std::find_if_not(value.begin(), value.end(), [](unsigned char ch) {
        return std::isspace(ch);
    });
    value.erase(value.begin(), begin);
    if (value.empty()) return std::nullopt;
    return value;
}

struct PosixUsbIdentity {
    EndpointIdentity endpoint;
    unsigned device_major = 0;
    unsigned device_minor = 0;
};

bool deviceNumberMatches(const struct stat& status, const PosixUsbIdentity& identity) {
    return S_ISCHR(status.st_mode) &&
           static_cast<unsigned>(major(status.st_rdev)) == identity.device_major &&
           static_cast<unsigned>(minor(status.st_rdev)) == identity.device_minor;
}

std::optional<PosixUsbIdentity> posixUsbIdentity(
    std::string_view commissioned_address, const std::filesystem::path& target,
    const SerialTransportOptions& options, std::string& why) {
    const auto tty_path = std::filesystem::path(options.posix_sysfs_tty_root) / target.filename();
    const auto device_number = readSysfsAttribute(tty_path / "dev");
    PosixUsbIdentity identity;
    if (!device_number) {
        why = "serial USB identity refused: sysfs device number is unavailable";
        return std::nullopt;
    }
    const auto colon = device_number->find(':');
    if (colon == std::string::npos) {
        why = "serial USB identity refused: sysfs device number is invalid";
        return std::nullopt;
    }
    const char* begin = device_number->data();
    const char* end = begin + device_number->size();
    const auto [major_end, major_error] =
        std::from_chars(begin, begin + colon, identity.device_major);
    const auto [minor_end, minor_error] =
        std::from_chars(begin + colon + 1, end, identity.device_minor);
    if (major_error != std::errc{} || major_end != begin + colon ||
        minor_error != std::errc{} || minor_end != end) {
        why = "serial USB identity refused: sysfs device number is invalid";
        return std::nullopt;
    }
    std::filesystem::path cursor = tty_path / "device";
    std::error_code error;
    if (!std::filesystem::exists(cursor, error) || error) {
        why = "serial USB identity refused: sysfs device path is unavailable";
        return std::nullopt;
    }
    cursor = std::filesystem::canonical(cursor, error);
    if (error) {
        why = "serial USB identity refused: sysfs device path cannot be resolved";
        return std::nullopt;
    }
    for (unsigned depth = 0; depth < 16; ++depth) {
        const auto vid = readSysfsAttribute(cursor / "idVendor");
        const auto pid = readSysfsAttribute(cursor / "idProduct");
        const auto serial = readSysfsAttribute(cursor / "serial");
        const unsigned present = static_cast<unsigned>(vid.has_value()) +
                                 static_cast<unsigned>(pid.has_value()) +
                                 static_cast<unsigned>(serial.has_value());
        if (present != 0) {
            if (present != 3) {
                why = "serial USB identity refused: sysfs USB tuple is incomplete";
                return std::nullopt;
            }
            identity.endpoint = EndpointIdentity::serial(
                std::string(commissioned_address), *vid, *pid, *serial);
            if (!identity.endpoint.valid(&why)) return std::nullopt;
            return identity;
        }
        const auto parent = cursor.parent_path();
        if (parent == cursor || parent.empty()) break;
        cursor = parent;
    }
    why = "serial USB identity refused: sysfs USB tuple is unavailable";
    return std::nullopt;
}

std::string usbTuple(const EndpointIdentity& identity) {
    return identity.usb_vid + ":" + identity.usb_pid + ":" + identity.usb_serial;
}
#endif

}  // namespace

SerialTransport::SerialTransport(DeviceAllowlist& allowlist, device::MachineClass machine_class,
                                 SerialTransportOptions options)
    : allowlist_(allowlist), machine_class_(machine_class), options_(std::move(options)) {
    if (options_.max_read_bytes == 0) options_.max_read_bytes = 1;
}

SerialTransport::~SerialTransport() { close(); }

FreshSource SerialTransport::evidenceSourceForEndpoint(const device::Endpoint& endpoint) {
    return endpoint.endpoint_is_physical && !virtualSerialAddress(endpoint.address)
               ? FreshSource::Probed
               : FreshSource::Simulated;
}

device::OpenResult SerialTransport::open(const device::Endpoint& endpoint) {
    close();
    if (endpoint.address.empty()) return {false, "serial endpoint address is empty"};

    EndpointIdentity identity = EndpointIdentity::serial(endpoint.address);
#ifdef _WIN32
    for (const auto& candidate : enumerate()) {
        if (candidate.identity.address == identity.address) {
            identity = candidate.identity;
            break;
        }
    }
#else
    std::filesystem::path open_target;
    std::string identity_why;
    if (!resolvePosixSerialTarget(identity.address, options_, open_target, identity_why))
        return {false, std::move(identity_why)};

    const auto commissioned = std::find_if(
        allowlist_.entries().begin(), allowlist_.entries().end(),
        [&](const AllowlistEntry& entry) {
            return entry.endpoint.transport == device::TransportKind::Serial &&
                   entry.endpoint.address == identity.address &&
                   entry.machine_class == machine_class_;
        });
    if (commissioned == allowlist_.entries().end())
        return {false, allowlist_.authorize({identity, machine_class_,
                    {{"baud", std::to_string(options_.baud)}}, options_.operator_note}).why};
    if (!commissioned->endpoint.valid(&identity_why))
        return {false, "commissioning refused: " + identity_why};
    struct stat validated_status {};
    if (::lstat(open_target.c_str(), &validated_status) != 0)
        return {false, "serial path validation failed: " + std::string(std::strerror(errno))};
    if (!S_ISCHR(validated_status.st_mode))
        return {false, "serial open refused: endpoint is not a character device"};
    const auto observed = posixUsbIdentity(identity.address, open_target, options_, identity_why);
    if (!observed) return {false, std::move(identity_why)};
    if (!deviceNumberMatches(validated_status, *observed))
        return {false, "serial USB identity mismatch: sysfs device number differs from endpoint"};
    const bool matched = std::any_of(
        allowlist_.entries().begin(), allowlist_.entries().end(),
        [&](const AllowlistEntry& entry) {
            return entry.endpoint.key() == observed->endpoint.key() &&
                   entry.machine_class == machine_class_;
        });
    if (!matched)
        return {false, "serial USB identity mismatch: observed " + usbTuple(observed->endpoint) +
                           " does not match the commissioned tuple"};
    identity = observed->endpoint;
#endif
    CommissioningCandidate commissioning{
        identity,
        machine_class_,
        {{"baud", std::to_string(options_.baud)}},
        options_.operator_note,
    };
    const auto authorization = allowlist_.authorize(commissioning);
    if (!authorization.allowed) return {false, authorization.why};
    const auto baud_field = authorization.entry->connection.find("baud");
    if (baud_field == authorization.entry->connection.end())
        return {false, "commissioning refused: allowlist serial entry has no baud parameter"};
    const auto baud = parseBaud(baud_field->second);
    if (!baud) return {false, "commissioning refused: allowlist serial baud is invalid"};

#ifdef _WIN32
    std::string port = identity.address;
    if (!port.starts_with(R"(\\.\)")) port = R"(\\.\)" + port;
    HANDLE file = CreateFileW(widenAscii(port).c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        const DWORD error = GetLastError();
        if (error == ERROR_ACCESS_DENIED)
            return {false, "serial exclusive open refused: port is already claimed"};
        return {false, "serial open failed: " + windowsMessage(error)};
    }
    handle_ = reinterpret_cast<std::intptr_t>(file);
#else
    const int fd = openPosixSerialTarget(open_target);
    if (fd < 0) {
        if (errno == ELOOP || errno == ENOTDIR)
            return {false, "serial symlink refused: device path contains a symlink or non-directory"};
        if (errno == EBUSY || errno == EACCES)
            return {false, "serial exclusive open refused: port is already claimed"};
        return {false, "serial open failed: " + std::string(std::strerror(errno))};
    }
    struct stat descriptor_status {};
    if (::fstat(fd, &descriptor_status) != 0) {
        const int error = errno;
        ::close(fd);
        return {false, "serial descriptor validation failed: " +
                           std::string(std::strerror(error))};
    }
    if (!S_ISCHR(descriptor_status.st_mode)) {
        ::close(fd);
        return {false, "serial open refused: endpoint is not a character device"};
    }
    // No termios changes or identification traffic may precede this binding.
    const auto opened_identity =
        posixUsbIdentity(identity.address, open_target, options_, identity_why);
    if (!opened_identity || !deviceNumberMatches(descriptor_status, *observed) ||
        !deviceNumberMatches(descriptor_status, *opened_identity) ||
        opened_identity->endpoint.key() != identity.key() ||
        descriptor_status.st_dev != validated_status.st_dev ||
        descriptor_status.st_ino != validated_status.st_ino ||
        descriptor_status.st_rdev != validated_status.st_rdev) {
        ::close(fd);
        return {false, "serial USB identity mismatch: opened descriptor differs from validated device"};
    }
    if (::flock(fd, LOCK_EX | LOCK_NB) != 0) {
        const int error = errno;
        ::close(fd);
        if (error == EWOULDBLOCK || error == EAGAIN)
            return {false, "serial exclusive open refused: advisory lock is held"};
        return {false, "serial advisory lock failed: " + std::string(std::strerror(error))};
    }
#ifdef TIOCEXCL
    if (::ioctl(fd, TIOCEXCL) != 0) {
        const int error = errno;
        (void)::flock(fd, LOCK_UN);
        ::close(fd);
        return {false, "serial exclusive claim failed: " + std::string(std::strerror(error))};
    }
#endif
    handle_ = fd;
#endif

    std::string configure_why;
    if (!configure(*baud, &configure_why)) {
        close();
        return {false, std::move(configure_why)};
    }
    endpoint_ = endpoint;
    source_ = evidenceSourceForEndpoint(endpoint);
    return {true, {}};
}

void SerialTransport::close() {
    if (handle_ == invalid_handle) return;
#ifdef _WIN32
    HANDLE file = reinterpret_cast<HANDLE>(handle_);
    (void)CancelIoEx(file, nullptr);
    (void)PurgeComm(file, PURGE_RXABORT | PURGE_TXABORT);
    CloseHandle(file);
#else
    const int fd = static_cast<int>(handle_);
#ifdef TIOCNXCL
    (void)::ioctl(fd, TIOCNXCL);
#endif
    (void)::flock(fd, LOCK_UN);
    while (::close(fd) != 0 && errno == EINTR) {}
#endif
    handle_ = invalid_handle;
    endpoint_ = {};
    source_ = FreshSource::Simulated;
}

bool SerialTransport::configure(std::uint32_t baud, std::string* why) {
#ifdef _WIN32
    HANDLE file = reinterpret_cast<HANDLE>(handle_);
    DCB dcb{};
    dcb.DCBlength = sizeof(dcb);
    if (!GetCommState(file, &dcb)) {
        if (why) *why = "serial GetCommState failed: " + windowsMessage(GetLastError());
        return false;
    }
    dcb.BaudRate = baud;
    dcb.ByteSize = 8;
    dcb.Parity = NOPARITY;
    dcb.StopBits = ONESTOPBIT;
    dcb.fBinary = TRUE;
    dcb.fParity = FALSE;
    dcb.fOutxCtsFlow = FALSE;
    dcb.fOutxDsrFlow = FALSE;
    dcb.fDtrControl = DTR_CONTROL_ENABLE;
    dcb.fDsrSensitivity = FALSE;
    dcb.fTXContinueOnXoff = TRUE;
    dcb.fOutX = FALSE;
    dcb.fInX = FALSE;
    dcb.fErrorChar = FALSE;
    dcb.fNull = FALSE;
    dcb.fRtsControl = RTS_CONTROL_ENABLE;
    dcb.fAbortOnError = FALSE;
    if (!SetCommState(file, &dcb)) {
        if (why) *why = "serial SetCommState failed: " + windowsMessage(GetLastError());
        return false;
    }
    COMMTIMEOUTS timeouts{};
    timeouts.ReadIntervalTimeout = MAXDWORD;
    timeouts.ReadTotalTimeoutMultiplier = 0;
    timeouts.ReadTotalTimeoutConstant = 0;
    timeouts.WriteTotalTimeoutMultiplier = 0;
    timeouts.WriteTotalTimeoutConstant = static_cast<DWORD>(std::clamp<std::int64_t>(
        options_.write_timeout.count(), 1, std::numeric_limits<DWORD>::max()));
    if (!SetCommTimeouts(file, &timeouts)) {
        if (why) *why = "serial SetCommTimeouts failed: " + windowsMessage(GetLastError());
        return false;
    }
    (void)SetupComm(file, static_cast<DWORD>(std::min<std::size_t>(options_.max_read_bytes, 65536)),
                    65536);
    return true;
#else
    const auto speed = termiosBaud(baud);
    if (!speed) {
        if (why) *why = "unsupported POSIX serial baud: " + std::to_string(baud);
        return false;
    }
    termios settings{};
    const int fd = static_cast<int>(handle_);
    if (::tcgetattr(fd, &settings) != 0) {
        if (why) *why = "serial tcgetattr failed: " + std::string(std::strerror(errno));
        return false;
    }
    ::cfmakeraw(&settings);
    settings.c_cflag &= ~(CSIZE | PARENB | CSTOPB);
#ifdef CRTSCTS
    settings.c_cflag &= ~CRTSCTS;
#endif
    settings.c_cflag |= CS8 | CLOCAL | CREAD;
    settings.c_cc[VMIN] = 0;
    settings.c_cc[VTIME] = 0;
    if (::cfsetispeed(&settings, *speed) != 0 || ::cfsetospeed(&settings, *speed) != 0 ||
        ::tcsetattr(fd, TCSANOW, &settings) != 0) {
        if (why) *why = "serial raw termios configuration failed: " +
                        std::string(std::strerror(errno));
        return false;
    }
    return true;
#endif
}

device::WriteResult SerialTransport::write(std::span<const std::byte> bytes) {
    device::WriteResult result;
    if (handle_ == invalid_handle) {
        result.why = "serial transport is closed";
        return result;
    }
    if (bytes.empty()) {
        result.ok = true;
        return result;
    }
    const auto deadline = std::chrono::steady_clock::now() + options_.write_timeout;
#ifdef _WIN32
    HANDLE file = reinterpret_cast<HANDLE>(handle_);
    while (result.bytes_written < bytes.size()) {
        DWORD written = 0;
        const DWORD amount = static_cast<DWORD>(std::min<std::size_t>(
            bytes.size() - result.bytes_written, std::numeric_limits<DWORD>::max()));
        if (!WriteFile(file, bytes.data() + result.bytes_written, amount, &written, nullptr)) {
            const DWORD error = GetLastError();
            if (windowsDeviceLost(error)) {
                close();
                result.why = "device_lost: serial write failed: " + windowsMessage(error);
            } else {
                result.why = "serial write failed: " + windowsMessage(error);
            }
            return result;
        }
        if (written == 0) {
            if (std::chrono::steady_clock::now() >= deadline) {
                result.why = "serial write timed out after partial write";
                return result;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            continue;
        }
        result.bytes_written += written;
    }
#else
    const int fd = static_cast<int>(handle_);
    while (result.bytes_written < bytes.size()) {
        const ssize_t written = ::write(fd, bytes.data() + result.bytes_written,
                                        bytes.size() - result.bytes_written);
        if (written > 0) {
            result.bytes_written += static_cast<std::size_t>(written);
            continue;
        }
        if (written < 0 && errno == EINTR) continue;
        if (written < 0 && (errno == ENXIO || errno == EIO)) {
            const int error = errno;
            close();
            result.why = "device_lost: serial write failed: " + std::string(std::strerror(error));
            return result;
        }
        if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            const auto now = std::chrono::steady_clock::now();
            if (now >= deadline) {
                result.why = "serial write timed out after partial write";
                return result;
            }
            const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - now);
            pollfd ready{fd, POLLOUT, 0};
            const int timeout = static_cast<int>(std::clamp<std::int64_t>(remaining.count(), 1,
                                                                          std::numeric_limits<int>::max()));
            int status;
            do status = ::poll(&ready, 1, timeout); while (status < 0 && errno == EINTR);
            if (status == 0) {
                result.why = "serial write timed out after partial write";
                return result;
            }
            if (status < 0) {
                result.why = "serial write wait failed: " + std::string(std::strerror(errno));
                return result;
            }
            if (ready.revents & (POLLHUP | POLLERR | POLLNVAL)) {
                close();
                result.why = "device_lost: serial write endpoint disconnected";
                return result;
            }
            continue;
        }
        result.why = "serial write failed: " + std::string(std::strerror(errno));
        return result;
    }
#endif
    result.ok = true;
    return result;
}

device::PollResult SerialTransport::deviceLost(std::string why) {
    close();
    device::TransportEvent event;
    event.kind = device::TransportEvent::Kind::Error;
    event.why = "device_lost: " + std::move(why);
    return {{std::move(event)}};
}

device::PollResult SerialTransport::poll(device::Deadline deadline) {
    if (handle_ == invalid_handle) {
        device::TransportEvent event;
        event.kind = device::TransportEvent::Kind::Closed;
        event.why = "serial transport is closed";
        return {{std::move(event)}};
    }

#ifdef _WIN32
    HANDLE file = reinterpret_cast<HANDLE>(handle_);
    for (;;) {
        std::vector<std::byte> data(options_.max_read_bytes);
        DWORD read_count = 0;
        if (!ReadFile(file, data.data(), static_cast<DWORD>(std::min<std::size_t>(
                         data.size(), std::numeric_limits<DWORD>::max())), &read_count, nullptr)) {
            const DWORD error = GetLastError();
            if (windowsDeviceLost(error))
                return deviceLost("serial read failed: " + windowsMessage(error));
            device::TransportEvent event;
            event.kind = device::TransportEvent::Kind::Error;
            event.why = "serial read failed: " + windowsMessage(error);
            return {{std::move(event)}};
        }
        if (read_count > 0) {
            data.resize(read_count);
            device::TransportEvent event;
            event.kind = device::TransportEvent::Kind::Data;
            event.data = std::move(data);
            event.raw = bytesToString(event.data);
            return {{std::move(event)}};
        }
        if (deadline == device::Deadline::max() ||
            std::chrono::steady_clock::now() >= deadline) return {};
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
#else
    const int fd = static_cast<int>(handle_);
    for (;;) {
        const auto now = std::chrono::steady_clock::now();
        const auto remaining = deadline == device::Deadline::max()
            ? device::Deadline::duration::zero()
            : deadline > now ? deadline - now : device::Deadline::duration::zero();
        const auto remaining_ms = std::chrono::duration_cast<std::chrono::milliseconds>(remaining);
        const int timeout = deadline == device::Deadline::max() || deadline <= now
            ? 0 : static_cast<int>(std::clamp<std::int64_t>(
                remaining_ms.count() + (remaining > remaining_ms ? 1 : 0), 1,
                std::numeric_limits<int>::max()));
        pollfd ready{fd, POLLIN, 0};
        const int status = ::poll(&ready, 1, timeout);
        if (status == 0) return {};
        if (status < 0 && errno == EINTR) {
            if (deadline == device::Deadline::max() ||
                std::chrono::steady_clock::now() >= deadline) return {};
            continue;
        }
        if (status < 0)
            return deviceLost("serial poll failed: " + std::string(std::strerror(errno)));
        if (ready.revents & POLLNVAL) return deviceLost("serial descriptor became invalid");

        if (ready.revents & (POLLIN | POLLHUP | POLLERR)) {
            std::vector<std::byte> data(options_.max_read_bytes);
            const ssize_t count = ::read(fd, data.data(), data.size());
            if (count > 0) {
                data.resize(static_cast<std::size_t>(count));
                device::TransportEvent event;
                event.kind = device::TransportEvent::Kind::Data;
                event.data = std::move(data);
                event.raw = bytesToString(event.data);
                return {{std::move(event)}};
            }
            if (count < 0 && errno == EINTR) {
                if (deadline == device::Deadline::max() ||
                    std::chrono::steady_clock::now() >= deadline) return {};
                continue;
            }
            if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
                if (ready.revents & (POLLHUP | POLLERR))
                    return deviceLost("serial peer disconnected");
                if (deadline == device::Deadline::max() ||
                    std::chrono::steady_clock::now() >= deadline) return {};
                continue;
            }
            if (count == 0 || (count < 0 && (errno == ENXIO || errno == EIO)))
                return deviceLost("serial peer disconnected");
            if (count < 0)
                return deviceLost("serial read failed: " + std::string(std::strerror(errno)));
        }
        if (deadline == device::Deadline::max() ||
            std::chrono::steady_clock::now() >= deadline) return {};
    }
#endif
}

device::CapabilityReport SerialTransport::capabilityReport(
    std::string subject_id, std::string transport_evidence_ref) const {
    device::CapabilityReport report;
    report.subject_id = std::move(subject_id);
    report.max_advertised = device::CapabilityLevel::Transport;
    report.emulator_stage_passed = device::EmulatorStage::None;
    for (const auto level : device::capability_levels) {
        device::CapabilityTruth truth;
        truth.level = level;
        truth.emulator_stage = device::EmulatorStage::None;
        if (level == device::CapabilityLevel::Transport) {
            if (isOpen()) {
                truth.truth = device::CapabilityTruthValue::Verified;
                if (!transport_evidence_ref.empty())
                    truth.evidence_ref.push_back(std::move(transport_evidence_ref));
            } else {
                truth.truth = device::CapabilityTruthValue::Assumed;
                truth.missing_level = device::CapabilityLevel::Transport;
                truth.why = "serial endpoint is not open";
            }
        } else if (level == device::CapabilityLevel::Execute) {
            truth.truth = device::CapabilityTruthValue::Assumed;
            truth.missing_level = device::CapabilityLevel::Execute;
            truth.why = "physical execute requires an attended E6 hardware pilot";
        } else {
            truth.truth = device::CapabilityTruthValue::Unsupported;
            truth.why = level == device::CapabilityLevel::SafetyIntegration
                            ? "serial transport is not a safety integration"
                            : "serial transport does not establish this capability";
        }
        report.levels.emplace(level, std::move(truth));
    }
    return report;
}

std::vector<SerialCandidate> SerialTransport::enumerate(std::string* why) {
    std::vector<SerialCandidate> candidates;
#ifdef _WIN32
    HMODULE setupapi = LoadLibraryW(L"setupapi.dll");
    if (setupapi) {
        const auto get_devices = reinterpret_cast<decltype(&SetupDiGetClassDevsW)>(
            GetProcAddress(setupapi, "SetupDiGetClassDevsW"));
        const auto enumerate_device = reinterpret_cast<decltype(&SetupDiEnumDeviceInfo)>(
            GetProcAddress(setupapi, "SetupDiEnumDeviceInfo"));
        const auto get_property = reinterpret_cast<decltype(&SetupDiGetDeviceRegistryPropertyW)>(
            GetProcAddress(setupapi, "SetupDiGetDeviceRegistryPropertyW"));
        const auto get_instance = reinterpret_cast<decltype(&SetupDiGetDeviceInstanceIdW)>(
            GetProcAddress(setupapi, "SetupDiGetDeviceInstanceIdW"));
        const auto destroy = reinterpret_cast<decltype(&SetupDiDestroyDeviceInfoList)>(
            GetProcAddress(setupapi, "SetupDiDestroyDeviceInfoList"));
        const GUID ports_class = {0x4d36e978, 0xe325, 0x11ce,
                                  {0xbf, 0xc1, 0x08, 0x00, 0x2b, 0xe1, 0x03, 0x18}};
        if (get_devices && enumerate_device && get_property && get_instance && destroy) {
            HDEVINFO devices = get_devices(&ports_class, nullptr, nullptr, DIGCF_PRESENT);
            if (devices != INVALID_HANDLE_VALUE) {
                for (DWORD index = 0;; ++index) {
                    SP_DEVINFO_DATA info{};
                    info.cbSize = sizeof(info);
                    if (!enumerate_device(devices, index, &info)) {
                        if (GetLastError() == ERROR_NO_MORE_ITEMS) break;
                        continue;
                    }
                    std::array<wchar_t, 512> friendly{};
                    DWORD type = 0;
                    DWORD needed = 0;
                    if (!get_property(devices, &info, SPDRP_FRIENDLYNAME, &type,
                                      reinterpret_cast<PBYTE>(friendly.data()),
                                      static_cast<DWORD>(friendly.size() * sizeof(wchar_t)), &needed))
                        continue;
                    const std::wstring_view label(friendly.data());
                    const auto marker = label.rfind(L"(COM");
                    const auto close = label.find(L')', marker);
                    if (marker == std::wstring_view::npos || close == std::wstring_view::npos) continue;
                    const std::string port = narrowAscii(label.substr(marker + 1, close - marker - 1));

                    std::array<wchar_t, 1024> instance{};
                    std::string vid, pid, serial;
                    if (get_instance(devices, &info, instance.data(),
                                     static_cast<DWORD>(instance.size()), &needed)) {
                        const std::string id = narrowAscii(instance.data());
                        const std::string lowered = [&] {
                            std::string value = id;
                            std::transform(value.begin(), value.end(), value.begin(),
                                           [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });
                            return value;
                        }();
                        const auto vid_at = lowered.find("vid_");
                        const auto pid_at = lowered.find("pid_");
                        const auto slash = id.rfind('\\');
                        if (vid_at != std::string::npos && vid_at + 8 <= id.size())
                            vid = id.substr(vid_at + 4, 4);
                        if (pid_at != std::string::npos && pid_at + 8 <= id.size())
                            pid = id.substr(pid_at + 4, 4);
                        if (slash != std::string::npos && slash + 1 < id.size())
                            serial = id.substr(slash + 1);
                    }
                    EndpointIdentity identity = (vid.empty() || pid.empty() || serial.empty())
                                                    ? EndpointIdentity::serial(port)
                                                    : EndpointIdentity::serial(port, vid, pid, serial);
                    candidates.push_back({std::move(identity), narrowAscii(label), true});
                }
                destroy(devices);
            }
        }
        FreeLibrary(setupapi);
    }
    // Include non-USB and ports whose driver omitted SetupAPI friendly metadata.
    for (unsigned number = 1; number <= 256; ++number) {
        const std::string port = "COM" + std::to_string(number);
        std::array<wchar_t, 4> target{};
        if (QueryDosDeviceW(widenAscii(port).c_str(), target.data(), static_cast<DWORD>(target.size())) ||
            GetLastError() == ERROR_INSUFFICIENT_BUFFER) {
            const auto identity = EndpointIdentity::serial(port);
            if (std::none_of(candidates.begin(), candidates.end(), [&](const auto& candidate) {
                    return candidate.identity.address == identity.address;
                })) candidates.push_back({identity, port, true});
        }
    }
#elif defined(__APPLE__)
    // Darwin has no Linux-style /dev/serial/by-id tree. IOKit publishes serial
    // callout and dial-in character devices as /dev/cu.* and /dev/tty.*.
    const std::filesystem::path root("/dev");
    std::error_code error;
    std::filesystem::directory_iterator iterator(root, error);
    const std::filesystem::directory_iterator end;
    while (!error && iterator != end) {
        const auto path = iterator->path();
        const std::string name = path.filename().string();
        std::error_code status_error;
        if ((name.starts_with("cu.") || name.starts_with("tty.")) &&
            iterator->is_character_file(status_error) && !status_error) {
            candidates.push_back({EndpointIdentity::serial(path.string()), name, true});
        }
        iterator.increment(error);
    }
    if (error && why) *why = "serial enumeration failed: " + error.message();
#else
    const std::filesystem::path root("/dev/serial/by-id");
    std::error_code error;
    if (std::filesystem::exists(root, error)) {
        std::filesystem::directory_iterator iterator(root, error);
        const std::filesystem::directory_iterator end;
        while (!error && iterator != end) {
            const auto path = iterator->path();
            EndpointIdentity identity = EndpointIdentity::serial(path.string());
            std::filesystem::path target;
            std::string identity_why;
            const SerialTransportOptions options;
            if (resolvePosixSerialTarget(path, options, target, identity_why)) {
                const auto observed = posixUsbIdentity(path.string(), target, options, identity_why);
                if (observed) identity = observed->endpoint;
            }
            candidates.push_back({std::move(identity), path.filename().string(), true});
            iterator.increment(error);
        }
    }
    if (error && why) *why = "serial enumeration failed: " + error.message();
#endif
    std::sort(candidates.begin(), candidates.end(), [](const auto& left, const auto& right) {
        return left.identity.key() < right.identity.key();
    });
    return candidates;
}

}  // namespace chudware::mfg::gateway
