#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "manufacturing/gateway/device_allowlist.h"
#include "manufacturing/gateway/transport_serial.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#ifndef _WIN32
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <sys/stat.h>
#ifdef __linux__
#include <sys/sysmacros.h>
#endif
#include <unistd.h>
#endif

namespace {
using namespace std::chrono_literals;
using chudware::device::CapabilityLevel;
using chudware::device::CapabilityTruthValue;
using chudware::device::Endpoint;
using chudware::device::MachineClass;
using chudware::device::TransportEvent;
using chudware::device::TransportKind;
using chudware::mfg::FreshSource;
using namespace chudware::mfg::gateway;

struct TempDirectory {
    std::filesystem::path path;

    TempDirectory() {
#ifndef _WIN32
        std::string pattern = (std::filesystem::temp_directory_path() / "chudware-serial-XXXXXX").string();
        std::vector<char> writable(pattern.begin(), pattern.end());
        writable.push_back('\0');
        const char* made = ::mkdtemp(writable.data());
        REQUIRE_MESSAGE(made != nullptr, "mkdtemp failed: ", std::strerror(errno));
        path = made;
#else
        path = std::filesystem::temp_directory_path() / "chudware-serial-windows-skip";
#endif
    }

    ~TempDirectory() {
        std::error_code ignored;
        std::filesystem::remove_all(path, ignored);
    }
};

AllowlistEntry serialEntry(std::string path, std::string note = "PTY acceptance fixture") {
    return {
        EndpointIdentity::serial(std::move(path), "1a86", "7523", "CONTROLLER-42"),
        MachineClass::Emulated,
        {{"baud", "115200"}},
        std::move(note),
    };
}

TEST_CASE("commissioning allowlist is durable, class/config exact, and epoch tracked") {
    TempDirectory temporary;
    const auto file = temporary.path / "devices.allowlist";
    DeviceAllowlist allowlist(file);
    CHECK(allowlist.epoch() == 0);

    auto entry = serialEntry("/dev/serial/by-id/usb-Test_Controller_42");
    const std::string exact_line = DeviceAllowlist::formatLine(entry);
    std::string why;
    REQUIRE(allowlist.upsert(entry, &why));
    CHECK(allowlist.epoch() == 1);
    REQUIRE(allowlist.upsert(entry, &why));
    CHECK(allowlist.epoch() == 1);

    DeviceAllowlist reopened(file);
    REQUIRE(reopened.entries().size() == 1);
    CHECK(DeviceAllowlist::formatLine(reopened.entries().front()) == exact_line);

    auto wrong_class = entry;
    wrong_class.machine_class = MachineClass::OpenFrameRouter;
    const auto denied_class = reopened.authorize(wrong_class);
    CHECK_FALSE(denied_class.allowed);
    CHECK(denied_class.required_line == DeviceAllowlist::formatLine(wrong_class));
    CHECK(denied_class.why.find(denied_class.required_line) != std::string::npos);

    auto wrong_baud = entry;
    wrong_baud.connection["baud"] = "57600";
    const auto denied_baud = reopened.authorize(wrong_baud);
    CHECK_FALSE(denied_baud.allowed);
    CHECK(denied_baud.why.find("baud") != std::string::npos);

    entry.operator_note = "operator changed the commissioned fixture note";
    REQUIRE(allowlist.upsert(entry, &why));
    CHECK(allowlist.epoch() == 2);

    {
        std::ofstream human(file, std::ios::trunc);
        REQUIRE(human.is_open());
        auto externally_edited = entry;
        externally_edited.connection["baud"] = "57600";
        human << "# edited by operator\n" << DeviceAllowlist::formatLine(externally_edited) << '\n';
    }
    REQUIRE(allowlist.reload(&why));
    CHECK(allowlist.epoch() == 3);
    CHECK(allowlist.entries().front().connection.at("baud") == "57600");
}

TEST_CASE("serial commissioning refuses tupleless and incomplete USB identities") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    const std::array<EndpointIdentity, 4> invalid{
        EndpointIdentity::serial("/dev/ttyUSB0"),
        EndpointIdentity::serial("/dev/ttyUSB0", "1a86", "", "CONTROLLER-42"),
        EndpointIdentity::serial("/dev/ttyUSB0", "", "7523", "CONTROLLER-42"),
        EndpointIdentity::serial("/dev/ttyUSB0", "1a86", "7523", ""),
    };
    for (const auto& identity : invalid) {
        auto entry = serialEntry(identity.address);
        entry.endpoint = identity;
        std::string why;
        CHECK_FALSE(allowlist.upsert(entry, &why));
        CHECK(why.find("vid, pid, and serial together") != std::string::npos);
        CHECK_FALSE(allowlist.authorize(entry).allowed);
        CHECK_FALSE(DeviceAllowlist::parseLine(DeviceAllowlist::formatLine(entry), &why));
    }
    CHECK(allowlist.entries().empty());
    CHECK(allowlist.epoch() == 0);
}

