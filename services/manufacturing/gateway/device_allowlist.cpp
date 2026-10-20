#include "manufacturing/gateway/device_allowlist.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <limits>
#include <sstream>
#include <stdexcept>
#include <system_error>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace chudware::mfg::gateway {
namespace {

bool fail(std::string* why, std::string message) {
    if (why) *why = std::move(message);
    return false;
}

std::string lowerAscii(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return value;
}

#ifdef _WIN32
std::string upperAscii(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char ch) {
        return static_cast<char>(std::toupper(ch));
    });
    return value;
}
#endif

bool isNetwork(device::TransportKind kind) {
    return kind == device::TransportKind::Tcp || kind == device::TransportKind::WebSocket;
}

std::optional<device::TransportKind> parseKind(std::string_view text) {
    if (text == "serial") return device::TransportKind::Serial;
    if (text == "tcp") return device::TransportKind::Tcp;
    if (text == "websocket") return device::TransportKind::WebSocket;
    if (text == "http_adapter") return device::TransportKind::HttpAdapter;
    if (text == "opcua_read") return device::TransportKind::OpcUaRead;
    return std::nullopt;
}

std::string normalizeAddress(device::TransportKind kind, std::string value) {
    if (isNetwork(kind)) return lowerAscii(std::move(value));
#ifdef _WIN32
    if (kind == device::TransportKind::Serial) {
        constexpr std::string_view prefix = R"(\\.\)";
        if (value.starts_with(prefix)) value.erase(0, prefix.size());
        return upperAscii(std::move(value));
    }
#endif
    return value;
}

std::string normalizeHexId(std::string value) {
    value = lowerAscii(std::move(value));
    if (value.starts_with("0x")) value.erase(0, 2);
    return value;
}

bool validConnectionKey(std::string_view key) {
    if (key.empty()) return false;
    return std::all_of(key.begin(), key.end(), [](unsigned char ch) {
        return std::isalnum(ch) || ch == '_' || ch == '-';
    });
}

std::string escapeField(std::string_view value) {
    std::string out;
    out.reserve(value.size());
    for (char ch : value) {
        switch (ch) {
            case '\\': out += "\\\\"; break;
            case '\t': out += "\\t"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '=': out += "\\="; break;
            default: out.push_back(ch); break;
        }
    }
    return out;
}

std::optional<std::string> unescapeField(std::string_view value, std::string* why) {
    std::string out;
    out.reserve(value.size());
    bool escaped = false;
    for (char ch : value) {
        if (escaped) {
            switch (ch) {
                case '\\': out.push_back('\\'); break;
                case 't': out.push_back('\t'); break;
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case '=': out.push_back('='); break;
                default:
                    fail(why, std::string("invalid escape \\") + ch + " in allowlist field");
                    return std::nullopt;
            }
            escaped = false;
        } else if (ch == '\\') {
            escaped = true;
        } else {
            out.push_back(ch);
        }
    }
    if (escaped) {
        fail(why, "trailing escape in allowlist field");
        return std::nullopt;
    }
    return out;
}

std::vector<std::string_view> splitColumns(std::string_view line) {
    std::vector<std::string_view> columns;
    std::size_t begin = 0;
    bool escaped = false;
    for (std::size_t i = 0; i < line.size(); ++i) {
        if (!escaped && line[i] == '\t') {
            columns.push_back(line.substr(begin, i - begin));
            begin = i + 1;
        }
        if (!escaped && line[i] == '\\') escaped = true;
        else escaped = false;
    }
    columns.push_back(line.substr(begin));
    return columns;
}

