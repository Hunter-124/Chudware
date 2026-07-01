#pragma once
// GL-free topology pick record (extracted from host render/id_pass.h for the worker seam).

#include <cstdint>

namespace worker {

enum class PickKind : uint8_t { None = 0, Face = 1, Edge = 2, Vertex = 3 };

struct PickHit {
    uint32_t body_id = 0;
    PickKind kind = PickKind::None;
    uint32_t topo_id = 0;

    bool valid() const { return kind != PickKind::None && body_id != 0; }
    bool operator==(const PickHit& o) const {
        return body_id == o.body_id && kind == o.kind && topo_id == o.topo_id;
    }
    bool operator!=(const PickHit& o) const { return !(*this == o); }
};

}  // namespace worker
