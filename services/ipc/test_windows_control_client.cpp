// Disposable native/Python control-lane smoke. Token is supplied only on stdin.
#include "ipc_windows_local.h"
#include <iostream>
#include <string>

int main(int argc, char** argv) {
#ifdef _WIN32
    if (argc != 2) return 2;
    char frame[64 * 1024 + 2]{};
    if (!std::cin.getline(frame, sizeof frame)) return 2;
    std::string request(frame);
    if (request.size() > 64 * 1024) return 2;
    request.push_back('\n');
    volatile char* input = frame;
    for (std::size_t i = 0; i < sizeof frame; ++i) input[i] = 0;
    std::string response, error;
    const bool ok = chudware_ipc::exchangeLocalPipe(argv[1], request, response,
                                                  256 * 1024, 2500, error);
    volatile char* bytes = request.empty() ? nullptr : &request[0];
    for (std::size_t i = 0; i < request.size(); ++i) bytes[i] = 0;
    // Do not echo request/response/token bytes; callers assert a known fixture response.
    if (!ok) return 1;
    return response == "{\"ok\":true,\"result\":{\"state\":\"idle\"}}" ? 0 : 3;
#else
    (void)argc;
    (void)argv;
    return 77;  // Linux cannot certify the Windows pipe implementation.
#endif
}