std::optional<std::pair<std::string, std::string>> parseAssignment(std::string_view column,
                                                                   std::string* why) {
    bool escaped = false;
    std::size_t equals = std::string_view::npos;
    for (std::size_t i = 0; i < column.size(); ++i) {
        if (!escaped && column[i] == '=') {
            equals = i;
            break;
        }
        if (!escaped && column[i] == '\\') escaped = true;
        else escaped = false;
    }
    if (equals == std::string_view::npos) {
        fail(why, "allowlist column is not key=value");
        return std::nullopt;
    }
    auto key = unescapeField(column.substr(0, equals), why);
    auto value = unescapeField(column.substr(equals + 1), why);
    if (!key || !value || key->empty()) return std::nullopt;
    return std::pair<std::string, std::string>{std::move(*key), std::move(*value)};
}

std::optional<std::uint16_t> parsePort(std::string_view text) {
    unsigned value = 0;
    const auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
    if (error != std::errc{} || end != text.data() + text.size() || value == 0 ||
        value > std::numeric_limits<std::uint16_t>::max()) return std::nullopt;
    return static_cast<std::uint16_t>(value);
}

std::optional<EndpointIdentity> identityFromEndpoint(const device::Endpoint& endpoint,
                                                     device::TransportKind kind,
                                                     std::string* why) {
    if (kind == device::TransportKind::Serial)
        return EndpointIdentity::serial(endpoint.address);
    if (!isNetwork(kind)) {
        fail(why, "allowlist endpoint parsing supports serial, tcp, and websocket");
        return std::nullopt;
    }

    std::string_view value = endpoint.address;
    const auto scheme = value.find("://");
    if (scheme != std::string_view::npos) value.remove_prefix(scheme + 3);
    const auto slash = value.find('/');
    const std::string_view authority = value.substr(0, slash);
    std::string_view host;
    std::string_view port_text;
    if (authority.starts_with('[')) {
        const auto close = authority.find(']');
        if (close == std::string_view::npos || close + 2 > authority.size() ||
            authority[close + 1] != ':') {
            fail(why, "network allowlist address requires [IPv6]:port");
            return std::nullopt;
        }
        host = authority.substr(1, close - 1);
        port_text = authority.substr(close + 2);
    } else {
        const auto colon = authority.rfind(':');
        if (colon == std::string_view::npos) {
            fail(why, "network allowlist address requires host:port");
            return std::nullopt;
        }
        host = authority.substr(0, colon);
        port_text = authority.substr(colon + 1);
    }
    const auto port = parsePort(port_text);
    if (host.empty() || !port) {
        fail(why, "network allowlist address has an invalid host or port");
        return std::nullopt;
    }
    return EndpointIdentity::network(kind, std::string(host), *port);
}

bool sameEntries(const std::vector<AllowlistEntry>& left,
                 const std::vector<AllowlistEntry>& right) {
    if (left.size() != right.size()) return false;
    std::vector<std::string> a;
    std::vector<std::string> b;
    a.reserve(left.size());
    b.reserve(right.size());
    for (const auto& entry : left) a.push_back(DeviceAllowlist::formatLine(entry));
    for (const auto& entry : right) b.push_back(DeviceAllowlist::formatLine(entry));
    std::sort(a.begin(), a.end());
    std::sort(b.begin(), b.end());
    return a == b;
}

std::string missingTemplateLine(const EndpointIdentity& endpoint) {
    std::string line = std::string(device::to_string(endpoint.transport));
    line += "\taddress=" + escapeField(endpoint.address);
    if (endpoint.port) line += "\tport=" + std::to_string(endpoint.port);
    if (!endpoint.usb_vid.empty()) line += "\tusb_vid=" + escapeField(endpoint.usb_vid);
    if (!endpoint.usb_pid.empty()) line += "\tusb_pid=" + escapeField(endpoint.usb_pid);
    if (!endpoint.usb_serial.empty()) line += "\tusb_serial=" + escapeField(endpoint.usb_serial);
    line += "\tmachine_class=<machine_class>\tnote=<operator_note>";
    return line;
}

}  // namespace