TEST_CASE("network allowlist keys kind host port and validates websocket path") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "network.allowlist");
    AllowlistEntry entry{
        EndpointIdentity::network(TransportKind::WebSocket, "Controller.Local", 81),
        MachineClass::OpenFrameMill,
        {{"path", "/grbl"}, {"connect_timeout_ms", "1000"}},
        "commissioned FluidNC controller",
    };
    std::string why;
    REQUIRE(allowlist.upsert(entry, &why));

    CommissioningCandidate request{
        EndpointIdentity::network(TransportKind::WebSocket, "controller.local", 81),
        MachineClass::OpenFrameMill,
        {{"path", "/grbl"}},
        "request",
    };
    CHECK(allowlist.authorize(request).allowed);
    request.connection["path"] = "/wrong";
    CHECK_FALSE(allowlist.authorize(request).allowed);
    request.connection["path"] = "/grbl";
    request.endpoint.transport = TransportKind::Tcp;
    CHECK_FALSE(allowlist.authorize(request).allowed);

    Endpoint uri{"fluidnc", "ws://controller.local:81/grbl", true};
    CHECK(allowlist.allowed(uri, TransportKind::WebSocket));
}

#ifndef _WIN32

struct PseudoTerminal {
    int master = -1;
    std::string slave_path;

    PseudoTerminal() {
        master = ::posix_openpt(O_RDWR | O_NOCTTY | O_CLOEXEC);
        REQUIRE_MESSAGE(master >= 0, "posix_openpt failed: ", std::strerror(errno));
        REQUIRE_MESSAGE(::grantpt(master) == 0, "grantpt failed: ", std::strerror(errno));
        REQUIRE_MESSAGE(::unlockpt(master) == 0, "unlockpt failed: ", std::strerror(errno));
        std::vector<char> path(256);
        REQUIRE_MESSAGE(::ptsname_r(master, path.data(), path.size()) == 0,
                        "ptsname_r failed: ", std::strerror(errno));
        slave_path = path.data();
    }

    ~PseudoTerminal() {
        if (master >= 0) ::close(master);
    }

    void closePeer() {
        REQUIRE(master >= 0);
        REQUIRE(::close(master) == 0);
        master = -1;
    }
};
void writeTextFile(const std::filesystem::path& path, std::string_view value) {
    std::ofstream output(path);
    REQUIRE(output.is_open());
    output << value << '\n';
}

SerialTransportOptions fixtureOptions(const TempDirectory& temporary, std::string_view target,
                                      const AllowlistEntry& entry) {
    SerialTransportOptions options;
    options.operator_note = entry.operator_note;
    options.posix_sysfs_tty_root = (temporary.path / "sys-class-tty").string();
    const auto tty_path = std::filesystem::path(options.posix_sysfs_tty_root) /
                          std::filesystem::path(target).filename();
    const auto device_path = tty_path / "device";
    std::filesystem::create_directories(device_path);
    struct stat status {};
    REQUIRE(::stat(std::string(target).c_str(), &status) == 0);
    writeTextFile(tty_path / "dev", std::to_string(major(status.st_rdev)) + ":" +
                                      std::to_string(minor(status.st_rdev)));
    writeTextFile(device_path / "idVendor", entry.endpoint.usb_vid);
    writeTextFile(device_path / "idProduct", entry.endpoint.usb_pid);
    writeTextFile(device_path / "serial", entry.endpoint.usb_serial);
    return options;
}

