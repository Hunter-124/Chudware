#pragma once

// Thread-local for the duration of one host request. Once a document lock has
// been obtained the request must run to completion, including nested locks and
// assembly propagation; cancelling later could leave a partially applied edit.
namespace chudware::doc {
struct RequestCancel {
    int (*check)(void*) = nullptr;
    void* user = nullptr;
    bool committed = false;
};
inline thread_local RequestCancel request_cancel;
}
