#include <cstddef>
#include <exception>
#include <limits>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <bcrypt.h>
#elif defined(__APPLE__)
#  include <Security/Security.h>
#elif defined(__EMSCRIPTEN__)
#  include <emscripten.h>
#else
#  include <cerrno>
#  include <fcntl.h>
#  include <sys/random.h>
#  include <unistd.h>
#endif

extern "C" void randombytes(unsigned char* out, unsigned long long length) {
#if defined(_WIN32)
    while (length != 0) {
        const ULONG chunk = static_cast<ULONG>(
            length > std::numeric_limits<ULONG>::max()
                ? std::numeric_limits<ULONG>::max()
                : length);
        if (BCryptGenRandom(nullptr, out, chunk, BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0)
            std::terminate();
        out += chunk;
        length -= chunk;
    }
    return;
#elif defined(__APPLE__)
    if (length <= std::numeric_limits<std::size_t>::max() &&
        SecRandomCopyBytes(kSecRandomDefault, static_cast<std::size_t>(length), out) == errSecSuccess)
        return;
#elif defined(__EMSCRIPTEN__)
    if (length <= std::numeric_limits<std::size_t>::max() &&
        EM_ASM_INT({
            const view = HEAPU8.subarray($0, $0 + $1);
            if (!globalThis.crypto || typeof globalThis.crypto.getRandomValues !== 'function')
                return 0;
            const max = 65536;
            for (let offset = 0; offset < view.length; offset += max)
                globalThis.crypto.getRandomValues(view.subarray(offset, Math.min(offset + max, view.length)));
            return 1;
        }, out, static_cast<std::size_t>(length)))
        return;
#else
    unsigned long long done = 0;
    while (done < length) {
        const std::size_t chunk = static_cast<std::size_t>(
            length - done > std::numeric_limits<std::size_t>::max()
                ? std::numeric_limits<std::size_t>::max()
                : length - done);
        const ssize_t n = ::getrandom(out + done, chunk, 0);
        if (n > 0) {
            done += static_cast<unsigned long long>(n);
            continue;
        }
        if (n < 0 && errno == EINTR) continue;
        break;
    }
    if (done == length) return;
    const int fd = ::open("/dev/urandom", O_RDONLY | O_CLOEXEC);
    if (fd >= 0) {
        while (done < length) {
            const std::size_t chunk = static_cast<std::size_t>(
                length - done > std::numeric_limits<std::size_t>::max()
                    ? std::numeric_limits<std::size_t>::max()
                    : length - done);
            const ssize_t n = ::read(fd, out + done, chunk);
            if (n > 0) done += static_cast<unsigned long long>(n);
            else if (n < 0 && errno == EINTR) continue;
            else break;
        }
        ::close(fd);
        if (done == length) return;
    }
#endif
    std::terminate();  // never produce predictable crypto material
}