TEST_CASE("POSIX serial refuses generic symlinks and non-character devices by name") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    PseudoTerminal pty;
    std::string why;

    const auto symlink_path = temporary.path / "serial-alias";
    std::filesystem::create_symlink(pty.slave_path, symlink_path);
    auto symlink_entry = serialEntry(symlink_path.string());
    REQUIRE(allowlist.upsert(symlink_entry, &why));

    SerialTransportOptions options;
    options.operator_note = symlink_entry.operator_note;
    options.posix_by_id_root = (temporary.path / "by-id").string();
    SerialTransport transport(allowlist, MachineClass::Emulated, options);
    const auto symlink_result =
        transport.open({"symlink", symlink_path.string(), false});
    CHECK_FALSE(symlink_result.ok);
    CHECK(symlink_result.why.find("serial symlink refused") != std::string::npos);

    const auto regular_path = temporary.path / "not-a-tty";
    writeTextFile(regular_path, "not a serial device");
    auto regular_entry = serialEntry(regular_path.string());
    REQUIRE(allowlist.upsert(regular_entry, &why));
    const auto regular_result =
        transport.open({"regular", regular_path.string(), false});
    CHECK_FALSE(regular_result.ok);
    CHECK(regular_result.why.find("not a character device") != std::string::npos);
}

TEST_CASE("POSIX by-id serial open verifies the commissioned sysfs USB tuple") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    PseudoTerminal pty;
    const auto by_id_root = temporary.path / "by-id";
    std::filesystem::create_directories(by_id_root);
    const auto by_id_path = by_id_root / "usb-controller";
    std::filesystem::create_symlink(pty.slave_path, by_id_path);

    const auto fixture_entry = serialEntry(by_id_path.string(), "USB tuple fixture");
    auto options = fixtureOptions(temporary, pty.slave_path, fixture_entry);
    options.posix_by_id_root = by_id_root.string();
    const auto tty_path = std::filesystem::path(options.posix_sysfs_tty_root) /
                          std::filesystem::path(pty.slave_path).filename();
    const auto sysfs_device = tty_path / "device";

    AllowlistEntry entry{
        EndpointIdentity::serial(by_id_path.string(), "1a86", "7523", "CONTROLLER-42"),
        MachineClass::Emulated,
        {{"baud", "115200"}},
        "USB tuple fixture",
    };
    std::string why;
    REQUIRE(allowlist.upsert(entry, &why));

    options.operator_note = entry.operator_note;
    SerialTransport transport(allowlist, MachineClass::Emulated, options);
    const Endpoint endpoint{"usb-grbl", by_id_path.string(), false};
    const auto matched = transport.open(endpoint);
    REQUIRE_MESSAGE(matched.ok, matched.why);
    transport.close();

    writeTextFile(sysfs_device / "serial", "SUBSTITUTED-DEVICE");
    const auto mismatched = transport.open(endpoint);
    CHECK_FALSE(mismatched.ok);
    CHECK(mismatched.why.find("serial USB identity mismatch") != std::string::npos);
    CHECK(mismatched.why.find("SUBSTITUTED-DEVICE") != std::string::npos);

    writeTextFile(sysfs_device / "serial", entry.endpoint.usb_serial);
    writeTextFile(tty_path / "dev", "1:3");
    const auto wrong_device_number = transport.open(endpoint);
    CHECK_FALSE(wrong_device_number.ok);
    CHECK(wrong_device_number.why.find("sysfs device number differs") != std::string::npos);

    writeTextFile(tty_path / "dev", "invalid:device");
    const auto malformed_device_number = transport.open(endpoint);
    CHECK_FALSE(malformed_device_number.ok);
    CHECK(malformed_device_number.why.find("sysfs device number is invalid") != std::string::npos);

    std::filesystem::remove(tty_path / "dev");
    const auto missing_device_number = transport.open(endpoint);
    CHECK_FALSE(missing_device_number.ok);
    CHECK(missing_device_number.why.find("sysfs device number is unavailable") != std::string::npos);

    options = fixtureOptions(temporary, pty.slave_path, entry);
    options.posix_by_id_root = by_id_root.string();
    std::filesystem::remove(sysfs_device / "serial");
    SerialTransport incomplete(allowlist, MachineClass::Emulated, options);
    const auto missing_serial = incomplete.open(endpoint);
    CHECK_FALSE(missing_serial.ok);
    CHECK(missing_serial.why.find("sysfs USB tuple is incomplete") != std::string::npos);
}

