#include "manufacturing/journal.h"

#include <chrono>
#include <cerrno>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <system_error>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#include <share.h>
#include <sys/stat.h>
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace chudware::mfg {
namespace {

#ifdef _WIN32
constexpr int kOpenFlags = _O_CREAT | _O_RDWR | _O_APPEND | _O_BINARY;
int open_file(const std::filesystem::path& path, int flags = kOpenFlags) {
    return _open(path.string().c_str(), flags, _S_IREAD | _S_IWRITE);
}
int open_lock_file(const std::filesystem::path& path) {
    int fd = -1;
    const errno_t error = _wsopen_s(
        &fd, path.c_str(), _O_CREAT | _O_RDWR | _O_BINARY, _SH_DENYRW,
        _S_IREAD | _S_IWRITE);
    if (error != 0) errno = error;
    return fd;
}
void replace_file(const std::filesystem::path& source,
                  const std::filesystem::path& destination) {
    if (!::MoveFileExW(source.c_str(), destination.c_str(),
                       MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        throw std::system_error(
            static_cast<int>(::GetLastError()), std::system_category(),
            "replace journal checkpoint");
    }
}
int close_file(int fd) { return _close(fd); }
int sync_file(int fd) { return _commit(fd); }
int64_t seek_file(int fd, int64_t offset, int origin) { return _lseeki64(fd, offset, origin); }
int truncate_file(int fd, uint64_t size) { return _chsize_s(fd, size); }
int write_file(int fd, const char* bytes, unsigned count) { return _write(fd, bytes, count); }
int read_file(int fd, char* bytes, unsigned count) { return _read(fd, bytes, count); }
#else
constexpr int kOpenFlags = O_CREAT | O_RDWR | O_APPEND | O_CLOEXEC;
int open_file(const std::filesystem::path& path, int flags = kOpenFlags) {
    return ::open(path.c_str(), flags, 0600);
}
int open_lock_file(const std::filesystem::path& path) {
    const int fd = ::open(path.c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (fd < 0) return fd;
    if (::flock(fd, LOCK_EX | LOCK_NB) == 0) return fd;
    const int lock_errno = errno;
    ::close(fd);
    errno = lock_errno;
    return -1;
}
void replace_file(const std::filesystem::path& source,
                  const std::filesystem::path& destination) {
    std::filesystem::rename(source, destination);
}
int close_file(int fd) { return ::close(fd); }
#ifdef __APPLE__
int sync_file(int fd) {
    // Journal ordering must survive power loss. Darwin's fsync may leave data in the
    // drive cache, so prefer F_FULLFSYNC; unsupported filesystems fall back to fsync's
    // weaker best-effort durability rather than rejecting an otherwise valid write.
    if (::fcntl(fd, F_FULLFSYNC) == 0) return 0;
    return ::fsync(fd);
}
#else
int sync_file(int fd) { return ::fdatasync(fd); }
#endif
off_t seek_file(int fd, off_t offset, int origin) { return ::lseek(fd, offset, origin); }
int truncate_file(int fd, uint64_t size) { return ::ftruncate(fd, static_cast<off_t>(size)); }
ssize_t write_file(int fd, const char* bytes, size_t count) { return ::write(fd, bytes, count); }
ssize_t read_file(int fd, char* bytes, size_t count) { return ::read(fd, bytes, count); }
#endif

[[noreturn]] void throw_io(std::string_view operation) {
    throw std::system_error(errno, std::generic_category(), std::string(operation));
}

void write_all(int fd, std::string_view bytes) {
    size_t written = 0;
    while (written < bytes.size()) {
        const auto amount = write_file(fd, bytes.data() + written, bytes.size() - written);
        if (amount < 0) {
            if (errno == EINTR) continue;
            throw_io("journal write");
        }
        if (amount == 0) throw std::runtime_error("journal write made no progress");
        written += static_cast<size_t>(amount);
    }
}

uint64_t monotonic_ns() {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

std::vector<JournalRecord> read_records(const std::filesystem::path& path, device::Seq from) {
    std::ifstream input(path, std::ios::binary);
    if (!input && std::filesystem::exists(path)) throw std::runtime_error("cannot read journal");
    std::vector<JournalRecord> records;
    std::string line;
    size_t line_number = 0;
    while (std::getline(input, line)) {
        ++line_number;
        if (line.empty()) continue;
        try {
            auto record = nlohmann::json::parse(line).get<JournalRecord>();
            if (record.seq >= from) records.push_back(std::move(record));
        } catch (const std::exception& error) {
            throw std::runtime_error("corrupt journal line " + std::to_string(line_number) +
                                     ": " + error.what());
        }
    }
    return records;
}

void repair_torn_tail(int fd) {
    const auto end = seek_file(fd, 0, SEEK_END);
    if (end < 0) throw_io("journal seek");
    if (end == 0) return;
    char byte = 0;
    if (seek_file(fd, end - 1, SEEK_SET) < 0 || read_file(fd, &byte, 1) != 1)
        throw_io("journal tail read");
    if (byte == '\n') {
        if (seek_file(fd, 0, SEEK_END) < 0) throw_io("journal seek end");
        return;
    }
    auto position = end - 1;
    while (position > 0) {
        --position;
        if (seek_file(fd, position, SEEK_SET) < 0 || read_file(fd, &byte, 1) != 1)
            throw_io("journal tail scan");
        if (byte == '\n') {
            ++position;
            break;
        }
    }
    if (position == 0 && byte != '\n') position = 0;
    if (truncate_file(fd, static_cast<uint64_t>(position)) != 0 || sync_file(fd) != 0)
        throw_io("journal tail truncate");
    if (seek_file(fd, 0, SEEK_END) < 0) throw_io("journal seek end");
}

}  // namespace

void to_json(nlohmann::json& json, const JournalRecord& value) {
    json = {{"seq", value.seq}, {"epoch", value.epoch}, {"kind", value.kind},
            {"subject", value.subject}, {"body", value.body}, {"raw", value.raw},
            {"t_mono_ns", value.t_mono_ns}};
}

void from_json(const nlohmann::json& json, JournalRecord& value) {
    const auto epoch = device::LaneEpoch::parse(json.at("epoch"));
    if (!epoch) throw std::invalid_argument("invalid journal LaneEpoch");
    value.seq = json.at("seq").get<device::Seq>();
    value.epoch = *epoch;
    value.kind = json.at("kind").get<std::string>();
    value.subject = json.at("subject").get<std::string>();
    value.body = json.at("body");
    value.raw = json.value("raw", nlohmann::json(nullptr));
    value.t_mono_ns = json.at("t_mono_ns").get<uint64_t>();
    if (value.seq == 0 || value.kind.empty() || value.subject.empty())
        throw std::invalid_argument("invalid journal record");
}

struct Journal::Impl {
    std::filesystem::path directory;
    std::filesystem::path journal_path;
    std::filesystem::path checkpoint_path;
    std::filesystem::path lock_path;
    device::LaneEpoch epoch;
    int journal_fd = -1;
    int lock_fd = -1;
    device::Seq current_head = 0;
    device::Seq epoch_floor = 1;

    Impl(std::filesystem::path path, device::LaneEpoch current_epoch)
        : directory(std::move(path)), journal_path(directory / "journal.jsonl"),
          checkpoint_path(directory / "seq.head"), lock_path(directory / "writer.lock"),
          epoch(std::move(current_epoch)) {
        std::filesystem::create_directories(directory);
        lock_fd = open_lock_file(lock_path);
        if (lock_fd < 0) throw std::runtime_error("journal already has a writer");
        journal_fd = open_file(journal_path);
        if (journal_fd < 0) throw_io("journal open");
        repair_torn_tail(journal_fd);

        const auto records = read_records(journal_path, 1);
        for (const auto& record : records) current_head = std::max(current_head, record.seq);
        device::Seq checkpoint_head = 0;
        {
            // Windows file streams do not share delete access. End the read handle
            // before persist_head atomically replaces the checkpoint below.
            std::ifstream checkpoint(checkpoint_path);
            if (checkpoint) checkpoint >> checkpoint_head;
        }
        current_head = std::max(current_head, checkpoint_head);
        epoch_floor = current_head + 1;
        for (const auto& record : records) {
            if (record.epoch == epoch) {
                epoch_floor = std::min(epoch_floor, record.seq);
            }
        }
        persist_head(current_head);
    }

    ~Impl() {
        if (journal_fd >= 0) close_file(journal_fd);
#ifndef _WIN32
        if (lock_fd >= 0) ::flock(lock_fd, LOCK_UN);
#endif
        if (lock_fd >= 0) close_file(lock_fd);
    }

    void persist_head(device::Seq value) {
        const auto temporary = checkpoint_path.string() + ".tmp." + epoch.process_start_id;
        int fd = open_file(temporary,
#ifdef _WIN32
                                 _O_CREAT | _O_TRUNC | _O_WRONLY | _O_BINARY
#else
                                 O_CREAT | O_TRUNC | O_WRONLY | O_CLOEXEC
#endif
        );
        if (fd < 0) throw_io("seq checkpoint open");
        try {
            const std::string bytes = std::to_string(value) + "\n";
            write_all(fd, bytes);
            if (sync_file(fd) != 0) throw_io("seq checkpoint sync");
            const int close_result = close_file(fd);
            fd = -1;
            if (close_result != 0) throw_io("seq checkpoint close");
            replace_file(temporary, checkpoint_path);
#ifndef _WIN32
            const int directory_fd = ::open(directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
            if (directory_fd >= 0) {
                ::fsync(directory_fd);
                ::close(directory_fd);
            }
#endif
        } catch (...) {
            if (fd >= 0) close_file(fd);
            std::error_code ignored;
            std::filesystem::remove(temporary, ignored);
            throw;
        }
    }
};

Journal::Journal(std::filesystem::path install_directory, device::LaneEpoch epoch)
    : impl_(std::make_unique<Impl>(std::move(install_directory), std::move(epoch))) {}
Journal::~Journal() = default;
Journal::Journal(Journal&&) noexcept = default;
Journal& Journal::operator=(Journal&&) noexcept = default;

device::Seq Journal::append(std::string_view kind, std::string_view subject,
                            nlohmann::json body, nlohmann::json raw) {
    if (kind.empty() || subject.empty()) throw std::invalid_argument("journal kind and subject are required");
    if (impl_->current_head == std::numeric_limits<device::Seq>::max())
        throw std::overflow_error("journal seq exhausted");
    const device::Seq next = impl_->current_head + 1;
    JournalRecord record{next, impl_->epoch, std::string(kind), std::string(subject),
                         std::move(body), std::move(raw), monotonic_ns()};
    const std::string bytes = nlohmann::json(record).dump() + "\n";

    impl_->persist_head(next);  // reserve before append: a crash can create a gap, never reuse a seq
    impl_->current_head = next;
    write_all(impl_->journal_fd, bytes);
    if (sync_file(impl_->journal_fd) != 0) throw_io("journal sync");
    if (impl_->epoch_floor > next) impl_->epoch_floor = next;
    return next;
}

device::Seq Journal::head() const { return impl_->current_head; }
device::Seq Journal::epochFloor() const { return impl_->epoch_floor; }
std::vector<JournalRecord> Journal::replay(device::Seq from) const {
    return read_records(impl_->journal_path, from);
}

}  // namespace chudware::mfg