EndpointIdentity EndpointIdentity::serial(std::string stable_path_or_port, std::string vid,
                                          std::string pid, std::string serial_number) {
    EndpointIdentity value;
    value.transport = device::TransportKind::Serial;
    value.address = normalizeAddress(value.transport, std::move(stable_path_or_port));
    value.usb_vid = normalizeHexId(std::move(vid));
    value.usb_pid = normalizeHexId(std::move(pid));
    value.usb_serial = std::move(serial_number);
    return value;
}

EndpointIdentity EndpointIdentity::network(device::TransportKind kind, std::string host,
                                           std::uint16_t network_port) {
    EndpointIdentity value;
    value.transport = kind;
    value.address = normalizeAddress(kind, std::move(host));
    value.port = network_port;
    return value;
}

bool EndpointIdentity::valid(std::string* why) const {
    if (address.empty()) return fail(why, "allowlist endpoint address is empty");
    if (transport == device::TransportKind::Serial) {
        if (port != 0) return fail(why, "serial allowlist endpoint must not have a network port");
        if (usb_vid.empty() || usb_pid.empty() || usb_serial.empty())
            return fail(why, "serial USB identity requires vid, pid, and serial together");
        const auto valid_hex = [](std::string_view value) {
            if (value.starts_with("0x") || value.starts_with("0X")) value.remove_prefix(2);
            return value.size() == 4 && std::all_of(value.begin(), value.end(),
                [](unsigned char ch) { return std::isxdigit(ch); });
        };
        if (!valid_hex(usb_vid) || !valid_hex(usb_pid))
            return fail(why, "serial USB vid and pid must be four hexadecimal digits");
    } else if (isNetwork(transport)) {
        if (port == 0) return fail(why, "network allowlist endpoint requires a port");
        if (!usb_vid.empty() || !usb_pid.empty() || !usb_serial.empty())
            return fail(why, "network allowlist endpoint must not have USB identity fields");
    } else {
        return fail(why, "allowlist endpoint transport must be serial, tcp, or websocket");
    }
    return true;
}

std::string EndpointIdentity::key() const {
    std::string value = std::string(device::to_string(transport)) + "\n" +
                        normalizeAddress(transport, address);
    if (port) value += "\n" + std::to_string(port);
    if (transport == device::TransportKind::Serial) {
        value += "\n" + normalizeHexId(usb_vid) + "\n" + normalizeHexId(usb_pid) + "\n" +
                 usb_serial;
    }
    return value;
}

DeviceAllowlist::DeviceAllowlist(std::filesystem::path path) : path_(std::move(path)) {
    std::string why;
    if (!reload(&why)) throw std::runtime_error("unable to load device allowlist: " + why);
}

bool DeviceAllowlist::reload(std::string* why) {
    std::vector<AllowlistEntry> loaded;
    std::ifstream input(path_);
    if (input.is_open()) {
        std::string line;
        std::size_t line_number = 0;
        while (std::getline(input, line)) {
            ++line_number;
            if (!line.empty() && line.back() == '\r') line.pop_back();
            const auto first = line.find_first_not_of(" \t");
            if (first == std::string::npos || line[first] == '#') continue;
            std::string parse_why;
            auto entry = parseLine(line, &parse_why);
            if (!entry)
                return fail(why, path_.string() + ":" + std::to_string(line_number) + ": " +
                                     parse_why);
            if (std::any_of(loaded.begin(), loaded.end(), [&](const auto& existing) {
                    return existing.endpoint.key() == entry->endpoint.key();
                })) {
                return fail(why, path_.string() + ":" + std::to_string(line_number) +
                                     ": duplicate endpoint identity");
            }
            loaded.push_back(std::move(*entry));
        }
        if (input.bad()) return fail(why, "failed while reading " + path_.string());
    } else {
        std::error_code error;
        if (std::filesystem::exists(path_, error) || error)
            return fail(why, "cannot open " + path_.string());
    }

    if (!sameEntries(entries_, loaded)) {
        entries_ = std::move(loaded);
        ++epoch_;
    }
    return true;
}

