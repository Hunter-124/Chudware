#include "ui/file_dialog.h"
#include "ui/web_capabilities.h"

#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
#include <unistd.h>
namespace fs = std::filesystem;

static void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
static void set(const char* name, const std::string& value) {
    require(::setenv(name, value.c_str(), 1) == 0, "set fixture environment");
}
static std::string value(const char* name) {
    const char* p = std::getenv(name);
    return p ? p : "";
}
static int recorder(int argc, char** argv) {
    const std::string tool = fs::path(argv[0]).filename().string();
    if (tool == "touch") {
        // The old shell-injection sink would execute this harmless, owned sentinel writer.
        if (argc == 2 && std::string(argv[1]) == "SENTINEL")
            std::ofstream(fs::path(value("CHUDWARE_EXTERNAL_TEST_ROOT")) / "SENTINEL") << "bad";
        return 0;
    }
    const fs::path log = fs::path(value("CHUDWARE_EXTERNAL_TEST_LOG")) /
                         (tool + "-" + std::to_string(::getpid()) + ".args");
    const fs::path staging = log.string() + ".tmp";
    std::ofstream out(staging, std::ios::binary);
    for (int i = 0; i < argc; ++i) {
        const std::string arg(argv[i]);
        const std::uint32_t size = static_cast<std::uint32_t>(arg.size());
        out.write(reinterpret_cast<const char*>(&size), sizeof(size));
        out.write(arg.data(), static_cast<std::streamsize>(arg.size()));
    }
    out.close();
    fs::rename(staging, log);
    if (tool == "kdialog" || tool == "zenity") {
        const auto flood = value("CHUDWARE_EXTERNAL_TEST_FLOOD");
        if (!flood.empty()) std::cout << std::string(65537, 'x');
        else std::cout << value("CHUDWARE_EXTERNAL_TEST_PICK") << '\n';
    }
    return tool == "dbus-send" || tool == "open"
               ? std::atoi(value("CHUDWARE_EXTERNAL_TEST_REVEAL_STATUS").c_str())
               : (tool == "kdialog" || tool == "zenity")
                     ? std::atoi(value("CHUDWARE_EXTERNAL_TEST_DIALOG_STATUS").c_str()) : 0;
}
static std::vector<std::string> readArgs(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    std::vector<std::string> result;
    for (;;) {
        std::uint32_t size = 0;
        in.read(reinterpret_cast<char*>(&size), sizeof(size));
        if (in.eof()) break;
        require(in.good() && size <= 65536, "bounded recorder arg");
        std::string arg(size, '\0');
        in.read(arg.data(), size);
        require(in.good(), "complete recorder arg");
        result.push_back(std::move(arg));
    }
    return result;
}
static std::vector<std::vector<std::string>> waitArgs(const fs::path& dir, size_t count) {
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
    for (;;) {
        std::vector<std::vector<std::string>> results;
        for (const auto& item : fs::directory_iterator(dir))
            if (item.path().extension() == ".args") results.push_back(readArgs(item.path()));
        if (results.size() >= count) {
            require(results.size() == count, "exact launcher count");
            return results;
        }
        require(std::chrono::steady_clock::now() < deadline, "launcher recorder completed");
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
}
#ifndef __APPLE__
static std::string uri(const std::string& path) {
    constexpr char hex[] = "0123456789ABCDEF";
    std::string out = "file://";
    for (unsigned char c : path) {
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '/' || c == '.' || c == '-' || c == '_' || c == '~')
            out += static_cast<char>(c);
        else { out += '%'; out += hex[c >> 4]; out += hex[c & 15]; }
    }
    return out;
}
#endif
#endif

