#include "doc/native_policy.h"

namespace chudware::doc::native_policy {

Configuration& configuration() {
    static Configuration value;
    return value;
}

}  // namespace chudware::doc::native_policy