Authorization DeviceAllowlist::authorize(const CommissioningCandidate& candidate) const {
    Authorization result;
    result.required_line = formatLine(candidate);
    std::string invalid;
    if (!candidate.endpoint.valid(&invalid)) {
        result.why = "commissioning refused: " + invalid + "; add: " + result.required_line;
        return result;
    }
    for (const auto& entry : entries_) {
        if (entry.endpoint.key() != candidate.endpoint.key()) continue;
        if (entry.machine_class != candidate.machine_class) {
            result.why = "commissioning refused for " + candidate.endpoint.address +
                         ": allowlisted machine_class is " +
                         std::string(device::to_string(entry.machine_class)) + ", requested " +
                         std::string(device::to_string(candidate.machine_class)) + "; add: " +
                         result.required_line;
            return result;
        }
        for (const auto& [key, value] : candidate.connection) {
            const auto found = entry.connection.find(key);
            if (found == entry.connection.end() || found->second != value) {
                result.why = "commissioning refused for " + candidate.endpoint.address +
                             ": connection parameter " + key + " does not match; add: " +
                             result.required_line;
                return result;
            }
        }
        result.allowed = true;
        result.entry = entry;
        result.required_line.clear();
        return result;
    }
    result.why = "commissioning refused: " + candidate.endpoint.address +
                 " is not allowlisted; add: " + result.required_line;
    return result;
}

Authorization DeviceAllowlist::authorize(const device::Endpoint& endpoint,
                                         device::TransportKind kind) const {
    Authorization result;
    std::string why;
    const auto identity = identityFromEndpoint(endpoint, kind, &why);
    if (!identity) {
        result.why = "commissioning refused for " + endpoint.address + ": " + why;
        return result;
    }
    result.required_line = missingTemplateLine(*identity);
    const AllowlistEntry* match = nullptr;
    for (const auto& entry : entries_) {
        if (entry.endpoint.key() != identity->key()) continue;
        if (match) {
            result.why = "commissioning refused for " + endpoint.address +
                         ": endpoint identity is ambiguous; add: " + result.required_line;
            return result;
        }
        match = &entry;
    }
    if (!match) {
        result.why = "commissioning refused: " + endpoint.address +
                     " is not allowlisted; add: " + result.required_line;
        return result;
    }
    result.allowed = true;
    result.entry = *match;
    result.required_line.clear();
    return result;
}

bool DeviceAllowlist::upsert(const AllowlistEntry& entry, std::string* why) {
    std::string invalid;
    if (!entry.endpoint.valid(&invalid)) return fail(why, invalid);
    if (entry.operator_note.empty()) return fail(why, "allowlist operator note is required");
    if (entry.connection.contains("origin"))
        return fail(why, "connection parameter origin is forbidden; transport physicality is server-attested");
    for (const auto& [key, value] : entry.connection) {
        if (!validConnectionKey(key)) return fail(why, "invalid connection parameter key: " + key);
        if (value.empty()) return fail(why, "connection parameter value is empty: " + key);
    }

    std::vector<AllowlistEntry> changed = entries_;
    const auto found = std::find_if(changed.begin(), changed.end(), [&](const auto& current) {
        return current.endpoint.key() == entry.endpoint.key();
    });
    if (found == changed.end()) changed.push_back(entry);
    else *found = entry;
    if (sameEntries(entries_, changed)) return true;
    if (!persist(changed, why)) return false;
    entries_ = std::move(changed);
    ++epoch_;
    return true;
}

bool DeviceAllowlist::erase(const EndpointIdentity& endpoint, std::string* why) {
    std::vector<AllowlistEntry> changed = entries_;
    std::erase_if(changed, [&](const auto& entry) { return entry.endpoint.key() == endpoint.key(); });
    if (changed.size() == entries_.size()) return true;
    if (!persist(changed, why)) return false;
    entries_ = std::move(changed);
    ++epoch_;
    return true;
}