int main(int argc, char** argv) {
#if defined(_WIN32) || defined(__EMSCRIPTEN__)
    std::cout << "SKIP: this PATH-recorder fixture cannot intercept native Windows OS APIs or WASM window.open\n";
    return 77;
#else
    try {
        if (fs::path(argv[0]).filename() == "dbus-send" ||
            fs::path(argv[0]).filename() == "xdg-open" ||
            fs::path(argv[0]).filename() == "open" ||
            fs::path(argv[0]).filename() == "kdialog" ||
            fs::path(argv[0]).filename() == "zenity" ||
            fs::path(argv[0]).filename() == "touch") return recorder(argc, argv);
        const auto executable = fs::absolute(argv[0]);
        std::string pattern = (fs::temp_directory_path() / "chudware-open-XXXXXX").string();
        std::vector<char> buffer(pattern.begin(), pattern.end()); buffer.push_back('\0');
        char* made = ::mkdtemp(buffer.data());
        require(made != nullptr, "private scratch root");
        const fs::path root(made);
        struct Cleanup {
            fs::path root, cwd;
            ~Cleanup() { std::error_code ec; fs::current_path(cwd, ec); fs::remove_all(root, ec); }
        } cleanup{root, fs::current_path()};
        fs::create_directory(root / "bin"); fs::create_directory(root / "empty");
        for (const char* tool : {"dbus-send", "xdg-open", "open", "kdialog", "zenity", "touch"})
            fs::copy_file(executable, root / "bin" / tool);
        set("PATH", (root / "bin").string());
        set("CHUDWARE_EXTERNAL_TEST_ROOT", root.string());
        set("CHUDWARE_EXTERNAL_TEST_REVEAL_STATUS", "0");
        set("CHUDWARE_EXTERNAL_TEST_DIALOG_STATUS", "0");
        fs::current_path(root);
        size_t seq = 0;
        auto logs = [&] {
            const fs::path p = root / ("logs-" + std::to_string(++seq));
            fs::create_directory(p); set("CHUDWARE_EXTERNAL_TEST_LOG", p.string()); return p;
        };
        const std::vector<std::string> names = {
            "ordinary photo.png", "-leading.png", "quote\" apostrophe' ;&|<>$.png",
            "line\nbreak\n.png", "unicode-\xe9\x9b\xaa-\xf0\x9f\x93\x90.png",
            "sample$(touch SENTINEL).png", "sample`touch SENTINEL`.png", "comma,#%?.png"
        };
        for (const auto& name : names) {
            std::ofstream(root / name) << "inert";
            auto dir = logs();
            require(ui::openExternalFile(name), "file opener launched");
            auto args = waitArgs(dir, 1).front();
            require(args.size() == 2 && args[1] == (root / name).string(), "exact file argv");
            dir = logs(); ui::revealInFileManager(name); args = waitArgs(dir, 1).front();
#ifdef __APPLE__
            require(args == std::vector<std::string>{"open", "-R", (root / name).string()}, "exact Finder reveal argv");
#else
            require(args == std::vector<std::string>{"dbus-send", "--session", "--print-reply", "--reply-timeout=2000",
                        "--dest=org.freedesktop.FileManager1", "--type=method_call", "/org/freedesktop/FileManager1",
                        "org.freedesktop.FileManager1.ShowItems", "array:string:" + uri((root / name).string()), "string:"},
                    "exact FileManager1 selected URI");
#endif
            require(!fs::exists(root / "SENTINEL"), "filename cannot execute shell substitution");
        }
        set("CHUDWARE_EXTERNAL_TEST_REVEAL_STATUS", "1");
        auto dir = logs(); ui::revealInFileManager((root / names.front()).string());
        const auto fallback = waitArgs(dir, 2);
        bool foundFolder = false;
        for (const auto& args : fallback)
            if (args.size() == 2 && args[1] == root.string()) foundFolder = true;
        require(foundFolder, "failed selection falls back to containing folder");
        set("CHUDWARE_EXTERNAL_TEST_REVEAL_STATUS", "0");
#ifndef __APPLE__
        fs::remove(root / "bin" / "dbus-send");
        dir = logs(); ui::revealInFileManager((root / names.front()).string());
        const auto missing = waitArgs(dir, 1).front();
        require(missing == std::vector<std::string>{"xdg-open", root.string()}, "missing FileManager1 tool falls back");
#endif
        dir = logs(); ui::revealInFileManager(root.string());
        require(waitArgs(dir, 1).front().at(1) == root.string(), "directory opens itself");
        require(!ui::openExternalFile(std::string("a\0b", 3)) && !ui::openExternalFile(""), "invalid local paths refused");

        for (const std::string& url : {std::string("https://example.invalid/a?'quote'&x=$(touch${IFS}SENTINEL)"),
                                      std::string("HTTP://example.invalid/path#anchor")}) {
            dir = logs(); require(ui::openExternalHttpUrl(url), "safe web scheme launched");
            const auto args = waitArgs(dir, 1).front();
            require(args.size() == 2 && args[1] == url, "exact URL argv");
        }
        for (const std::string& url : {"file:///tmp/x", "javascript:alert(1)", "data:text/plain,x", "-x",
                                      "https://user:pass@example.invalid", "https://example.invalid/a\nb",
                                      "https://example.invalid/a b", "https://example.invalid/\"x"})
            require(!ui::openExternalHttpUrl(url), "unsafe URL refused");
        require(!fs::exists(root / "SENTINEL"), "URL cannot execute shell substitution");

        const std::string picked = (root / "picked'$(touch SENTINEL)\n").string();
        set("CHUDWARE_EXTERNAL_TEST_PICK", picked);
        set("CHUDWARE_START_DIR", root.string());
        dir = logs();
        require(ui::saveFileDialog("All\0*.*\0", nullptr, names[2].c_str()) == picked,
                "dialog preserves exact filename including final newline");
        auto args = waitArgs(dir, 1).front();
        require(args.size() == 4 && args[2] == (root / names[2]).string(), "kdialog exact suggested filename argv");
        set("CHUDWARE_EXTERNAL_TEST_DIALOG_STATUS", "1"); logs();
        require(ui::openFileDialog("All\0*.*\0").empty(), "cancelled dialog output refused");
        set("CHUDWARE_EXTERNAL_TEST_DIALOG_STATUS", "0");
        set("CHUDWARE_EXTERNAL_TEST_FLOOD", "1"); logs();
        require(ui::openFileDialog("All\0*.*\0").empty(), "oversized dialog output refused");
        set("CHUDWARE_EXTERNAL_TEST_FLOOD", "");
        fs::remove(root / "bin" / "kdialog"); dir = logs();
        require(ui::openFileDialog("All\0*.*\0") == picked, "zenity exact selected filename");
        args = waitArgs(dir, 1).front();
        require(args.size() == 4 && args[2] == "--filename=" + root.string() + "/",
                "zenity exact path argv");
        require(!fs::exists(root / "SENTINEL"), "dialog filename cannot execute shell substitution");
        set("PATH", (root / "empty").string());
        require(!ui::openExternalFile(names.front()) && !ui::openExternalHttpUrl("https://example.invalid"),
                "missing opener returns launch failure");
        std::cout << "external file/URL argv and dialog regressions passed\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << "FAILED: " << error.what() << '\n'; return 1; }
#endif
}