TEST_CASE("POSIX tupleless by-id commissioning cannot authorize either replacement PTY") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    PseudoTerminal original;
    PseudoTerminal replacement;
    const auto root = temporary.path / "by-id";
    std::filesystem::create_directories(root);
    const auto alias = root / "usb-controller";
    std::filesystem::create_symlink(original.slave_path, alias);
    auto entry = serialEntry(alias.string());
    entry.endpoint = EndpointIdentity::serial(alias.string());
    std::string why;
    CHECK_FALSE(allowlist.upsert(entry, &why));
    CHECK(why.find("vid, pid, and serial together") != std::string::npos);
    SerialTransportOptions options;
    options.posix_by_id_root = root.string();
    SerialTransport transport(allowlist, MachineClass::Emulated, options);
    CHECK_FALSE(transport.open({"tupleless", alias.string(), true}).ok);
    std::filesystem::remove(alias);
    std::filesystem::create_symlink(replacement.slave_path, alias);
    CHECK_FALSE(transport.open({"tupleless", alias.string(), false}).ok);
    CHECK_FALSE(transport.isOpen());
}

TEST_CASE("POSIX serial refuses symlinks in device path parents") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    PseudoTerminal pty;
    const auto alias_root = temporary.path / "aliased-pts";
    std::filesystem::create_directory_symlink(
        std::filesystem::path(pty.slave_path).parent_path(), alias_root);
    const auto alias = alias_root / std::filesystem::path(pty.slave_path).filename();
    const auto entry = serialEntry(alias.string());
    std::string why;
    REQUIRE(allowlist.upsert(entry, &why));
    auto options = fixtureOptions(temporary, pty.slave_path, entry);
    SerialTransport transport(allowlist, MachineClass::Emulated, options);
    const auto result = transport.open({"parent-symlink", alias.string(), false});
    CHECK_FALSE(result.ok);
    CHECK(result.why.find("serial symlink refused") != std::string::npos);
    CHECK_FALSE(transport.isOpen());
}

TEST_CASE("POSIX serial binds the opened descriptor to the pre-open sysfs device number") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    PseudoTerminal pty;
    const auto entry = serialEntry(pty.slave_path);
    std::string why;
    REQUIRE(allowlist.upsert(entry, &why));
    auto options = fixtureOptions(temporary, pty.slave_path, entry);
    const auto tty_path = std::filesystem::path(options.posix_sysfs_tty_root) /
                          std::filesystem::path(pty.slave_path).filename();
    const auto vendor_path = tty_path / "device" / "idVendor";
    std::filesystem::remove(vendor_path);
    REQUIRE(::mkfifo(vendor_path.c_str(), 0600) == 0);
    std::atomic<bool> fixture_failed = false;
    // The first dev read precedes idVendor. Change the sysfs mapping while that
    // first tuple read is paused, then restore a regular vendor attribute so the
    // descriptor's post-open tuple read cannot block.
    std::thread substitution([&] {
        int writer = -1;
        const auto deadline = std::chrono::steady_clock::now() + 5s;
        while (std::chrono::steady_clock::now() < deadline) {
            writer = ::open(vendor_path.c_str(), O_WRONLY | O_NONBLOCK | O_CLOEXEC);
            if (writer >= 0) break;
            if (errno != ENXIO && errno != EINTR) {
                fixture_failed = true;
                return;
            }
            std::this_thread::sleep_for(1ms);
        }
        if (writer < 0) {
            fixture_failed = true;
            return;
        }
        std::error_code error;
        std::filesystem::remove(vendor_path, error);
        std::ofstream vendor(vendor_path);
        std::ofstream number(tty_path / "dev", std::ios::trunc);
        vendor << "1a86\n";
        number << "1:3\n";
        vendor.close();
        number.close();
        if (error || !vendor || !number || ::write(writer, "1a86\n", 5) != 5)
            fixture_failed = true;
        ::close(writer);
    });
    SerialTransport transport(allowlist, MachineClass::Emulated, options);
    const auto result = transport.open({"descriptor-substitution", pty.slave_path, false});
    substitution.join();
    CHECK_FALSE(fixture_failed.load());
    CHECK_FALSE(result.ok);
    CHECK(result.why.find("opened descriptor differs") != std::string::npos);
    CHECK_FALSE(transport.isOpen());
}