std::string DeviceAllowlist::formatLine(const AllowlistEntry& entry) {
    std::string line = std::string(device::to_string(entry.endpoint.transport));
    line += "\taddress=" + escapeField(entry.endpoint.address);
    if (entry.endpoint.port) line += "\tport=" + std::to_string(entry.endpoint.port);
    if (!entry.endpoint.usb_vid.empty())
        line += "\tusb_vid=" + escapeField(normalizeHexId(entry.endpoint.usb_vid));
    if (!entry.endpoint.usb_pid.empty())
        line += "\tusb_pid=" + escapeField(normalizeHexId(entry.endpoint.usb_pid));
    if (!entry.endpoint.usb_serial.empty())
        line += "\tusb_serial=" + escapeField(entry.endpoint.usb_serial);
    line += "\tmachine_class=" + std::string(device::to_string(entry.machine_class));
    for (const auto& [key, value] : entry.connection)
        line += "\t" + escapeField(key) + "=" + escapeField(value);
    line += "\tnote=" + escapeField(entry.operator_note);
    return line;
}

std::optional<AllowlistEntry> DeviceAllowlist::parseLine(std::string_view line, std::string* why) {
    const auto columns = splitColumns(line);
    if (columns.empty()) {
        fail(why, "empty allowlist line");
        return std::nullopt;
    }
    const auto kind_text = unescapeField(columns.front(), why);
    if (!kind_text) return std::nullopt;
    const auto kind = parseKind(*kind_text);
    if (!kind || (*kind != device::TransportKind::Serial && !isNetwork(*kind))) {
        fail(why, "allowlist transport must be serial, tcp, or websocket");
        return std::nullopt;
    }

    std::map<std::string, std::string> fields;
    for (std::size_t i = 1; i < columns.size(); ++i) {
        const auto assignment = parseAssignment(columns[i], why);
        if (!assignment) return std::nullopt;
        if (!fields.emplace(assignment->first, assignment->second).second) {
            fail(why, "duplicate allowlist field: " + assignment->first);
            return std::nullopt;
        }
    }
    const auto address = fields.find("address");
    const auto machine = fields.find("machine_class");
    const auto note = fields.find("note");
    if (address == fields.end() || machine == fields.end() || note == fields.end()) {
        fail(why, "allowlist line requires address, machine_class, and note");
        return std::nullopt;
    }
    const auto machine_class = device::parse_machine_class(machine->second);
    if (!machine_class) {
        fail(why, "invalid machine_class: " + machine->second);
        return std::nullopt;
    }
    if (note->second.empty()) {
        fail(why, "allowlist operator note is required");
        return std::nullopt;
    }

    AllowlistEntry entry;
    entry.endpoint.transport = *kind;
    entry.endpoint.address = normalizeAddress(*kind, address->second);
    entry.machine_class = *machine_class;
    entry.operator_note = note->second;
    if (const auto port = fields.find("port"); port != fields.end()) {
        const auto parsed = parsePort(port->second);
        if (!parsed) {
            fail(why, "invalid allowlist port: " + port->second);
            return std::nullopt;
        }
        entry.endpoint.port = *parsed;
    }
    if (const auto value = fields.find("usb_vid"); value != fields.end())
        entry.endpoint.usb_vid = normalizeHexId(value->second);
    if (const auto value = fields.find("usb_pid"); value != fields.end())
        entry.endpoint.usb_pid = normalizeHexId(value->second);
    if (const auto value = fields.find("usb_serial"); value != fields.end())
        entry.endpoint.usb_serial = value->second;


    if (fields.contains("origin")) {
        fail(why, "connection parameter origin is forbidden; transport physicality is server-attested");
        return std::nullopt;
    }
    static constexpr std::array reserved = {"address", "port", "usb_vid", "usb_pid",
                                             "usb_serial", "machine_class", "note"};
    for (const auto& [key, value] : fields) {
        if (std::find(reserved.begin(), reserved.end(), key) != reserved.end()) continue;
        if (!validConnectionKey(key)) {
            fail(why, "invalid connection parameter key: " + key);
            return std::nullopt;
        }
        if (value.empty()) {
            fail(why, "connection parameter value is empty: " + key);
            return std::nullopt;
        }
        entry.connection.emplace(key, value);
    }
    std::string invalid;
    if (!entry.endpoint.valid(&invalid)) {
        fail(why, invalid);
        return std::nullopt;
    }
    return entry;
}

bool DeviceAllowlist::persist(const std::vector<AllowlistEntry>& entries, std::string* why) const {
    std::error_code fs_error;
    const auto parent = path_.parent_path().empty() ? std::filesystem::path(".") : path_.parent_path();
    std::filesystem::create_directories(parent, fs_error);
    if (fs_error) return fail(why, "cannot create allowlist directory: " + fs_error.message());

    std::vector<std::string> lines;
    lines.reserve(entries.size());
    for (const auto& entry : entries) lines.push_back(formatLine(entry));
    std::sort(lines.begin(), lines.end());
    std::string content = "# Chudware manufacturing commissioning allowlist v1\n";
    for (const auto& line : lines) content += line + "\n";
    auto temporary = path_;
    temporary += ".tmp";

#ifdef _WIN32
    HANDLE file = CreateFileW(temporary.wstring().c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE)
        return fail(why, "cannot create temporary allowlist file: Windows error " +
                             std::to_string(GetLastError()));
    std::size_t offset = 0;
    while (offset < content.size()) {
        DWORD written = 0;
        const DWORD amount = static_cast<DWORD>(std::min<std::size_t>(
            content.size() - offset, std::numeric_limits<DWORD>::max()));
        if (!WriteFile(file, content.data() + offset, amount, &written, nullptr) || written == 0) {
            const DWORD error = GetLastError();
            CloseHandle(file);
            DeleteFileW(temporary.wstring().c_str());
            return fail(why, "cannot write allowlist: Windows error " + std::to_string(error));
        }
        offset += written;
    }
    if (!FlushFileBuffers(file)) {
        const DWORD error = GetLastError();
        CloseHandle(file);
        DeleteFileW(temporary.wstring().c_str());
        return fail(why, "cannot synchronize allowlist: Windows error " + std::to_string(error));
    }
    CloseHandle(file);
    if (!MoveFileExW(temporary.wstring().c_str(), path_.wstring().c_str(),
                     MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        const DWORD error = GetLastError();
        DeleteFileW(temporary.wstring().c_str());
        return fail(why, "cannot replace allowlist: Windows error " + std::to_string(error));
    }
#else
    const int fd = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0) return fail(why, "cannot create temporary allowlist: " + std::string(std::strerror(errno)));
    std::size_t offset = 0;
    while (offset < content.size()) {
        const ssize_t written = ::write(fd, content.data() + offset, content.size() - offset);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) {
            const int error = errno;
            ::close(fd);
            ::unlink(temporary.c_str());
            return fail(why, "cannot write allowlist: " + std::string(std::strerror(error)));
        }
        offset += static_cast<std::size_t>(written);
    }
    if (::fsync(fd) != 0) {
        const int error = errno;
        ::close(fd);
        ::unlink(temporary.c_str());
        return fail(why, "cannot synchronize allowlist: " + std::string(std::strerror(error)));
    }
    if (::close(fd) != 0) {
        const int error = errno;
        ::unlink(temporary.c_str());
        return fail(why, "cannot close allowlist: " + std::string(std::strerror(error)));
    }
    if (::rename(temporary.c_str(), path_.c_str()) != 0) {
        const int error = errno;
        ::unlink(temporary.c_str());
        return fail(why, "cannot replace allowlist: " + std::string(std::strerror(error)));
    }
#ifdef O_DIRECTORY
    const int directory = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (directory >= 0) {
        (void)::fsync(directory);
        ::close(directory);
    }
#endif
#endif
    return true;
}

}  // namespace chudware::mfg::gateway