TEST_CASE("POSIX serial transport enforces allowlist and exclusive ownership") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    PseudoTerminal pty;
    std::string why;
    const auto entry = serialEntry(pty.slave_path);
    REQUIRE(allowlist.upsert(entry, &why));

    auto options = fixtureOptions(temporary, pty.slave_path, entry);
    SerialTransport first(allowlist, MachineClass::Emulated, options);
    Endpoint endpoint{"pty-grbl", pty.slave_path, false};
    const auto first_open = first.open(endpoint);
    REQUIRE_MESSAGE(first_open.ok, first_open.why);
    CHECK(first.kind() == TransportKind::Serial);
    CHECK(first.evidenceSource() == FreshSource::Simulated);

    SerialTransport second(allowlist, MachineClass::Emulated, options);
    const auto second_open = second.open(endpoint);
    CHECK_FALSE(second_open.ok);
    const bool exclusive_refusal = second_open.why.find("exclusive") != std::string::npos ||
                                   second_open.why.find("claimed") != std::string::npos;
    CHECK(exclusive_refusal);

    PseudoTerminal missing;
    Endpoint denied_endpoint{"not-commissioned", missing.slave_path, false};
    const auto denied = second.open(denied_endpoint);
    const auto exact_missing = DeviceAllowlist::formatLine({
        EndpointIdentity::serial(missing.slave_path), MachineClass::Emulated,
        {{"baud", "115200"}}, options.operator_note});
    CHECK_FALSE(denied.ok);
    CHECK(denied.why.find(missing.slave_path) != std::string::npos);
    CHECK(denied.why.find(exact_missing) != std::string::npos);

    first.close();
    REQUIRE(second.open(endpoint).ok);
    second.close();
}

TEST_CASE("POSIX serial max deadline services silently without awaiting data") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    PseudoTerminal pty;
    const auto entry = serialEntry(pty.slave_path);
    std::string why;
    REQUIRE(allowlist.upsert(entry, &why));
    auto options = fixtureOptions(temporary, pty.slave_path, entry);
    SerialTransport transport(allowlist, MachineClass::Emulated, options);
    REQUIRE(transport.open({"pty-grbl", pty.slave_path, false}).ok);

    std::thread delayed_peer([&] {
        std::this_thread::sleep_for(200ms);
        const char byte = '?';
        (void)::write(pty.master, &byte, 1);
    });
    const auto polled = transport.poll(chudware::device::Deadline::max());
    delayed_peer.join();
    CHECK(polled.events.empty());
}

TEST_CASE("POSIX serial handles partial writes and bounded chunked status reads") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    PseudoTerminal pty;
    auto entry = serialEntry(pty.slave_path);
    std::string why;
    REQUIRE(allowlist.upsert(entry, &why));

    auto options = fixtureOptions(temporary, pty.slave_path, entry);
    options.max_read_bytes = 11;
    options.write_timeout = 5s;
    SerialTransport transport(allowlist, MachineClass::Emulated, options);
    REQUIRE(transport.open({"pty-grbl", pty.slave_path, false}).ok);

    std::vector<std::byte> payload(512 * 1024);
    for (std::size_t i = 0; i < payload.size(); ++i)
        payload[i] = static_cast<std::byte>(i % 251);
    std::vector<std::byte> received;
    received.reserve(payload.size());
    std::atomic<bool> reader_failed = false;
    std::thread reader([&] {
        std::this_thread::sleep_for(25ms);  // ensure the nonblocking first write is partial
        std::array<std::byte, 4096> chunk{};
        while (received.size() < payload.size()) {
            const ssize_t count = ::read(pty.master, chunk.data(), chunk.size());
            if (count > 0) {
                received.insert(received.end(), chunk.begin(), chunk.begin() + count);
                continue;
            }
            if (count < 0 && errno == EINTR) continue;
            reader_failed = true;
            return;
        }
    });
    const auto write_result = transport.write(payload);
    reader.join();
    REQUIRE_MESSAGE(write_result.ok, write_result.why);
    CHECK(write_result.bytes_written == payload.size());
    CHECK_FALSE(reader_failed.load());
    CHECK(received == payload);

    const std::string first = "<Idle|MPos:0.000,";
    const std::string second = "0.000,0.000|FS:0,0>\r\n";
    REQUIRE(::write(pty.master, first.data(), first.size()) == static_cast<ssize_t>(first.size()));
    std::string assembled;
    while (assembled.size() < first.size()) {
        const auto polled = transport.poll(std::chrono::steady_clock::now() + 250ms);
        REQUIRE(polled.events.size() == 1);
        CHECK(polled.events.front().kind == TransportEvent::Kind::Data);
        CHECK(polled.events.front().data.size() <= options.max_read_bytes);
        assembled += polled.events.front().raw;
    }
    REQUIRE(::write(pty.master, second.data(), second.size()) == static_cast<ssize_t>(second.size()));
    while (assembled.size() < first.size() + second.size()) {
        const auto polled = transport.poll(std::chrono::steady_clock::now() + 250ms);
        REQUIRE(polled.events.size() == 1);
        CHECK(polled.events.front().kind == TransportEvent::Kind::Data);
        CHECK(polled.events.front().data.size() <= options.max_read_bytes);
        assembled += polled.events.front().raw;
    }
    CHECK(assembled == first + second);
}

TEST_CASE("POSIX serial reports device_lost and capability remains transport-only") {
    TempDirectory temporary;
    DeviceAllowlist allowlist(temporary.path / "devices.allowlist");
    PseudoTerminal pty;
    auto entry = serialEntry(pty.slave_path);
    std::string why;
    REQUIRE(allowlist.upsert(entry, &why));
    auto options = fixtureOptions(temporary, pty.slave_path, entry);
    SerialTransport transport(allowlist, MachineClass::Emulated, options);
    REQUIRE(transport.open({"pty-grbl", pty.slave_path, true}).ok);

    // A PTY is virtual even if a caller incorrectly labels the endpoint physical.
    CHECK(transport.evidenceSource() == FreshSource::Simulated);
    CHECK(SerialTransport::evidenceSourceForEndpoint(
              {"real-controller", "/dev/serial/by-id/usb-Controller_42", true}) ==
          FreshSource::Probed);
    CHECK(SerialTransport::evidenceSourceForEndpoint(
              {"untrusted-flag", "/dev/serial/by-id/usb-Controller_42", false}) ==
          FreshSource::Simulated);
#ifdef __APPLE__
    CHECK(SerialTransport::evidenceSourceForEndpoint(
              {"darwin-pty", "/dev/ttys001", true}) ==
          FreshSource::Simulated);
    CHECK(SerialTransport::evidenceSourceForEndpoint(
              {"darwin-controller", "/dev/cu.usbserial-Controller_42", true}) ==
          FreshSource::Probed);
#endif
    const auto report = transport.capabilityReport("dev:emulated:test", "res:device_identity:test");
    REQUIRE(report.complete());
    CHECK(report.max_advertised == CapabilityLevel::Transport);
    CHECK(transport.maxCapability() == CapabilityLevel::Transport);
    CHECK(report.levels.at(CapabilityLevel::Transport).truth == CapabilityTruthValue::Verified);
    CHECK(report.levels.at(CapabilityLevel::Execute).truth == CapabilityTruthValue::Assumed);
    REQUIRE(report.levels.at(CapabilityLevel::Execute).missing_level);
    CHECK(*report.levels.at(CapabilityLevel::Execute).missing_level == CapabilityLevel::Execute);
    CHECK(report.levels.at(CapabilityLevel::SafetyIntegration).truth ==
          CapabilityTruthValue::Unsupported);

    pty.closePeer();
    const auto lost = transport.poll(std::chrono::steady_clock::now() + 500ms);
    REQUIRE(lost.events.size() == 1);
    CHECK(lost.events.front().kind == TransportEvent::Kind::Error);
    CHECK(lost.events.front().why.find("device_lost") != std::string::npos);
    CHECK_FALSE(transport.isOpen());
}

#else

TEST_CASE("POSIX PTY serial acceptance paths are unavailable on Windows: requires posix_openpt"
          * doctest::skip(true)) {}

#endif

}  // namespace
