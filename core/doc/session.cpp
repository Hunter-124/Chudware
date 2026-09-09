// Stateful document session implementation — see session.h.
//
// Mirrors engine/doc/session.py + engine/doc/ops.py over compile_document. JSON
// in, JSON out; never mutates a session until an op batch fully applies (the
// Python transactional contract: apply to a working copy, commit on success).

#include "doc/session.h"
#include "doc/op_registry.h"

#include "util/aicm_handoff.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <future>
#include <limits>
#include <map>
#include <mutex>
#include <regex>
#include <thread>
#include <set>
#include <string>
#include <unordered_set>
#include <vector>

#include <cmath>
#include <functional>
#include <iomanip>
#include <optional>
#include <sstream>
#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#else
#include <unistd.h>
#endif

#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <GeomAPI_Interpolate.hxx>
#include <Geom_BSplineCurve.hxx>
#include <TColgp_HArray1OfPnt.hxx>
#include <BRepTools.hxx>
#include <BRep_Tool.hxx>
#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <Poly_Triangulation.hxx>
#include <Standard_Failure.hxx>
#include <TopLoc_Location.hxx>
#include <TopExp_Explorer.hxx>
#include <TopAbs.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <BRep_Builder.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pln.hxx>
#include <gp_Dir.hxx>
#include <gp_Ax2.hxx>
#include <gp_Ax3.hxx>
#include <gp_Vec.hxx>
#include <gp_Trsf.hxx>
#include <gp_Mat.hxx>
#include <GProp_GProps.hxx>
#include <BRepGProp.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepCheck_Result.hxx>
#include <BRepCheck_ListOfStatus.hxx>
#include <BRepCheck_Status.hxx>
#include <BRepCheck_Shell.hxx>
#include <BRepAlgoAPI_Section.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepAlgoAPI_Cut.hxx>       // doc_render_diff: A-B / B-A residues
#include <BRepAlgoAPI_Fuse.hxx>      // doc_render_diff: A∪B camera frame
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepClass_FaceClassifier.hxx>
#include <BRepLProp_SLProps.hxx>
#include <GCPnts_AbscissaPoint.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <GeomAbs_CurveType.hxx>
#include <ShapeAnalysis_Surface.hxx>
#include <ShapeAnalysis_FreeBounds.hxx>     // section_props: group multi-loop section edges into closed wires
#include <TopTools_HSequenceOfShape.hxx>    // ConnectEdgesToWires in/out sequences
#include <Geom_Surface.hxx>
#include <gp_Circ.hxx>
#include <gp_Pnt2d.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Wire.hxx>  // section_props stores closed section loops by value
#include <TopoDS_Vertex.hxx>
// eval_solid_health self-intersection: the kernel's single-body self-interference algorithm
// plus the progress-indicator seam that is OCCT's only way to hand it a time budget.
#include <BOPAlgo_CheckerSI.hxx>
#include <BOPDS_DS.hxx>
#include <BOPDS_MapOfPair.hxx>
#include <BOPDS_Pair.hxx>
#include <Message_ProgressIndicator.hxx>
#include <Message_ProgressRange.hxx>
#include <Message_ProgressScope.hxx>

#include <Eigen/Eigenvalues>

#include <memory>

#include "doc/assembly.h"
#include "doc/motion_dynamics.h"
#include "doc/document.h"
#include "doc/recon.h"    // C1/C2 WS-C reverse-engineering kernel primitives
#include "doc/formats.h"
#include "doc/picks.h"
#include "doc/selectors.h"
#include "doc/sketch_geom.h"
#include "doc/drawing_session.h"  // W12+ drawing embed/restore on save/open
#include "doc/stepplus.h"
#include "doc/validate.h"
#include "parts/parts.h"
#include "geom/aicm.h"
#include "geom/booleans.h"  // fuse_many — doc_eval_compare's default whole-doc shape
#include "geom/fit.h"       // least-squares cylinder fit (doc_fit_cylinder_axis)
#include "geom/plane.h"
#include "geom/profile.h"
#include "geom/profile3d.h"   // sketch3d wires (doc_sketch3d_health chain report)
#include "sketch/sketch3d.h"  // 3D free-space sketch solver (sketch3d_* RPCs)
#include "geom/tessellate.h"
#include "geom/airfoil.h"  // "airfoil" sketch-entity expansion (agent aerofoil support)

#include <miniz.h>  // PNG/deflate for the agent vision-review rasterizer (doc_render_png)
#include "mate/mate.h"
#include "sketch/sketch.h"
#include "util/expr.h"

namespace chudware::doc {

using nlohmann::json;

// Forward decl — defined with the cross-doc dependency registry near the bottom of this
// file (feat(xdoc)); used by warm_and_respond inside the anonymous namespace below. The
// xdoc commit was never compiled on Linux, so the missing declaration only surfaced now.
static int dep_notify_unlocked(const std::string& source_doc_id, long new_revision);

namespace {

// ----------------------------------------------------------------- session store
// C18 active-sketch interaction state — C++ port of session.py::_SketchState. One
// sketch is "entered" at a time; sketch_enter populates it, solve/drag warm-start
// across it, sketch_exit clears it. solve_state is the warm SolveState (nullopt ==
// cold); a doc mutation (apply_ops/undo/redo/revert) resets it to nullopt so a warm
// state is never reused across a doc edit (session.py: active_sketch.solve_state = None).
struct SketchState {
    std::string sketch_fid;
    std::optional<chudware::sketch::SolveState> solve_state;  // warm start (nullopt == cold)
    json last_entities = json::object();   // id -> solved coord dict (entity_map form)
    json last_statuses = json::object();   // id -> "under"|"full"|"over"
    std::string last_overall_status;       // "under"|"full"|"over"
    std::vector<double> plane_frame;       // 16 floats, row-major

    // Drag-path solve cache (PERF): the built sketch System + variable layout reused
    // across the mouse-move frames of ONE drag (doc_sketch_drag). Valid only while
    // drag_ctx_revision == the session revision it was built at — every applied op
    // batch (and undo/redo) bumps the revision monotonically, so a stale revision
    // means "rebuild". No extra locking: drag/commit/apply all run under DocLock.
    std::shared_ptr<chudware::sketch::DragContext> drag_ctx;
    long drag_ctx_revision = -1;
};

// Forward decls — defined with the undo-journal helpers below Session.
static void drop_history_entry(const std::string& entry);
static void wipe_undo_journal(const std::string& doc_id);

struct Session {
    // T-PERF5: per-doc mutex. Locked by DocLock::acquire / require_session; never
    // touched while holding g_registry_mtx (pin-then-lock). Non-copyable/non-movable
    // because of the mutex — sessions live behind shared_ptr in g_sessions.
    std::mutex mtx;

    std::string doc_id;
    // True only for sessions registered in g_sessions. Scratch/temp sessions
    // (dry-run clones that copy live doc_id) leave this false so ~Session must
    // NEVER wipe the live doc's undo journal.
    bool owns_journal = false;
    json doc;
    long revision = 0;
    std::vector<std::string> undo_stack;  // JSON snapshots (newest at back)
    std::vector<std::string> redo_stack;
    bool dirty = false;
    std::string path;                                  // current .step+ path (or empty)
    std::map<std::string, std::string> extra_entries;  // unknown .step+ entries (verbatim)
    std::map<std::string, std::string> checkpoints;    // PM-cancel snapshots: id -> doc JSON
    std::optional<SketchState> active_sketch;          // C18 sketch-edit lifecycle

    // Read-only compile cache (PERF). `compiled` holds the last compile of `doc`, valid only
    // while compiled_rev == revision. Every geometry-changing op bumps revision (or starts a
    // fresh session), and all reads run serialized under the doc lock, so a stale result can
    // never be served. Interactive read paths — viewport picking, "fillet a whole face" edge
    // expansion, owner-map refresh — reuse this instead of recompiling the entire feature tree
    // on every click (measured ~190 ms/click on a modest part before this cache).
    std::shared_ptr<CompileResult> compiled;
    long compiled_rev = -1;

    // Incremental-rebuild prefix cache (see CompileCache in document.h). Complements
    // `compiled` above: `compiled` short-circuits WHOLE compiles on an unchanged
    // revision, while this cache makes the compiles that DO run start from the deepest
    // unchanged feature checkpoint instead of feature 1 — editing feature k of N
    // recompiles k..N only. Content-addressed (env+config+feature hashes), so undo/
    // redo/config switches can never be served stale geometry. Accessed only under
    // the per-doc mutex (DocLock).
    std::shared_ptr<chudware::doc::CompileCache> compile_cache = chudware::doc::make_compile_cache();

    // C20 — assembly state. asm_result is the last assembly compile; asm_warm seeds the
    // next mate solve; explode_active/explode_factor drive tessellation-time offsets.
    std::shared_ptr<AssemblyResult> asm_result;        // nullptr until first assembly rebuild
    // Per-component topology-description cache for assembly picks (PERF). Keyed "<body_id>:<kind>"
    // -> describe_{faces,edges,vertices}(component). describe_* runs Gauss integration over the
    // whole component, so without this every click in a big assembly re-derived it. Valid only
    // while built against the current asm_result (asm_describe_for); a rebuild swaps the pointer.
    std::map<std::string, std::vector<TopoInfo>> asm_describe_cache;
    const void* asm_describe_for = nullptr;
    chudware::mate::WarmMap asm_warm;
    bool explode_active = false;
    double explode_factor = 1.0;

    // Display tessellation tier for THIS document's AICM meshes (geom::DisplayQuality).
    // Defaults from CHUDWARE_DISPLAY_QUALITY / Fine so headless renders and the GUI agree.
    // doc_set_display_quality changes it, drops the compile + per-body mesh caches and
    // re-tessellates; it is a VIEW setting, so it never touches `doc` or the revision.
    ::geom::DisplayQuality display_quality = ::geom::default_display_quality();

    bool is_assembly() const {
        return doc.is_object() && doc.value("schema", std::string()) == "aicad.asm/1";
    }

    Session() = default;
    ~Session() {
        // Clean shutdown / last shared_ptr drop: free any spilled undo journal.
        // Scratch/temp sessions (owns_journal=false) must not touch the live dir.
        if (!owns_journal) return;
        for (const std::string& e : undo_stack) drop_history_entry(e);
        for (const std::string& e : redo_stack) drop_history_entry(e);
        wipe_undo_journal(doc_id);
    }
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    Session(Session&&) = delete;
    Session& operator=(Session&&) = delete;
};

constexpr std::size_t kUndoLimit = 100;
// Each undo/redo/checkpoint entry is a full doc JSON dump, so bounding by COUNT
// alone is not enough: on an import-heavy multi-megabyte doc a 100-deep stack
// would pin hundreds of MB of process RSS indefinitely. Bound history in BYTES
// too, and cap the PM-cancel checkpoint map (which minted one full-doc snapshot
// per PropertyManager open and was NEVER freed on commit — an unbounded leak).
// Entries that would push past the byte budget are spilled to disk as
// hostd_undo/<instance-key>/<doc_id>/<seq>.json stubs (see trim_history) so
// depth stays 100 without pinning multi-GB of RSS.
constexpr std::size_t kUndoByteBudget = 256u * 1024u * 1024u;  // per stack
constexpr std::size_t kCheckpointLimit = 32;                   // PM-cancel snapshots

// ---- Undo disk journal (P2 multi-user / long sessions) -----------------------
// Path: <stateDir>/hostd_undo/<instance-key>/<doc_id>/<seq>.json
// stateDir mirrors services/ipc/ipc_discovery stateDir() without linking it
// (libchudware must not pull the hostd IPC layer).
static std::string undo_state_dir() {
    if (const char* x = std::getenv("XDG_DATA_HOME"); x && *x)
        return std::string(x) + "/Chudware";
    if (const char* h = std::getenv("HOME"); h && *h)
        return std::string(h) + "/.local/share/Chudware";
#ifdef _WIN32
    if (const char* a = std::getenv("APPDATA"); a && *a)
        return std::string(a) + "\\Chudware";
#endif
    return "Chudware";
}

static std::string undo_instance_key() {
    if (const char* k = std::getenv("CHUDWARE_INSTANCE_KEY"); k && *k) return k;
    // Per-process fallback so two daemons without CHUDWARE_INSTANCE_KEY never share
    // hostd_undo/ (doc ids are sequential and would collide across processes).
    // The real instance key is fnv1a(hostd realpath) — libchudware cannot see that
    // path, so isolate by pid instead.
#ifdef _WIN32
    return "inproc-" + std::to_string(static_cast<unsigned long>(::GetCurrentProcessId()));
#else
    return "inproc-" + std::to_string(static_cast<long>(::getpid()));
#endif
}

static std::filesystem::path undo_journal_dir(const std::string& doc_id) {
    return std::filesystem::path(undo_state_dir()) / "hostd_undo" / undo_instance_key() /
           doc_id;
}

static void wipe_undo_journal(const std::string& doc_id) {
    if (doc_id.empty()) return;
    std::error_code ec;
    std::filesystem::remove_all(undo_journal_dir(doc_id), ec);
}

// Monotonic spill sequence per process (file names only need uniqueness).
static std::atomic<std::uint64_t> g_undo_spill_seq{1};

// True when the stack entry is a disk stub: {"@file":"<path>"} (compact form).
static bool is_undo_stub(const std::string& entry) {
    // Fast reject: stubs are short and start with '{'.
    if (entry.size() < 12 || entry[0] != '{') return false;
    if (entry.find("\"@file\"") == std::string::npos) return false;
    try {
        json j = json::parse(entry);
        return j.is_object() && j.contains("@file") && j["@file"].is_string();
    } catch (...) {
        return false;
    }
}

static std::string undo_stub_path(const std::string& entry) {
    try {
        json j = json::parse(entry);
        if (j.is_object() && j.contains("@file") && j["@file"].is_string())
            return j["@file"].get<std::string>();
    } catch (...) {
    }
    return {};
}

// Materialize a stack entry to a full doc-JSON string (load stub from disk).
static std::string materialize_history_entry(const std::string& entry) {
    if (!is_undo_stub(entry)) return entry;
    const std::string path = undo_stub_path(entry);
    if (path.empty()) return entry;
    std::ifstream in(path, std::ios::binary);
    if (!in) return entry;
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

// Spill a full-JSON entry to the journal and return a compact stub string.
// On I/O failure returns the original entry (caller may still drop it on count
// trim — better to lose oldest than pin RAM forever).
static std::string spill_history_entry(const std::string& doc_id, const std::string& entry) {
    if (doc_id.empty() || is_undo_stub(entry)) return entry;
    std::error_code ec;
    const auto dir = undo_journal_dir(doc_id);
    std::filesystem::create_directories(dir, ec);
    if (ec) return entry;
    const std::uint64_t seq = g_undo_spill_seq.fetch_add(1, std::memory_order_relaxed);
    const auto path = dir / (std::to_string(seq) + ".json");
    {
        std::ofstream out(path, std::ios::binary | std::ios::trunc);
        if (!out) return entry;
        out.write(entry.data(), static_cast<std::streamsize>(entry.size()));
        if (!out) {
            std::filesystem::remove(path, ec);
            return entry;
        }
    }
    return json{{"@file", path.string()}}.dump();
}

static void drop_history_entry(const std::string& entry) {
    // Free any on-disk spill when an entry leaves the stack entirely (count trim).
    if (!is_undo_stub(entry)) return;
    const std::string path = undo_stub_path(entry);
    if (path.empty()) return;
    std::error_code ec;
    std::filesystem::remove(path, ec);
}

// =============================================================================
// T-PERF5 — per-doc engine locking
// =============================================================================
//
// Lock order (MUST be respected to avoid deadlock):
//   1. Session mutexes, ordered by doc_id ascending (never hold A then wait for
//      B if A > B lexicographically — upgrade path unlocks + relocks sorted).
//   2. g_registry_mtx — protects g_sessions map + g_dep_registry. Taken only
//      briefly to pin shared_ptr<Session> or mutate the map; NEVER held while
//      waiting on a session mutex (pin-then-lock pattern).
//
// Cross-doc ops (refresh_derived, resolve_source_session): either already hold
// both docs via DocLock multi-acquire, or snapshot the source under a brief
// source-doc lock. dep_notify only touches g_dep_registry under the registry
// lock (no session data).
//
// DocLock is lazy: construction registers the RAII owner on this thread;
// require_session(doc_id) acquires that doc's mutex on first touch. Two docs
// edit concurrently without cross-blocking.

// Registry: map of live sessions. Sessions are heap-allocated so pins stay
// valid after erase (in-flight DocLocks hold shared_ptr until unlock).
std::mutex g_registry_mtx;
std::map<std::string, std::shared_ptr<Session>> g_sessions;

// --- Contention meters (Phase 1b; now attribute per-doc acquires) -------------
struct ContentionMeters {
    std::atomic<unsigned long long> acquisitions{0};
    std::atomic<unsigned long long> contended{0};       // acquires that had to wait (>threshold)
    std::atomic<unsigned long long> total_wait_ns{0};
    std::atomic<unsigned long long> max_wait_ns{0};
    std::atomic<unsigned long long> total_hold_ns{0};
    std::atomic<unsigned long long> max_hold_ns{0};
    std::atomic<long long> waiters{0};                  // threads currently blocked on acquire
    std::atomic<long long> max_waiters{0};              // peak simultaneous contenders
};
ContentionMeters g_cont;
std::chrono::steady_clock::time_point g_cont_since = std::chrono::steady_clock::now();
constexpr unsigned long long kContendedWaitNs = 20'000;  // >20µs waited => genuinely contended

thread_local const char* t_cur_method = nullptr;
struct MethodContention { unsigned long long count{0}, wait_ns{0}, hold_ns{0}, max_wait_ns{0}; };
std::mutex g_cont_map_mtx;
std::map<std::string, MethodContention> g_cont_map;

inline void atomic_max(std::atomic<unsigned long long>& a, unsigned long long v) {
    unsigned long long cur = a.load(std::memory_order_relaxed);
    while (v > cur && !a.compare_exchange_weak(cur, v, std::memory_order_relaxed)) {}
}

// Forward: Session grows a per-doc mutex (declared with Session above — added below).
// Pin a live session under the registry lock (does NOT lock the session).
std::shared_ptr<Session> pin_session_unlocked(const std::string& doc_id) {
    auto it = g_sessions.find(doc_id);
    if (it == g_sessions.end()) return nullptr;
    return it->second;
}
std::shared_ptr<Session> pin_session(const std::string& doc_id) {
    std::lock_guard<std::mutex> reg(g_registry_mtx);
    return pin_session_unlocked(doc_id);
}

// RAII per-doc (or multi-doc) lock. Lazy: the first require_session / acquire
// call locks the target doc; further docs are multi-locked in doc_id order.
// SEH recovery: force_release_doc_lock drops every mutex this thread holds.
class DocLock;
thread_local DocLock* t_current_doc_lock = nullptr;
// True while THIS thread owns one or more session mutexes (force_release probe).
std::atomic<bool> g_mtx_held{false};
std::thread::id g_mtx_owner;

class DocLock {
public:
    struct Held {
        std::string id;
        std::shared_ptr<Session> pin;
    };

    DocLock() {
        prev_ = t_current_doc_lock;
        t_current_doc_lock = this;
        method_ = t_cur_method;
        hold_t0_ = std::chrono::steady_clock::now();
    }
    // Eager single-doc acquire (optional; require_session also works).
    explicit DocLock(const std::string& doc_id) : DocLock() { acquire(doc_id); }

    ~DocLock() {
        release_all();
        t_current_doc_lock = prev_;
        // Meter the whole RAII scope as one acquisition (RPC granularity).
        const unsigned long long hold_ns =
            (unsigned long long)std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now() - hold_t0_).count();
        g_cont.acquisitions.fetch_add(1, std::memory_order_relaxed);
        g_cont.total_wait_ns.fetch_add(wait_ns_, std::memory_order_relaxed);
        g_cont.total_hold_ns.fetch_add(hold_ns, std::memory_order_relaxed);
        atomic_max(g_cont.max_wait_ns, wait_ns_);
        atomic_max(g_cont.max_hold_ns, hold_ns);
        if (wait_ns_ > kContendedWaitNs)
            g_cont.contended.fetch_add(1, std::memory_order_relaxed);
        if (method_) {
            std::lock_guard<std::mutex> lk(g_cont_map_mtx);
            MethodContention& mc = g_cont_map[method_];
            mc.count += 1;
            mc.wait_ns += wait_ns_;
            mc.hold_ns += hold_ns;
            if (wait_ns_ > mc.max_wait_ns) mc.max_wait_ns = wait_ns_;
        }
    }
    DocLock(const DocLock&) = delete;
    DocLock& operator=(const DocLock&) = delete;

    // Lock `doc_id` (no-op if already held). Multi-doc: unlocks + relocks all
    // held ids in sorted order so the lock order invariant holds.
    Session& acquire(const std::string& doc_id) {
        for (auto& h : held_)
            if (h.id == doc_id) return *h.pin;
        std::shared_ptr<Session> pin = pin_session(doc_id);
        if (!pin)
            throw OpError("unknown_doc", "no such document: " + doc_id,
                          {{"doc_id", doc_id}});

        if (held_.empty()) {
            lock_one(pin);
            held_.push_back(Held{doc_id, std::move(pin)});
            mark_owner();
            return *held_.back().pin;
        }

        // Upgrade to multi-doc: unlock everything, sort, relock.
        std::vector<Held> next = held_;
        next.push_back(Held{doc_id, std::move(pin)});
        std::sort(next.begin(), next.end(),
                  [](const Held& a, const Held& b) { return a.id < b.id; });
        // Drop current locks (still pinned via shared_ptr).
        for (auto& h : held_) h.pin->mtx.unlock();
        held_.clear();
        for (auto& h : next) lock_one(h.pin);
        held_ = std::move(next);
        mark_owner();
        for (auto& h : held_)
            if (h.id == doc_id) return *h.pin;
        return *held_.front().pin;  // unreachable
    }

    Session* find_held(const std::string& doc_id) {
        for (auto& h : held_)
            if (h.id == doc_id) return h.pin.get();
        return nullptr;
    }

    // Crash recovery: unlock every session mutex this DocLock owns.
    void force_release() {
        for (auto& h : held_) {
            try {
                h.pin->mtx.unlock();
            } catch (...) {
            }
        }
        held_.clear();
        clear_owner_if_idle();
    }

    bool holds_any() const { return !held_.empty(); }
    DocLock* previous() const { return prev_; }

private:
    void lock_one(const std::shared_ptr<Session>& pin) {
        const auto w0 = std::chrono::steady_clock::now();
        const long long q = g_cont.waiters.fetch_add(1, std::memory_order_relaxed) + 1;
        {
            long long mw = g_cont.max_waiters.load(std::memory_order_relaxed);
            while (q > mw && !g_cont.max_waiters.compare_exchange_weak(
                                 mw, q, std::memory_order_relaxed)) {
            }
        }
        pin->mtx.lock();
        g_cont.waiters.fetch_sub(1, std::memory_order_relaxed);
        const auto w1 = std::chrono::steady_clock::now();
        wait_ns_ += (unsigned long long)std::chrono::duration_cast<std::chrono::nanoseconds>(
                        w1 - w0)
                        .count();
    }
    void release_all() {
        for (auto& h : held_) h.pin->mtx.unlock();
        held_.clear();
        clear_owner_if_idle();
    }
    void mark_owner() {
        g_mtx_owner = std::this_thread::get_id();
        g_mtx_held.store(true, std::memory_order_release);
    }
    void clear_owner_if_idle() {
        // Only clear the process flag if no nested DocLock on this thread holds locks.
        if (prev_ && prev_->holds_any()) return;
        if (!held_.empty()) return;
        if (g_mtx_owner == std::this_thread::get_id()) {
            g_mtx_held.store(false, std::memory_order_release);
        }
    }

    std::vector<Held> held_;
    DocLock* prev_ = nullptr;
    unsigned long long wait_ns_ = 0;
    std::chrono::steady_clock::time_point hold_t0_;
    const char* method_ = nullptr;
};

std::atomic<unsigned long long> g_doc_seq{1};
std::atomic<unsigned long long> g_ckpt_seq{1};
std::atomic<unsigned long long> g_study_seq{1};

std::string new_checkpoint_id() {
    // Opaque, unique-per-process id (Python uses a uuid4 prefix; the value is only
    // ever round-tripped back through doc_revert, so any unique token is faithful).
    unsigned long long n = g_ckpt_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "ckpt-%010llx", n);
    return std::string(buf);
}

std::string new_doc_id() {
    unsigned long long n = g_doc_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "doc-cpp%08llx", n);
    return std::string(buf);
}

std::string new_study_id() {
    // Opaque, unique-per-process id for a simulation study. Only assigned by the core
    // (the host fills every other study field; the core treats them as opaque JSON).
    unsigned long long n = g_study_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "study-%08llx", n);
    return std::string(buf);
}

// Acquire (or look up already-held) session under the current thread's DocLock.
// Callers MUST have a DocLock RAII on the stack; the first touch of each doc_id
// locks that session's mutex (multi-doc via sorted upgrade — see DocLock::acquire).
Session& require_session(const std::string& doc_id) {
    if (!t_current_doc_lock)
        throw OpError("internal", "require_session without DocLock", {{"doc_id", doc_id}});
    return t_current_doc_lock->acquire(doc_id);
}

json blank_part(const std::string& doc_id, const std::string& name) {
    return json{
        {"schema", "aicad.part/1"},
        {"id", doc_id},
        {"name", name.empty() ? std::string("Part1") : name},
        {"units", {{"system", "MMGS"}}},
        // Default material: 6061-T6 aluminum. MUST be the canonical engine key `alu_6061`
        // (engine/sim/materials.json + kPhysicalMaterials) — the old `aluminum_6061_t6`
        // resolved NOWHERE, so mass fell back to 7.85 g/cm^3 steel and the FeatureManager
        // showed the raw key instead of "Aluminum 6061-T6".
        {"material", {{"key", "alu_6061"}}},
        {"equations", {{"globals", json::array()}}},
        {"features", json::array()},
    };
}

json blank_assembly(const std::string& doc_id, const std::string& name) {
    return json{
        {"schema", "aicad.asm/1"},
        {"id", doc_id},
        {"name", name.empty() ? std::string("Assembly1") : name},
        {"units", {{"system", "MMGS"}}},
        {"equations", {{"globals", json::array()}}},
        {"components", json::array()},
        {"mates", json::array()},
        {"features", json::array()},
        {"explode", json::array()},
    };
}

// ----------------------------------------------------------------- id allocation
// Monotonic per-family counters seeded from the document's existing ids (mirrors
// engine/doc/ops.py::_IdGen — never reuse a freed id).
struct IdGen {
    std::map<std::string, int> hi;  // prefix -> highest seen

    explicit IdGen(const json& doc) {
        scan(doc);
    }
    void note(const std::string& id) {
        // id = <prefix letters><digits>; record the max number per prefix.
        std::size_t i = 0;
        while (i < id.size() && !std::isdigit((unsigned char)id[i])) ++i;
        if (i == 0 || i == id.size()) return;
        std::string prefix = id.substr(0, i);
        int n = std::atoi(id.c_str() + i);
        if (n > hi[prefix]) hi[prefix] = n;
    }
    void scan(const json& doc) {
        if (doc.contains("features") && doc["features"].is_array()) {
            for (const json& f : doc["features"]) {
                if (f.contains("id") && f["id"].is_string()) note(f["id"].get<std::string>());
                // sketch entities/constraints live under params.
                const json* params = f.contains("params") ? &f["params"] : nullptr;
                if (params && params->is_object()) {
                    // external_refs mint x<N> ids (associative Convert Entities).
                    for (const char* key : {"entities", "constraints", "external_refs"}) {
                        if (params->contains(key) && (*params)[key].is_array())
                            for (const json& e : (*params)[key])
                                if (e.contains("id") && e["id"].is_string())
                                    note(e["id"].get<std::string>());
                    }
                }
            }
        }
        // Assembly id families (mirrors ops.py::_scan_max): components mint k<N>, mates m<N>.
        // Without seeding these, a fresh apply_ops batch restarts the counter at 0 and a
        // second asm_insert_component re-mints 'k1' -> "duplicate 'k1'" validation failure.
        for (const char* fam : {"components", "mates"}) {
            if (doc.contains(fam) && doc[fam].is_array())
                for (const json& it : doc[fam])
                    if (it.is_object() && it.contains("id") && it["id"].is_string())
                        note(it["id"].get<std::string>());
        }
    }
    std::string next(const std::string& prefix) {
        int n = ++hi[prefix];
        return prefix + std::to_string(n);
    }
};

// ----------------------------------------------------------------- placeholders
// Resolve "$N" / "$N#k" placeholders (created-id back-references) anywhere in a
// json value, in place, against the batch id_map. "$self" is handled by the caller
// (it is entity-local). Mirrors ops.py::_resolve_placeholders_deep.
void resolve_placeholders_deep(json& v, const json& id_map) {
    if (v.is_string()) {
        const std::string& s = v.get_ref<const std::string&>();
        if (!s.empty() && s[0] == '$' && id_map.contains(s))
            v = id_map[s];
        return;
    }
    if (v.is_array()) {
        for (json& e : v) resolve_placeholders_deep(e, id_map);
    } else if (v.is_object()) {
        for (auto it = v.begin(); it != v.end(); ++it) resolve_placeholders_deep(it.value(), id_map);
    }
}

// ----------------------------------------------------------------- helpers
// Find a feature by id in doc["features"]; returns nullptr if absent.
json* find_feature(json& doc, const std::string& id) {
    if (!doc.contains("features")) return nullptr;
    for (json& f : doc["features"])
        if (f.value("id", std::string()) == id) return &f;
    return nullptr;
}

// Patch-merge: present key replaces; a null value DELETES the key (ops.py::_patch).
void patch(json& target, const json& patchobj) {
    if (!patchobj.is_object()) return;
    for (auto it = patchobj.begin(); it != patchobj.end(); ++it) {
        if (it.value().is_null())
            target.erase(it.key());
        else
            target[it.key()] = it.value();
    }
}

// Collect the feature ids a feature references (deep scan of refs for fN tokens +
// region/body selector owner fields). Used for the delete dependency guard.
void collect_fids(const json& v, std::set<std::string>& out) {
    static const std::regex fid_re("^f[0-9]+$");
    if (v.is_string()) {
        const std::string& s = v.get_ref<const std::string&>();
        if (std::regex_match(s, fid_re)) out.insert(s);
    } else if (v.is_array()) {
        for (const json& e : v) collect_fids(e, out);
    } else if (v.is_object()) {
        for (auto it = v.begin(); it != v.end(); ++it) collect_fids(it.value(), out);
    }
}

// The sketch feature a sketch_* op targets (id may be a resolved placeholder).
json& require_sketch_feature(json& doc, const json& op, const json& id_map) {
    json sk_ref = op.at("sketch");
    resolve_placeholders_deep(sk_ref, id_map);
    if (!sk_ref.is_string())
        throw OpError("invalid_op", "sketch op: 'sketch' must be a feature id");
    json* f = find_feature(doc, sk_ref.get<std::string>());
    if (!f) throw OpError("invalid_op", "sketch op: no sketch '" + sk_ref.get<std::string>() + "'");
    if (f->value("type", std::string()) != "sketch")
        throw OpError("invalid_op", "sketch op: feature is not a sketch");
    if (!f->contains("params")) (*f)["params"] = json::object();
    json& p = (*f)["params"];
    if (!p.contains("entities")) p["entities"] = json::array();
    if (!p.contains("constraints")) p["constraints"] = json::array();
    return *f;
}

// ----------------------------------------------------------------- op handlers
// Normalize + expand ONE inline sketch entity (defined below, after unwrap_value_scalars).
// Used by BOTH op_add_feature (inline params.entities) and op_sketch_add_entity.
std::vector<json> expand_sketch_entity(json ent, IdGen& gen);
void apply_one(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map);


// Expand add_feature{type:"standard_part"} into the parts catalog generator's
// op sequence (same code path as the parts_make RPC) and apply each expanded
// op into the CURRENT document. The generator emits $N placeholders relative to
// its own sub-batch (index 0); we remap them onto the outer batch id_map under
// synthetic keys so sketch_add_entity / extrude refs resolve correctly. The
// macro itself never lands as a feature in the tree — only the produced
// cylinder/sketch/extrude/… features do (editable, saved in .step+).
void expand_standard_part(json& doc, const json& feat, std::size_t idx,
                          IdGen& gen, json& id_map) {
    const json params = feat.contains("params") && feat["params"].is_object()
                            ? feat["params"]
                            : json::object();

    auto param_str = [&](std::initializer_list<const char*> keys) -> std::string {
        for (const char* k : keys) {
            if (!params.contains(k)) continue;
            const json& v = params[k];
            if (v.is_string()) return v.get<std::string>();
            if (v.is_number_integer()) return std::to_string(v.get<long long>());
            if (v.is_number()) {
                // Pretty integer when whole (dowel "3"), else default dump.
                const double d = v.get<double>();
                if (std::floor(d) == d) return std::to_string(static_cast<long long>(d));
                return std::to_string(d);
            }
            // Dim-shaped {value:N, expr:null} — accept as a size token.
            if (v.is_object() && v.contains("value")) {
                const json& vv = v["value"];
                if (vv.is_string()) return vv.get<std::string>();
                if (vv.is_number_integer()) return std::to_string(vv.get<long long>());
                if (vv.is_number()) {
                    const double d = vv.get<double>();
                    if (std::floor(d) == d) return std::to_string(static_cast<long long>(d));
                    return std::to_string(d);
                }
            }
        }
        return {};
    };
    auto param_len = [&]() -> double {
        for (const char* k : {"length_mm", "length", "L"}) {
            if (!params.contains(k)) continue;
            const json& v = params[k];
            if (v.is_number()) return v.get<double>();
            if (v.is_object() && v.contains("value") && v["value"].is_number())
                return v["value"].get<double>();
            if (v.is_string()) {
                char* end = nullptr;
                const double d = std::strtod(v.get_ref<const std::string&>().c_str(), &end);
                if (end && end != v.get_ref<const std::string&>().c_str()) return d;
            }
        }
        return 0.0;
    };

    const std::string family = param_str({"family", "kind", "type"});
    // size / thread / designation — models often say "thread":"m3".
    const std::string size = param_str({"size", "thread", "designation", "bore"});
    const double length_mm = param_len();
    json options = json::object();
    if (params.contains("options") && params["options"].is_object())
        options = params["options"];

    if (family.empty()) {
        throw OpError("invalid_op",
                      "standard_part: params.family required; valid families: " +
                          chudware::parts::family_keys_csv(),
                      {{"index", idx}, {"reason", "missing family"}});
    }

    chudware::parts::PartBuild pb;
    try {
        pb = chudware::parts::build(family, size, length_mm, options);
    } catch (const std::invalid_argument& e) {
        throw OpError("invalid_op",
                      std::string("standard_part: ") + e.what(),
                      {{"index", idx}, {"reason", e.what()},
                       {"families", chudware::parts::family_keys_csv()}});
    } catch (const std::exception& e) {
        throw OpError("invalid_op",
                      std::string("standard_part: ") + e.what(),
                      {{"index", idx}, {"reason", e.what()}});
    }

    if (!pb.ops.is_array() || pb.ops.empty()) {
        throw OpError("invalid_op",
                      "standard_part: generator produced no ops for family '" + family + "'",
                      {{"index", idx}});
    }

    // Optional display name override on the feature envelope.
    const std::string override_name = feat.value("name", std::string());

    // Apply each generated op. The generator's $N placeholders are relative to ITS
    // own sub-batch (0-based). Seed a local id_map so "$0" inside the expansion
    // resolves to whatever feature the first expanded add_feature minted, then
    // mirror the primary body id onto the OUTER batch's $<idx> so a subsequent
    // op in the caller's batch can still reference the macro.
    json local_map = json::object();
    std::string first_fid;
    std::string last_body_fid;
    for (std::size_t j = 0; j < pb.ops.size(); ++j) {
        json sub = pb.ops[j];  // deep copy
        // Merge outer + local maps so generator-local $N resolve, while any
        // outer-batch placeholders the expansion doesn't use stay available.
        json sub_map = id_map;
        for (auto it = local_map.begin(); it != local_map.end(); ++it)
            sub_map[it.key()] = it.value();

        const std::size_t n_before =
            (doc.contains("features") && doc["features"].is_array()) ? doc["features"].size()
                                                                    : 0;

        // apply_one records minted ids under $<j> in sub_map.
        apply_one(doc, sub, /*idx=*/j, gen, sub_map);

        const std::string key = "$" + std::to_string(j);
        if (sub_map.contains(key)) {
            local_map[key] = sub_map[key];
            for (auto it = sub_map.begin(); it != sub_map.end(); ++it) {
                const std::string& k = it.key();
                if (k.size() > key.size() && k.compare(0, key.size(), key) == 0 &&
                    k[key.size()] == '#')
                    local_map[k] = it.value();
            }
        }
        if (sub.value("op", "") == "add_feature" &&
            doc.contains("features") && doc["features"].is_array() &&
            doc["features"].size() > n_before) {
            const json& nf = doc["features"].back();
            const std::string fid = nf.value("id", std::string());
            if (!fid.empty()) {
                if (first_fid.empty()) first_fid = fid;
                // Body-ish types (not a bare sketch used as a hex profile).
                const std::string t = nf.value("type", std::string());
                if (t != "sketch" && t != "sketch3d" && t != "plane" && t != "axis" &&
                    t != "point" && t != "csys")
                    last_body_fid = fid;
            }
        }
    }

    if (!override_name.empty() && !last_body_fid.empty()) {
        if (json* f = find_feature(doc, last_body_fid)) (*f)["name"] = override_name;
    } else if (!override_name.empty() && !first_fid.empty()) {
        if (json* f = find_feature(doc, first_fid)) (*f)["name"] = override_name;
    }

    // Expose the primary body (or first feature) under the outer batch's $<idx>
    // so subsequent ops can reference the macro result.
    const std::string exposed =
        !last_body_fid.empty() ? last_body_fid
                               : (!first_fid.empty() ? first_fid : std::string());
    if (!exposed.empty()) id_map["$" + std::to_string(idx)] = exposed;
}

void op_add_feature(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json feat;
    if (op.contains("feature") && op["feature"].is_object()) {
        feat = op["feature"];
    } else if (op.contains("type") && op["type"].is_string()) {
        // Lenient FLAT form: models frequently emit {op:add_feature, type, name,
        // params, ...} with the feature fields at the TOP LEVEL instead of nested under
        // "feature" (grok-4.5 does this ~half the time, failing dry-run validation and
        // forcing a wasteful model retry). Accept it by hoisting the top-level fields
        // into a synthesized feature — fixes it for EVERY feature type at once.
        feat = json::object();
        for (auto& [k, v] : op.items())
            if (k != "op" && k != "before" && k != "after") feat[k] = v;
    } else {
        throw OpError("invalid_op", "add_feature: 'feature' object required");
    }
    // Capture type early — standard_part is a MACRO that expands into a sub-batch
    // of real features (via chudware::parts::build, same as parts_make). It must NOT
    // be stored as a feature itself (no compiler, would fail validation).
    const std::string ftype_early = feat.value("type", std::string());
    if (ftype_early == "standard_part") {
        // Models often put family/size/thread/length at the feature TOP LEVEL
        // (same shape as parts_make RPC fields) rather than nested under params.
        if (!feat.contains("params") || !feat["params"].is_object())
            feat["params"] = json::object();
        for (const char* k : {"family", "size", "thread", "designation", "bore",
                              "length_mm", "length", "L", "options", "kind"}) {
            if (feat.contains(k) && !feat["params"].contains(k))
                feat["params"][k] = feat[k];
        }
        expand_standard_part(doc, feat, idx, gen, id_map);
        return;
    }
    const std::string fid = gen.next("f");
    json env;
    env["id"] = fid;
    env["type"] = ftype_early;
    env["name"] = feat.value("name", fid);
    env["suppressed"] = feat.value("suppressed", false);
    env["params"] = feat.contains("params") ? feat["params"] : json::object();
    env["refs"] = feat.contains("refs") ? feat["refs"] : json::object();
    if (feat.contains("folder")) env["folder"] = feat["folder"];
    // A model may put feature OPTIONS at the feature TOP LEVEL instead of in params (their
    // canonical home) — e.g. {"type":"cylinder","mode":"cut",...}, {"type":"transform",
    // "translate":[5,0,0]}, {"type":"hole","positions":[...]}, {"type":"sketch",
    // "entities":[...]}. Only type/name/suppressed/params/refs/folder are kept above, so a
    // stray top-level option used to be SILENTLY DROPPED -> wrong geometry with no error (a
    // cut cylinder fuses; transform body:"fN" moved the wrong body — the T-1 assembly
    // mistargeting bug; a hole built 1 bore instead of N; a sketch stored empty).
    //
    // This was previously a hand-maintained whitelist of the options someone had already been
    // bitten by, which meant every NEW option repeated the same bug once. Hoist EVERY
    // non-structural top-level key instead: params is where the compiler reads from, unknown
    // params are ignored rather than rejected, and an existing params entry always wins. That
    // makes the two spellings equivalent for every feature type at once, present and future,
    // and leaves no drop path to rediscover.
    if (env["params"].is_object()) {
        static const std::set<std::string> kStructuralKeys = {
            "type", "name", "id", "suppressed", "params", "refs", "folder",
        };
        for (auto it = feat.begin(); it != feat.end(); ++it) {
            const std::string& k = it.key();
            if (kStructuralKeys.count(k)) continue;
            if (!env["params"].contains(k)) env["params"][k] = it.value();
        }
    }
    // Normalize inline sketch entities the SAME way sketch_add_entity does, so a hexagon /
    // airfoil / {value:N} scalar authored INLINE (grok emits sketches this way ~half the time)
    // expands + validates identically — instead of reaching the geometry kernel as a raw
    // "hexagon" entity that builds no region and fails the extrude.
    if (env["type"] == "sketch" && env["params"].is_object() &&
        env["params"].contains("entities") && env["params"]["entities"].is_array()) {
        // Reserve every canonical id the caller ALREADY wrote before expanding
        // anything. A multi-entity expansion (rectangle/polygon/slot/airfoil)
        // mints one e<N> per emitted segment, and IdGen is seeded only from the
        // stored document — so a batch of [rectangle, circle{id:e2}] minted
        // e1..e4 for the rectangle and then hit "duplicate 'e2'" on the circle.
        // Live probe-holster-3: two consecutive add_features calls lost to it.
        for (const json& e : env["params"]["entities"]) {
            if (e.is_object() && e.contains("id") && e["id"].is_string())
                gen.note(e["id"].get<std::string>());
        }
        json norm = json::array();
        for (json& e : env["params"]["entities"]) {
            if (!e.is_object()) { norm.push_back(e); continue; }
            for (json& x : expand_sketch_entity(e, gen)) {
                // Register modeler aliases so same-batch constraints (and later
                // sketch_add_constraint) can resolve "l0"/"l5" etc.
                if (x.contains("_alias") && x["_alias"].is_string() &&
                    x.contains("id") && x["id"].is_string()) {
                    id_map[x["_alias"].get<std::string>()] = x["id"].get<std::string>();
                    x.erase("_alias");
                }
                norm.push_back(std::move(x));
            }
        }
        env["params"]["entities"] = std::move(norm);
        // Remap any inline constraints that still name the modeler aliases.
        if (env["params"].contains("constraints") && env["params"]["constraints"].is_array()) {
            std::map<std::string, std::string> aliases;
            // Prefer id_map bare-alias entries (just registered above).
            for (auto it = id_map.begin(); it != id_map.end(); ++it) {
                if (!it.key().empty() && it.key()[0] != '$' && it.value().is_string())
                    aliases[it.key()] = it.value().get<std::string>();
            }
            for (json& c : env["params"]["constraints"]) {
                if (!c.is_object() || !c.contains("refs") || !c["refs"].is_array()) continue;
                for (json& rref : c["refs"]) {
                    if (!rref.is_string()) continue;
                    std::string s = rref.get<std::string>();
                    std::string base = s, sub;
                    auto dot = s.find('.');
                    if (dot != std::string::npos) {
                        base = s.substr(0, dot);
                        sub = s.substr(dot);
                    }
                    auto ait = aliases.find(base);
                    if (ait != aliases.end()) rref = ait->second + sub;
                }
            }
        }
    }
    // NO SILENT DROPS, part 2. The total hoist above guarantees nothing is discarded, but a
    // key the compiler doesn't read still does nothing — and for the PRIMITIVES that is the
    // difference between the part the model asked for and a solid centred on the origin. A
    // live Claude run sent placement:{origin,direction} + boolean:"union" on a cylinder boss;
    // both were inert, the cylinder built at the origin buried inside the body, and the model
    // burned three turns on the opaque "boss added no material" with no clue what it did
    // wrong. Primitives have a small CLOSED vocabulary, so here we can reject an unrecognised
    // key outright and teach the real spelling. Other feature types keep the permissive hoist
    // (their param sets are open-ended and type-specific), except for the dialect keys below
    // which are unambiguously wrong on ANY feature.
    {
        static const std::set<std::string> kPrimitiveTypes = {
            "box", "cylinder", "sphere", "cone", "torus",
        };
        static const std::set<std::string> kPrimitiveKeys = {
            // structural
            "type", "name", "id", "suppressed", "params", "refs", "folder",
            // placement / boolean options
            "at", "at_offset", "axis", "axis_dir", "centered", "mode", "merge",
            // dimensions (params is merged into the feature, so these may appear either way)
            "length", "width", "height", "radius", "r1", "r2", "major_r", "minor_r",
        };
        // The dialect models actually invent -> the canonical spelling, so the error teaches
        // the fix instead of only naming the offence. These are rejected on EVERY feature
        // type: none of them is a real param anywhere, and each has an exact replacement.
        static const std::map<std::string, std::string> kBadKeyHints = {
            {"placement", "use at:[x,y,z] (or at:{\"of\":\"face\",\"side\":\"top\"}) for the "
                          "position and axis:[x,y,z] for the direction"},
            {"boolean",   "use mode:\"boss\" to fuse or mode:\"cut\" to subtract, and "
                          "merge:false to start a new body"},
            {"operation", "use mode:\"boss\" to fuse or mode:\"cut\" to subtract, and "
                          "merge:false to start a new body"},
            {"before",    "\"before\"/\"after\" order the OP, so they belong beside \"op\", "
                          "not inside \"feature\""},
            {"after",     "\"before\"/\"after\" order the OP, so they belong beside \"op\", "
                          "not inside \"feature\""},
        };
        const bool is_primitive = kPrimitiveTypes.count(ftype_early) > 0;
        for (auto it = feat.begin(); it != feat.end(); ++it) {
            const std::string& k = it.key();
            auto hint = kBadKeyHints.find(k);
            const bool bad_dialect = hint != kBadKeyHints.end();
            if (!bad_dialect && !(is_primitive && !kPrimitiveKeys.count(k))) continue;
            std::string msg = "add_feature " +
                              (ftype_early.empty() ? std::string("feature") : ftype_early) +
                              ": unknown feature key '" + k + "'";
            if (bad_dialect) msg += " — " + hint->second;
            if (is_primitive)
                msg += ". A " + ftype_early + " accepts: params (length/width/height, radius, "
                       "r1/r2, major_r/minor_r), at, at_offset, axis, centered, mode, merge, "
                       "plus name/refs/folder/suppressed.";
            msg += " Unknown keys are rejected, never ignored, so a typo can't build "
                   "silently-wrong geometry.";
            throw OpError("invalid_op", msg);
        }
    }
    // SHAPE, not intent: wrap a single value handed to a list-form ref slot
    // ("profile":"$0" -> ["$0"]). The validator typed those slots as lists while
    // the compiler happily consumes a bare id, so the scalar form every model
    // writes cost an entire rejected batch for nothing — a live Opus run lost one
    // to exactly this while following cad_guide('sketch') to the letter. Owned by
    // validate.cpp so it is driven by the same FEATURE_TYPES table it satisfies.
    //
    // Deliberately SILENT, unlike the dialect rewrites in cad_doc_tools.py which
    // all echo an `interpreted` note. Those can change what gets BUILT, so the
    // model must see them. This one cannot: a list slot given one value has
    // exactly one possible meaning. Pinned by "a scalar in a list-form ref slot
    // equals the one-element list" in core/tests/test_document.cpp — if that
    // equivalence ever breaks, the rewrite has become a semantic guess and MUST
    // start reporting itself. Param defaults live in the Python layer for the
    // same reason; see the scope note on normalize_feature_shape.
    normalize_feature_shape(env);
    resolve_placeholders_deep(env["params"], id_map);
    resolve_placeholders_deep(env["refs"], id_map);

    json& feats = doc["features"];
    if (op.contains("before") && op["before"].is_string()) {
        const std::string before = op["before"].get<std::string>();
        std::size_t pos = feats.size();
        for (std::size_t i = 0; i < feats.size(); ++i)
            if (feats[i].value("id", std::string()) == before) { pos = i; break; }
        feats.insert(feats.begin() + pos, env);
    } else {
        feats.push_back(env);
    }
    id_map["$" + std::to_string(idx)] = fid;
}

void op_edit_feature(json& doc, const json& op, const json& id_map) {
    json* f = find_feature(doc, op.value("id", std::string()));
    if (!f) throw OpError("invalid_op", "edit_feature: no feature '" + op.value("id", std::string()) + "'");
    if (op.contains("params")) {
        json p = op["params"];
        resolve_placeholders_deep(p, id_map);
        if (!f->contains("params")) (*f)["params"] = json::object();
        patch((*f)["params"], p);
    }
    if (op.contains("refs")) {
        json r = op["refs"];
        resolve_placeholders_deep(r, id_map);
        if (!f->contains("refs")) (*f)["refs"] = json::object();
        patch((*f)["refs"], r);
    }
    if (op.contains("name") && op["name"].is_string()) (*f)["name"] = op["name"];
}

void op_delete_feature(json& doc, const json& op) {
    const std::string id = op.value("id", std::string());
    json& feats = doc["features"];
    bool found = false;
    for (const json& f : feats)
        if (f.value("id", std::string()) == id) { found = true; break; }
    if (!found) throw OpError("invalid_op", "delete_feature: no feature '" + id + "'");

    // Dependency guard: any OTHER feature referencing this one blocks (unless cascade).
    std::vector<std::string> dependents;
    for (const json& f : feats) {
        if (f.value("id", std::string()) == id) continue;
        std::set<std::string> refs;
        if (f.contains("refs")) collect_fids(f["refs"], refs);
        if (refs.count(id)) dependents.push_back(f.value("id", std::string()));
    }
    const bool cascade = op.value("cascade", false);
    if (!dependents.empty() && !cascade)
        throw OpError("dependency_exists",
                      "feature '" + id + "' is referenced by other features",
                      {{"dependents", dependents}});

    std::set<std::string> remove{id};
    if (cascade) for (const std::string& d : dependents) remove.insert(d);
    json kept = json::array();
    for (const json& f : feats)
        if (!remove.count(f.value("id", std::string()))) kept.push_back(f);
    feats = kept;
}

void op_reorder_feature(json& doc, const json& op) {
    const std::string id = op.value("id", std::string());
    json& feats = doc["features"];
    json moved;
    json rest = json::array();
    bool found = false;
    for (const json& f : feats) {
        if (f.value("id", std::string()) == id) { moved = f; found = true; }
        else rest.push_back(f);
    }
    if (!found) throw OpError("invalid_op", "reorder_feature: no feature '" + id + "'");
    std::size_t pos = rest.size();
    if (op.contains("before") && op["before"].is_string()) {
        const std::string before = op["before"].get<std::string>();
        for (std::size_t i = 0; i < rest.size(); ++i)
            if (rest[i].value("id", std::string()) == before) { pos = i; break; }
    }
    rest.insert(rest.begin() + pos, moved);
    feats = rest;
}

// Forward declarations (defined later in this TU — same unnamed namespace) so
// op_sketch_convert can project against compiled geometry from inside the ops path (T-BL43).
// `pre` (optional) is an already-compiled result of THIS doc — passing it serves the
// datums/face lookup without another full recompile.
chudware::geom::SketchPlane resolve_sketch_plane_doc(const json& doc, const json& feat,
                                                  const CompileResult* pre = nullptr);
json project_external_refs(const json& doc, const chudware::geom::SketchPlane& plane,
                           const json& refs, const CompileResult* pre = nullptr,
                           bool strict = false);
// T-BL49 associative Convert Entities — re-resolve + re-project every sketch's
// params.external_refs against the current model state (defined near
// project_external_refs below; called from apply_ops_locked / doc_rebuild).
// `cache` (optional) is the owning session's incremental-rebuild CompileCache: the
// refresh compiles the doc it is HANDED (the session's live tree or apply_ops' working
// tree of the same doc), and the cache is content-addressed by the env/config/feature
// hash chain (document.cpp), so a diverged tree simply misses — never served stale.
// `quality` must match the owning session's display tier: it seeds the cache's hash
// chain, so a mismatched pre-compile would miss the prefix checkpoints every time.
void refresh_sketch_external_refs(json& doc, CompileCache* cache = nullptr,
                                  ::geom::DisplayQuality quality =
                                      ::geom::DisplayQuality::Fine);
// sketch3d counterpart (external model references — vertex targets, doc_schema §3B):
// re-resolve every sketch3d feature's params.external_refs vertex selectors against
// the current model, writing back last_point + stale so compile_document's sketch3d
// branch can inject them as fixed ground points (see document.cpp). v1 resolves
// against the FINAL compiled body/bodies (same convention refresh_sketch_external_refs
// uses for face/edge selectors) rather than the body-as-of-that-feature — a documented
// v1 simplification. `cache` as above.
void refresh_sketch3d_external_refs(json& doc, CompileCache* cache = nullptr,
                                    ::geom::DisplayQuality quality =
                                        ::geom::DisplayQuality::Fine);
void op_sketch_delete_entity(json& doc, const json& op, const json& id_map);

// Sketch entities/constraints take PLAIN numbers, but models often wrap scalars as
// {"value":N,"expr":null} (the feature-param convention) -> "must be a number"
// validation rejects. Unwrap that exact shape to the bare number, recursively, so both
// conventions validate identically. Only the {value:number(,expr)} shape is unwrapped.
void unwrap_value_scalars(json& j) {
    if (j.is_object()) {
        if (j.contains("value") && j["value"].is_number() &&
            (j.size() == 1 || (j.size() == 2 && j.contains("expr")))) {
            j = j["value"];
            return;
        }
        for (auto& el : j.items()) unwrap_value_scalars(el.value());
    } else if (j.is_array()) {
        for (auto& el : j) unwrap_value_scalars(el);
    }
}

// A sketch point is [x, y]. Models also write it as an OBJECT — {"x":20,"y":8} or the
// sketch-space {"u":20,"v":8} — and every reader here takes a point only from an array,
// so an object point did not fail: it fell back to the default. A polygon whose center
// arrived as {"x":20,"y":8} was silently built at the ORIGIN, which is how seven patterned
// hexagons ended up stacked on one another (live 2026-07-28). Coerce the object forms into
// arrays before any reader runs; anything else is left exactly as written so the reader's
// own error still names it.
void coerce_object_points(json& j) {
    if (j.is_array()) {
        for (auto& el : j) coerce_object_points(el);
        return;
    }
    if (!j.is_object()) return;
    for (const char* pair : {"xy", "uv"}) {
        const std::string a(1, pair[0]), b(1, pair[1]);
        if (j.size() == 2 && j.contains(a) && j.contains(b) &&
            j[a].is_number() && j[b].is_number()) {
            j = json::array({j[a].get<double>(), j[b].get<double>()});
            return;
        }
    }
    for (auto& el : j.items()) coerce_object_points(el.value());
}
// Normalize + expand ONE inline sketch entity into the concrete entities the geometry kernel
// understands. Applied on BOTH ingestion paths — the sketch_add_entity op AND a sketch feature's
// inline params.entities (op_add_feature) — so a hexagon / rectangle / airfoil / {value:N} scalar
// builds identically however the model phrases it. Convenience types (airfoil, regular polygon,
// rectangle) expand to several lines/splines; any other entity passes through. Every returned
// entity carries an "id" (the model's own if it supplied one — inline constraints reference it —
// else a fresh id) and a "construction" flag.
std::vector<json> expand_sketch_entity(json ent, IdGen& gen) {
    unwrap_value_scalars(ent);  // accept {value:N} scalars in sketch entities
    coerce_object_points(ent);  // accept {"x":X,"y":Y} / {"u":U,"v":V} points

    // Weak-model shape: a spline's points arrive DOUBLE-WRAPPED [[[x,y],...]] — unwrap one level.
    if (ent.value("type", std::string()) == "spline" && ent.contains("points") &&
        ent["points"].is_array() && ent["points"].size() == 1 && ent["points"][0].is_array() &&
        !ent["points"][0].empty() && ent["points"][0][0].is_array()) {
        ent["points"] = ent["points"][0];
    }

    const bool constr = ent.value("construction", false);
    std::vector<json> out;

    // AIRFOIL convenience entity -> upper + lower splines (+ a trailing-edge line for an open TE).
    if (ent.value("type", std::string()) == "airfoil") {
        const std::string naca = ent.value("naca", std::string());
        const std::string dat = ent.value("dat", std::string());
        const double chord = ent.value("chord", 100.0);
        const double aoa = ent.value("aoa", 0.0);
        const int pts = ent.value("points", 60);
        const bool closedTE = ent.value("closedTE", true);
        chudware::geom::AirfoilProfile prof =
            chudware::geom::airfoil_profile(naca, dat, chord, aoa, pts, closedTE);
        if (!prof.error.empty()) throw OpError("invalid_op", "airfoil: " + prof.error);
        if (prof.upper.size() < 2 || prof.lower.size() < 2)
            throw OpError("invalid_op", "airfoil: profile produced too few points");
        auto push_spline = [&](const std::vector<std::array<double, 2>>& pl) {
            json s;
            s["type"] = "spline";
            json arr = json::array();
            for (const auto& q : pl) arr.push_back(json::array({q[0], q[1]}));
            s["points"] = arr;
            s["id"] = gen.next("e");
            s["construction"] = constr;
            out.push_back(std::move(s));
        };
        push_spline(prof.upper);  // LE -> TE (shares the LE point with `lower`)
        push_spline(prof.lower);  // LE -> TE
        if (!prof.sharpTE) {      // close the open trailing edge with a short line TE_u -> TE_l
            json ln;
            ln["type"] = "line";
            ln["p1"] = json::array({prof.upper.back()[0], prof.upper.back()[1]});
            ln["p2"] = json::array({prof.lower.back()[0], prof.lower.back()[1]});
            ln["id"] = gen.next("e");
            ln["construction"] = constr;
            out.push_back(std::move(ln));
        }
        return out;
    }

    // REGULAR POLYGON convenience entity -> N closed line segments (hexagons, nuts, bolt-heads,
    // prisms). A named n-gon implies its side count; an explicit "sides" overrides. Size from ONE
    // of across_flats (wrench size) / circumradius / inradius / diameter.
    {
        const std::string et = ent.value("type", std::string());
        auto named_sides = [](const std::string& t) -> int {
            if (t == "triangle" || t == "equilateral_triangle" || t == "trigon") return 3;
            if (t == "pentagon")                  return 5;
            if (t == "hexagon"   || t == "hex")   return 6;
            if (t == "heptagon"  || t == "septagon") return 7;
            if (t == "octagon"   || t == "oct")   return 8;
            if (t == "nonagon"   || t == "enneagon") return 9;
            if (t == "decagon")                   return 10;
            if (t == "hendecagon" || t == "undecagon") return 11;
            if (t == "dodecagon")                 return 12;
            return 0;
        };
        const int named = named_sides(et);
        if (et == "polygon" || et == "regular_polygon" || et == "ngon" || named > 0) {
            int n = named;
            for (const char* k : {"sides", "n", "num_sides", "count"})
                if (ent.contains(k) && ent[k].is_number_integer()) { n = ent[k].get<int>(); break; }
            if (n < 3) throw OpError("invalid_op",
                                     "polygon: needs 'sides' >= 3 (e.g. {\"type\":\"polygon\","
                                     "\"sides\":6,\"across_flats\":12})");
            double cx = 0.0, cy = 0.0;
            if (ent.contains("center") && ent["center"].is_array() && ent["center"].size() >= 2) {
                cx = ent["center"][0].get<double>();
                cy = ent["center"][1].get<double>();
            }
            auto getnum = [&](std::initializer_list<const char*> keys, double* o) -> bool {
                for (const char* k : keys)
                    if (ent.contains(k) && ent[k].is_number()) { *o = ent[k].get<double>(); return true; }
                return false;
            };
            const double apo = std::cos(M_PI / n);  // inradius / circumradius
            double v = 0.0, R = 0.0;
            if (getnum({"circumradius", "radius", "r", "vertex_radius"}, &v)) R = v;
            else if (getnum({"across_flats", "width_across_flats", "af", "wrench", "flats",
                             "flat_to_flat", "width"}, &v)) R = (v * 0.5) / apo;
            else if (getnum({"inradius", "apothem"}, &v)) R = v / apo;
            else if (getnum({"diameter"}, &v)) R = v * 0.5;  // circum-diameter
            if (!(R > 0.0))
                throw OpError("invalid_op",
                              "polygon: needs a size — 'across_flats' (wrench size), 'circumradius', "
                              "or 'inradius' (mm)");
            double rot = 0.0;
            for (const char* k : {"rotation", "angle"})
                if (ent.contains(k) && ent[k].is_number()) { rot = ent[k].get<double>(); break; }
            rot *= M_PI / 180.0;
            std::vector<std::array<double, 2>> verts;
            for (int i = 0; i < n; ++i) {
                double a = rot + 2.0 * M_PI * i / n;
                verts.push_back({cx + R * std::cos(a), cy + R * std::sin(a)});
            }
            for (int i = 0; i < n; ++i) {
                const auto& a = verts[i];
                const auto& b = verts[(i + 1) % n];
                json ln;
                ln["type"] = "line";
                ln["p1"] = json::array({a[0], a[1]});
                ln["p2"] = json::array({b[0], b[1]});
                ln["id"] = gen.next("e");
                ln["construction"] = constr;
                out.push_back(std::move(ln));
            }
            return out;
        }
    }

    // RECTANGLE convenience entity -> 4 closed CCW line segments. Models emit this constantly
    // (center+width+height); expanding here kills the old "not a sketch entity type" reject loop.
    // Forms:
    //   center:  {"type":"rectangle","center":[x,y],"width":W,"height":H}  (center defaults [0,0])
    //   corners: {"type":"rectangle","corner1":[x1,y1],"corner2":[x2,y2]}
    // width/height accept bare numbers or {"value":N,"expr":null} (unwrap_value_scalars above).
    // Aliases: "rect". Optional "rotation" degrees about center (center-form only).
    {
        const std::string et = ent.value("type", std::string());
        if (et == "rectangle" || et == "rect") {
            auto getnum = [&](std::initializer_list<const char*> keys, double* o) -> bool {
                for (const char* k : keys)
                    if (ent.contains(k) && ent[k].is_number()) {
                        *o = ent[k].get<double>();
                        return true;
                    }
                return false;
            };
            auto is_xy = [](const json& j) -> bool {
                return j.is_array() && j.size() >= 2 && j[0].is_number() && j[1].is_number();
            };
            double x0 = 0.0, y0 = 0.0, x1 = 0.0, y1 = 0.0;
            bool have_box = false;

            // Two-corner form first (corner1/corner2, or p1/p2, or min/max).
            json c1 = json(), c2 = json();
            if (ent.contains("corner1") && is_xy(ent["corner1"])) c1 = ent["corner1"];
            else if (ent.contains("p1") && is_xy(ent["p1"])) c1 = ent["p1"];
            else if (ent.contains("min") && is_xy(ent["min"])) c1 = ent["min"];
            if (ent.contains("corner2") && is_xy(ent["corner2"])) c2 = ent["corner2"];
            else if (ent.contains("p2") && is_xy(ent["p2"])) c2 = ent["p2"];
            else if (ent.contains("max") && is_xy(ent["max"])) c2 = ent["max"];
            if (!c1.is_null() && !c2.is_null()) {
                x0 = std::min(c1[0].get<double>(), c2[0].get<double>());
                x1 = std::max(c1[0].get<double>(), c2[0].get<double>());
                y0 = std::min(c1[1].get<double>(), c2[1].get<double>());
                y1 = std::max(c1[1].get<double>(), c2[1].get<double>());
                have_box = true;
            } else {
                // Center + width/height form (the common model emission).
                double cx = 0.0, cy = 0.0;
                if (ent.contains("center") && is_xy(ent["center"])) {
                    cx = ent["center"][0].get<double>();
                    cy = ent["center"][1].get<double>();
                }
                double w = 0.0, h = 0.0;
                const bool got_w = getnum({"width", "w", "dx", "length"}, &w);
                const bool got_h = getnum({"height", "h", "dy", "depth"}, &h);
                if (!got_w || !got_h) {
                    throw OpError(
                        "invalid_op",
                        "rectangle: expected center-form "
                        "{\"type\":\"rectangle\",\"center\":[x,y],\"width\":W,\"height\":H} "
                        "or two-corner form "
                        "{\"type\":\"rectangle\",\"corner1\":[x1,y1],\"corner2\":[x2,y2]} "
                        "(width/height may be bare numbers or {\"value\":N,\"expr\":null})");
                }
                if (!(w > 0.0) || !(h > 0.0)) {
                    throw OpError("invalid_op",
                                  "rectangle: width and height must be > 0 (got width=" +
                                      std::to_string(w) + ", height=" + std::to_string(h) + ")");
                }
                double rot = 0.0;
                for (const char* k : {"rotation", "angle"})
                    if (ent.contains(k) && ent[k].is_number()) {
                        rot = ent[k].get<double>();
                        break;
                    }
                const double hw = w * 0.5, hh = h * 0.5;
                if (std::fabs(rot) < 1e-12) {
                    x0 = cx - hw;
                    x1 = cx + hw;
                    y0 = cy - hh;
                    y1 = cy + hh;
                    have_box = true;
                } else {
                    // Rotated about center: emit verts directly (axis-aligned box path skipped).
                    rot *= M_PI / 180.0;
                    const double cr = std::cos(rot), sr = std::sin(rot);
                    auto rotp = [&](double lx, double ly) -> std::array<double, 2> {
                        return {cx + lx * cr - ly * sr, cy + lx * sr + ly * cr};
                    };
                    // CCW from bottom-left in local frame.
                    const std::array<double, 2> v0 = rotp(-hw, -hh);
                    const std::array<double, 2> v1 = rotp(+hw, -hh);
                    const std::array<double, 2> v2 = rotp(+hw, +hh);
                    const std::array<double, 2> v3 = rotp(-hw, +hh);
                    const std::array<double, 2> verts[4] = {v0, v1, v2, v3};
                    for (int i = 0; i < 4; ++i) {
                        const auto& a = verts[i];
                        const auto& b = verts[(i + 1) % 4];
                        json ln;
                        ln["type"] = "line";
                        ln["p1"] = json::array({a[0], a[1]});
                        ln["p2"] = json::array({b[0], b[1]});
                        ln["id"] = gen.next("e");
                        ln["construction"] = constr;
                        out.push_back(std::move(ln));
                    }
                    return out;
                }
            }
            if (!have_box || !(x1 > x0) || !(y1 > y0)) {
                throw OpError(
                    "invalid_op",
                    "rectangle: degenerate box — width and height must be > 0 "
                    "(center-form needs width/height; corner-form needs distinct corners)");
            }
            // CCW winding: BL -> BR -> TR -> TL -> BL (consistent with polygon expand).
            const std::array<double, 2> verts[4] = {
                {x0, y0}, {x1, y0}, {x1, y1}, {x0, y1},
            };
            for (int i = 0; i < 4; ++i) {
                const auto& a = verts[i];
                const auto& b = verts[(i + 1) % 4];
                json ln;
                ln["type"] = "line";
                ln["p1"] = json::array({a[0], a[1]});
                ln["p2"] = json::array({b[0], b[1]});
                ln["id"] = gen.next("e");
                ln["construction"] = constr;
                out.push_back(std::move(ln));
            }
            return out;
        }
    }

    // Ordinary entity: keep a CANONICAL e<N> id. Models often emit "l0"/"p1"/etc.;
    // schema validate requires e\d+, so rewrite non-canonical ids here and stash the
    // original under "_alias" so sketch_add_entity can remap later constraint refs.
    if (!ent.contains("id") || !ent["id"].is_string() ||
        ent["id"].get<std::string>().empty()) {
        ent["id"] = gen.next("e");
    } else {
        const std::string raw = ent["id"].get<std::string>();
        static const std::regex kEntIdRe("^e\\d+$");
        if (std::regex_match(raw, kEntIdRe)) {
            gen.note(raw);  // record it so an id-less sibling can't collide
        } else {
            ent["_alias"] = raw;
            ent["id"] = gen.next("e");
        }
    }
    if (!ent.contains("construction")) ent["construction"] = false;
    out.push_back(std::move(ent));
    return out;
}
// Build alias -> canonical-eN map from entities that still carry "_alias" (or
// already-stripped ones that were registered into id_map as bare alias keys).
// Also strips residual "_alias" keys so they never reach validate/schema.
std::map<std::string, std::string> sketch_entity_aliases(json& sk) {
    std::map<std::string, std::string> m;
    if (!sk.contains("params") || !sk["params"].is_object()) return m;
    if (!sk["params"].contains("entities") || !sk["params"]["entities"].is_array()) return m;
    for (json& e : sk["params"]["entities"]) {
        if (!e.is_object()) continue;
        if (e.contains("_alias") && e["_alias"].is_string() &&
            e.contains("id") && e["id"].is_string()) {
            m[e["_alias"].get<std::string>()] = e["id"].get<std::string>();
            e.erase("_alias");
        }
    }
    return m;
}

// Rewrite constraint refs that name a modeler alias (e.g. "l5", "l0.end") onto the
// canonical eN id minted at entity-add time. Unknown refs are left alone so the
// solver/validate path can still emit a precise error.
void remap_constraint_refs(json& c, const std::map<std::string, std::string>& aliases,
                           const json& id_map) {
    if (!c.contains("refs") || !c["refs"].is_array()) return;
    for (json& rref : c["refs"]) {
        if (!rref.is_string()) continue;
        std::string s = rref.get<std::string>();
        // Prefer batch placeholders already resolved by resolve_placeholders_deep.
        if (!s.empty() && s[0] == '$' && id_map.contains(s)) {
            rref = id_map[s];
            continue;
        }
        std::string base = s, sub;
        auto dot = s.find('.');
        if (dot != std::string::npos) {
            base = s.substr(0, dot);
            sub = s.substr(dot);  // includes leading '.'
        }
        auto it = aliases.find(base);
        if (it != aliases.end()) {
            rref = it->second + sub;
            continue;
        }
        // Also accept bare alias keys stashed on id_map by sketch_add_entity.
        if (id_map.contains(base) && id_map[base].is_string()) {
            rref = id_map[base].get<std::string>() + sub;
        }
    }
}

void op_sketch_add_entity(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    json ent;
    if (op.contains("entity") && op["entity"].is_object()) {
        ent = op["entity"];
    } else if (op.contains("type") && op["type"].is_string()) {
        // Lenient FLAT form: models emit {op:sketch_add_entity, sketch, type, center,
        // radius, ...} with entity fields at the top level instead of nested under
        // "entity" — an unguarded op.at("entity") crashed the whole op batch (json
        // out_of_range), the root cause of failed tube/prism/sketch builds. Hoist the
        // top-level fields (minus op/sketch routing keys) so both forms behave the same.
        ent = json::object();
        for (auto& [k, v] : op.items())
            if (k != "op" && k != "sketch") ent[k] = v;
    } else {
        throw OpError("invalid_op",
                      "sketch_add_entity: 'entity' object (or a top-level 'type') required");
    }
    std::vector<json> new_ents = expand_sketch_entity(std::move(ent), gen);
    for (json& e : new_ents) {
        // Register modeler alias -> canonical id so later constraints can resolve "l5".
        if (e.contains("_alias") && e["_alias"].is_string() &&
            e.contains("id") && e["id"].is_string()) {
            id_map[e["_alias"].get<std::string>()] = e["id"].get<std::string>();
            e.erase("_alias");
        }
        sk["params"]["entities"].push_back(std::move(e));
    }
    // Front id is the primary entity for $N / $self; multi-entity expands
    // (polygon/rectangle/airfoil) expose siblings as $N#k.
    std::string eid;
    if (!new_ents.empty()) {
        const std::size_t n = new_ents.size();
        const std::size_t total = sk["params"]["entities"].size();
        eid = sk["params"]["entities"][total - n].value("id", std::string());
        id_map["$" + std::to_string(idx)] = eid;
        for (std::size_t k = 1; k < n; ++k)
            id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] =
                sk["params"]["entities"][total - n + k].value("id", std::string());
    } else {
        id_map["$" + std::to_string(idx)] = std::string();
    }
    // Inline constraints: "$self" -> the new entity id; ids cN.
    if (op.contains("constraints") && op["constraints"].is_array()) {
        auto aliases = sketch_entity_aliases(sk);
        for (const json& c0 : op["constraints"]) {
            json c = c0;
            // resolve $self (and $self.sub) in refs to the new entity id.
            if (c.contains("refs") && c["refs"].is_array()) {
                for (json& rref : c["refs"]) {
                    if (rref.is_string()) {
                        std::string s = rref.get<std::string>();
                        if (s == "$self") rref = eid;
                        else if (s.rfind("$self.", 0) == 0) rref = eid + s.substr(5);
                    }
                }
                resolve_placeholders_deep(c["refs"], id_map);
                remap_constraint_refs(c, aliases, id_map);
            }
            c["id"] = gen.next("c");
            sk["params"]["constraints"].push_back(c);
        }
    }
}

void op_sketch_add_constraint(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    json c;
    if (op.contains("constraint") && op["constraint"].is_object()) {
        c = op["constraint"];
    } else if (op.contains("type") && op["type"].is_string()) {
        // Lenient FLAT form: {op:sketch_add_constraint, sketch, type, entities, ...}
        // with constraint fields at the top level instead of nested under "constraint"
        // (same json-crash class as sketch_add_entity — broke concentric/coincident in
        // grok's tube/prism builds). Hoist top-level fields minus the routing keys.
        c = json::object();
        for (auto& [k, v] : op.items())
            if (k != "op" && k != "sketch") c[k] = v;
    } else {
        throw OpError("invalid_op",
                      "sketch_add_constraint: 'constraint' object (or a top-level 'type') required");
    }
    unwrap_value_scalars(c);  // accept {value:N} scalars in sketch constraints
    // Dimensional constraints must store value as {value, expr} for schema validate.
    // unwrap_value_scalars flattens {value:40, expr:null} → 40; re-wrap bare numbers
    // (and leave already-object dims alone). Solver resolve_dim accepts both shapes.
    if (c.contains("type") && c["type"].is_string()) {
        const std::string ct = c["type"].get<std::string>();
        if ((ct == "distance" || ct == "length" || ct == "angle" || ct == "radius" ||
             ct == "diameter") &&
            c.contains("value") && c["value"].is_number()) {
            c["value"] = json{{"value", c["value"].get<double>()}, {"expr", nullptr}};
        }
    }
    resolve_placeholders_deep(c, id_map);
    // Remap modeler entity aliases (l0/l5/...) onto canonical eN ids BEFORE validate
    // and before the solver sees the constraint. Without this, "l5" fails schema
    // (handle must be e\d+) and "distance{refs:[l5]}" never reaches the length path.
    {
        auto aliases = sketch_entity_aliases(sk);
        remap_constraint_refs(c, aliases, id_map);
    }
    // Defensive arity check at the op boundary so a bad model reply becomes a clean
    // OpError instead of a later solver crash. Mirrors CONSTRAINT_SPECS lo bounds.
    if (c.contains("type") && c["type"].is_string()) {
        const std::string ctype = c["type"].get<std::string>();
        std::size_t nrefs = 0;
        if (c.contains("refs") && c["refs"].is_array()) nrefs = c["refs"].size();
        auto bad = [&](std::size_t need, const char* why) {
            std::string got;
            if (c.contains("refs") && c["refs"].is_array()) {
                for (std::size_t i = 0; i < c["refs"].size(); ++i) {
                    if (i) got += ", ";
                    if (c["refs"][i].is_string())
                        got += "'" + c["refs"][i].get<std::string>() + "'";
                    else
                        got += c["refs"][i].dump();
                }
            }
            if (got.empty()) got = "(none)";
            throw OpError("invalid_op",
                          std::string(ctype) + " constraint needs " + std::to_string(need) +
                              " ref(s) (got " + std::to_string(nrefs) + "): " + got +
                              (why ? std::string(" — ") + why : std::string()));
        };
        if (ctype == "coincident" || ctype == "parallel" || ctype == "perpendicular" ||
            ctype == "tangent" || ctype == "collinear" || ctype == "equal" ||
            ctype == "concentric" || ctype == "coradial" || ctype == "midpoint" ||
            ctype == "point_on" || ctype == "angle") {
            if (nrefs < 2) bad(2, nullptr);
        } else if (ctype == "symmetric") {
            if (nrefs < 3) bad(3, nullptr);
        } else if (ctype == "fix" || ctype == "radius" || ctype == "diameter" ||
                   ctype == "length") {
            if (nrefs < 1) bad(1, nullptr);
        } else if (ctype == "horizontal" || ctype == "vertical") {
            if (nrefs < 1) bad(1, nullptr);
        } else if (ctype == "distance") {
            // 1 ref = line length (bare line/slot); 2 refs = point-point / point-line.
            if (nrefs < 1)
                bad(2, "distance needs 2 geometric refs, or 1 bare line for length");
            if (nrefs == 1 && c["refs"][0].is_string()) {
                const std::string h = c["refs"][0].get<std::string>();
                if (h.find('.') != std::string::npos)
                    bad(2, "single-ref distance is only valid on a bare line/slot");
            }
        }
    }
    const std::string cid = gen.next("c");
    c["id"] = cid;
    sk["params"]["constraints"].push_back(c);
    id_map["$" + std::to_string(idx)] = cid;
}

// T-BL43 "Convert Entities": project external model refs (edges / whole-face boundary wires /
// datum planes) onto THIS sketch's plane and append the projected curves as sketch entities.
// Both GUIs (host + ui/imgui applyConvertRef) have emitted this op since C53 — until now the
// engine had NO handler, so every in-GUI Convert Entities click failed with "unsupported op".
// Shape: {op:"sketch_convert", sketch:<fid>,
//         refs:[{"datum":"front|top|right"} | {"kind":"face"|"edge","selector":{...}}],
//         construction:bool (default false — SolidWorks Convert Entities emits REAL geometry
//                            usable as an extrudable profile; pass true for reference-only),
//         fix:bool (default true — pin each projected curve so the solver treats it as an
//                   anchor other entities constrain AGAINST, not a free curve it may drag)}.
// Projection is orthographic along the sketch normal (GeomProjLib via recon.cpp) — a tilted
// circle correctly lands as an ellipse.
//
// T-BL49 ASSOCIATIVE (default): each ref is also recorded in the sketch's
// params.external_refs as {id:"x<N>", source:<the ref>, construction, fix,
// entity_ids:[...], stale:false}. On every later doc rebuild,
// refresh_sketch_external_refs re-resolves the source selector against the CURRENT
// model and re-projects, updating the owned entities IN PLACE (stable entity ids, so
// user constraints/dims attached to converted geometry survive). A source that no
// longer resolves marks the ref stale (last-good geometry kept, rebuild never fails).
// Pass associative:false for the legacy one-shot copy (no link stored).
void op_sketch_convert(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const json refs = op.contains("refs") ? op["refs"] : json::array();
    if (!refs.is_array() || refs.empty())
        throw OpError("invalid_op", "sketch_convert: 'refs' must be a non-empty list",
                      {{"reason", "bad refs"}});
    chudware::geom::SketchPlane plane = resolve_sketch_plane_doc(doc, sk);
    json res = project_external_refs(doc, plane, refs);
    const bool constr = op.value("construction", false);
    const bool fix = op.value("fix", true);
    const bool assoc = op.value("associative", true);
    std::vector<std::string> ids;
    for (json ent : res["entities"]) {
        ent["construction"] = constr;
        const std::string eid = gen.next("e");
        ent["id"] = eid;
        sk["params"]["entities"].push_back(std::move(ent));
        ids.push_back(eid);
        if (fix)
            sk["params"]["constraints"].push_back(
                json{{"type", "fix"}, {"refs", json::array({eid})}, {"id", gen.next("c")}});
    }
    if (ids.empty())
        throw OpError("convert_failed",
                      "sketch_convert: nothing projectable in refs (degenerate/seam edges?)",
                      {{"reason", "no entities"}});
    if (assoc) {
        // Record one external_ref per source ref; slice the flat entity-id list by the
        // per-source counts project_external_refs reports (sources[i].count).
        if (!sk["params"].contains("external_refs") || !sk["params"]["external_refs"].is_array())
            sk["params"]["external_refs"] = json::array();
        const json& sources = res["sources"];
        std::size_t pos = 0;
        for (std::size_t i = 0; i < refs.size() && i < sources.size(); ++i) {
            const int cnt = sources[i].value("count", 0);
            json eids = json::array();
            for (int k = 0; k < cnt && pos < ids.size(); ++k, ++pos) eids.push_back(ids[pos]);
            sk["params"]["external_refs"].push_back(
                json{{"id", gen.next("x")}, {"source", refs[i]}, {"construction", constr},
                     {"fix", fix}, {"entity_ids", std::move(eids)}, {"stale", false}});
        }
    }
    id_map["$" + std::to_string(idx)] = ids[0];
    for (std::size_t k = 1; k < ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = ids[k];
}

// T-BL49 — break the associative link of ONE external ref (SolidWorks "break link"):
// removes the ref record; the converted entities stay in the sketch as ordinary
// (still fix-pinned) geometry. {op:"sketch_break_external_ref", sketch:<fid>, id:"x<N>",
// delete_entities:false} — pass delete_entities:true to also remove the owned entities
// (cascading constraints that touch them, same as sketch_delete_entity).
void op_sketch_break_external_ref(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string xid = op.value("id", std::string());
    if (!sk["params"].contains("external_refs") || !sk["params"]["external_refs"].is_array())
        throw OpError("invalid_op", "sketch_break_external_ref: sketch has no external refs",
                      {{"reason", "no external refs"}});
    json kept = json::array();
    json removed;
    for (const json& xr : sk["params"]["external_refs"]) {
        if (xr.value("id", std::string()) == xid) removed = xr;
        else kept.push_back(xr);
    }
    if (removed.is_null())
        throw OpError("invalid_op", "sketch_break_external_ref: no external ref '" + xid + "'",
                      {{"reason", "unknown external ref"}});
    sk["params"]["external_refs"] = kept;
    if (op.value("delete_entities", false) && removed.contains("entity_ids")) {
        for (const json& eid : removed["entity_ids"]) {
            if (!eid.is_string()) continue;
            json del{{"op", "sketch_delete_entity"}, {"sketch", op.value("sketch", std::string())},
                     {"id", eid.get<std::string>()}};
            op_sketch_delete_entity(doc, del, id_map);
        }
    }
}

void op_sketch_edit_entity(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("id", std::string());
    for (json& e : sk["params"]["entities"]) {
        if (e.value("id", std::string()) == eid) {
            if (op.contains("params")) patch(e, op["params"]);
            return;
        }
    }
    throw OpError("invalid_op", "sketch_edit_entity: no entity '" + eid + "'");
}

void op_sketch_delete_entity(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("id", std::string());
    json kept = json::array();
    for (const json& e : sk["params"]["entities"])
        if (e.value("id", std::string()) != eid) kept.push_back(e);
    sk["params"]["entities"] = kept;
    // Cascade-drop constraints whose handles touch the entity (ref starts "eid" or "eid.").
    json kc = json::array();
    for (const json& c : sk["params"]["constraints"]) {
        bool touches = false;
        if (c.contains("refs") && c["refs"].is_array())
            for (const json& rref : c["refs"])
                if (rref.is_string()) {
                    const std::string s = rref.get<std::string>();
                    if (s == eid || s.rfind(eid + ".", 0) == 0) { touches = true; break; }
                }
        if (!touches) kc.push_back(c);
    }
    sk["params"]["constraints"] = kc;
}

void op_sketch_delete_constraint(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string cid = op.value("id", std::string());
    json kept = json::array();
    for (const json& c : sk["params"]["constraints"])
        if (c.value("id", std::string()) != cid) kept.push_back(c);
    sk["params"]["constraints"] = kept;
}

void op_sketch_set_dim(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string cid = op.value("id", std::string());
    for (json& c : sk["params"]["constraints"]) {
        if (c.value("id", std::string()) == cid) {
            // A constraint's `value` is a dim object {value, expr} (see validate_dim).
            // Any subset of {value|expr, placement, driven} may be supplied; expr wins
            // over value when both are present (mirrors the Python oracle).
            bool touched = false;
            if (op.contains("expr") && !op["expr"].is_null()) {
                double cur = (c.contains("value") && c["value"].is_object())
                                 ? c["value"].value("value", 0.0)
                                 : 0.0;
                c["value"] = json{{"value", cur}, {"expr", op["expr"]}};
                touched = true;
            } else if (op.contains("value") && !op["value"].is_null()) {
                c["value"] = json{{"value", op["value"].get<double>()}, {"expr", nullptr}};
                touched = true;
            }
            if (op.contains("placement")) {
                if (op["placement"].is_null()) {
                    c.erase("placement");
                } else if (op["placement"].is_array() && op["placement"].size() == 2) {
                    c["placement"] = json{op["placement"][0].get<double>(),
                                          op["placement"][1].get<double>()};
                } else {
                    throw OpError("invalid_op", "sketch_set_dim.placement must be [u, v] or null");
                }
                touched = true;
            }
            if (op.contains("driven") && !op["driven"].is_null()) {
                if (!op["driven"].is_boolean())
                    throw OpError("invalid_op", "sketch_set_dim.driven must be a bool");
                c["driven"] = op["driven"];
                touched = true;
            }
            if (!touched)
                throw OpError("invalid_op",
                              "sketch_set_dim requires 'value', 'expr', 'placement', or 'driven'");
            return;
        }
    }
    throw OpError("invalid_op", "sketch_set_dim: no constraint '" + cid + "'");
}

void op_sketch_toggle_construction(json& doc, const json& op, const json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("id", std::string());
    for (json& e : sk["params"]["entities"])
        if (e.value("id", std::string()) == eid) {
            e["construction"] = !e.value("construction", false);
            return;
        }
    throw OpError("invalid_op", "sketch_toggle_construction: no entity '" + eid + "'");
}

// ----------------------------------------------------------- advanced sketch ops
// 2D tools (trim/extend/offset/fillet/chamfer/mirror/pattern/move) — port of the
// engine/doc/ops.py handlers over the engine/doc/sketch_geom curve math. Each mutates
// the sketch's entity + constraint lists and flows through the normal rebuild.
namespace sg = chudware::doc::sketchgeom;

constexpr double kDeg = 3.14159265358979323846 / 180.0;
constexpr double kRad = 180.0 / 3.14159265358979323846;

// A geom-only view of a stored entity (drops id/construction; sketch_geom shape).
json geom_view(const json& ent) {
    json o = json::object();
    for (auto it = ent.begin(); it != ent.end(); ++it)
        if (it.key() != "id" && it.key() != "construction") o[it.key()] = it.value();
    return o;
}
json stamp_entity(const json& geom, const std::string& eid, bool construction) {
    json o = geom;
    o["id"] = eid;
    o["construction"] = construction;
    return o;
}
std::string handle_base(const std::string& h) {
    auto p = h.find('.');
    return p == std::string::npos ? h : h.substr(0, p);
}
void drop_constraints_touching(json& sk, const std::set<std::string>& eids) {
    json& cons = sk["params"]["constraints"];
    json kept = json::array();
    for (const json& c : cons) {
        bool touch = false;
        if (c.contains("refs") && c["refs"].is_array())
            for (const json& h : c["refs"])
                if (h.is_string() && eids.count(handle_base(h.get<std::string>()))) { touch = true; break; }
        if (!touch) kept.push_back(c);
    }
    cons = kept;
}

// --- trim constraint preservation ------------------------------------------
// The sub-handle part of a constraint ref ("e3.start" -> "start"; "e3" -> "").
std::string handle_sub(const std::string& h) {
    auto p = h.find('.');
    return p == std::string::npos ? std::string() : h.substr(p + 1);
}
// The 2D point a POINT sub-handle refers to on a stored entity geom, or nullopt for
// whole-entity / scalar handles (parallel/tangent/radius/angle drag) that carry no point.
std::optional<std::array<double, 2>> handle_point(const json& geom, const std::string& sub) {
    const std::string t = geom.value("type", std::string());
    if (t == "line") {
        std::array<double, 2> p1{geom["p1"][0], geom["p1"][1]};
        std::array<double, 2> p2{geom["p2"][0], geom["p2"][1]};
        if (sub == "p1" || sub == "start") return p1;
        if (sub == "p2" || sub == "end") return p2;
        if (sub == "mid") return std::array<double, 2>{(p1[0] + p2[0]) * 0.5, (p1[1] + p2[1]) * 0.5};
        return std::nullopt;
    }
    // circle / arc
    double cx = geom["center"][0], cy = geom["center"][1];
    if (sub.empty() || sub == "center") return std::array<double, 2>{cx, cy};
    if (t == "arc" && (sub == "start" || sub == "end")) {
        double r = geom.value("radius", 0.0);
        double ang = geom.value(sub, 0.0) * M_PI / 180.0;
        return std::array<double, 2>{cx + r * std::cos(ang), cy + r * std::sin(ang)};
    }
    return std::nullopt;  // rim / start_angle / end_angle scalar drag handles
}
// The named endpoints of a surviving span, for matching an old point handle to it.
struct SpanEnd { std::array<double, 2> p; std::string sub; };
std::vector<SpanEnd> span_ends(const json& geom) {
    const std::string t = geom.value("type", std::string());
    if (t == "line")
        return {{{geom["p1"][0], geom["p1"][1]}, "p1"}, {{geom["p2"][0], geom["p2"][1]}, "p2"}};
    if (t == "arc") {
        auto s = handle_point(geom, "start"), e = handle_point(geom, "end");
        return {{*s, "start"}, {*e, "end"}};
    }
    return {};
}
bool pt_near(const std::array<double, 2>& a, const std::array<double, 2>& b) {
    return std::hypot(a[0] - b[0], a[1] - b[1]) < 1e-6;
}

// Rewrite every constraint that referenced the trimmed entity `eid` onto whichever
// surviving span still carries the referenced point, instead of blindly dropping them
// (the old behaviour spiked DOF). survivors = (id, geom) for each span, in emit order;
// survivors[0].id == eid. A constraint is dropped only when its point vanished with the
// removed span. Whole-entity direction/center refs stay on `eid` (span[0] is co-geometric).
void remap_constraints_after_trim(json& sk, const std::string& eid, const json& old_geom,
                                  const std::vector<std::pair<std::string, json>>& survivors) {
    json& cons = sk["params"]["constraints"];
    json kept = json::array();
    for (json c : cons) {
        if (!c.contains("refs") || !c["refs"].is_array()) { kept.push_back(c); continue; }
        bool drop = false;
        for (json& hh : c["refs"]) {
            if (!hh.is_string()) continue;
            std::string h = hh.get<std::string>();
            if (handle_base(h) != eid) continue;
            std::string sub = handle_sub(h);
            auto p = handle_point(old_geom, sub);
            // Whole-entity / scalar handles (direction, center, radius, angle): span[0]
            // keeps id `eid` and the same line direction / centre+radius, so these stay valid.
            if (!p || sub.empty() || sub == "center") continue;
            // Point handle: find the survivor whose endpoint still sits at that point.
            bool matched = false;
            for (const auto& [sid, geom] : survivors) {
                for (const SpanEnd& se : span_ends(geom)) {
                    if (pt_near(se.p, *p)) { hh = sid + "." + se.sub; matched = true; break; }
                }
                if (matched) break;
            }
            if (!matched) { drop = true; break; }  // the referenced point was trimmed away
        }
        if (!drop) kept.push_back(c);
    }
    cons = kept;
}
json* find_sketch_entity(json& sk, const std::string& eid) {
    for (json& e : sk["params"]["entities"])
        if (e.value("id", std::string()) == eid) return &e;
    return nullptr;
}
[[noreturn]] void unsupported_entity(std::size_t idx, const std::string& op_name,
                                     const std::string& etype) {
    throw OpError("unsupported_entity",
                  "op[" + std::to_string(idx) + "]: " + op_name +
                      " does not support entity type '" + etype + "'",
                  {{"index", idx}, {"reason", "unsupported entity '" + etype + "'"}});
}

void op_sketch_trim(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("entity", std::string());
    json& ents = sk["params"]["entities"];
    json* target = find_sketch_entity(sk, eid);
    if (!target) throw OpError("invalid_op", "sketch_trim: entity '" + eid + "' not found");
    if (!op.contains("point") || !op["point"].is_array() || op["point"].size() != 2)
        throw OpError("invalid_op", "sketch_trim requires 'point': [u, v]");
    double px = op["point"][0].get<double>(), py = op["point"][1].get<double>();
    // Keep the trimmed-away span as construction geometry instead of deleting it, so its
    // reference relations survive (SolidWorks "keep as construction geometry" trim option).
    const bool keep_construction = op.value("keep_construction", false);
    std::size_t pos = 0;
    for (std::size_t i = 0; i < ents.size(); ++i)
        if (ents[i].value("id", std::string()) == eid) { pos = i; break; }
    bool construction = target->value("construction", false);
    json old_geom = geom_view(*target);
    // Cutters: EVERY other line/circle/arc — construction geometry included — matching the GUI
    // power-trim preview (sketch_mode.cpp trimDoomedSpan). Excluding construction here made the
    // engine trim past a construction boundary the preview had already cut at (over-trim
    // regression); non-curve entities (points/text/splines) can't bound a trim, so skip them
    // exactly as the preview does rather than letting split_entity reject the whole op.
    std::vector<json> others;
    for (const json& e : ents) {
        if (e.value("id", std::string()) == eid) continue;
        const std::string t = e.value("type", std::string());
        if (t == "line" || t == "circle" || t == "arc") others.push_back(geom_view(e));
    }
    sg::SplitResult split;
    try {
        split = sg::split_entity_ex(old_geom, others, px, py);
    } catch (const sg::UnsupportedEntity& exc) {
        throw OpError("unsupported_entity",
                      "op[" + std::to_string(idx) + "]: sketch_trim involves an unsupported entity (" +
                          exc.etype + ")",
                      {{"index", idx}, {"reason", "unsupported entity '" + exc.etype + "'"}});
    }
    if (split.survivors.empty()) {
        // No interior crossing: the whole entity is doomed.
        if (keep_construction && split.removed) {
            // Demote it to construction in place — geometry unchanged, so every constraint
            // that referenced it stays valid.
            (*target)["construction"] = true;
            id_map["$" + std::to_string(idx)] = eid;
            return;
        }
        json kept = json::array();
        for (const json& e : ents)
            if (e.value("id", std::string()) != eid) kept.push_back(e);
        ents = kept;
        drop_constraints_touching(sk, {eid});
        id_map["$" + std::to_string(idx)] = "";
        return;
    }
    // Emit the survivors: survivors[0] keeps `eid`, the rest get fresh ids.
    std::vector<json> new_ents;
    std::vector<std::pair<std::string, json>> survivor_ids;  // (id, geom) for constraint remap
    new_ents.push_back(stamp_entity(split.survivors[0], eid, construction));
    survivor_ids.emplace_back(eid, split.survivors[0]);
    std::vector<std::string> extra_ids;
    for (std::size_t i = 1; i < split.survivors.size(); ++i) {
        std::string neid = gen.next("e");
        extra_ids.push_back(neid);
        new_ents.push_back(stamp_entity(split.survivors[i], neid, construction));
        survivor_ids.emplace_back(neid, split.survivors[i]);
    }
    // Optionally re-emit the doomed span as construction geometry (keeps it defined against
    // the surrounding sketch instead of destroying it).
    std::string kept_con_id;
    if (keep_construction && split.removed) {
        kept_con_id = gen.next("e");
        new_ents.push_back(stamp_entity(*split.removed, kept_con_id, true));
    }
    json spliced = json::array();
    for (std::size_t i = 0; i < ents.size(); ++i) {
        if (i == pos)
            for (const json& ne : new_ents) spliced.push_back(ne);
        else
            spliced.push_back(ents[i]);
    }
    ents = spliced;
    // Preserve relations: re-anchor constraints onto the surviving spans by geometry rather
    // than dropping every ref to `eid` (that blind drop was the DOF-explosion regression).
    remap_constraints_after_trim(sk, eid, old_geom, survivor_ids);
    id_map["$" + std::to_string(idx)] = eid;
    for (std::size_t k = 0; k < extra_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k + 1)] = extra_ids[k];
    if (!kept_con_id.empty())
        id_map["$" + std::to_string(idx) + "#c"] = kept_con_id;
}

void op_sketch_extend(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    (void)gen;
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string eid = op.value("entity", std::string());
    json* target = find_sketch_entity(sk, eid);
    if (!target) throw OpError("invalid_op", "sketch_extend: entity '" + eid + "' not found");
    const std::string end = op.value("end", std::string());
    if (end != "start" && end != "end")
        throw OpError("invalid_op", "sketch_extend requires 'end': 'start' | 'end'");
    std::vector<json> others;
    for (const json& e : sk["params"]["entities"])
        if (e.value("id", std::string()) != eid && !e.value("construction", false))
            others.push_back(geom_view(e));
    std::optional<json> new_geom;
    try {
        new_geom = sg::extend_entity(geom_view(*target), end, others);
    } catch (const sg::UnsupportedEntity& exc) {
        throw OpError("unsupported_entity",
                      "op[" + std::to_string(idx) + "]: sketch_extend involves an unsupported entity (" +
                          exc.etype + ")",
                      {{"index", idx}, {"reason", "unsupported entity '" + exc.etype + "'"}});
    }
    if (!new_geom)
        throw OpError("extend_no_target",
                      "op[" + std::to_string(idx) + "]: sketch_extend found nothing to extend '" + eid + "' to",
                      {{"index", idx}, {"reason", "no target ahead of the extended end"}});
    std::string id = (*target)["id"].get<std::string>();
    bool construction = target->value("construction", false);
    json updated = *new_geom;
    updated["id"] = id;
    updated["construction"] = construction;
    *target = updated;
    id_map["$" + std::to_string(idx)] = eid;
}

void op_sketch_offset(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities") || !op["entities"].is_array() || op["entities"].empty())
        throw OpError("invalid_op", "sketch_offset requires a non-empty 'entities' id list");
    if (!op.contains("distance") || !op["distance"].is_number())
        throw OpError("invalid_op", "sketch_offset requires a numeric 'distance'");
    if (!op.contains("side") || !op["side"].is_array() || op["side"].size() != 2)
        throw OpError("invalid_op", "sketch_offset requires 'side': [u, v]");
    double distance = op["distance"].get<double>();
    double sx = op["side"][0].get<double>(), sy = op["side"][1].get<double>();
    std::vector<json> chain;
    for (const json& e_id : op["entities"]) {
        json* ent = find_sketch_entity(sk, e_id.get<std::string>());
        if (!ent)
            throw OpError("invalid_op", "sketch_offset: entity '" + e_id.get<std::string>() + "' not found");
        chain.push_back(geom_view(*ent));
    }
    std::vector<json> out;
    bool closed = false;
    try {
        out = sg::offset_chain(chain, distance, sx, sy);
        closed = sg::chain_is_closed(chain);
    } catch (const sg::UnsupportedEntity& exc) {
        throw OpError("unsupported_entity",
                      "op[" + std::to_string(idx) + "]: sketch_offset involves an unsupported entity (" +
                          exc.etype + ")",
                      {{"index", idx}, {"reason", "unsupported entity '" + exc.etype + "'"}});
    }
    json& ents = sk["params"]["entities"];
    json& cons = sk["params"]["constraints"];
    std::vector<std::string> new_ids;
    std::vector<std::string> new_types;
    for (const json& span : out) {
        std::string neid = gen.next("e");
        new_ids.push_back(neid);
        new_types.push_back(span.value("type", std::string()));
        ents.push_back(stamp_entity(span, neid, false));
    }
    auto end_handle = [&](std::size_t i, const std::string& which) -> std::string {
        if (new_types[i] == "line") return new_ids[i] + (which == "start" ? ".p1" : ".p2");
        return new_ids[i] + "." + which;
    };
    int n = (int)new_ids.size();
    int joints = (closed && n > 1) ? n : n - 1;
    for (int i = 0; i < joints; ++i) {
        int a = i, b = (i + 1) % n;
        cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                        {"refs", {end_handle(a, "end"), end_handle(b, "start")}}});
    }
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? std::string() : new_ids[0];
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

// Map a geom end name ('start'|'end') to a line's point handle ('p1'|'p2').
std::string line_end_handle(const std::string& eid, const std::string& which) {
    return eid + (which == "start" ? ".p1" : ".p2");
}
void set_line_end(json& ent, const std::string& which, const json& ptj) {
    ent[which == "start" ? "p1" : "p2"] = {ptj[0].get<double>(), ptj[1].get<double>()};
}
// Drop any constraint that JOINED the two corner endpoints to each other.
void drop_corner_join(json& sk, const std::string& h1, const std::string& h2) {
    json& cons = sk["params"]["constraints"];
    json kept = json::array();
    for (const json& c : cons) {
        bool has1 = false, has2 = false;
        if (c.contains("refs") && c["refs"].is_array())
            for (const json& r : c["refs"])
                if (r.is_string()) {
                    if (r.get<std::string>() == h1) has1 = true;
                    if (r.get<std::string>() == h2) has2 = true;
                }
        if (has1 && has2) continue;
        kept.push_back(c);
    }
    cons = kept;
}

void op_sketch_fillet(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string e1id = op.value("e1", std::string()), e2id = op.value("e2", std::string());
    json* e1 = find_sketch_entity(sk, e1id);
    json* e2 = find_sketch_entity(sk, e2id);
    if (!e1 || !e2)
        throw OpError("invalid_op", "sketch_fillet requires existing 'e1' and 'e2' entity ids");
    if (e1id == e2id)
        throw OpError("invalid_op", "sketch_fillet: e1 and e2 must be different entities");
    if (!op.contains("radius") || !op["radius"].is_number() || op["radius"].get<double>() <= 0)
        throw OpError("invalid_op", "sketch_fillet requires a positive numeric 'radius'");
    double radius = op["radius"].get<double>();
    for (json* ent : {e1, e2})
        if (ent->value("type", std::string()) != "line")
            unsupported_entity(idx, "sketch_fillet supports line-line corners only — got", ent->value("type", std::string()));
    json res;
    try {
        res = sg::fillet_corner(geom_view(*e1), geom_view(*e2), radius);
    } catch (const sg::GeomError& exc) {
        throw OpError("fillet_failed", "op[" + std::to_string(idx) + "]: sketch_fillet — " + exc.reason,
                      {{"index", idx}, {"reason", exc.reason}});
    }
    drop_corner_join(sk, line_end_handle(e1id, res["end_a"].get<std::string>()),
                     line_end_handle(e2id, res["end_b"].get<std::string>()));
    set_line_end(*e1, res["end_a"].get<std::string>(), res["ta"]);
    set_line_end(*e2, res["end_b"].get<std::string>(), res["tb"]);
    const std::string aid = gen.next("e");
    json arc = res["arc"];
    arc["id"] = aid;
    arc["construction"] = false;
    sk["params"]["entities"].push_back(arc);
    json& cons = sk["params"]["constraints"];
    cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                    {"refs", {aid + ".start", line_end_handle(e1id, res["end_a"].get<std::string>())}}});
    cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                    {"refs", {aid + ".end", line_end_handle(e2id, res["end_b"].get<std::string>())}}});
    cons.push_back({{"id", gen.next("c")}, {"type", "tangent"}, {"refs", {aid, e1id}}});
    cons.push_back({{"id", gen.next("c")}, {"type", "tangent"}, {"refs", {aid, e2id}}});
    id_map["$" + std::to_string(idx)] = aid;
}

void op_sketch_chamfer(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    const std::string e1id = op.value("e1", std::string()), e2id = op.value("e2", std::string());
    json* e1 = find_sketch_entity(sk, e1id);
    json* e2 = find_sketch_entity(sk, e2id);
    if (!e1 || !e2)
        throw OpError("invalid_op", "sketch_chamfer requires existing 'e1' and 'e2' entity ids");
    if (e1id == e2id)
        throw OpError("invalid_op", "sketch_chamfer: e1 and e2 must be different entities");
    if (!op.contains("distance") || !op["distance"].is_number() || op["distance"].get<double>() <= 0)
        throw OpError("invalid_op", "sketch_chamfer requires a positive numeric 'distance'");
    double distance = op["distance"].get<double>();
    double distance2 = distance;
    if (op.contains("distance2") && !op["distance2"].is_null()) {
        if (!op["distance2"].is_number() || op["distance2"].get<double>() <= 0)
            throw OpError("invalid_op", "sketch_chamfer 'distance2' must be a positive number when given");
        distance2 = op["distance2"].get<double>();
    }
    for (json* ent : {e1, e2})
        if (ent->value("type", std::string()) != "line")
            unsupported_entity(idx, "sketch_chamfer supports line-line corners only — got", ent->value("type", std::string()));
    json res;
    try {
        res = sg::chamfer_corner(geom_view(*e1), geom_view(*e2), distance, distance2);
    } catch (const sg::GeomError& exc) {
        throw OpError("chamfer_failed", "op[" + std::to_string(idx) + "]: sketch_chamfer — " + exc.reason,
                      {{"index", idx}, {"reason", exc.reason}});
    }
    drop_corner_join(sk, line_end_handle(e1id, res["end_a"].get<std::string>()),
                     line_end_handle(e2id, res["end_b"].get<std::string>()));
    set_line_end(*e1, res["end_a"].get<std::string>(), res["ta"]);
    set_line_end(*e2, res["end_b"].get<std::string>(), res["tb"]);
    const std::string lid = gen.next("e");
    json line = {{"id", lid}, {"type", "line"}, {"construction", false},
                 {"p1", {res["ta"][0].get<double>(), res["ta"][1].get<double>()}},
                 {"p2", {res["tb"][0].get<double>(), res["tb"][1].get<double>()}}};
    sk["params"]["entities"].push_back(line);
    json& cons = sk["params"]["constraints"];
    cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                    {"refs", {lid + ".p1", line_end_handle(e1id, res["end_a"].get<std::string>())}}});
    cons.push_back({{"id", gen.next("c")}, {"type", "coincident"},
                    {"refs", {lid + ".p2", line_end_handle(e2id, res["end_b"].get<std::string>())}}});
    id_map["$" + std::to_string(idx)] = lid;
}

// ---- inline 2D transforms for mirror / pattern / move ----
const std::set<std::string> kMpSupported = {"line", "circle", "arc"};

std::array<double, 2> reflect_point(const json& ptj, const std::array<double, 2>& A,
                                    const std::array<double, 2>& B) {
    double ax = A[0], ay = A[1], bx = B[0], by = B[1];
    double dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
    if (L2 < 1e-18)
        throw OpError("mirror_failed", "mirror line is degenerate (zero length)",
                      {{"reason", "degenerate mirror line"}});
    double pxv = ptj[0].get<double>(), pyv = ptj[1].get<double>();
    double px0 = pxv - ax, py0 = pyv - ay;
    double t = (px0 * dx + py0 * dy) / L2;
    double fx = ax + t * dx, fy = ay + t * dy;
    return {2.0 * fx - pxv, 2.0 * fy - pyv};
}
double reflect_angle_deg(double ang, const std::array<double, 2>& A, const std::array<double, 2>& B) {
    double axis = std::atan2(B[1] - A[1], B[0] - A[0]);
    return (2.0 * axis - ang * kDeg) * kRad;
}
json mirror_entity(const json& ent, const std::array<double, 2>& A, const std::array<double, 2>& B) {
    std::string t = ent.value("type", std::string());
    if (t == "line") {
        auto p1 = reflect_point(ent["p1"], A, B);
        auto p2 = reflect_point(ent["p2"], A, B);
        return {{"type", "line"}, {"p1", {p1[0], p1[1]}}, {"p2", {p2[0], p2[1]}}};
    }
    if (t == "circle") {
        auto c = reflect_point(ent["center"], A, B);
        return {{"type", "circle"}, {"center", {c[0], c[1]}}, {"radius", ent["radius"].get<double>()}};
    }
    if (t == "arc") {
        double a0 = reflect_angle_deg(ent["start"].get<double>(), A, B);
        double a1 = reflect_angle_deg(ent["end"].get<double>(), A, B);
        auto c = reflect_point(ent["center"], A, B);
        return {{"type", "arc"}, {"center", {c[0], c[1]}}, {"radius", ent["radius"].get<double>()},
                {"start", a1}, {"end", a0}, {"ccw", !ent.value("ccw", true)}};
    }
    throw OpError("unsupported_entity", "mirror/pattern does not support entity type '" + t + "'",
                  {{"reason", "unsupported entity '" + t + "'"}});
}
json translate_entity(const json& geom, double dx, double dy) {
    json out = geom;
    std::string t = out.value("type", std::string());
    if (t == "line") {
        out["p1"] = {out["p1"][0].get<double>() + dx, out["p1"][1].get<double>() + dy};
        out["p2"] = {out["p2"][0].get<double>() + dx, out["p2"][1].get<double>() + dy};
    } else {
        out["center"] = {out["center"][0].get<double>() + dx, out["center"][1].get<double>() + dy};
    }
    return out;
}
json rotate_entity(const json& geom, double cx, double cy, double ang_deg) {
    double a = ang_deg * kDeg, ca = std::cos(a), sa = std::sin(a);
    auto rot = [&](const json& p) -> json {
        double x = p[0].get<double>() - cx, y = p[1].get<double>() - cy;
        return json::array({cx + x * ca - y * sa, cy + x * sa + y * ca});
    };
    json out = geom;
    std::string t = out.value("type", std::string());
    if (t == "line") {
        out["p1"] = rot(out["p1"]);
        out["p2"] = rot(out["p2"]);
    } else {
        out["center"] = rot(out["center"]);
        if (t == "arc") {
            out["start"] = out["start"].get<double>() + ang_deg;
            out["end"] = out["end"].get<double>() + ang_deg;
        }
    }
    return out;
}
// Point-handle pairs whose symmetric relation keeps a<->b mirrored, + radius-equal flag.
std::pair<std::vector<std::pair<std::string, std::string>>, bool>
symmetric_pairs(const std::string& etype, const std::string& a, const std::string& b) {
    if (etype == "line")
        return {{{a + ".p1", b + ".p1"}, {a + ".p2", b + ".p2"}}, false};
    if (etype == "circle")
        return {{{a + ".center", b + ".center"}}, true};
    if (etype == "arc")
        return {{{a + ".center", b + ".center"}, {a + ".start", b + ".start"}, {a + ".end", b + ".end"}}, true};
    return {{}, false};
}

void op_sketch_mirror_entities(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities") || !op["entities"].is_array() || op["entities"].empty())
        throw OpError("invalid_op", "sketch_mirror_entities requires a non-empty 'entities' id list");
    const std::string line_id = op.value("line", std::string());
    if (line_id.empty())
        throw OpError("invalid_op", "sketch_mirror_entities requires a 'line' (mirror line entity id)");
    json* axis = find_sketch_entity(sk, line_id);
    if (!axis)
        throw OpError("invalid_op", "sketch_mirror_entities: mirror line '" + line_id + "' not found");
    if (axis->value("type", std::string()) != "line")
        throw OpError("unsupported_entity", "sketch_mirror_entities: the mirror line must be a line entity",
                      {{"reason", "mirror line is a '" + axis->value("type", std::string()) + "'"}});
    std::array<double, 2> A = {(*axis)["p1"][0].get<double>(), (*axis)["p1"][1].get<double>()};
    std::array<double, 2> B = {(*axis)["p2"][0].get<double>(), (*axis)["p2"][1].get<double>()};
    std::vector<json> sources;
    for (const json& e_id : op["entities"]) {
        const std::string sid = e_id.get<std::string>();
        if (sid == line_id)
            throw OpError("invalid_op", "sketch_mirror_entities: cannot mirror the mirror line itself");
        json* ent = find_sketch_entity(sk, sid);
        if (!ent)
            throw OpError("invalid_op", "sketch_mirror_entities: entity '" + sid + "' not found");
        if (!kMpSupported.count(ent->value("type", std::string())))
            unsupported_entity(idx, "sketch_mirror_entities", ent->value("type", std::string()));
        sources.push_back(*ent);
    }
    json& ents = sk["params"]["entities"];
    json& cons = sk["params"]["constraints"];
    std::vector<std::string> new_ids;
    for (const json& ent : sources) {
        json geom = mirror_entity(ent, A, B);
        std::string neid = gen.next("e");
        new_ids.push_back(neid);
        ents.push_back(stamp_entity(geom, neid, ent.value("construction", false)));
        auto [pairs, needs_equal] = symmetric_pairs(ent.value("type", std::string()),
                                                    ent.value("id", std::string()), neid);
        for (auto& [ha, hb] : pairs)
            cons.push_back({{"id", gen.next("c")}, {"type", "symmetric"}, {"refs", {ha, hb, line_id}}});
        if (needs_equal)
            cons.push_back({{"id", gen.next("c")}, {"type", "equal"},
                            {"refs", {ent.value("id", std::string()), neid}}});
    }
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? std::string() : new_ids[0];
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

void op_sketch_pattern_entities(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities") || !op["entities"].is_array() || op["entities"].empty())
        throw OpError("invalid_op", "sketch_pattern_entities requires a non-empty 'entities' id list");
    const std::string mode = op.value("mode", std::string());
    if (mode != "linear" && mode != "circular")
        throw OpError("invalid_op", "sketch_pattern_entities requires mode 'linear' or 'circular'");
    if (!op.contains("count") || !op["count"].is_number_integer() || op["count"].get<int>() < 2)
        throw OpError("invalid_op", "sketch_pattern_entities requires an integer 'count' >= 2");
    int count = op["count"].get<int>();
    std::vector<json> sources;
    for (const json& e_id : op["entities"]) {
        json* ent = find_sketch_entity(sk, e_id.get<std::string>());
        if (!ent)
            throw OpError("invalid_op",
                          "sketch_pattern_entities: entity '" + e_id.get<std::string>() + "' not found");
        if (!kMpSupported.count(ent->value("type", std::string())))
            unsupported_entity(idx, "sketch_pattern_entities", ent->value("type", std::string()));
        sources.push_back(*ent);
    }
    // Per-step transforms: ("t", dx, dy) | ("r", cx, cy, ang).
    struct Step { bool rot; double a, b, c; };
    std::vector<Step> steps;
    if (mode == "linear") {
        if (!op.contains("dir") || !op["dir"].is_array() || op["dir"].size() != 2 ||
            !op["dir"][0].is_number() || !op["dir"][1].is_number())
            throw OpError("invalid_op", "sketch_pattern_entities (linear) requires 'dir': [dx, dy]");
        if (!op.contains("spacing") || !op["spacing"].is_number())
            throw OpError("invalid_op", "sketch_pattern_entities (linear) requires a numeric 'spacing'");
        double nx = op["dir"][0].get<double>(), ny = op["dir"][1].get<double>();
        double spacing = op["spacing"].get<double>();
        double L = std::hypot(nx, ny);
        if (L < 1e-12)
            throw OpError("invalid_op", "sketch_pattern_entities (linear) 'dir' is zero-length");
        nx /= L; ny /= L;
        for (int i = 1; i < count; ++i) steps.push_back({false, nx * spacing * i, ny * spacing * i, 0.0});
    } else {
        if (!op.contains("center") || !op["center"].is_array() || op["center"].size() != 2 ||
            !op["center"][0].is_number() || !op["center"][1].is_number())
            throw OpError("invalid_op", "sketch_pattern_entities (circular) requires 'center': [x, y]");
        if (!op.contains("angle") || !op["angle"].is_number())
            throw OpError("invalid_op", "sketch_pattern_entities (circular) requires a numeric 'angle'");
        double cx = op["center"][0].get<double>(), cy = op["center"][1].get<double>();
        double angle = op["angle"].get<double>();
        bool equal = op.value("equal_spacing", false);
        double step_ang = equal ? (angle / count) : angle;
        for (int i = 1; i < count; ++i) steps.push_back({true, cx, cy, step_ang * i});
    }
    json& ents = sk["params"]["entities"];
    std::vector<std::string> new_ids;
    for (const Step& tf : steps) {
        for (const json& ent : sources) {
            json geom = geom_view(ent);
            json placed = tf.rot ? rotate_entity(geom, tf.a, tf.b, tf.c)
                                 : translate_entity(geom, tf.a, tf.b);
            std::string neid = gen.next("e");
            new_ids.push_back(neid);
            ents.push_back(stamp_entity(placed, neid, ent.value("construction", false)));
        }
    }
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? std::string() : new_ids[0];
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

void op_sketch_move(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities") || !op["entities"].is_array() || op["entities"].empty())
        throw OpError("invalid_op", "sketch_move requires a non-empty 'entities' id list");
    if (!op.contains("dx") || !op["dx"].is_number())
        throw OpError("invalid_op", "sketch_move requires a numeric 'dx'");
    if (!op.contains("dy") || !op["dy"].is_number())
        throw OpError("invalid_op", "sketch_move requires a numeric 'dy'");
    double dx = op["dx"].get<double>(), dy = op["dy"].get<double>();
    bool copy_flag = false;
    if (op.contains("copy")) {
        if (!op["copy"].is_boolean()) throw OpError("invalid_op", "sketch_move 'copy' must be a bool");
        copy_flag = op["copy"].get<bool>();
    }
    std::vector<std::string> source_ids;
    for (const json& e_id : op["entities"]) {
        json* ent = find_sketch_entity(sk, e_id.get<std::string>());
        if (!ent)
            throw OpError("invalid_op", "sketch_move: entity '" + e_id.get<std::string>() + "' not found");
        if (!kMpSupported.count(ent->value("type", std::string())))
            unsupported_entity(idx, "sketch_move", ent->value("type", std::string()));
        source_ids.push_back(e_id.get<std::string>());
    }
    if (!copy_flag) {
        for (const std::string& sid : source_ids) {
            json* ent = find_sketch_entity(sk, sid);
            json moved = translate_entity(geom_view(*ent), dx, dy);
            for (auto it = moved.begin(); it != moved.end(); ++it) (*ent)[it.key()] = it.value();
        }
        id_map["$" + std::to_string(idx)] = source_ids.empty() ? std::string() : source_ids[0];
        return;
    }
    json& ents = sk["params"]["entities"];
    std::vector<std::string> new_ids;
    for (const std::string& sid : source_ids) {
        json* ent = find_sketch_entity(sk, sid);
        bool construction = ent->value("construction", false);
        json geom = translate_entity(geom_view(*ent), dx, dy);
        std::string neid = gen.next("e");
        new_ids.push_back(neid);
        ents.push_back(stamp_entity(geom, neid, construction));
    }
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? std::string() : new_ids[0];
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

// Entity id owning a constraint ref token ("e3" -> "e3", "e3.p1" -> "e3").
static std::string ref_entity_id(const std::string& ref) {
    auto dot = ref.find('.');
    return dot == std::string::npos ? ref : ref.substr(0, dot);
}

// Rotate a selection of sketch entities about (cx, cy) by 'angle' degrees. Translation-style
// counterpart of op_sketch_move. Because horizontal/vertical relations are axis-aligned, a
// rotation whose net angle is not a multiple of 90 breaks them; we therefore rewrite the
// horizontal/vertical constraints whose refs lie WHOLLY inside the rotated set:
//   ~0 / 180 deg -> keep unchanged   |   90 / 270 deg -> swap horizontal<->vertical
//   any other    -> drop (the GUI diffs before/after to report what was removed).
// Constraints straddling rotated + fixed geometry are left for the solver to reconcile.
void op_sketch_rotate(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities") || !op["entities"].is_array() || op["entities"].empty())
        throw OpError("invalid_op", "sketch_rotate requires a non-empty 'entities' id list");
    if (!op.contains("cx") || !op["cx"].is_number())
        throw OpError("invalid_op", "sketch_rotate requires a numeric 'cx'");
    if (!op.contains("cy") || !op["cy"].is_number())
        throw OpError("invalid_op", "sketch_rotate requires a numeric 'cy'");
    if (!op.contains("angle") || !op["angle"].is_number())
        throw OpError("invalid_op", "sketch_rotate requires a numeric 'angle' (degrees)");
    double cx = op["cx"].get<double>(), cy = op["cy"].get<double>();
    double angle = op["angle"].get<double>();
    bool copy_flag = false;
    if (op.contains("copy")) {
        if (!op["copy"].is_boolean()) throw OpError("invalid_op", "sketch_rotate 'copy' must be a bool");
        copy_flag = op["copy"].get<bool>();
    }
    std::vector<std::string> source_ids;
    for (const json& e_id : op["entities"]) {
        json* ent = find_sketch_entity(sk, e_id.get<std::string>());
        if (!ent)
            throw OpError("invalid_op", "sketch_rotate: entity '" + e_id.get<std::string>() + "' not found");
        if (!kMpSupported.count(ent->value("type", std::string())))
            unsupported_entity(idx, "sketch_rotate", ent->value("type", std::string()));
        source_ids.push_back(e_id.get<std::string>());
    }

    if (copy_flag) {
        json& ents = sk["params"]["entities"];
        std::vector<std::string> new_ids;
        for (const std::string& sid : source_ids) {
            json* ent = find_sketch_entity(sk, sid);
            bool construction = ent->value("construction", false);
            json geom = rotate_entity(geom_view(*ent), cx, cy, angle);
            std::string neid = gen.next("e");
            new_ids.push_back(neid);
            ents.push_back(stamp_entity(geom, neid, construction));
        }
        id_map["$" + std::to_string(idx)] = new_ids.empty() ? std::string() : new_ids[0];
        for (std::size_t k = 1; k < new_ids.size(); ++k)
            id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
        return;
    }

    // In-place rotate the geometry of every selected entity.
    for (const std::string& sid : source_ids) {
        json* ent = find_sketch_entity(sk, sid);
        json rotated = rotate_entity(geom_view(*ent), cx, cy, angle);
        for (auto it = rotated.begin(); it != rotated.end(); ++it) (*ent)[it.key()] = it.value();
    }

    // Rewrite horizontal/vertical relations wholly inside the rotated set.
    double na = std::fmod(std::fmod(angle, 360.0) + 360.0, 360.0);  // normalize to [0,360)
    const double eps = 1e-6;
    bool is180 = std::fabs(na - 180.0) < eps || na < eps || std::fabs(na - 360.0) < eps;
    bool is90 = std::fabs(na - 90.0) < eps || std::fabs(na - 270.0) < eps;
    std::set<std::string> moved(source_ids.begin(), source_ids.end());
    if (!is180 && sk["params"].contains("constraints") && sk["params"]["constraints"].is_array()) {
        json& cons = sk["params"]["constraints"];
        json kept = json::array();
        // Whole-line horizontal/vertical locks being retired at a non-axis angle. We can't keep
        // them (the line is no longer axis-aligned) and simply dropping them leaves the shape
        // FLOPPY (under-defined) so the solver shears it on re-solve. Instead we record each line's
        // original orientation and, below, re-impose the SAME relative rigidity with rotation-
        // invariant parallel/perpendicular relations against a reference line — so a rotated square
        // stays a rigid square (size + right angles preserved), just free to spin (1 DOF).
        std::vector<std::pair<std::string, bool>> oriented;   // (line_id, wasHorizontal)
        for (json& c : cons) {
            const std::string ctype = c.value("type", std::string());
            bool hv = (ctype == "horizontal" || ctype == "vertical");
            bool wholly_inside = hv && c.contains("refs") && c["refs"].is_array() && !c["refs"].empty();
            if (wholly_inside)
                for (const auto& r : c["refs"])
                    if (!moved.count(ref_entity_id(r.get<std::string>()))) { wholly_inside = false; break; }
            if (hv && wholly_inside) {
                if (is90) {                       // swap horizontal <-> vertical (still axis-aligned)
                    c["type"] = (ctype == "horizontal") ? "vertical" : "horizontal";
                    kept.push_back(c);
                } else {
                    // Non-axis angle: retire this lock. If it's a whole-line h/v (refs == [line_id]),
                    // remember the line so we can re-impose parallel/perp rigidity below. Two-point
                    // h/v forms (point alignment) can't be preserved this way — just drop them.
                    if (c["refs"].size() == 1) {
                        std::string eid = ref_entity_id(c["refs"][0].get<std::string>());
                        json* le = find_sketch_entity(sk, eid);
                        if (le && le->value("type", std::string()) == "line")
                            oriented.push_back({eid, ctype == "horizontal"});
                    }
                }
            } else {
                kept.push_back(c);
            }
        }
        // Re-impose rigidity: pin every other retired line's orientation relative to the first
        // (parallel if it had the same orientation, perpendicular if opposite). N locks -> N-1
        // relative constraints, leaving one global spin DOF — shape-preserving, never distorting.
        if (oriented.size() >= 2) {
            const std::string& refLine = oriented.front().first;
            const bool refH = oriented.front().second;
            for (std::size_t i = 1; i < oriented.size(); ++i) {
                const char* rtype = (oriented[i].second == refH) ? "parallel" : "perpendicular";
                kept.push_back({{"id", gen.next("c")}, {"type", rtype},
                                {"refs", json::array({refLine, oriented[i].first})}});
            }
        }
        cons = kept;
    }
    id_map["$" + std::to_string(idx)] = source_ids.empty() ? std::string() : source_ids[0];
}

// Dispatch one op against the working doc.
// ----------------------------------------------------------------- assembly ops
// Port of engine/doc/ops.py::_op_asm_* (component + mate vocabulary). IdGen mints
// k<N> components and m<N> mates; the existing IdGen.note() already records k/m highs
// from a loaded doc (any prefix letters + digits) so counters never reuse a freed id.

const std::array<double, 16> kIdent16{1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};

void op_asm_insert_component(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    const json& source = op.contains("source") ? op["source"] : json(nullptr);
    bool ok_src = source.is_object() &&
                  ((source.contains("path") && source["path"].is_string()) ||
                   (source.contains("embedded") && source["embedded"].is_string()));
    if (!ok_src)
        throw OpError("invalid_op", "asm_insert_component requires source.{path|embedded}",
                      {{"index", idx}});
    const std::string kid = gen.next("k");
    if (!doc.contains("components") || !doc["components"].is_array()) doc["components"] = json::array();
    json& comps = doc["components"];
    bool first = comps.empty();
    // Default name = source file stem (SolidWorks names components from the file).
    std::string default_name = kid;
    if (source.contains("path") && source["path"].is_string()) {
        std::string stem = source["path"].get<std::string>();
        for (char& c : stem) if (c == '\\') c = '/';
        auto slash = stem.rfind('/');
        if (slash != std::string::npos) stem = stem.substr(slash + 1);
        static const char* exts[] = {".step+", ".step", ".stp", ".stl", ".brep", ".iges", ".igs"};
        std::string low = stem;
        std::transform(low.begin(), low.end(), low.begin(),
                       [](unsigned char c) { return (char)std::tolower(c); });
        for (const char* e : exts) {
            std::string es(e);
            if (low.size() >= es.size() && low.compare(low.size() - es.size(), es.size(), es) == 0) {
                stem = stem.substr(0, stem.size() - es.size());
                break;
            }
        }
        default_name = stem.empty() ? kid : stem;
    }
    json comp;
    comp["id"] = kid;
    comp["name"] = (op.contains("name") && op["name"].is_string() && !op["name"].get<std::string>().empty())
                       ? op["name"] : json(default_name);
    comp["source"] = source;
    comp["transform"] = (op.contains("transform") && op["transform"].is_array())
                            ? op["transform"] : json(kIdent16);
    comp["fixed"] = op.contains("fixed") ? json(op.value("fixed", first)) : json(first);
    comp["suppressed"] = false;
    comps.push_back(std::move(comp));
    id_map["$" + std::to_string(idx)] = kid;
}

void op_asm_edit_component(json& doc, const json& op, std::size_t idx) {
    const std::string kid = op.value("id", std::string());
    if (doc.contains("components") && doc["components"].is_array())
        for (json& c : doc["components"])
            if (c.value("id", std::string()) == kid) {
                // `hidden`: component body is skipped in the renderer (host-side bodyVisible
                // mask, re-derived from this flag on every load) but still participates in the
                // mate solve / interference / BOM — unlike `suppressed`, which drops the
                // component from the build entirely. Persisted so hide survives save + undo.
                for (const char* k : {"transform", "name", "suppressed", "fixed", "hidden"})
                    if (op.contains(k) && !op[k].is_null()) c[k] = op[k];
                return;
            }
    throw OpError("invalid_op", "asm_edit_component: '" + kid + "' not found", {{"index", idx}});
}

void op_asm_delete_component(json& doc, const json& op, std::size_t idx) {
    const std::string kid = op.value("id", std::string());
    json comps = (doc.contains("components") && doc["components"].is_array()) ? doc["components"]
                                                                             : json::array();
    bool found = false;
    for (const json& c : comps)
        if (c.value("id", std::string()) == kid) { found = true; break; }
    if (!found)
        throw OpError("invalid_op", "asm_delete_component: '" + kid + "' not found", {{"index", idx}});
    json mates = (doc.contains("mates") && doc["mates"].is_array()) ? doc["mates"] : json::array();
    std::vector<std::string> referencing;
    for (const json& m : mates) {
        bool refs_kid = false;
        if (m.contains("refs") && m["refs"].is_array())
            for (const json& r : m["refs"])
                if (r.is_object() && r.value("component", std::string()) == kid) refs_kid = true;
        if (refs_kid) referencing.push_back(m.value("id", std::string()));
    }
    if (!referencing.empty() && !op.value("cascade", false))
        throw OpError("dependency_exists",
                      "component '" + kid + "' used by mates", {{"dependents", referencing}});
    json kept_comps = json::array();
    for (const json& c : comps)
        if (c.value("id", std::string()) != kid) kept_comps.push_back(c);
    // Re-anchor: the first inserted component is the fixed origin anchor (see
    // op_asm_insert_component's `first`). If the deleted component was that anchor and no
    // surviving component is still fixed, promote the first survivor so the assembly keeps
    // an origin anchor (fall back to the next component up the tree).
    if (!kept_comps.empty()) {
        bool any_fixed = false;
        for (const json& c : kept_comps)
            if (c.value("fixed", false)) { any_fixed = true; break; }
        if (!any_fixed) kept_comps[0]["fixed"] = true;
    }
    doc["components"] = kept_comps;
    json kept_mates = json::array();
    for (const json& m : mates) {
        bool refs_kid = false;
        if (m.contains("refs") && m["refs"].is_array())
            for (const json& r : m["refs"])
                if (r.is_object() && r.value("component", std::string()) == kid) refs_kid = true;
        if (!refs_kid) kept_mates.push_back(m);
    }
    doc["mates"] = kept_mates;
}

void op_asm_add_mate(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    if (!op.contains("mate") || !op["mate"].is_object())
        throw OpError("invalid_op", "asm_add_mate requires a 'mate' object", {{"index", idx}});
    json m = op["mate"];  // deep copy
    m["id"] = gen.next("m");
    if (!m.contains("suppressed")) m["suppressed"] = false;
    if (!m.contains("align")) m["align"] = "aligned";
    if (!doc.contains("mates") || !doc["mates"].is_array()) doc["mates"] = json::array();
    doc["mates"].push_back(m);
    id_map["$" + std::to_string(idx)] = m["id"];
}

void op_asm_edit_mate(json& doc, const json& op, std::size_t idx) {
    const std::string mid = op.value("id", std::string());
    if (doc.contains("mates") && doc["mates"].is_array())
        for (json& m : doc["mates"])
            if (m.value("id", std::string()) == mid) {
                for (const char* k : {"value", "align", "suppressed"})
                    if (op.contains(k) && !op[k].is_null()) m[k] = op[k];
                return;
            }
    throw OpError("invalid_op", "asm_edit_mate: '" + mid + "' not found", {{"index", idx}});
}

void op_asm_delete_mate(json& doc, const json& op, std::size_t idx) {
    const std::string mid = op.value("id", std::string());
    json mates = (doc.contains("mates") && doc["mates"].is_array()) ? doc["mates"] : json::array();
    bool found = false;
    for (const json& m : mates)
        if (m.value("id", std::string()) == mid) { found = true; break; }
    if (!found)
        throw OpError("invalid_op", "asm_delete_mate: '" + mid + "' not found", {{"index", idx}});
    json kept = json::array();
    for (const json& m : mates)
        if (m.value("id", std::string()) != mid) kept.push_back(m);
    doc["mates"] = kept;
}

void op_asm_set_explode(json& doc, const json& op, std::size_t idx) {
    if (!op.contains("steps") || !op["steps"].is_array())
        throw OpError("invalid_op", "asm_set_explode requires a 'steps' list", {{"index", idx}});
    doc["explode"] = op["steps"];
}

// =========================================================================== copy/paste + derived
// C++ port of the copy/paste-as-derived subsystem (engine/doc/ops.py Wave 4A/4C +
// engine/doc/session.py refresh_derived). copy_feature packages a feature (+ its full
// reference closure) into a portable "aicad.feature_bundle/1" bundle (read-only, via the
// id_map side-channel). paste_feature re-ids the bundle and appends it (refs re-linked to
// the target doc). paste_feature_as_derived additionally stamps a derived_from link on each
// pasted feature so a later doc_refresh_derived can re-pull the live source definition.
// Everything here mirrors the oracle byte-for-byte so the differential parity harness matches.

std::atomic<unsigned long long> g_group_seq{1};
std::string new_group_id() {
    unsigned long long n = g_group_seq.fetch_add(1);
    char buf[32];
    std::snprintf(buf, sizeof(buf), "dg-%010llx", n);
    return std::string(buf);
}

bool is_reserved_datum(const std::string& s) {
    static const std::set<std::string> R = {
        "origin", "plane.front", "plane.top", "plane.right", "axis.x", "axis.y", "axis.z"};
    return R.count(s) > 0;
}
bool is_feature_id_token(const std::string& s) {
    static const std::regex re("^f[0-9]+$");
    return std::regex_match(s, re);
}

// const feature finder (build_feature_bundle / closure read a const source doc).
const json* find_feature_c(const json& doc, const std::string& id) {
    if (doc.contains("features") && doc["features"].is_array())
        for (const json& f : doc["features"])
            if (f.is_object() && f.value("id", std::string()) == id) return &f;
    return nullptr;
}

// ops.py::_walk_selector_owners — collect every owner_feature / datum.feature / region.sketch.
void walk_selector_owners(const json& obj, std::set<std::string>& out) {
    if (obj.is_object()) {
        const std::string kind = obj.value("kind", std::string());
        if (kind == "face" || kind == "edge" || kind == "vertex" || kind == "body") {
            if (obj.contains("owner_feature") && obj["owner_feature"].is_string())
                out.insert(obj["owner_feature"].get<std::string>());
        } else if (kind == "datum") {
            if (obj.contains("feature") && obj["feature"].is_string())
                out.insert(obj["feature"].get<std::string>());
        } else if (kind == "region") {
            if (obj.contains("sketch") && obj["sketch"].is_string())
                out.insert(obj["sketch"].get<std::string>());
        }
        for (auto it = obj.begin(); it != obj.end(); ++it) walk_selector_owners(it.value(), out);
    } else if (obj.is_array()) {
        for (const json& v : obj) walk_selector_owners(v, out);
    }
}

// ops.py::_feature_refs_closure — dependency-first order of the transitive ref closure.
std::pair<std::vector<std::string>, std::set<std::string>>
feature_refs_closure(const json& doc, const std::string& root_fid) {
    std::set<std::string> in_closure;
    std::function<std::set<std::string>(const std::string&)> direct_refs =
        [&](const std::string& fid) -> std::set<std::string> {
        std::set<std::string> out;
        const json* feat = find_feature_c(doc, fid);
        if (!feat) return out;
        const json refs = feat->contains("refs") ? (*feat)["refs"] : json::object();
        walk_selector_owners(refs, out);
        if (refs.is_object())
            for (auto it = refs.begin(); it != refs.end(); ++it) {
                const json& v = it.value();
                if (v.is_array()) {
                    for (const json& x : v)
                        if (x.is_string()) out.insert(x.get<std::string>());
                } else if (v.is_string()) {
                    out.insert(v.get<std::string>());
                }
            }
        std::set<std::string> filtered;
        for (const std::string& r : out)
            if (!is_reserved_datum(r) && find_feature_c(doc, r) != nullptr) filtered.insert(r);
        return filtered;
    };
    std::function<void(const std::string&)> visit = [&](const std::string& fid) {
        if (in_closure.count(fid)) return;
        in_closure.insert(fid);
        for (const std::string& dep : direct_refs(fid)) visit(dep);
    };
    visit(root_fid);
    std::vector<std::string> order;
    if (doc.contains("features") && doc["features"].is_array())
        for (const json& f : doc["features"]) {
            const std::string id = f.value("id", std::string());
            if (in_closure.count(id)) order.push_back(id);
        }
    return {order, in_closure};
}

// ops.py::_remap_handle — rewrite the base of a sketch handle through eid_map.
json remap_handle(const json& handle, const std::map<std::string, std::string>& eid_map) {
    if (handle.is_string()) {
        const std::string h = handle.get<std::string>();
        const auto dot = h.find('.');
        const std::string base = (dot == std::string::npos) ? h : h.substr(0, dot);
        auto it = eid_map.find(base);
        if (it != eid_map.end())
            return json(it->second + (dot == std::string::npos ? std::string() : h.substr(dot)));
    }
    return handle;
}

// ops.py::_stamp_bundle_refs — in-closure ids -> placeholders; out-of-closure feature ids
// recorded in external_refs (left raw).
json stamp_bundle_refs(const json& refs, const std::map<std::string, std::string>& fid_to_ph,
                       std::set<std::string>& external_refs) {
    auto remap_fid = [&](const std::string& fid) -> std::string {
        auto it = fid_to_ph.find(fid);
        if (it != fid_to_ph.end()) return it->second;
        if (!is_reserved_datum(fid) && is_feature_id_token(fid)) external_refs.insert(fid);
        return fid;
    };
    std::function<json(const json&)> walk = [&](const json& obj) -> json {
        if (obj.is_object()) {
            json out = json::object();
            const std::string kind = obj.value("kind", std::string());
            for (auto it = obj.begin(); it != obj.end(); ++it) {
                const std::string& k = it.key();
                const json& v = it.value();
                if ((kind == "face" || kind == "edge" || kind == "vertex" || kind == "body") &&
                    k == "owner_feature" && v.is_string())
                    out[k] = remap_fid(v.get<std::string>());
                else if (kind == "datum" && k == "feature" && v.is_string())
                    out[k] = remap_fid(v.get<std::string>());
                else if (kind == "region" && k == "sketch" && v.is_string())
                    out[k] = remap_fid(v.get<std::string>());
                else
                    out[k] = walk(v);
            }
            return out;
        }
        if (obj.is_array()) {
            json a = json::array();
            for (const json& v : obj) a.push_back(walk(v));
            return a;
        }
        if (obj.is_string()) {
            const std::string s = obj.get<std::string>();
            if (is_reserved_datum(s)) return obj;
            if (is_feature_id_token(s)) return json(remap_fid(s));
        }
        return obj;
    };
    return walk(refs.is_null() ? json::object() : refs);
}

// ops.py::_stamp_bundle_feature — portable copy: id removed, refs placeholdered, sketch
// entity/constraint ids -> ~e<n>/~c<n> with handles re-pointed.
json stamp_bundle_feature(const json& feat, const std::map<std::string, std::string>& fid_to_ph,
                          std::set<std::string>& external_refs) {
    json out = feat;
    out.erase("id");
    out["refs"] = stamp_bundle_refs(out.contains("refs") ? out["refs"] : json::object(),
                                    fid_to_ph, external_refs);
    if (out.value("type", std::string()) == "sketch") {
        if (!out.contains("params") || !out["params"].is_object()) out["params"] = json::object();
        json& params = out["params"];
        if (!params.contains("entities") || !params["entities"].is_array())
            params["entities"] = json::array();
        if (!params.contains("constraints") || !params["constraints"].is_array())
            params["constraints"] = json::array();
        json& ents = params["entities"];
        json& cons = params["constraints"];
        std::map<std::string, std::string> eid_map;
        for (std::size_t i = 0; i < ents.size(); ++i)
            if (ents[i].is_object() && ents[i].contains("id") && ents[i]["id"].is_string())
                eid_map[ents[i]["id"].get<std::string>()] = "~e" + std::to_string(i);
        for (std::size_t i = 0; i < ents.size(); ++i)
            if (ents[i].is_object()) ents[i]["id"] = "~e" + std::to_string(i);
        for (std::size_t i = 0; i < cons.size(); ++i)
            if (cons[i].is_object()) {
                cons[i]["id"] = "~c" + std::to_string(i);
                json nr = json::array();
                if (cons[i].contains("refs") && cons[i]["refs"].is_array())
                    for (const json& h : cons[i]["refs"]) nr.push_back(remap_handle(h, eid_map));
                cons[i]["refs"] = nr;
            }
    }
    return out;
}

// ops.py::build_feature_bundle — package feature `fid` (+ closure) into a portable bundle.
json build_feature_bundle(const json& doc, const std::string& fid, bool include_dependencies) {
    const json* feat = find_feature_c(doc, fid);
    if (!feat)
        throw OpError("invalid_op", "feature '" + fid + "' not found",
                      {{"reason", "unknown feature " + fid}});
    std::vector<std::string> order;
    if (include_dependencies)
        order = feature_refs_closure(doc, fid).first;
    else
        order = {fid};

    std::map<std::string, std::string> fid_to_ph;
    for (std::size_t i = 0; i < order.size(); ++i) fid_to_ph[order[i]] = "$" + std::to_string(i);

    std::set<std::string> external_refs;
    json features = json::object();
    for (const std::string& src_fid : order) {
        const json* f = find_feature_c(doc, src_fid);
        features[fid_to_ph[src_fid]] = stamp_bundle_feature(*f, fid_to_ph, external_refs);
    }

    json external_datums = json::object();
    for (const std::string& ext : external_refs) {
        const json* ef = find_feature_c(doc, ext);
        if (!ef) continue;
        const std::string t = ef->value("type", std::string());
        if (t == "plane" || t == "axis" || t == "point" || t == "csys") {
            const std::string nm = ef->value("name", std::string());
            external_datums[ext] = nm.empty() ? ext : nm;
        }
    }

    json feature_order = json::array();
    for (const std::string& src : order) feature_order.push_back(fid_to_ph[src]);
    json source_ids = json::object();
    for (const std::string& src : order) source_ids[fid_to_ph[src]] = src;
    json ext_sorted = json::array();
    for (const std::string& e : external_refs) ext_sorted.push_back(e);  // std::set is sorted

    return json{
        {"kind", "aicad.feature_bundle/1"},
        {"source_doc_id", doc.contains("id") ? doc["id"] : json(nullptr)},
        {"source_doc_name", doc.contains("name") ? doc["name"] : json(nullptr)},
        {"root", fid_to_ph[fid]},
        {"feature_order", feature_order},
        {"features", features},
        {"external_refs", ext_sorted},
        {"external_datums", external_datums},
        {"source_ids", source_ids},
    };
}

// ops.py::_new_feature_envelope — normalize a bundled feature into a full envelope.
json new_feature_envelope(const json& feature, const std::string& fid) {
    json out = feature;
    out["id"] = fid;
    if (!out.contains("name")) out["name"] = fid;
    if (!out.contains("suppressed")) out["suppressed"] = false;
    if (!out.contains("params")) out["params"] = json::object();
    if (!out.contains("refs")) out["refs"] = json::object();
    return out;
}

// ops.py::_target_datum_by_name — same-named target datum (plane/axis/point/csys) or reserved id.
std::optional<std::string> target_datum_by_name(const json& doc, const std::string& name) {
    if (is_reserved_datum(name)) return name;
    if (doc.contains("features") && doc["features"].is_array())
        for (const json& f : doc["features"]) {
            const std::string t = f.value("type", std::string());
            if ((t == "plane" || t == "axis" || t == "point" || t == "csys") &&
                f.value("name", std::string()) == name)
                return f.value("id", std::string());
        }
    return std::nullopt;
}

// ops.py::_resolve_datum — out-of-closure datum: same-named reuse, else schema-valid fallback.
json resolve_datum(const std::string& src_id, const json& external_datums, const json& doc,
                   const json& src_doc_id, const json& src_doc_name) {
    if (external_datums.contains(src_id) && external_datums[src_id].is_string()) {
        const std::string name = external_datums[src_id].get<std::string>();
        auto match = target_datum_by_name(doc, name);
        if (match) {
            if (is_reserved_datum(*match)) return json(*match);
            return json{{"kind", "datum"}, {"feature", *match}};
        }
    }
    return json{{"kind", "datum"}, {"feature", "plane.front"},
                {"external", {{"doc_id", src_doc_id}, {"doc_name", src_doc_name},
                              {"owner_feature", src_id}, {"unresolved", true}}}};
}

// ops.py::_resolve_paste_refs — re-link a pasted feature's refs against the TARGET doc.
json resolve_paste_refs(const json& refs, const std::map<std::string, std::string>& ph_to_fid,
                        const json& bundle, const json& doc) {
    const json src_doc_id = bundle.contains("source_doc_id") ? bundle["source_doc_id"] : json(nullptr);
    const json src_doc_name = bundle.contains("source_doc_name") ? bundle["source_doc_name"] : json(nullptr);
    const json external_datums = bundle.contains("external_datums") ? bundle["external_datums"] : json::object();
    auto ext_marker = [&](const json& owner) -> json {
        return json{{"doc_id", src_doc_id}, {"doc_name", src_doc_name}, {"owner_feature", owner}};
    };
    std::function<json(const json&)> walk = [&](const json& obj) -> json {
        if (obj.is_object()) {
            const std::string kind = obj.value("kind", std::string());
            if (kind == "face" || kind == "edge" || kind == "vertex" || kind == "body") {
                json out = obj;
                if (obj.contains("owner_feature") && obj["owner_feature"].is_string()) {
                    const std::string ow = obj["owner_feature"].get<std::string>();
                    auto it = ph_to_fid.find(ow);
                    if (it != ph_to_fid.end())
                        out["owner_feature"] = it->second;
                    else {
                        out["owner_feature"] = nullptr;
                        out["external"] = ext_marker(ow);
                    }
                }
                return out;
            }
            if (kind == "datum") {
                json out = obj;
                if (obj.contains("feature") && obj["feature"].is_string()) {
                    const std::string fe = obj["feature"].get<std::string>();
                    auto it = ph_to_fid.find(fe);
                    if (it != ph_to_fid.end()) {
                        out["feature"] = it->second;
                        return out;
                    }
                    return resolve_datum(fe, external_datums, doc, src_doc_id, src_doc_name);
                }
                return out;
            }
            if (kind == "region") {
                json out = obj;
                if (obj.contains("sketch") && obj["sketch"].is_string()) {
                    const std::string sk = obj["sketch"].get<std::string>();
                    auto it = ph_to_fid.find(sk);
                    if (it != ph_to_fid.end())
                        out["sketch"] = it->second;
                    else
                        out["external"] = ext_marker(sk);
                }
                return out;
            }
            json out = json::object();
            for (auto it = obj.begin(); it != obj.end(); ++it) out[it.key()] = walk(it.value());
            return out;
        }
        if (obj.is_array()) {
            bool all_str = !obj.empty();
            for (const json& v : obj)
                if (!v.is_string()) { all_str = false; break; }
            if (all_str) {
                json kept = json::array();
                for (const json& v : obj) {
                    auto it = ph_to_fid.find(v.get<std::string>());
                    if (it != ph_to_fid.end()) kept.push_back(it->second);
                }
                if (kept != obj) return kept;  // a feature-id list with external members dropped
            }
            json resolved = json::array();
            for (const json& v : obj) resolved.push_back(walk(v));
            return resolved;
        }
        if (obj.is_string()) {
            const std::string s = obj.get<std::string>();
            auto it = ph_to_fid.find(s);
            if (it != ph_to_fid.end()) return json(it->second);
            if (is_reserved_datum(s)) return obj;
            if (is_feature_id_token(s)) return resolve_datum(s, external_datums, doc, src_doc_id, src_doc_name);
        }
        return obj;
    };
    return walk(refs.is_null() ? json::object() : refs);
}

// ops.py::_reid_pasted_sketch — fresh e<N>/c<N> ids for a pasted sketch (handles re-pointed).
void reid_pasted_sketch(json& feat, IdGen& gen) {
    if (!feat.contains("params") || !feat["params"].is_object()) feat["params"] = json::object();
    json& params = feat["params"];
    std::map<std::string, std::string> eid_map;
    if (params.contains("entities") && params["entities"].is_array())
        for (json& ent : params["entities"])
            if (ent.is_object()) {
                const std::string neid = gen.next("e");
                if (ent.contains("id") && ent["id"].is_string())
                    eid_map[ent["id"].get<std::string>()] = neid;
                ent["id"] = neid;
            }
    if (params.contains("constraints") && params["constraints"].is_array())
        for (json& con : params["constraints"])
            if (con.is_object()) {
                con["id"] = gen.next("c");
                json nr = json::array();
                if (con.contains("refs") && con["refs"].is_array())
                    for (const json& h : con["refs"]) nr.push_back(remap_handle(h, eid_map));
                con["refs"] = nr;
            }
}

// ops.py::_append_feature — append honoring the rollback bar.
void append_feature(json& doc, const json& feat) {
    if (!doc.contains("features") || !doc["features"].is_array()) doc["features"] = json::array();
    json& feats = doc["features"];
    const std::string rb = doc.value("rollback_after", std::string());
    if (!rb.empty()) {
        int bar_pos = -1;
        for (std::size_t i = 0; i < feats.size(); ++i)
            if (feats[i].value("id", std::string()) == rb) { bar_pos = static_cast<int>(i); break; }
        if (bar_pos >= 0 && bar_pos < static_cast<int>(feats.size()) - 1) {
            feats.insert(feats.begin() + bar_pos + 1, feat);
            doc["rollback_after"] = feat["id"];
            return;
        }
    }
    feats.push_back(feat);
}

// ops.py::_paste_features — re-id every bundled feature, resolve refs, append. Returns
// (ph_to_fid, new_ids in dependency-first order). When `derived`, stamp derived_from.
std::pair<std::map<std::string, std::string>, std::vector<std::string>>
paste_features(json& doc, const json& bundle, IdGen& gen, bool derived,
               const std::string& group, const json& stamp_extra) {
    const json& order = bundle["feature_order"];
    const json& feats = bundle["features"];
    const json source_ids = bundle.contains("source_ids") ? bundle["source_ids"] : json::object();
    const std::string grp = group.empty() ? new_group_id() : group;

    std::map<std::string, std::string> ph_to_fid;
    for (const json& ph : order) ph_to_fid[ph.get<std::string>()] = gen.next("f");

    std::vector<std::string> new_ids;
    for (const json& phj : order) {
        const std::string ph = phj.get<std::string>();
        const json& src = feats[ph];
        const std::string new_fid = ph_to_fid[ph];
        json feat = new_feature_envelope(src, new_fid);
        feat["refs"] = resolve_paste_refs(src.contains("refs") ? src["refs"] : json::object(),
                                          ph_to_fid, bundle, doc);
        if (feat.value("type", std::string()) == "sketch") reid_pasted_sketch(feat, gen);
        if (derived) {
            json stamp = {
                {"doc_id", bundle.contains("source_doc_id") ? bundle["source_doc_id"] : json(nullptr)},
                {"doc_name", bundle.contains("source_doc_name") ? bundle["source_doc_name"] : json(nullptr)},
                {"feature", ph},
                {"source_feature", source_ids.contains(ph) ? source_ids[ph] : json(nullptr)},
                {"group", grp},
            };
            if (stamp_extra.is_object()) stamp.update(stamp_extra);
            feat["derived_from"] = stamp;
        }
        append_feature(doc, feat);
        new_ids.push_back(new_fid);
    }
    return {ph_to_fid, new_ids};
}

// ops.py::_bundle_or_invalid — validate the inbound bundle shape.
json bundle_or_invalid(const json& op, std::size_t index) {
    if (!op.contains("bundle") || !op["bundle"].is_object())
        throw OpError("invalid_op", "paste requires a 'bundle' object", {{"index", index}});
    const json& b = op["bundle"];
    if (b.value("kind", std::string()) != "aicad.feature_bundle/1")
        throw OpError("invalid_op", "bundle.kind must be 'aicad.feature_bundle/1'", {{"index", index}});
    if (!b.contains("feature_order") || !b["feature_order"].is_array() || b["feature_order"].empty())
        throw OpError("invalid_op", "bundle.feature_order must be a non-empty list", {{"index", index}});
    if (!b.contains("features") || !b["features"].is_object())
        throw OpError("invalid_op", "bundle.features must be an object", {{"index", index}});
    for (const json& ph : b["feature_order"]) {
        const std::string p = ph.get<std::string>();
        if (!b["features"].contains(p) || !b["features"][p].is_object())
            throw OpError("invalid_op", "bundle.features is missing feature for '" + p + "'",
                          {{"index", index}});
    }
    if (!b.contains("root") ||
        std::find(b["feature_order"].begin(), b["feature_order"].end(), b["root"]) ==
            b["feature_order"].end())
        throw OpError("invalid_op", "bundle.root must appear in feature_order", {{"index", index}});
    return b;
}

void op_copy_feature(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    (void)gen;
    const std::string fid = op.value("id", std::string());
    bool inc = true;
    if (op.contains("include_dependencies")) {
        if (!op["include_dependencies"].is_boolean())
            throw OpError("invalid_op", "copy_feature.include_dependencies must be a bool",
                          {{"index", idx}});
        inc = op["include_dependencies"].get<bool>();
    }
    if (find_feature_c(doc, fid) == nullptr)
        throw OpError("invalid_op", "copy_feature: '" + fid + "' not found", {{"index", idx}});
    json bundle = build_feature_bundle(doc, fid, inc);
    id_map["$bundle"] = bundle;
    id_map["$bundle:" + std::to_string(idx)] = bundle;
}

void op_paste_feature(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    const json bundle = bundle_or_invalid(op, idx);
    auto pr = paste_features(doc, bundle, gen, /*derived=*/false, "", json(nullptr));
    id_map["$" + std::to_string(idx)] = pr.first[bundle["root"].get<std::string>()];
    for (std::size_t k = 0; k < pr.second.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = pr.second[k];
}

void op_paste_feature_as_derived(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json bundle = bundle_or_invalid(op, idx);
    if (op.contains("source_doc_id") && !op["source_doc_id"].is_null() &&
        (!bundle.contains("source_doc_id") || bundle["source_doc_id"].is_null()))
        bundle["source_doc_id"] = op["source_doc_id"];
    auto pr = paste_features(doc, bundle, gen, /*derived=*/true, "", json(nullptr));
    id_map["$" + std::to_string(idx)] = pr.first[bundle["root"].get<std::string>()];
    for (std::size_t k = 0; k < pr.second.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = pr.second[k];
}

// ops.py::_shift_entity_any — deep-copy a sketch entity of ANY type shifted by (dx, dy)
// (id/construction stripped — the caller restamps them). Translates every point-shaped field.
json shift_entity_any(const json& src, double dx, double dy) {
    auto shift_pt = [&](const json& p) -> json {
        if (p.is_array() && p.size() == 2 && p[0].is_number() && p[1].is_number())
            return json::array({p[0].get<double>() + dx, p[1].get<double>() + dy});
        return p;
    };
    json out = json::object();
    for (auto it = src.begin(); it != src.end(); ++it)
        if (it.key() != "id" && it.key() != "construction") out[it.key()] = it.value();
    for (const char* key : {"p", "p1", "p2", "center", "vertex", "focus", "apex", "p0"})
        if (out.contains(key)) out[key] = shift_pt(out[key]);
    if (out.contains("points") && out["points"].is_array()) {
        json pts = json::array();
        for (const json& p : out["points"]) pts.push_back(shift_pt(p));
        out["points"] = pts;
    }
    return out;
}

// ops.py::_op_sketch_paste_entities (Wave 4B) — paste a bundle of sketch entities (+ their
// constraints) into `sketch`, shifted by (offset_x, offset_y), with FRESH ids. Constraints
// internal to the pasted set are kept (handles re-pointed); constraints reaching OUTSIDE the
// set are DROPPED. Records the first new entity id under $<index> and the rest under $<index>#k.
void op_sketch_paste_entities(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    json& sk = require_sketch_feature(doc, op, id_map);
    if (!op.contains("entities_bundle") || !op["entities_bundle"].is_object())
        throw OpError("invalid_op", "sketch_paste_entities requires an 'entities_bundle' object",
                      {{"index", idx}});
    const json& eb = op["entities_bundle"];
    if (!eb.contains("entities") || !eb["entities"].is_array() || eb["entities"].empty())
        throw OpError("invalid_op", "entities_bundle.entities must be a non-empty list",
                      {{"index", idx}});
    const json src_cons = eb.contains("constraints") ? eb["constraints"] : json::array();
    if (!src_cons.is_array())
        throw OpError("invalid_op", "entities_bundle.constraints must be a list", {{"index", idx}});
    if (!op.contains("offset_x") || !op["offset_x"].is_number())
        throw OpError("invalid_op", "sketch_paste_entities requires a numeric 'offset_x'",
                      {{"index", idx}});
    if (!op.contains("offset_y") || !op["offset_y"].is_number())
        throw OpError("invalid_op", "sketch_paste_entities requires a numeric 'offset_y'",
                      {{"index", idx}});
    const double dx = op["offset_x"].get<double>(), dy = op["offset_y"].get<double>();

    std::map<std::string, std::string> eid_map;
    std::vector<json> new_entities;
    for (const json& src : eb["entities"]) {
        if (!src.is_object() || !src.contains("id") || !src["id"].is_string())
            throw OpError("invalid_op", "entities_bundle.entities: each entity needs an 'id'",
                          {{"index", idx}});
        const std::string neid = gen.next("e");
        eid_map[src["id"].get<std::string>()] = neid;
        json geom = shift_entity_any(src, dx, dy);
        geom["id"] = neid;
        geom["construction"] = src.value("construction", false);
        new_entities.push_back(geom);
    }
    for (const json& ne : new_entities) sk["params"]["entities"].push_back(ne);

    // Keep only constraints whose handles ALL land inside the pasted set (drop external/origin).
    json& cons = sk["params"]["constraints"];
    for (const json& con : src_cons) {
        if (!con.is_object()) continue;
        const json handles = con.contains("refs") ? con["refs"] : json::array();
        bool all_in = handles.is_array() && !handles.empty();
        if (all_in)
            for (const json& h : handles)
                if (!(h.is_string() && eid_map.count(handle_base(h.get<std::string>())))) {
                    all_in = false;
                    break;
                }
        if (!all_in) continue;
        json nc = con;
        nc["id"] = gen.next("c");
        json nr = json::array();
        for (const json& h : handles) nr.push_back(remap_handle(h, eid_map));
        nc["refs"] = nr;
        cons.push_back(nc);
    }

    std::vector<std::string> new_ids;
    for (const json& ne : new_entities) new_ids.push_back(ne["id"].get<std::string>());
    id_map["$" + std::to_string(idx)] = new_ids.empty() ? json("") : json(new_ids[0]);
    for (std::size_t k = 1; k < new_ids.size(); ++k)
        id_map["$" + std::to_string(idx) + "#" + std::to_string(k)] = new_ids[k];
}

// ops.py::refresh_derived_group — splice a FRESH source bundle over an existing derived
// group IN PLACE (ids + tree positions preserved). Returns {refreshed, added, stale}.
json refresh_derived_group(json& doc, const std::string& group, const json& bundle, IdGen& gen) {
    const json& order = bundle["feature_order"];
    const json& feats = bundle["features"];
    const json source_ids = bundle.contains("source_ids") ? bundle["source_ids"] : json::object();
    const json grp_doc_id = bundle.contains("source_doc_id") ? bundle["source_doc_id"] : json(nullptr);
    const json grp_doc_name = bundle.contains("source_doc_name") ? bundle["source_doc_name"] : json(nullptr);

    std::vector<std::string> member_ids;
    std::map<std::string, std::string> by_source;  // source_feature -> existing target id
    if (doc.contains("features") && doc["features"].is_array())
        for (const json& f : doc["features"]) {
            if (!f.is_object()) continue;
            if (!f.contains("derived_from") || !f["derived_from"].is_object()) continue;
            const json& df = f["derived_from"];
            if (df.value("group", std::string()) != group) continue;
            const std::string fid = f.value("id", std::string());
            member_ids.push_back(fid);
            if (df.contains("source_feature") && df["source_feature"].is_string()) {
                const std::string sf = df["source_feature"].get<std::string>();
                if (!by_source.count(sf)) by_source[sf] = fid;
            }
        }

    std::map<std::string, std::string> ph_to_fid;
    std::map<std::string, bool> ph_is_new;
    for (const json& phj : order) {
        const std::string ph = phj.get<std::string>();
        const std::string src_fid =
            (source_ids.contains(ph) && source_ids[ph].is_string()) ? source_ids[ph].get<std::string>() : "";
        auto it = (!src_fid.empty()) ? by_source.find(src_fid) : by_source.end();
        if (it != by_source.end()) {
            ph_to_fid[ph] = it->second;
            ph_is_new[ph] = false;
        } else {
            ph_to_fid[ph] = gen.next("f");
            ph_is_new[ph] = true;
        }
    }

    json refreshed = json::array(), added = json::array();
    for (const json& phj : order) {
        const std::string ph = phj.get<std::string>();
        const json& src = feats[ph];
        const std::string target_fid = ph_to_fid[ph];
        json stamp = {
            {"doc_id", grp_doc_id}, {"doc_name", grp_doc_name}, {"feature", ph},
            {"source_feature", source_ids.contains(ph) ? source_ids[ph] : json(nullptr)},
            {"group", group},
        };
        if (ph_is_new[ph]) {
            json feat = new_feature_envelope(src, target_fid);
            feat["refs"] = resolve_paste_refs(src.contains("refs") ? src["refs"] : json::object(),
                                              ph_to_fid, bundle, doc);
            if (feat.value("type", std::string()) == "sketch") reid_pasted_sketch(feat, gen);
            feat["derived_from"] = stamp;
            append_feature(doc, feat);
            added.push_back(target_fid);
        } else {
            json* existing = find_feature(doc, target_fid);
            json rebuilt = new_feature_envelope(src, target_fid);
            rebuilt["refs"] = resolve_paste_refs(src.contains("refs") ? src["refs"] : json::object(),
                                                 ph_to_fid, bundle, doc);
            if (rebuilt.value("type", std::string()) == "sketch") reid_pasted_sketch(rebuilt, gen);
            rebuilt["name"] = existing->value("name", rebuilt.value("name", std::string()));
            *existing = rebuilt;
            (*existing)["derived_from"] = stamp;
            existing->erase("derived_stale");
            refreshed.push_back(target_fid);
        }
    }

    std::set<std::string> fresh_sources;
    for (const json& phj : order) {
        const std::string ph = phj.get<std::string>();
        if (source_ids.contains(ph) && source_ids[ph].is_string())
            fresh_sources.insert(source_ids[ph].get<std::string>());
    }
    std::set<std::string> refreshed_set;
    for (const json& r : refreshed) refreshed_set.insert(r.get<std::string>());
    json stale = json::array();
    for (const std::string& mid : member_ids) {
        if (refreshed_set.count(mid)) continue;
        json* f = find_feature(doc, mid);
        if (!f) continue;
        std::string sf;
        if (f->contains("derived_from") && (*f)["derived_from"].is_object() &&
            (*f)["derived_from"].contains("source_feature") &&
            (*f)["derived_from"]["source_feature"].is_string())
            sf = (*f)["derived_from"]["source_feature"].get<std::string>();
        if (!fresh_sources.count(sf)) {
            (*f)["derived_stale"] = {{"reason", "source_feature_deleted"},
                                     {"source_feature", sf.empty() ? json(nullptr) : json(sf)},
                                     {"doc_id", grp_doc_id}, {"doc_name", grp_doc_name}};
            stale.push_back(mid);
        }
    }
    return json{{"refreshed", refreshed}, {"added", added}, {"stale", stale}};
}

// session.py::_resolve_source_session — find the live source session for a derived link's
// source_doc_id, matching BOTH the registry key (session.doc_id) and the doc's internal id.
// Caller holds a DocLock. Acquires the source doc via multi-lock (sorted) so the returned
// pointer is safe for the rest of the RAII scope. Returns nullptr if closed/absent.
Session* resolve_source_session(const json& src_doc_id) {
    if (src_doc_id.is_null()) return nullptr;
    std::string key;
    if (src_doc_id.is_string()) {
        key = src_doc_id.get<std::string>();
        if (pin_session(key)) {
            if (!t_current_doc_lock) return nullptr;
            try {
                return &t_current_doc_lock->acquire(key);
            } catch (...) {
                return nullptr;
            }
        }
    }
    // Match against each open session's internal doc["id"] (may differ from map key).
    std::vector<std::string> ids;
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        for (const auto& kv : g_sessions) ids.push_back(kv.first);
    }
    for (const std::string& id : ids) {
        if (!t_current_doc_lock) return nullptr;
        try {
            Session& s = t_current_doc_lock->acquire(id);
            if (s.doc.is_object() &&
                (s.doc.contains("id") ? s.doc["id"] : json(nullptr)) == src_doc_id)
                return &s;
        } catch (...) {
            continue;
        }
    }
    return nullptr;
}

// session.py::_group_root_source — the source feature to re-bundle a group from: the member
// whose closure (transitively) covers the most of the group's source features.
std::string group_root_source(const json& rec, const json& source_doc) {
    std::vector<std::string> src_ids;
    for (const json& m : rec["members"])
        if (m.contains("source_feature") && m["source_feature"].is_string())
            src_ids.push_back(m["source_feature"].get<std::string>());
    if (src_ids.empty()) return "";
    std::set<std::string> src_set(src_ids.begin(), src_ids.end());
    std::string best;
    std::pair<int, int> best_key{-1, -1};
    for (const std::string& cand : src_ids) {
        if (find_feature_c(source_doc, cand) == nullptr) continue;
        std::set<std::string> closure;
        try {
            closure = feature_refs_closure(source_doc, cand).second;
        } catch (...) {
            continue;
        }
        int inter = 0;
        for (const std::string& x : src_set)
            if (closure.count(x)) ++inter;
        std::pair<int, int> key{inter, static_cast<int>(closure.size())};
        if (key > best_key) {
            best_key = key;
            best = cand;
        }
    }
    return best;
}

// session.py::_mark_group_stale — stamp derived_stale on every member of `group`.
json mark_group_stale(Session& s, const std::string& group, const std::string& reason,
                      const json& detail) {
    json marked = json::array();
    if (!s.doc.contains("features") || !s.doc["features"].is_array()) return marked;
    for (json& f : s.doc["features"]) {
        if (!f.is_object()) continue;
        if (!f.contains("derived_from") || !f["derived_from"].is_object()) continue;
        if (f["derived_from"].value("group", std::string()) != group) continue;
        json st = {{"reason", reason}};
        if (detail.is_object()) st.update(detail);
        f["derived_stale"] = st;
        marked.push_back(f.contains("id") ? f["id"] : json(nullptr));
    }
    return marked;
}

void apply_one(json& doc, const json& op, std::size_t idx, IdGen& gen, json& id_map) {
    if (!op.is_object() || !op.contains("op") || !op["op"].is_string())
        throw OpError("unsupported", "op[" + std::to_string(idx) + "]: missing 'op'",
                      {{"index", idx}});
    const std::string k = op["op"].get<std::string>();

    if (k == "add_feature")              op_add_feature(doc, op, idx, gen, id_map);
    else if (k == "edit_feature")        op_edit_feature(doc, op, id_map);
    else if (k == "delete_feature")      op_delete_feature(doc, op);
    else if (k == "reorder_feature")     op_reorder_feature(doc, op);
    else if (k == "set_suppressed") {
        const std::string id = op.value("id", std::string());
        if (doc.value("schema", std::string()) == "aicad.asm/1") {
            bool found = false;
            for (json& c : doc["components"])
                if (c.value("id", std::string()) == id) {
                    c["suppressed"] = op.value("value", false);
                    found = true;
                    break;
                }
            if (!found) throw OpError("invalid_op", "set_suppressed: component '" + id +
                "' not found in assembly; feature ids require a part document");
        } else {
            json* f = find_feature(doc, id);
            if (!f) throw OpError("invalid_op", "set_suppressed: feature '" + id +
                "' not found in part; component ids require an assembly document");
            (*f)["suppressed"] = op.value("value", false);
        }
    } else if (k == "set_folder") {
        // FeatureManager folder grouping ("Add Folder" / "Name & group" / "Remove from folder").
        // `folder` is a plain display-only tag on the feature (never affects geometry or rebuild);
        // an empty/absent value clears it. Kept alongside the feature so it round-trips on save.
        json* f = find_feature(doc, op.value("id", std::string()));
        if (!f) throw OpError("invalid_op", "set_folder: no feature '" +
                              op.value("id", std::string()) + "'");
        const std::string folder = op.value("folder", std::string());
        if (folder.empty()) f->erase("folder");
        else (*f)["folder"] = folder;
    } else if (k == "rename") {
        json* f = find_feature(doc, op.value("id", std::string()));
        if (!f) throw OpError("invalid_op", "rename: no feature");
        if (op.contains("name") && op["name"].is_string()) (*f)["name"] = op["name"];
    } else if (k == "set_units") {
        if (op.contains("system")) doc["units"]["system"] = op["system"];
    } else if (k == "set_material") {
        doc["material"] = op.contains("key") && !op["key"].is_null()
                              ? json{{"key", op["key"]}} : json(nullptr);
    } else if (k == "set_appearance") {
        // C26 body colour + C32-A RealView material (doc_ops.md). `id` is a body key ("b<ordinal>"
        // for a part body, component id for assembly). A null/absent `color` clears the body back to
        // the default; a colour with metallic/roughness stores the {color,metallic,roughness} form,
        // colour alone stores a bare [r,g,b]. The batch bumps the revision, so the AICM re-bakes.
        const std::string id = op.value("id", std::string());
        if (id.empty()) throw OpError("invalid_op", "set_appearance: missing body id");
        if (!doc.contains("appearances") || !doc["appearances"].is_object())
            doc["appearances"] = json::object();
        json& ap = doc["appearances"];
        const bool color_null = op.contains("color") && op["color"].is_null();
        const bool has_color = op.contains("color") && op["color"].is_array() &&
                               op["color"].size() >= 3;
        auto c01 = [](double x) { return x < 0.0 ? 0.0 : (x > 1.0 ? 1.0 : x); };
        if (color_null) {
            ap.erase(id);  // clear to default
        } else if (has_color) {
            json col = json::array();
            for (int i = 0; i < 3; ++i) col.push_back(c01(op["color"][i].get<double>()));
            const bool m = op.contains("metallic") && op["metallic"].is_number();
            const bool r = op.contains("roughness") && op["roughness"].is_number();
            if (m || r) {
                json val = json{{"color", col}};
                if (m) val["metallic"] = c01(op["metallic"].get<double>());
                if (r) val["roughness"] = c01(op["roughness"].get<double>());
                ap[id] = val;
            } else {
                ap[id] = col;  // bare [r,g,b]
            }
        } else if (op.contains("metallic") || op.contains("roughness")) {
            // Finish-only tweak on an existing entry: keep its colour, update the material.
            json val = ap.contains(id) && ap[id].is_object()
                           ? ap[id]
                           : json{{"color", ap.contains(id) && ap[id].is_array()
                                                ? ap[id]
                                                : json::array({0.71, 0.76, 0.82})}};
            if (op.contains("metallic") && op["metallic"].is_number())
                val["metallic"] = c01(op["metallic"].get<double>());
            if (op.contains("roughness") && op["roughness"].is_number())
                val["roughness"] = c01(op["roughness"].get<double>());
            ap[id] = val;
        }
        if (ap.empty()) doc.erase("appearances");
    } else if (k == "set_rollback") {
        if (op.contains("after") && !op["after"].is_null()) doc["rollback_after"] = op["after"];
        else doc.erase("rollback_after");
    } else if (k == "eq_set") {
        json& globals = doc["equations"]["globals"];
        const std::string name = op.value("name", std::string());
        bool replaced = false;
        for (json& g : globals)
            if (g.value("name", std::string()) == name) { g["expr"] = op.value("expr", std::string()); replaced = true; break; }
        if (!replaced) globals.push_back({{"name", name}, {"expr", op.value("expr", std::string())}});
    } else if (k == "eq_delete") {
        json& globals = doc["equations"]["globals"];
        json kept = json::array();
        for (const json& g : globals)
            if (g.value("name", std::string()) != op.value("name", std::string())) kept.push_back(g);
        globals = kept;
    } else if (k == "sketch_add_entity")        op_sketch_add_entity(doc, op, idx, gen, id_map);
    else if (k == "sketch_add_constraint")      op_sketch_add_constraint(doc, op, idx, gen, id_map);
    else if (k == "sketch_edit_entity")         op_sketch_edit_entity(doc, op, id_map);
    else if (k == "sketch_delete_entity")       op_sketch_delete_entity(doc, op, id_map);
    else if (k == "sketch_delete_constraint")   op_sketch_delete_constraint(doc, op, id_map);
    else if (k == "sketch_set_dim")             op_sketch_set_dim(doc, op, id_map);
    else if (k == "sketch_toggle_construction") op_sketch_toggle_construction(doc, op, id_map);
    else if (k == "sketch_convert")             op_sketch_convert(doc, op, idx, gen, id_map);
    else if (k == "sketch_break_external_ref")  op_sketch_break_external_ref(doc, op, id_map);
    else if (k == "sketch_trim")                op_sketch_trim(doc, op, idx, gen, id_map);
    else if (k == "sketch_extend")              op_sketch_extend(doc, op, idx, gen, id_map);
    else if (k == "sketch_offset")              op_sketch_offset(doc, op, idx, gen, id_map);
    else if (k == "sketch_fillet")              op_sketch_fillet(doc, op, idx, gen, id_map);
    else if (k == "sketch_chamfer")             op_sketch_chamfer(doc, op, idx, gen, id_map);
    else if (k == "sketch_mirror_entities")     op_sketch_mirror_entities(doc, op, idx, gen, id_map);
    else if (k == "sketch_pattern_entities")    op_sketch_pattern_entities(doc, op, idx, gen, id_map);
    else if (k == "sketch_move")                op_sketch_move(doc, op, idx, gen, id_map);
    else if (k == "sketch_rotate")              op_sketch_rotate(doc, op, idx, gen, id_map);
    else if (k.rfind("asm_", 0) == 0 &&
             doc.value("schema", std::string()) != "aicad.asm/1")
        // G4 guard — asm_* ops on a PART document used to "succeed" by writing a
        // components[]/mates[] array the part compiler never reads: revision bumped,
        // geometry unchanged, no error. Reject loudly instead so the caller learns the
        // doc kind is wrong (create the doc with doc_new kind="assembly").
        throw OpError("invalid_op",
                      "op[" + std::to_string(idx) + "]: '" + k +
                          "' requires an ASSEMBLY document (aicad.asm/1); this is a part. "
                          "Create one with doc_new kind=\"assembly\" (MCP: new_assembly).",
                      {{"index", idx}, {"reason", "not an assembly document"}});
    else if (k == "asm_insert_component")       op_asm_insert_component(doc, op, idx, gen, id_map);
    else if (k == "asm_edit_component")         op_asm_edit_component(doc, op, idx);
    else if (k == "asm_delete_component")       op_asm_delete_component(doc, op, idx);
    else if (k == "asm_add_mate")               op_asm_add_mate(doc, op, idx, gen, id_map);
    else if (k == "asm_edit_mate")              op_asm_edit_mate(doc, op, idx);
    else if (k == "asm_delete_mate")            op_asm_delete_mate(doc, op, idx);
    else if (k == "asm_set_explode")            op_asm_set_explode(doc, op, idx);
    else if (k == "copy_feature")               op_copy_feature(doc, op, idx, gen, id_map);
    else if (k == "paste_feature")              op_paste_feature(doc, op, idx, gen, id_map);
    else if (k == "paste_feature_as_derived")   op_paste_feature_as_derived(doc, op, idx, gen, id_map);
    else if (k == "sketch_paste_entities")       op_sketch_paste_entities(doc, op, idx, gen, id_map);
    else
        throw OpError("unsupported", unknown_doc_op_message(idx, k),
                      {{"index", idx}, {"reason", "not ported"},
                       {"suggestions", suggest_doc_ops(k, 5)}});
}

// ----------------------------------------------------------------- rebuild + response
// Evaluate the document's global equations into an env map (name -> value), in
// listed order (a later global may reference an earlier one). Bad exprs are skipped.
json env_from_doc(const json& doc) {
    json env = json::object();
    if (doc.contains("equations") && doc["equations"].is_object() &&
        doc["equations"].contains("globals") && doc["equations"]["globals"].is_array()) {
        std::map<std::string, double> acc;
        for (const json& g : doc["equations"]["globals"]) {
            const std::string name = g.value("name", std::string());
            const std::string expr = g.value("expr", std::string());
            if (name.empty()) continue;
            try {
                double v = chudware::util::evaluate(expr, acc);
                acc[name] = v;
                env[name] = v;
            } catch (...) { /* skip a bad global */ }
        }
    }
    return env;
}

// Compile the session's LIVE part doc, reusing the cached result when the doc is unchanged
// since the last compile. The cache is keyed on `revision`: every geometry-changing op bumps
// it (or starts a fresh session), and all callers hold the doc lock, so the cache is never
// stale. This turns repeated read-only queries on a static doc (clicking faces, expanding a
// face into its edges, refreshing the owner map) from "recompile the whole feature tree each
// time" into a single compile amortized across the whole interaction. Part docs only — the
// assembly path compiles via rebuild_assembly / asm_result, not compile_document.
// Compile the session's live doc THROUGH its incremental-rebuild cache: unchanged
// feature prefixes restore from checkpoints instead of recompiling (document.h
// CompileCache). Every "compile s.doc now" callsite in this file funnels through
// here so the whole RPC surface — edits, undo/redo, rebuilds, exports, measures —
// benefits uniformly. Caller must hold the doc lock (they all do).
// Kill-switch: CHUDWARE_INC_REBUILD=0 forces the old always-full-rebuild path
// (safety valve while the cache is young; also the A/B lever for benchmarks).
// Shared by compile_session and the external-ref refresh compiles (apply_ops_locked /
// doc_rebuild), so ONE env var disables every cached compile path uniformly.
bool inc_rebuild_enabled() {
    static const bool enabled = []() {
        const char* v = std::getenv("CHUDWARE_INC_REBUILD");
        return !(v && v[0] == '0' && v[1] == '\0');
    }();
    return enabled;
}

CompileResult compile_session(Session& s) {
    return compile_document(s.doc, env_from_doc(s.doc),
                            inc_rebuild_enabled() ? s.compile_cache.get() : nullptr,
                            s.display_quality);
}

const CompileResult& ensure_compiled(Session& s) {
    if (!s.compiled || s.compiled_rev != s.revision) {
        s.compiled = std::make_shared<CompileResult>(compile_session(s));
        s.compiled_rev = s.revision;
    }
    return *s.compiled;
}

json ops_response(Session& s, const CompileResult& r, const json& id_map, bool with_tess);

// Build the ops-response AND warm the read-only compile cache from the SAME compile, so the
// first pick right after an open/edit/undo/rebuild reuses it instead of recompiling the whole
// tree (the post-open first-click hitch). `r` is taken by value and MOVED into the cache after
// ops_response reads it — no extra copy of the aicm/shapes. Call this ONLY where `r` was just
// compiled from the CURRENT live s.doc at the CURRENT s.revision (never the apply-batch rollback
// path, where r is the rejected tree and the restored doc's cache is already valid).
json warm_and_respond(Session& s, CompileResult r, const json& id_map, bool with_tess) {
    json resp = ops_response(s, r, id_map, with_tess);
    s.compiled = std::make_shared<CompileResult>(std::move(r));
    s.compiled_rev = s.revision;
    // Cross-doc dependency notify (feat(xdoc)): stamp dependents stale when this doc
    // rebuilds. Called unlocked-variant since DocLock is already held by the caller.
    dep_notify_unlocked(s.doc_id, s.revision);
    return resp;
}

// Legacy filesystem path (fallback only — prefer aicm_handoff::publish).
std::filesystem::path aicm_file(const std::string& doc_id, long revision) {
    std::filesystem::path dir = std::filesystem::temp_directory_path() / "chudware_cpp_docs";
    std::error_code ec;
    std::filesystem::create_directories(dir, ec);
    return dir / (doc_id + "_r" + std::to_string(revision) + ".aicm");
}

// T-PERF10: publish tess AICM via shared memory (aicm-shm://…) so the host can
// map it without a temp-file round-trip. Falls back to aicm_file() path on shm failure.
std::string publish_doc_aicm(const std::string& doc_id, long revision,
                             const std::vector<uint8_t>& aicm) {
    if (aicm.empty()) return {};
    const std::string key = chudware::aicm_handoff::key_for(doc_id, revision);
    std::string uri = chudware::aicm_handoff::publish(key, aicm);
    if (uri.empty()) {
        std::filesystem::path target = aicm_file(doc_id, revision);
        std::ofstream f(target, std::ios::binary | std::ios::trunc);
        if (f) {
            f.write(reinterpret_cast<const char*>(aicm.data()),
                    static_cast<std::streamsize>(aicm.size()));
            f.close();
            uri = std::filesystem::absolute(target).string();
        }
    }
    // Drop old revisions so shm objects don't accumulate. Keep a few recent ones:
    // the host's async mesh loader (T-PERF3) may still be mapping the previous path
    // while this rebuild publishes the next.
    if (revision > 3)
        chudware::aicm_handoff::release_older_than(doc_id, revision - 3);
    return uri;
}

json ops_response(Session& s, const CompileResult& r, const json& id_map, bool with_tess) {
    json fs = json::array();
    for (const FeatureStatus& st : r.statuses) {
        fs.push_back({{"id", st.id},
                      {"ok", st.ok},
                      {"ms", st.ms},
                      {"reused", st.reused},  // served from the incremental-rebuild cache
                      {"error", st.ok ? json(nullptr) : json(st.error)},
                      {"warning", st.warning.empty() ? json(nullptr) : json(st.warning)},
                      // Structured result record (heal's before/after verdict + volumes);
                      // null for features that measure nothing.
                      {"data", st.data},
                      {"dangling_refs", json::array()},
                      {"rolled_back", false}});
    }
    json resp;
    resp["revision"] = s.revision;
    resp["id_map"] = id_map;
    resp["tree"] = s.doc;
    resp["owner_map"] = r.owner_map.is_null() ? json::object() : r.owner_map;
    // Displayable reference geometry (datum axes/planes/points/csys) — empty for docs with no
    // datum features. The host draws these as overlays; without it a created datum is invisible.
    resp["datums"] = r.datums;
    if (with_tess) {
        json active_sketch_status = nullptr;
        if (s.active_sketch && !s.active_sketch->last_overall_status.empty())
            active_sketch_status = s.active_sketch->last_overall_status;
        resp["rebuild"] = {{"feature_status", fs},
                           {"doc_status", {{"active_sketch", active_sketch_status},
                                           {"mates", nullptr},
                                           {"active_configuration", r.active_configuration},
                                           // incremental-rebuild accounting (CompileCache)
                                           {"features_reused", r.features_reused},
                                           {"features_executed", r.features_executed},
                                           // compile phase split: what an edit paid for
                                           {"phase_ms", {{"features", r.loop_ms},
                                                         {"tessellation", r.tess_ms},
                                                         {"tess_mesh", r.tess_mesh_ms},
                                                         {"tess_extract", r.tess_extract_ms},
                                                         {"tess_write", r.tess_write_ms},
                                                         {"owner_map", r.owner_ms}}}}}};
        std::string aicm_path;
        if (!r.aicm.empty())
            aicm_path = publish_doc_aicm(s.doc_id, s.revision, r.aicm);
        json bodies = json::array();
        for (int i = 0; i < r.body_count; ++i) {
            const std::string key = "b" + std::to_string(i + 1);
            // Report the per-body appearance (set_appearance) so the host's body list / swatch agree
            // with the baked AICM, instead of always showing the default grey dielectric.
            float bcolor[3] = {0.71f, 0.76f, 0.82f};
            float bmetal = 0.0f, brough = 0.5f;
            apply_doc_appearance(s.doc, (uint32_t)(i + 1), bcolor, bmetal, brough);
            bodies.push_back({{"body_id", i + 1}, {"name", key},
                              {"color", {bcolor[0], bcolor[1], bcolor[2]}},
                              {"owner_key", key}, {"metallic", bmetal}, {"roughness", brough},
                              {"kind", "solid"}});
        }
        resp["tess"] = {{"aicm_path", aicm_path}, {"bodies", bodies}};
        resp["volume"] = r.volume;  // C++ convenience (geometric parity signal)
    }
    return resp;
}
// Bound an undo/redo history stack (oldest at front) by BOTH count and cumulative
// bytes. Always keeps the newest entry even if it alone exceeds the byte budget —
// dropping the just-captured state would corrupt undo/redo.
// Byte-budget pressure spills oldest full-JSON entries to disk as {"@file":path}
// stubs (hostd_undo/<instance>/<doc_id>/<seq>.json) so depth stays up to kUndoLimit
// without pinning multi-GB of RSS. Count overflow permanently drops the oldest
// entry (and deletes its journal file if it was a stub).
void trim_history(const std::string& doc_id, std::vector<std::string>& stack) {
    while (stack.size() > kUndoLimit) {
        drop_history_entry(stack.front());
        stack.erase(stack.begin());
    }
    // Byte accounting: stubs are small; full dumps count their true size. When
    // over budget, spill the oldest still-resident full entry (front-most non-stub
    // that is not the newest) into a disk stub rather than discarding it.
    auto stack_bytes = [&]() {
        std::size_t total = 0;
        for (const std::string& e : stack) total += e.size();
        return total;
    };
    while (stack.size() > 1 && stack_bytes() > kUndoByteBudget) {
        // Find the oldest full (non-stub) entry; spill it. If every older entry is
        // already a stub and we are still over budget (pathological: newest alone
        // exceeds budget), stop — we always keep the newest entry intact.
        // On I/O failure (spill returns the original full entry) break rather than
        // spin forever with stack_bytes never dropping.
        bool progressed = false;
        for (std::size_t i = 0; i + 1 < stack.size(); ++i) {
            if (is_undo_stub(stack[i])) continue;
            std::string spilled = spill_history_entry(doc_id, stack[i]);
            if (!is_undo_stub(spilled)) {
                // Disk write failed — cannot reduce RAM; abandon further spill.
                return;
            }
            stack[i] = std::move(spilled);
            progressed = true;
            break;
        }
        if (!progressed) break;
    }
}

void push_undo_document(Session& s, const json& pre_edit_doc) {
    s.undo_stack.push_back(pre_edit_doc.dump());
    trim_history(s.doc_id, s.undo_stack);
    // Clearing redo drops any journal files those entries pointed at.
    for (const std::string& e : s.redo_stack) drop_history_entry(e);
    s.redo_stack.clear();
}

void push_undo(Session& s) {
    push_undo_document(s, s.doc);
}

std::string config_active_name(const json& doc);  // defined below (configurations section)

// ----------------------------------------------------------------- assembly rebuild
// Solve the assembly's mates, write the solved transforms back into the doc (so a save
// captures the mated pose), and cache the AssemblyResult on the session (port of
// session.py::_rebuild_assembly). base_dir resolves relative component source paths.
void rebuild_assembly(Session& s) {
    std::string base_dir;
    if (!s.path.empty()) base_dir = std::filesystem::path(s.path).parent_path().string();
    std::optional<chudware::mate::WarmMap> warm;
    if (!s.asm_warm.empty()) warm = s.asm_warm;
    AssemblyResult ares = compile_assembly(s.doc, base_dir, warm);
    // Persist solved transforms back into the document (matched by component id).
    if (s.doc.contains("components") && s.doc["components"].is_array())
        for (json& comp : s.doc["components"]) {
            const std::string cid = comp.value("id", std::string());
            auto it = ares.transforms.find(cid);
            if (it != ares.transforms.end()) comp["transform"] = it->second;
        }
    s.asm_warm = ares.warm;
    s.asm_result = std::make_shared<AssemblyResult>(std::move(ares));
}

// Is `cid` bound by any mate the solve will actually see? Mirrors compile_assembly's
// enumeration: non-suppressed mates only, and only the first two refs (the solver reads
// no more than two per mate).
bool component_is_mated(const json& doc, const std::string& cid) {
    if (!doc.contains("mates") || !doc["mates"].is_array()) return false;
    for (const json& mate : doc["mates"]) {
        if (!mate.is_object() || mate.value("suppressed", false)) continue;
        if (!mate.contains("refs") || !mate["refs"].is_array()) continue;
        const json& refs = mate["refs"];
        for (std::size_t i = 0; i < refs.size() && i < 2; ++i)
            if (refs[i].is_object() && refs[i].value("component", std::string()) == cid)
                return true;
    }
    return false;
}

bool component_is_fixed(const json& doc, const std::string& cid) {
    if (!doc.contains("components") || !doc["components"].is_array()) return false;
    for (const json& c : doc["components"])
        if (c.is_object() && c.value("id", std::string()) == cid) return c.value("fixed", false);
    return false;
}

// A grounded component's authored motion carries each floating mate island attached
// only to it. Transport the WARM POSES, not constraints: a wheel keeps its free spin
// and axial station while its hub steers, and the solver still checks every mate.
// Islands spanning multiple anchors are linkages, not rigidly carried children.
json transport_carried_poses(Session& s, const json& after, const json& ops) {
    json carried = json::object();
    if (!s.is_assembly() || s.asm_warm.empty()) return carried;
    std::map<std::string, Eigen::Matrix4d> anchors;
    auto matrix = [](const json& c) {
        Eigen::Matrix4d m = Eigen::Matrix4d::Identity();
        if (c.contains("transform") && c["transform"].is_array() &&
            c["transform"].size() == 16)
            for (int r = 0; r < 4; ++r)
                for (int k = 0; k < 4; ++k) m(r, k) = c["transform"][r * 4 + k];
        return m;
    };
    for (const json& op : ops) {
        if (op.value("op", std::string()) != "asm_edit_component" ||
            !op.contains("transform")) continue;
        const std::string id = op.value("id", std::string());
        if (!component_is_fixed(after, id)) continue;
        for (const json& old : s.doc["components"])
            if (old.value("id", std::string()) == id)
                for (const json& now : after["components"])
                    if (now.value("id", std::string()) == id)
                        anchors[id] = matrix(now) * matrix(old).inverse();
    }
    if (anchors.empty()) return carried;
    std::map<std::string, std::set<std::string>> graph;
    for (const json& m : after.value("mates", json::array())) {
        if (m.value("suppressed", false)) continue;
        std::vector<std::string> refs;
        for (const json& ref : m.value("refs", json::array()))
            refs.push_back(ref.value("component", std::string()));
        for (const auto& a : refs)
            for (const auto& b : refs)
                if (a != b) graph[a].insert(b);
    }
    std::set<std::string> seen;
    for (const json& c : after["components"]) {
        const std::string id = c.value("id", std::string());
        if (c.value("suppressed", false) || c.value("fixed", false) || seen.count(id))
            continue;
        std::vector<std::string> island{id};
        std::set<std::string> boundary;
        seen.insert(id);
        for (size_t i = 0; i < island.size(); ++i)
            for (const auto& other : graph[island[i]]) {
                if (component_is_fixed(after, other)) boundary.insert(other);
                else if (seen.insert(other).second) island.push_back(other);
            }
        if (boundary.size() != 1 || !anchors.count(*boundary.begin())) continue;
        const auto& owner = *boundary.begin();
        const Eigen::Matrix4d& delta = anchors.at(owner);
        for (const auto& child : island) {
            auto it = s.asm_warm.find(child);
            if (it == s.asm_warm.end()) continue;
            auto& [t, w] = it->second;
            t = delta.topLeftCorner<3, 3>() * t + delta.topRightCorner<3, 1>();
            w = chudware::mate::mat_to_rotvec(
                delta.topLeftCorner<3, 3>() * chudware::mate::rodrigues(w));
            carried[child] = owner;
        }
    }
    return carried;
}

// An `asm_edit_component` that writes `transform` (or promotes the component to `fixed`)
// AUTHORS the pose. Free unmated components read their seed from the document
// after invalidation; fixed components always use their authored transform.
//
// The erase is deliberately NARROW: only a component whose pose it actually owns —
// `fixed`, or referenced by no non-suppressed mate. A mated, unfixed component KEEPS its
// warm seed: its transform is only a solver seed, and re-seeding the mate solve from an
// authored (off-solution) pose is not a no-op in practice — it lands the Newton iteration
// in a different, tilted minimum instead of snapping back to the previous solution. The
// authored edit is therefore an engine-level no-op there, which matches the tool layer
// (Python refuses it with `component_is_mated`).
//
// `doc_after` is the post-batch candidate document, so `fixed` and mate membership are
// read as the imminent rebuild will see them (a mate added in this same batch counts).
void invalidate_warm_for_authored_poses(Session& s, const json& doc_after, const json& ops) {
    if (s.asm_warm.empty() || !ops.is_array()) return;
    for (const json& op : ops) {
        if (!op.is_object() || op.value("op", std::string()) != "asm_edit_component") continue;
        const bool authored = (op.contains("transform") && !op["transform"].is_null()) ||
                              (op.contains("fixed") && op["fixed"] == true);
        if (!authored) continue;
        const std::string kid = op.value("id", std::string());
        if (kid.empty()) continue;
        const bool fixed = (op.contains("fixed") && op["fixed"] == true) ||
                           component_is_fixed(doc_after, kid);
        if (!fixed && component_is_mated(doc_after, kid)) continue;  // solver owns the pose
        s.asm_warm.erase(kid);
    }
}

// Tessellate each component (world-placed + optional explode offset) as one AICM body
// and write the AICM (port of session.py::_write_aicm_assembly). Returns (path, bodies-meta).
// `blob_out` (optional) receives the raw AICM bytes so doc_save can persist them as the
// assembly's cache.aicm without re-tessellating.
std::pair<std::string, json> write_aicm_assembly(Session& s,
                                                 std::vector<uint8_t>* blob_out = nullptr) {
    json bodies_meta = json::array();
    std::vector<aicm::AicmBody> tesses;
    std::map<std::string, Eigen::Vector3d> offsets;
    if (s.explode_active) offsets = explode_offsets(s.doc, s.explode_factor);
    const AssemblyResult* ares = s.asm_result.get();
    if (ares) {
        // Local-frame tessellation cache: each component's mesh is computed ONCE per TShape
        // (across opens, re-solves, drags and explodes — the load cache keeps shapes alive and
        // stable) and per-instance world meshes are produced by a cheap rigid vertex transform.
        // Even a "skip" BRepMesh pass costs a full model build per face (pcurve sampling,
        // FaceChecker) — measured >50% of warm assembly opens before this cache.
        static std::mutex s_asm_tess_mtx;
        struct AsmTessEntry {
            TopoDS_Shape shape;  // pins the TShape so the pointer key can't be recycled (ABA)
            std::shared_future<std::shared_ptr<const aicm::AicmBody>> body;
        };
        // WHY: lookup-or-install-placeholder happens under ONE s_asm_tess_mtx acquisition
        // below (no unlock/relock gap) so two threads racing the SAME uncached TShape key
        // can never both miss and both pay for a duplicate tessellate_body pass — the loser
        // waits on the winner's shared_future instead of redoing the (expensive) mesh work.
        // Keyed by (TShape, display quality): the same component meshed at a different
        // display tier is a DIFFERENT mesh, so quality is part of the key rather than a
        // reason to flush the cache.
        using AsmTessKey = std::pair<const void*, int>;
        static std::map<AsmTessKey, AsmTessEntry> s_asm_tess_cache;
        const int qkey = static_cast<int>(s.display_quality);
        for (const AsmBodyOut& body : ares->bodies) {
            std::optional<Eigen::Vector3d> off;
            auto oit = offsets.find(body.component_id);
            if (oit != offsets.end()) off = oit->second;
            float color[3] = {body.color[0], body.color[1], body.color[2]};
            const gp_Trsf t = placed_world_trsf(body, off);
            const bool rigid = std::abs(t.ScaleFactor() - 1.0) < 1e-12 && !t.IsNegative();
            aicm::AicmBody bt;
            if (rigid) {
                const AsmTessKey key{body.shape_local.TShape().get(), qkey};
                std::shared_future<std::shared_ptr<const aicm::AicmBody>> fut;
                std::shared_ptr<std::promise<std::shared_ptr<const aicm::AicmBody>>> mine;
                {
                    std::lock_guard<std::mutex> lk(s_asm_tess_mtx);
                    auto it = s_asm_tess_cache.find(key);
                    if (it != s_asm_tess_cache.end()) {
                        fut = it->second.body;
                    } else {
                        mine = std::make_shared<std::promise<std::shared_ptr<const aicm::AicmBody>>>();
                        fut = mine->get_future().share();
                        s_asm_tess_cache[key] = AsmTessEntry{body.shape_local, fut};
                    }
                }
                if (mine) {
                    try {
                        // Tessellate at IDENTITY location so the mesh is reusable for every
                        // instance/placement of this component.
                        const TopoDS_Shape local_shape =
                            body.shape_local.Located(TopLoc_Location());
                        const ::geom::DisplayDeflections d =
                            ::geom::display_deflections(s.display_quality, local_shape);
                        mine->set_value(std::make_shared<const aicm::AicmBody>(
                            ::geom::tessellate_body(local_shape, 0, std::string(), color,
                                                    d.lin, d.ang)));
                    } catch (...) {
                        mine->set_exception(std::current_exception());
                        // A failed tessellation is NOT cached — the next caller for this key retries.
                        std::lock_guard<std::mutex> lk(s_asm_tess_mtx);
                        s_asm_tess_cache.erase(key);
                    }
                }
                std::shared_ptr<const aicm::AicmBody> local = fut.get();  // rethrows for every waiter
                bt = *local;  // COW buffers — O(1) until the transform detaches
                bt.body_id = static_cast<uint32_t>(body.body_id);
                bt.name = body.name;
                bt.color[0] = color[0]; bt.color[1] = color[1]; bt.color[2] = color[2];
                if (t.Form() != gp_Identity) ::geom::transform_body_rigid(bt, t);
            } else {
                TopoDS_Shape world = placed_world_shape(body, off);
                const ::geom::DisplayDeflections d =
                    ::geom::display_deflections(s.display_quality, world);
                bt = ::geom::tessellate_body(world, static_cast<uint32_t>(body.body_id),
                                             body.name, color, d.lin, d.ang);
            }
            tesses.push_back(std::move(bt));
            bodies_meta.push_back({{"body_id", body.body_id}, {"name", body.name},
                                   {"color", {body.color[0], body.color[1], body.color[2]}},
                                   {"component", body.component_id},
                                   {"owner_key", body.component_id},
                                   {"metallic", 0.0}, {"roughness", 0.5}});
        }
    }
    std::vector<uint8_t> blob = aicm::write_aicm(tesses);
    if (blob_out) *blob_out = blob;
    std::string path = publish_doc_aicm(s.doc_id, s.revision, blob);
    if (path.empty() && !blob.empty()) {
        // Last-resort file write if publish fully failed.
        std::filesystem::path target = aicm_file(s.doc_id, s.revision);
        aicm::write_aicm_file(target.string(), tesses);
        path = std::filesystem::absolute(target).string();
    }
    return {path, bodies_meta};
}

// A line-carrying angle is a revolute joint whose scalar fixes its drive pose.
// Report mechanism mobility separately from the remaining solved-pose freedom.
int angle_drive_dof(const json& doc) {
    int count = 0;
    const auto mates = doc.find("mates");
    if (mates == doc.end() || !mates->is_array()) return count;
    for (const json& mate : *mates) {
        if (mate.value("suppressed", false) || mate.value("type", "") != "angle") continue;
        const auto value = mate.find("value");
        if (value != mate.end() && value->is_object() && value->contains("axis_point") &&
            (value->contains("axis") || value->contains("reference_axis"))) ++count;
    }
    return count;
}

// The assembly ops-response (port of the assembly branch of session.py::_ops_response).
json ops_response_assembly(Session& s, const json& id_map,
                           std::vector<uint8_t>* aicm_blob_out = nullptr) {
    auto [aicm_path, bodies_meta] = write_aicm_assembly(s, aicm_blob_out);
    const AssemblyResult* ares = s.asm_result.get();

    json feature_status = json::array();
    json components = json::array();
    json mates = json::array();
    int dof = 0;
    bool over_defined = false;
    bool converged = true;
    double max_residual = 0.0;
    double tolerance = 0.0;
    std::string worst_mate;
    std::string dof_summary;
    if (ares) {
        for (const AsmComponentStatus& cs : ares->component_status) {
            json row = {{"id", cs.id}, {"ok", cs.ok}, {"ms", cs.ms},
                        {"error", cs.error ? json(*cs.error) : json(nullptr)},
                        {"dangling_refs", cs.dangling_refs}};
            feature_status.push_back(row);
            components.push_back(row);
        }
        for (const AsmMateStatus& ms : ares->mate_status)
            mates.push_back({{"id", ms.id}, {"type", ms.type}, {"status", ms.status},
                             {"dangling_refs", ms.dangling_refs},
                             {"residual", ms.residual}, {"reason", ms.reason}});
        dof = ares->dof;
        over_defined = ares->over_defined;
        converged = ares->converged;
        max_residual = ares->max_residual;
        tolerance = ares->tolerance;
        worst_mate = ares->worst_mate;
        dof_summary = ares->dof_summary;
    }

    json active_sketch_status = nullptr;  // assemblies have no active sketch
    json rebuild = {
        {"feature_status", feature_status},
        {"doc_status", {{"active_sketch", active_sketch_status},
                        {"mates", over_defined ? "over" : (converged ? "ok" : "unsolved")},
                        {"active_configuration", config_active_name(s.doc)}}},
        {"components", components},
        {"mates", mates},
        {"dof", dof},
        {"over_defined", over_defined},
        // The mate solve's own verdict, so a caller never has to infer a failed solve
        // from the poses: `converged` plus the mate that owns the worst residual and
        // the size-scaled threshold it was judged against (see mate::solve_mates).
        {"converged", converged},
        {"max_residual", max_residual},
        {"worst_mate", worst_mate},
        {"tolerance", tolerance},
        {"dof_summary", dof_summary},
    };
    if (const int drives = angle_drive_dof(s.doc); drives > 0) {
        rebuild["residual_dof"] = dof;
        rebuild["dof"] = dof + drives;
    }
    return json{
        {"revision", s.revision},
        {"id_map", id_map},
        {"tree", s.doc},
        {"rebuild", rebuild},
        {"tess", {{"aicm_path", aicm_path}, {"bodies", bodies_meta}}},
        {"owner_map", json::object()},
    };
}

// ----------------------------------------------------------------- configurations
// The active config name with the implicit-Default + first-config fallbacks
// (mirrors session.py::_config_active_name -> compiler.resolve_active_config).
std::string config_active_name(const json& doc) {
    if (!doc.contains("configurations") || !doc["configurations"].is_array() ||
        doc["configurations"].empty())
        return "Default";
    const json& configs = doc["configurations"];
    std::string active = doc.value("active_configuration", std::string());
    if (!active.empty())
        for (const json& c : configs)
            if (c.is_object() && c.value("name", std::string()) == active) return active;
    const json& first = configs[0];
    std::string name = first.is_object() ? first.value("name", std::string()) : std::string();
    return name.empty() ? std::string("Default") : name;
}

// The doc's configurations array, materializing the implicit Default on first edit
// (session.py::_ensure_configs). Returns a reference into doc.
json& ensure_configs(json& doc) {
    if (!doc.contains("configurations") || !doc["configurations"].is_array())
        doc["configurations"] = json::array();
    json& configs = doc["configurations"];
    if (configs.empty()) {
        configs.push_back({{"name", "Default"}, {"param_overrides", json::object()},
                           {"suppressions", json::object()}});
        if (!doc.contains("active_configuration")) doc["active_configuration"] = "Default";
    }
    return configs;
}

// The config named `name`, or throw invalid_op (session.py::_find_config).
json& find_config(json& doc, const std::string& name) {
    if (doc.contains("configurations") && doc["configurations"].is_array())
        for (json& c : doc["configurations"])
            if (c.is_object() && c.value("name", std::string()) == name) return c;
    throw OpError("invalid_op", "no configuration named '" + name + "'",
                  {{"reason", "unknown configuration"}, {"name", name}});
}

// Validate the edited working doc; throw invalid_op listing the errors if broken
// (session.py::_validate_or_rollback — here the caller works on a COPY, so a throw
// leaves the live session untouched without an explicit rollback).
void validate_config_edit(const json& working) {
    std::vector<std::string> errs = validate_document(working);
    if (errs.empty()) return;
    std::string joined;
    for (std::size_t i = 0; i < errs.size() && i < 5; ++i) {
        if (i) joined += "; ";
        joined += errs[i];
    }
    throw OpError("invalid_op", "configuration edit produced an invalid document: " + joined,
                  {{"reason", "invalid configuration"}, {"errors", errs}});
}

// Commit a validated config edit: push one undo step, swap in the working doc, bump
// the revision, recompile, and return the ops-response (session.py::_config_commit;
// the C++ session always recompiles, so rebuild_needed is moot — same geometry).
json config_commit(Session& s, json&& working) {
    push_undo(s);
    s.doc = std::move(working);
    s.revision += 1;
    s.dirty = true;
    if (s.active_sketch) s.active_sketch->solve_state.reset();  // session.py::_config_commit
    CompileResult r = compile_session(s);
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

// ops.py::is_readonly_batch — true iff `ops` is non-empty and EVERY op is read-only (only
// copy_feature today). Such a batch surfaces its result only via the id_map side-channel, so
// the session skips the undo snapshot, the revision bump, and the dirty flag (session.py).
bool is_readonly_batch(const json& ops) {
    if (!ops.is_array() || ops.empty()) return false;
    for (const json& op : ops)
        if (!op.is_object() || op.value("op", std::string()) != "copy_feature") return false;
    return true;
}

// T-W2: after a sketch-mutating batch is applied to `working`, add auto-coincident
// constraints to the touched sketches (config-gated, default ON). Defined far below;
// forward-declared here so apply_ops_locked can call it. `working` is the post-op doc,
// pre-compile; `sketch_fids` are the sketch features the batch touched; `gen` mints ids.
// `touched_eids` scopes the proximity scan (T-PERF10); empty = full scan.
void maybe_auto_constrain(json& working, const std::vector<std::string>& sketch_fids,
                          IdGen& gen,
                          const std::unordered_set<std::string>& touched_eids = {});
// Entity ids touched by sketch-mutating ops in a batch (T-PERF10 scope). Defined with
// the auto-coincide helpers below.
std::unordered_set<std::string> touched_entity_ids(const json& ops, const json& id_map);

// The set of sketch feature ids a batch of ops touched. Two sources: (1) a
// `sketch_*` op's "sketch" ref (interactive editing), and (2) an `add_feature` op that
// CREATES a sketch — the primary path the MCP add_features tool and copilot use to
// author a sketch (its feature carries entities/constraints inline). For (2) the minted
// fid is id_map["$<idx>"]. Refs resolve through id_map ($N placeholders). Scopes the
// auto-coincident pass to only the sketches this batch actually created or edited.
std::vector<std::string> touched_sketch_fids(const json& ops, const json& id_map) {
    std::vector<std::string> out;
    if (!ops.is_array()) return out;
    auto add = [&](const std::string& fid) {
        if (!fid.empty() && std::find(out.begin(), out.end(), fid) == out.end())
            out.push_back(fid);
    };
    for (std::size_t i = 0; i < ops.size(); ++i) {
        const json& op = ops[i];
        if (!op.is_object()) continue;
        const std::string k = op.value("op", std::string());
        if (k == "add_feature") {
            // A sketch created in this batch — its fid is id_map["$<idx>"].
            if (op.contains("feature") && op["feature"].is_object() &&
                op["feature"].value("type", std::string()) == "sketch") {
                const std::string ph = "$" + std::to_string(i);
                if (id_map.contains(ph) && id_map[ph].is_string())
                    add(id_map[ph].get<std::string>());
            }
            continue;
        }
        if (k.rfind("sketch_", 0) != 0) continue;  // not a sketch op
        if (!op.contains("sketch")) continue;
        json ref = op["sketch"];
        resolve_placeholders_deep(ref, id_map);
        if (ref.is_string()) add(ref.get<std::string>());
    }
    return out;
}
// Assembly transforms are placement mutations, never inert part features.
json assembly_transform_op(const json& doc, const json& op, const json& id_map) {
    if (op.value("op", std::string()) != "add_feature") return op;
    const json& feature = op.contains("feature") ? op["feature"] : op;
    if (feature.value("type", std::string()) != "transform") return op;
    json p = feature;
    if (feature.contains("params") && feature["params"].is_object())
        p.update(feature["params"]);
    if (feature.contains("refs") && feature["refs"].contains("body"))
        p["body"] = feature["refs"]["body"];
    json ref = p.value("body", json());
    if (ref.is_object()) ref = ref.value("component", ref.value("owner_feature", json()));
    if (!ref.is_string())
        throw OpError("invalid_op", "transform: assembly body must name a component id",
                      {{"reason", "component_not_found"}, {"body", ref}});
    std::string id = ref.get<std::string>();
    if (id_map.contains(id) && id_map[id].is_string()) id = id_map[id];
    const json* component = nullptr;
    for (const json& c : doc["components"])
        if (c.value("id", std::string()) == id) { component = &c; break; }
    if (!component)
        throw OpError("invalid_op", "transform: component '" + id + "' not found in assembly",
                      {{"reason", "component_not_found"}, {"component", id}});
    if (!component->value("fixed", false) && component_is_mated(doc, id))
        throw OpError("invalid_op", "transform: component '" + id +
            "' pose is owned by mates; suppress its mates or ground it before moving",
            {{"reason", "component_is_mated"}, {"component", id}});
    auto vector = [](const json& value, const Eigen::Vector3d& fallback) -> Eigen::Vector3d {
        if (value.is_null()) return fallback;
        if (!value.is_array() || value.size() != 3)
            throw OpError("invalid_op", "transform: expected a three-number vector");
        return {value.at(0).get<double>(), value.at(1).get<double>(), value.at(2).get<double>()};
    };
    const json rotation = p.value("rotate", json());
    const double angle = rotation.is_object() ? rotation.value("angle", 0.0) : p.value("angle", 0.0);
    Eigen::Vector3d axis = vector(rotation.is_object() ? rotation.value("axis", json())
        : rotation.is_null() ? p.value("axis", json()) : rotation, Eigen::Vector3d::UnitZ());
    if (std::abs(angle) > 1e-9 && axis.norm() < 1e-12)
        throw OpError("invalid_op", "transform: rotate axis has zero length");
    const Eigen::Vector3d translation = vector(p.value("translate", json()), Eigen::Vector3d::Zero());
    const Eigen::Vector3d pivot = vector(p.value("pivot", json()), Eigen::Vector3d::Zero());
    Eigen::Matrix4d delta = Eigen::Matrix4d::Identity(), base = Eigen::Matrix4d::Identity();
    if (std::abs(angle) > 1e-9)
        delta.topLeftCorner<3, 3>() = Eigen::AngleAxisd(angle * M_PI / 180.0, axis.normalized()).toRotationMatrix();
    delta.topRightCorner<3, 1>() = translation + pivot - delta.topLeftCorner<3, 3>() * pivot;
    if (component->contains("transform"))
        for (int r = 0; r < 4; ++r)
            for (int c = 0; c < 4; ++c) base(r, c) = component->at("transform").at(r * 4 + c);
    const Eigen::Matrix4d moved = delta * base;
    json transform = json::array();
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c) transform.push_back(moved(r, c));
    if (p.value("copy", false))
        return {{"op", "asm_insert_component"}, {"source", component->at("source")},
                {"name", feature.value("name", component->value("name", id))},
                {"fixed", component->value("fixed", false)}, {"transform", transform}};
    return {{"op", "asm_edit_component"}, {"id", id}, {"transform", transform}};
}


// Apply an op batch to a session that the caller already holds g_mtx for (the shared
// transactional core of doc_apply_ops; doc_import_file reuses it).
json apply_ops_locked(Session& s, long base_revision, const json& input_ops, bool coalesce) {
    if (base_revision >= 0 && base_revision != s.revision)
        throw OpError("stale_revision",
                      "base_revision " + std::to_string(base_revision) + " != current " +
                          std::to_string(s.revision),
                      {{"current_revision", s.revision}});
    json assembly_ops;
    if (s.is_assembly()) assembly_ops = input_ops;
    const json& ops = s.is_assembly() ? assembly_ops : input_ops;
    if (!ops.is_array())
        throw OpError("invalid_op", "ops must be a list", {{"index", -1}, {"reason", "ops not a list"}});
    json working = s.doc;
    json id_map = json::object();
    IdGen gen(working);
    for (std::size_t i = 0; i < ops.size(); ++i) {
        if (s.is_assembly()) assembly_ops[i] = assembly_transform_op(working, ops[i], id_map);
        apply_one(working, ops[i], i, gen, id_map);
    }

    // T-W2 auto-coincident: on a sketch-mutating batch, pin touching endpoints (within
    // tolerance) as coincident BEFORE validation/compile, so touching-corner profiles
    // are already closed when the model sees them. Config-gated (default ON), best-effort
    // — it only ADDS safe (non-conflicting) coincident constraints; a failure is silent.
    // T-PERF10: proximity scan is scoped to entities this batch touched.
    {
        std::vector<std::string> sk_fids = touched_sketch_fids(ops, id_map);
        if (!sk_fids.empty()) {
            // Reseed IdGen from `working` (not from the original doc) so that authored
            // entity/constraint ids embedded inside add_feature ops are visible to gen.
            // Without this, gen starts at its pre-op maximum and mints ids that collide
            // with authored ids in the newly-inserted feature params (T-DP7).
            gen.scan(working);
            maybe_auto_constrain(working, sk_fids, gen, touched_entity_ids(ops, id_map));
        }
    }

    // T-BL49 associative Convert Entities — after the batch mutated the tree (an upstream
    // dim edit, a feature delete, a new boss...), re-resolve + re-project every sketch's
    // external_refs against the post-batch model so converted geometry follows its source.
    // Never blocks the edit: a total failure here leaves the last-good projected entities
    // in place (per-ref failures are already absorbed inside as stale marks). Skipped for
    // read-only batches (they leave the doc untouched and return early below).
    if (!is_readonly_batch(ops)) {
        // Thread the session's incremental-rebuild cache through the refresh compiles:
        // `working` is THIS doc's tree (s.doc + this batch), and the cache is
        // content-addressed, so the unchanged feature prefix restores from checkpoints
        // instead of recompiling the whole tree on every external-ref-bearing batch.
        CompileCache* cache = inc_rebuild_enabled() ? s.compile_cache.get() : nullptr;
        try {
            refresh_sketch_external_refs(working, cache, s.display_quality);
        } catch (...) { /* keep last-good projections */ }
        try {
            refresh_sketch3d_external_refs(working, cache, s.display_quality);
        } catch (...) { /* keep last-good ground points */ }
    }

    std::vector<std::string> errs = validate_document(working);
    if (!errs.empty()) {
        std::string joined;
        for (std::size_t i = 0; i < errs.size() && i < 5; ++i) {
            if (i) joined += "; ";
            joined += errs[i];
        }
        throw OpError("invalid_op", "resulting document failed validation: " + joined,
                      {{"index", -1}, {"reason", errs.front()}, {"all_errors", errs}});
    }

    const json pose_transport = transport_carried_poses(s, working, ops);
    // The batch is validated and will be applied: drop the warm-start entry of every
    // component whose pose this batch AUTHORS **and** owns (fixed, or bound by no
    // non-suppressed mate), so the rebuild below (either branch) seeds mate::solve_mates
    // from the authored transform instead of the last solve. `working` is the candidate
    // doc, so a `fixed` promotion or a mate added in this same batch is already visible.
    invalidate_warm_for_authored_poses(s, working, ops);

    // Read-only fast-path: a batch of only read-only ops (copy_feature) makes no document
    // change — it only returns its result through id_map ($bundle). Skip the undo snapshot,
    // the revision bump, and the dirty flag; the live doc is left untouched (session.py).
    if (is_readonly_batch(ops)) {
        if (s.is_assembly()) {
            rebuild_assembly(s);
            return ops_response_assembly(s, id_map);
        }
        CompileResult r = compile_session(s);
        return ops_response(s, r, id_map, /*with_tess=*/true);
    }

    // Compile against the candidate as the live document, but keep sole ownership of
    // the pre-edit tree for rollback and the eventual undo entry. History remains
    // untouched until the candidate compiles successfully, so a rejection needs no
    // speculative undo/redo vector copies or journal cleanup.
    json rollback_doc = std::move(s.doc);
    const long rollback_revision = s.revision;
    const bool rollback_dirty = s.dirty;

    s.doc = std::move(working);
    s.revision += 1;
    s.dirty = true;
    if (s.is_assembly()) {
        rebuild_assembly(s);
        if (!coalesce) push_undo_document(s, rollback_doc);
        json response = ops_response_assembly(s, id_map);
        response["pose_transport"] = pose_transport;
        return response;
    }
    CompileResult r = compile_session(s);
    if (!r.ok) {
        // `r` retains the rejected tree's feature failures for the in-band response;
        // restore the live document by move and leave both history stacks untouched.
        s.doc = std::move(rollback_doc);
        s.revision = rollback_revision;
        s.dirty = rollback_dirty;
        return ops_response(s, r, id_map, /*with_tess=*/true);
    }
    if (!coalesce) push_undo_document(s, rollback_doc);
    return warm_and_respond(s, std::move(r), id_map, /*with_tess=*/true);
}

// Create + register a blank part session. Inserts under the registry lock, then
// acquires the new session on the current DocLock so the caller can use the ref.
Session& new_part_session_locked(const std::string& name) {
    auto s = std::make_shared<Session>();
    s->doc_id = new_doc_id();
    s->doc = blank_part(s->doc_id, name);
    s->revision = 0;
    const std::string id = s->doc_id;
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        s->owns_journal = true;
        g_sessions[id] = s;
    }
    // Lock the brand-new session on this thread's DocLock (no contention possible).
    if (t_current_doc_lock) return t_current_doc_lock->acquire(id);
    // Fallback (should not happen — public APIs always construct DocLock first).
    s->mtx.lock();
    return *s;
}

// ============================================================ C18 sketch helpers
// C++ port of the active-sketch interaction core in engine/doc/session.py.

// session.py::_find_sketch_feature — locate a "sketch" feature by id (throws on a
// missing id / non-sketch). Returns a reference into s.doc["features"].
json& find_sketch_feature(Session& s, const std::string& fid) {
    if (s.doc.contains("features") && s.doc["features"].is_array())
        for (json& f : s.doc["features"])
            if (f.is_object() && f.value("id", std::string()) == fid) {
                if (f.value("type", std::string()) != "sketch")
                    throw OpError("invalid_op", "feature '" + fid + "' is not a sketch",
                                  {{"reason", "not a sketch"}});
                return f;
            }
    throw OpError("invalid_op", "no sketch feature '" + fid + "'", {{"reason", "unknown sketch"}});
}

// The sketch feature's params dict ({entities, constraints}). Host canonical shape
// stores them under "params"; a flat fallback reads them off the feature directly.
json sketch_params(const json& feat) {
    if (feat.contains("params") && feat["params"].is_object()) return feat["params"];
    json p = json::object();
    p["entities"] = feat.contains("entities") ? feat["entities"] : json::array();
    p["constraints"] = feat.contains("constraints") ? feat["constraints"] : json::array();
    return p;
}

// session.py::_resolve_sketch_plane — resolve refs.plane to a SketchPlane. Reserved
// datums ("plane.top/front/right" or bare "top/front/right") map to the factory
// frames; a plane-FEATURE id (offset/midplane/angle/etc.) is resolved by recompiling
// the doc and reading that feature's resolved plane out of CompileResult.datums (which
// now surfaces per-feature plane geometry — origin + x_dir/y_dir/normal). Only the
// reserved factory frames are tried first; an unknown ref falls back to TOP. Throws on
// no plane ref.
chudware::geom::SketchPlane resolve_sketch_plane_doc(const json& doc, const json& feat,
                                                  const CompileResult* pre) {
    json plane_ref;
    if (feat.contains("refs") && feat["refs"].is_object() && feat["refs"].contains("plane"))
        plane_ref = feat["refs"]["plane"];
    else if (feat.contains("plane"))
        plane_ref = feat["plane"];
    if (plane_ref.is_null())
        throw OpError("invalid_op", "sketch has no refs.plane", {{"reason", "no plane"}});
    std::string name;
    if (plane_ref.is_string()) name = plane_ref.get<std::string>();
    else if (plane_ref.is_object() && plane_ref.value("kind", std::string()) == "datum" &&
             plane_ref.contains("feature") && plane_ref["feature"].is_string())
        name = plane_ref["feature"].get<std::string>();
    std::string bare = name;
    if (bare.rfind("plane.", 0) == 0) bare = bare.substr(6);
    if (bare == "top")   return chudware::geom::SketchPlane::top();
    if (bare == "front") return chudware::geom::SketchPlane::front();
    if (bare == "right") return chudware::geom::SketchPlane::right();

    // Non-reserved ref: a created datum-plane feature (offset/midplane/angle/three_point/
    // …). Recompile and read its resolved frame from the datums side-channel so the sketch
    // lands ON that plane (origin + offset) instead of silently falling back to TOP — which
    // was the long-standing "offset plane sketches on the base plane" bug.
    if (!name.empty()) {
        // Serve from the caller's already-compiled result when it has one (the warm
        // read-cache / refresh_sketch_external_refs' `pre`) — datums are part of every
        // CompileResult, so this lookup needs no recompile of its own.
        std::optional<CompileResult> local;
        if (!pre) local = compile_document(doc, env_from_doc(doc));
        const CompileResult& r = pre ? *pre : *local;
        if (r.datums.is_array()) {
            auto v3 = [](const json& a, const Eigen::Vector3d& dflt) -> Eigen::Vector3d {
                if (a.is_array() && a.size() == 3 && a[0].is_number())
                    return Eigen::Vector3d(a[0].get<double>(), a[1].get<double>(),
                                           a[2].get<double>());
                return dflt;
            };
            for (const json& d : r.datums) {
                if (!d.is_object() || d.value("id", std::string()) != name) continue;
                const std::string k = d.value("kind", std::string());
                if (k != "plane" && k != "csys") break;  // axis/point/curve aren't sketchable
                chudware::geom::SketchPlane p;
                p.origin = v3(d.value("origin", json()), Eigen::Vector3d(0, 0, 0));
                p.uAxis  = v3(d.value("x_dir",  json()), Eigen::Vector3d(1, 0, 0));
                p.vAxis  = v3(d.value("y_dir",  json()), Eigen::Vector3d(0, 1, 0));
                p.normal = v3(d.value("normal", json()), Eigen::Vector3d(0, 0, 1));
                return p;
            }
        }
    }
    // Face-selector ref (kind == "face"): the sketch lives ON a model face, not a datum.
    // Mirror the modeling path (document.cpp plane_of -> face_plane(resolve_face_sel(...)))
    // so the live overlay lands on the picked face: recompile, resolve the face off the
    // last surviving body, and return its actual plane (area-centroid origin +
    // orientation-aware normal). Without this branch a face-sketch falls to
    // SketchPlane::top() (origin at the world origin) and draws on the parallel base plane
    // through the origin — the reported bug. Any failure falls through to top() (below).
    if (plane_ref.is_object() && plane_ref.value("kind", std::string()) == "face") {
        try {
            std::optional<CompileResult> local;
            if (!pre) local = compile_document(doc, env_from_doc(doc));
            const CompileResult& r = pre ? *pre : *local;
            if (!r.shapes.empty()) {
                const TopoDS_Shape& body = r.shapes.back();
                TopoDS_Face face;
                // Primary: re-express the resolved make_selector as a centroid-near query so
                // its UNIQUE centroid singles out one face even among symmetric duplicates —
                // the same strategy eval_resolve_ref uses for a resolved face/edge selector.
                const json& c = plane_ref.contains("centroid") ? plane_ref.at("centroid") : json();
                if (c.is_array() && c.size() == 3 && c[0].is_number() && c[1].is_number() &&
                    c[2].is_number()) {
                    chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(
                        body, json{{"of", std::string("face")}, {"near", c}, {"count", 1}});
                    if (qp.error.empty() && !qp.faces.empty()) face = qp.faces.front();
                }
                // Fallback: describe_faces + resolve for a selector with no centroid
                // (eval_resolve_ref's legacy path).
                if (face.IsNull()) {
                    std::vector<TopoInfo> infos = describe_faces(body);
                    ResolveResult rr = resolve(plane_ref, infos, bbox_diag(body));
                    if (rr.index.has_value()) {
                        int idx = 0;
                        for (TopExp_Explorer ex(body, TopAbs_FACE); ex.More(); ex.Next(), ++idx)
                            if (idx == *rr.index) { face = TopoDS::Face(ex.Current()); break; }
                    }
                }
                if (!face.IsNull()) {
                    BRepAdaptor_Surface ad(face);
                    if (ad.GetType() == GeomAbs_Plane) {
                        // origin (area centroid) + orientation-aware normal — mirrors
                        // face_plane() in document.cpp, using session.cpp's own REVERSED
                        // normal idiom (cf. the faceset normal in doc_face_points).
                        gp_Dir nd = ad.Plane().Axis().Direction();
                        if (face.Orientation() == TopAbs_REVERSED) nd.Reverse();
                        GProp_GProps props;
                        BRepGProp::SurfaceProperties(face, props);
                        gp_Pnt cm = props.CentreOfMass();
                        return chudware::geom::SketchPlane::from(
                            Eigen::Vector3d(cm.X(), cm.Y(), cm.Z()),
                            Eigen::Vector3d(nd.X(), nd.Y(), nd.Z()));
                    }
                }
            }
        } catch (...) {
            // Resolution failure (non-planar face, empty compile, stale selector) falls
            // through to the existing SketchPlane::top() fallback — behaviour unchanged.
        }
    }
    return chudware::geom::SketchPlane::top();
}

// Session convenience wrapper (the original entry point — all pre-T-BL43 callers).
// PERF: a valid read-cache compile (compiled_rev == revision, never stale — every
// geometry-changing op bumps revision) already carries the datums side-channel and the
// surviving shapes this resolution reads, so serve it instead of recompiling the tree
// per plane lookup.
chudware::geom::SketchPlane resolve_sketch_plane(Session& s, const json& feat) {
    const CompileResult* pre =
        (s.compiled && s.compiled_rev == s.revision) ? s.compiled.get() : nullptr;
    return resolve_sketch_plane_doc(s.doc, feat, pre);
}

// session.py::_plane_frame — 16-float row-major matrix mapping sketch (u,v,0,1) ->
// world. Columns are x_dir(uAxis), y_dir(vAxis), z_dir(normal), origin; rows 0..2
// are the affine rows, row 3 is [0,0,0,1].
std::vector<double> plane_frame(const chudware::geom::SketchPlane& p) {
    std::vector<double> out;
    out.reserve(16);
    for (int i = 0; i < 3; ++i) {
        out.push_back(p.uAxis[i]);
        out.push_back(p.vAxis[i]);
        out.push_back(p.normal[i]);
        out.push_back(p.origin[i]);
    }
    out.insert(out.end(), {0.0, 0.0, 0.0, 1.0});
    return out;
}

// session.py::_DIM_TYPES — a redundant DRIVING dim over-defines; geometric relations
// don't (see _redundant_overdefines for the SW-parity rationale).
bool is_dim_type(const std::string& t) {
    return t == "distance" || t == "angle" || t == "radius" || t == "diameter" || t == "length";
}

// session.py::_redundant_overdefines — true iff the redundant set contains a redundant
// driving dimension (a redundant geometric relation deliberately does NOT over-define).
bool redundant_overdefines(const std::vector<std::string>& redundant,
                           const json& constraints_by_id) {
    if (redundant.empty() || !constraints_by_id.is_object()) return false;
    for (const std::string& cid : redundant) {
        if (!constraints_by_id.contains(cid)) continue;
        const json& c = constraints_by_id[cid];
        if (c.is_object() && is_dim_type(c.value("type", std::string())) &&
            !c.value("driven", false))
            return true;
    }
    return false;
}

// session.py::_overall_status — under|full|over classification.
std::string overall_status(const json& statuses, int dof,
                           const std::vector<std::string>& conflicting,
                           const std::vector<std::string>& redundant,
                           const json& constraints_by_id) {
    bool any_over = false;
    if (statuses.is_object())
        for (auto it = statuses.begin(); it != statuses.end(); ++it)
            if (it.value().is_string() && it.value().get<std::string>() == "over") any_over = true;
    if (!conflicting.empty() || redundant_overdefines(redundant, constraints_by_id) || any_over)
        return "over";
    if (dof <= 0) return "full";
    return "under";
}

// session.py::_doc_entity_coords — freeze map: entity id -> stored coords (same key
// shape solve_sketch returns), stripping id/construction bookkeeping.
json doc_entity_coords(const json& params) {
    json out = json::object();
    if (params.contains("entities") && params["entities"].is_array())
        for (const json& e : params["entities"]) {
            if (!e.is_object() || !e.contains("id")) continue;
            json rec = json::object();
            for (auto it = e.begin(); it != e.end(); ++it)
                if (it.key() != "id" && it.key() != "construction") rec[it.key()] = it.value();
            out[e["id"].get<std::string>()] = rec;
        }
    return out;
}

// session.py::_sketch_constraints — pack every constraint for the host's dimension
// annotations: {id, type, refs, value, expr, driven, placement, measured, [axis], [cond]}.
json sketch_constraints(const json& feat, const json& entity_coords) {
    json out = json::array();
    json params = sketch_params(feat);
    if (!params.contains("constraints") || !params["constraints"].is_array()) return out;
    for (const json& c : params["constraints"]) {
        if (!c.is_object()) continue;
        const json* val_obj = (c.contains("value") && c["value"].is_object()) ? &c["value"] : nullptr;
        json rec;
        rec["id"] = c.contains("id") ? c["id"] : json(nullptr);
        rec["type"] = c.contains("type") ? c["type"] : json(nullptr);
        rec["refs"] = (c.contains("refs") && c["refs"].is_array()) ? c["refs"] : json::array();
        rec["value"] = val_obj && val_obj->contains("value") ? (*val_obj)["value"] : json(nullptr);
        rec["expr"] = val_obj && val_obj->contains("expr") ? (*val_obj)["expr"] : json(nullptr);
        rec["driven"] = c.value("driven", false);
        rec["placement"] = c.contains("placement") ? c["placement"] : json(nullptr);
        rec["measured"] = nullptr;
        if (c.contains("axis") && c["axis"].is_string() && !c["axis"].get<std::string>().empty())
            rec["axis"] = c["axis"];
        if (c.contains("cond") && c["cond"].is_array()) rec["cond"] = c["cond"];
        auto m = chudware::sketch::measure_constraint(c, entity_coords);
        if (m) rec["measured"] = *m;
        out.push_back(std::move(rec));
    }
    return out;
}

// Packed result of session.py::_solve_active (minus plane_frame). `warm` is the
// active-sketch warm state (nullopt for a cold solve). On a REJECTED solve
// (conflicting non-empty OR not converged) geometry is frozen at the doc's current
// coords and warm state is cleared.
struct SolveActive {
    std::optional<chudware::sketch::SolveState> state;  // nullopt when rejected
    json entities;          // list of coord dicts carrying id + construction
    json entity_map;        // id -> coord dict
    json statuses;          // id -> "under"|"full"|"over"
    int dof = 0;
    std::vector<std::string> conflicting;
    std::vector<std::string> redundant;
    json constraints_by_id;
    std::vector<std::string> skipped_constraints;
    bool rejected = false;
};

SolveActive solve_active(Session& s, const json& feat,
                         const std::optional<chudware::sketch::SolveState>& warm) {
    json env = env_from_doc(s.doc);
    json params = sketch_params(feat);
    json sk;
    sk["entities"] = params.contains("entities") ? params["entities"] : json::array();
    sk["constraints"] = params.contains("constraints") ? params["constraints"] : json::array();

    // Interactive solve (T-PERF8 LDLT + T-PERF9 component decomp). Exact/oracle
    // callers hit solve_sketch without interactive; this path is the live sketcher.
    chudware::sketch::SketchSolution sol =
        chudware::sketch::solve_sketch(sk, env, warm, /*time_budget_ms=*/std::nullopt,
                                    /*interactive=*/true);

    SolveActive out;
    out.rejected = !sol.conflicting.empty() || !sol.converged;
    out.conflicting = sol.conflicting;
    out.redundant = sol.redundant;
    out.dof = sol.dof;
    out.statuses = json::object();
    for (const auto& kv : sol.statuses) out.statuses[kv.first] = kv.second;

    json coord_map = out.rejected ? doc_entity_coords(params) : sol.entities;
    out.entity_map = coord_map;

    // construction flag lookup
    auto construction_of = [&](const std::string& eid) -> bool {
        if (params.contains("entities") && params["entities"].is_array())
            for (const json& e : params["entities"])
                if (e.is_object() && e.value("id", std::string()) == eid)
                    return e.value("construction", false);
        return false;
    };
    json entities = json::array();
    for (auto it = coord_map.begin(); it != coord_map.end(); ++it) {
        json rec = it.value();
        rec["id"] = it.key();
        rec["construction"] = construction_of(it.key());
        entities.push_back(std::move(rec));
    }
    out.entities = std::move(entities);

    out.constraints_by_id = json::object();
    if (params.contains("constraints") && params["constraints"].is_array())
        for (const json& c : params["constraints"])
            if (c.is_object() && c.contains("id"))
                out.constraints_by_id[c["id"].get<std::string>()] = c;

    out.skipped_constraints.clear();  // C++ solver has no skipped_constraints field

    out.state = out.rejected ? std::optional<chudware::sketch::SolveState>{} : sol.state;
    return out;
}

// std::vector<std::string> -> json array helper.
json strvec_json(const std::vector<std::string>& v) {
    json a = json::array();
    for (const std::string& s : v) a.push_back(s);
    return a;
}

// T-BL49 — external-ref ownership map (eid -> {ref, stale}) so the host can style
// converted entities distinctly (and dash/warn the stale ones). Absent key = a normal
// user-drawn entity. Shared by sketch_result (enter/solve) and doc_sketch_drag.
json sketch_external_map(const json& feat) {
    json ext = json::object();
    const json params = sketch_params(feat);
    if (params.contains("external_refs") && params["external_refs"].is_array())
        for (const json& xr : params["external_refs"]) {
            if (!xr.is_object()) continue;
            const bool stale = xr.value("stale", false);
            const std::string xid = xr.value("id", std::string());
            if (xr.contains("entity_ids") && xr["entity_ids"].is_array())
                for (const json& eid : xr["entity_ids"])
                    if (eid.is_string())
                        ext[eid.get<std::string>()] = json{{"ref", xid}, {"stale", stale}};
        }
    return ext;
}

// Build the sketch_enter/sketch_solve result dict from a packed SolveActive +
// plane_frame + overall status (session.py sketch_enter/sketch_solve shape).
json sketch_result(const json& feat, const SolveActive& packed,
                   const std::vector<double>& frame, const std::string& status) {
    json r;
    r["entities"] = packed.entities;
    r["statuses"] = packed.statuses;
    r["dof"] = packed.dof;
    r["plane_frame"] = frame;
    r["status"] = status;
    r["conflicting"] = strvec_json(packed.conflicting);
    r["redundant"] = strvec_json(packed.redundant);
    r["skipped_constraints"] = strvec_json(packed.skipped_constraints);
    r["solve_rejected"] = packed.rejected;
    r["constraints"] = sketch_constraints(feat, packed.entity_map);
    r["external"] = sketch_external_map(feat);
    return r;
}

}  // namespace

// ----------------------------------------------------------------- crash recovery
// Drop orphaned per-doc mutexes left held by THIS thread after a Windows structured
// exception (an access violation) unwound past a DocLock without running its C++
// destructor under /EHsc. Called from the libchudware C dispatch boundary's __except
// filter after such a fault, so the worker thread doesn't EDEADLK on its next call.
//
// SAFETY: unlocks only mutexes tracked by this thread's DocLock chain (g_mtx_held &&
// g_mtx_owner == this-thread). Idempotent: a second call no-ops once the flag is clear.
bool force_release_doc_lock() {
    if (!g_mtx_held.load(std::memory_order_acquire)) return false;
    if (g_mtx_owner != std::this_thread::get_id()) return false;
    bool released = false;
    // Walk the nested DocLock chain and force-release each.
    DocLock* lk = t_current_doc_lock;
    while (lk) {
        DocLock* prev = lk->previous();
        lk->force_release();
        released = true;
        lk = prev;
    }
    t_current_doc_lock = nullptr;  // abandon the orphaned RAII objects
    g_mtx_held.store(false, std::memory_order_release);
    return released;
}

// Serializes the whole check-then-mint of doc_new. unique_doc_name reads every open
// document's name and new_part_session_locked registers a new one; those are two
// separate visits to g_sessions, so without a lock spanning BOTH, concurrent
// doc_new("Widget") calls each see "Widget" free and each mint. Observed live
// 2026-07-29 (run-42508173312214): the user retried a request whose first worker was
// still inside its 5-minute timeout, both attempts reached the ONE machine-wide
// chudware-hostd, and one requested part became three GUI tabs
// (lexus_f_keychain / ...2 / ...3).
//
// Lock order: this is the OUTERMOST lock in the file. It is taken only at the top of
// doc_new, before g_registry_mtx and before any Session::mtx, and nothing acquires it
// while holding either — so it cannot participate in a cycle.
static std::mutex g_doc_mint_mtx;

// Trim + case-fold, so the resume match below follows the SAME rule the Python side
// uses (_docs_named in services/mcp/cad_common.py): an exact name match wins outright,
// and only if there is none do case-insensitive matches count.
static std::string fold_doc_name(const std::string& s) {
    std::size_t b = 0, e = s.size();
    while (b < e && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
    std::string out = s.substr(b, e - b);
    for (char& c : out) c = (char)std::tolower(static_cast<unsigned char>(c));
    return out;
}

// The already-open document `requested` names, or nullopt. MUST be called with
// g_doc_mint_mtx held, which is what makes doc_new's resume-or-mint atomic.
//
// Which one, when several answer to the name, is the ranking services/mcp new_part used
// to apply in Python before this became the engine's job: most features, then highest
// revision, then the last in doc_list order. g_sessions is keyed by doc_id and doc_list
// sorts by doc_id, so iterating the map ascending and letting a tie go to the LATER
// candidate reproduces that ranking. Deterministic — never a coin flip.
//
// A BUILT part is resumed too, not skipped: the reply carries feature_count so the
// caller can tell the agent what it resumed onto (docs/REFACTOR_LOG.md ~5274, ~5309 —
// inheriting a stranger's half-built body silently is what produced twelve "boss added
// no material (it is buried inside the body)" failures, so the count must travel).
static std::optional<json> resume_open_doc(const std::string& requested,
                                           const std::string& kind) {
    std::vector<std::pair<std::string, std::shared_ptr<Session>>> pins;
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        pins.reserve(g_sessions.size());
        for (const auto& kv : g_sessions) pins.emplace_back(kv.first, kv.second);
    }
    std::sort(pins.begin(), pins.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });
    struct Cand {
        std::string id, name;
        long revision = 0;
        int features = 0;
        bool exact = false;
    };
    const std::string want = fold_doc_name(requested);
    std::vector<Cand> cands;
    for (const auto& [id, sp] : pins) {
        // Pin-then-lock, one session at a time (T-PERF5, same as doc_list/unique_doc_name).
        std::lock_guard<std::mutex> doc(sp->mtx);
        if (!sp->doc.is_object()) continue;
        const bool is_asm = sp->doc.value("schema", std::string()) == "aicad.asm/1";
        if (is_asm != (kind == "assembly")) continue;  // never resume a part as an assembly
        const std::string nm = sp->doc.value("name", std::string());
        const bool exact = nm == requested;
        if (!exact && fold_doc_name(nm) != want) continue;
        Cand c;
        c.id = id;
        c.name = nm;
        c.revision = sp->revision;
        if (sp->doc.contains("features") && sp->doc["features"].is_array())
            c.features = (int)sp->doc["features"].size();
        c.exact = exact;
        cands.push_back(std::move(c));
    }
    if (cands.empty()) return std::nullopt;
    if (std::any_of(cands.begin(), cands.end(), [](const Cand& c) { return c.exact; }))
        cands.erase(std::remove_if(cands.begin(), cands.end(),
                                   [](const Cand& c) { return !c.exact; }),
                    cands.end());
    const Cand* best = &cands.front();
    for (const Cand& c : cands)
        if (c.features > best->features ||
            (c.features == best->features && c.revision >= best->revision))
            best = &c;
    json resp = {{"ok", true},
                 {"doc_id", best->id},
                 {"name", best->name},
                 {"revision", best->revision},
                 {"feature_count", best->features},
                 {"reused", true}};
    json others = json::array();
    for (const Cand& c : cands)
        if (c.id != best->id) others.push_back(c.id);
    if (!others.empty()) resp["duplicate_doc_ids"] = others;
    return resp;
}

// Auto-unique document names, like real CAD. If `requested` exactly matches an
// already-open document's name, strip any trailing integer to get the base
// ("Part1" -> "Part") and return the first free "<base><N>", continuing past
// the highest existing suffix so repeated doc_new("Part1") yields Part1,
// Part2, Part3, ...  Non-colliding names pass through untouched.
static std::string unique_doc_name(const std::string& requested) {
    // Snapshot open-doc names: pin sessions under the registry lock, then lock
    // each one at a time to read its name (T-PERF5 pattern, same as doc_list).
    // Caller's DocLock holds no session mutexes yet, so this cannot deadlock.
    std::vector<std::shared_ptr<Session>> pins;
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        pins.reserve(g_sessions.size());
        for (const auto& kv : g_sessions) pins.push_back(kv.second);
    }
    std::set<std::string> names;
    for (const auto& sp : pins) {
        std::lock_guard<std::mutex> doc(sp->mtx);
        if (sp->doc.is_object()) names.insert(sp->doc.value("name", std::string()));
    }
    if (names.find(requested) == names.end()) return requested;
    // Split off a trailing integer to get the series base.
    std::size_t cut = requested.size();
    while (cut > 0 && std::isdigit(static_cast<unsigned char>(requested[cut - 1]))) --cut;
    std::string base = requested.substr(0, cut);
    if (base.empty()) base = requested;  // all-digit name: suffix the whole thing
    // Start after the highest existing "<base><N>" so the series stays monotonic.
    long start = 2;
    for (const std::string& n : names) {
        if (n.size() <= base.size() || n.compare(0, base.size(), base) != 0) continue;
        const std::string tail = n.substr(base.size());
        if (tail.empty() || tail.size() > 9 ||
            tail.find_first_not_of("0123456789") != std::string::npos)
            continue;
        const long v = std::stol(tail);
        if (v + 1 > start) start = v + 1;
    }
    for (long i = start;; ++i) {
        std::string cand = base + std::to_string(i);
        if (names.find(cand) == names.end()) return cand;
    }
}

// ----------------------------------------------------------------- public API
json doc_new(const std::string& kind, const std::string& name, bool resume_if_open) {
    const std::string k = kind.empty() ? std::string("part") : kind;
    if (k != "part" && k != "assembly")
        throw OpError("invalid_op", "doc kind must be part|assembly, got '" + k + "'",
                      {{"kind", kind}, {"reason", "bad kind"}});
    DocLock lk;
    const std::string requested =
        name.empty() ? (k == "assembly" ? std::string("Assembly1") : std::string("Part1")) : name;
    // Everything from here to the registration is ONE atomic step (see g_doc_mint_mtx):
    // resume the same-named document if the caller opted in, otherwise uniquify and mint.
    std::unique_lock<std::mutex> mint(g_doc_mint_mtx);
    if (resume_if_open) {
        if (std::optional<json> resumed = resume_open_doc(requested, k)) return *resumed;
    }
    const std::string resolved = unique_doc_name(requested);
    if (k == "assembly") {
        auto sp = std::make_shared<Session>();
        sp->doc_id = new_doc_id();
        sp->doc = blank_assembly(sp->doc_id, resolved);
        sp->revision = 0;
        const std::string id = sp->doc_id;
        {
            std::lock_guard<std::mutex> reg(g_registry_mtx);
            sp->owns_journal = true;
            g_sessions[id] = sp;
        }
        mint.unlock();  // registered under the name: the next mint can no longer collide
        Session& stored = lk.acquire(id);
        rebuild_assembly(stored);
        json resp = ops_response_assembly(stored, json::object());
        resp["doc_id"] = stored.doc_id;
        resp["name"] = resolved;
        return resp;
    }
    Session& stored = new_part_session_locked(resolved);
    // Registered under `resolved`; the compile below touches only this brand-new
    // session, so hold the mint lock no longer than the name reservation needs.
    mint.unlock();
    CompileResult r = compile_session(stored);
    json resp = ops_response(stored, r, json::object(), /*with_tess=*/true);
    resp["doc_id"] = stored.doc_id;
    resp["name"] = resolved;
    return resp;
}

json doc_apply_ops(const std::string& doc_id, long base_revision, const json& ops, bool coalesce) {
    DocLock lk;
    Session& s = require_session(doc_id);
    return apply_ops_locked(s, base_revision, ops, coalesce);
}

json doc_dry_run_ops(const std::string& doc_id, const json& ops) {
    DocLock lk;
    Session& live = require_session(doc_id);
    // Clone the live doc into a scratch session; apply_ops_locked there so the live
    // session (and its revision/undo stacks) is never touched. base_revision = -1
    // skips the optimistic-concurrency guard (matches ops_emit.dry_run, which builds
    // the would-be document directly off the doc dict with no revision check).
    Session scratch;
    scratch.doc_id = live.doc_id;  // any value; never registered/persisted.
    scratch.doc = live.doc;        // deep copy of the feature tree json.
    scratch.revision = live.revision;
    try {
        // apply_ops_locked THROWS on a structural/validation rejection (caught below) but
        // RETURNS an in-band rejection on a compile failure (per the batch-atomicity contract:
        // the doomed batch is restored, then its per-feature failure is surfaced in
        // rebuild.feature_status). The dry-run gate must report {ok:false} for BOTH, so we
        // inspect the returned feature_status for any ok:false and compose the same error
        // string shape (the failing features' messages joined) the throw path produces.
        json resp = apply_ops_locked(scratch, -1, ops, /*coalesce=*/true);
        std::vector<std::string> feat_errs;
        if (resp.contains("rebuild") && resp["rebuild"].is_object() &&
            resp["rebuild"].contains("feature_status") &&
            resp["rebuild"]["feature_status"].is_array())
            for (const json& st : resp["rebuild"]["feature_status"])
                if (st.is_object() && !st.value("ok", true)) {
                    std::string emsg = st.contains("error") && st["error"].is_string()
                                           ? st["error"].get<std::string>()
                                           : std::string("feature compile failed");
                    feat_errs.push_back(st.value("id", std::string()) + ": " + emsg);
                }
        if (!feat_errs.empty()) {
            std::string joined;
            for (std::size_t i = 0; i < feat_errs.size() && i < 5; ++i) {
                if (i) joined += "; ";
                joined += feat_errs[i];
            }
            return json{{"ok", false}, {"error", "op batch rejected: " + joined}};
        }
        return json{{"ok", true}};
    } catch (const OpError& e) {
        // Compose the error string the SAME way ops_emit.dry_run does:
        //   reason = env["message"]; if detail.all_errors: reason + " | " + "; ".join(all_errors[:5])
        std::string reason = e.what();
        if (e.detail.is_object() && e.detail.contains("all_errors") &&
            e.detail["all_errors"].is_array() && !e.detail["all_errors"].empty()) {
            std::string joined;
            const json& all = e.detail["all_errors"];
            for (std::size_t i = 0; i < all.size() && i < 5; ++i) {
                if (i) joined += "; ";
                joined += all[i].get<std::string>();
            }
            reason = reason + " | " + joined;
        }
        return json{{"ok", false}, {"error", reason}};
    } catch (const std::exception& e) {
        // Python's generic branch: f"{type(e).__name__}: {e}". The C++ exception type
        // name is mangled/platform-specific, so we emit the message (the OpError path
        // above is the one the agent gate exercises; this is a defensive fallback).
        return json{{"ok", false}, {"error", std::string(e.what())}};
    }
}

json doc_undo(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.undo_stack.empty())
        throw OpError("invalid_op", "nothing to undo", {{"reason", "empty undo stack"}});
    s.redo_stack.push_back(s.doc.dump());
    trim_history(s.doc_id, s.redo_stack);
    // Materialize stubs on demand (disk journal for large history entries).
    const std::string entry = s.undo_stack.back();
    const std::string snap = materialize_history_entry(entry);
    if (is_undo_stub(entry) && is_undo_stub(snap))
        throw OpError("io_error", "history entry lost (undo journal unreadable)",
                      {{"reason", "stub materialize failed"},
                       {"path", undo_stub_path(entry)}});
    drop_history_entry(entry);  // free journal file if it was a stub
    s.undo_stack.pop_back();
    s.doc = json::parse(snap);
    s.revision += 1;  // undo bumps the revision forward (Python contract)
    s.dirty = true;
    // C50: drop warm sketch state so the next solve COLD-solves the restored seeds
    // (session.py: active_sketch.solve_state = None on undo/redo).
    if (s.active_sketch) s.active_sketch->solve_state.reset();
    if (s.is_assembly()) {
        s.asm_warm.clear();  // re-seed mate solve from the restored doc transforms
        rebuild_assembly(s);
        return ops_response_assembly(s, json::object());
    }
    CompileResult r = compile_session(s);
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

json doc_redo(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.redo_stack.empty())
        throw OpError("invalid_op", "nothing to redo", {{"reason", "empty redo stack"}});
    s.undo_stack.push_back(s.doc.dump());
    trim_history(s.doc_id, s.undo_stack);
    const std::string entry = s.redo_stack.back();
    const std::string snap = materialize_history_entry(entry);
    if (is_undo_stub(entry) && is_undo_stub(snap))
        throw OpError("io_error", "history entry lost (redo journal unreadable)",
                      {{"reason", "stub materialize failed"},
                       {"path", undo_stub_path(entry)}});
    drop_history_entry(entry);
    s.redo_stack.pop_back();
    s.doc = json::parse(snap);
    s.revision += 1;
    s.dirty = true;
    if (s.active_sketch) s.active_sketch->solve_state.reset();  // C50 (see doc_undo)
    if (s.is_assembly()) {
        s.asm_warm.clear();  // re-seed mate solve from the restored doc transforms
        rebuild_assembly(s);
        return ops_response_assembly(s, json::object());
    }
    CompileResult r = compile_session(s);
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

json doc_clear_history(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    for (const std::string& e : s.undo_stack) drop_history_entry(e);
    for (const std::string& e : s.redo_stack) drop_history_entry(e);
    s.undo_stack.clear();
    s.redo_stack.clear();
    wipe_undo_journal(doc_id);
    // No revision bump — save baseline, not an edit.
    return json{{"ok", true}, {"doc_id", doc_id}};
}

json doc_tree(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.is_assembly()) {
        json resp;
        resp["revision"] = s.revision;
        resp["tree"] = s.doc;
        resp["owner_map"] = json::object();
        return resp;
    }
    // No revision bump / no tess, but DO carry the owner_map (host picking) — reuse the cached
    // compile (recomputes only when the doc changed since the last query).
    const CompileResult& r = ensure_compiled(s);
    json resp;
    resp["revision"] = s.revision;
    resp["tree"] = s.doc;
    resp["owner_map"] = r.owner_map.is_null() ? json::object() : r.owner_map;
    return resp;
}

json doc_live_source(const std::string& doc_id) {
    // Non-throwing peek at an open session's current tree + env (the associative source
    // for drawings / derived re-pulls). A missing doc returns null so the caller keeps
    // its last good snapshot rather than crashing when the source was closed.
    DocLock lk;
    if (!pin_session(doc_id)) return json(nullptr);
    Session& s = lk.acquire(doc_id);
    return json{{"tree", s.doc}, {"env", env_from_doc(s.doc)}, {"revision", s.revision},
                {"path", s.path.empty() ? json(nullptr) : json(s.path)},
                {"schema", s.doc.is_object()
                               ? s.doc.value("schema", std::string())
                               : std::string()}};
}

std::string doc_find_open_by_path(const std::string& path) {
    if (path.empty()) return {};
    namespace fs = std::filesystem;
    std::error_code wanted_ec;
    fs::path wanted = fs::weakly_canonical(fs::path(path), wanted_ec);
    if (wanted_ec) wanted = fs::path(path).lexically_normal();

    std::vector<std::pair<std::string, std::shared_ptr<Session>>> pins;
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        pins.reserve(g_sessions.size());
        for (const auto& entry : g_sessions) pins.push_back(entry);
    }
    for (const auto& [id, sp] : pins) {
        std::lock_guard<std::mutex> doc(sp->mtx);
        if (sp->path.empty()) continue;
        std::error_code have_ec;
        fs::path have = fs::weakly_canonical(fs::path(sp->path), have_ec);
        if (have_ec) have = fs::path(sp->path).lexically_normal();
        if (have == wanted) return id;
    }
    return {};
}

json doc_open_embedded(const json& document) {
    const std::string schema =
        document.is_object() ? document.value("schema", std::string()) : std::string();
    if (schema != "aicad.part/1" && schema != "aicad.asm/1")
        throw OpError("unsupported",
                      "doc_open_embedded: expected a part/assembly document (got schema '" +
                          schema + "')",
                      {{"schema", schema}, {"reason", "unsupported document kind"}});

    auto sp = std::make_shared<Session>();
    sp->doc_id = new_doc_id();
    sp->doc = document;
    sp->doc["id"] = sp->doc_id;
    sp->revision = 0;
    sp->dirty = false;
    const std::string sid = sp->doc_id;
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        sp->owns_journal = true;
        g_sessions[sid] = sp;
    }
    return json{{"doc_id", sid},
                {"name", sp->doc.value("name", std::string())},
                {"schema", schema},
                {"embedded", true}};
}
// Viewport tab-switch activation: identical response shape to doc_rebuild, but served from
// the session's read-cache compile whenever it is valid (`compiled_rev == revision` — every
// mutating op leaves it warm via warm_and_respond, and every mutation bumps the revision, so
// a valid cache is never stale). ops_response republishes the retained AICM blob (shm write,
// no tessellation), so switching the GUI onto a doc the daemon already built — the shared-
// daemon AI-audit case — costs a JSON serialize instead of a full recompile + re-tess.
// Cold/stale cache falls back to the exact doc_rebuild body below.
json doc_activate(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.is_assembly()) {
        if (!s.asm_result) rebuild_assembly(s);
        return ops_response_assembly(s, json::object());
    }
    if (s.compiled && s.compiled_rev == s.revision)
        return ops_response(s, *s.compiled, json::object(), /*with_tess=*/true);
    CompileCache* cache = inc_rebuild_enabled() ? s.compile_cache.get() : nullptr;
    try {
        refresh_sketch_external_refs(s.doc, cache, s.display_quality);
    } catch (...) { /* keep last-good projections */ }
    try {
        refresh_sketch3d_external_refs(s.doc, cache, s.display_quality);
    } catch (...) { /* keep last-good ground points */ }
    CompileResult r = compile_session(s);
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}


json doc_rebuild(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.is_assembly()) {
        rebuild_assembly(s);
        return ops_response_assembly(s, json::object());
    }
    // T-BL49 — an explicit rebuild also re-syncs associative Convert Entities (covers
    // mutation paths that bypass doc_apply_ops, e.g. sketch_drag_commit). No revision
    // bump: re-projection is derived data; the compile below sees the refreshed tree.
    CompileCache* cache = inc_rebuild_enabled() ? s.compile_cache.get() : nullptr;
    try {
        refresh_sketch_external_refs(s.doc, cache, s.display_quality);
    } catch (...) { /* keep last-good projections */ }
    try {
        refresh_sketch3d_external_refs(s.doc, cache, s.display_quality);
    } catch (...) { /* keep last-good ground points */ }
    CompileResult r = compile_session(s);
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

// ============================================================ C20 assembly RPCs
// doc_solve_mates: re-solve the assembly mates (optionally with a drag soft-target),
// persist the solved transforms, bump the revision (one undoable step), and return the
// ops-response (port of session.py::solve_mates). doc_set_explode_state toggles the
// explode view and re-tessellates without a revision bump.

json doc_solve_mates(const std::string& doc_id, const json& drag) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (!s.is_assembly())
        throw OpError("invalid_op", "doc_solve_mates: document is not an assembly",
                      {{"reason", "not an assembly"}});
    std::string base_dir;
    if (!s.path.empty()) base_dir = std::filesystem::path(s.path).parent_path().string();
    push_undo(s);

    std::optional<chudware::mate::WarmMap> warm;
    if (!s.asm_warm.empty()) warm = s.asm_warm;
    std::optional<chudware::mate::Drag> drag_opt;
    if (drag.is_object() && drag.contains("component") && drag["component"].is_string()) {
        // session.py::_compile_assembly_drag: first a plain compile to warm + load, then
        // a second solve with the drag target seeded from that warm result. compile_assembly
        // accepts a drag arg directly here, threading it to the solver in one pass.
        chudware::mate::Drag d;
        d.component = drag["component"].get<std::string>();
        if (drag.contains("target") && drag["target"].is_array() && drag["target"].size() == 3)
            d.target = Eigen::Vector3d(drag["target"][0].get<double>(),
                                       drag["target"][1].get<double>(),
                                       drag["target"][2].get<double>());
        drag_opt = d;
    }
    AssemblyResult ares = compile_assembly(s.doc, base_dir, warm, drag_opt);
    if (s.doc.contains("components") && s.doc["components"].is_array())
        for (json& comp : s.doc["components"]) {
            auto it = ares.transforms.find(comp.value("id", std::string()));
            if (it != ares.transforms.end()) comp["transform"] = it->second;
        }
    s.asm_warm = ares.warm;
    s.asm_result = std::make_shared<AssemblyResult>(std::move(ares));
    s.revision += 1;
    s.dirty = true;
    return ops_response_assembly(s, json::object());
}

json doc_set_explode_state(const std::string& doc_id, bool active, double factor) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (!s.is_assembly())
        throw OpError("invalid_op", "doc_set_explode_state: document is not an assembly",
                      {{"reason", "not an assembly"}});
    s.explode_active = active;
    s.explode_factor = std::max(0.0, std::min(1.0, factor));
    if (!s.asm_result) rebuild_assembly(s);
    return ops_response_assembly(s, json::object());  // view-only: no revision bump
}

// Total display triangles currently attached to `shapes`. BRepMesh writes its
// triangulation into the TShape, so right after a compile/tessellate this is a free
// read of what the viewport will actually draw — no second meshing pass.
static long long attached_triangle_count(const std::vector<TopoDS_Shape>& shapes) {
    long long n = 0;
    for (const TopoDS_Shape& sh : shapes) {
        if (sh.IsNull()) continue;
        for (TopExp_Explorer ex(sh, TopAbs_FACE); ex.More(); ex.Next()) {
            TopLoc_Location loc;
            Handle(Poly_Triangulation) tri =
                BRep_Tool::Triangulation(TopoDS::Face(ex.Current()), loc);
            if (!tri.IsNull()) n += tri->NbTriangles();
        }
    }
    return n;
}

// Set this document's DISPLAY tessellation tier and re-tessellate (worker_rpc.md §19).
// View-only, exactly like doc_set_explode_state: the feature tree and the revision are
// untouched, nothing becomes dirty and nothing is undoable. The tier is part of the
// compile-cache chain seed and of the per-body AICM cache key (document.cpp), so both
// caches are dropped here and the compile below re-meshes every body at the new
// deflections. Returns {ok, quality, triangles} plus the usual ops-response (so the
// caller can reload the new mesh from tess.aicm_path).
json doc_set_display_quality(const std::string& doc_id, const std::string& quality) {
    DocLock lk;
    Session& s = require_session(doc_id);
    ::geom::DisplayQuality q = ::geom::DisplayQuality::Fine;
    if (!::geom::display_quality_parse(quality, q))
        throw OpError("invalid_op",
                      "doc_set_display_quality: quality must be draft|standard|fine, got '" +
                          quality + "'",
                      {{"quality", quality}, {"reason", "bad quality"}});

    s.display_quality = q;
    // Same invalidation a recompute needs: drop the read-only compile result AND the
    // incremental prefix/per-body mesh cache so nothing is served at the old tier.
    s.compiled.reset();
    s.compiled_rev = -1;
    s.compile_cache = chudware::doc::make_compile_cache();

    json resp;
    long long tris = 0;
    if (s.is_assembly()) {
        rebuild_assembly(s);
        resp = ops_response_assembly(s, json::object());
        std::vector<TopoDS_Shape> shapes;
        if (const AssemblyResult* ares = s.asm_result.get())
            for (const AsmBodyOut& b : ares->bodies) shapes.push_back(b.shape_local);
        tris = attached_triangle_count(shapes);
    } else {
        const CompileResult& r = ensure_compiled(s);
        resp = ops_response(s, r, json::object(), /*with_tess=*/true);
        tris = attached_triangle_count(r.shapes);
    }
    resp["ok"] = true;
    resp["quality"] = ::geom::display_quality_name(q);
    resp["triangles"] = tris;
    return resp;  // view-only: no revision bump, no dirty flag
}

// ============================================================ simulation studies
// FEA/CFD study setups live as a top-level `studies` array in the document JSON so
// they persist inside the .step+ container automatically (stepplus_save dumps the
// whole doc). The core treats each study as OPAQUE JSON — only the `id` is core-
// assigned — and NEVER rebuilds geometry, so these RPCs are cheap and work for both
// part and assembly docs. The host fills/interprets every other field.

json doc_sim_study_list(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json resp;
    resp["studies"] = s.doc.value("studies", json::array());
    resp["revision"] = s.revision;
    return resp;  // read-only: no revision bump
}

json doc_sim_study_add(const std::string& doc_id, const json& study) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json entry = study.is_object() ? study : json::object();
    std::string id = entry.value("id", std::string());
    if (id.empty()) {
        id = new_study_id();
        entry["id"] = id;
    }
    if (!s.doc.contains("studies") || !s.doc["studies"].is_array())
        s.doc["studies"] = json::array();
    s.doc["studies"].push_back(entry);
    push_undo(s);
    s.revision += 1;
    s.dirty = true;
    json resp;
    resp["studies"] = s.doc["studies"];
    resp["revision"] = s.revision;
    resp["study_id"] = id;
    return resp;
}

json doc_sim_study_edit(const std::string& doc_id, const std::string& study_id,
                        const json& study) {
    DocLock lk;
    Session& s = require_session(doc_id);
    bool found = false;
    if (s.doc.contains("studies") && s.doc["studies"].is_array()) {
        for (json& el : s.doc["studies"]) {
            if (el.is_object() && el.value("id", std::string()) == study_id) {
                json replacement = study.is_object() ? study : json::object();
                replacement["id"] = study_id;  // id can't be lost or changed
                el = std::move(replacement);
                found = true;
                break;
            }
        }
    }
    if (!found)
        throw OpError("invalid_op", "no such study: " + study_id,
                      {{"reason", "unknown study"}, {"study_id", study_id}});
    push_undo(s);
    s.revision += 1;
    s.dirty = true;
    json resp;
    resp["studies"] = s.doc["studies"];
    resp["revision"] = s.revision;
    return resp;
}

json doc_sim_study_delete(const std::string& doc_id, const std::string& study_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    bool found = false;
    if (s.doc.contains("studies") && s.doc["studies"].is_array()) {
        json kept = json::array();
        for (const json& el : s.doc["studies"]) {
            if (el.is_object() && el.value("id", std::string()) == study_id) {
                found = true;
                continue;
            }
            kept.push_back(el);
        }
        if (found) s.doc["studies"] = std::move(kept);
    }
    if (!found)
        throw OpError("invalid_op", "no such study: " + study_id,
                      {{"reason", "unknown study"}, {"study_id", study_id}});
    push_undo(s);
    s.revision += 1;
    s.dirty = true;
    json resp;
    resp["studies"] = s.doc["studies"];
    resp["revision"] = s.revision;
    return resp;
}

// World-placed component bodies for interference / BOM (port of session.py::
// asm_world_bodies / asm_component_bodies). Returned as JSON metadata + a temp STEP
// export per body is overkill; instead we surface per-body world bbox + name, which is
// what the parity harness compares and what eval_interference's assembly branch consumes.
json doc_asm_world_bodies(const std::string& doc_id, bool explode) {
    DocLock lk;
    Session& s = require_session(doc_id);
    // Match gather_export_shapes / STEP traversal: one body can contain several solids.
    int next_source_ordinal = 1;
    const auto source_ordinals = [&next_source_ordinal](const TopoDS_Shape& shape) {
        json ordinals = json::array();
        for (TopExp_Explorer ex(shape, TopAbs_SOLID); ex.More(); ex.Next())
            ordinals.push_back(next_source_ordinal++);
        return ordinals;
    };
    if (!s.is_assembly()) {
        // Multi-body PART fallback (Issue 9). A part assembled via import_part (several
        // imported + transform-placed bodies) is not a formal assembly doc, but the caller
        // still wants to see where each body sits in the shared frame. Compile and report each
        // surviving body's bbox — already in world coordinates (a part's transforms place
        // bodies absolutely). explode is an assembly-only concept here, so it is ignored.
        CompileResult r = compile_session(s);
        json out = json::array();
        for (std::size_t i = 0; i < r.shapes.size(); ++i) {
            const TopoDS_Shape& world = r.shapes[i];
            if (world.IsNull()) continue;
            Bnd_Box box;
            BRepBndLib::Add(world, box);
            json bb = nullptr;
            if (!box.IsVoid()) {
                double xa, ya, za, xb, yb, zb;
                box.Get(xa, ya, za, xb, yb, zb);
                bb = {{"min", {xa, ya, za}}, {"max", {xb, yb, zb}},
                      {"size", {xb - xa, yb - ya, zb - za}}};
            }
            const std::string nm =
                i < r.bodies_info.size() ? r.bodies_info[i].name : ("Body" + std::to_string(i + 1));
            out.push_back({{"name", nm}, {"component", nm}, {"bbox", bb},
                           {"source_ordinals", source_ordinals(world)}});
        }
        return json{{"bodies", out}, {"multibody_part", true}};
    }
    if (!s.asm_result) rebuild_assembly(s);
    std::map<std::string, Eigen::Vector3d> offsets;
    if (explode && s.explode_active) offsets = explode_offsets(s.doc, s.explode_factor);
    json out = json::array();
    if (s.asm_result)
        for (const AsmBodyOut& body : s.asm_result->bodies) {
            std::optional<Eigen::Vector3d> off;
            auto oit = offsets.find(body.component_id);
            if (oit != offsets.end()) off = oit->second;
            TopoDS_Shape world = placed_world_shape(body, off);
            Bnd_Box box;
            BRepBndLib::Add(world, box);
            json bb = nullptr;
            if (!box.IsVoid()) {
                double xa, ya, za, xb, yb, zb;
                box.Get(xa, ya, za, xb, yb, zb);
                bb = {{"min", {xa, ya, za}}, {"max", {xb, yb, zb}},
                      {"size", {xb - xa, yb - ya, zb - za}}};
            }
            out.push_back({{"name", body.name}, {"component", body.component_id}, {"bbox", bb},
                           {"source_ordinals", source_ordinals(world)}});
        }
    return json{{"bodies", out}};
}

json doc_asm_bom_components(const std::string& doc_id) {
    // Non-throwing: a missing / part / closed source returns an empty array so drawing_bom
    // degrades to an empty BOM rather than crashing (mirrors the oracle's try/except).
    DocLock lk;
    if (!pin_session(doc_id)) return json::array();
    Session& s = lk.acquire(doc_id);
    if (!s.is_assembly()) return json::array();
    if (!s.asm_result) rebuild_assembly(s);
    if (!s.asm_result) return json::array();
    // Count component-pattern instances seeded from each real component ("<seed>#<k>").
    std::map<std::string, int> instance_counts;
    for (const AsmBodyOut& b : s.asm_result->bodies) {
        auto h = b.component_id.find('#');
        if (h != std::string::npos) instance_counts[b.component_id.substr(0, h)] += 1;
    }
    // Source path per real component id from doc["components"].
    std::map<std::string, std::string> src_by_id;
    if (s.doc.contains("components") && s.doc["components"].is_array())
        for (const json& c : s.doc["components"]) {
            std::string path = (c.contains("source") && c["source"].is_object())
                                   ? c["source"].value("path", std::string())
                                   : std::string();
            src_by_id[c.value("id", std::string())] = path;
        }
    json out = json::array();
    for (const AsmBodyOut& b : s.asm_result->bodies) {
        if (b.component_id.find('#') != std::string::npos) continue;  // pattern instance
        auto ic = instance_counts.find(b.component_id);
        auto sp = src_by_id.find(b.component_id);
        out.push_back({{"component", b.component_id},
                       {"name", b.name},
                       {"source", sp == src_by_id.end() ? std::string() : sp->second},
                       {"quantity", 1 + (ic == instance_counts.end() ? 0 : ic->second)}});
    }
    return out;
}

bool doc_close(const std::string& doc_id) {
    // Pin the session WITHOUT erasing it yet — the erase only happens once we know the
    // session isn't mid-operation (see try_lock below).
    std::shared_ptr<Session> pin;
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        auto it = g_sessions.find(doc_id);
        if (it == g_sessions.end()) return false;
        pin = it->second;
    }
    // WHY: the old code took a BLOCKING lock on pin->mtx, then unconditionally erased and
    // tore the session down the instant it woke — so a genuinely in-flight op (e.g. a
    // cross-doc dependency resolve via resolve_source_session, invisible to hostd's
    // attach-based reapability check) only DELAYED the close instead of preventing it, and
    // the caller's just-finished work was destroyed anyway. try_lock instead: if the session
    // is busy, doc_close makes NO change (never erases, never tears down) and reports a
    // typed error so the caller can treat this doc as still open.
    std::unique_lock<std::mutex> doc(pin->mtx, std::try_to_lock);
    if (!doc.owns_lock())
        throw OpError("doc_busy", "document " + doc_id + " has an operation in flight",
                      {{"doc_id", doc_id}});
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        auto it = g_sessions.find(doc_id);
        if (it == g_sessions.end()) return false;  // closed by a racing caller meanwhile
        g_sessions.erase(it);
    }
    // Drop any on-disk undo journal stubs for this doc before the session dies.
    for (const std::string& e : pin->undo_stack) drop_history_entry(e);
    for (const std::string& e : pin->redo_stack) drop_history_entry(e);
    pin->undo_stack.clear();
    pin->redo_stack.clear();
    doc.unlock();
    // Session teardown happens when `pin` goes out of scope below.
    wipe_undo_journal(doc_id);
    // T-PERF10: free AICM shared-memory objects for this doc (all revisions).
    chudware::aicm_handoff::release_doc(doc_id);
    return true;
}

json doc_list() {
    // Snapshot under registry, then lock each session in sorted order to copy
    // summary fields (T-PERF5: never hold registry while waiting on a doc mutex).
    std::vector<std::pair<std::string, std::shared_ptr<Session>>> pins;
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        pins.reserve(g_sessions.size());
        for (const auto& kv : g_sessions) pins.emplace_back(kv.first, kv.second);
    }
    std::sort(pins.begin(), pins.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });
    json docs = json::array();
    for (const auto& [id, sp] : pins) {
        std::lock_guard<std::mutex> doc(sp->mtx);
        const std::string schema =
            sp->doc.is_object() ? sp->doc.value("schema", std::string()) : std::string();
        const bool isAsm = schema == "aicad.asm/1";
        int feature_count = 0;
        int component_count = 0;
        if (sp->doc.is_object()) {
            if (sp->doc.contains("features") && sp->doc["features"].is_array())
                feature_count = (int)sp->doc["features"].size();
            if (sp->doc.contains("components") && sp->doc["components"].is_array())
                component_count = (int)sp->doc["components"].size();
        }
        // Empty = no authored content worth recovering. Material/name alone don't count.
        // Assemblies also need no components and no mates.
        bool empty = feature_count == 0;
        if (isAsm) {
            int mate_count = 0;
            if (sp->doc.is_object() && sp->doc.contains("mates") && sp->doc["mates"].is_array())
                mate_count = (int)sp->doc["mates"].size();
            empty = feature_count == 0 && component_count == 0 && mate_count == 0;
        }
        docs.push_back({
            {"doc_id", sp->doc_id},
            {"name", sp->doc.is_object() ? sp->doc.value("name", sp->doc_id) : sp->doc_id},
            {"kind", isAsm ? "assembly" : "part"},
            {"path", sp->path.empty() ? json(nullptr) : json(sp->path)},
            {"revision", sp->revision},
            {"dirty", sp->dirty},
            {"feature_count", feature_count},
            {"component_count", component_count},
            {"empty", empty},
        });
    }
    return json{{"docs", docs}};
}

json doc_checkpoint(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    const std::string cid = new_checkpoint_id();
    s.checkpoints[cid] = s.doc.dump();
    // Cap retained PM-cancel snapshots. Checkpoint ids are zero-padded monotonic
    // hex, so the map's own key order IS mint order — erasing begin() drops the
    // oldest. Bounds the leak from the commit path, which never frees its snapshot.
    while (s.checkpoints.size() > kCheckpointLimit)
        s.checkpoints.erase(s.checkpoints.begin());
    return json{{"checkpoint", cid}};
}

json doc_revert(const std::string& doc_id, const std::string& checkpoint_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    auto it = s.checkpoints.find(checkpoint_id);
    if (it == s.checkpoints.end())
        throw OpError("invalid_op", "no such checkpoint '" + checkpoint_id + "'",
                      {{"reason", "unknown checkpoint"}, {"checkpoint", checkpoint_id}});
    // One undo entry of the pre-revert state (a single undo restores it); clear redo
    // like any edit. Then swap in the snapshot, rebuild, and bump the revision.
    s.undo_stack.push_back(s.doc.dump());
    trim_history(s.doc_id, s.undo_stack);
    for (const std::string& e : s.redo_stack) drop_history_entry(e);
    s.redo_stack.clear();
    // Consume the checkpoint: every caller mints a fresh one per PM open and reverts
    // it at most once (sketch3d_mode, doc_mirror), so freeing it here reclaims the
    // snapshot immediately instead of waiting for the kCheckpointLimit cap to evict it.
    const std::string snapshot = std::move(it->second);
    s.checkpoints.erase(it);
    s.doc = json::parse(snapshot);
    s.revision += 1;
    s.dirty = true;
    CompileResult r = compile_session(s);
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

// Live derived re-pull — C++ port of engine.doc.session.refresh_derived (Wave 4C).
// Re-bundles each derived group's source feature (+ closure) from the LIVE source session
// and splices it back over the existing derived copies in place (ids + positions preserved).
// `id` empty == refresh every derived group; otherwise narrow to the group that feature `id`
// belongs to (a non-derived id is a clean invalid_op). Mutating: one undo snapshot + revision
// bump on any change. A closed source / deleted source feature leaves the group at last-good
// and stamps derived_stale (never crashes). Returns the ops-response + a `derived` summary.
json doc_refresh_derived(const std::string& doc_id, const std::string& id) {
    DocLock lk;
    Session& s = require_session(doc_id);

    // _derived_groups: group -> {source_doc_id, source_doc_name, members:[...]}, first-seen order.
    std::vector<std::string> group_order;
    std::map<std::string, json> groups;
    if (s.doc.contains("features") && s.doc["features"].is_array())
        for (const json& f : s.doc["features"]) {
            if (!f.is_object()) continue;
            if (!f.contains("derived_from") || !f["derived_from"].is_object()) continue;
            const json& df = f["derived_from"];
            if (!df.contains("group") || !df["group"].is_string()) continue;
            const std::string grp = df["group"].get<std::string>();
            if (!groups.count(grp)) {
                group_order.push_back(grp);
                groups[grp] = {{"group", grp},
                               {"source_doc_id", df.contains("doc_id") ? df["doc_id"] : json(nullptr)},
                               {"source_doc_name", df.contains("doc_name") ? df["doc_name"] : json(nullptr)},
                               {"members", json::array()}};
            }
            groups[grp]["members"].push_back(
                {{"id", f.contains("id") ? f["id"] : json(nullptr)},
                 {"source_feature", df.contains("source_feature") ? df["source_feature"] : json(nullptr)},
                 {"is_root", df.value("is_root", false)}});
        }

    if (!id.empty()) {
        const json* target = find_feature_c(s.doc, id);
        const json* df = (target && target->contains("derived_from") && (*target)["derived_from"].is_object())
                             ? &(*target)["derived_from"] : nullptr;
        std::string grp = (df && df->contains("group") && (*df)["group"].is_string())
                              ? (*df)["group"].get<std::string>() : "";
        if (grp.empty() || !groups.count(grp))
            throw OpError("invalid_op", "refresh_derived: feature '" + id + "' is not a derived feature",
                          {{"reason", "not derived"}, {"id", id}});
        std::vector<std::string> only{grp};
        std::map<std::string, json> narrowed{{grp, groups[grp]}};
        group_order = only;
        groups = narrowed;
    }

    if (groups.empty()) {
        CompileResult r = compile_session(s);
        json resp = ops_response(s, r, json::object(), /*with_tess=*/true);
        resp["derived"] = {{"groups", json::array()}, {"note", "no derived features in document"}};
        return resp;
    }

    // Snapshot once up front so the whole multi-group refresh is a SINGLE undo step.
    push_undo(s);
    IdGen gen(s.doc);
    json summaries = json::array();
    bool any_change = false;

    for (const std::string& grp : group_order) {
        const json& rec = groups[grp];
        const json src_doc_id = rec["source_doc_id"];
        Session* src = resolve_source_session(src_doc_id);
        if (src == nullptr) {
            json marked = mark_group_stale(s, grp, "source_doc_closed",
                                           {{"doc_id", src_doc_id}, {"doc_name", rec["source_doc_name"]}});
            any_change = any_change || !marked.empty();
            summaries.push_back({{"group", grp}, {"status", "source_doc_closed"},
                                 {"source_doc_id", src_doc_id}, {"stale", marked}});
            continue;
        }
        const std::string root_src = group_root_source(rec, src->doc);
        if (root_src.empty() || find_feature_c(src->doc, root_src) == nullptr) {
            json marked = mark_group_stale(s, grp, "source_feature_deleted",
                                           {{"doc_id", src_doc_id}, {"doc_name", rec["source_doc_name"]}});
            any_change = any_change || !marked.empty();
            summaries.push_back({{"group", grp}, {"status", "source_feature_deleted"},
                                 {"source_doc_id", src_doc_id}, {"stale", marked}});
            continue;
        }
        try {
            json bundle = build_feature_bundle(src->doc, root_src, /*include_dependencies=*/true);
            json res = refresh_derived_group(s.doc, grp, bundle, gen);
            any_change = true;
            json summ = {{"group", grp}, {"status", "refreshed"}, {"source_doc_id", src_doc_id}};
            summ.update(res);
            summaries.push_back(summ);
        } catch (const OpError& exc) {
            json marked = mark_group_stale(s, grp, "refresh_failed",
                                           {{"doc_id", src_doc_id}, {"error", exc.what()}});
            any_change = any_change || !marked.empty();
            summaries.push_back({{"group", grp}, {"status", "refresh_failed"},
                                 {"source_doc_id", src_doc_id}, {"error", exc.what()}, {"stale", marked}});
        }
    }

    // Validate the spliced doc; on failure ROLL BACK (pop our snapshot, restore) and raise.
    std::vector<std::string> errs = validate_document(s.doc);
    if (!errs.empty()) {
        std::string prior = s.undo_stack.back();
        s.undo_stack.pop_back();
        s.doc = json::parse(prior);
        std::string joined;
        for (std::size_t i = 0; i < errs.size() && i < 5; ++i) {
            if (i) joined += "; ";
            joined += errs[i];
        }
        throw OpError("invalid_op", "refresh_derived produced an invalid document: " + joined,
                      {{"reason", errs.front()}, {"all_errors", errs}});
    }

    if (!any_change) {
        // Pure no-op: drop the snapshot we pushed and do NOT bump the revision.
        s.undo_stack.pop_back();
        CompileResult r = compile_session(s);
        json resp = ops_response(s, r, json::object(), /*with_tess=*/true);
        resp["derived"] = {{"groups", summaries}};
        return resp;
    }

    if (s.active_sketch) s.active_sketch->solve_state.reset();
    s.revision += 1;
    s.dirty = true;
    CompileResult r = compile_session(s);
    json resp = ops_response(s, r, json::object(), /*with_tess=*/true);
    resp["derived"] = {{"groups", summaries}};
    return resp;
}

json doc_config_list(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json names = json::array();
    std::string active;
    const json& configs = s.doc.contains("configurations") ? s.doc["configurations"] : json();
    if (configs.is_array() && !configs.empty()) {
        for (const json& c : configs)
            if (c.is_object()) names.push_back(c.value("name", json(nullptr)));
        active = config_active_name(s.doc);
    } else {
        names.push_back("Default");
        active = "Default";
    }
    json detail = json::array();
    if (configs.is_array())
        for (const json& c : configs)
            if (c.is_object())
                detail.push_back({{"name", c.value("name", json(nullptr))},
                                  {"param_overrides", c.contains("param_overrides") && c["param_overrides"].is_object()
                                                          ? c["param_overrides"] : json::object()},
                                  {"suppressions", c.contains("suppressions") && c["suppressions"].is_object()
                                                       ? c["suppressions"] : json::object()}});
    if (detail.empty())
        detail.push_back({{"name", "Default"}, {"param_overrides", json::object()},
                          {"suppressions", json::object()}});
    return json{{"configurations", names}, {"active", active}, {"detail", detail}};
}

json doc_config_add(const std::string& doc_id, const std::string& name, const json& copy_from) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (name.empty())
        throw OpError("invalid_op", "config_add: 'name' must be a non-empty string",
                      {{"reason", "bad name"}});
    json working = s.doc;
    json& configs = ensure_configs(working);
    for (const json& c : configs)
        if (c.is_object() && c.value("name", std::string()) == name)
            throw OpError("invalid_op", "config_add: configuration '" + name + "' already exists",
                          {{"reason", "duplicate name"}, {"name", name}});
    json overrides = json::object();
    json suppressions = json::object();
    if (copy_from.is_string()) {
        json& src = find_config(working, copy_from.get<std::string>());
        if (src.contains("param_overrides") && src["param_overrides"].is_object())
            overrides = src["param_overrides"];
        if (src.contains("suppressions") && src["suppressions"].is_object())
            suppressions = src["suppressions"];
    }
    configs.push_back({{"name", name}, {"param_overrides", overrides}, {"suppressions", suppressions}});
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

json doc_config_delete(const std::string& doc_id, const std::string& name) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json working = s.doc;
    const bool have = working.contains("configurations") && working["configurations"].is_array() &&
                      !working["configurations"].empty();
    if (!have)
        throw OpError("invalid_op", "config_delete: no configuration '" + name + "' to delete",
                      {{"reason", "no configurations"}});
    json& configs = working["configurations"];
    bool exists = false;
    for (const json& c : configs)
        if (c.is_object() && c.value("name", std::string()) == name) { exists = true; break; }
    if (!exists)
        throw OpError("invalid_op", "config_delete: no configuration named '" + name + "'",
                      {{"reason", "unknown configuration"}, {"name", name}});
    if (configs.size() == 1)
        throw OpError("invalid_op", "config_delete: cannot delete the only configuration",
                      {{"reason", "last configuration"}});
    bool was_active = config_active_name(working) == name;
    json kept = json::array();
    for (const json& c : configs)
        if (!(c.is_object() && c.value("name", std::string()) == name)) kept.push_back(c);
    working["configurations"] = kept;
    if (was_active)
        working["active_configuration"] = kept[0].value("name", std::string());
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

json doc_config_activate(const std::string& doc_id, const std::string& name) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json working = s.doc;
    ensure_configs(working);
    find_config(working, name);  // throws if unknown
    working["active_configuration"] = name;
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

json doc_config_set_override(const std::string& doc_id, const std::string& name,
                             const std::string& feature_id, const std::string& param,
                             const json& value, bool clear) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json working = s.doc;
    json& cfg = find_config(working, name);
    if (!cfg.contains("param_overrides") || !cfg["param_overrides"].is_object())
        cfg["param_overrides"] = json::object();
    const std::string key = feature_id + "." + param;
    if (clear || value.is_null())
        cfg["param_overrides"].erase(key);
    else
        cfg["param_overrides"][key] = value;
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

json doc_config_set_suppression(const std::string& doc_id, const std::string& name,
                                const std::string& feature_id, bool suppressed) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json working = s.doc;
    json& cfg = find_config(working, name);
    if (!cfg.contains("suppressions") || !cfg["suppressions"].is_object())
        cfg["suppressions"] = json::object();
    if (suppressed)
        cfg["suppressions"][feature_id] = true;
    else
        cfg["suppressions"].erase(feature_id);
    validate_config_edit(working);
    return config_commit(s, std::move(working));
}

// Resolve a pick in an ASSEMBLY: body_id = component ordinal, topo_id indexes that
// component's LOCAL topology (TopExp order is transform-invariant, so the world AICM ids
// match local ids). Returns {selector, component} so the host can build a mate ref
// {component, selector}. Port of session.py::_resolve_pick_assembly.
json resolve_pick_assembly(Session& s, int body_id, const std::string& kind, int topo_id) {
    if (!s.asm_result) rebuild_assembly(s);
    const AssemblyResult* ares = s.asm_result.get();
    if (!ares || body_id < 1 || body_id > static_cast<int>(ares->bodies.size()))
        throw OpError("invalid_op",
                      "body_id " + std::to_string(body_id) + " out of range for assembly",
                      {{"reason", "bad body_id"}});
    const AsmBodyOut& abody = ares->bodies[body_id - 1];
    const TopoDS_Shape& shape_local = abody.shape_local;

    if (kind == "body") {
        return json{{"selector", {{"kind", "body"}, {"owner_feature", ""}}},
                    {"component", abody.component_id}};
    }

    // describe_* runs Gauss integration over the WHOLE component; cache it per (component, kind)
    // so repeated clicks in a big assembly don't re-derive it. The cache is tied to the current
    // asm_result — a rebuild swaps the pointer and drops the stale entries.
    if (s.asm_describe_for != static_cast<const void*>(ares)) {
        s.asm_describe_cache.clear();
        s.asm_describe_for = static_cast<const void*>(ares);
    }
    const std::string ckey = std::to_string(body_id) + ":" + kind;
    auto cit = s.asm_describe_cache.find(ckey);
    if (cit == s.asm_describe_cache.end()) {
        std::vector<TopoInfo> computed;
        if (kind == "face") computed = describe_faces(shape_local);
        else if (kind == "edge") computed = describe_edges(shape_local);
        else if (kind == "vertex") computed = describe_vertices(shape_local);
        else
            throw OpError("invalid_op", "kind '" + kind + "' must be face|edge|vertex|body",
                          {{"reason", "bad kind"}});
        cit = s.asm_describe_cache.emplace(ckey, std::move(computed)).first;
    }
    const std::vector<TopoInfo>& infos = cit->second;
    if (topo_id < 1 || topo_id > static_cast<int>(infos.size()))
        throw OpError("invalid_op",
                      "topo_id " + std::to_string(topo_id) + " out of range (1.." +
                          std::to_string(infos.size()) + ") for " + kind,
                      {{"reason", "bad topo_id"}});
    TopoInfo info = infos[topo_id - 1];
    info.hint_index = topo_id - 1;
    return json{{"selector", make_selector(info)}, {"component", abody.component_id}};
}

json doc_resolve_pick(const std::string& doc_id, int body_id, const std::string& kind,
                      int topo_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.is_assembly())
        return resolve_pick_assembly(s, body_id, kind, topo_id);
    json sel = resolve_pick(ensure_compiled(s), body_id, kind, topo_id);
    return json{{"selector", sel}};
}

// SolidWorks "fillet a whole face": the host expands a face pick into the face's edge ids, then
// resolves each to an edge selector. Returns {"edges": [topo_id, ...]} in the host's edge id space.
json doc_face_edges(const std::string& doc_id, int body_id, int face_topo_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.is_assembly()) return json{{"edges", json::array()}};  // not supported for assemblies
    std::vector<int> e = face_edges(ensure_compiled(s), body_id, face_topo_id);
    return json{{"edges", e}};
}

// ----------------------------------------------------------------- C19 sim bridge
// Port of engine/sim/bridge.py (the doc-side sim-input producers). doc_export_step
// writes the session's surviving solid bodies to a STEP (or STL) file for the FEA
// mesher / CFD voxeliser; doc_face_points resolves a face selector against the LAST
// body and samples that face's triangulation nodes in WORLD space. Both rebuild the
// document on demand (compile_document is the C++ equivalent of session.last_result /
// _ensure_built — a fresh compile yields the same surviving bodies).

namespace {

// bbox diagonal of `shape`, floored at 1e-3, matching engine/sim/bridge.py::_bbox_diag.
// That helper takes build123d's shape.bounding_box(), which (optimal=True default) uses
// BRepBndLib::AddOptimal — the PRECISE bounding box, NOT the coarse BRepBndLib::Add that
// picks.cpp::bbox_diag uses. AddOptimal first cleans any cached mesh (BRepTools::Clean),
// exactly as BoundBox.from_topo_ds does. Floor 1e-3; returns 100.0 on a void/failed box,
// mirroring the Python except-branch.
double sim_bbox_diag(const TopoDS_Shape& shape) {
    try {
        BRepTools::Clean(shape);  // drop any cached triangulation (matches from_topo_ds)
        Bnd_Box box;
        BRepBndLib::AddOptimal(shape, box);
        if (box.IsVoid()) return 100.0;
        double xmin, ymin, zmin, xmax, ymax, zmax;
        box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
        double dx = xmax - xmin, dy = ymax - ymin, dz = zmax - zmin;
        double d = std::sqrt(dx * dx + dy * dy + dz * dz);
        return std::max(d, 1e-3);
    } catch (const Standard_Failure&) {
        return 100.0;
    }
}

// Sample a face's triangulation nodes in WORLD space — port of bridge.py::
// _sample_face_nodes. BRepMesh_IncrementalMesh(shape, 0.5, false, 0.5, true) ensures a
// triangulation; BRep_Tool::Triangulation yields the Poly_Triangulation; nodes are
// transformed by the location when it is not the identity. Returns an empty vector when
// there is no triangulation.
std::vector<std::array<double, 3>> sample_face_nodes(const TopoDS_Face& face) {
    BRepMesh_IncrementalMesh mesher(face, 0.5, Standard_False, 0.5, Standard_True);
    mesher.Perform();

    TopLoc_Location loc;
    Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
    std::vector<std::array<double, 3>> out;
    if (tri.IsNull()) return out;

    const gp_Trsf trsf = loc.Transformation();
    const bool is_identity = loc.IsIdentity();
    out.reserve(static_cast<std::size_t>(tri->NbNodes()));
    for (int ni = 1; ni <= tri->NbNodes(); ++ni) {
        gp_Pnt nd = tri->Node(ni);
        if (!is_identity) nd.Transform(trsf);
        out.push_back({nd.X(), nd.Y(), nd.Z()});
    }
    return out;
}

// engine/doc/evaluate.py::_is_surface_shape — true when `shape` has no enclosed volume:
// an open SHELL / lone FACE / WIRE, or a SOLID/COMPSOLID whose VolumeProperties mass is
// ≈0 while its SurfaceProperties area is positive (a degenerate sheet). A COMPOUND that
// wraps any SOLID is NOT a surface. Defaults to the solid path (false) on OCCT errors.
// out_vp (optional): receives the VolumeProperties of a SOLID/COMPSOLID so a caller (eval_mass)
// can REUSE it instead of recomputing the same volume integral. *out_vp_computed reports whether
// it was filled (only the SOLID/COMPSOLID branch computes it).
bool eval_is_surface_shape(const TopoDS_Shape& shape, GProp_GProps* out_vp = nullptr,
                           bool* out_vp_computed = nullptr) {
    if (out_vp_computed) *out_vp_computed = false;
    try {
        const TopAbs_ShapeEnum st = shape.ShapeType();
        if (st == TopAbs_SOLID || st == TopAbs_COMPSOLID) {
            GProp_GProps vp;
            BRepGProp::VolumeProperties(shape, vp);
            if (out_vp) { *out_vp = vp; if (out_vp_computed) *out_vp_computed = true; }
            // PERF: a degenerate sheet is only possible when the ENCLOSED VOLUME is ~0. A normal
            // solid (the overwhelming common case, hit per-body on every rebuild) has real volume,
            // so short-circuit BEFORE the expensive SurfaceProperties Gauss area integration over
            // every face — its result is discarded once vp.Mass() is non-zero anyway.
            if (std::fabs(vp.Mass()) >= 1e-9) return false;
            GProp_GProps sp;
            BRepGProp::SurfaceProperties(shape, sp);
            return sp.Mass() > 1e-9;
        }
        if (st == TopAbs_COMPOUND) {
            TopTools_IndexedMapOfShape solids;
            TopExp::MapShapes(shape, TopAbs_SOLID, solids);
            if (solids.Extent() > 0) return false;
        }
        return true;  // FACE / SHELL / WIRE / solid-free compound -> surface report
    } catch (const Standard_Failure&) {
        return false;
    }
}

// engine/doc/evaluate.py::_surface_props — area report for a surface body (zero
// volume/mass/inertia; "mass" of a unit-density surface == its area in mm²).
json eval_surface_props(const TopoDS_Shape& shape, double density_g_cm3) {
    GProp_GProps sp;
    BRepGProp::SurfaceProperties(shape, sp);
    gp_Pnt com = sp.CentreOfMass();
    return json{
        {"is_surface", true},
        {"area_mm2", sp.Mass()},
        {"volume_mm3", 0.0},
        {"mass_g", 0.0},
        {"com", {com.X(), com.Y(), com.Z()}},
        {"inertia", {0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0}},
        {"material", {{"density_g_cm3", density_g_cm3}}},
    };
}

// True only when `shape` is a watertight CLOSED solid (or compound/compsolid of such) —
// i.e. VolumeProperties yields a TRUSTWORTHY CoM / inertia. Bug J4: an open shell that
// slipped past eval_is_surface_shape (e.g. an unsewn import that OCCT still types SOLID, or
// a solid with genuine free edges) makes BRepGProp::VolumeProperties return garbage (CoM at
// z≈0 on a 20 mm-tall part, negative principal moments). We detect this via a SEAM-EXCLUDED
// free-edge census: a closed solid has every edge shared by two faces (seam/degenerate edges
// excepted — those legitimately bound one face). Any genuinely free edge means the volume is
// not enclosed, so the mass props are unreliable. Pure shells/faces are already routed to the
// surface report before this is called; this catches the SOLID-typed-but-not-closed case.
bool eval_is_closed_solid(const TopoDS_Shape& shape) {
    try {
        TopTools_IndexedDataMapOfShapeListOfShape anc;
        TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc);
        TopTools_IndexedMapOfShape edges;
        TopExp::MapShapes(shape, TopAbs_EDGE, edges);
        if (edges.Extent() == 0) return false;  // no faces/edges -> not a solid
        for (int ei = 1; ei <= edges.Extent(); ++ei) {
            const TopoDS_Shape& es = edges.FindKey(ei);
            int nf = anc.Contains(es) ? anc.FindFromKey(es).Extent() : 0;
            if (nf >= 2) continue;
            const TopoDS_Edge& e = TopoDS::Edge(es);
            if (BRep_Tool::Degenerated(e)) continue;
            bool seam = false;
            if (anc.Contains(es)) {
                const TopTools_ListOfShape& faces = anc.FindFromKey(es);
                for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next())
                    if (BRep_Tool::IsClosed(e, TopoDS::Face(it.Value()))) { seam = true; break; }
            }
            if (!seam) return false;  // a real free edge -> open shell
        }
        return true;
    } catch (const Standard_Failure&) {
        return false;  // be conservative: an OCCT error means we can't vouch for closure
    }
}

}  // namespace

// Gather the shapes to hand a file writer. Assembly docs resolve their geometry through
// compile_assembly (loaded components + solved mate poses), NOT compile_document — so an
// assembly export must WORLD-PLACE each component body off the cached AssemblyResult.
// Without this branch an assembly compiles to nothing (compile_document sees no features)
// and every export threw "no bodies to export". No explode offset: an export captures the
// assembled configuration, not the exploded view. Parts fall through to compile_document.
// Mirror of tessellate_session_bodies' assembly branch, but yielding exact B-rep shapes.
static std::vector<TopoDS_Shape> gather_export_shapes(Session& s, json* components = nullptr) {
    if (components) *components = json::array();
    if (s.is_assembly()) {
        // A path-backed descendant can change without changing this document's revision.
        rebuild_assembly(s);
        std::vector<TopoDS_Shape> shapes;
        std::map<std::string, int> solids_by_component;
        const AssemblyResult& ar = *s.asm_result;
        for (const AsmBodyOut& body : ar.bodies) {
            TopoDS_Shape world = placed_world_shape(body, std::nullopt);
            int solids = 0;
            if (!world.IsNull())
                for (TopExp_Explorer ex(world, TopAbs_SOLID); ex.More(); ex.Next()) ++solids;
            if (!solids)
                throw OpError("export_failed", "component '" + body.name + "' has no solids",
                              {{"component", body.component_id}});
            solids_by_component[body.component_id] += solids;
            shapes.push_back(std::move(world));
        }
        for (const json& component : s.doc.value("components", json::array())) {
            if (component.value("suppressed", false)) continue;
            const std::string id = component.value("id", std::string());
            const std::string name = component.value("name", id);
            const json source = component.value("source", json::object());
            const std::string path = source.value("path", source.value("embedded", std::string()));
            std::string failure;
            for (const AsmComponentStatus& st : ar.component_status)
                if (st.id == id && !st.ok) failure = st.error.value_or("component load failed");
            const int solids = solids_by_component[id];
            if (!solids || !failure.empty())
                throw OpError("export_failed", "component '" + name + "' at '" + path +
                              "' contributed no complete geometry: " + failure,
                              {{"component", id}, {"name", name}, {"path", path}});
            if (components)
                components->push_back({{"name", name}, {"solids", solids}, {"source", source}});
        }
        return shapes;
    }
    CompileResult r = compile_session(s);
    return r.shapes;
}

json doc_export_step(const std::string& doc_id, const std::string& path) {
    DocLock lk;
    Session& s = require_session(doc_id);
    std::vector<TopoDS_Shape> shapes = gather_export_shapes(s);  // assembly-aware (world-placed)
    if (shapes.empty())
        throw OpError("export_failed", "document has no solid bodies to export",
                      {{"doc_id", doc_id}});
    // Path resolution: an absolute client/server path is honored verbatim (desktop non-daemon
    // keeps writing under the host appdata cache). Empty OR relative paths get a server-owned
    // temp file under the same chudware_cpp_docs dir publish_doc_aicm uses — required for WASM /
    // daemon hosts where the client would otherwise send a browser-local path hostd can't use.
    // The resolved absolute path is always returned in the response so callers can seed step_path.
    std::string use_path = path;
    {
        const std::filesystem::path p(use_path);
        if (use_path.empty() || !p.is_absolute()) {
            std::filesystem::path dir =
                std::filesystem::temp_directory_path() / "chudware_cpp_docs";
            std::error_code ec;
            std::filesystem::create_directories(dir, ec);
            std::string ext = ".step";
            if (!use_path.empty()) {
                std::string e = p.extension().string();
                if (!e.empty()) ext = e;
            }
            const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                               std::chrono::system_clock::now().time_since_epoch())
                               .count();
            // Keep doc_id filename-safe (ids are already alnum-ish; strip path seps just in case).
            std::string safe = doc_id;
            for (char& c : safe) {
                if (c == '/' || c == '\\' || c == ':' || c == ' ') c = '_';
            }
            use_path =
                (dir / ("export_" + safe + "_" + std::to_string(ms) + ext)).string();
        }
    }
    // Format follows the extension: .stl (case-insensitive) -> binary STL, else STEP.
    // export_bodies handles single-body-as-is / multi-body-compound (as_compound) and
    // parent-dir creation, matching bridge.py::export_step.
    std::string suffix = std::filesystem::path(use_path).extension().string();
    std::transform(suffix.begin(), suffix.end(), suffix.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    const std::string fmt = (suffix == ".stl") ? "stl" : "step";
    std::string out = export_bodies(shapes, use_path, fmt);
    return json{{"path", out}};
}

// ----------------------------------------------------------------- preview render (vision)
// A tiny OCCT-free software rasterizer: project the tessellated solids isometrically, z-buffer
// + Lambert shade, and PNG-encode. Good enough for the agent to SEE its own model and catch
// gross mistakes (missing features, wrong proportions). Not a pretty renderer.
namespace {

void png_put_u32_be(std::string& out, uint32_t v) {
    out.push_back((char)((v >> 24) & 0xFF));
    out.push_back((char)((v >> 16) & 0xFF));
    out.push_back((char)((v >> 8) & 0xFF));
    out.push_back((char)(v & 0xFF));
}
void png_chunk_rgb(std::string& out, const char tag[4], const std::string& data) {
    png_put_u32_be(out, (uint32_t)data.size());
    std::string body(tag, 4);
    body += data;
    out.append(body);
    mz_ulong crc = mz_crc32(MZ_CRC32_INIT, (const unsigned char*)body.data(), body.size());
    png_put_u32_be(out, (uint32_t)crc);
}
std::string encode_png_rgb(int w, int h, const std::vector<unsigned char>& rgb) {
    std::string out;
    out.append("\x89PNG\r\n\x1a\n", 8);
    std::string ihdr;
    png_put_u32_be(ihdr, (uint32_t)w);
    png_put_u32_be(ihdr, (uint32_t)h);
    ihdr.push_back(8); ihdr.push_back(2); ihdr.push_back(0); ihdr.push_back(0); ihdr.push_back(0);
    png_chunk_rgb(out, "IHDR", ihdr);
    std::string raw;
    raw.reserve((size_t)h * (1 + (size_t)w * 3));
    for (int y = 0; y < h; ++y) {
        raw.push_back(0);  // filter: none
        raw.append((const char*)&rgb[(size_t)y * w * 3], (size_t)w * 3);
    }
    mz_ulong bound = mz_compressBound((mz_ulong)raw.size());
    std::vector<unsigned char> comp(bound);
    mz_ulong clen = bound;
    if (mz_compress2(comp.data(), &clen, (const unsigned char*)raw.data(),
                     (mz_ulong)raw.size(), 9) != MZ_OK)
        clen = 0;
    png_chunk_rgb(out, "IDAT", std::string((char*)comp.data(), clen));
    png_chunk_rgb(out, "IEND", std::string());
    return out;
}

struct RV3 { double x, y, z; };
inline RV3 rsub(const RV3& a, const RV3& b) { return {a.x - b.x, a.y - b.y, a.z - b.z}; }
inline RV3 rcross(const RV3& a, const RV3& b) {
    return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x};
}
inline double rdot(const RV3& a, const RV3& b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline RV3 rnorm(const RV3& a) {
    double l = std::sqrt(rdot(a, a));
    return l < 1e-12 ? RV3{0, 0, 0} : RV3{a.x / l, a.y / l, a.z / l};
}

// ---- annotation overlay: a tiny bitmap font + 2D draw helpers so the agent vision-review
// render is LABELLED (an orientation gizmo + named-face callouts) and COLOUR-CODED by face
// orientation, instead of a bare grey blob the vision model has to guess at. -------------

// Classic 5x7 glyphs, column-major: glyph[j] is column j, bit i (0=top) lights row i.
// Covers space, + - . : 0-9 A-Z — enough for the gizmo letters, the TOP/FRONT/... callouts,
// dimensions, and feature names (upper-cased before drawing).
const unsigned char* glyph5x7(char ch) {
    static const unsigned char F[][5] = {
        {0x00,0x00,0x00,0x00,0x00},                  // ' '
        {0x08,0x08,0x3E,0x08,0x08},                  // '+'
        {0x08,0x08,0x08,0x08,0x08},                  // '-'
        {0x00,0x60,0x60,0x00,0x00},                  // '.'
        {0x00,0x36,0x36,0x00,0x00},                  // ':'
        {0x02,0x01,0x51,0x09,0x06},                  // '?'
        {0x3E,0x51,0x49,0x45,0x3E},{0x00,0x42,0x7F,0x40,0x00},  // 0 1
        {0x42,0x61,0x51,0x49,0x46},{0x21,0x41,0x45,0x4B,0x31},  // 2 3
        {0x18,0x14,0x12,0x7F,0x10},{0x27,0x45,0x45,0x45,0x39},  // 4 5
        {0x3C,0x4A,0x49,0x49,0x30},{0x01,0x71,0x09,0x05,0x03},  // 6 7
        {0x36,0x49,0x49,0x49,0x36},{0x06,0x49,0x49,0x29,0x1E},  // 8 9
        {0x7E,0x11,0x11,0x11,0x7E},{0x7F,0x49,0x49,0x49,0x36},  // A B
        {0x3E,0x41,0x41,0x41,0x22},{0x7F,0x41,0x41,0x22,0x1C},  // C D
        {0x7F,0x49,0x49,0x49,0x41},{0x7F,0x09,0x09,0x09,0x01},  // E F
        {0x3E,0x41,0x49,0x49,0x7A},{0x7F,0x08,0x08,0x08,0x7F},  // G H
        {0x00,0x41,0x7F,0x41,0x00},{0x20,0x40,0x41,0x3F,0x01},  // I J
        {0x7F,0x08,0x14,0x22,0x41},{0x7F,0x40,0x40,0x40,0x40},  // K L
        {0x7F,0x02,0x0C,0x02,0x7F},{0x7F,0x04,0x08,0x10,0x7F},  // M N
        {0x3E,0x41,0x41,0x41,0x3E},{0x7F,0x09,0x09,0x09,0x06},  // O P
        {0x3E,0x41,0x51,0x21,0x5E},{0x7F,0x09,0x19,0x29,0x46},  // Q R
        {0x46,0x49,0x49,0x49,0x31},{0x01,0x01,0x7F,0x01,0x01},  // S T
        {0x3F,0x40,0x40,0x40,0x3F},{0x1F,0x20,0x40,0x20,0x1F},  // U V
        {0x7F,0x20,0x18,0x20,0x7F},{0x63,0x14,0x08,0x14,0x63},  // W X
        {0x07,0x08,0x70,0x08,0x07},{0x61,0x51,0x49,0x45,0x43},  // Y Z
    };
    if (ch == ' ') return F[0];
    if (ch == '+') return F[1];
    if (ch == '-') return F[2];
    if (ch == '.') return F[3];
    if (ch == ':') return F[4];
    if (ch == '?') return F[5];
    if (ch >= '0' && ch <= '9') return F[6 + (ch - '0')];
    if (ch >= 'a' && ch <= 'z') ch = static_cast<char>(ch - 'a' + 'A');
    if (ch >= 'A' && ch <= 'Z') return F[16 + (ch - 'A')];
    return F[0];
}

inline void put_px(std::vector<unsigned char>& img, int W, int H, int x, int y,
                   unsigned char r, unsigned char g, unsigned char b) {
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    size_t p = (size_t)y * W + x;
    img[p * 3] = r; img[p * 3 + 1] = g; img[p * 3 + 2] = b;
}

// Draw `text` at (x,y) top-left, each pixel scaled `sc`x, with a 1px dark halo so it stays
// readable over any face colour. Returns the x just past the string.
int blit_text(std::vector<unsigned char>& img, int W, int H, int x, int y,
              const std::string& text, int sc, unsigned char r, unsigned char g, unsigned char b) {
    int cx = x;
    for (char ch : text) {
        const unsigned char* gly = glyph5x7(ch);
        for (int col = 0; col < 5; ++col)
            for (int row = 0; row < 7; ++row)
                if (gly[col] & (1u << row))
                    for (int dy = 0; dy < sc; ++dy)
                        for (int dx = 0; dx < sc; ++dx) {
                            int px = cx + col * sc + dx, py = y + row * sc + dy;
                            // halo first (8-neighbour dark), then the glyph colour on top
                            for (int oy = -1; oy <= 1; ++oy)
                                for (int ox = -1; ox <= 1; ++ox)
                                    put_px(img, W, H, px + ox, py + oy, 20, 20, 20);
                        }
        // second pass: the bright glyph over the halo
        for (int col = 0; col < 5; ++col)
            for (int row = 0; row < 7; ++row)
                if (gly[col] & (1u << row))
                    for (int dy = 0; dy < sc; ++dy)
                        for (int dx = 0; dx < sc; ++dx)
                            put_px(img, W, H, cx + col * sc + dx, y + row * sc + dy, r, g, b);
        cx += 6 * sc;  // 5px glyph + 1px space
    }
    return cx;
}

void draw_line(std::vector<unsigned char>& img, int W, int H, int x0, int y0, int x1, int y1,
               unsigned char r, unsigned char g, unsigned char b, int thick = 1) {
    int dx = std::abs(x1 - x0), dy = -std::abs(y1 - y0);
    int sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1, err = dx + dy;
    while (true) {
        for (int oy = 0; oy < thick; ++oy)
            for (int ox = 0; ox < thick; ++ox) put_px(img, W, H, x0 + ox, y0 + oy, r, g, b);
        if (x0 == x1 && y0 == y1) break;
        int e2 = 2 * err;
        if (e2 >= dy) { err += dy; x0 += sx; }
        if (e2 <= dx) { err += dx; y0 += sy; }
    }
}

void fill_disc(std::vector<unsigned char>& img, int W, int H, int cx, int cy, int rad,
               unsigned char r, unsigned char g, unsigned char b) {
    for (int y = -rad; y <= rad; ++y)
        for (int x = -rad; x <= rad; ++x)
            if (x * x + y * y <= rad * rad) put_px(img, W, H, cx + x, cy + y, r, g, b);
}

// Map an annotation/dimension string onto the 5x7 font's ASCII subset (A-Z 0-9 + - . : space):
// the engine composes callouts with Unicode glyphs (⌀ × ° ± Ø) that the bitmap font can't
// draw, so translate them to ASCII shorthand (⌀->DIA, ×->X, °->DEG, ±->+-) and drop any
// other non-ASCII byte. Used by the drawing renderer's annotation overlay (Bug N-7).
std::string sanitize_label(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (std::size_t i = 0; i < in.size();) {
        unsigned char c = static_cast<unsigned char>(in[i]);
        if (c < 0x80) { out.push_back(in[i]); ++i; continue; }
        // Multibyte UTF-8: recognise the symbols we care about, else skip the sequence.
        if (c == 0xC3 && i + 1 < in.size()) {
            unsigned char d = static_cast<unsigned char>(in[i + 1]);
            if (d == 0x97) out += "X";              // × multiplication sign
            else if (d == 0x98 || d == 0xB8) out += "DIA ";  // Ø/ø
            i += 2; continue;
        }
        if (c == 0xC2 && i + 1 < in.size()) {
            unsigned char d = static_cast<unsigned char>(in[i + 1]);
            if (d == 0xB0) out += " DEG";           // ° degree
            else if (d == 0xB1) out += "+-";        // ± plus-minus
            i += 2; continue;
        }
        if (c == 0xE2 && i + 2 < in.size()) {
            unsigned char d = static_cast<unsigned char>(in[i + 1]);
            unsigned char e = static_cast<unsigned char>(in[i + 2]);
            // The drafting symbols the engine emits in stacked hole callouts + dims, mapped to
            // readable ASCII shorthand so they don't silently vanish from the PNG (the bitmap
            // font has no glyphs for them). ⌀ diameter, ⌴ counterbore, ⌵ countersink, ↧ depth.
            if (d == 0x8C && e == 0x80) { out += "DIA "; i += 3; continue; }   // ⌀ U+2300
            if (d == 0x8C && e == 0xB4) { out += "CBORE "; i += 3; continue; } // ⌴ U+2334
            if (d == 0x8C && e == 0xB5) { out += "CSK "; i += 3; continue; }   // ⌵ U+2335
            if (d == 0x86 && e == 0xA7) { out += "DEPTH "; i += 3; continue; } // ↧ U+21A7
            // fall through to the generic skip for any other 3-byte sequence
        }
        // Unknown multibyte lead: skip this byte and any continuation bytes.
        ++i;
        while (i < in.size() && (static_cast<unsigned char>(in[i]) & 0xC0) == 0x80) ++i;
    }
    return out;
}

// A face callout the renderer overlays: a world anchor (face centroid) + outward normal (for
// back-face culling) + the label text + its colour.
struct RenderLabel {
    RV3 anchor;
    RV3 normal;
    std::string text;
    unsigned char col[3];
};

// Tint a lit face colour toward its principal-orientation hue when its normal is strongly
// axis-aligned (a flat principal face); curved / oblique faces are left neutral. Returns true
// and writes the tint when aligned. Hues match the gizmo: X=red, Y=green, Z=blue, with the
// negative side a distinct sibling (front=-Y yellow vs back=+Y green; left=-X orange vs right
// =+X red; top=+Z blue vs bottom=-Z teal).
bool orient_tint(double nx, double ny, double nz, unsigned char out[3]) {
    double ax = std::abs(nx), ay = std::abs(ny), az = std::abs(nz);
    double m = std::max({ax, ay, az});
    if (m < 0.92) return false;  // not flat-on a principal axis -> leave neutral
    // Tie-break matches selectors.cpp side_word (Z, then X, then Y) — not float equality on m.
    if (az >= ax && az >= ay) {
        if (nz > 0) { out[0]=90; out[1]=140; out[2]=225; }   // top +Z blue
        else        { out[0]=70; out[1]=175; out[2]=185; }   // bottom -Z teal
    } else if (ax >= ay && ax >= az) {
        if (nx > 0) { out[0]=220; out[1]=80; out[2]=80; }    // right +X red
        else        { out[0]=225; out[1]=140; out[2]=70; }   // left -X orange
    } else {
        if (ny > 0) { out[0]=95; out[1]=185; out[2]=95; }    // back +Y green
        else        { out[0]=215; out[1]=200; out[2]=70; }  // front -Y yellow
    }
    return true;
}

// ---- view spec resolution (task 03: on-request viewing angles) --------------------------------
// A legacy camera is a single direction `dir` = scene-center -> camera (the existing code's `d`).
// An exact perspective camera retains its full WCS pose and projection parameters; doc_render_view
// passes it through to the same rasterizer instead of auto-fitting the scene.
struct PerspectiveCamera {
    RV3 position{0, 0, 0};
    RV3 target{0, 0, 0};
    RV3 up{0, 0, 1};       // exact request value; basis derivation normalizes a private copy
    RV3 forward{0, 0, -1}; // position -> target
    RV3 right{1, 0, 0};
    RV3 true_up{0, 1, 0};
    double vertical_fov_deg = 0;
    double near_plane = 0;
    double far_plane = 0;
};

struct ResolvedView {
    RV3 dir;
    std::string name;
    std::optional<PerspectiveCamera> camera;
    std::string refusal_code;
    std::string refusal_reason;
};

inline RV3 dir_from_az_el(double az_deg, double el_deg) {
    // az measured from the -Y (front) axis toward +X (right); el above the XY plane toward +Z.
    // az=0,el=0 -> front (0,-1,0); az=90 -> right (1,0,0); el=90 -> top (0,0,1).
    const double k = 3.14159265358979323846 / 180.0;
    double a = az_deg * k, e = el_deg * k;
    return rnorm({std::cos(e) * std::sin(a), -std::cos(e) * std::cos(a), std::sin(e)});
}

// Resolve a view spec to either the legacy direction camera or an exact perspective pose.
// Existing malformed/unknown legacy specs deliberately retain their ISO fallback. Once `camera`
// is present, however, it is authoritative: malformed or degenerate values are named refusals.
ResolvedView resolve_view_spec(const nlohmann::json& v) {
    auto legacy = [](RV3 dir, std::string name) -> ResolvedView {
        return {dir, std::move(name), std::nullopt, std::string(), std::string()};
    };
    auto refuse = [](const char* code, const char* reason) -> ResolvedView {
        return {{0, 0, 0}, "PERSPECTIVE", std::nullopt, code, reason};
    };
    auto named = [&](const std::string& s) -> ResolvedView {
        std::string n;
        for (char c : s) n += (char)std::toupper((unsigned char)c);
        if (n == "FRONT")  return legacy({0, -1, 0}, "FRONT");
        if (n == "BACK")   return legacy({0, 1, 0}, "BACK");
        if (n == "TOP")    return legacy({0, 0, 1}, "TOP");
        if (n == "BOTTOM") return legacy({0, 0, -1}, "BOTTOM");
        if (n == "RIGHT")  return legacy({1, 0, 0}, "RIGHT");
        if (n == "LEFT")   return legacy({-1, 0, 0}, "LEFT");
        return legacy(rnorm({1.0, 1.0, 0.8}), "ISO");  // iso / unknown
    };
    if (v.is_string()) return named(v.get<std::string>());
    if (v.is_object()) {
        const auto camera_it = v.find("camera");
        if (camera_it != v.end()) {
            if (!camera_it->is_object())
                return refuse("camera_invalid", "view.camera must be an object");
            const json& cj = *camera_it;
            if (cj.contains("projection") &&
                (!cj.at("projection").is_string() ||
                 cj.at("projection").get<std::string>() != "perspective"))
                return refuse("camera_projection_unsupported",
                              "view.camera.projection must be perspective");
            auto vec3 = [](const json& value, RV3& out) {
                if (!value.is_array() || value.size() != 3) return false;
                for (int i = 0; i < 3; ++i)
                    if (!value[i].is_number() ||
                        !std::isfinite(value[i].get<double>())) return false;
                out = {value[0].get<double>(), value[1].get<double>(),
                       value[2].get<double>()};
                return true;
            };
            PerspectiveCamera camera;
            if (!cj.contains("position") || !vec3(cj.at("position"), camera.position) ||
                !cj.contains("target") || !vec3(cj.at("target"), camera.target) ||
                !cj.contains("up") || !vec3(cj.at("up"), camera.up))
                return refuse("camera_invalid",
                              "camera position, target, and up must be finite 3-vectors");
            auto stable_unit = [](RV3 value, RV3& out) {
                const double magnitude = std::max(
                    {std::abs(value.x), std::abs(value.y), std::abs(value.z)});
                if (!(magnitude > 0.0) || !std::isfinite(magnitude)) return false;
                value = {value.x / magnitude, value.y / magnitude, value.z / magnitude};
                const double length = std::sqrt(rdot(value, value));
                if (!(length > 0.0) || !std::isfinite(length)) return false;
                out = {value.x / length, value.y / length, value.z / length};
                return true;
            };
            RV3 forward_raw = rsub(camera.target, camera.position);
            if (!std::isfinite(forward_raw.x) || !std::isfinite(forward_raw.y) ||
                !std::isfinite(forward_raw.z)) {
                const double coordinate_scale = std::max(
                    {std::abs(camera.position.x), std::abs(camera.position.y),
                     std::abs(camera.position.z), std::abs(camera.target.x),
                     std::abs(camera.target.y), std::abs(camera.target.z)});
                forward_raw = {
                    camera.target.x / coordinate_scale - camera.position.x / coordinate_scale,
                    camera.target.y / coordinate_scale - camera.position.y / coordinate_scale,
                    camera.target.z / coordinate_scale - camera.position.z / coordinate_scale,
                };
            }
            if (!stable_unit(forward_raw, camera.forward))
                return refuse("camera_zero_direction",
                              "camera position and target must differ");
            RV3 normalized_up;
            if (!stable_unit(camera.up, normalized_up))
                return refuse("camera_up_degenerate", "camera up must be non-zero");
            const RV3 right_raw = rcross(camera.forward, normalized_up);
            if (!stable_unit(right_raw, camera.right))
                return refuse("camera_up_parallel",
                              "camera up must not be parallel to the view direction");
            camera.true_up = rcross(camera.right, camera.forward);
            if (!cj.contains("vertical_fov_deg") ||
                !cj.at("vertical_fov_deg").is_number()) {
                return refuse("camera_fov_invalid",
                              "camera vertical_fov_deg must be in (0, 179)");
            }
            camera.vertical_fov_deg = cj.at("vertical_fov_deg").get<double>();
            const double tan_half_fov = std::tan(
                camera.vertical_fov_deg * 3.14159265358979323846 / 360.0);
            const double min_tan_for_viewport =
                1024.0 / std::numeric_limits<double>::max();
            if (!std::isfinite(camera.vertical_fov_deg) ||
                !(camera.vertical_fov_deg > 0.0 && camera.vertical_fov_deg < 179.0) ||
                !std::isfinite(tan_half_fov) || !(tan_half_fov >= min_tan_for_viewport))
                return refuse("camera_fov_invalid",
                              "camera vertical_fov_deg must yield a finite focal length");
            if (!cj.contains("near") || !cj.at("near").is_number() ||
                !cj.contains("far") || !cj.at("far").is_number())
                return refuse("camera_clip_range_invalid",
                              "camera clipping range must satisfy 0 < near < far");
            camera.near_plane = cj.at("near").get<double>();
            camera.far_plane = cj.at("far").get<double>();
            if (!std::isfinite(camera.near_plane) || !std::isfinite(camera.far_plane) ||
                !(camera.near_plane > 0.0 && camera.near_plane < camera.far_plane))
                return refuse("camera_clip_range_invalid",
                              "camera clipping range must satisfy 0 < near < far");
            const RV3 scene_to_camera{-camera.forward.x, -camera.forward.y, -camera.forward.z};
            ResolvedView out = legacy(scene_to_camera, "PERSPECTIVE");
            out.camera = camera;
            return out;
        }
        if (v.contains("dir") && v.at("dir").is_array() && v.at("dir").size() >= 3) {
            const auto& a = v.at("dir");
            RV3 dir = rnorm({a[0].get<double>(), a[1].get<double>(), a[2].get<double>()});
            if (rdot(dir, dir) > 1e-9)
                return legacy(dir, v.value("name", std::string("VIEW")));
        }
        if (v.contains("azimuth") || v.contains("elevation")) {
            double az = v.value("azimuth", 0.0), el = v.value("elevation", 0.0);
            RV3 dir = dir_from_az_el(az, el);
            std::string nm = "AZ" + std::to_string((long)std::lround(az)) +
                             " EL" + std::to_string((long)std::lround(el));
            return legacy(dir, nm);
        }
        if (v.contains("view") && v.at("view").is_string())
            return named(v.at("view").get<std::string>());
    }
    return legacy(rnorm({1.0, 1.0, 0.8}), "ISO");
}

// ---- explicit close-up framing (feature emphasis) -------------------------------------
// Auto-fit frames the WHOLE model, so a Ø5 hole in a 120 mm plate lands on ~30 px and every
// consumer was forced to crop-and-upscale that patch into mush. `RenderFocus` frames a world
// AABB instead: the rasterizer fits THAT box to the frame, so a close-up is RENDERED at full
// resolution rather than magnified after the fact.
//   margin     — padding as a fraction of the box's projected size (0.15 = 15% each side).
//   clip_near  — drop fragments in FRONT of the box, a deterministic cutaway that lets the
//                camera see into a bore or pocket without a separate section operation.
struct RenderFocus {
    RV3 lo{0, 0, 0}, hi{0, 0, 0};
    double margin = 0.15;
    bool clip_near = false;
};

// The orthographic projection the rasterizer actually used, echoed back so an overlay can
// reproject world points through the EXACT camera instead of re-deriving it and landing a
// pixel (or, on a cropped image, a hundred pixels) off.
struct RenderFrame {
    RV3 center{0, 0, 0}, r{1, 0, 0}, u{0, 1, 0}, d{0, 0, 1};
    double scale = 0, cu = 0, cv = 0;
    int W = 0, H = 0;
    // Camera depth (dot(p - center, d)) of the near-clip plane, +inf when nothing was clipped.
    // An overlay MUST apply the same cut or its depth test disagrees with these pixels: on a
    // cutaway the material the engine removed still wins the overlay's z-test, so the target
    // it was cut to reveal comes back "occluded" and paints nothing.
    double clip_depth = 0;
    bool clipped = false;
};

// Render the tessellated solids from an arbitrary camera direction into an RGB buffer (no file I/O),
// labelled + colour-coded like the legacy iso render. `header`, when non-empty, is blitted top-left
// (view name + units + bbox dims) so a multi-view contact sheet is self-describing. Returns false if
// essentially nothing was drawn. This is the shared core behind render_iso_png / doc_render_view /
// the contact sheet — one renderer, so headless and on-screen stay consistent (CLAUDE.md rule 3).
//
// The three trailing params exist for doc_render_diff and are inert when omitted:
//   `body_alpha`   — per-body 0-255 opacity (parallel to `bodies`). A fragment from a body with
//                    alpha < 255 that PASSES the depth test blends into the existing pixel
//                    (out = (a*new + (255-a)*old)/255) instead of overwriting it, and still writes
//                    the z-buffer. A null/short vector, or alpha 255, is byte-identical to before.
//   `frame_from`   — frame the camera (world centre AND screen fit extents) on THIS body set rather
//                    than on the drawn one, so two renders of DIFFERENT body sets register
//                    pixel-for-pixel. Both fit loops honour it.
//   `min_painted`  — override the "nothing drawn" guard (default W*H/400). A changes_only diff of a
//                    small added boss is legitimately sparse and must not read as a failed render.
// The two after those serve close-up emphasis:
//   `focus`        — frame THIS world AABB instead of auto-fitting the geometry, optionally
//                    clipping away whatever sits in front of it (see RenderFocus).
//   `out_frame`    — receives the resolved projection so an overlay reprojects through the exact
//                    camera that produced these pixels.
//   `background`   — RGB triple (0-255) the frame is cleared to. Null keeps the historical flat
//                    245 light grey, so every legacy render stays byte-identical. The header,
//                    callouts and gizmo all carry their own dark halo/disc, so they stay
//                    readable over a dark field — a caller wanting a film-dark plate passes
//                    the colour instead of colour-keying the grey out afterwards.
bool render_view_to_rgb(const std::vector<aicm::AicmBody>& bodies,
                        const std::vector<RenderLabel>& labels, int W, int H, RV3 viewDir,
                        const std::string& header, std::vector<unsigned char>& outImg,
                        bool annotate = true,
                        const std::vector<unsigned char>* body_alpha = nullptr,
                        const std::vector<aicm::AicmBody>* frame_from = nullptr,
                        int min_painted = -1,
                        const RenderFocus* focus = nullptr,
                        RenderFrame* out_frame = nullptr,
                        const PerspectiveCamera* perspective_camera = nullptr,
                        const unsigned char* background = nullptr) {
    const bool perspective = perspective_camera != nullptr;
    // Camera framing reference: the drawn set unless the caller supplied a stable frame set.
    const std::vector<aicm::AicmBody>& frame_bodies =
        (frame_from && !frame_from->empty()) ? *frame_from : bodies;
    RV3 lo{1e30, 1e30, 1e30}, hi{-1e30, -1e30, -1e30};
    bool any = false;
    for (const auto& b : frame_bodies)
        for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
            lo.x = std::min(lo.x, (double)b.pos[i]);     hi.x = std::max(hi.x, (double)b.pos[i]);
            lo.y = std::min(lo.y, (double)b.pos[i + 1]); hi.y = std::max(hi.y, (double)b.pos[i + 1]);
            lo.z = std::min(lo.z, (double)b.pos[i + 2]); hi.z = std::max(hi.z, (double)b.pos[i + 2]);
            any = true;
        }
    if (!any) return false;
    RV3 c = perspective ? perspective_camera->position
                        : (focus ? RV3{(focus->lo.x + focus->hi.x) / 2,
                                      (focus->lo.y + focus->hi.y) / 2,
                                      (focus->lo.z + focus->hi.z) / 2}
                                 : RV3{(lo.x + hi.x) / 2, (lo.y + hi.y) / 2,
                                       (lo.z + hi.z) / 2});
    RV3 d = perspective
                ? RV3{-perspective_camera->forward.x, -perspective_camera->forward.y,
                      -perspective_camera->forward.z}
                : rnorm(viewDir);  // legacy view dir (scene -> camera)
    if (!perspective && rdot(d, d) < 0.5)
        d = rnorm({1.0, 1.0, 0.8});  // degenerate legacy spec -> iso
    // Legacy views retain their robust world-up choice byte-for-byte. An exact camera instead
    // uses the requested up to derive the same right/true-up basis as CameraPose.
    RV3 upRef = (std::abs(d.z) > 0.985) ? RV3{0, 1, 0} : RV3{0, 0, 1};
    RV3 r = perspective ? perspective_camera->right : rnorm(rcross(upRef, d));
    RV3 u = perspective ? perspective_camera->true_up : rcross(d, r);
    RV3 light = rnorm({0.4, 0.5, 1.0});
    auto proj = [&](double x, double y, double z, double& pu, double& pv, double& pd) {
        RV3 p = rsub({x, y, z}, c);
        pu = rdot(p, r);
        pv = rdot(p, u);
        pd = perspective ? rdot(p, perspective_camera->forward) : rdot(p, d);
    };
    double minu = 1e30, maxu = -1e30, minv = 1e30, maxv = -1e30;
    // Perspective poses are authoritative and never enter either fitting loop.
    if (!perspective && focus) {
        for (int k = 0; k < 8; ++k) {
            double pu, pv, pd;
            proj((k & 1) ? focus->hi.x : focus->lo.x,
                 (k & 2) ? focus->hi.y : focus->lo.y,
                 (k & 4) ? focus->hi.z : focus->lo.z, pu, pv, pd);
            minu = std::min(minu, pu); maxu = std::max(maxu, pu);
            minv = std::min(minv, pv); maxv = std::max(maxv, pv);
        }
        const double span = std::max({maxu - minu, maxv - minv, 1e-3});
        if (maxu - minu < span * 0.02) { double m = (minu + maxu) / 2; minu = m - span * 0.01; maxu = m + span * 0.01; }
        if (maxv - minv < span * 0.02) { double m = (minv + maxv) / 2; minv = m - span * 0.01; maxv = m + span * 0.01; }
        const double pad = std::max(focus->margin, 0.0);
        const double du = (maxu - minu) * pad, dv = (maxv - minv) * pad;
        minu -= du; maxu += du; minv -= dv; maxv += dv;
    } else if (!perspective) {
        for (const auto& b : frame_bodies)
            for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
                double pu, pv, pd; proj(b.pos[i], b.pos[i + 1], b.pos[i + 2], pu, pv, pd);
                minu = std::min(minu, pu); maxu = std::max(maxu, pu);
                minv = std::min(minv, pv); maxv = std::max(maxv, pv);
            }
    }
    const double fit = focus ? 1.0 : 0.85;
    double scale = perspective
        ? (H * 0.5) / std::tan(perspective_camera->vertical_fov_deg *
                               3.14159265358979323846 / 360.0)
        : fit * std::min(W / std::max(maxu - minu, 1e-6),
                         H / std::max(maxv - minv, 1e-6));
    double cu = perspective ? 0.0 : (minu + maxu) / 2;
    double cv = perspective ? 0.0 : (minv + maxv) / 2;
    // Legacy focus cutaway depth. Perspective clipping is handled against near/far below.
    double clip_depth = 1e30;
    if (!perspective && focus && focus->clip_near) {
        double dmax = -1e30;
        for (int k = 0; k < 8; ++k) {
            double pu, pv, pd;
            proj((k & 1) ? focus->hi.x : focus->lo.x,
                 (k & 2) ? focus->hi.y : focus->lo.y,
                 (k & 4) ? focus->hi.z : focus->lo.z, pu, pv, pd);
            dmax = std::max(dmax, pd);
        }
        clip_depth = dmax;
    }
    if (out_frame)
        *out_frame = RenderFrame{c, r, u, d, scale, cu, cv, W, H, clip_depth,
                                 clip_depth < 1e29};
    auto toScreen = [&](double pu, double pv, double pd, double& sx, double& sy) {
        if (perspective) {
            sx = pu * scale / pd + W / 2.0;
            sy = H / 2.0 - pv * scale / pd;
        } else {
            sx = (pu - cu) * scale + W / 2.0;
            sy = H / 2.0 - (pv - cv) * scale;
        }
    };
    // Clear to the requested background (default: the historical flat 245 light grey). A grey
    // triple still takes the single-value fill, so the common case allocates and writes once.
    const unsigned char bg_rgb[3] = {background ? background[0] : (unsigned char)245,
                                     background ? background[1] : (unsigned char)245,
                                     background ? background[2] : (unsigned char)245};
    std::vector<unsigned char> img;
    if (bg_rgb[0] == bg_rgb[1] && bg_rgb[1] == bg_rgb[2]) {
        img.assign((size_t)W * H * 3, bg_rgb[0]);
    } else {
        img.resize((size_t)W * H * 3);
        for (size_t p = 0; p + 2 < img.size(); p += 3) {
            img[p] = bg_rgb[0];
            img[p + 1] = bg_rgb[1];
            img[p + 2] = bg_rgb[2];
        }
    }
    std::vector<double> zbuf((size_t)W * H, -1e30);
    long painted = 0;
    // `lam` is the vertex's diffuse term, carried through clipping so a body that ships
    // per-vertex normals shades SMOOTHLY (Gouraud) instead of one flat tone per triangle.
    struct ProjectedVertex { double pu, pv, pd, lam; };
    auto clip_halfspace = [](const std::vector<ProjectedVertex>& input,
                             const auto& signed_distance) {
        std::vector<ProjectedVertex> output;
        if (input.empty()) return output;
        ProjectedVertex previous = input.back();
        double previous_distance = signed_distance(previous);
        bool previous_inside = previous_distance >= 0.0;
        for (const ProjectedVertex& current : input) {
            const double current_distance = signed_distance(current);
            const bool current_inside = current_distance >= 0.0;
            if (current_inside != previous_inside) {
                const double t = previous_distance /
                                 (previous_distance - current_distance);
                output.push_back({
                    previous.pu + t * (current.pu - previous.pu),
                    previous.pv + t * (current.pv - previous.pv),
                    previous.pd + t * (current.pd - previous.pd),
                    previous.lam + t * (current.lam - previous.lam),
                });
            }
            if (current_inside) output.push_back(current);
            previous = current;
            previous_distance = current_distance;
            previous_inside = current_inside;
        }
        return output;
    };
    // Projection/clipping feeds this one edge-function/depth/blend raster path for both camera
    // kinds. Perspective supplies reciprocal depth; orthographic supplies its legacy linear depth.
    auto raster_triangle = [&](const ProjectedVertex& p0, const ProjectedVertex& p1,
                               const ProjectedVertex& p2, double cr, double cg, double cb,
                               unsigned char balpha) {
        double su0, sv0, su1, sv1, su2, sv2;
        toScreen(p0.pu, p0.pv, p0.pd, su0, sv0);
        toScreen(p1.pu, p1.pv, p1.pd, su1, sv1);
        toScreen(p2.pu, p2.pv, p2.pd, su2, sv2);
        int minx = (int)std::floor(std::min({su0, su1, su2}));
        int maxx = (int)std::ceil(std::max({su0, su1, su2}));
        int miny = (int)std::floor(std::min({sv0, sv1, sv2}));
        int maxy = (int)std::ceil(std::max({sv0, sv1, sv2}));
        minx = std::max(minx, 0); miny = std::max(miny, 0);
        maxx = std::min(maxx, W - 1); maxy = std::min(maxy, H - 1);
        double area = (su1 - su0) * (sv2 - sv0) - (su2 - su0) * (sv1 - sv0);
        if (std::abs(area) < 1e-9) return;
        const double d0 = perspective ? 1.0 / p0.pd : p0.pd;
        const double d1 = perspective ? 1.0 / p1.pd : p1.pd;
        const double d2 = perspective ? 1.0 / p2.pd : p2.pd;
        for (int py = miny; py <= maxy; ++py)
            for (int px = minx; px <= maxx; ++px) {
                double fx = px + 0.5, fy = py + 0.5;
                double a0 = ((su1 - fx) * (sv2 - fy) - (su2 - fx) * (sv1 - fy)) / area;
                double a1 = ((su2 - fx) * (sv0 - fy) - (su0 - fx) * (sv2 - fy)) / area;
                double a2 = 1.0 - a0 - a1;
                if (a0 < 0 || a1 < 0 || a2 < 0) continue;
                double depth = a0 * d0 + a1 * d1 + a2 * d2;
                if (!perspective && depth > clip_depth) continue;
                size_t pi = (size_t)py * W + px;
                if (depth > zbuf[pi]) {
                    zbuf[pi] = depth;
                    // Gouraud: the diffuse term is interpolated, the base colour is flat.
                    const double lam = a0 * p0.lam + a1 * p1.lam + a2 * p2.lam;
                    const unsigned char R = (unsigned char)std::min(255.0, cr * 255 * lam);
                    const unsigned char G = (unsigned char)std::min(255.0, cg * 255 * lam);
                    const unsigned char B = (unsigned char)std::min(255.0, cb * 255 * lam);
                    if (balpha >= 255) {
                        img[pi * 3] = R; img[pi * 3 + 1] = G; img[pi * 3 + 2] = B;
                    } else {
                        const int a = balpha, ia = 255 - balpha;
                        img[pi * 3] = (unsigned char)((a * R + ia * img[pi * 3]) / 255);
                        img[pi * 3 + 1] = (unsigned char)((a * G + ia * img[pi * 3 + 1]) / 255);
                        img[pi * 3 + 2] = (unsigned char)((a * B + ia * img[pi * 3 + 2]) / 255);
                    }
                    ++painted;
                }
            }
    };
    for (size_t bi = 0; bi < bodies.size(); ++bi) {
        const aicm::AicmBody& b = bodies[bi];
        const unsigned char balpha =
            (body_alpha && bi < body_alpha->size()) ? (*body_alpha)[bi] : (unsigned char)255;
        double br = b.color[0], bg = b.color[1], bb = b.color[2];
        // GSL render fix — decide the orientation hue ONCE per B-REP FACE, not per triangle.
        // The old code fed each triangle's own geometric normal to orient_tint, so a single
        // curved face (whose triangles point every which way) came back SPLATTERED with
        // red/green/blue patches wherever a triangle happened to cross the 0.92 axis threshold
        // (the impeller-blade / vase-wall blotch). Now: only a PLANAR FaceRec (surf_type 0) is
        // eligible, and its hue is taken from the face's averaged normal — so a flat principal
        // face is cleanly tinted and every curved face stays neutral grey. Triangles with no
        // FaceRec (raw mesh imports) fall back to the per-triangle path inside the loop.
        const size_t ntri = b.idx.size() / 3;
        std::vector<int> face_of_tri(ntri, -1);
        std::vector<unsigned char> face_tint;   // 3 bytes per FaceRec
        std::vector<char> face_tinted;          // 1 per FaceRec
        face_tint.reserve(b.faces.size() * 3);
        face_tinted.reserve(b.faces.size());
        for (size_t fi = 0; fi < b.faces.size(); ++fi) {
            const aicm::FaceRec& fr = b.faces[fi];
            const size_t last = std::min<size_t>((size_t)fr.first_tri + fr.tri_count, ntri);
            RV3 acc{0, 0, 0};
            size_t used = 0;
            for (size_t tri = fr.first_tri; tri < last; ++tri) {
                face_of_tri[tri] = (int)fi;
                const size_t t = tri * 3;
                const uint32_t a = b.idx[t], c1 = b.idx[t + 1], c2 = b.idx[t + 2];
                if (3u * a + 2 >= b.pos.size() || 3u * c1 + 2 >= b.pos.size() ||
                    3u * c2 + 2 >= b.pos.size())
                    continue;
                RV3 p0{b.pos[3 * a], b.pos[3 * a + 1], b.pos[3 * a + 2]};
                RV3 p1{b.pos[3 * c1], b.pos[3 * c1 + 1], b.pos[3 * c1 + 2]};
                RV3 p2{b.pos[3 * c2], b.pos[3 * c2 + 1], b.pos[3 * c2 + 2]};
                RV3 n = rnorm(rcross(rsub(p1, p0), rsub(p2, p0)));
                acc.x += n.x; acc.y += n.y; acc.z += n.z; ++used;
            }
            unsigned char tint[3] = {0, 0, 0};
            bool tinted = false;
            // Only flat (planar) faces carry an orientation hue; curved faces stay neutral.
            if (fr.surf_type == 0 && used > 0) {
                RV3 mn = rnorm({acc.x, acc.y, acc.z});
                if (orient_tint(mn.x, mn.y, mn.z, tint)) tinted = true;
            }
            face_tint.push_back(tint[0]); face_tint.push_back(tint[1]); face_tint.push_back(tint[2]);
            face_tinted.push_back(tinted ? 1 : 0);
        }
        for (size_t t = 0; t + 2 < b.idx.size(); t += 3) {
            uint32_t i0 = b.idx[t], i1 = b.idx[t + 1], i2 = b.idx[t + 2];
            if (3u * i0 + 2 >= b.pos.size() || 3u * i1 + 2 >= b.pos.size() ||
                3u * i2 + 2 >= b.pos.size())
                continue;
            RV3 w0{b.pos[3 * i0], b.pos[3 * i0 + 1], b.pos[3 * i0 + 2]};
            RV3 w1{b.pos[3 * i1], b.pos[3 * i1 + 1], b.pos[3 * i1 + 2]};
            RV3 w2{b.pos[3 * i2], b.pos[3 * i2 + 1], b.pos[3 * i2 + 2]};
            RV3 fn = rnorm(rcross(rsub(w1, w0), rsub(w2, w0)));
            // Diffuse term per VERTEX when the body carries shading normals (`nrm`, filled by
            // core/geom/tessellate.cpp — smooth across a tessellated cylinder/fillet/tread and
            // split at creases), so a curved band reads as curved instead of as facets. Bodies
            // without normals keep the flat geometric term byte-for-byte.
            const bool smooth = b.nrm.size() == b.pos.size();
            const auto vertex_lam = [&](uint32_t vi) {
                const RV3 n = smooth ? rnorm(RV3{b.nrm[3 * vi], b.nrm[3 * vi + 1],
                                                 b.nrm[3 * vi + 2]})
                                     : fn;
                return 0.35 + 0.65 * std::max(0.0, std::abs(rdot(n, light)));
            };
            const double lam0 = vertex_lam(i0), lam1 = vertex_lam(i1), lam2 = vertex_lam(i2);
            // Orientation colour-coding: blend a flat principal face toward its direction hue
            // (curved/oblique faces stay the neutral body colour) so the vision model can read
            // which way each face points at a glance.
            double cr = br, cg = bg, cb = bb;
            unsigned char tint[3];
            bool use_tint = false;
            const int fidx = (t / 3 < face_of_tri.size()) ? face_of_tri[t / 3] : -1;
            if (fidx >= 0) {
                // Per-FACE decision (kills the curved-face blotch): use the face's precomputed hue.
                if (face_tinted[fidx]) {
                    tint[0] = face_tint[3 * fidx];
                    tint[1] = face_tint[3 * fidx + 1];
                    tint[2] = face_tint[3 * fidx + 2];
                    use_tint = true;
                }
            } else if (orient_tint(fn.x, fn.y, fn.z, tint)) {
                use_tint = true;  // raw mesh body (no FaceRec) -> legacy per-triangle behaviour
            }
            if (use_tint && annotate) {   // orientation tint is an annotation — off for clean shots
                const double mix = 0.55;
                cr = (1 - mix) * br + mix * (tint[0] / 255.0);
                cg = (1 - mix) * bg + mix * (tint[1] / 255.0);
                cb = (1 - mix) * bb + mix * (tint[2] / 255.0);
            }
            ProjectedVertex p0, p1, p2;
            proj(w0.x, w0.y, w0.z, p0.pu, p0.pv, p0.pd);
            proj(w1.x, w1.y, w1.z, p1.pu, p1.pv, p1.pd);
            proj(w2.x, w2.y, w2.z, p2.pu, p2.pv, p2.pd);
            p0.lam = lam0; p1.lam = lam1; p2.lam = lam2;
            if (!perspective) {
                raster_triangle(p0, p1, p2, cr, cg, cb, balpha);
                continue;
            }
            std::vector<ProjectedVertex> polygon{p0, p1, p2};
            const double tan_y = 0.5 * H / scale;
            const double tan_x = tan_y * W / H;
            polygon = clip_halfspace(polygon, [&](const ProjectedVertex& p) {
                return p.pd - perspective_camera->near_plane;
            });
            polygon = clip_halfspace(polygon, [&](const ProjectedVertex& p) {
                return perspective_camera->far_plane - p.pd;
            });
            polygon = clip_halfspace(polygon, [&](const ProjectedVertex& p) {
                return p.pu + p.pd * tan_x;
            });
            polygon = clip_halfspace(polygon, [&](const ProjectedVertex& p) {
                return p.pd * tan_x - p.pu;
            });
            polygon = clip_halfspace(polygon, [&](const ProjectedVertex& p) {
                return p.pv + p.pd * tan_y;
            });
            polygon = clip_halfspace(polygon, [&](const ProjectedVertex& p) {
                return p.pd * tan_y - p.pv;
            });
            for (size_t k = 1; k + 1 < polygon.size(); ++k)
                raster_triangle(polygon[0], polygon[k], polygon[k + 1], cr, cg, cb, balpha);
        }
    }
    const long paint_floor = (min_painted >= 0) ? (long)min_painted : (long)(W * H) / 400;
    if (painted < paint_floor) return false;  // ~nothing drawn — treat as failed render

    // ---- annotation overlay (drawn ON TOP of the shaded mesh) ----------------------------
    // (a) named-face CALLOUTS: a coloured dot + leader + text at each visible principal face's
    //     screen position, using the SAME words the model addresses geometry with (TOP/FRONT
    //     /RIGHT/...). Back-facing labels (normal pointing away from the camera) are culled.
    for (const RenderLabel& lb : labels) {
        if (rdot(lb.normal, d) < 0.15) continue;  // face points away from camera -> skip
        double pu, pv, pd, sx, sy;
        proj(lb.anchor.x, lb.anchor.y, lb.anchor.z, pu, pv, pd);
        if (perspective) {
            const double half_width_at_depth = pd * W * 0.5 / scale;
            const double half_height_at_depth = pd * H * 0.5 / scale;
            if (pd < perspective_camera->near_plane || pd > perspective_camera->far_plane ||
                std::abs(pu) > half_width_at_depth || std::abs(pv) > half_height_at_depth)
                continue;
        }
        toScreen(pu, pv, pd, sx, sy);
        int ix = (int)std::lround(sx), iy = (int)std::lround(sy);
        // A label whose anchor projects OUTSIDE the frame is a lie: the text below is clamped
        // back inside, so a close-up used to sprout a giant "R" belonging to a face metres away.
        if (ix < 0 || ix >= W || iy < 0 || iy >= H) continue;
        fill_disc(img, W, H, ix, iy, 4, 20, 20, 20);
        fill_disc(img, W, H, ix, iy, 3, lb.col[0], lb.col[1], lb.col[2]);
        // place the text just up-right of the dot, nudged inside the frame
        int tx = std::min(ix + 7, W - (int)lb.text.size() * 12 - 2);
        int ty = std::max(2, iy - 8);
        blit_text(img, W, H, tx, ty, lb.text, 2, lb.col[0], lb.col[1], lb.col[2]);
    }
    // (b) ORIENTATION GIZMO bottom-left: the three world axes drawn through the SAME camera so
    //     the model can read which way is +X/+Y/+Z (and thus right/back/top). Fixed screen
    //     anchor; only the projected screen direction of each axis is used. Annotation only —
    //     suppressed for clean product shots.
    if (annotate) {
        int ox = 40, oy = H - 36;
        const int axlen = 26;
        struct Ax { RV3 v; unsigned char c[3]; const char* n; };
        Ax axes[3] = {{{1,0,0},{220,80,80},"X"}, {{0,1,0},{95,185,95},"Y"}, {{0,0,1},{90,140,225},"Z"}};
        for (const Ax& a : axes) {
            double su = rdot(a.v, r), sv = rdot(a.v, u);   // axis dir in screen space
            int ex = ox + (int)std::lround(su * axlen);
            int ey = oy - (int)std::lround(sv * axlen);
            draw_line(img, W, H, ox, oy, ex, ey, a.c[0], a.c[1], a.c[2], 2);
            blit_text(img, W, H, ex - 3, ey - 6, a.n, 2, a.c[0], a.c[1], a.c[2]);
        }
        fill_disc(img, W, H, ox, oy, 3, 30, 30, 30);
    }

    // (c) HEADER top-left (task 03): view name + units + bbox dims, so each preview — especially a
    //     tile in a multi-view contact sheet — says what it is and at what scale. Drawn last so it
    //     sits over everything; blit_text carries its own dark halo, readable over geometry or bg.
    if (!header.empty())
        blit_text(img, W, H, 6, 5, sanitize_label(header), 2, 235, 240, 248);

    outImg.swap(img);
    return true;
}

// Back-compat single-view iso render to a PNG file (the legacy entry the agent vision-review uses).
// Thin wrapper over render_view_to_rgb so there is exactly ONE renderer.
bool render_iso_png(const std::vector<aicm::AicmBody>& bodies, const std::string& out_path,
                    const std::vector<RenderLabel>& labels = {},
                    int W = 512, int H = 400, bool annotate = true,
                    const unsigned char* background = nullptr) {
    std::vector<unsigned char> img;
    if (!render_view_to_rgb(bodies, labels, W, H, rnorm({1.0, 1.0, 0.8}), std::string(), img,
                            annotate, /*body_alpha=*/nullptr, /*frame_from=*/nullptr,
                            /*min_painted=*/-1, /*focus=*/nullptr, /*out_frame=*/nullptr,
                            /*perspective_camera=*/nullptr, background))
        return false;
    std::string png = encode_png_rgb(W, H, img);
    std::ofstream f(out_path, std::ios::binary | std::ios::trunc);
    if (!f) return false;
    f.write(png.data(), (std::streamsize)png.size());
    return (bool)f;
}

// Format "<W> X <D> X <H> MM" from a bbox for the preview header. Integer mm reads cleanly and the
// 5x7 font has no fraction glyphs to spare; sub-mm parts still show non-zero via a 0.x fallback.
inline std::string bbox_dims_label(const std::vector<aicm::AicmBody>& bodies) {
    RV3 lo{1e30, 1e30, 1e30}, hi{-1e30, -1e30, -1e30};
    bool any = false;
    for (const auto& b : bodies)
        for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
            lo.x = std::min(lo.x, (double)b.pos[i]);     hi.x = std::max(hi.x, (double)b.pos[i]);
            lo.y = std::min(lo.y, (double)b.pos[i + 1]); hi.y = std::max(hi.y, (double)b.pos[i + 1]);
            lo.z = std::min(lo.z, (double)b.pos[i + 2]); hi.z = std::max(hi.z, (double)b.pos[i + 2]);
            any = true;
        }
    if (!any) return std::string();
    auto fmt = [](double v) -> std::string {
        if (v >= 9.95) return std::to_string((long)std::lround(v));
        long tenths = (long)std::lround(v * 10.0);  // sub-cm: keep one decimal so it isn't "0"
        return std::to_string(tenths / 10) + "." + std::to_string(tenths % 10);
    };
    return fmt(hi.x - lo.x) + " X " + fmt(hi.y - lo.y) + " X " + fmt(hi.z - lo.z) + " MM";
}

// Parse an optional render background: a 3-element [r,g,b] array in 0-255 (clamped, rounded).
// Anything else — null, absent, wrong length, non-numeric — means "keep the default grey", so
// a malformed value never costs the caller its render. Returns false when nothing was set.
bool parse_render_background(const nlohmann::json& spec, unsigned char out_rgb[3]) {
    if (!spec.is_array() || spec.size() < 3) return false;
    for (int i = 0; i < 3; ++i) {
        if (!spec[i].is_number()) return false;
        const double v = spec[i].get<double>();
        const long q = std::lround(v);
        out_rgb[i] = (unsigned char)(q < 0 ? 0 : (q > 255 ? 255 : q));
    }
    return true;
}

}  // namespace

// Tessellate the surviving solids of `s` into AICM bodies (no lock — callers hold g_mtx).
static std::vector<aicm::AicmBody> tessellate_session_bodies(Session& s) {
    std::vector<aicm::AicmBody> bodies;
    // Assembly docs resolve their geometry through compile_assembly (loaded components +
    // solved mate poses), NOT compile_document — so tessellate the world-placed component
    // bodies off the cached AssemblyResult. Without this branch every assembly tessellates
    // to nothing (compile_document sees no features), which broke CFD/render on assemblies.
    // No explode offset here: consumers (CFD, iso render) want the assembled configuration.
    if (s.is_assembly()) {
        if (!s.asm_result) rebuild_assembly(s);
        const AssemblyResult* ares = s.asm_result.get();
        if (ares)
            for (const AsmBodyOut& body : ares->bodies) {
                try {
                    TopoDS_Shape world = placed_world_shape(body, std::nullopt);
                    float color[3] = {body.color[0], body.color[1], body.color[2]};
                    const ::geom::DisplayDeflections d =
                        ::geom::display_deflections(s.display_quality, world);
                    bodies.push_back(::geom::tessellate_body(
                        world, static_cast<uint32_t>(body.body_id), body.name, color,
                        d.lin, d.ang));
                } catch (...) {
                }
            }
        return bodies;
    }
    CompileResult r = compile_session(s);
    uint32_t id = 1;
    const float color[3] = {0.62f, 0.70f, 0.82f};
    for (const auto& shp : r.shapes) {
        try {
            // Apply any set_appearance colour/material so the render path (doc_render_png /
            // doc_render_view / CFD preview) matches the host's baked AICM. Default grey otherwise.
            float bcolor[3] = {color[0], color[1], color[2]};
            float bmetal = 0.0f, brough = 0.5f;
            apply_doc_appearance(s.doc, id, bcolor, bmetal, brough);
            const ::geom::DisplayDeflections d =
                ::geom::display_deflections(s.display_quality, shp);
            bodies.push_back(::geom::tessellate_body(shp, id, "b" + std::to_string(id), bcolor,
                                                     d.lin, d.ang, bmetal, brough));
            ++id;
        } catch (...) {
        }
    }
    return bodies;
}

// doc_render_diff support: turn one boolean-result shape (A∩B / A-B / B-A) into a renderable
// AICM body carrying an explicit diff colour. Returns false for a null/empty shape or a shape
// that tessellated to nothing (a fuzz sliver with no triangles is not worth drawing). Never
// throws — a boolean residue can be a degenerate compound that trips BRepMesh, and a diff
// render must degrade rather than take the RPC down.
static bool diff_body_from_shape(const TopoDS_Shape& shape, uint32_t body_id,
                                 const char* name, const float color[3],
                                 std::vector<aicm::AicmBody>& out) {
    if (shape.IsNull()) return false;
    try {
        aicm::AicmBody body = ::geom::tessellate_body(shape, body_id, name, color, 0.0, 0.0);
        if (body.idx.empty()) return false;
        out.push_back(std::move(body));
        return true;
    } catch (const Standard_Failure&) {
        return false;
    } catch (...) {
        return false;
    }
}

std::vector<aicm::AicmBody> doc_tessellate_bodies(const std::string& doc_id) {
    try {
        DocLock lk;
        Session& s = require_session(doc_id);
        return tessellate_session_bodies(s);
    } catch (...) {
        return {};
    }
}

// Named-face CALLOUTS (part docs only): label each principal face with the SAME word the model
// queries geometry by, so the vision reviewer can SEE and reference faces concretely (TOP/FRONT/
// RIGHT/...) instead of guessing at a grey blob. Fail-soft (returns whatever it resolved). Shared
// by doc_render_png and the new doc_render_view / contact-sheet entries so every preview is labelled
// identically.
static std::vector<RenderLabel> principal_face_labels(Session& s) {
    std::vector<RenderLabel> labels;
    if (s.is_assembly()) return labels;
    try {
        CompileResult r = compile_session(s);
        if (r.shapes.empty()) return labels;
        const TopoDS_Shape& shp = r.shapes.back();
        struct Dir { const char* side; const char* label; RV3 nrm; unsigned char c[3]; };
        const Dir dirs[] = {
            {"top",    "TOP",    {0, 0, 1},  {90, 140, 225}},
            {"bottom", "BOTTOM", {0, 0, -1}, {70, 175, 185}},
            {"front",  "FRONT",  {0, -1, 0}, {215, 200, 70}},
            {"back",   "BACK",   {0, 1, 0},  {95, 185, 95}},
            {"right",  "RIGHT",  {1, 0, 0},  {220, 80, 80}},
            {"left",   "LEFT",   {-1, 0, 0}, {225, 140, 70}},
        };
        for (const Dir& dd : dirs) {
            chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(
                shp, json{{"of", "face"}, {"side", dd.side}});
            if (!qp.error.empty() || !qp.selectors.is_array() || qp.selectors.empty()) continue;
            const json& sel = qp.selectors.front();
            if (!sel.contains("centroid") || !sel.at("centroid").is_array() ||
                sel.at("centroid").size() < 3)
                continue;
            const json& ct = sel.at("centroid");
            RenderLabel L;
            L.anchor = {ct[0].get<double>(), ct[1].get<double>(), ct[2].get<double>()};
            L.normal = dd.nrm;
            L.text = dd.label;
            L.col[0] = dd.c[0]; L.col[1] = dd.c[1]; L.col[2] = dd.c[2];
            labels.push_back(L);
        }
    } catch (...) {
    }
    return labels;
}

json doc_render_png(const std::string& doc_id, const std::string& out_path, bool annotate,
                    const nlohmann::json& background) {
    try {
        DocLock lk;
        Session& s = require_session(doc_id);
        std::vector<aicm::AicmBody> bodies = tessellate_session_bodies(s);
        if (bodies.empty()) return json{{"ok", false}, {"reason", "no bodies"}};
        // annotate=false → a CLEAN product shot: no face callouts, no orientation triad, no
        // orientation tint (so the true material colour shows). Used for parts-library thumbnails.
        std::vector<RenderLabel> labels = annotate ? principal_face_labels(s)
                                                   : std::vector<RenderLabel>{};
        unsigned char bg[3];
        const bool has_bg = parse_render_background(background, bg);
        bool ok = render_iso_png(bodies, out_path, labels, 512, 400, annotate,
                                 has_bg ? bg : nullptr);
        return json{{"ok", ok}, {"path", ok ? out_path : std::string()}};
    } catch (const OpError& e) {
        // Bug K8 (P2): a CLOSED/missing doc made require_session throw unknown_doc, which the
        // catch-all below flattened to an opaque "render exception" — so render_part read very
        // differently from add_features/measure/undo, which all return the structured
        // {ok:false, error, code:"unknown_doc"} envelope on a stale doc id. Surface the same
        // shape here so a closed-doc render is diagnosable (and the orchestrator can route it)
        // instead of looking like an internal renderer crash.
        return json{{"ok", false}, {"error", e.what()}, {"code", e.code}};
    } catch (...) {
        return json{{"ok", false}, {"reason", "render exception"}};
    }
}

// Render one requested view through the shared software rasterizer. Legacy named, azimuth/
// elevation, direction, size, and focus specs retain their orthographic auto-fit path. A
// `view.camera` object selects the authoritative perspective path: position, target, up, FOV, and
// near/far are validated without fallback or clamping, while width/height alone clamp to 64..2048.
//
// Legacy responses keep the orthographic `{center,right,up,dir,scale,cu,cv,...}` frame. Perspective
// responses identify `projection:"perspective"` and return position, forward/right/true-up,
// focal/principal-point, viewport, and clip range so WCS points can be reprojected exactly. They
// also echo the applied camera, capability, and locked document revision.
//
// The legacy object form accepts CLOSE-UP framing:
//   "width" / "height"   image size in pixels (default 512x400, clamped to 64..2048)
//   "focus": {"min":[x,y,z], "max":[x,y,z]} or {"center":[x,y,z], "radius":r}
//            "margin":0.15, "clip_near":false
// Exact cameras refuse `focus` because honoring both would silently reframe or clip the pose.
// `annotate=false` drops orientation tint, face callouts, and the axis triad.
// `background` is an optional [r,g,b] (0-255) the frame is cleared to; omitted/null keeps the
// engine's flat 245 light grey, so every existing caller renders byte-identically.
json doc_render_view(const std::string& doc_id, const std::string& out_path,
                     const nlohmann::json& view, bool annotate,
                     const nlohmann::json& background) {
    try {
        DocLock lk;
        Session& s = require_session(doc_id);
        ResolvedView rv = resolve_view_spec(view);
        if (!rv.refusal_code.empty())
            return json{{"ok", false}, {"code", rv.refusal_code},
                        {"reason", rv.refusal_reason}, {"path", std::string()}};
        if (rv.camera && view.is_object() && view.contains("focus"))
            return json{{"ok", false}, {"code", "camera_focus_conflict"},
                        {"reason", "exact perspective camera cannot be combined with auto-fit focus"},
                        {"path", std::string()}};

        int W = 512, H = 400;
        json requested_W = W, requested_H = H;
        bool image_size_clamped = false;
        if (view.is_object() && rv.camera) {
            const json& camera = view.at("camera");
            auto read_dimension = [&](const char* key, const char* alias, int fallback,
                                      json& requested, int& applied) -> bool {
                const json* value = nullptr;
                if (camera.contains(key)) value = &camera.at(key);
                else if (camera.contains(alias)) value = &camera.at(alias);
                else if (view.contains(key)) value = &view.at(key);
                if (!value) {
                    requested = fallback;
                    applied = fallback;
                    return true;
                }
                if (!value->is_number_integer()) return false;
                requested = *value;
                if (value->is_number_unsigned()) {
                    const unsigned long long raw = value->get<unsigned long long>();
                    applied = raw > 2048 ? 2048 : (raw < 64 ? 64 : static_cast<int>(raw));
                } else {
                    const long long raw = value->get<long long>();
                    applied = raw < 64 ? 64 : (raw > 2048 ? 2048 : static_cast<int>(raw));
                }
                return true;
            };
            if (!read_dimension("width", "image_width_px", 512, requested_W, W) ||
                !read_dimension("height", "image_height_px", 400, requested_H, H))
                return json{{"ok", false}, {"code", "camera_image_size_invalid"},
                            {"reason", "camera image width and height must be integers"},
                            {"path", std::string()}};
            image_size_clamped = requested_W != W || requested_H != H;
        } else if (view.is_object()) {
            auto clampi = [](int v, int lo2, int hi2) {
                return std::max(lo2, std::min(v, hi2));
            };
            W = clampi(view.value("width", 512), 64, 2048);
            H = clampi(view.value("height", 400), 64, 2048);
            requested_W = W;
            requested_H = H;
        }

        std::vector<aicm::AicmBody> bodies = tessellate_session_bodies(s);
        if (bodies.empty()) return json{{"ok", false}, {"reason", "no bodies"}};
        std::string dims = bbox_dims_label(bodies);
        RenderFocus fbox;
        bool has_focus = false;
        if (view.is_object() && !rv.camera) {
            const auto it = view.find("focus");
            if (it != view.end() && it->is_object()) {
                const json& f = *it;
                auto vec3 = [](const json& a, RV3& out) -> bool {
                    if (!a.is_array() || a.size() < 3) return false;
                    for (int i = 0; i < 3; ++i)
                        if (!a[i].is_number()) return false;
                    out = RV3{a[0].get<double>(), a[1].get<double>(), a[2].get<double>()};
                    return true;
                };
                RV3 lo{0, 0, 0}, hi{0, 0, 0}, ctr{0, 0, 0};
                if (f.contains("min") && f.contains("max") && vec3(f.at("min"), lo)
                    && vec3(f.at("max"), hi)) {
                    has_focus = true;
                } else if (f.contains("center") && vec3(f.at("center"), ctr)) {
                    double rad = std::abs(f.value("radius", 0.0));
                    if (rad > 0) {
                        lo = RV3{ctr.x - rad, ctr.y - rad, ctr.z - rad};
                        hi = RV3{ctr.x + rad, ctr.y + rad, ctr.z + rad};
                        has_focus = true;
                    }
                }
                if (has_focus) {
                    fbox.lo = RV3{std::min(lo.x, hi.x), std::min(lo.y, hi.y),
                                  std::min(lo.z, hi.z)};
                    fbox.hi = RV3{std::max(lo.x, hi.x), std::max(lo.y, hi.y),
                                  std::max(lo.z, hi.z)};
                    fbox.margin = std::max(0.0, std::min(f.value("margin", 0.15), 4.0));
                    fbox.clip_near = f.value("clip_near", false);
                }
            }
        }

        std::string header = (rv.camera && !annotate) ? std::string() : rv.name;
        if (has_focus) {
            auto fmt1 = [](double v) -> std::string {
                if (v >= 9.95) return std::to_string((long)std::lround(v));
                long tenths = (long)std::lround(v * 10.0);
                return std::to_string(tenths / 10) + "." + std::to_string(tenths % 10);
            };
            header += "  FOCUS " + fmt1(fbox.hi.x - fbox.lo.x) + " X "
                      + fmt1(fbox.hi.y - fbox.lo.y) + " X "
                      + fmt1(fbox.hi.z - fbox.lo.z) + " MM";
            if (fbox.clip_near) header += "  CUTAWAY";
        } else if (!header.empty() && !dims.empty()) {
            header += "  " + dims;
        }
        std::vector<RenderLabel> labels;
        if (annotate) labels = principal_face_labels(s);
        std::vector<unsigned char> img;
        RenderFrame frame;
        const PerspectiveCamera* camera = rv.camera ? &*rv.camera : nullptr;
        const int min_painted = (has_focus || camera) ? 1 : -1;
        unsigned char bg[3];
        const bool has_bg = parse_render_background(background, bg);
        bool ok = render_view_to_rgb(bodies, labels, W, H, rv.dir, header, img,
                                     annotate, /*body_alpha=*/nullptr,
                                     /*frame_from=*/nullptr, min_painted,
                                     has_focus ? &fbox : nullptr, &frame, camera,
                                     has_bg ? bg : nullptr);
        if (ok) {
            std::string png = encode_png_rgb(W, H, img);
            std::ofstream f(out_path, std::ios::binary | std::ios::trunc);
            ok = (bool)f && (f.write(png.data(), (std::streamsize)png.size()), (bool)f);
        }
        json out{{"ok", ok}, {"path", ok ? out_path : std::string()},
                 {"view", rv.name}, {"dims", dims},
                 {"dir", json::array({rv.dir.x, rv.dir.y, rv.dir.z})}};
        if (frame.W > 0 && camera) {
            const double tan_half_fov = std::tan(
                camera->vertical_fov_deg * 3.14159265358979323846 / 360.0);
            out["frame"] = json{
                {"projection", "perspective"},
                {"position", json::array({camera->position.x, camera->position.y,
                                           camera->position.z})},
                {"right", json::array({frame.r.x, frame.r.y, frame.r.z})},
                {"up", json::array({frame.u.x, frame.u.y, frame.u.z})},
                {"forward", json::array({camera->forward.x, camera->forward.y,
                                          camera->forward.z})},
                {"vertical_fov_deg", camera->vertical_fov_deg},
                {"tan_half_vertical_fov", tan_half_fov},
                {"focal_length_px", frame.scale},
                {"principal_x_px", W / 2.0}, {"principal_y_px", H / 2.0},
                {"near", camera->near_plane}, {"far", camera->far_plane},
                {"width", W}, {"height", H}};
        } else if (frame.W > 0) {
            out["frame"] = json{
                {"center", json::array({frame.center.x, frame.center.y, frame.center.z})},
                {"right", json::array({frame.r.x, frame.r.y, frame.r.z})},
                {"up", json::array({frame.u.x, frame.u.y, frame.u.z})},
                {"dir", json::array({frame.d.x, frame.d.y, frame.d.z})},
                {"scale", frame.scale}, {"cu", frame.cu}, {"cv", frame.cv},
                {"width", frame.W}, {"height", frame.H},
                {"clip_depth", frame.clipped ? frame.clip_depth : 0.0},
                {"focused", has_focus}, {"clip_near", frame.clipped}};
        }
        if (ok && camera) {
            out["capability"] = "doc_render_view.camera_pose.v1";
            out["camera"] = json{
                {"position", json::array({camera->position.x, camera->position.y,
                                           camera->position.z})},
                {"target", json::array({camera->target.x, camera->target.y, camera->target.z})},
                {"up", json::array({camera->up.x, camera->up.y, camera->up.z})},
                {"vertical_fov_deg", camera->vertical_fov_deg},
                {"image_width_px", W}, {"image_height_px", H},
                {"near", camera->near_plane}, {"far", camera->far_plane}};
            out["revision"] = s.revision;
            out["image_size_clamped"] = image_size_clamped;
            if (image_size_clamped) {
                out["requested_image_width_px"] = requested_W;
                out["requested_image_height_px"] = requested_H;
            }
        }
        return out;
    } catch (const OpError& e) {
        return json{{"ok", false}, {"error", e.what()}, {"code", e.code}};
    } catch (const std::exception& e) {
        return json{{"ok", false}, {"reason", "render exception"}, {"error", e.what()}};
    } catch (...) {
        return json{{"ok", false}, {"reason", "render exception"}};
    }
}

// doc_render_contact_sheet (task 03): render SEVERAL views and tile them into one labelled PNG — the
// ideal multi-view self-check artifact. `views` is an array of specs (resolve_view_spec each);
// defaults to the six standard ortho views + iso when empty/omitted. Each tile carries its own
// header (view name + dims). Returns {ok, path, views:[names], cols, rows, tile:[w,h]}.
// `background` is an optional [r,g,b] (0-255) applied to every tile AND to the empty-tile
// fallback; the inter-tile gutters stay the fixed dark 30 grey that separates them.
json doc_render_contact_sheet(const std::string& doc_id, const std::string& out_path,
                              const nlohmann::json& views,
                              const nlohmann::json& background) {
    try {
        DocLock lk;
        Session& s = require_session(doc_id);
        std::vector<aicm::AicmBody> bodies = tessellate_session_bodies(s);
        if (bodies.empty()) return json{{"ok", false}, {"reason", "no bodies"}};

        std::vector<nlohmann::json> specs;
        if (views.is_array() && !views.empty())
            for (const auto& v : views) specs.push_back(v);
        else
            for (const char* n : {"front", "right", "top", "iso"})
                specs.push_back(std::string(n));

        std::vector<RenderLabel> labels = principal_face_labels(s);
        std::string dims = bbox_dims_label(bodies);
        const int TW = 384, TH = 300;  // per-tile size (contact-sheet tiles are a touch smaller)
        std::vector<std::vector<unsigned char>> tiles;
        std::vector<std::string> names;
        unsigned char bg[3];
        const bool has_bg = parse_render_background(background, bg);
        for (const auto& sp : specs) {
            ResolvedView rv = resolve_view_spec(sp);
            std::string header = rv.name + (dims.empty() ? "" : "  " + dims);
            std::vector<unsigned char> img;
            if (!render_view_to_rgb(bodies, labels, TW, TH, rv.dir, header, img,
                                    /*annotate=*/true, /*body_alpha=*/nullptr,
                                    /*frame_from=*/nullptr, /*min_painted=*/-1,
                                    /*focus=*/nullptr, /*out_frame=*/nullptr,
                                    /*perspective_camera=*/nullptr,
                                    has_bg ? bg : nullptr)) {
                // blank tile rather than dropping the slot — in the caller's own background,
                // with a label that contrasts against it.
                img.resize((size_t)TW * TH * 3);
                for (size_t p = 0; p + 2 < img.size(); p += 3) {
                    img[p] = has_bg ? bg[0] : (unsigned char)245;
                    img[p + 1] = has_bg ? bg[1] : (unsigned char)245;
                    img[p + 2] = has_bg ? bg[2] : (unsigned char)245;
                }
                const bool dark_tile =
                    has_bg && (bg[0] * 299 + bg[1] * 587 + bg[2] * 114) / 1000 < 128;
                const unsigned char ink = dark_tile ? 220 : 60;
                blit_text(img, TW, TH, 6, 5, rv.name + " (EMPTY)", 2, ink, ink, ink);
            }
            tiles.push_back(std::move(img));
            names.push_back(rv.name);
        }
        if (tiles.empty()) return json{{"ok", false}, {"reason", "no views"}};

        int n = (int)tiles.size();
        int cols = (int)std::ceil(std::sqrt((double)n));
        int rows = (cols > 0) ? (n + cols - 1) / cols : 1;
        const int GAP = 4;
        int SW = cols * TW + (cols - 1) * GAP;
        int SH = rows * TH + (rows - 1) * GAP;
        std::vector<unsigned char> sheet((size_t)SW * SH * 3, 30);  // dark gutters between tiles
        for (int i = 0; i < n; ++i) {
            int cx = (i % cols) * (TW + GAP);
            int cy = (i / cols) * (TH + GAP);
            const std::vector<unsigned char>& t = tiles[i];
            for (int y = 0; y < TH; ++y) {
                size_t dst = ((size_t)(cy + y) * SW + cx) * 3;
                size_t src = (size_t)y * TW * 3;
                std::memcpy(&sheet[dst], &t[src], (size_t)TW * 3);
            }
        }
        std::string png = encode_png_rgb(SW, SH, sheet);
        std::ofstream f(out_path, std::ios::binary | std::ios::trunc);
        bool ok = (bool)f && (f.write(png.data(), (std::streamsize)png.size()), (bool)f);
        return json{{"ok", ok}, {"path", ok ? out_path : std::string()},
                    {"views", names}, {"cols", cols}, {"rows", rows},
                    {"tile", json::array({TW, TH})}};
    } catch (const OpError& e) {
        return json{{"ok", false}, {"error", e.what()}, {"code", e.code}};
    } catch (...) {
        return json{{"ok", false}, {"reason", "render exception"}};
    }
}

// Rasterize a 2D DRAWING document to a PNG so the AI can SEE and self-review the drawing
// it just made — the same feedback loop it has for 3D parts (Bug D-1; previously
// render_part threw an opaque "render exception" because a drawing has no 3D bodies and
// lives in a separate session registry). Visible edges draw black, hidden edges light
// grey, on white paper, auto-fit to the canvas. Returns {ok, path, visible, hidden}.
json drawing_render_png(const std::string& doc_id, const std::string& out_path) {
    try {
        // drawing_get works on the drawing-session registry (its own locking); it throws
        // unknown_doc if doc_id isn't an open drawing.
        json full = chudware::doc::drawing_get(doc_id);

        std::vector<std::vector<std::array<double, 2>>> visible, hidden;
        auto pool = [](const json& jpls,
                       std::vector<std::vector<std::array<double, 2>>>& out) {
            if (!jpls.is_array()) return;
            for (const auto& jpl : jpls) {
                if (!jpl.is_array() || jpl.size() < 2) continue;
                std::vector<std::array<double, 2>> pl;
                for (const auto& jp : jpl)
                    if (jp.is_array() && jp.size() >= 2)
                        pl.push_back({jp[0].get<double>(), jp[1].get<double>()});
                if (pl.size() >= 2) out.push_back(std::move(pl));
            }
        };
        // Annotation overlay: render the SHARED draw primitives (annot_draw) emitted per
        // annotation by drawing_get — so GD&T frames, surface-finish/weld symbols, section
        // hatch, dims + callouts all rasterize for the AI vision review, identical to the
        // on-screen sheet and the DXF/PDF export.
        std::vector<std::vector<std::array<double, 2>>> annSolid, annDash;  // sheet-mm strokes
        struct Txt { std::array<double, 2> pos{}; std::string text; double h = 3.5; };
        std::vector<Txt> texts;
        // Bug D-8 — title block as label/value cells (not one crammed "key: value" line),
        // so all standard fields fit a two-column grid without truncating or overlapping.
        std::vector<std::pair<std::string, std::string>> title_cells;  // (LABEL, value)
        double page_w = 0.0, page_h = 0.0;
        auto poolDraw = [&](const json& d) {
            if (!d.is_object()) return;
            pool(d.value("segments", json::array()), annSolid);
            pool(d.value("dashed", json::array()), annDash);
            if (d.contains("tris") && d["tris"].is_array())
                for (const auto& t : d["tris"]) {
                    if (!t.is_array() || t.size() != 3) continue;
                    std::vector<std::array<double, 2>> tri;
                    for (const auto& p : t) tri.push_back({p[0].get<double>(), p[1].get<double>()});
                    tri.push_back(tri.front());
                    annSolid.push_back(std::move(tri));
                }
            if (d.contains("arcs") && d["arcs"].is_array())
                for (const auto& a : d["arcs"]) {
                    if (!a.is_object() || !a.contains("center")) continue;
                    double cx = a["center"][0].get<double>(), cy = a["center"][1].get<double>();
                    double r = a.value("radius", 0.0);
                    double a0 = a.value("a0", 0.0), a1 = a.value("a1", 6.28318530718);
                    if (r <= 0) continue;
                    int n = std::max(10, (int)(std::fabs(a1 - a0) / 0.3));
                    std::vector<std::array<double, 2>> pl;
                    for (int i = 0; i <= n; ++i) {
                        double tt = a0 + (a1 - a0) * (double)i / (double)n;
                        pl.push_back({cx + r * std::cos(tt), cy + r * std::sin(tt)});
                    }
                    annSolid.push_back(std::move(pl));
                }
            if (d.contains("texts") && d["texts"].is_array())
                for (const auto& t : d["texts"]) {
                    if (!t.is_object() || !t.contains("pos")) continue;
                    Txt T;
                    T.pos = {t["pos"][0].get<double>(), t["pos"][1].get<double>()};
                    T.text = sanitize_label(t.value("text", std::string()));
                    T.h = t.value("h", 3.5);
                    if (!T.text.empty()) texts.push_back(std::move(T));
                }
        };
        if (full.contains("sheets") && full["sheets"].is_array())
            for (const auto& sheet : full["sheets"]) {
                if (page_w <= 0.0) {
                    const json& mm = sheet.value("size_mm", json(nullptr));
                    if (mm.is_array() && mm.size() == 2 && mm[0].is_number() &&
                        mm[1].is_number()) {
                        page_w = mm[0].get<double>();
                        page_h = mm[1].get<double>();
                        if (page_w > 20.0 && page_h > 20.0)
                            annSolid.push_back({{10.0, 10.0}, {page_w - 10.0, 10.0},
                                               {page_w - 10.0, page_h - 10.0},
                                               {10.0, page_h - 10.0}, {10.0, 10.0}});
                    }
                }
                if (sheet.contains("views") && sheet["views"].is_array())
                    for (const auto& view : sheet["views"]) {
                        pool(view.value("visible", json::array()), visible);
                        pool(view.value("hidden", json::array()), hidden);
                        pool(view.value("hatch", json::array()), annSolid);  // section hatch
                        for (const auto& an : view.value("annotations", json::array()))
                            if (an.is_object() && an.contains("draw")) poolDraw(an["draw"]);
                        // R9 — cutting-plane line + "A-A" label on the PARENT view of a section.
                        // attach_parent_markers puts {label,line} onto the parent view's
                        // section_lines; draw each as a dashed cutting-plane line with the
                        // section label at both endpoints (the SolidWorks "A ──┼── A" marker),
                        // so the section view reads as derived from a real cut on the parent.
                        for (const auto& sl : view.value("section_lines", json::array())) {
                            if (!sl.is_object() || !sl.contains("line") || !sl["line"].is_array())
                                continue;
                            std::vector<std::array<double, 2>> ln;
                            for (const auto& jp : sl["line"])
                                if (jp.is_array() && jp.size() >= 2)
                                    ln.push_back({jp[0].get<double>(), jp[1].get<double>()});
                            if (ln.size() < 2) continue;
                            annDash.push_back(ln);
                            std::string lab = sl.value("label", std::string());
                            if (lab.empty()) lab = "A";
                            Txt t0; t0.pos = ln.front();   t0.text = sanitize_label(lab); t0.h = 4.5;
                            Txt t1; t1.pos = ln.back();    t1.text = sanitize_label(lab); t1.h = 4.5;
                            texts.push_back(std::move(t0));
                            texts.push_back(std::move(t1));
                        }
                    }
                // Title block: gather every non-empty standard field as a (LABEL, value) cell.
                // The full ASME set is 10 fields (set_titleblock): title, drawn_by, date,
                // material, finish, scale, rev, sheet, part_number, company. `rev`/`revision`
                // are accepted interchangeably. Values are rendered in their own column so a
                // long value never eats the label and nothing truncates (Bug D-8).
                const json& tb = sheet.value("title_block", json::object());
                if (tb.is_object()) {
                    // {json key(s), display label}; first present key wins.
                    struct Field { const char* keys[2]; const char* label; };
                    static const Field kFields[] = {
                        {{"title", nullptr}, "TITLE"},
                        {{"part_number", "part_no"}, "PART NO"},
                        {{"drawn_by", "drawn"}, "DRAWN"},
                        {{"date", nullptr}, "DATE"},
                        {{"material", nullptr}, "MATERIAL"},
                        {{"finish", nullptr}, "FINISH"},
                        {{"scale", nullptr}, "SCALE"},
                        {{"rev", "revision"}, "REV"},
                        {{"sheet", nullptr}, "SHEET"},
                        {{"company", nullptr}, "COMPANY"},
                    };
                    for (const Field& fld : kFields) {
                        std::string val;
                        for (const char* k : fld.keys) {
                            if (!k) break;
                            auto it = tb.find(k);
                            if (it != tb.end() && it->is_string() && !it->get<std::string>().empty()) {
                                val = it->get<std::string>();
                                break;
                            }
                        }
                        if (!val.empty())
                            title_cells.emplace_back(std::string(fld.label),
                                                     sanitize_label(val));
                    }
                }
            }
        if (visible.empty() && hidden.empty())
            return json{{"ok", false},
                        {"reason", "drawing has no view geometry to render — add views, then Rebuild"}};

        double minx = 1e30, miny = 1e30, maxx = -1e30, maxy = -1e30;
        auto acc = [&](const std::vector<std::vector<std::array<double, 2>>>& src) {
            for (const auto& pl : src)
                for (const auto& p : pl) {
                    minx = std::min(minx, p[0]); miny = std::min(miny, p[1]);
                    maxx = std::max(maxx, p[0]); maxy = std::max(maxy, p[1]);
                }
        };
        // Stateful drawings have a real paper size. Render that whole sheet rather than
        // auto-cropping to the projected geometry: auto-crop made off-sheet views invisible,
        // detached the title block from the paper, and showed the model a different composition
        // from the fixed-size PDF it was supposed to review.
        acc(visible); acc(hidden);
        const bool fixed_sheet = page_w > 20.0 && page_h > 20.0;
        if (fixed_sheet) {
            minx = 0.0; miny = 0.0; maxx = page_w; maxy = page_h;
        }
        if (!(maxx > minx) || !(maxy > miny))
            return json{{"ok", false}, {"reason", "degenerate drawing bounds"}};

        const int W = 1100, H = 850, margin = 20;
        std::vector<unsigned char> img(static_cast<std::size_t>(W) * H * 3, 255);  // white paper
        const double sc = std::min(double(W - 2 * margin) / (maxx - minx),
                                   double(H - 2 * margin) / (maxy - miny));
        const double offx = margin + (W - 2 * margin - (maxx - minx) * sc) / 2.0;
        const double offy = margin + (H - 2 * margin - (maxy - miny) * sc) / 2.0;
        auto to_pix = [&](const std::array<double, 2>& p, int& ix, int& iy) {
            ix = static_cast<int>(offx + (p[0] - minx) * sc);
            iy = static_cast<int>(H - (offy + (p[1] - miny) * sc));  // flip Y: sheet-up = image-up
        };
        for (const auto& pl : hidden)
            for (std::size_t i = 1; i < pl.size(); ++i) {
                int x0, y0, x1, y1; to_pix(pl[i - 1], x0, y0); to_pix(pl[i], x1, y1);
                draw_line(img, W, H, x0, y0, x1, y1, 150, 150, 150);
            }
        for (const auto& pl : visible)
            for (std::size_t i = 1; i < pl.size(); ++i) {
                int x0, y0, x1, y1; to_pix(pl[i - 1], x0, y0); to_pix(pl[i], x1, y1);
                draw_line(img, W, H, x0, y0, x1, y1, 20, 20, 20);
            }
        // Annotations on top: the compiled draw primitives (dims, callouts, GD&T, hatch) in
        // dim-blue, with text via the bitmap font sized from the paper-space text height.
        const unsigned char DR = 30, DG = 70, DB = 170;  // dimension blue
        for (const auto& pl : annDash)
            for (std::size_t i = 1; i < pl.size(); ++i) {
                int x0, y0, x1, y1; to_pix(pl[i - 1], x0, y0); to_pix(pl[i], x1, y1);
                draw_line(img, W, H, x0, y0, x1, y1, DR, DG, DB);
            }
        for (const auto& pl : annSolid)
            for (std::size_t i = 1; i < pl.size(); ++i) {
                int x0, y0, x1, y1; to_pix(pl[i - 1], x0, y0); to_pix(pl[i], x1, y1);
                draw_line(img, W, H, x0, y0, x1, y1, DR, DG, DB);
            }
        int n_anns_drawn = (int)texts.size();
        for (const auto& T : texts) {
            int tx, ty; to_pix(T.pos, tx, ty);
            int fs = std::max(1, (int)std::lround(T.h * sc / 7.0));  // 5x7 bitmap font scale
            blit_text(img, W, H, tx + 1, ty - fs * 7, T.text, fs, DR, DG, DB);
        }
        // Title block: a fixed two-column grid panel in the bottom-right corner (screen
        // space, never clipped). One row per field; a LABEL column and a VALUE column with a
        // divider between them, so a long value can never overrun a label and nothing
        // truncates against another field (Bug D-8). Text is scale-1 (6px/char advance, 7px
        // tall) so the full 10-field block fits; an over-long value is clipped to its cell
        // with a trailing '.' so it degrades gracefully instead of bleeding past the frame.
        if (!title_cells.empty()) {
            const int ts = 1;            // bitmap-font scale
            const int chW = 6 * ts;      // glyph advance
            const int rowH = 7 * ts + 6; // text height + vertical padding
            const int padX = 4;
            // Label column: sized to the widest label (bounded so it can't dominate).
            int labChars = 0;
            for (const auto& c : title_cells) labChars = std::max(labChars, (int)c.first.size());
            int labW = std::min(labChars * chW + 2 * padX, 90);
            // Value column: sized to the widest value, but the whole panel is capped so it
            // always fits the canvas; the value cell is clipped to whatever width remains.
            int valChars = 0;
            for (const auto& c : title_cells) valChars = std::max(valChars, (int)c.second.size());
            int valW = valChars * chW + 2 * padX;
            const int boxWmax = 360;     // panel width cap
            int boxW = std::min(labW + valW, boxWmax);
            boxW = std::min(boxW, W - 4);
            boxW = std::max(boxW, 150);
            valW = boxW - labW;          // value column gets the remainder
            int boxH = (int)title_cells.size() * rowH;
            // Keep the panel on-paper even if there are many rows.
            boxH = std::min(boxH, H - 4);
            int bx0 = W - boxW - 2, by0 = H - boxH - 2;
            int labX = bx0 + labW;       // label|value divider x
            // Outer frame.
            draw_line(img, W, H, bx0, by0, bx0 + boxW, by0, 20, 20, 20);
            draw_line(img, W, H, bx0, by0 + boxH, bx0 + boxW, by0 + boxH, 20, 20, 20);
            draw_line(img, W, H, bx0, by0, bx0, by0 + boxH, 20, 20, 20);
            draw_line(img, W, H, bx0 + boxW, by0, bx0 + boxW, by0 + boxH, 20, 20, 20);
            // Label|value divider, full height.
            draw_line(img, W, H, labX, by0, labX, by0 + boxH, 20, 20, 20);
            // Clip a string to a pixel width (cells are fixed): keep what fits, mark a cut.
            auto clip_to = [&](const std::string& s, int wpx) -> std::string {
                int maxChars = std::max(0, (wpx - 2 * padX) / chW);
                if ((int)s.size() <= maxChars) return s;
                if (maxChars <= 1) return s.substr(0, std::max(0, maxChars));
                return s.substr(0, maxChars - 1) + ".";
            };
            int ry = by0;
            for (const auto& c : title_cells) {
                if (ry + rowH > by0 + boxH) break;  // ran out of panel height
                if (ry > by0)  // row divider (skip above the first row)
                    draw_line(img, W, H, bx0, ry, bx0 + boxW, ry, 90, 90, 90);
                int ty = ry + 3;
                blit_text(img, W, H, bx0 + padX, ty, clip_to(c.first, labW), ts, 20, 20, 20);
                blit_text(img, W, H, labX + padX, ty, clip_to(c.second, valW), ts, 20, 20, 20);
                ry += rowH;
            }
        }
        std::string png = encode_png_rgb(W, H, img);
        if (png.empty()) return json{{"ok", false}, {"reason", "png encode failed"}};
        std::ofstream f(out_path, std::ios::binary | std::ios::trunc);
        if (!f) return json{{"ok", false}, {"reason", "cannot open output path"}};
        f.write(png.data(), static_cast<std::streamsize>(png.size()));
        if (!f) return json{{"ok", false}, {"reason", "png write failed"}};
        return json{{"ok", true}, {"path", out_path},
                    {"visible", static_cast<int>(visible.size())},
                    {"hidden", static_cast<int>(hidden.size())},
                    {"annotations", n_anns_drawn},
                    {"title_block", !title_cells.empty()}};
    } catch (const OpError& e) {
        // Bug D-3 — review after a detail/section view. drawing_get() re-projects EVERY view
        // on each render; if a derived view (e.g. a detail whose parent is a section with a
        // degenerate cut plane) throws an OpError during re-projection, the whole render
        // aborts. The old catch flattened EVERY OpError to "not a drawing document", which is
        // wrong and undiagnosable (unknown_doc is the only case that actually means that).
        // Surface the real code+message so review_drawing reports WHY (and the orchestrator
        // can route a genuine derived-view-creation bug to drawing_session.cpp). A true
        // missing/closed drawing still reads clearly via the unknown_doc code.
        if (e.code == "unknown_doc")
            return json{{"ok", false}, {"reason", "not a drawing document"},
                        {"code", e.code}};
        return json{{"ok", false},
                    {"reason", std::string("drawing render failed: ") + e.what()},
                    {"code", e.code}};
    } catch (...) {
        return json{{"ok", false}, {"reason", "drawing render exception"}};
    }
}

// A document can compile to ZERO surviving bodies for two very different reasons: the
// feature tree is genuinely empty, OR a feature FAILED to rebuild (e.g. an `import` whose
// source file is gone and that carries no embedded geometry, a degenerate sweep/loft, a
// boolean that consumed its only body). The latter used to surface as the generic
// "document has no bodies …" — indistinguishable from an empty doc, and easy to misread as
// a phantom "eval lost the body right after open" bug. Build a message that NAMES the
// culprit feature(s) + their rebuild error so the failure is self-explaining. Returns ""
// when nothing failed (genuinely empty document).
static std::string rebuild_failure_detail(const CompileResult& r) {
    std::string out;
    int n = 0;
    for (const FeatureStatus& st : r.statuses) {
        if (st.ok) continue;
        out += n++ ? "; " : "";
        out += "feature '" + st.id + "'";
        if (!st.type.empty()) out += " (" + st.type + ")";
        out += " failed to rebuild";
        if (!st.error.empty()) out += ": " + st.error;
    }
    return out;
}

// Throw the right "no bodies to <action>" OpError for an empty compile: a rebuild-failure-
// aware message (naming the broken feature) when a feature errored, else the plain message.
// Centralises the pattern shared by eval_mass / doc_query / doc_bbox / face_points / …
[[noreturn]] static void throw_no_bodies(const std::string& doc_id, const CompileResult& r,
                                         const char* action) {
    const std::string detail = rebuild_failure_detail(r);
    const std::string base = std::string("document has no bodies to ") + action;
    if (!detail.empty())
        throw OpError("invalid_op", base + " — " + detail,
                      {{"doc_id", doc_id}, {"reason", "feature rebuild failed"}});
    throw OpError("invalid_op", base, {{"doc_id", doc_id}});
}

// (asm-fea) The whole document as ONE resolvable shape: every surviving body — assembly
// components world-placed via gather_export_shapes — wrapped in a compound (a single body
// passes through unwrapped, so part docs are bit-identical to the old path). doc_query and
// doc_face_points used to resolve against bodies[-1] ONLY (bridge.py's single-body-era
// policy), so on a multi-body/assembly doc a fixture or load query could never address any
// face of the other bodies — "side":"bottom" on a 40-body vehicle landed on whichever
// sliver compiled last instead of the wheels. Null shape = no bodies (caller throws).
static TopoDS_Shape whole_doc_shape(Session& s) {
    std::vector<TopoDS_Shape> bodies = gather_export_shapes(s);
    if (bodies.empty()) return TopoDS_Shape();
    if (bodies.size() == 1) return bodies.front();
    TopoDS_Compound comp;
    BRep_Builder bld;
    bld.MakeCompound(comp);
    for (const TopoDS_Shape& b : bodies)
        if (!b.IsNull()) bld.Add(comp, b);
    return comp;
}

json doc_query(const std::string& doc_id, const json& query) {
    DocLock lk;
    Session& s = require_session(doc_id);
    const std::vector<std::string> unknown = chudware::doc::unknown_query_keys(query);
    if (!unknown.empty())
        throw OpError("invalid_op",
                      chudware::doc::unknown_query_keys_error("doc_query", unknown));
    // Default: query the WHOLE document — every body of a multi-body/assembly doc (see
    // whole_doc_shape). An optional 1-based "body_id" in the query dict still selects a
    // SPECIFIC surviving solid — the same per-body selection eval_measure/eval_mass/
    // eval_check already expose via eval_active_shape (interface_check, T-BL49).
    int body_id = query.is_object() && query.contains("body_id") && query["body_id"].is_number()
                      ? query["body_id"].get<int>() : 0;
    TopoDS_Shape shape;
    if (body_id >= 1) {
        CompileResult r = compile_session(s);
        if (r.shapes.empty())
            throw_no_bodies(doc_id, r, "query");
        if (body_id > static_cast<int>(r.shapes.size()))
            throw OpError("invalid_op",
                          "body_id " + std::to_string(body_id) + " out of range (doc has " +
                              std::to_string(r.shapes.size()) + " bodies)",
                          {{"reason", "bad body_id"}});
        shape = r.shapes[static_cast<std::size_t>(body_id - 1)];
    } else {
        shape = whole_doc_shape(s);
        if (shape.IsNull()) {
            CompileResult r = compile_session(s);
            throw_no_bodies(doc_id, r, "query");
        }
    }
    chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(shape, query);
    if (!qp.error.empty())
        throw OpError("invalid_op", qp.error);
    int count = qp.selectors.is_array() ? static_cast<int>(qp.selectors.size()) : 0;
    return json{{"count", count}, {"matches", qp.selectors}};
}

// doc_adjacency (GSL W3) — the face-adjacency graph the flat read-back never exposed. For each
// manifold edge (shared by exactly two faces) we record which two faces it joins and whether the
// join is convex (an outside corner) or concave (an inside corner/pocket), plus each face's
// neighbour set. `ord` is the describe_faces enumeration index == the query-match position, so
// the LLM can finally say "the edge between the top and front face" and verify it.
json doc_adjacency(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "adjacency");
    const TopoDS_Shape& shape = r.shapes.back();
    std::vector<chudware::doc::TopoInfo> faces = chudware::doc::describe_faces(shape);

    // Face ordinals MUST follow describe_faces (TopExp_Explorer), not MapShapes — query
    // matches and doc_resolve_pick face ids use that enumeration.
    TopTools_IndexedMapOfShape face_ord;
    for (TopExp_Explorer fexp(shape, TopAbs_FACE); fexp.More(); fexp.Next())
        face_ord.Add(fexp.Current());
    TopTools_IndexedDataMapOfShapeListOfShape e2f;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, e2f);

    std::vector<std::set<int>> nbr(faces.size());
    json edges = json::array();
    auto round6 = [](double x) {
        if (std::abs(x) < 1e-9) return 0.0;
        return std::round(x * 1e6) / 1e6;
    };
    for (int i = 1; i <= e2f.Extent(); ++i) {
        const TopoDS_Edge& e = TopoDS::Edge(e2f.FindKey(i));
        const TopTools_ListOfShape& fl = e2f.FindFromIndex(i);
        if (fl.Extent() != 2) continue;  // only manifold shared edges have a defined dihedral
        TopTools_ListIteratorOfListOfShape lit(fl);
        TopoDS_Face fa = TopoDS::Face(lit.Value());
        lit.Next();
        TopoDS_Face fb = TopoDS::Face(lit.Value());
        int ia = face_ord.FindIndex(fa) - 1, ib = face_ord.FindIndex(fb) - 1;
        if (ia < 0 || ib < 0 || ia >= (int)faces.size() || ib >= (int)faces.size()) continue;
        nbr[ia].insert(ib);
        nbr[ib].insert(ia);
        GProp_GProps gp;
        BRepGProp::LinearProperties(e, gp);
        gp_Pnt cm = gp.CentreOfMass();
        Eigen::Vector3d M(cm.X(), cm.Y(), cm.Z());
        // Convex (outside corner) when each face's outward normal points AWAY from the other
        // face's interior; concave (inside corner/valley) when they point toward each other.
        bool convex = true;
        if (faces[ia].rep_dir && faces[ib].rep_dir) {
            const Eigen::Vector3d& nA = *faces[ia].rep_dir;
            const Eigen::Vector3d& nB = *faces[ib].rep_dir;
            double sgn = nA.dot(faces[ib].centroid - M) + nB.dot(faces[ia].centroid - M);
            convex = (sgn < 0.0);
        }
        std::string edge_surf = "other";
        try {
            BRepAdaptor_Curve ec(e);
            switch (ec.GetType()) {
                case GeomAbs_Line:         edge_surf = "line"; break;
                case GeomAbs_Circle:       edge_surf = "circle"; break;
                case GeomAbs_Ellipse:      edge_surf = "ellipse"; break;
                case GeomAbs_BSplineCurve: edge_surf = "bspline"; break;
                default:                   break;
            }
        } catch (const Standard_Failure&) {}
        edges.push_back({{"between", json::array({ia, ib})},
                         {"convex", convex},
                         {"surf", edge_surf},
                         {"length", round6(gp.Mass())}});
    }

    json jfaces = json::array();
    for (size_t i = 0; i < faces.size(); ++i) {
        json sel = chudware::doc::make_selector(faces[i]);  // reuse Stage-0 side/surf
        json nb = json::array();
        for (int n : nbr[i]) nb.push_back(n);
        jfaces.push_back({{"ord", (int)i},
                          {"surf", sel["surf"]},
                          {"side", sel["side"]},
                          {"concavity", sel["concavity"]},
                          {"neighbors", nb}});
    }
    return json{{"faces", jfaces}, {"edges", edges}};
}

// doc_set_meta / doc_get_meta — generic opaque document metadata, persisted inside the .step+
// exactly like `studies` (the engine never interprets it). The intake subsystem keeps its
// MeasuredPartProfile / ProcessProfile / CriticalDimensionLedger / FitReport / IntakeState here.
json doc_set_meta(const std::string& doc_id, const std::string& key, const json& value) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (!s.doc.contains("meta") || !s.doc["meta"].is_object())
        s.doc["meta"] = json::object();
    s.doc["meta"][key] = value;
    s.dirty = true;
    return json{{"ok", true}, {"key", key}};
}

json doc_get_meta(const std::string& doc_id, const std::string& key) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json v = nullptr;
    if (s.doc.contains("meta") && s.doc["meta"].is_object() && s.doc["meta"].contains(key))
        v = s.doc["meta"][key];
    return json{{"ok", true}, {"key", key}, {"value", v}};
}

json doc_set_name(const std::string& doc_id, const std::string& name) {
    DocLock lk;
    Session& s = require_session(doc_id);
    // Trim whitespace; empty after trim leaves the existing name untouched.
    std::size_t a = name.find_first_not_of(" \t\r\n\f\v");
    if (a != std::string::npos) {
        std::size_t b = name.find_last_not_of(" \t\r\n\f\v");
        std::string trimmed = name.substr(a, b - a + 1);
        if (!trimmed.empty()) {
            s.doc["name"] = trimmed;
            s.dirty = true;
        }
    }
    return json{{"ok", true}, {"name", s.doc.value("name", std::string())}};
}

json doc_face_points(const std::string& doc_id, const json& selector, int max_n) {
    DocLock lk;
    Session& s = require_session(doc_id);

    if (!selector.is_object() || selector.value("kind", std::string()) != "face")
        throw OpError("invalid_op", "face_points requires a face selector");

    // Resolve against the WHOLE document (asm-fea) — every body of a multi-body/assembly
    // doc, not bodies[-1]. Selector centroid/area scoring pins the exact face, so a GUI
    // pick or doc_query match on ANY component (a wheel, a seat) resolves here instead of
    // dangling against whichever body compiled last. Single-body docs are unchanged.
    const TopoDS_Shape shape = whole_doc_shape(s);
    if (shape.IsNull()) {
        CompileResult rr_fail = compile_session(s);
        throw_no_bodies(doc_id, rr_fail, "evaluate");
    }
    const double diag = sim_bbox_diag(shape);

    std::vector<TopoInfo> infos = describe_faces(shape);
    ResolveResult rr = resolve(selector, infos, diag);
    if (!rr.index.has_value())
        throw OpError("invalid_op",
                      "face selector dangling: " +
                          rr.dangling_reason.value_or(std::string("unresolved")));

    // elems[index]: the index'th face in TopExp_Explorer(TopAbs_FACE) order — the SAME
    // enumeration describe_faces uses, so rr.index maps directly (build123d's
    // shape.faces() likewise enumerates faces in TopExp_Explorer order).
    TopoDS_Face face;
    {
        int idx = 0;
        bool found = false;
        for (TopExp_Explorer exp(shape, TopAbs_FACE); exp.More(); exp.Next(), ++idx) {
            if (idx == *rr.index) {
                face = TopoDS::Face(exp.Current());
                found = true;
                break;
            }
        }
        if (!found)
            throw OpError("invalid_op", "face selector dangling: index out of range");
    }

    std::vector<std::array<double, 3>> pts = sample_face_nodes(face);

    if (pts.empty()) {
        // Fall back to the selector's centroid (a 3-list) so the solver has one anchor.
        const json& c = selector.contains("centroid") ? selector.at("centroid") : json();
        if (c.is_array() && c.size() == 3 && c[0].is_number() && c[1].is_number() &&
            c[2].is_number()) {
            pts.push_back({c[0].get<double>(), c[1].get<double>(), c[2].get<double>()});
        } else {
            throw OpError("invalid_op", "face has no triangulation nodes to sample");
        }
    }

    // Z1 FIX: For flat (planar) faces, OCCT's BRepMesh_IncrementalMesh only produces
    // the 4 boundary corners as triangulation nodes (chord deviation on a plane is 0 for
    // any interior point, so the mesher adds no interior nodes). When these 4 corners are
    // the only sample points, the proximity search in nodes_in_faceset uses a tol based on
    // the body diagonal (~2mm for a 101mm bar) — too small to reach the face center
    // (7.07mm away for a 10×10mm face) — so most gmsh mesh nodes on the end face are
    // EXCLUDED. With only 4 corner sample nodes picked, the work-consistent force
    // distribution concentrates the full 10000N into 4 instead of ~300 nodes → ~21×
    // stress spike (original bug), or with add_midside_face_nodes it still gives
    // corner-biased BCs → wrong stress.
    //
    // FIX: For planar faces, export the face normal vector alongside the sample points.
    // The solver (nodes_in_faceset_with_normal in fea_run.cpp) splits the proximity check
    // into (a) normal-direction distance < perp_tol_mm (tight: 1.5mm — any gmsh node
    // ON this face passes; a node on the OPPOSITE face of a 5mm plate is 5mm away →
    // rejected) and (b) in-plane distance < tol_mm (large: half of face diagonal — covers
    // the entire face surface). This is the ONLY correct fix for the thin-plate case
    // (Z2): a 100×100mm face with 100/2 = 50mm in-plane tol but only 1.5mm normal tol
    // correctly includes the 100×100 fixture face but excludes the top face 5mm away.
    // The face normal vector is added as "face_normal": [nx, ny, nz] in the response and
    // read by fea_run.cpp's resolve_faceset_samples(). Falls back to old Euclidean tol
    // when no normal is present (backward compat — e.g. manually-constructed studies).
    std::optional<std::array<double, 3>> face_normal;
    json surface_desc;  // analytic surface for CURVED faces (C2 fix — see fea_run.h)
    double tol_mm = std::max(0.5, diag * 0.02);  // default: old formula for curved faces
    {
        BRepAdaptor_Surface ad_face(face);
        const GeomAbs_SurfaceType stype = ad_face.GetType();
        // C2 FIX (curved analogue of Z1 below): triangulation vertices only appear where
        // CURVATURE demands them, so a long cylinder barrel samples as two end rings — every
        // mesh node in between is > tol away and gets DROPPED by nodes_in_faceset (loads /
        // fixtures on curved faces under-resolved, mesh-fineness dependent). For the quadric
        // types the fix exports the analytic surface itself: the solver keeps a node iff its
        // exact distance-to-surface < perp_tol (tight) AND it is near ANY sample (large
        // tol = trimmed-extent delimiter). Samples are densified with a UV grid filtered to
        // the trimmed face so the extent test has no axial gaps.
        if (stype == GeomAbs_Cylinder || stype == GeomAbs_Cone || stype == GeomAbs_Sphere ||
            stype == GeomAbs_Torus) {
            if (stype == GeomAbs_Cylinder) {
                const gp_Cylinder cyl = ad_face.Cylinder();
                const gp_Pnt o = cyl.Axis().Location();
                const gp_Dir a = cyl.Axis().Direction();
                surface_desc = {{"type", "cylinder"},
                                {"origin", {o.X(), o.Y(), o.Z()}},
                                {"axis", {a.X(), a.Y(), a.Z()}},
                                {"radius", cyl.Radius()}};
            } else if (stype == GeomAbs_Cone) {
                const gp_Cone cone = ad_face.Cone();
                const gp_Pnt apex = cone.Apex();
                const gp_Dir a = cone.Axis().Direction();
                surface_desc = {{"type", "cone"},
                                {"origin", {apex.X(), apex.Y(), apex.Z()}},
                                {"axis", {a.X(), a.Y(), a.Z()}},
                                {"semi_angle_rad", std::fabs(cone.SemiAngle())}};
            } else if (stype == GeomAbs_Sphere) {
                const gp_Sphere sph = ad_face.Sphere();
                const gp_Pnt c = sph.Location();
                surface_desc = {{"type", "sphere"},
                                {"origin", {c.X(), c.Y(), c.Z()}},
                                {"radius", sph.Radius()}};
            } else {
                const gp_Torus tor = ad_face.Torus();
                const gp_Pnt c = tor.Axis().Location();
                const gp_Dir a = tor.Axis().Direction();
                surface_desc = {{"type", "torus"},
                                {"origin", {c.X(), c.Y(), c.Z()}},
                                {"axis", {a.X(), a.Y(), a.Z()}},
                                {"major_radius", tor.MajorRadius()},
                                {"minor_radius", tor.MinorRadius()}};
            }
            // Trim-extent samples: a UV grid classified against the trimmed face (keeps the
            // grid off holes/slots and outside-trim regions), added to the triangulation
            // vertices. 16×16 caps the work and stays under the max_n=400 downsample line.
            double u0, u1, v0, v1;
            BRepTools::UVBounds(face, u0, u1, v0, v1);
            Handle(Geom_Surface) surf = BRep_Tool::Surface(face);
            const int N = 16;
            double max_step = 0.0;  // max world-space distance between adjacent kept samples
            std::array<double, 3> prev{};
            bool have_prev = false;
            for (int ii = 0; ii < N; ++ii) {
                have_prev = false;  // spacing is measured along each u-row only
                const double u = u0 + (u1 - u0) * (ii + 0.5) / N;
                for (int jj = 0; jj < N; ++jj) {
                    const double v = v0 + (v1 - v0) * (jj + 0.5) / N;
                    BRepClass_FaceClassifier cls(face, gp_Pnt2d(u, v), 1e-7);
                    const TopAbs_State st = cls.State();
                    if (st != TopAbs_IN && st != TopAbs_ON) { have_prev = false; continue; }
                    gp_Pnt p = surf->Value(u, v);
                    pts.push_back({p.X(), p.Y(), p.Z()});
                    if (have_prev) {
                        const double dx = p.X() - prev[0], dy = p.Y() - prev[1],
                                     dz = p.Z() - prev[2];
                        max_step = std::max(max_step, std::sqrt(dx * dx + dy * dy + dz * dz));
                    }
                    prev = {p.X(), p.Y(), p.Z()};
                    have_prev = true;
                }
            }
            // Lateral tol: enough to bridge between adjacent samples (nodes mid-gap must
            // still hit one) but NOT the planar half-diagonal — on a periodic surface (a
            // hole split into two half-cylinder faces at the seam) a huge lateral tol would
            // bleed deep into the sibling face. The tight analytic perp test carries the
            // off-surface rejection either way.
            tol_mm = std::max(tol_mm, 1.25 * max_step);
        } else if (stype == GeomAbs_Plane && static_cast<int>(pts.size()) <= 8) {
            // Planar face with few triangulation nodes (just the 4 boundary corners).
            // Use a dense UV-parametric grid so the face is well-sampled in-plane.
            double u0, u1, v0, v1;
            BRepTools::UVBounds(face, u0, u1, v0, v1);
            Handle(Geom_Surface) surf = BRep_Tool::Surface(face);
            const int N = 7;  // 7×7 = 49 interior points for good in-plane coverage
            const double du = (u1 - u0) / (N + 1);
            const double dv = (v1 - v0) / (N + 1);
            for (int ii = 1; ii <= N; ++ii) {
                for (int jj = 1; jj <= N; ++jj) {
                    gp_Pnt p = surf->Value(u0 + ii * du, v0 + jj * dv);
                    pts.push_back({p.X(), p.Y(), p.Z()});
                }
            }
            // The in-plane tol = half the face diagonal: covers the full face plane.
            // For a 10×10 face: sqrt(10²+10²)/2 = 7.07mm — reaches any interior node.
            // For a 100×100 face: sqrt(100²+100²)/2 = 70.7mm — covers full face.
            // This is safe because the PERPENDICULAR check (via face_normal + perp_tol)
            // in nodes_in_faceset rejects any node not on the face plane.
            gp_Pnt p_corner0 = surf->Value(u0, v0);
            gp_Pnt p_corner1 = surf->Value(u1, v1);
            const double face_diag_2d = p_corner0.Distance(p_corner1);
            tol_mm = std::max(0.5, face_diag_2d * 0.5);
            // Outward face normal (signed: REVERSED → flip so normal points away from solid).
            gp_Dir dir = ad_face.Plane().Axis().Direction();
            if (face.Orientation() == TopAbs_REVERSED) dir.Reverse();
            face_normal = {dir.X(), dir.Y(), dir.Z()};
        }
    }

    // Down-sample to <= max_n with a uniform stride: stride = (n + max_n - 1) // max_n;
    // pts = pts[::stride][:max_n].
    if (static_cast<int>(pts.size()) > max_n) {
        const int n = static_cast<int>(pts.size());
        const int stride = (n + max_n - 1) / max_n;
        std::vector<std::array<double, 3>> ds;
        for (int i = 0; i < n && static_cast<int>(ds.size()) < max_n; i += stride)
            ds.push_back(pts[static_cast<std::size_t>(i)]);
        pts = std::move(ds);
        // A stride-k downsample multiplies the worst-case gap between surviving samples by
        // ~k. For a curved face the samples ARE the trimmed-extent test, so scale the
        // lateral tol to keep the coverage invariant (every on-face node within tol of a
        // survivor). Planar faces already carry a face-diagonal tol — no scaling needed.
        if (!surface_desc.is_null()) tol_mm *= stride;
    }

    json points = json::array();
    for (const auto& p : pts) points.push_back({p[0], p[1], p[2]});
    json result = json{{"points", points}, {"tol_mm", tol_mm}};
    if (!surface_desc.is_null()) {
        result["surface"] = surface_desc;
        // Same tight ON-face threshold as the planar path: the analytic distance replaces
        // the plane-normal dot, so 1.5mm rejects interior/adjacent-face nodes identically.
        result["perp_tol_mm"] = 1.5;
    }
    if (face_normal) {
        result["face_normal"] = {(*face_normal)[0], (*face_normal)[1], (*face_normal)[2]};
        // perp_tol: tight threshold in the face normal direction to exclude off-face nodes.
        // 1.5mm handles gmsh mesh noise (nodes on a face are at exactly the CAD plane, but
        // floating-point mesh positions may drift ≤ 0.1mm). The nearest off-face node for
        // a 5mm-thick plate is 5mm away → safely excluded. Thinner plates (e.g. 2mm) would
        // need perp_tol < 2mm — 1.5mm is conservative. For very thin (<2mm) plates the user
        // should use a different meshing strategy.
        result["perp_tol_mm"] = 1.5;
    }
    return result;
}

// Precise world axis-aligned bounding box of the whole document (all bodies), via
// BRepBndLib::AddOptimal over the ANALYTIC surfaces. Returns {min,max,size,center} in mm.
// Used to hand callers (e.g. make_standard_part) the extents + a placement datum so they
// can position a part without a measure round-trip (Bug A-1).
//
// useTriangulation MUST stay false. AddOptimal defaults it to TRUE, which bounds the
// shape's CACHED DISPLAY MESH and pads by its deflection, so every curved face inflated
// the box while the solid itself was exact. Measured 2026-08-04: a plain box reported
// [40.0000002]^3, but a r=5 h=40 cylinder (volume exact to 1e-11) reported
// [10.0062156, 10.0093228, 40.012431] — note x != y, the giveaway that this is a facet
// artifact and not geometry. A 40 mm cube with a 10 mm through hole reported z = 40.012431
// and a live agent handed the user "bbox_mm: [40.0, 40.0, 40.012]" as the part's size.
// sim_bbox_diag solves the same problem with BRepTools::Clean, but that MUTATES the shape
// by dropping the render mesh — not allowed here: doc_bbox is on hostd's read-only RPC
// allowlist (doc/../hostd/doc_registry.cpp). Ignoring the mesh needs no mutation, and for
// planes/cylinders/cones/spheres/tori the analytic bound is exact.
// SetGap(0) removes the residual safety margin (the 2e-7 above) so a reported dimension
// is the real one; AddOptimal is already called with useShapeTolerance=false.
json doc_bbox(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    const auto shapes = gather_export_shapes(s);
    if (shapes.empty())
        throw_no_bodies(doc_id, ensure_compiled(s), "bound");
    Bnd_Box box;
    for (const TopoDS_Shape& shp : shapes)
        if (!shp.IsNull())
            BRepBndLib::AddOptimal(shp, box, /*useTriangulation=*/Standard_False,
                                   /*useShapeTolerance=*/Standard_False);
    box.SetGap(0.0);
    if (box.IsVoid())
        throw OpError("invalid_op", "bounding box is empty", {{"doc_id", doc_id}});
    Standard_Real xmin, ymin, zmin, xmax, ymax, zmax;
    box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
    return json{
        {"min", {xmin, ymin, zmin}},
        {"max", {xmax, ymax, zmax}},
        {"size", {xmax - xmin, ymax - ymin, zmax - zmin}},
        {"center", {(xmin + xmax) / 2.0, (ymin + ymax) / 2.0, (zmin + zmax) / 2.0}},
    };
}

// Port of engine/doc/evaluate.py::mass_props (via engine/worker.py::_eval_mass). Computes
// volume/mass/centre-of-mass/inertia of the active body, plus principal moments+axes. A
// surface body (open shell / degenerate solid) returns the _surface_props area report.
// body_id is 1-based; <= 0 selects the LAST body (the _eval_active_shape default).
json doc_eval_mass(const std::string& doc_id, int body_id, double density_g_cm3) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r;
    if (s.is_assembly())
        r.shapes = gather_export_shapes(s);
    else
        r = compile_session(s);
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "evaluate");

    // Flatten the surviving bodies into their individual CONNECTED solids. An in-document
    // boolean fuse / boss-merge of two DISJOINT solids (e.g. two cylinders placed far
    // apart) yields a single TopoDS_Compound holding multiple TopAbs_SOLIDs — OCCT does
    // NOT weld non-touching solids — which the compile pipeline tracks as ONE body. For
    // mass properties we want the PHYSICAL body count, so explode each surviving shape
    // into its sub-solids and report each separately (count + per-body breakdown). A
    // surface / non-solid body (no SOLID inside) stays as a single entry. A shape that IS
    // a single solid maps to exactly one entry, so single-solid docs are unchanged.
    std::vector<TopoDS_Shape> solids;
    for (const TopoDS_Shape& sh : r.shapes) {
        TopTools_IndexedMapOfShape smap;
        TopExp::MapShapes(sh, TopAbs_SOLID, smap);
        if (smap.Extent() == 0) {
            solids.push_back(sh);  // surface / degenerate body — counts as one
        } else {
            for (int i = 1; i <= smap.Extent(); ++i) solids.push_back(smap.FindKey(i));
        }
    }

    const double density_g_mm3 = density_g_cm3 * 1e-3;  // 1 g/cm³ = 1e-3 g/mm³

    // Build the mass-props JSON block from a computed GProp_GProps (volume, mass, COM, the inertia
    // tensor about the global origin, and its principal moments/axes). Shared by the single-body
    // and the per-body assembly paths.
    auto props_json = [&](const GProp_GProps& props) -> json {
        const double volume_mm3 = props.Mass();
        const double mass_g = volume_mm3 * density_g_mm3;
        gp_Pnt com = props.CentreOfMass();
        const gp_Mat im = props.MatrixOfInertia();  // gp_Mat is 1-based, row-major access
        std::array<double, 9> inertia{};
        for (int row = 1; row <= 3; ++row)
            for (int col = 1; col <= 3; ++col)
                inertia[static_cast<std::size_t>((row - 1) * 3 + (col - 1))] =
                    im.Value(row, col) * density_g_mm3;
        // Principal moments + axes: eigendecomposition of the (symmetric) inertia tensor. Eigen's
        // SelfAdjointEigenSolver yields ascending eigenvalues + orthonormal eigenvectors as COLUMNS
        // — matching numpy.linalg.eigh. Symmetrise first to kill fp asymmetry. NOTE: eigenvector
        // SIGN is library-arbitrary, so principal_axes match the oracle only up to a ± per axis.
        Eigen::Matrix3d M;
        for (int a = 0; a < 3; ++a)
            for (int b = 0; b < 3; ++b) M(a, b) = inertia[static_cast<std::size_t>(a * 3 + b)];
        M = 0.5 * (M + M.transpose());
        Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> es(M);
        const Eigen::Vector3d evals = es.eigenvalues();
        const Eigen::Matrix3d evecs = es.eigenvectors();
        json principal_moments = json::array();
        for (int i = 0; i < 3; ++i) principal_moments.push_back(evals(i));
        json principal_axes = json::array();  // principal_axes[i] = eigenvector column i
        for (int i = 0; i < 3; ++i)
            principal_axes.push_back({evecs(0, i), evecs(1, i), evecs(2, i)});
        return json{
            {"volume_mm3", volume_mm3},
            {"mass_g", mass_g},
            {"com", {com.X(), com.Y(), com.Z()}},
            {"inertia", {inertia[0], inertia[1], inertia[2], inertia[3], inertia[4], inertia[5],
                         inertia[6], inertia[7], inertia[8]}},
            {"principal_moments", principal_moments},
            {"principal_axes", principal_axes},
        };
    };

    // Single explicit body, OR a genuinely single-solid document: report that one body
    // (legacy shape). body_id and the per-body breakdown both index the EXPLODED solids,
    // so a disjoint fuse reports its true N>1 physical bodies instead of one compound.
    if (body_id >= 1 || solids.size() == 1) {
        const TopoDS_Shape* shape = nullptr;
        if (body_id >= 1) {
            if (body_id > static_cast<int>(solids.size()))
                throw OpError("invalid_op", "body_id " + std::to_string(body_id) + " out of range",
                              {{"reason", "bad body_id"}});
            shape = &solids[static_cast<std::size_t>(body_id - 1)];
        } else {
            shape = &solids.front();
        }
        GProp_GProps props;
        bool props_ok = false;
        if (eval_is_surface_shape(*shape, &props, &props_ok))
            return eval_surface_props(*shape, density_g_cm3);
        if (!props_ok)
            BRepGProp::VolumeProperties(*shape, props);  // unit density -> Mass() is volume in mm³
        json out = props_json(props);
        out["body_count"] = 1;
        out["material"] = {{"density_g_cm3", density_g_cm3}};
        // Bug J4: VolumeProperties on a non-closed solid returns a meaningless CoM/inertia
        // (the integral over an unbounded volume). Don't report it as if trustworthy — flag it.
        if (!eval_is_closed_solid(*shape)) {
            out["closed_solid"] = false;
            out["warning"] = "body is not a closed solid (free edges / open shell) — "
                             "volume, mass, centre of mass and inertia may be unreliable";
        }
        return out;
    }

    // MULTI-BODY (body_id <= 0): per-body breakdown PLUS the combined assembly total. Previously
    // this returned only the LAST body, silently under-reporting a multi-body doc. The top-level
    // fields now carry the TOTAL (so the in-app Mass Properties dialog shows the whole assembly);
    // `bodies[]` lists each solid. GProp_GProps::Add combines correctly (same origin reference).
    json bodies = json::array();
    GProp_GProps total;
    bool have_total = false;
    bool any_open = false;  // Bug J4: a non-closed solid taints its own + the assembly total
    for (std::size_t i = 0; i < solids.size(); ++i) {
        const TopoDS_Shape& shape = solids[i];
        json entry;
        entry["body_id"] = static_cast<int>(i + 1);
        GProp_GProps props;
        bool props_ok = false;
        if (eval_is_surface_shape(shape, &props, &props_ok)) {
            entry["surface"] = true;
            json sp = eval_surface_props(shape, density_g_cm3);
            for (auto it = sp.begin(); it != sp.end(); ++it) entry[it.key()] = it.value();
        } else {
            if (!props_ok) BRepGProp::VolumeProperties(shape, props);
            json pj = props_json(props);
            for (auto it = pj.begin(); it != pj.end(); ++it) entry[it.key()] = it.value();
            if (!eval_is_closed_solid(shape)) {
                entry["closed_solid"] = false;
                entry["warning"] = "body is not a closed solid (free edges / open shell) — "
                                   "its volume, mass, centre of mass and inertia may be unreliable";
                any_open = true;
            }
            total.Add(props);  // accumulate the assembly total (volume-weighted COM + inertia)
            have_total = true;
        }
        bodies.push_back(std::move(entry));
    }

    json out = have_total ? props_json(total)
                          : json{{"volume_mm3", 0.0}, {"mass_g", 0.0}};
    out["body_count"] = static_cast<int>(solids.size());
    out["bodies"] = std::move(bodies);
    out["material"] = {{"density_g_cm3", density_g_cm3}};
    if (any_open)
        out["warning"] = "one or more bodies are not closed solids — the combined total "
                         "mass / centre of mass / inertia may be unreliable";
    return out;
}

// ----------------------------------------------------------------- weldment cut list
// Port of engine/doc/cutlist.py — group weld_member structural members into a
// SolidWorks "Weldment Cut List". build_cut_list is a PURE function over the
// CompileResult.weld_members side-channel ({fid: {profile, dims, members:[...]}});
// doc_weld_cut_list runs a FRESH compile (so the weld_member compiler repopulates
// the side-channel) and groups the pooled members.

namespace {

constexpr int kLenRound = 1;  // cutlist._LEN_ROUND => round member lengths to 0.1 mm

// round-half-to-even? No — Python's round() is banker's rounding, but the oracle
// only ever rounds already-clean test lengths; we mirror Python round(v, ndigits)
// with round-half-away to keep parity on the exact values these tests use (clean
// integer mm) while matching the 0.1 mm grouping resolution.
double round_to(double v, int ndigits) {
    double scale = std::pow(10.0, ndigits);
    return std::round(v * scale) / scale;
}

// cutlist._fmt_num — drop a trailing ".0" so 20.0 reads "20" but 2.5 stays "2.5".
std::string fmt_num(double v) {
    if (std::abs(v - std::round(v)) < 1e-9) {
        long long iv = static_cast<long long>(std::llround(v));
        return std::to_string(iv);
    }
    // "%g"-style formatting (matches Python f"{f:g}").
    std::ostringstream os;
    os << std::defaultfloat << std::setprecision(6) << v;
    return os.str();
}

// Per-profile (label, [dim keys in display order]) — cutlist._PROFILE_LABELS.
const std::vector<std::pair<std::string, std::vector<std::string>>>* profile_label(
    const std::string& profile) {
    static const std::pair<std::string, std::vector<std::string>> sq = {"Square Tube", {"size", "size", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> rect = {"Rectangular Tube", {"w", "h", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> rnd = {"Round Tube", {"od", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> ang = {"Angle", {"leg", "leg", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> flat = {"Flat Bar", {"w", "thickness"}};
    static const std::pair<std::string, std::vector<std::string>> cch = {"C-Channel", {"h", "w", "thickness"}};
    static const std::map<std::string, const std::pair<std::string, std::vector<std::string>>*> tbl = {
        {"square_tube", &sq}, {"rect_tube", &rect}, {"round_tube", &rnd},
        {"angle_L", &ang}, {"flat_bar", &flat}, {"c_channel", &cch}};
    auto it = tbl.find(profile);
    if (it == tbl.end()) return nullptr;
    // Wrap the single descriptor in a vector* shape is awkward; return via a thread
    // local single-element holder.
    static thread_local std::vector<std::pair<std::string, std::vector<std::string>>> hold;
    hold.assign(1, *it->second);
    return &hold;
}

// Read a dim value from the (resolved) dims JSON object as a double; missing -> nullopt.
std::optional<double> dim_at(const json& dims, const std::string& key) {
    if (!dims.is_object()) return std::nullopt;
    auto it = dims.find(key);
    if (it == dims.end() || !it->is_number()) return std::nullopt;
    return it->get<double>();
}

// cutlist._describe — human profile label, e.g. square_tube{size:20,thickness:2}
// -> "Square Tube 20 x 20 x 2". Unknown profiles fall back to a title-cased name +
// dims in sorted-key order.
std::string describe(const std::string& profile, const json& dims) {
    const auto* spec = profile_label(profile);
    if (spec != nullptr) {
        const std::string& label = (*spec)[0].first;
        const std::vector<std::string>& keys = (*spec)[0].second;
        std::vector<std::string> parts;
        for (const std::string& k : keys) {
            auto v = dim_at(dims, k);
            if (v) parts.push_back(fmt_num(*v));
        }
        if (parts.empty()) return label;
        std::string out = label + " ";
        for (std::size_t i = 0; i < parts.size(); ++i) {
            if (i) out += " x ";
            out += parts[i];
        }
        return out;
    }
    // Fallback: title-case the snake_case name, then dims in sorted-key order.
    std::string title;
    {
        std::string word;
        auto flush = [&]() {
            if (word.empty()) return;
            if (!title.empty()) title += " ";
            word[0] = static_cast<char>(std::toupper(static_cast<unsigned char>(word[0])));
            title += word;
            word.clear();
        };
        for (char c : profile) {
            if (c == '_') flush();
            else word += c;
        }
        flush();
    }
    std::vector<std::string> parts;
    if (dims.is_object()) {
        std::vector<std::string> keys;
        for (auto it = dims.begin(); it != dims.end(); ++it) keys.push_back(it.key());
        std::sort(keys.begin(), keys.end());
        for (const std::string& k : keys) {
            auto v = dim_at(dims, k);
            if (v) parts.push_back(fmt_num(*v));
        }
    }
    if (parts.empty()) return title;
    std::string out = title + " ";
    for (std::size_t i = 0; i < parts.size(); ++i) {
        if (i) out += " x ";
        out += parts[i];
    }
    return out;
}

// cutlist._dims_key — a hashable, order-independent key for a resolved-dims dict
// (each value rounded to 4 dp). Realized as a sorted "k=val|" string.
std::string dims_key(const json& dims) {
    if (!dims.is_object()) return std::string();
    std::vector<std::pair<std::string, double>> kv;
    for (auto it = dims.begin(); it != dims.end(); ++it) {
        double v = it->is_number() ? it->get<double>() : 0.0;
        kv.emplace_back(it.key(), round_to(v, 4));
    }
    std::sort(kv.begin(), kv.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });
    std::string out;
    for (const auto& p : kv) {
        std::ostringstream os;
        os << p.first << '=' << std::setprecision(12) << p.second << '|';
        out += os.str();
    }
    return out;
}

// build_cut_list — the PURE function (cutlist.build_cut_list), ported 1:1.
json build_cut_list(const json& weld_members) {
    struct Group {
        std::string profile;
        std::string description;
        double length_mm = 0.0;
        long long quantity = 0;
        double total_length_mm = 0.0;
        // sort companion key: (profile, -length)
    };
    // group key (profile, dims_key, length@0.1mm) -> accumulator, insertion-stable.
    std::map<std::string, Group> groups;
    long long total_members = 0;
    double total_length = 0.0;
    std::set<std::string> profiles_seen;

    if (weld_members.is_object()) {
        for (auto fit = weld_members.begin(); fit != weld_members.end(); ++fit) {
            const json& rec = fit.value();
            if (!rec.is_object()) continue;
            std::string profile = rec.value("profile", std::string("?"));
            json dims = rec.contains("dims") && rec["dims"].is_object()
                            ? rec["dims"] : json::object();
            if (!rec.contains("members") || !rec["members"].is_array()) continue;
            for (const json& m : rec["members"]) {
                if (!m.is_object()) continue;
                std::string mp = m.contains("profile") && m["profile"].is_string()
                                     ? m["profile"].get<std::string>() : profile;
                double raw_len = m.contains("length") && m["length"].is_number()
                                     ? m["length"].get<double>() : 0.0;
                double length = round_to(raw_len, kLenRound);
                // Composite key (profile, dims_key, length) — length in the key text.
                std::ostringstream ks;
                ks << mp << "\x1f" << dims_key(dims) << "\x1f"
                   << std::setprecision(12) << length;
                std::string key = ks.str();
                auto it = groups.find(key);
                if (it == groups.end()) {
                    Group g;
                    g.profile = mp;
                    g.description = describe(mp, dims);
                    g.length_mm = length;
                    g.quantity = 0;
                    g.total_length_mm = 0.0;
                    it = groups.emplace(key, std::move(g)).first;
                }
                it->second.quantity += 1;
                it->second.total_length_mm =
                    round_to(it->second.total_length_mm + length, kLenRound);
                total_members += 1;
                total_length += length;
                profiles_seen.insert(mp);
            }
        }
    }

    // Sort: profile asc, then length desc. 1-based item after sort.
    std::vector<const Group*> ordered;
    ordered.reserve(groups.size());
    for (const auto& kv : groups) ordered.push_back(&kv.second);
    std::stable_sort(ordered.begin(), ordered.end(),
                     [](const Group* a, const Group* b) {
                         if (a->profile != b->profile) return a->profile < b->profile;
                         return a->length_mm > b->length_mm;  // length descending
                     });

    json rows = json::array();
    int item = 1;
    for (const Group* g : ordered) {
        rows.push_back(json{
            {"item", item++},
            {"profile", g->profile},
            {"description", g->description},
            {"length_mm", g->length_mm},
            {"quantity", g->quantity},
            {"total_length_mm", g->total_length_mm},
        });
    }
    json profiles = json::array();
    for (const std::string& p : profiles_seen) profiles.push_back(p);

    return json{
        {"rows", std::move(rows)},
        {"total_members", total_members},
        {"total_length_mm", round_to(total_length, kLenRound)},
        {"profiles", std::move(profiles)},
    };
}

}  // namespace

// cutlist.cut_list_for_session — fresh compile so the weld_member compiler repopulates
// the side-channel, then group. An assembly (no part weldment topology) reports an
// empty cut list. A doc with no weld_member feature yields empty rows + zero totals.
json doc_weld_cut_list(const std::string& doc_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (s.doc.value("kind", std::string()) == "assembly" ||
        s.doc.value("is_assembly", false))
        return build_cut_list(json::object());
    CompileResult r = compile_session(s);
    return build_cut_list(r.weld_members);
}

// ----------------------------------------------------------------- Evaluate suite
// Ports of engine/doc/evaluate.py {check, section_props, symmetry, interference,
// measure} via the engine/worker.py _eval_* handlers. The active body is the LAST
// compiled body by default (body_id 1-based; <=0 = last), EXCEPT interference (all
// bodies) and measure (refs resolved against the LAST body).

namespace {

// Pick the active (or selected) surviving solid from a fresh compile. body_id is
// 1-based; <=0 selects the LAST body. Mirrors worker.py::_eval_active_shape.
const TopoDS_Shape& eval_active_shape(const CompileResult& r, const std::string& doc_id,
                                      int body_id) {
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "evaluate");
    if (body_id >= 1) {
        if (body_id > static_cast<int>(r.shapes.size()))
            throw OpError("invalid_op", "body_id " + std::to_string(body_id) + " out of range",
                          {{"reason", "bad body_id"}});
        return r.shapes[static_cast<std::size_t>(body_id - 1)];
    }
    return r.shapes.back();
}

// Resolve a plane ref to a gp_Pln. Accepts:
//   * datum id string "plane.top/front/right" (or bare "top"/"front"/"right")
//   * {"kind":"datum","feature":<id>}
//   * {"origin":[x,y,z],"normal":[nx,ny,nz]} with optional "xdir":[3]
// Origin/normal/X-axis match the build123d Plane the Python oracle produces for
// reserved datums (section_props Ixx/Iyy/Ixy are expressed in the plane frame).
// Throws OpError on a non-reserved/non-planar/degenerate ref.
gp_Pln eval_resolve_plane(const json& plane_ref) {
    // Explicit origin+normal plane (Evaluate tool custom plane).
    if (plane_ref.is_object() && plane_ref.contains("origin") && plane_ref.contains("normal") &&
        plane_ref["origin"].is_array() && plane_ref["origin"].size() == 3 &&
        plane_ref["normal"].is_array() && plane_ref["normal"].size() == 3) {
        const json& o = plane_ref["origin"];
        const json& n = plane_ref["normal"];
        if (!o[0].is_number() || !o[1].is_number() || !o[2].is_number() ||
            !n[0].is_number() || !n[1].is_number() || !n[2].is_number())
            throw OpError("invalid_op", "plane origin/normal must be numeric [x,y,z]",
                          {{"reason", "bad plane"}});
        const double nx = n[0].get<double>(), ny = n[1].get<double>(), nz = n[2].get<double>();
        const double nlen = std::sqrt(nx * nx + ny * ny + nz * nz);
        if (nlen < 1e-12)
            throw OpError("invalid_op", "plane normal is near-zero",
                          {{"reason", "degenerate normal"}});
        gp_Pnt origin(o[0].get<double>(), o[1].get<double>(), o[2].get<double>());
        gp_Dir normal(nx / nlen, ny / nlen, nz / nlen);
        try {
            if (plane_ref.contains("xdir") && plane_ref["xdir"].is_array() &&
                plane_ref["xdir"].size() == 3 && plane_ref["xdir"][0].is_number() &&
                plane_ref["xdir"][1].is_number() && plane_ref["xdir"][2].is_number()) {
                const json& xd = plane_ref["xdir"];
                const double xx = xd[0].get<double>(), xy = xd[1].get<double>(),
                             xz = xd[2].get<double>();
                const double xlen = std::sqrt(xx * xx + xy * xy + xz * xz);
                if (xlen < 1e-12)
                    throw OpError("invalid_op", "plane xdir is near-zero",
                                  {{"reason", "degenerate xdir"}});
                gp_Dir xdir(xx / xlen, xy / xlen, xz / xlen);
                // gp_Ax3 throws when xdir is parallel to normal.
                return gp_Pln(gp_Ax3(origin, normal, xdir));
            }
            return gp_Pln(gp_Ax3(origin, normal));
        } catch (const Standard_Failure& e) {
            throw OpError("invalid_op",
                          std::string("plane frame invalid: ") + e.GetMessageString(),
                          {{"reason", "bad plane frame"}});
        }
    }

    std::string name;
    if (plane_ref.is_string()) {
        name = plane_ref.get<std::string>();
    } else if (plane_ref.is_object() && plane_ref.value("kind", std::string()) == "datum" &&
               plane_ref.contains("feature") && plane_ref["feature"].is_string()) {
        name = plane_ref["feature"].get<std::string>();
    } else {
        throw OpError("invalid_op",
                      "plane must be a datum id, datum selector, or {origin,normal}",
                      {{"reason", "bad plane"}});
    }
    std::string bare = name;
    if (bare.rfind("plane.", 0) == 0) bare = bare.substr(6);
    if (bare != "top" && bare != "front" && bare != "right")
        throw OpError("invalid_op", "plane '" + name + "' is not a reserved planar datum",
                      {{"reason", "bad plane"}});
    // SketchPlane factory: top u=+X n=+Z; front u=+X n=-Y; right u=+Y n=+X — these
    // are exactly build123d's Plane.XY / .XZ / .YZ (origin, x_dir, z_dir).
    chudware::geom::SketchPlane sp = (bare == "front") ? chudware::geom::SketchPlane::front()
                                  : (bare == "right") ? chudware::geom::SketchPlane::right()
                                                      : chudware::geom::SketchPlane::top();
    gp_Pnt origin(sp.origin.x(), sp.origin.y(), sp.origin.z());
    gp_Dir normal(sp.normal.x(), sp.normal.y(), sp.normal.z());
    gp_Dir xdir(sp.uAxis.x(), sp.uAxis.y(), sp.uAxis.z());
    return gp_Pln(gp_Ax3(origin, normal, xdir));
}

// engine/doc/evaluate.py::_edge_length — arc length via GCPnts_AbscissaPoint; 0 on error.
double eval_edge_length(const TopoDS_Edge& edge) {
    try {
        BRepAdaptor_Curve ca(edge);
        return GCPnts_AbscissaPoint::Length(ca, ca.FirstParameter(), ca.LastParameter(), 1e-4);
    } catch (const Standard_Failure&) {
        return 0.0;
    }
}

// engine/doc/evaluate.py::_vol_or_zero — VolumeProperties mass clamped at >= 0; 0 on error.
double eval_vol_or_zero(const TopoDS_Shape& shape) {
    try {
        GProp_GProps gp;
        BRepGProp::VolumeProperties(shape, gp);
        return std::max(0.0, gp.Mass());
    } catch (const Standard_Failure&) {
        return 0.0;
    }
}

// engine/doc/evaluate.py::_face_normal_at_center — surface normal at the face's UV midpoint
// (returns false when undefined). Mirrors BRepLProp_SLProps(.., 1, 1e-7).IsNormalDefined.
bool eval_face_normal_at_center(const TopoDS_Face& face, gp_Dir& out) {
    try {
        BRepAdaptor_Surface sa(face);
        double u = (sa.FirstUParameter() + sa.LastUParameter()) / 2.0;
        double v = (sa.FirstVParameter() + sa.LastVParameter()) / 2.0;
        BRepLProp_SLProps sl(sa, u, v, 1, 1e-7);
        if (sl.IsNormalDefined()) {
            out = sl.Normal();
            return true;
        }
    } catch (const Standard_Failure&) {
    }
    return false;
}

// engine/doc/evaluate.py::_edge_tangent_at_mid — D1 tangent at the edge mid-parameter.
bool eval_edge_tangent_at_mid(const TopoDS_Edge& edge, gp_Dir& out) {
    try {
        BRepAdaptor_Curve ca(edge);
        double t = (ca.FirstParameter() + ca.LastParameter()) / 2.0;
        gp_Pnt p;
        gp_Vec tang;
        ca.D1(t, p, tang);
        if (tang.Magnitude() > 1e-12) {
            out = gp_Dir(tang);
            return true;
        }
    } catch (const Standard_Failure&) {
    }
    return false;
}

// engine/doc/evaluate.py::_try_angle — degrees between two faces (normals) or two edges
// (tangents); returns false when not applicable. abs(dot) so the answer is in [0,90].
bool eval_try_angle(const TopoDS_Shape& a, const TopoDS_Shape& b, double& out) {
    try {
        if (a.ShapeType() == TopAbs_FACE && b.ShapeType() == TopAbs_FACE) {
            gp_Dir n1, n2;
            if (eval_face_normal_at_center(TopoDS::Face(a), n1) &&
                eval_face_normal_at_center(TopoDS::Face(b), n2)) {
                double dot = std::max(-1.0, std::min(1.0, n1.Dot(n2)));
                out = std::acos(std::fabs(dot)) * (180.0 / 3.14159265358979323846);
                return true;
            }
        } else if (a.ShapeType() == TopAbs_EDGE && b.ShapeType() == TopAbs_EDGE) {
            gp_Dir d1, d2;
            if (eval_edge_tangent_at_mid(TopoDS::Edge(a), d1) &&
                eval_edge_tangent_at_mid(TopoDS::Edge(b), d2)) {
                double dot = std::max(-1.0, std::min(1.0, d1.Dot(d2)));
                out = std::acos(std::fabs(dot)) * (180.0 / 3.14159265358979323846);
                return true;
            }
        }
    } catch (const Standard_Failure&) {
    }
    return false;
}

// Sample an edge as a world-mm polyline, capped at max_pts (inclusive of endpoints).
json eval_sample_edge_polyline(const TopoDS_Edge& edge, int max_pts = 128) {
    json pts = json::array();
    try {
        BRepAdaptor_Curve ca(edge);
        const double t0 = ca.FirstParameter();
        const double t1 = ca.LastParameter();
        const int n = std::max(2, std::min(max_pts, 128));
        pts.get_ptr<json::array_t*>()->reserve(static_cast<std::size_t>(n));
        for (int i = 0; i < n; ++i) {
            const double t = (n == 1) ? t0
                                     : t0 + (t1 - t0) * (static_cast<double>(i) / (n - 1));
            gp_Pnt p = ca.Value(t);
            pts.push_back(json::array({p.X(), p.Y(), p.Z()}));
        }
    } catch (const Standard_Failure&) {
        pts = json::array();
    }
    return pts;
}

// Outer-wire polyline for a face: walk edges of BRepTools::OuterWire, sample, cap at 128.
json eval_sample_face_polyline(const TopoDS_Face& face, int max_pts = 128) {
    json pts = json::array();
    try {
        const TopoDS_Wire outer = BRepTools::OuterWire(face);
        if (outer.IsNull()) return pts;
        std::vector<json> raw;
        raw.reserve(static_cast<std::size_t>(max_pts));
        // First count edges so each gets a fair sample budget.
        int n_edges = 0;
        for (TopExp_Explorer cx(outer, TopAbs_EDGE); cx.More(); cx.Next()) ++n_edges;
        if (n_edges <= 0) return pts;
        const int per_edge = std::max(2, std::min(16, max_pts / n_edges));
        for (TopExp_Explorer ex(outer, TopAbs_EDGE); ex.More(); ex.Next()) {
            if (static_cast<int>(raw.size()) >= max_pts) break;
            const TopoDS_Edge e = TopoDS::Edge(ex.Current());
            const int remain = max_pts - static_cast<int>(raw.size());
            const int edge_budget = std::max(2, std::min(remain, per_edge));
            json ep = eval_sample_edge_polyline(e, edge_budget);
            for (const auto& p : ep) {
                if (static_cast<int>(raw.size()) >= max_pts) break;
                // Drop near-duplicate joints between consecutive edges.
                if (!raw.empty()) {
                    const json& prev = raw.back();
                    const double dx = p[0].get<double>() - prev[0].get<double>();
                    const double dy = p[1].get<double>() - prev[1].get<double>();
                    const double dz = p[2].get<double>() - prev[2].get<double>();
                    if (dx * dx + dy * dy + dz * dz < 1e-12) continue;
                }
                raw.push_back(p);
            }
        }
        if (static_cast<int>(raw.size()) > max_pts && max_pts > 1) {
            std::vector<json> ds;
            ds.reserve(static_cast<std::size_t>(max_pts));
            for (int i = 0; i < max_pts; ++i) {
                const std::size_t idx = static_cast<std::size_t>(
                    (static_cast<double>(i) / (max_pts - 1)) * (raw.size() - 1));
                ds.push_back(raw[idx]);
            }
            raw.swap(ds);
        }
        for (auto& p : raw) pts.push_back(std::move(p));
    } catch (const Standard_Failure&) {
        pts = json::array();
    }
    return pts;
}

// Centroid + optional polyline enrichment shared by single/pair measure paths.
void eval_measure_enrich(json& out, const TopoDS_Shape& raw) {
    try {
        const TopAbs_ShapeEnum st = raw.ShapeType();
        if (st == TopAbs_EDGE) {
            const TopoDS_Edge edge = TopoDS::Edge(raw);
            GProp_GProps gp;
            BRepGProp::LinearProperties(edge, gp);
            gp_Pnt c = gp.CentreOfMass();
            out["anchor"] = json::array({c.X(), c.Y(), c.Z()});
            json pl = eval_sample_edge_polyline(edge, 128);
            if (!pl.empty()) out["polyline"] = std::move(pl);
        } else if (st == TopAbs_FACE) {
            const TopoDS_Face face = TopoDS::Face(raw);
            GProp_GProps gp;
            BRepGProp::SurfaceProperties(face, gp);
            gp_Pnt c = gp.CentreOfMass();
            out["anchor"] = json::array({c.X(), c.Y(), c.Z()});
            json pl = eval_sample_face_polyline(face, 128);
            if (!pl.empty()) out["polyline"] = std::move(pl);
        } else if (st == TopAbs_VERTEX) {
            gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(raw));
            out["anchor"] = json::array({p.X(), p.Y(), p.Z()});
            // no polyline for vertices
        } else {
            GProp_GProps gp;
            BRepGProp::VolumeProperties(raw, gp);
            gp_Pnt c = gp.CentreOfMass();
            out["anchor"] = json::array({c.X(), c.Y(), c.Z()});
        }
    } catch (const Standard_Failure&) {
        // leave without enrichment on failure
    } catch (...) {
    }
}


// engine/doc/evaluate.py::_measure_single — 1 ref. EDGE -> radius+diameter+length (circle)
// or length; FACE -> area+perimeter; VERTEX -> point; else volume.
json eval_measure_single(const TopoDS_Shape& raw) {
    const TopAbs_ShapeEnum st = raw.ShapeType();
    json result;
    if (st == TopAbs_EDGE) {
        TopoDS_Edge edge = TopoDS::Edge(raw);
        const double len = eval_edge_length(edge);
        try {
            BRepAdaptor_Curve ca(edge);
            if (ca.GetType() == GeomAbs_Circle) {
                const double r = ca.Circle().Radius();
                result = json{{"kind", "radius"},
                              {"radius", r},
                              {"diameter", 2.0 * r},
                              {"length", len}};
            }
        } catch (const Standard_Failure&) {
        }
        if (result.empty()) result = json{{"kind", "length"}, {"length", len}};
    } else if (st == TopAbs_FACE) {
        GProp_GProps gp;
        BRepGProp::SurfaceProperties(raw, gp);
        double perimeter = 0.0;
        try {
            const TopoDS_Face face = TopoDS::Face(raw);
            const TopoDS_Wire outer = BRepTools::OuterWire(face);
            if (!outer.IsNull()) {
                for (TopExp_Explorer ex(outer, TopAbs_EDGE); ex.More(); ex.Next()) {
                    GProp_GProps lp;
                    BRepGProp::LinearProperties(ex.Current(), lp);
                    perimeter += lp.Mass();
                }
            }
        } catch (const Standard_Failure&) {
            perimeter = 0.0;
        }
        result = json{{"kind", "area"}, {"area", gp.Mass()}, {"perimeter", perimeter}};
    } else if (st == TopAbs_VERTEX) {
        gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(raw));
        result = json{{"kind", "point"},
                      {"points", json::array({json::array({p.X(), p.Y(), p.Z()})})}};
    } else {
        GProp_GProps gp;
        BRepGProp::VolumeProperties(raw, gp);
        result = json{{"kind", "volume"}, {"volume", gp.Mass()}};
    }
    eval_measure_enrich(result, raw);
    return result;
}

// engine/doc/evaluate.py::_measure_pair — distance (BRepExtrema) + optional angle.
// Always emits path==points when distance extrema are available. Two EDGEs that share an
// endpoint (within 1e-3 mm) report as connected_edges with segment/total lengths.
json eval_measure_pair(const TopoDS_Shape& a, const TopoDS_Shape& b) {
    json result = json::object();

    // Connected open edges: any endpoint pair within 1e-3 mm → chain length.
    if (a.ShapeType() == TopAbs_EDGE && b.ShapeType() == TopAbs_EDGE) {
        try {
            TopoDS_Vertex av1, av2, bv1, bv2;
            TopExp::Vertices(TopoDS::Edge(a), av1, av2);
            TopExp::Vertices(TopoDS::Edge(b), bv1, bv2);
            if (!av1.IsNull() && !av2.IsNull() && !bv1.IsNull() && !bv2.IsNull()) {
                const gp_Pnt ap[2] = {BRep_Tool::Pnt(av1), BRep_Tool::Pnt(av2)};
                const gp_Pnt bp[2] = {BRep_Tool::Pnt(bv1), BRep_Tool::Pnt(bv2)};
                constexpr double kConnTol = 1e-3;  // mm
                bool connected = false;
                for (int i = 0; i < 2 && !connected; ++i)
                    for (int j = 0; j < 2 && !connected; ++j)
                        if (ap[i].Distance(bp[j]) <= kConnTol) connected = true;
                if (connected) {
                    const double L1 = eval_edge_length(TopoDS::Edge(a));
                    const double L2 = eval_edge_length(TopoDS::Edge(b));
                    result["kind"] = "connected_edges";
                    result["segment_lengths"] = json::array({L1, L2});
                    result["total_length"] = L1 + L2;
                    // Still compute distance/path below for the extrema read-out.
                }
            }
        } catch (const Standard_Failure&) {
        }
    }

    try {
        BRepExtrema_DistShapeShape dss(a, b);
        if (dss.IsDone()) {
            result["distance"] = dss.Value();
            if (!result.contains("kind")) result["kind"] = "distance";
            if (dss.NbSolution() > 0) {
                gp_Pnt p1 = dss.PointOnShape1(1);
                gp_Pnt p2 = dss.PointOnShape2(1);
                json pts = json::array({json::array({p1.X(), p1.Y(), p1.Z()}),
                                        json::array({p2.X(), p2.Y(), p2.Z()})});
                result["points"] = pts;
                result["path"] = pts;  // path mirrors extrema points when distance solved
            }
        }
    } catch (const Standard_Failure&) {
    }
    double angle = 0.0;
    if (eval_try_angle(a, b, angle)) {
        result["angle"] = angle;
        if (!result.contains("kind")) result["kind"] = "angle";
    }
    if (result.empty()) result["kind"] = "unknown";
    // Per-element enrichment for the two resolved refs (anchor/polyline on a/b).
    json ea = json::object();
    eval_measure_enrich(ea, a);
    json eb = json::object();
    eval_measure_enrich(eb, b);
    if (!ea.empty()) result["a"] = std::move(ea);
    if (!eb.empty()) result["b"] = std::move(eb);
    return result;
}

// Resolve a measure ref to a single OCCT sub-shape — the SAME predicate-aware path doc_query
// uses (doc/picks::resolve_geom_query), so a measure ref honours pick/near/count/index exactly
// as the rest of the app does.
//
// WHY this matters (the symmetric-ambiguity bug it fixes): the prior code re-resolved the ref
// through the plain resolve() scorer. That scorer ranks a selector against describe_* candidates
// and REFUSES to guess when several equivalent candidates co-rank (margin 0 "symmetric
// ambiguity"). A cylinder's lone seam/round edge, or any one of a box's four identical vertical
// edges, therefore dangled — even though the resolved selector carries a UNIQUE centroid that
// pins down exactly one edge, and even though doc_query (resolve_geom_query) disambiguates the
// very same geometry. We now route through resolve_geom_query:
//   * a raw geom-query ref ({"of":"edge","pick":"longest"}) resolves with its predicate intact;
//   * a RESOLVED selector ref (kind+centroid, what _resolve_one_selector hands us) is turned into
//     {"of":kind,"near":<centroid>,"count":1} so its unique centroid singles out one sub-shape.
// pick:"longest"/"shortest" (and friends) break ties deterministically by taking the first of the
// stable MapShapes-ordered result; a genuinely UNQUALIFIED multi-match (no singling predicate)
// still errors loudly so the caller adds a predicate. Vertices (no resolve_geom_query support)
// keep the original describe_vertices + resolve() path.
TopoDS_Shape eval_resolve_ref(const json& ref, const TopoDS_Shape& shape, double diag) {
    // 1) build the predicate-aware query for an edge/face ref.
    json query;
    bool singling = false;  // does the query explicitly single out ONE sub-shape?
    if (chudware::doc::is_geom_query(ref)) {
        // A raw plain-words query — honour every predicate it carries. It is "singling" when it
        // names a pick/near/index or a count cap (the predicates that intentionally narrow to a
        // specific sub-shape and so license a deterministic tie-break on the survivors).
        query = ref;
        singling = ref.contains("pick") || ref.contains("near") || ref.contains("index") ||
                   ref.contains("count") || ref.contains("side");
    } else {
        const std::string kind = ref.value("kind", std::string());
        if (kind == "edge" || kind == "face") {
            // A resolved make_selector dict: re-express it as a query keyed on its UNIQUE
            // centroid so the symmetric describe_* duplicates can't co-rank it into a dangle.
            const json& c = ref.contains("centroid") ? ref.at("centroid") : json();
            if (c.is_array() && c.size() == 3 && c[0].is_number() && c[1].is_number() &&
                c[2].is_number()) {
                query = json{{"of", kind}, {"near", c}, {"count", 1}};
                singling = true;
            }
        }
        // vertices, or an edge/face selector with no centroid -> handled by the legacy path below.
    }

    if (!query.is_null()) {
        chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(shape, query);
        if (!qp.error.empty())
            throw OpError("invalid_op", "measure ref: " + qp.error, {{"reason", "bad query"}});
        const std::size_t n = qp.faces.size() + qp.edges.size();
        if (n == 0)
            throw OpError("invalid_op", "measure ref matched no geometry",
                          {{"reason", "no match"}});
        if (n > 1 && !singling)
            throw OpError("invalid_op",
                          "measure ref matched " + std::to_string(n) +
                              " candidates, not one — narrow it with a predicate "
                              "(pick/side/near/count)",
                          {{"reason", "ambiguous"}});
        // exactly one, OR a singling predicate that deterministically picks the first of the
        // stable-ordered survivors (the longest/shortest tie-break the caller asked for).
        if (!qp.faces.empty()) return qp.faces.front();
        return qp.edges.front();
    }

    // 2) legacy describe_* + resolve() path (vertices, and selectors without a centroid).
    const std::string kind = ref.value("kind", std::string());
    std::vector<TopoInfo> infos;
    TopAbs_ShapeEnum tt;
    if (kind == "face") {
        infos = describe_faces(shape);
        tt = TopAbs_FACE;
    } else if (kind == "edge") {
        infos = describe_edges(shape);
        tt = TopAbs_EDGE;
    } else if (kind == "vertex") {
        infos = describe_vertices(shape);
        tt = TopAbs_VERTEX;
    } else {
        throw OpError("invalid_op", "measure ref kind '" + kind + "' unsupported",
                      {{"reason", "bad kind"}});
    }
    ResolveResult rr = resolve(ref, infos, diag);
    if (!rr.index.has_value())
        throw OpError("invalid_op",
                      "measure ref dangling: " + rr.dangling_reason.value_or(std::string("unresolved")),
                      {{"reason", rr.dangling_reason.value_or(std::string("dangling"))}});
    int idx = 0;
    for (TopExp_Explorer exp(shape, tt); exp.More(); exp.Next(), ++idx) {
        if (idx == *rr.index) return exp.Current();
    }
    throw OpError("invalid_op", "measure ref dangling: index out of range",
                  {{"reason", "index out of range"}});
}

}  // namespace

// engine/doc/evaluate.py::check via worker.py::_eval_check. BRepCheck_Analyzer +
// free-edge / tiny-edge / sliver-face census. Returns {"issues":[{kind,severity,text}]}.
json doc_eval_check(const std::string& doc_id, int body_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    const TopoDS_Shape& shape = eval_active_shape(r, doc_id, body_id);

    json issues = json::array();

    // BRepCheck: walk the analyzer result tree, emit one "brep_check" error per non-OK
    // sub-result status (mirrors _collect_brep_issues).
    BRepCheck_Analyzer analyzer(shape);
    if (!analyzer.IsValid()) {
        try {
            const Handle(BRepCheck_Result)& res = analyzer.Result(shape);
            if (!res.IsNull()) {
                for (res->InitContextIterator(); res->MoreShapeInContext();
                     res->NextShapeInContext()) {
                    const TopoDS_Shape& sub = res->ContextualShape();
                    try {
                        const Handle(BRepCheck_Result)& res2 = analyzer.Result(sub);
                        if (!res2.IsNull()) {
                            const BRepCheck_ListOfStatus& sts =
                                res2->IsStatusOnShape(sub) ? res2->StatusOnShape(sub) : res2->Status();
                            for (BRepCheck_ListOfStatus::Iterator it(sts); it.More(); it.Next()) {
                                if (it.Value() != BRepCheck_NoError) {
                                    issues.push_back(json{
                                        {"kind", "brep_check"},
                                        {"severity", "error"},
                                        {"text", "BRepCheck status " +
                                                     std::to_string(static_cast<int>(it.Value()))}});
                                }
                            }
                        }
                    } catch (const Standard_Failure&) {
                    }
                }
            }
        } catch (const Standard_Failure&) {
        }
    }

    // Free-edge census: edges with fewer than 2 adjacent faces — BUT a SEAM edge of a
    // periodic face (the 0°/360° closure of a cylinder/sphere/full-revolve) and a
    // DEGENERATE edge (a cone apex / sphere pole) are real, watertight B-rep topology that
    // legitimately bound just one face. MapShapesAndAncestors lists a seam's parent face a
    // single time, so the naive "< 2 faces == free" census flagged every cylindrical bore /
    // fillet / chamfer seam as a "free edge" and reported valid CLOSED solids as open shells
    // (bugs H3/H4/H5/J4). Exclude seams (BRep_Tool::IsClosed(edge,face)) and degenerate edges
    // so only the genuine boundary of an open shell (a lone face, an unsewn import) is
    // counted. This is precise — a real free edge has NO seam/degenerate marker, so an
    // honestly-open shell still reports its real free-edge count.
    TopTools_IndexedDataMapOfShapeListOfShape anc_map;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc_map);
    TopTools_IndexedMapOfShape edge_map;
    TopExp::MapShapes(shape, TopAbs_EDGE, edge_map);

    int free_edges = 0;
    for (int ei = 1; ei <= edge_map.Extent(); ++ei) {
        const TopoDS_Shape& edge_sh = edge_map.FindKey(ei);
        int n_faces = anc_map.Contains(edge_sh) ? anc_map.FindFromKey(edge_sh).Extent() : 0;
        if (n_faces >= 2) continue;  // shared by two faces -> interior, never free
        const TopoDS_Edge& edge = TopoDS::Edge(edge_sh);
        // A degenerate edge (cone apex / sphere pole) bounds one face but is not free.
        if (BRep_Tool::Degenerated(edge)) continue;
        // A seam edge is closed on its (single) parent face — periodic-surface closure, not
        // a free boundary. The face appears once in the ancestor list yet the edge is glued
        // to itself across the seam, so it is fully shared.
        bool is_seam = false;
        if (anc_map.Contains(edge_sh)) {
            const TopTools_ListOfShape& faces = anc_map.FindFromKey(edge_sh);
            for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next()) {
                if (BRep_Tool::IsClosed(edge, TopoDS::Face(it.Value()))) { is_seam = true; break; }
            }
        }
        if (is_seam) continue;
        ++free_edges;
    }
    if (free_edges > 0)
        issues.push_back(json{{"kind", "free_edges"},
                              {"severity", "warning"},
                              {"text", std::to_string(free_edges) +
                                           " free edge(s) detected (open shell)"}});

    // Tiny-edge census (< 0.01 mm).
    constexpr double TINY_EDGE_MM = 0.01;
    int tiny_edges = 0;
    for (int ei = 1; ei <= edge_map.Extent(); ++ei) {
        if (eval_edge_length(TopoDS::Edge(edge_map.FindKey(ei))) < TINY_EDGE_MM) ++tiny_edges;
    }
    if (tiny_edges > 0) {
        char buf[96];
        std::snprintf(buf, sizeof(buf), "%d tiny edge(s) shorter than %g mm", tiny_edges,
                      TINY_EDGE_MM);
        issues.push_back(json{{"kind", "tiny_edges"}, {"severity", "warning"}, {"text", buf}});
    }

    // Sliver-face census (< 0.01 mm^2).
    constexpr double SLIVER_AREA_MM2 = 0.01;
    TopTools_IndexedMapOfShape face_map;
    TopExp::MapShapes(shape, TopAbs_FACE, face_map);
    int sliver_faces = 0;
    for (int fi = 1; fi <= face_map.Extent(); ++fi) {
        GProp_GProps gp;
        BRepGProp::SurfaceProperties(face_map.FindKey(fi), gp);
        if (gp.Mass() < SLIVER_AREA_MM2) ++sliver_faces;
    }
    if (sliver_faces > 0) {
        char buf[96];
        std::snprintf(buf, sizeof(buf), "%d sliver face(s) smaller than %g mm\xc2\xb2",
                      sliver_faces, SLIVER_AREA_MM2);
        issues.push_back(json{{"kind", "sliver_faces"}, {"severity", "warning"}, {"text", buf}});
    }

    return json{{"issues", issues}};
}

// ── eval_solid_health (T-GH1) ────────────────────────────────────────────────────────────
// "Is this body actually a sound closed solid?" — the question every downstream consumer
// (an FEA mesher, a CAM toolpath, a STEP/STL export) silently assumes the answer to, and
// which nothing in the API could answer. doc_eval_check returns a bare issue LIST: no
// counts, no offending sids, no shell/solid census, no volume. This returns the census that
// JUSTIFIES the verdict, per body, naming every offender by its stable sid so the agent can
// hover/measure it instead of being handed an unactionable number.
//
// The watertight rule is the seam-aware one the kernel already trusts internally
// (geom/dress.cpp::blend_is_watertight, doc/document.cpp::dress_is_closed_solid): every edge
// of a closed solid is shared by >= 2 faces; the only legitimate single-face exceptions are
// SEAM edges (the 0/2pi closure of a cylinder/sphere/revolve skin) and DEGENERATE edges (a
// cone apex / sphere pole). Both of those are file-local statics elsewhere, and neither
// yields the offending edge, so the walk is reimplemented here rather than called.
//
// body_id is 1-based; <= 0 reports EVERY surviving body (deliberately unlike eval_*'s
// "last body" default — a health sweep that silently skipped bodies 1..n-1 would be exactly
// the confidently-incomplete answer this op exists to kill).
static const char* health_status_text(BRepCheck_Status st) {
    switch (st) {
        case BRepCheck_NoError:                    return "no_error";
        case BRepCheck_NotClosed:                  return "not_closed";
        case BRepCheck_RedundantEdge:              return "redundant_edge";
        case BRepCheck_InvalidMultiConnexity:      return "invalid_multi_connexity";
        case BRepCheck_BadOrientation:             return "bad_orientation";
        case BRepCheck_BadOrientationOfSubshape:   return "bad_orientation_of_subshape";
        case BRepCheck_UnorientableShape:          return "unorientable_shape";
        default:                                   return nullptr;
    }
}

// ── eval_solid_health: the self-intersection time budget ─────────────────────────────────
// BOPAlgo_CheckerSI pave-fills a body against ITSELF at full level, which is quadratic in
// sub-shape count: on a pathological import it can grind for minutes, and a health check
// that hangs is worse than one that declines. OCCT's only cancellation seam is a
// Message_ProgressIndicator whose UserBreak() goes true, so that is what the budget is.
//
// MEASURED on OCCT 7.9.1: a break neither raises nor sets HasErrors() — Perform() just
// returns with a TRUNCATED interference table (a figure-eight prism that reports 3
// interferences reports 0 when broken). So the flag below is the ONLY evidence the run was
// cut short, and without it a timeout would read back as "zero interferences": a fabricated
// clean bill of health, which is precisely what this op exists to never emit. UserBreak may
// be called concurrently by design, hence the atomic.
class SelfIntersectBudget : public Message_ProgressIndicator {
public:
    explicit SelfIntersectBudget(double secs)
        : deadline_(std::chrono::steady_clock::now()
                    + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                          std::chrono::duration<double>(secs))) {}
    bool broke() const { return broke_.load(std::memory_order_relaxed); }

protected:
    Standard_Boolean UserBreak() override {
        if (std::chrono::steady_clock::now() < deadline_) return Standard_False;
        broke_.store(true, std::memory_order_relaxed);
        return Standard_True;
    }
    void Show(const Message_ProgressScope&, const Standard_Boolean) override {}

private:
    std::chrono::steady_clock::time_point deadline_;
    std::atomic<bool> broke_{false};
};

// Interference kinds are named the way OCCT names its own interference tables (V/V, V/E,
// E/E, V/F, E/F, F/F, V/S, E/S, F/S, S/S) so a reader can map a reported kind straight onto
// the algorithm's vocabulary. Lower dimension first.
static const char* si_shape_kind(TopAbs_ShapeEnum t) {
    switch (t) {
        case TopAbs_VERTEX: return "vertex";
        case TopAbs_EDGE:   return "edge";
        case TopAbs_WIRE:   return "wire";
        case TopAbs_FACE:   return "face";
        case TopAbs_SHELL:  return "shell";
        case TopAbs_SOLID:  return "solid";
        default:            return "shape";
    }
}

// DumpErrors writes one alert per line; a JSON reason wants one line.
static std::string si_one_line(const std::string& in) {
    std::string out;
    out.reserve(in.size());
    for (char c : in) {
        if (c == '\n' || c == '\r' || c == '\t') {
            if (!out.empty() && out.back() != ' ') out.push_back(' ');
        } else {
            out.push_back(c);
        }
    }
    while (!out.empty() && out.back() == ' ') out.pop_back();
    return out;
}

json doc_eval_solid_health(const std::string& doc_id, int body_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "solid health check");

    const int nbodies = static_cast<int>(r.shapes.size());
    std::vector<int> want;
    if (body_id > 0) {
        if (body_id > nbodies)
            throw OpError("invalid_op",
                          "body_id " + std::to_string(body_id) + " is out of range: the "
                          "document has " + std::to_string(nbodies) + " body(ies)");
        want.push_back(body_id);
    } else {
        for (int i = 1; i <= nbodies; ++i) want.push_back(i);
    }

    constexpr double TINY_EDGE_MM = 0.01;
    constexpr double SLIVER_AREA_MM2 = 0.01;
    constexpr double SI_BUDGET_S = 20.0;  // wall-clock cap per body on the CheckerSI run
    auto round6 = [](double x) { return std::abs(x) < 1e-9 ? 0.0 : std::round(x * 1e6) / 1e6; };

    json bodies = json::array();
    bool all_watertight = true;
    bool all_sound = true;
    for (int bid : want) {
        const TopoDS_Shape& shape = r.shapes[bid - 1];
        json b = json::object();
        b["body_id"] = bid;

        TopTools_IndexedMapOfShape emap, fmap, vmap, shmap, somap;
        TopExp::MapShapes(shape, TopAbs_EDGE, emap);
        TopExp::MapShapes(shape, TopAbs_FACE, fmap);
        TopExp::MapShapes(shape, TopAbs_VERTEX, vmap);
        TopExp::MapShapes(shape, TopAbs_SHELL, shmap);
        TopExp::MapShapes(shape, TopAbs_SOLID, somap);
        b["faces"] = fmap.Extent();
        b["edges"] = emap.Extent();
        b["vertices"] = vmap.Extent();
        b["shells"] = shmap.Extent();
        b["solids"] = somap.Extent();

        TopTools_IndexedDataMapOfShapeListOfShape anc;
        TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc);

        json free_sids = json::array();
        json nm_sids = json::array();
        json tiny_sids = json::array();
        int seam_edges = 0, degenerate_edges = 0;
        for (int ei = 1; ei <= emap.Extent(); ++ei) {
            const TopoDS_Shape& esh = emap.FindKey(ei);
            const TopoDS_Edge& edge = TopoDS::Edge(esh);
            const int n_faces = anc.Contains(esh) ? anc.FindFromKey(esh).Extent() : 0;
            std::string sid;
            try {
                sid = make_selector(chudware::doc::edge_info(edge, ei - 1))
                          .value("sid", std::string());
            } catch (const Standard_Failure&) {}
            const bool degenerate = BRep_Tool::Degenerated(edge);
            // Seam-ness is a property of the EDGE, not of how many times the ancestor map
            // happened to list its parent face (OCCT lists a seam's face twice as often as
            // not), so classify it BEFORE the incidence branches — otherwise a cylinder
            // reports "0 seam edges" while relying on the seam exception.
            bool is_seam = false;
            if (anc.Contains(esh)) {
                const TopTools_ListOfShape& fl = anc.FindFromKey(esh);
                for (TopTools_ListIteratorOfListOfShape it(fl); it.More(); it.Next()) {
                    if (BRep_Tool::IsClosed(edge, TopoDS::Face(it.Value()))) { is_seam = true; break; }
                }
            }
            if (is_seam) ++seam_edges;
            if (degenerate) ++degenerate_edges;
            if (!degenerate && eval_edge_length(edge) < TINY_EDGE_MM) tiny_sids.push_back(sid);
            if (n_faces > 2) { nm_sids.push_back(sid); continue; }
            if (n_faces >= 2 || degenerate || is_seam) continue;
            free_sids.push_back(sid);
        }
        b["free_edges"] = json{{"count", (int)free_sids.size()}, {"sids", free_sids}};
        b["non_manifold_edges"] = json{{"count", (int)nm_sids.size()}, {"sids", nm_sids}};
        b["tiny_edges"] = json{{"count", (int)tiny_sids.size()}, {"sids", tiny_sids}};
        b["seam_edges"] = seam_edges;
        b["degenerate_edges"] = degenerate_edges;

        json sliver_sids = json::array();
        try {
            for (const TopoInfo& fi : describe_faces(shape)) {
                if (fi.measure && *fi.measure < SLIVER_AREA_MM2)
                    sliver_sids.push_back(make_selector(fi).value("sid", std::string()));
            }
        } catch (const Standard_Failure&) {}
        b["sliver_faces"] = json{{"count", (int)sliver_sids.size()}, {"sids", sliver_sids}};

        // Face-normal / shell-orientation consistency. Only a SHELL can be oriented, so a
        // body that produced none is reported as not-checked rather than as consistent.
        bool orient_checked = false, orient_ok = true;
        json orient_problems = json::array();
        for (int si = 1; si <= shmap.Extent(); ++si) {
            try {
                BRepCheck_Shell chk(TopoDS::Shell(shmap.FindKey(si)));
                const BRepCheck_Status st = chk.Orientation(Standard_False);
                orient_checked = true;
                if (st != BRepCheck_NoError) {
                    orient_ok = false;
                    const char* t = health_status_text(st);
                    orient_problems.push_back(
                        json{{"shell", si},
                             {"status", t ? std::string(t)
                                          : ("brep_check_status_" + std::to_string((int)st))}});
                }
            } catch (const Standard_Failure&) {}
        }
        b["orientation"] = orient_checked
            ? json{{"checked", true}, {"consistent", orient_ok}, {"problems", orient_problems}}
            : json{{"checked", false}, {"consistent", nullptr},
                   {"reason", "not checked — this body produced no SHELL, and shell "
                              "orientation is the only normal-consistency test the kernel "
                              "exposes"}};

        bool brep_valid = true;
        bool brep_checked = true;
        try { brep_valid = BRepCheck_Analyzer(shape).IsValid() == Standard_True; }
        catch (const Standard_Failure&) { brep_checked = false; brep_valid = false; }
        b["brep_valid"] = brep_checked ? json(brep_valid) : json(nullptr);
        if (!brep_checked)
            b["brep_valid_reason"] = "not checked — BRepCheck_Analyzer raised on this shape";

        // Volume is only meaningful for a body that HAS an inside. Reporting a number for a
        // bare shell/face would be worse than useless: BRepGProp happily integrates the
        // divergence over an open skin and returns a confident, meaningless figure.
        double vol = 0.0, area = 0.0;
        const bool has_solid = somap.Extent() >= 1;
        try {
            if (has_solid) {
                GProp_GProps vp;
                BRepGProp::VolumeProperties(shape, vp);
                vol = vp.Mass();
            }
            GProp_GProps sp;
            BRepGProp::SurfaceProperties(shape, sp);
            area = sp.Mass();
        } catch (const Standard_Failure&) {}
        b["volume_mm3"] = has_solid ? json(round6(vol)) : json(nullptr);
        b["volume_positive"] = has_solid && vol > 1e-9;
        if (!has_solid)
            b["volume_reason"] = "not computed — this body contains no SOLID, so it encloses "
                                 "no volume; an open shell has no inside to measure";
        b["area_mm2"] = round6(area);

        // Self-intersection — a real test, not a disclaimer. BOPAlgo_CheckerSI is the
        // kernel's single-body self-interference algorithm (the one
        // BOPAlgo_ArgumentAnalyzer's self-intersection mode drives): it pave-fills the body
        // against itself at level 9 (V/V … S/S — every interference type) and lands each
        // genuine self-interference in DS().Interferences(). Pairs touching a NEW
        // (section-born) shape are dropped, exactly as ArgumentAnalyzer does: the vertices
        // and edges the algorithm invents to DESCRIBE an intersection would otherwise be
        // counted as further intersections. MEASURED on OCCT 7.9.1: a box, a cylinder and a
        // fused pair of overlapping boxes all report 0 (adjacent faces meeting at a shared
        // edge produce an F/F record but no interference), a figure-eight prism reports 3.
        // A watertight verdict is still only a TOPOLOGY verdict — this is the geometric one.
        bool si_checked = false;
        bool si_self_intersecting = false;
        {
            const auto si_t0 = std::chrono::steady_clock::now();
            Handle(SelfIntersectBudget) si_budget = new SelfIntersectBudget(SI_BUDGET_S);
            std::string si_reason;
            int si_count = 0;
            std::set<std::string> si_kinds;
            try {
                BOPAlgo_CheckerSI ck;
                TopTools_ListOfShape one;
                one.Append(shape);
                ck.SetArguments(one);
                ck.SetLevelOfCheck(9);
                ck.SetNonDestructive(Standard_True);
                ck.SetRunParallel(Standard_False);
                ck.Perform(si_budget->Start());
                if (si_budget->broke()) {
                    si_reason = "not checked — the self-intersection check hit its " +
                                std::to_string(static_cast<int>(SI_BUDGET_S)) +
                                " s per-body budget and was stopped, so its interference "
                                "table is truncated and proves nothing either way. Split "
                                "this body or simplify the feature that made it.";
                } else if (ck.HasErrors()) {
                    std::ostringstream es;
                    ck.DumpErrors(es);
                    si_reason = "not checked — BOPAlgo_CheckerSI failed on this body: " +
                                si_one_line(es.str());
                } else if (ck.PDS() == nullptr) {
                    si_reason = "not checked — BOPAlgo_CheckerSI returned no data structure";
                } else {
                    const BOPDS_DS& ds = *ck.PDS();
                    for (BOPDS_MapOfPair::Iterator it(ds.Interferences()); it.More();
                         it.Next()) {
                        Standard_Integer n1 = 0, n2 = 0;
                        it.Value().Indices(n1, n2);
                        if (ds.IsNewShape(n1) || ds.IsNewShape(n2)) continue;
                        TopAbs_ShapeEnum t1 = ds.Shape(n1).ShapeType();
                        TopAbs_ShapeEnum t2 = ds.Shape(n2).ShapeType();
                        // TopAbs_ShapeEnum descends by dimension (SOLID 2 … VERTEX 7), so
                        // the LARGER enum is the lower-dimensional shape and goes first.
                        if (t1 < t2) std::swap(t1, t2);
                        si_kinds.insert(std::string(si_shape_kind(t1)) + "_" +
                                        si_shape_kind(t2));
                        ++si_count;
                    }
                    si_checked = true;
                    si_self_intersecting = si_count > 0;
                }
            } catch (const Standard_Failure& f) {
                const char* m = f.GetMessageString();
                si_reason = std::string("not checked — BOPAlgo_CheckerSI raised on this "
                                        "body: ") +
                            (m && *m ? m : f.DynamicType()->Name());
            }
            const double si_ms = std::chrono::duration<double, std::milli>(
                                     std::chrono::steady_clock::now() - si_t0).count();
            if (si_checked) {
                json kinds = json::array();
                for (const std::string& k : si_kinds) kinds.push_back(k);
                b["self_intersection"] = json{{"checked", true},
                                              {"self_intersecting", si_self_intersecting},
                                              {"interference_count", si_count},
                                              {"kinds", std::move(kinds)},
                                              {"elapsed_ms", std::round(si_ms * 10.0) / 10.0}};
            } else {
                b["self_intersection"] = json{{"checked", false},
                                              {"self_intersecting", nullptr},
                                              {"reason", si_reason},
                                              {"elapsed_ms", std::round(si_ms * 10.0) / 10.0}};
            }
        }

        const bool watertight =
            somap.Extent() >= 1 && free_sids.empty() && nm_sids.empty();
        // A self-intersecting body is NOT sound: it will mesh, boolean and export wrong.
        // A check that did not run cannot make a body unsound — that stays in not_checked.
        const bool sound = watertight && vol > 1e-9 && brep_checked && brep_valid &&
                           (!orient_checked || orient_ok) &&
                           !(si_checked && si_self_intersecting);
        b["watertight"] = watertight;
        b["is_watertight"] = watertight;  // trimesh spelling — the one models actually write
        b["sound"] = sound;
        all_watertight = all_watertight && watertight;
        all_sound = all_sound && sound;
        bodies.push_back(std::move(b));
    }

    return json{{"bodies", bodies},
                {"body_count", nbodies},
                {"all_watertight", all_watertight},
                {"all_sound", all_sound}};
}

// engine/doc/evaluate.py::section_props via worker.py::_eval_section_props. Cut the body by
// the cutting plane (BRepAlgoAPI_Section -> a compound of edges), GROUP those edges into the
// closed loop(s) of the cross-section, build the planar face (outer loop bounded by inner
// loops as HOLES), and read the REAL cross-section properties off the face: area =
// |SurfaceProperties.Mass| (so a hollow/annular section = outer - holes), centroid = its CoM,
// and the 2nd area moments Ixx/Iyy/Ixy about the CENTROID expressed in the CUTTING PLANE'S
// local (u,v) frame. Falls back to the section-wire (LinearProperties) centroid with zero
// area/moments only when no face can be built (open/degenerate section).
//
// Two bugs fixed here (vs the prior "force ALL edges into ONE wire / read GLOBAL-axis
// MatrixOfInertia" code):
//   * Multi-loop sections (a tube wall = outer loop + inner bore loop) made the single
//     MakeWire fail -> area 0. Now ShapeAnalysis_FreeBounds::ConnectEdgesToWires groups the
//     edges into separate closed wires and the largest-area wire becomes the face boundary
//     with the rest added as holes, so an annulus reports the wall area.
//   * Ixx/Iyy were read straight off MatrixOfInertia(1,1)/(2,2) which are GLOBAL-axis area
//     moments — correct only for an XY (top) cut. For a front/right cut they mixed in the
//     out-of-plane coordinate. We now express the face in the plane's LOCAL frame located at
//     the section centroid (a gp_Trsf SetTransformation about the centroidal local Ax3) so the
//     in-plane second moments fall straight out of the transformed face's MatrixOfInertia. For
//     an XY cut of a centred body that transform is the identity, so the top-cut result is
//     unchanged.
json doc_eval_section_props(const std::string& doc_id, const json& plane_ref) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    if (r.shapes.empty())
        throw_no_bodies(doc_id, r, "evaluate");
    const TopoDS_Shape& shape = r.shapes.back();  // worker: bodies[-1]
    gp_Pln pln = eval_resolve_plane(plane_ref);

    double area = 0.0, cx = 0.0, cy = 0.0, cz = 0.0, Ixx = 0.0, Iyy = 0.0, Ixy = 0.0;
    try {
        BRepAlgoAPI_Section section(shape, pln, Standard_False);
        section.Build();
        if (section.IsDone()) {
            const TopoDS_Shape& sec_shape = section.Shape();

            try {
                TopTools_IndexedMapOfShape edge_map;
                TopExp::MapShapes(sec_shape, TopAbs_EDGE, edge_map);
                if (edge_map.Extent() > 0) {
                    // Tolerance for joining edge ends into loops — relative to the body size so
                    // section sliver gaps still connect on big and small parts alike.
                    const double join_tol = std::max(sim_bbox_diag(shape) * 1e-4, 1e-6);

                    // Group the section edges into closed loops. A solid box/cylinder yields ONE
                    // loop; a hollow/annular section yields an outer loop + one inner loop per
                    // bore. (The old single MakeWire could only ever hold one loop -> area 0 on
                    // anything hollow.) shared=false joins by end-point proximity (join_tol),
                    // robust to the section's free-edge vertices not being shared.
                    Handle(TopTools_HSequenceOfShape) in_edges = new TopTools_HSequenceOfShape();
                    for (int ei = 1; ei <= edge_map.Extent(); ++ei)
                        in_edges->Append(edge_map.FindKey(ei));
                    Handle(TopTools_HSequenceOfShape) wires;
                    ShapeAnalysis_FreeBounds::ConnectEdgesToWires(in_edges, join_tol,
                                                                  Standard_False, wires);

                    // Build a face per closed wire; keep its area, wire and a 2D polygon (the
                    // wire's vertices projected into the plane's (u,v) basis) so we can classify
                    // loop NESTING. A solid box yields one loop; an annulus yields a big outer
                    // loop + an inner bore loop (the bore is INSIDE the outer); a tube cut along
                    // its length yields TWO DISJOINT solid loops (neither inside the other).
                    const gp_Ax3& pos = pln.Position();
                    const gp_Pnt o3 = pos.Location();
                    const gp_Dir ud = pos.XDirection();
                    const gp_Dir vd = pos.YDirection();
                    auto to_uv = [&](const gp_Pnt& p) -> Eigen::Vector2d {
                        gp_Vec d(o3, p);
                        return Eigen::Vector2d(d.Dot(gp_Vec(ud)), d.Dot(gp_Vec(vd)));
                    };
                    // Sample a wire into a 2D polygon (plane u,v coords). We sample ALONG each
                    // edge's curve — not just the vertices — so a CIRCULAR loop (which carries a
                    // single seam vertex) still yields a real polygon for the point-in-loop test;
                    // a vertex-only polygon would have <3 points and wrongly report "not inside",
                    // breaking the annulus (bore-inside-outer) classification.
                    auto wire_poly = [&](const TopoDS_Wire& w) -> std::vector<Eigen::Vector2d> {
                        std::vector<Eigen::Vector2d> poly;
                        for (TopExp_Explorer ee(w, TopAbs_EDGE); ee.More(); ee.Next()) {
                            try {
                                BRepAdaptor_Curve ca(TopoDS::Edge(ee.Current()));
                                const double t0 = ca.FirstParameter(), t1 = ca.LastParameter();
                                const int N = 16;  // straight edges collapse to their 2 ends
                                for (int k = 0; k < N; ++k)
                                    poly.push_back(to_uv(ca.Value(t0 + (t1 - t0) * k / N)));
                            } catch (const Standard_Failure&) {
                            }
                        }
                        return poly;
                    };
                    struct Loop { TopoDS_Wire wire; double area; std::vector<Eigen::Vector2d> poly;
                                  Eigen::Vector2d cuv{0, 0}; };
                    std::vector<Loop> loops;
                    if (!wires.IsNull()) {
                        for (int wi = 1; wi <= wires->Length(); ++wi) {
                            const TopoDS_Shape& ws = wires->Value(wi);
                            if (ws.ShapeType() != TopAbs_WIRE) continue;
                            TopoDS_Wire w = TopoDS::Wire(ws);
                            BRepBuilderAPI_MakeFace fm(pln, w);
                            if (!fm.IsDone()) continue;
                            GProp_GProps wp;
                            BRepGProp::SurfaceProperties(fm.Face(), wp);
                            Loop lp;
                            lp.wire = w;
                            lp.area = std::abs(wp.Mass());
                            lp.poly = wire_poly(w);
                            gp_Pnt c = wp.CentreOfMass();
                            lp.cuv = to_uv(c);
                            loops.push_back(std::move(lp));
                        }
                    }
                    // Fallback for the single-loop path when grouping returned nothing usable
                    // (e.g. one already-closed wire OCCT didn't re-emit): stitch every edge into
                    // one wire, exactly as before, so the solid-section path stays correct.
                    if (loops.empty()) {
                        BRepBuilderAPI_MakeWire wb;
                        for (int ei = 1; ei <= edge_map.Extent(); ++ei)
                            wb.Add(TopoDS::Edge(edge_map.FindKey(ei)));
                        if (wb.IsDone()) {
                            BRepBuilderAPI_MakeFace fm(pln, wb.Wire());
                            if (fm.IsDone()) {
                                GProp_GProps wp;
                                BRepGProp::SurfaceProperties(fm.Face(), wp);
                                Loop lp;
                                lp.wire = wb.Wire();
                                lp.area = std::abs(wp.Mass());
                                gp_Pnt c = wp.CentreOfMass();
                                lp.cuv = to_uv(c);
                                loops.push_back(std::move(lp));
                            }
                        }
                    }

                    if (!loops.empty()) {
                        // EVEN-ODD nesting: a loop's depth = how many OTHER loops contain it
                        // (its centroid inside their polygon). Depth 0/2/… = SOLID material;
                        // depth 1/3/… = a HOLE in the solid that immediately encloses it. This
                        // is correct for an annulus (outer depth 0, bore depth 1 -> a hole) AND
                        // for two disjoint solids (both depth 0 -> two separate material faces),
                        // unlike a naive "biggest loop is outer, rest are holes" rule which would
                        // wrongly punch one disjoint solid out of the other.
                        const std::size_t n = loops.size();
                        std::vector<int> depth(n, 0);
                        std::vector<int> parent(n, -1);  // immediate (smallest) enclosing loop
                        auto poly_inside = [](const Eigen::Vector2d& pt,
                                              const std::vector<Eigen::Vector2d>& poly) -> bool {
                            const std::size_t m = poly.size();
                            if (m < 3) return false;
                            bool in = false;
                            for (std::size_t i = 0, j = m - 1; i < m; j = i++) {
                                double xi = poly[i].x(), yi = poly[i].y();
                                double xj = poly[j].x(), yj = poly[j].y();
                                if (((yi > pt.y()) != (yj > pt.y())) &&
                                    (pt.x() < (xj - xi) * (pt.y() - yi) / (yj - yi + 1e-30) + xi))
                                    in = !in;
                            }
                            return in;
                        };
                        for (std::size_t i = 0; i < n; ++i) {
                            double parent_area = std::numeric_limits<double>::infinity();
                            for (std::size_t j = 0; j < n; ++j) {
                                if (i == j) continue;
                                // i is inside j only if j is STRICTLY LARGER — without this guard
                                // CONCENTRIC loops (a bore and its outer wall share a centroid)
                                // each test "inside" the other and both collapse to holes (area 0).
                                if (loops[j].area <= loops[i].area) continue;
                                if (poly_inside(loops[i].cuv, loops[j].poly)) {
                                    ++depth[i];
                                    if (loops[j].area < parent_area) {  // smallest enclosing = parent
                                        parent_area = loops[j].area;
                                        parent[i] = static_cast<int>(j);
                                    }
                                }
                            }
                        }

                        // One face per SOLID loop, with its direct HOLE children added. Collect
                        // every solid face into a compound so disjoint material regions SUM.
                        TopoDS_Compound sec_comp;
                        BRep_Builder cb;
                        cb.MakeCompound(sec_comp);
                        bool any_face = false;
                        for (std::size_t i = 0; i < n; ++i) {
                            if (depth[i] % 2 != 0) continue;  // odd depth = a hole, not material
                            BRepBuilderAPI_MakeFace fm(pln, loops[i].wire);
                            for (std::size_t h = 0; h < n; ++h) {
                                if (depth[h] % 2 == 0 || parent[h] != static_cast<int>(i)) continue;
                                // A hole wire must run opposite to its outer wire; MakeFace::Add
                                // treats the added wire as an inner boundary and subtracts it.
                                TopoDS_Wire hole = loops[h].wire;
                                hole.Reverse();
                                fm.Add(hole);
                            }
                            if (fm.IsDone()) { cb.Add(sec_comp, fm.Face()); any_face = true; }
                        }

                        if (any_face) {
                            GProp_GProps fp;
                            BRepGProp::SurfaceProperties(sec_comp, fp);
                            area = std::abs(fp.Mass());
                            gp_Pnt com = fp.CentreOfMass();
                            cx = com.X();
                            cy = com.Y();
                            cz = com.Z();

                            // Ixx/Iyy/Ixy in the CUTTING PLANE's local frame, about the section
                            // centroid. Re-express the section in a frame whose origin is the
                            // centroid and whose X/Y axes are the plane's in-plane u/v: a
                            // gp_Trsf::SetTransformation(localAx3) maps a point's GLOBAL coords
                            // into that frame's LOCAL coords, so after the transform the section
                            // lies in z=0 with its centroid at the origin. Its MatrixOfInertia is
                            // then the centroidal planar tensor: (1,1)=∫v²dA=Ixx (about local u),
                            // (2,2)=∫u²dA=Iyy (about local v), (1,2)=-∫uv dA=Ixy. (Reading the
                            // GLOBAL-axis MatrixOfInertia directly only happens to be right for an
                            // XY/top cut; a front/right cut mixed in the out-of-plane axis.)
                            gp_Ax3 local(com, pos.Direction(), pos.XDirection());
                            gp_Trsf to_local;
                            to_local.SetTransformation(local);
                            TopoDS_Shape lf =
                                BRepBuilderAPI_Transform(sec_comp, to_local, true).Shape();
                            GProp_GProps lp;
                            BRepGProp::SurfaceProperties(lf, lp);
                            const gp_Mat& mat = lp.MatrixOfInertia();  // 1-based, about origin
                            Ixx = mat.Value(1, 1);
                            Iyy = mat.Value(2, 2);
                            Ixy = mat.Value(1, 2);
                        }
                    }
                }
            } catch (const Standard_Failure&) {
                // face build failed -> fall through to the wire-centroid fallback below
            }

            // Fallback: a centroid from the section wire when no face could be built (open
            // or degenerate section); leaves area/moments at 0.
            if (area == 0.0 && cx == 0.0 && cy == 0.0 && cz == 0.0) {
                GProp_GProps lin;
                BRepGProp::LinearProperties(sec_shape, lin);
                gp_Pnt com = lin.CentreOfMass();
                cx = com.X();
                cy = com.Y();
                cz = com.Z();
            }
        }
    } catch (const Standard_Failure&) {
        // matches the oracle's broad except -> zero everything
    }

    return json{
        {"area_mm2", area},
        {"centroid", {cx, cy, cz}},
        {"Ixx", Ixx},
        {"Iyy", Iyy},
        {"Ixy", Ixy},
    };
}

// engine/doc/evaluate.py::symmetry via worker.py::_eval_symmetry. Mirror the active body
// across the plane and measure the symmetric-difference volume. symmetric iff deviation
// <= 1e-3 * max(vol, 1.0). Returns {"symmetric", "deviation_mm3"}.
json doc_eval_symmetry(const std::string& doc_id, int body_id, const json& plane_ref) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    const TopoDS_Shape& shape = eval_active_shape(r, doc_id, body_id);
    gp_Pln pln = eval_resolve_plane(plane_ref);

    try {
        // Mirror about the plane: SetMirror(gp_Ax2) is planar symmetry. gp_Pln.Position()
        // is a gp_Ax3; build the gp_Ax2 from its location + Z + X (matches _to_gp_plane ->
        // gp_Ax2(location, direction, xdirection)).
        const gp_Ax3& pos = pln.Position();
        gp_Ax2 ax2(pos.Location(), pos.Direction(), pos.XDirection());
        gp_Trsf mirror;
        mirror.SetMirror(ax2);
        BRepBuilderAPI_Transform xf(shape, mirror, Standard_True);
        TopoDS_Shape mirrored = xf.Shape();

        double vol_a = eval_vol_or_zero(shape);
        double vol_mirror = eval_vol_or_zero(mirrored);
        double vol_common = 0.0;
        BRepAlgoAPI_Common common(shape, mirrored);
        common.Build();
        if (common.IsDone()) vol_common = eval_vol_or_zero(common.Shape());

        double deviation = (vol_a - vol_common) + (vol_mirror - vol_common);
        deviation = std::max(0.0, deviation);
        double threshold = 1e-3 * std::max(vol_a, 1.0);
        return json{{"symmetric", deviation <= threshold}, {"deviation_mm3", deviation}};
    } catch (const Standard_Failure& ex) {
        return json{{"symmetric", false}, {"deviation_mm3", -1.0}, {"error", ex.GetMessageString()}};
    }
}

// Explode a compound/compsolid into SOLID children; fall back to the shape as-is when
// no solid is present (imported shells / face soups). Used so BRepAlgoAPI_Common sees
// real solids on STEP imports that arrive as TopoDS_Compound.
TopoDS_Shape eval_interference_member_shape(const TopoDS_Shape& raw) {
    if (raw.IsNull()) return raw;
    try {
        const TopAbs_ShapeEnum st = raw.ShapeType();
        if (st == TopAbs_SOLID || st == TopAbs_COMPSOLID) return raw;
        TopTools_IndexedMapOfShape smap;
        TopExp::MapShapes(raw, TopAbs_SOLID, smap);
        if (smap.Extent() == 0) return raw;  // shell / face soup — use as-is
        if (smap.Extent() == 1) return smap.FindKey(1);
        BRep_Builder bb;
        TopoDS_Compound comp;
        bb.MakeCompound(comp);
        for (int i = 1; i <= smap.Extent(); ++i) bb.Add(comp, smap.FindKey(i));
        return comp;
    } catch (const Standard_Failure&) {
        return raw;
    } catch (...) {
        return raw;
    }
}

// Tessellate a Common-overlap shape into positions[]/indices[] (world mm). Coarse
// deflection; cap ~50k tris; never throws — empty arrays on any failure.
json eval_overlap_mesh(const TopoDS_Shape& common_shape) {
    json empty = json{{"positions", json::array()}, {"indices", json::array()}};
    if (common_shape.IsNull()) return empty;
    try {
        Bnd_Box box;
        BRepBndLib::Add(common_shape, box);
        double diag = 1.0;
        if (!box.IsVoid()) {
            double xmn, ymn, zmn, xmx, ymx, zmx;
            box.Get(xmn, ymn, zmn, xmx, ymx, zmx);
            const double dx = xmx - xmn, dy = ymx - ymn, dz = zmx - zmn;
            diag = std::max(1e-6, std::sqrt(dx * dx + dy * dy + dz * dz));
        }
        auto mesh_at = [&](double defl) -> json {
            BRepTools::Clean(common_shape);
            BRepMesh_IncrementalMesh mesher(common_shape, defl, Standard_False, 0.5,
                                            Standard_True);
            mesher.Perform();
            json positions = json::array();
            json indices = json::array();
            std::size_t n_tris = 0;
            constexpr std::size_t kMaxTris = 50000;
            for (TopExp_Explorer ex(common_shape, TopAbs_FACE); ex.More(); ex.Next()) {
                if (n_tris >= kMaxTris) break;
                const TopoDS_Face face = TopoDS::Face(ex.Current());
                TopLoc_Location loc;
                Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
                if (tri.IsNull() || tri->NbTriangles() == 0) continue;
                const gp_Trsf trsf = loc.Transformation();
                const bool reversed = (face.Orientation() == TopAbs_REVERSED);
                const int base = static_cast<int>(positions.size() / 3);
                for (int ni = 1; ni <= tri->NbNodes(); ++ni) {
                    gp_Pnt p = tri->Node(ni).Transformed(trsf);
                    positions.push_back(p.X());
                    positions.push_back(p.Y());
                    positions.push_back(p.Z());
                }
                for (int ti = 1; ti <= tri->NbTriangles(); ++ti) {
                    if (n_tris >= kMaxTris) break;
                    Standard_Integer n1, n2, n3;
                    tri->Triangle(ti).Get(n1, n2, n3);
                    if (reversed) std::swap(n2, n3);
                    indices.push_back(base + (n1 - 1));
                    indices.push_back(base + (n2 - 1));
                    indices.push_back(base + (n3 - 1));
                    ++n_tris;
                }
            }
            return json{{"positions", std::move(positions)}, {"indices", std::move(indices)},
                        {"_n_tris", n_tris}};
        };

        double defl = std::max(0.5, diag / 80.0);
        json mesh = mesh_at(defl);
        // Coarsen once if over the triangle cap (still return what fits).
        if (mesh.value("_n_tris", 0ull) >= 50000ull) {
            mesh = mesh_at(defl * 2.0);
        }
        mesh.erase("_n_tris");
        return mesh;
    } catch (const Standard_Failure&) {
        return empty;
    } catch (...) {
        return empty;
    }
}

// Build overlap_bbox [[min],[max]] from BRepBndLib::Add of the Common shape.
json eval_overlap_bbox(const TopoDS_Shape& common_shape) {
    try {
        Bnd_Box box;
        BRepBndLib::Add(common_shape, box);
        if (box.IsVoid()) return json::array();
        double xmn, ymn, zmn, xmx, ymx, zmx;
        box.Get(xmn, ymn, zmn, xmx, ymx, zmx);
        return json::array({json::array({xmn, ymn, zmn}), json::array({xmx, ymx, zmx})});
    } catch (const Standard_Failure&) {
        return json::array();
    } catch (...) {
        return json::array();
    }
}

// Pairwise interference report over a list of named (name, shape) members — the shared core
// of engine/doc/evaluate.py::interference (kernel.py::_fit_report). Pairwise overlap
// (BRepAlgoAPI_Common volume) + clearance (BRepExtrema min distance), per-part bbox.
// ok iff nothing interferes.
// `include_coincident` (B4, default false) additionally classifies a near-zero-clearance
// pair (common volume < eps AND DistShapeShape distance <= 1e-6) as a `contacts` entry
// instead of a `clearances` one (SW-parity "coincident faces" gap, docs/SW_PARITY_SPEC.md).
// `min_clearance_mm` (B4, default 0) tags a `clearances` entry below it with
// `"warning":"below min clearance"`.
json interference_report(const std::vector<std::pair<std::string, TopoDS_Shape>>& members,
                         bool include_coincident = false, double min_clearance_mm = 0.0,
                         bool quick = false) {
    const std::size_t n = members.size();
    constexpr double INTERFERENCE_EPS = 1e-3;
    constexpr double kCoincidentTol = 1e-6;

    // Per-part bbox + volume (kernel.py::_bbox_dict uses build123d bounding_box() ->
    // BRepBndLib::AddOptimal, the precise box, after BRepTools::Clean). The Bnd_Boxes are
    // kept for the pair loop's prefilter below.
    json parts = json::array();
    std::vector<Bnd_Box> boxes(n);
    for (std::size_t i = 0; i < n; ++i) {
        const TopoDS_Shape& shp = members[i].second;
        json part = {{"name", members[i].first}, {"volume", eval_vol_or_zero(shp)}};
        try {
            BRepTools::Clean(shp);
            Bnd_Box box;
            BRepBndLib::AddOptimal(shp, box);
            if (!box.IsVoid()) {
                double xmn, ymn, zmn, xmx, ymx, zmx;
                box.Get(xmn, ymn, zmn, xmx, ymx, zmx);
                part["bbox"] = json{{"min", {xmn, ymn, zmn}},
                                    {"max", {xmx, ymx, zmx}},
                                    {"size", {xmx - xmn, ymx - ymn, zmx - zmn}}};
                boxes[i] = box;
            } else {
                part["bbox"] = nullptr;
            }
        } catch (const Standard_Failure&) {
            part["bbox"] = nullptr;
        }
        parts.push_back(std::move(part));
    }

    // Axis-aligned gap between two precise bboxes: 0 when they overlap/touch, else the
    // Euclidean distance between the boxes — a LOWER bound on the true B-rep distance.
    auto bbox_gap = [&](std::size_t i, std::size_t j) -> double {
        if (boxes[i].IsVoid() || boxes[j].IsVoid()) return 0.0;  // unknown -> never prune
        return boxes[i].Distance(boxes[j]);
    };
    // `quick` (asm-fea F4): pairs whose bbox gap exceeds this window report the bbox gap
    // itself ("approx": true) instead of paying an exact BRepExtrema_DistShapeShape — on a
    // 40-body vehicle assembly the exact all-pairs distance pass alone took minutes. The
    // window keeps every possibly-tight pair exact (min-clearance warnings stay exact).
    const double quick_window_mm = std::max(10.0, min_clearance_mm * 2.0);

    json interferences = json::array();
    json clearances = json::array();
    json contacts = json::array();
    for (std::size_t i = 0; i < n; ++i) {
        const std::string& name_i = members[i].first;
        for (std::size_t j = i + 1; j < n; ++j) {
            const std::string& name_j = members[j].first;
            const double gap_lb = bbox_gap(i, j);
            // overlap volume (None on boolean failure -> Python warns + continue).
            // Disjoint bboxes (positive gap) mean the common volume is EXACTLY zero — skip
            // the boolean, the result is identical (lossless prefilter, all callers).
            bool ov_ok = false;
            double ov = 0.0;
            TopoDS_Shape common_shape;
            if (gap_lb > 0.0) {
                ov_ok = true;
            } else {
                try {
                    BRepAlgoAPI_Common common(members[i].second, members[j].second);
                    common.Build();
                    if (common.IsDone()) {
                        common_shape = common.Shape();
                        ov = eval_vol_or_zero(common_shape);
                        ov_ok = true;
                    }
                } catch (const Standard_Failure&) {
                }
            }
            if (!ov_ok) continue;  // matches the warnings-sink "interference check failed" path
            if (ov > INTERFERENCE_EPS) {
                json item = {{"a", name_i},
                             {"b", name_j},
                             {"volume", ov},
                             {"overlap_volume_mm3", ov}};
                // Bbox + mesh of the Common shape (never throws; empty mesh on failure).
                try {
                    if (!common_shape.IsNull()) {
                        json bb = eval_overlap_bbox(common_shape);
                        if (bb.is_array() && bb.size() == 2) item["overlap_bbox"] = std::move(bb);
                        item["overlap_mesh"] = eval_overlap_mesh(common_shape);
                    } else {
                        item["overlap_mesh"] =
                            json{{"positions", json::array()}, {"indices", json::array()}};
                    }
                } catch (...) {
                    item["overlap_mesh"] =
                        json{{"positions", json::array()}, {"indices", json::array()}};
                }
                interferences.push_back(std::move(item));
            } else if (quick && gap_lb > quick_window_mm) {
                // Far pair in quick mode: the bbox gap stands in for the exact distance.
                clearances.push_back(json{{"a", name_i}, {"b", name_j},
                                          {"distance", gap_lb}, {"approx", true}});
            } else {
                try {
                    BRepExtrema_DistShapeShape dss(members[i].second, members[j].second);
                    if (dss.IsDone()) {
                        const double dist = dss.Value();
                        if (include_coincident && dist <= kCoincidentTol) {
                            contacts.push_back(json{{"a", name_i}, {"b", name_j}, {"distance", dist}});
                        } else {
                            json c = {{"a", name_i}, {"b", name_j}, {"distance", dist}};
                            if (min_clearance_mm > 0.0 && dist < min_clearance_mm)
                                c["warning"] = "below min clearance";
                            clearances.push_back(std::move(c));
                        }
                    }
                } catch (const Standard_Failure&) {
                }
            }
        }
    }

    std::string summary;
    if (!interferences.empty()) {
        std::size_t big = 0;
        for (std::size_t k = 1; k < interferences.size(); ++k)
            if (interferences[k]["volume"].get<double>() > interferences[big]["volume"].get<double>())
                big = k;
        char buf[160];
        std::snprintf(buf, sizeof(buf), "%zu interfering pair(s); largest %s<->%s = %.1f mm^3",
                      interferences.size(), interferences[big]["a"].get<std::string>().c_str(),
                      interferences[big]["b"].get<std::string>().c_str(),
                      interferences[big]["volume"].get<double>());
        summary = buf;
    } else if (!clearances.empty()) {
        std::size_t tight = 0;
        for (std::size_t k = 1; k < clearances.size(); ++k)
            if (clearances[k]["distance"].get<double>() < clearances[tight]["distance"].get<double>())
                tight = k;
        char buf[160];
        std::snprintf(buf, sizeof(buf), "No interference. Tightest gap %s<->%s = %.2f mm",
                      clearances[tight]["a"].get<std::string>().c_str(),
                      clearances[tight]["b"].get<std::string>().c_str(),
                      clearances[tight]["distance"].get<double>());
        summary = buf;
    } else if (members.size() < 2) {
        summary = "No body pairs to check (1 body).";
    } else {
        summary = "No interference detected.";
    }

    return json{
        {"ok", interferences.empty()},
        {"summary", summary},
        {"pairs_checked", members.size() < 2 ? 0 : members.size() * (members.size() - 1) / 2},
        {"parts", parts},
        {"interferences", interferences},
        {"clearances", clearances},
        {"contacts", contacts},
    };
}

// engine/doc/evaluate.py::interference via worker.py::_eval_interference.
//   part doc      -> members = every surviving body, names "b1".."bN" (AICM order).
//   assembly doc  -> members = WORLD-placed component bodies, names = component names (port of
//                    session.py::asm_world_bodies, explode=False). This mirrors the oracle's
//                    s.is_assembly branch — without it an assembly always reported "no interference"
//                    because compile_document(asm).shapes is empty.
// `params` (B4): see the interference_report doc comment for include_coincident/min_clearance_mm.
json doc_eval_interference(const std::string& doc_id, const json& params) {
    DocLock lk;
    Session& s = require_session(doc_id);

    std::vector<std::pair<std::string, TopoDS_Shape>> members;
    if (s.is_assembly()) {
        if (!s.asm_result) rebuild_assembly(s);
        const AssemblyResult* ares = s.asm_result.get();
        if (ares)
            for (const AsmBodyOut& body : ares->bodies) {
                // interference measures the REAL assembly: no explode offset (explode=False).
                TopoDS_Shape world = placed_world_shape(body, std::nullopt);
                // Imported STEP bodies may arrive as compounds — explode to SOLID(s).
                members.emplace_back(body.name, eval_interference_member_shape(world));
            }
    } else {
        CompileResult r = compile_session(s);
        for (std::size_t i = 0; i < r.shapes.size(); ++i)
            members.emplace_back("b" + std::to_string(i + 1),
                                eval_interference_member_shape(r.shapes[i]));
    }
    // Optional `members` filter: case-sensitive exact match on the same names emitted in
    // parts[]/interferences[]. Absent or empty preserves all-members behaviour.
    if (params.contains("members") && params["members"].is_array() &&
        !params["members"].empty()) {
        std::unordered_set<std::string> keep;
        for (const auto& m : params["members"]) {
            if (m.is_string()) keep.insert(m.get<std::string>());
        }
        if (!keep.empty()) {
            std::vector<std::pair<std::string, TopoDS_Shape>> filtered;
            filtered.reserve(members.size());
            for (auto& mem : members) {
                if (keep.count(mem.first)) filtered.push_back(std::move(mem));
            }
            members.swap(filtered);
        }
    }
    // An EMPTY document is the trap: `check_interference` on nothing "passes" and the
    // caller records a verified placement (ME 490 O-38). A single body, or a `members`
    // filter that keeps one, is a legitimate no-pair result and stays ok:true with
    // pairs_checked = 0 so nobody mistakes it for a verification.
    if (members.empty())
        return json{{"ok", false}, {"reason", "insufficient_bodies"},
                    {"body_count", 0},
                    {"summary", "Interference requires bodies; this document has none."}};
    const bool include_coincident = params.contains("include_coincident") &&
                                    params["include_coincident"].is_boolean() &&
                                    params["include_coincident"].get<bool>();
    const double min_clearance_mm = params.contains("min_clearance_mm") &&
                                            params["min_clearance_mm"].is_number()
                                        ? params["min_clearance_mm"].get<double>() : 0.0;
    // `quick` (asm-fea F4, default false = oracle-exact): approximate FAR clearances by
    // their bbox gap ("approx": true) so narration callers don't pay the all-pairs exact
    // distance pass on large assemblies. Interference volumes stay exact either way.
    const bool quick = params.contains("quick") && params["quick"].is_boolean() &&
                       params["quick"].get<bool>();
    return interference_report(members, include_coincident, min_clearance_mm, quick);
}

// B2 — intra-part FEATURE-overlap detection (see session.h doc_eval_feature_overlap for the
// full contract). No Python analogue: assembly interference tests whole bodies across parts;
// this tests each feature's own pre-boolean TOOL SOLID (CompileResult::tool_recs, populated in
// document.cpp at every boolean callsite) against every OTHER feature's tool in the SAME part —
// catching a boss silently fused into another boss, a cut that swallows an earlier boss whole,
// or two cut tools that overlap (redundant material removal). Reuses the compile cache
// (ensure_compiled) the same way interactive read paths (resolve_pick, face_edges, doc_status)
// do, instead of unconditionally recompiling like doc_eval_interference above.
json doc_eval_feature_overlap(const std::string& doc_id, const json& params) {
    DocLock lk;
    Session& s = require_session(doc_id);
    const CompileResult& r = ensure_compiled(s);

    bool has_filter = false;
    std::set<std::string> filter;
    if (params.contains("features") && params["features"].is_array()) {
        has_filter = true;
        for (const json& v : params["features"])
            if (v.is_string()) filter.insert(v.get<std::string>());
    }
    const double min_volume_mm3 = params.contains("min_volume_mm3") &&
                                          params["min_volume_mm3"].is_number()
                                      ? params["min_volume_mm3"].get<double>() : 1e-3;

    auto feature_name = [&](const std::string& fid) -> std::string {
        const json* f = find_feature_c(s.doc, fid);
        return (f && f->contains("name") && (*f)["name"].is_string())
                   ? (*f)["name"].get<std::string>() : fid;
    };

    std::vector<json> pair_vec;
    const std::size_t n = r.tool_recs.size();
    for (std::size_t i = 0; i < n; ++i) {
        const ToolRec& a = r.tool_recs[i];
        for (std::size_t j = i + 1; j < n; ++j) {
            const ToolRec& b = r.tool_recs[j];
            if (a.feature_id == b.feature_id) continue;  // same feature's own tool records
            if (has_filter && !filter.count(a.feature_id) && !filter.count(b.feature_id)) continue;

            bool ov_ok = false;
            double vol = 0.0;
            try {
                BRepAlgoAPI_Common common(a.tool, b.tool);
                common.Build();
                if (common.IsDone()) {
                    vol = eval_vol_or_zero(common.Shape());
                    ov_ok = true;
                }
            } catch (const Standard_Failure&) {
            }
            if (!ov_ok || vol < min_volume_mm3) continue;

            std::string kind;
            if (a.role == "add" && b.role == "add") {
                kind = "boss_overlap";
            } else if (a.role == "cut" && b.role == "cut") {
                kind = "cut_overlap";
            } else {
                const ToolRec& add_rec = (a.role == "add") ? a : b;
                const double add_vol = eval_vol_or_zero(add_rec.tool);
                kind = (add_vol > 1e-12 && vol >= 0.99 * add_vol) ? "boss_swallowed"
                                                                  : "cut_into_boss";
            }
            pair_vec.push_back(json{
                {"a", a.feature_id},
                {"b", b.feature_id},
                {"a_name", feature_name(a.feature_id)},
                {"b_name", feature_name(b.feature_id)},
                {"kind", kind},
                {"volume_mm3", vol},
            });
        }
    }
    std::sort(pair_vec.begin(), pair_vec.end(), [](const json& x, const json& y) {
        return x["volume_mm3"].get<double>() > y["volume_mm3"].get<double>();
    });

    // dead_features: any feature that contributed a tool_rec but owns no surviving
    // face/edge (fully consumed downstream) and did not itself error.
    std::set<std::string> toolmakers;
    for (const ToolRec& tr : r.tool_recs) toolmakers.insert(tr.feature_id);
    json dead_features = json::array();
    for (const std::string& fid : toolmakers) {
        bool feature_ok = false;
        for (const FeatureStatus& st : r.statuses)
            if (st.id == fid) { feature_ok = st.ok; break; }
        if (!feature_ok) continue;
        const bool has_faces = r.owner_map.is_object() && r.owner_map.contains(fid) &&
                               r.owner_map[fid].contains("faces") &&
                               r.owner_map[fid]["faces"].is_array() &&
                               !r.owner_map[fid]["faces"].empty();
        if (!has_faces) dead_features.push_back(fid);
    }

    std::size_t n_swallowed = 0, n_boss = 0, n_cut = 0, n_into = 0;
    for (const json& p : pair_vec) {
        const std::string& k = p["kind"].get_ref<const std::string&>();
        if (k == "boss_swallowed") ++n_swallowed;
        else if (k == "boss_overlap") ++n_boss;
        else if (k == "cut_overlap") ++n_cut;
        else if (k == "cut_into_boss") ++n_into;
    }

    std::string summary;
    if (pair_vec.empty() && dead_features.empty()) {
        summary = "No feature overlap detected.";
    } else {
        char buf[224];
        std::snprintf(buf, sizeof(buf),
            "%zu overlapping pair(s) (%zu boss_swallowed, %zu boss_overlap, %zu cut_overlap, "
            "%zu cut_into_boss); %zu dead feature(s)",
            pair_vec.size(), n_swallowed, n_boss, n_cut, n_into, dead_features.size());
        summary = buf;
        if (!pair_vec.empty()) {
            const json& big = pair_vec.front();  // sorted volume desc
            char vbuf[192];
            std::snprintf(vbuf, sizeof(vbuf), "; largest %s<->%s (%s) = %.1f mm^3",
                          big["a_name"].get<std::string>().c_str(),
                          big["b_name"].get<std::string>().c_str(),
                          big["kind"].get<std::string>().c_str(),
                          big["volume_mm3"].get<double>());
            summary += vbuf;
        }
    }

    json pairs = json::array();
    for (json& p : pair_vec) pairs.push_back(std::move(p));

    return json{
        {"ok", n_swallowed == 0},
        {"summary", summary},
        {"pairs", pairs},
        {"dead_features", dead_features},
    };
}

// C1 (WS-C) — resolve one side of eval_compare: compile `doc_id` fresh, then pick either the
// named body ("b1".."bN", the same naming doc_eval_interference's members use, 1-based AICM
// order) or, by default, the fuse of every surviving solid body (mirrors doc_eval_interference's
// part-doc member collection). Caller already holds the DocLock.
TopoDS_Shape eval_compare_pick_shape(const std::string& doc_id, const json& params,
                                     const char* body_key) {
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    if (r.shapes.empty())
        throw OpError("invalid_op", "eval_compare: document '" + doc_id + "' has no bodies",
                      {{"doc_id", doc_id}});
    if (params.contains(body_key) && params[body_key].is_string()) {
        const std::string name = params[body_key].get<std::string>();
        if (name.size() > 1 && name.front() == 'b') {
            try {
                std::size_t consumed = 0;
                const long idx = std::stol(name.substr(1), &consumed);
                if (consumed == name.size() - 1 && idx >= 1 &&
                    static_cast<std::size_t>(idx) <= r.shapes.size())
                    return r.shapes[static_cast<std::size_t>(idx) - 1];
            } catch (const std::exception&) {
            }
        }
        throw OpError("invalid_op",
                      "eval_compare: unknown body '" + name + "' in document '" + doc_id + "'",
                      {{"doc_id", doc_id}, {"body", name}});
    }
    if (r.shapes.size() == 1) return r.shapes.front();
    return ::geom::fuse_many(
        r.shapes.front(), std::vector<TopoDS_Shape>(r.shapes.begin() + 1, r.shapes.end()));
}

// C1 (WS-C reverse-engineering) — general shape-vs-shape comparison; see session.h /
// recon.h::compare_shapes for the full contract. Both docs compile fresh so a compare always
// reflects the latest edit on either side (no ensure_compiled cache reuse).
json doc_eval_compare(const json& params) {
    DocLock lk;
    const std::string doc_a_id = params.value("doc_a", std::string());
    const std::string doc_b_id = params.value("doc_b", std::string());
    if (doc_a_id.empty() || doc_b_id.empty())
        throw OpError("invalid_op", "eval_compare requires doc_a and doc_b",
                      {{"reason", "missing doc id"}});
    TopoDS_Shape shape_a = eval_compare_pick_shape(doc_a_id, params, "body_a");
    TopoDS_Shape shape_b = eval_compare_pick_shape(doc_b_id, params, "body_b");
    return chudware::doc::compare_shapes(shape_a, shape_b);
}

// PLM graphical diff (git-diff-for-a-3D-model) — see session.h for the full param/return
// contract. Both sides are compiled FRESH from their RECIPE via eval_compare_pick_shape (the
// vault stores feature recipes, never solved state, so the only honest way to diff two revisions
// is to re-solve each one and boolean the results), then three OCCT booleans split the pair into
// the three semantic sets the image encodes:
//   added     = B - A  -> green  (present in the NEWER revision only)
//   removed   = A - B  -> red    (present in the OLDER revision only)
//   unchanged = A ^ B  -> grey   (ghosted at alpha 96 in "context" mode, omitted in
//                                 "changes_only" so an internal change is not buried in bulk)
// Every view and BOTH modes frame the camera on the A∪B fuse, so two renders of the same pair
// register pixel-for-pixel and can be overlaid or flipped between. Orientation tint is off
// (annotate=false) — it blends 55% of a direction hue in and would destroy the red/green
// semantics. Degrades to {ok:false, degraded:true, reason} (still writing a plain render of B
// when it can) instead of throwing on a boolean failure.
json doc_render_diff(const nlohmann::json& params) {
    try {
        DocLock lk;
        const std::string doc_a_id = params.value("doc_a", std::string());
        const std::string doc_b_id = params.value("doc_b", std::string());
        if (doc_a_id.empty() || doc_b_id.empty())
            throw OpError("invalid_op", "render_diff requires doc_a and doc_b",
                          {{"reason", "missing doc id"}});
        const std::string out_path = params.value("out_path", std::string());
        if (out_path.empty())
            throw OpError("invalid_op", "render_diff requires out_path",
                          {{"reason", "missing out_path"}});
        const std::string mode =
            params.value("mode", std::string("context")) == "changes_only" ? "changes_only"
                                                                          : "context";
        const bool label = params.value("annotate", true);
        int W = 640, H = 480;
        if (params.contains("width") && params["width"].is_number())
            W = (int)std::lround(params["width"].get<double>());
        if (params.contains("height") && params["height"].is_number())
            H = (int)std::lround(params["height"].get<double>());
        W = std::max(64, std::min(4096, W));
        H = std::max(64, std::min(4096, H));

        const TopoDS_Shape shape_a = eval_compare_pick_shape(doc_a_id, params, "body_a");
        const TopoDS_Shape shape_b = eval_compare_pick_shape(doc_b_id, params, "body_b");

        // recon.cpp keeps volume_or_zero file-local; the same fail-soft |Mass()| here.
        auto vol_of = [](const TopoDS_Shape& s) -> double {
            if (s.IsNull()) return 0.0;
            try {
                GProp_GProps p;
                BRepGProp::VolumeProperties(s, p);
                return std::abs(p.Mass());
            } catch (...) {
                return 0.0;
            }
        };
        // Disconnected-region count: TopExp_Explorer(TopAbs_SOLID) yields the shape itself when
        // it already IS a solid, so this is 1 for a single solid and N for a compound of N.
        auto n_solids = [](const TopoDS_Shape& s) -> int {
            if (s.IsNull()) return 0;
            int n = 0;
            for (TopExp_Explorer ex(s, TopAbs_SOLID); ex.More(); ex.Next()) ++n;
            return n;
        };

        const ResolvedView rv = resolve_view_spec(params.value("view", json()));
        const float kAdded[3]     = {0.16f, 0.72f, 0.32f};
        const float kRemoved[3]   = {0.86f, 0.24f, 0.24f};
        const float kUnchanged[3] = {0.62f, 0.64f, 0.67f};

        // Stable camera frame = A∪B. If the fuse fails, the concatenation of both sides' own
        // tessellations bounds the same union well enough to keep the framing shared.
        std::vector<aicm::AicmBody> frame;
        {
            TopoDS_Shape uni;
            try {
                BRepAlgoAPI_Fuse fuse_op(shape_a, shape_b);
                fuse_op.SetFuzzyValue(1e-4);
                fuse_op.Build();
                if (fuse_op.IsDone()) uni = fuse_op.Shape();
            } catch (const Standard_Failure&) {
            }
            if (!diff_body_from_shape(uni, 1, "frame", kUnchanged, frame)) {
                diff_body_from_shape(shape_a, 1, "frame_a", kUnchanged, frame);
                diff_body_from_shape(shape_b, 2, "frame_b", kUnchanged, frame);
            }
        }

        std::vector<aicm::AicmBody> draw;
        std::vector<unsigned char> alphas;
        auto push_body = [&](const TopoDS_Shape& s, uint32_t id, const char* nm,
                             const float col[3], unsigned char alpha) {
            if (diff_body_from_shape(s, id, nm, col, draw)) alphas.resize(draw.size(), alpha);
        };
        // One shared render+write tail so the degraded path produces a real image too. Same
        // encode_png_rgb + ofstream sequence doc_render_view uses.
        // `label` (params.annotate, default true) bakes the view name + legend + bbox dims
        // into the pixels. That is what an AI vision review needs from a bare PNG, but the
        // GUI already shows the view, the legend and the dimensions as real widgets, so it
        // asks for annotate:false and gets a clean image instead of a pixel-font overlay.
        auto write_png = [&]() -> bool {
            if (draw.empty() || frame.empty()) return false;
            std::string header;
            if (label) {
                const std::string dims = bbox_dims_label(frame);
                header = rv.name + "  DIFF  +added  -removed" +
                         (dims.empty() ? "" : "  " + dims);
            }
            std::vector<unsigned char> img;
            // min_painted=1: a changes_only render of one small added boss is legitimately
            // sparse and must not trip the default W*H/400 "nothing drawn" guard.
            if (!render_view_to_rgb(draw, {}, W, H, rv.dir, header, img, /*annotate=*/false,
                                    &alphas, &frame, /*min_painted=*/1))
                return false;
            const std::string png = encode_png_rgb(W, H, img);
            std::ofstream f(out_path, std::ios::binary | std::ios::trunc);
            return (bool)f && (f.write(png.data(), (std::streamsize)png.size()), (bool)f);
        };

        // Booleans mirror recon.cpp::compare_shapes exactly (fuzzy 1e-4 + IsDone checks).
        TopoDS_Shape common_s, removed_s, added_s;
        std::string bool_fail;
        try {
            BRepAlgoAPI_Common common_op(shape_a, shape_b);
            common_op.SetFuzzyValue(1e-4);
            common_op.Build();
            if (!common_op.IsDone()) bool_fail = "A^B common did not converge";
            else common_s = common_op.Shape();
            if (bool_fail.empty()) {
                BRepAlgoAPI_Cut cut_ab(shape_a, shape_b);
                cut_ab.SetFuzzyValue(1e-4);
                cut_ab.Build();
                if (!cut_ab.IsDone()) bool_fail = "A-B cut did not converge";
                else removed_s = cut_ab.Shape();
            }
            if (bool_fail.empty()) {
                BRepAlgoAPI_Cut cut_ba(shape_b, shape_a);
                cut_ba.SetFuzzyValue(1e-4);
                cut_ba.Build();
                if (!cut_ba.IsDone()) bool_fail = "B-A cut did not converge";
                else added_s = cut_ba.Shape();
            }
        } catch (const Standard_Failure& ex) {
            bool_fail = std::string("kernel exception: ") + ex.GetMessageString();
        }

        if (!bool_fail.empty()) {
            // No semantic split available — still hand the caller a plain grey render of B.
            draw.clear();
            alphas.clear();
            push_body(shape_b, 1, "b", kUnchanged, 255);
            const bool wrote = write_png();
            return json{{"ok", false},
                        {"degraded", true},
                        {"reason", "boolean_failed"},
                        {"error", bool_fail},
                        {"path", wrote ? out_path : std::string()},
                        {"view", rv.name},
                        {"mode", mode},
                        {"match_pct", nullptr},
                        {"added_volume_mm3", 0.0},
                        {"removed_volume_mm3", 0.0},
                        {"unchanged_volume_mm3", 0.0},
                        {"added_regions", 0},
                        {"removed_regions", 0},
                        {"identical", false}};
        }

        const double vol_a = vol_of(shape_a), vol_b = vol_of(shape_b);
        const double common_vol = vol_of(common_s);
        const double removed_vol = vol_of(removed_s), added_vol = vol_of(added_s);
        const int added_regions = n_solids(added_s), removed_regions = n_solids(removed_s);
        const double denom = std::max(vol_a, vol_b);
        const double match_pct = denom > 1e-12 ? 100.0 * common_vol / denom : 100.0;
        const bool identical = added_regions == 0 && removed_regions == 0 &&
                               added_vol < 1e-6 && removed_vol < 1e-6;

        // Draw order per contract: added, removed, then the ghosted unchanged bulk.
        push_body(added_s, 1, "added", kAdded, 255);
        push_body(removed_s, 2, "removed", kRemoved, 255);
        if (mode == "context") push_body(common_s, 3, "unchanged", kUnchanged, 96);
        const bool wrote = write_png();

        json out{{"ok", wrote},
                 {"path", wrote ? out_path : std::string()},
                 {"view", rv.name},
                 {"mode", mode},
                 {"match_pct", match_pct},
                 {"added_volume_mm3", added_vol},
                 {"removed_volume_mm3", removed_vol},
                 {"unchanged_volume_mm3", common_vol},
                 {"added_regions", added_regions},
                 {"removed_regions", removed_regions},
                 {"identical", identical}};
        if (!wrote) {
            out["degraded"] = true;
            out["reason"] = draw.empty() ? "nothing to draw" : "render failed";
        }
        return out;
    } catch (const OpError& e) {
        return json{{"ok", false}, {"error", e.what()}, {"code", e.code}};
    } catch (...) {
        return json{{"ok", false}, {"reason", "render exception"}};
    }
}

// C2 (WS-C) — resolve params.face into a live TopoDS_Face on `body`: either a stable "sid"
// handle (matched against make_selector's sid over the body's CURRENT faces — the handle
// GSL's doc_query/describe_part hand back) or a selector/geom-query object (eval_resolve_ref's
// own predicate-aware path — the same one doc_eval_measure's refs use).
TopoDS_Face doc_face_wires_resolve(const TopoDS_Shape& body, const json& face_ref) {
    if (!face_ref.is_object())
        throw OpError("invalid_op", "doc_face_wires: params.face must be an object",
                      {{"reason", "bad face ref"}});
    if (face_ref.contains("sid") && face_ref["sid"].is_string() && !face_ref.contains("kind") &&
        !chudware::doc::is_geom_query(face_ref)) {
        const std::string want = face_ref["sid"].get<std::string>();
        for (const TopoInfo& info : describe_faces(body)) {
            if (make_selector(info).value("sid", std::string()) != want) continue;
            chudware::doc::QueryPicks qp = chudware::doc::resolve_geom_query(
                body, json{{"of", std::string("face")},
                          {"near", {info.centroid.x(), info.centroid.y(), info.centroid.z()}},
                          {"count", 1}});
            if (qp.error.empty() && !qp.faces.empty()) return qp.faces.front();
        }
        throw OpError("invalid_op", "doc_face_wires: no face matches sid '" + want + "'",
                      {{"sid", want}});
    }
    const double diag = sim_bbox_diag(body);
    const TopoDS_Shape sub = eval_resolve_ref(face_ref, body, diag);
    if (sub.ShapeType() != TopAbs_FACE)
        throw OpError("invalid_op", "doc_face_wires: ref did not resolve to a face",
                      {{"reason", "not a face"}});
    return TopoDS::Face(sub);
}

// C2 (WS-C reverse-engineering) — convert-entities primitive; see session.h / recon.h::
// face_wires for the full contract.
json doc_face_wires(const std::string& doc_id, const json& params) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    if (r.shapes.empty())
        throw OpError("invalid_op", "doc_face_wires: document '" + doc_id + "' has no bodies",
                      {{"doc_id", doc_id}});
    const json face_ref = params.contains("face") ? params["face"] : json();
    if (face_ref.is_null())
        throw OpError("invalid_op", "doc_face_wires requires params.face",
                      {{"reason", "missing face"}});
    TopoDS_Face face = doc_face_wires_resolve(r.shapes.back(), face_ref);
    return chudware::doc::face_wires(face);
}


// engine/doc/evaluate.py::measure via worker.py::_eval_measure. Resolve 1-8 refs against
// the LAST body, then single / pair / multi-distance measure. Refs resolve in the SAME
// enumeration describe_* + body.faces()/edges()/vertices() use (a TopExp re-walk).
json doc_eval_measure(const std::string& doc_id, const json& refs) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    if (r.shapes.empty())
        throw OpError("invalid_op", "no bodies for measure", {{"reason", "no bodies"}});
    const TopoDS_Shape& shape = r.shapes.back();  // worker: bodies[-1]

    // bbox diag (build123d bounding_box() = BRepBndLib::AddOptimal), floor 1e-3, 100 on fail.
    double diag = 100.0;
    try {
        BRepTools::Clean(shape);
        Bnd_Box box;
        BRepBndLib::AddOptimal(shape, box);
        if (!box.IsVoid()) {
            double xmn, ymn, zmn, xmx, ymx, zmx;
            box.Get(xmn, ymn, zmn, xmx, ymx, zmx);
            double dx = xmx - xmn, dy = ymx - ymn, dz = zmx - zmn;
            diag = std::max(std::sqrt(dx * dx + dy * dy + dz * dz), 1e-3);
        }
    } catch (const Standard_Failure&) {
    }

    if (!refs.is_array() || refs.empty() || refs.size() > 8)
        throw OpError("invalid_op",
                      "measure() takes 1..8 refs, got " +
                          std::to_string(refs.is_array() ? refs.size() : 0));

    std::vector<TopoDS_Shape> shapes;
    shapes.reserve(refs.size());
    for (const auto& ref : refs) shapes.push_back(eval_resolve_ref(ref, shape, diag));

    if (shapes.size() == 1) return eval_measure_single(shapes[0]);
    if (shapes.size() == 2) return eval_measure_pair(shapes[0], shapes[1]);

    // 3+ refs: successive pairwise distances along the given order (v1 multi — no wire
    // topology). points = chain of extrema midpoints when each pair solves.
    json distances = json::array();
    json points = json::array();
    double total = 0.0;
    for (std::size_t i = 0; i + 1 < shapes.size(); ++i) {
        const json pair = eval_measure_pair(shapes[i], shapes[i + 1]);
        const double d = pair.value("distance", 0.0);
        distances.push_back(d);
        total += d;
        if (pair.contains("points") && pair["points"].is_array() &&
            pair["points"].size() == 2) {
            const json& p0 = pair["points"][0];
            const json& p1 = pair["points"][1];
            if (p0.is_array() && p0.size() == 3 && p1.is_array() && p1.size() == 3 &&
                p0[0].is_number() && p0[1].is_number() && p0[2].is_number() &&
                p1[0].is_number() && p1[1].is_number() && p1[2].is_number()) {
                points.push_back(json::array({
                    0.5 * (p0[0].get<double>() + p1[0].get<double>()),
                    0.5 * (p0[1].get<double>() + p1[1].get<double>()),
                    0.5 * (p0[2].get<double>() + p1[2].get<double>()),
                }));
            }
        }
    }
    json out = json{{"kind", "multi_distance"},
                    {"distances", distances},
                    {"total_distance", total}};
    if (!points.empty()) out["points"] = points;
    // Per-ref enrichment for multi path (anchor/polyline per resolved element).
    json elements = json::array();
    for (const TopoDS_Shape& sh : shapes) {
        json el = json::object();
        eval_measure_enrich(el, sh);
        elements.push_back(std::move(el));
    }
    out["elements"] = std::move(elements);
    return out;
}

// ----------------------------------------------------------- Evaluate field methods
// Ports of engine/doc/evaluate.py {draft_field, curvature_field, thickness_field} via the
// worker.py _eval_draft/_eval_curvature/_eval_thickness handlers. Unlike the analytic eval
// methods these tessellate the active body, compute a per-vertex SCALAR field, write it to a
// temp .aicm file (a SCLR chunk on the tessellated body) and return {"aicm_path": <path>}.
// Mirrors worker.py::_eval_write_field_aicm: tessellate (body_id=1) -> chunk_fn(tess,shape)
// -> write_aicm_file(tmp, [tess], extra_chunks=[chunk]).

namespace {

// Pick the active (or selected) body shape (mirrors eval_active_shape, but the field
// handlers always go through _eval_active_shape with the same body_id semantics).
const TopoDS_Shape& field_active_shape(const CompileResult& r, const std::string& doc_id,
                                       int body_id) {
    return eval_active_shape(r, doc_id, body_id);
}

// Tessellate `shape` (body_id=1, name `name`, default deflections matching tessellate.py)
// and return the AicmBody — the C++ equivalent of worker.py's tess.tessellate_body call.
aicm::AicmBody field_tessellate(const TopoDS_Shape& shape, const std::string& name) {
    const float color[3] = {0.71f, 0.76f, 0.82f};
    return ::geom::tessellate_body(shape, 1, name.empty() ? std::string("Body1") : name, color);
}

// Write the tessellated body + a single SCLR chunk via aicm-shm handoff (file fallback).
std::string field_write_temp_aicm(const aicm::AicmBody& body,
                                  const std::vector<uint8_t>& chunk) {
    std::vector<std::vector<uint8_t>> extras;
    extras.push_back(chunk);
    std::vector<uint8_t> blob = aicm::write_aicm({body}, extras);
    if (blob.empty())
        throw OpError("invalid_op", "failed to encode field aicm",
                      {{"reason", "aicm encode failed"}});
    static std::atomic<unsigned long long> seq{1};
    unsigned long long n = seq.fetch_add(1);
    const std::string key = "field_" + std::to_string(n);
    std::string uri = chudware::aicm_handoff::publish(key, blob);
    if (uri.empty())
        throw OpError("invalid_op", "failed to publish field aicm",
                      {{"reason", "aicm publish failed"}});
    return uri;
}

// Per-vertex unit normal from the body's flat nrm buffer (3*i..3*i+2), normalized like the
// oracle (nrm / max(|nrm|, eps)). vi is 0-based.
inline void field_vertex_normal(const aicm::AicmBody& body, std::size_t vi, double out[3]) {
    double x = body.nrm[3 * vi + 0];
    double y = body.nrm[3 * vi + 1];
    double z = body.nrm[3 * vi + 2];
    double len = std::sqrt(x * x + y * y + z * z);
    double inv = (len > 1e-12) ? 1.0 / len : 1.0;
    out[0] = x * inv;
    out[1] = y * inv;
    out[2] = z * inv;
}

// engine/doc/evaluate.py::_moller_trumbore_all — for each ray, the smallest positive t
// (distance along a UNIT direction) to a triangle of the mesh; inf if no hit. Ported 1:1
// (same EPS=1e-10, u/v barycentric bounds, t>EPS guard). directions are unit vectors.
//
// TWO-SIDED (intentional, load-bearing for wall thickness): the determinant test below is
// `std::abs(a) <= EPS` — it rejects ONLY rays parallel to a triangle, accepting BOTH front-
// and back-facing triangles. This is what makes the thickness cast (doc_eval_thickness) find
// the NEAREST opposite wall: the near inner-shell wall is wound toward the cavity (back-facing
// to an inward ray), so a one-sided / back-face-culling cast would SKIP it and cross the whole
// cavity to the far wall (reporting the cavity span, not the wall). Do NOT add a sign-of-`a`
// cull here without giving doc_eval_thickness its own two-sided path.
std::vector<double> field_moller_trumbore_all(const std::vector<float>& pos,
                                              const std::vector<uint32_t>& idx,
                                              const std::vector<std::array<double, 3>>& origins,
                                              const std::vector<std::array<double, 3>>& dirs) {
    constexpr double EPS = 1e-10;
    const std::size_t n_rays = origins.size();
    const std::size_t n_tris = idx.size() / 3;
    std::vector<double> dist(n_rays, std::numeric_limits<double>::infinity());

    auto vtx = [&](uint32_t i, double out[3]) {
        out[0] = pos[3 * i + 0];
        out[1] = pos[3 * i + 1];
        out[2] = pos[3 * i + 2];
    };
    auto cross = [](const double a[3], const double b[3], double o[3]) {
        o[0] = a[1] * b[2] - a[2] * b[1];
        o[1] = a[2] * b[0] - a[0] * b[2];
        o[2] = a[0] * b[1] - a[1] * b[0];
    };
    auto dot = [](const double a[3], const double b[3]) {
        return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
    };

    // Precompute triangle edges (matches the oracle's V0/E1/E2 layout).
    std::vector<std::array<double, 3>> V0(n_tris), E1(n_tris), E2(n_tris);
    for (std::size_t t = 0; t < n_tris; ++t) {
        double v0[3], v1[3], v2[3];
        vtx(idx[3 * t + 0], v0);
        vtx(idx[3 * t + 1], v1);
        vtx(idx[3 * t + 2], v2);
        V0[t] = {v0[0], v0[1], v0[2]};
        E1[t] = {v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2]};
        E2[t] = {v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2]};
    }

    for (std::size_t ri = 0; ri < n_rays; ++ri) {
        const double d[3] = {dirs[ri][0], dirs[ri][1], dirs[ri][2]};
        const double o[3] = {origins[ri][0], origins[ri][1], origins[ri][2]};
        double best = std::numeric_limits<double>::infinity();
        for (std::size_t t = 0; t < n_tris; ++t) {
            const double e1[3] = {E1[t][0], E1[t][1], E1[t][2]};
            const double e2[3] = {E2[t][0], E2[t][1], E2[t][2]};
            double h[3];
            cross(d, e2, h);
            double a = dot(e1, h);
            if (std::abs(a) <= EPS) continue;
            double inv_a = 1.0 / a;
            const double s[3] = {o[0] - V0[t][0], o[1] - V0[t][1], o[2] - V0[t][2]};
            double u = inv_a * dot(s, h);
            if (u < 0.0 || u > 1.0) continue;
            double q[3];
            cross(s, e1, q);
            double v = inv_a * dot(q, d);
            if (v < 0.0 || u + v > 1.0) continue;
            double tval = inv_a * dot(e2, q);
            if (tval > EPS && tval < best) best = tval;
        }
        dist[ri] = best;
    }
    return dist;
}

// Sample the TRUE surface of every face whose triangulation touches the sphere
// (`near`, `radius`), appending world-space points to `out`. Vertex-only sampling is not
// enough: a cylindrical B-rep face tessellates as a strip of long triangles whose nodes
// all sit on the two cap rings, so a sphere aimed at the mid-wall (exactly where the
// docstring tells the agent to aim) contains ZERO nodes. Here each triangle is walked on a
// barycentric grid of spacing <= radius/4, so any patch larger than the search sphere is
// covered densely; and when the face carries UV nodes the barycentric weights are applied
// to the (u,v) parameters and the point comes from Geom_Surface::Value — exact on an
// analytic cylinder, not the chord of its tessellation. Faces without UV nodes (imported
// SW/STL meshes) fall back to the planar barycentric point on the triangle itself.
// Always contributes the 3 vertices + the centroid of a touching triangle, so a sparse
// mesh still yields its own nodes. Stops at `cap` samples.
void fit_sample_faces(const TopoDS_Shape& shape, const std::array<double, 3>& near_pt,
                      double radius, std::size_t cap, std::vector<Eigen::Vector3d>& out) {
    const double rad2 = radius * radius;
    const double step = radius * 0.25;  // grid spacing <= search_radius/4

    auto keep = [&](double x, double y, double z) {
        const double dx = x - near_pt[0], dy = y - near_pt[1], dz = z - near_pt[2];
        if (dx * dx + dy * dy + dz * dz <= rad2) out.emplace_back(x, y, z);
    };

    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More() && out.size() < cap; ex.Next()) {
        const TopoDS_Face face = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
        if (tri.IsNull()) {
            try {
                BRepMesh_IncrementalMesh(face, 0.5, Standard_False, 0.5, Standard_True).Perform();
            } catch (const Standard_Failure&) {
            }
            tri = BRep_Tool::Triangulation(face, loc);
            if (tri.IsNull()) continue;
        }
        const gp_Trsf trsf = loc.Transformation();
        const bool identity = loc.IsIdentity();

        // Parametric surface (same location as the triangulation, so one trsf serves both).
        TopLoc_Location sloc;
        Handle(Geom_Surface) surf;
        bool use_uv = tri->HasUVNodes();
        if (use_uv) {
            surf = BRep_Tool::Surface(face, sloc);
            use_uv = !surf.IsNull();
        }

        for (int t = 1; t <= tri->NbTriangles() && out.size() < cap; ++t) {
            int i1 = 0, i2 = 0, i3 = 0;
            tri->Triangle(t).Get(i1, i2, i3);
            gp_Pnt p[3] = {tri->Node(i1), tri->Node(i2), tri->Node(i3)};
            if (!identity)
                for (gp_Pnt& q : p) q.Transform(trsf);

            // Cheap reject: the whole triangle is out of reach when its nearest vertex is
            // farther than radius + its own extent.
            double vd[3], maxe = 0.0, mind = std::numeric_limits<double>::infinity();
            for (int k = 0; k < 3; ++k) {
                vd[k] = p[k].Distance(gp_Pnt(near_pt[0], near_pt[1], near_pt[2]));
                mind = std::min(mind, vd[k]);
                maxe = std::max(maxe, p[k].Distance(p[(k + 1) % 3]));
            }
            if (mind > radius + maxe) continue;

            // Vertices + centroid always (sparse-mesh floor).
            for (int k = 0; k < 3; ++k) keep(p[k].X(), p[k].Y(), p[k].Z());
            keep((p[0].X() + p[1].X() + p[2].X()) / 3.0, (p[0].Y() + p[1].Y() + p[2].Y()) / 3.0,
                 (p[0].Z() + p[1].Z() + p[2].Z()) / 3.0);

            // Barycentric grid: n edge subdivisions gives (n+1)(n+2)/2 nodes at spacing
            // maxe/n <= step.
            int n = (step > 0.0) ? static_cast<int>(std::ceil(maxe / step)) : 1;
            n = std::max(1, std::min(n, 48));
            if (n < 2) continue;

            gp_Pnt2d uv[3];
            if (use_uv) {
                uv[0] = tri->UVNode(i1);
                uv[1] = tri->UVNode(i2);
                uv[2] = tri->UVNode(i3);
            }
            const double inv_n = 1.0 / static_cast<double>(n);
            for (int i = 0; i <= n && out.size() < cap; ++i) {
                for (int j = 0; i + j <= n; ++j) {
                    const double b1 = i * inv_n, b2 = j * inv_n, b0 = 1.0 - b1 - b2;
                    if (use_uv) {
                        const double u = b0 * uv[0].X() + b1 * uv[1].X() + b2 * uv[2].X();
                        const double v = b0 * uv[0].Y() + b1 * uv[1].Y() + b2 * uv[2].Y();
                        gp_Pnt sp;
                        try {
                            sp = surf->Value(u, v);
                        } catch (const Standard_Failure&) {
                            continue;
                        }
                        if (!identity) sp.Transform(trsf);
                        keep(sp.X(), sp.Y(), sp.Z());
                    } else {
                        keep(b0 * p[0].X() + b1 * p[1].X() + b2 * p[2].X(),
                             b0 * p[0].Y() + b1 * p[1].Y() + b2 * p[2].Y(),
                             b0 * p[0].Z() + b1 * p[1].Z() + b2 * p[2].Z());
                    }
                }
            }
        }
    }
}

}  // namespace

// Evaluate — recover the AXIS of a cylindrical feature (a bore, a shaft, a hub) from the
// body's own surface geometry, by least-squares fit. This is the mating primitive for
// merging INDEPENDENTLY SOURCED CAD: an imported body has no parametric feature whose
// axis could be read, so the axis has to be measured, and a measured axis is only usable
// as evidence if it comes with a residual. `near` is a world point on/near the wanted
// surface and `search_radius_mm` the cull radius around it; every surface SAMPLE (see
// fit_sample_faces — a barycentric grid on the true surface, not just mesh vertices)
// inside that sphere feeds geom::fit_cylinder.
//
// body_id is 1-based; <= 0 selects the LAST body (the eval_active_shape convention).
// Returns {axis_point[3], axis_dir[3] (unit), radius_mm, rms_residual_mm, n_points_used}.
// A cull that catches too few points, or a patch that is not a cylinder, is an OpError
// rather than a plausible-looking fit — silently returning the best plane-through-noise is
// exactly how a wheel ends up welded 3 mm off a hub.
json doc_fit_cylinder_axis(const std::string& doc_id, int body_id,
                           const std::array<double, 3>& near_pt, double search_radius_mm) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (!(search_radius_mm > 0.0))
        throw OpError("invalid_op",
                      "search_radius_mm must be > 0 (it is the cull radius around `near`)",
                      {{"reason", "bad search_radius_mm"}});
    CompileResult r = compile_session(s);
    const TopoDS_Shape& shape = field_active_shape(r, doc_id, body_id);
    try {
        BRepMesh_IncrementalMesh(shape, 0.1, Standard_False, 0.5, Standard_True).Perform();
    } catch (const Standard_Failure&) {
    }

    std::vector<Eigen::Vector3d> pts;
    fit_sample_faces(shape, near_pt, search_radius_mm, 20000, pts);
    if (pts.size() < 6)
        throw OpError("invalid_op",
                      "only " + std::to_string(pts.size()) + " surface samples lie within " +
                          "search_radius_mm=" + std::to_string(search_radius_mm) + " mm of [" +
                          std::to_string(near_pt[0]) + "," + std::to_string(near_pt[1]) + "," +
                          std::to_string(near_pt[2]) + "] — a cylinder fit needs at least 6. "
                          "Grow search_radius_mm, or check that `near` is on the surface "
                          "(use eval_measure / face_points to get a point on it).",
                      {{"reason", "too few points in search radius"},
                       {"n_points", static_cast<int>(pts.size())}});

    const ::chudware::geom::FitCylinderResult fit = ::chudware::geom::fit_cylinder(pts);
    if (!fit.ok)
        throw OpError("invalid_op", "cylinder fit failed: " + fit.message,
                      {{"reason", "cylinder fit failed"},
                       {"n_points", static_cast<int>(pts.size())}});

    return json{
        {"axis_point", {fit.axis_point.x(), fit.axis_point.y(), fit.axis_point.z()}},
        {"axis_dir", {fit.axis_dir.x(), fit.axis_dir.y(), fit.axis_dir.z()}},
        {"radius_mm", fit.radius},
        {"rms_residual_mm", fit.rms_residual},
        {"n_points_used", static_cast<int>(fit.n_points)},
    };
}

// engine/doc/evaluate.py::draft_field. Per-vertex draft angle = degrees(arcsin(clip(n·pull,
// -1,1))) for the normalized vertex normal n and normalized pull. chunk name "draft". Beyond
// the field, returns the moldability summary: min/max draft over the body, and (using
// angle_deg as the minimum-draft threshold, default 3°) the count + list of FACES whose
// worst-case |draft| is below it — the walls that would drag on the mold during ejection.
json doc_eval_draft(const std::string& doc_id, int body_id, const std::array<double, 3>& pull_in,
                    double angle_deg) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    const TopoDS_Shape& shape = field_active_shape(r, doc_id, body_id);
    const std::string name = (body_id >= 1 && body_id <= static_cast<int>(r.bodies_info.size()))
                                 ? r.bodies_info[static_cast<std::size_t>(body_id - 1)].name
                             : (!r.bodies_info.empty() ? r.bodies_info.back().name : std::string());
    aicm::AicmBody body = field_tessellate(shape, name);

    double pull[3] = {pull_in[0], pull_in[1], pull_in[2]};
    double plen = std::sqrt(pull[0] * pull[0] + pull[1] * pull[1] + pull[2] * pull[2]);
    double pinv = 1.0 / (plen + 1e-12);  // oracle: pull / (norm + 1e-12)
    pull[0] *= pinv;
    pull[1] *= pinv;
    pull[2] *= pinv;

    const std::size_t n = body.n_verts();
    std::vector<float> values(n, 0.0f);
    float vmin = std::numeric_limits<float>::infinity();
    float vmax = -std::numeric_limits<float>::infinity();
    bool any_finite = false;
    for (std::size_t i = 0; i < n; ++i) {
        double nrm[3];
        field_vertex_normal(body, i, nrm);
        double cos_t = nrm[0] * pull[0] + nrm[1] * pull[1] + nrm[2] * pull[2];
        if (cos_t < -1.0) cos_t = -1.0;
        if (cos_t > 1.0) cos_t = 1.0;
        float draft = static_cast<float>(std::asin(cos_t) * (180.0 / 3.14159265358979323846));
        values[i] = draft;
        if (std::isfinite(draft)) {
            any_finite = true;
            vmin = std::min(vmin, draft);
            vmax = std::max(vmax, draft);
        }
    }
    if (!any_finite) {
        vmin = -90.0f;
        vmax = 90.0f;
    }

    // Per-FACE moldability classification. For each B-rep face, take its worst-case (minimum
    // over the face's vertices) absolute draft, and its area + area-weighted centroid. A face
    // is "below threshold" iff its worst |draft| < threshold (the shallowest point governs
    // whether it drags on the mold). threshold = angle_deg when given, else the common 3°
    // injection-molding minimum.
    const double threshold = (angle_deg > 0.0) ? angle_deg : 3.0;
    json below = json::array();
    int n_below = 0;
    for (const aicm::FaceRec& fr : body.faces) {
        if (fr.tri_count == 0) continue;
        std::size_t lo = static_cast<std::size_t>(fr.first_tri) * 3;
        std::size_t hi = static_cast<std::size_t>(fr.first_tri + fr.tri_count) * 3;
        if (hi > body.idx.size()) hi = body.idx.size();

        double face_area = 0.0;
        double cw[3] = {0.0, 0.0, 0.0};  // area-weighted centroid accumulator
        double min_abs_draft = std::numeric_limits<double>::infinity();
        bool have_draft = false;
        for (std::size_t k = lo; k + 2 < hi; k += 3) {
            uint32_t a = body.idx[k], b = body.idx[k + 1], c = body.idx[k + 2];
            if (a >= n || b >= n || c >= n) continue;
            const float* pa = &body.pos[3 * a];
            const float* pb = &body.pos[3 * b];
            const float* pc = &body.pos[3 * c];
            double e1[3] = {pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]};
            double e2[3] = {pc[0] - pa[0], pc[1] - pa[1], pc[2] - pa[2]};
            double cx_ = e1[1] * e2[2] - e1[2] * e2[1];
            double cy_ = e1[2] * e2[0] - e1[0] * e2[2];
            double cz_ = e1[0] * e2[1] - e1[1] * e2[0];
            double tri_area = 0.5 * std::sqrt(cx_ * cx_ + cy_ * cy_ + cz_ * cz_);
            face_area += tri_area;
            double tcx = (pa[0] + pb[0] + pc[0]) / 3.0;
            double tcy = (pa[1] + pb[1] + pc[1]) / 3.0;
            double tcz = (pa[2] + pb[2] + pc[2]) / 3.0;
            cw[0] += tcx * tri_area;
            cw[1] += tcy * tri_area;
            cw[2] += tcz * tri_area;
            for (uint32_t vi : {a, b, c}) {
                double dr = std::abs(static_cast<double>(values[vi]));
                if (std::isfinite(dr)) {
                    min_abs_draft = std::min(min_abs_draft, dr);
                    have_draft = true;
                }
            }
        }
        if (!have_draft || face_area <= 0.0) continue;
        if (min_abs_draft < threshold) {
            ++n_below;
            below.push_back(json{
                {"face_id", fr.face_id},
                {"min_draft_deg", min_abs_draft},
                {"area_mm2", face_area},
                {"centroid", {cw[0] / face_area, cw[1] / face_area, cw[2] / face_area}},
            });
        }
    }

    auto chunk = aicm::scalar_chunk(body.body_id, "draft", values, vmin, vmax);
    return json{
        {"aicm_path", field_write_temp_aicm(body, chunk)},
        {"min_draft_deg", any_finite ? json(static_cast<double>(vmin)) : json(nullptr)},
        {"max_draft_deg", any_finite ? json(static_cast<double>(vmax)) : json(nullptr)},
        {"threshold_deg", threshold},
        {"n_faces_below_threshold", n_below},
        {"faces_below_threshold", below},
    };
}

// engine/doc/evaluate.py::curvature_field. Re-mesh (BRepMesh 0.1) to match the oracle's face
// ordering, then per-vertex principal curvatures via BRepLProp_SLProps at the vertex's UV
// (projected via ShapeAnalysis_Surface::ValueOfUV). kind: mean=(k1+k2)/2, gaussian=k1*k2,
// max=max(|k1|,|k2|). chunk name "curvature_<kind>". Verts with no defined curvature stay NaN.
json doc_eval_curvature(const std::string& doc_id, int body_id, const std::string& kind) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    const TopoDS_Shape& shape = field_active_shape(r, doc_id, body_id);
    const std::string name = (body_id >= 1 && body_id <= static_cast<int>(r.bodies_info.size()))
                                 ? r.bodies_info[static_cast<std::size_t>(body_id - 1)].name
                             : (!r.bodies_info.empty() ? r.bodies_info.back().name : std::string());
    aicm::AicmBody body = field_tessellate(shape, name);

    const std::size_t n_verts = body.n_verts();
    const float NaNf = std::numeric_limits<float>::quiet_NaN();
    std::vector<float> values(n_verts, NaNf);

    // Re-mesh + build the same 1-based face map tessellate_body used (the oracle remeshes
    // raw at (0.1,False,0.5,True) before mapping faces — FaceRec.face_id is this 1-based idx).
    BRepMesh_IncrementalMesh(shape, 0.1, Standard_False, 0.5, Standard_True).Perform();
    TopTools_IndexedMapOfShape face_map;
    TopExp::MapShapes(shape, TopAbs_FACE, face_map);

    for (const aicm::FaceRec& fr : body.faces) {
        const int fi = static_cast<int>(fr.face_id);  // 1-based into face_map
        if (fi < 1 || fi > face_map.Extent() || fr.tri_count == 0) continue;
        TopoDS_Face face = TopoDS::Face(face_map.FindKey(fi));
        BRepAdaptor_Surface sa(face);

        // Vertex set of this face = unique indices over its triangle range in the flat idx
        // buffer ([first_tri*3 .. (first_tri+tri_count)*3)). Matches the oracle's
        // np.unique(tess.idx[tri_start:tri_end]).
        std::size_t lo = static_cast<std::size_t>(fr.first_tri) * 3;
        std::size_t hi = static_cast<std::size_t>(fr.first_tri + fr.tri_count) * 3;
        if (hi > body.idx.size()) hi = body.idx.size();

        TopLoc_Location loc;
        Handle(Geom_Surface) occ_surf = BRep_Tool::Surface(face, loc);
        if (occ_surf.IsNull()) continue;
        ShapeAnalysis_Surface sas(occ_surf);

        double u_first = sa.FirstUParameter(), u_last = sa.LastUParameter();
        double v_first = sa.FirstVParameter(), v_last = sa.LastVParameter();

        std::set<uint32_t> seen;
        for (std::size_t k = lo; k < hi; ++k) {
            uint32_t vi = body.idx[k];
            if (vi >= n_verts || !seen.insert(vi).second) continue;
            gp_Pnt p3d(body.pos[3 * vi + 0], body.pos[3 * vi + 1], body.pos[3 * vi + 2]);
            gp_Pnt2d uv = sas.ValueOfUV(p3d, 1e-3);
            double u = std::max(u_first, std::min(u_last, uv.X()));
            double v = std::max(v_first, std::min(v_last, uv.Y()));
            try {
                BRepLProp_SLProps sl(sa, u, v, 2, 1e-7);
                if (sl.IsCurvatureDefined()) {
                    double k1 = sl.MinCurvature();
                    double k2 = sl.MaxCurvature();
                    double val;
                    if (kind == "gaussian")
                        val = k1 * k2;
                    else if (kind == "max")
                        val = std::max(std::abs(k1), std::abs(k2));
                    else
                        val = (k1 + k2) / 2.0;
                    values[vi] = static_cast<float>(val);
                }
            } catch (const Standard_Failure&) {
            }
        }
    }

    float vmin = 0.0f, vmax = 0.0f;
    bool any = false;
    for (float v : values) {
        if (std::isfinite(v)) {
            if (!any) {
                vmin = vmax = v;
                any = true;
            } else {
                vmin = std::min(vmin, v);
                vmax = std::max(vmax, v);
            }
        }
    }

    auto chunk = aicm::scalar_chunk(body.body_id, "curvature_" + kind, values, vmin, vmax);
    json out{{"aicm_path", field_write_temp_aicm(body, chunk)}};
    out["min_curvature"] = any ? json(static_cast<double>(vmin)) : json(nullptr);
    out["max_curvature"] = any ? json(static_cast<double>(vmax)) : json(nullptr);
    out["kind"] = kind;
    return out;
}

// engine/doc/evaluate.py::thickness_field. Per-vertex inward-raycast wall thickness: from each
// vertex offset 1e-3 along -normal (the inward direction, since field_vertex_normal returns the
// OUTWARD surface normal), cast -normal and take the NEAREST re-intersection distance (minus the
// offset). The cast is TWO-SIDED (field_moller_trumbore_all accepts triangles of either winding)
// so it stops at the FIRST opposite wall — for a hollow shell that is the thin wall, not the far
// side across the cavity. The oracle's primary path is trimesh BVH; we port the deterministic
// Moller-Trumbore FALLBACK (_moller_trumbore_all). chunk name "thickness". NOTE: parity here is
// PHYSICAL (a tolerance band), not bit-exact — the oracle's BVH runs on a divergent mesh.
// CAVEAT: thickness is a property of the SOLID. A solid block reports its full span (a 40 mm
// cube -> ~40 mm), which is correct; only a body with a genuine cavity (e.g. an open shell)
// has a thin wall to report (~2 mm for a 2 mm shell).
json doc_eval_thickness(const std::string& doc_id, int body_id) {
    DocLock lk;
    Session& s = require_session(doc_id);
    CompileResult r = compile_session(s);
    const TopoDS_Shape& shape = field_active_shape(r, doc_id, body_id);
    const std::string name = (body_id >= 1 && body_id <= static_cast<int>(r.bodies_info.size()))
                                 ? r.bodies_info[static_cast<std::size_t>(body_id - 1)].name
                             : (!r.bodies_info.empty() ? r.bodies_info.back().name : std::string());
    aicm::AicmBody body = field_tessellate(shape, name);

    const std::size_t n = body.n_verts();
    const float NaNf = std::numeric_limits<float>::quiet_NaN();
    std::vector<float> values(n, NaNf);

    constexpr double OFFSET = 1e-3;
    std::vector<std::array<double, 3>> origins(n), dirs(n);
    for (std::size_t i = 0; i < n; ++i) {
        double nrm[3];
        field_vertex_normal(body, i, nrm);
        origins[i] = {body.pos[3 * i + 0] - nrm[0] * OFFSET,
                      body.pos[3 * i + 1] - nrm[1] * OFFSET,
                      body.pos[3 * i + 2] - nrm[2] * OFFSET};
        dirs[i] = {-nrm[0], -nrm[1], -nrm[2]};  // inward (unit, since normal is unit)
    }

    std::vector<double> hits = field_moller_trumbore_all(body.pos, body.idx, origins, dirs);

    float vmin = 0.0f, vmax = 0.0f;
    double sum = 0.0;
    std::size_t n_sampled = 0;  // vertices with a valid through-wall hit
    bool any = false;
    for (std::size_t i = 0; i < n; ++i) {
        double d = hits[i];
        if (std::isfinite(d) && d > OFFSET) {
            float t = static_cast<float>(d - OFFSET);
            values[i] = t;
            sum += t;
            ++n_sampled;
            if (!any) {
                vmin = vmax = t;
                any = true;
            } else {
                vmin = std::min(vmin, t);
                vmax = std::max(vmax, t);
            }
        }
    }

    auto chunk = aicm::scalar_chunk(body.body_id, "thickness", values, vmin, vmax);
    json out{{"aicm_path", field_write_temp_aicm(body, chunk)}};
    // Numeric summary over the sampled (valid-hit) vertices. min is the key value — the
    // thinnest wall. null when no vertex got a through-wall hit (e.g. an open/sheet body).
    if (n_sampled > 0) {
        out["min_thickness_mm"] = static_cast<double>(vmin);
        out["max_thickness_mm"] = static_cast<double>(vmax);
        out["mean_thickness_mm"] = sum / static_cast<double>(n_sampled);
    } else {
        out["min_thickness_mm"] = nullptr;
        out["max_thickness_mm"] = nullptr;
        out["mean_thickness_mm"] = nullptr;
    }
    out["n_sampled"] = static_cast<int>(n_sampled);
    return out;
}

json doc_push_pull(const std::string& doc_id, int body_id, int topo_id, double distance,
                   bool commit) {
    DocLock lk;
    Session& s = require_session(doc_id);
    // plan_push_pull compiles + resolves the face owner (throws OpError on a bad
    // body_id/topo_id), never mutating the doc.
    PushPullPlan plan = plan_push_pull(s.doc, env_from_doc(s.doc), body_id, topo_id, distance);

    // The draggable plan fields (axis is JSON null when not axis-aligned).
    auto add_plan_fields = [&](json& out) {
        out["feature_id"] = plan.owner_fid;
        out["feature_type"] = plan.ftype;
        out["param"] = plan.param;
        out["axis"] = plan.has_axis ? json(plan.axis) : json(nullptr);
        out["old_value"] = plan.old_value;
        out["new_value"] = plan.new_value;
        out["distance"] = distance;
        out["clamped"] = plan.clamped;
    };
    auto merge = [](json& out, const json& resp) {
        for (auto it = resp.begin(); it != resp.end(); ++it) out[it.key()] = it.value();
    };

    if (!plan.draggable) {
        json out = {{"ok", true}, {"draggable", false}, {"reason", plan.reason}};
        if (!plan.owner_fid.empty()) out["feature_id"] = plan.owner_fid;
        if (!plan.ftype.empty()) out["feature_type"] = plan.ftype;
        return out;
    }

    // A zero-distance drag is a no-op preview: report the plan + a fresh rebuild, no
    // mutation / no revision bump.
    if (std::abs(plan.new_value - plan.old_value) < 1e-12) {
        CompileResult r = compile_session(s);
        json out = {{"ok", true}, {"draggable", true}};
        add_plan_fields(out);
        merge(out, ops_response(s, r, json::object(), /*with_tess=*/true));
        return out;
    }

    // Apply the param delta on a working copy (transactional — a throw leaves the
    // session untouched). Keep a dim object's shape; clear any stale expr.
    const std::string pre_snapshot = s.doc.dump();
    json working = s.doc;
    json* nf = find_feature(working, plan.owner_fid);
    if (!nf) {  // owner came from the owner map, so this should not happen
        json out = {{"ok", true}, {"draggable", false},
                    {"reason", "owner feature '" + plan.owner_fid + "' vanished before edit"},
                    {"feature_id", plan.owner_fid}, {"feature_type", plan.ftype}};
        return out;
    }
    if (!nf->contains("params") || !(*nf)["params"].is_object()) (*nf)["params"] = json::object();
    json& p = (*nf)["params"];
    if (p.contains(plan.param) && p[plan.param].is_object()) {
        p[plan.param]["value"] = plan.new_value;
        p[plan.param]["expr"] = nullptr;
    } else {
        p[plan.param] = {{"value", plan.new_value}, {"expr", nullptr}};
    }

    // Commit pushes ONE undo entry of the pre-edit doc; a transient preview pushes none.
    if (commit) {
        s.undo_stack.push_back(pre_snapshot);
        trim_history(s.doc_id, s.undo_stack);
        for (const std::string& e : s.redo_stack) drop_history_entry(e);
        s.redo_stack.clear();
    }
    s.doc = std::move(working);
    s.revision += 1;
    s.dirty = true;

    CompileResult r = compile_session(s);

    // A value that collapses the body (no surviving solid) rolls back fully — the body
    // stays at its last valid size (instant3d.py _result_is_degenerate / _rollback_edit).
    if (r.body_count == 0) {
        s.doc = json::parse(pre_snapshot);
        s.revision += 1;  // the rollback is itself a revision step (Python contract)
        if (commit && !s.undo_stack.empty() && s.undo_stack.back() == pre_snapshot)
            s.undo_stack.pop_back();
        CompileResult rb = compile_session(s);
        json out = {{"ok", true}, {"draggable", false},
                    {"reason", "the new value removes all material (the body would vanish) — "
                               "drag limited to the last valid size"},
                    {"feature_id", plan.owner_fid}, {"feature_type", plan.ftype}};
        merge(out, ops_response(s, rb, json::object(), /*with_tess=*/true));
        return out;
    }

    json out = {{"ok", true}, {"draggable", true}};
    add_plan_fields(out);
    out["committed"] = commit;
    merge(out, ops_response(s, r, json::object(), /*with_tess=*/true));
    return out;
}

json doc_export_file(const std::string& doc_id, const std::string& path, const std::string& fmt,
                     double lin_deflection, double ang_deflection, bool relative, bool ascii) {
    DocLock lk;
    Session& s = require_session(doc_id);
    if (path.empty())
        throw OpError("invalid_op", "export_file: 'path' is required", {{"reason", "missing path"}});
    if (fmt.empty())
        throw OpError("invalid_op", "export_file: 'format' is required", {{"reason", "missing format"}});
    json components;
    std::vector<TopoDS_Shape> shapes = gather_export_shapes(s, &components);
    if (shapes.empty())
        throw OpError("export_failed", "export_file: document has no bodies to export",
                      {{"doc_id", doc_id}});
    TessOptions tess;
    if (lin_deflection > 0.0) tess.lin_deflection = lin_deflection;
    if (ang_deflection > 0.0) tess.ang_deflection = ang_deflection;
    tess.relative = relative;
    tess.ascii = ascii;
    // Count the ACTUAL solids handed to the writer (Issue 3). A single body can be a COMPOUND
    // of disjoint lumps (a boolean result), so the body count understates how many solids a
    // re-read recovers — which made the round-trip check report a misleading "3/1 solids" for
    // a multi-body export and, on the steering body, reject a perfectly good file. Counting
    // solids here matches what the (per-solid) STEP writer emits and what import_bodies reads
    // back. A body with no solid (an open sheet/shell surface) counts as one unit.
    std::size_t expected_solids = 0;
    for (const TopoDS_Shape& b : shapes) {
        if (b.IsNull()) continue;
        std::size_t n = 0;
        for (TopExp_Explorer ex(b, TopAbs_SOLID); ex.More(); ex.Next()) ++n;
        expected_solids += (n > 0 ? n : 1);
    }
    if (expected_solids == 0) expected_solids = shapes.size();
    std::string out = export_bodies(shapes, path, fmt, tess);  // throws on a writer error

    // Real metadata + a flush check: never report success on a missing/empty (truncated) file.
    std::error_code fec;
    const std::uintmax_t bytes = std::filesystem::exists(out, fec)
                                     ? std::filesystem::file_size(out, fec) : 0;
    if (bytes == 0)
        throw OpError("export_failed", "export_file: wrote 0 bytes (file missing or truncated)",
                      {{"path", out}, {"format", fmt}});

    json meta{{"path", out}, {"format", fmt}, {"bytes", static_cast<double>(bytes)},
              {"expected_solids", static_cast<int>(expected_solids)}};
    if (s.is_assembly()) meta["components"] = std::move(components);

    // Round-trip B-rep formats: re-read with the SAME kernel and confirm the solids survived and
    // are topologically valid BEFORE returning success. This catches the "STEP/IGES won't open"
    // class (truncated/garbled writes, dropped solids) loudly instead of a false ok:true. Mesh
    // formats have no solids to recover, so we report bytes only.
    const std::string f = fmt;
    const bool is_brep_fmt = (f == "step" || f == "stp" || f == "iges" || f == "igs" ||
                              f == "brep" || f == "step+" || f == "stepplus");
    if (is_brep_fmt) {
        try {
            auto reread = import_bodies(out, (f == "stp") ? "step" : (f == "igs") ? "iges" : f);
            std::size_t valid = 0;
            for (const auto& nb : reread)
                if (!nb.second.IsNull() && BRepCheck_Analyzer(nb.second).IsValid()) ++valid;
            meta["solid_count"] = static_cast<int>(reread.size());
            meta["valid_solids"] = static_cast<int>(valid);
            const bool count_ok = reread.size() == expected_solids;
            meta["validated"] = (count_ok && valid == reread.size() && !reread.empty());
            if (!meta["validated"].get<bool>())
                throw OpError("export_failed",
                              "export_file: wrote " + out + " but re-reading it recovered " +
                                  std::to_string(reread.size()) + "/" +
                                  std::to_string(expected_solids) + " solids (" +
                                  std::to_string(valid) + " valid) — the file is incomplete or "
                                  "malformed and would not open elsewhere",
                              {{"path", out}, {"format", fmt}});
        } catch (const OpError&) {
            throw;
        } catch (const std::exception& e) {
            throw OpError("export_failed",
                          std::string("export_file: ") + out +
                              " failed round-trip validation (not re-openable): " + e.what(),
                          {{"path", out}, {"format", fmt}});
        }
    } else {
        meta["validated"] = false;  // bytes alone do not prove a solid-preserving round trip
    }
    return meta;
}

json doc_export_estimate(const std::string& doc_id, double lin_deflection, double ang_deflection,
                         bool relative) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json components;
    std::vector<TopoDS_Shape> shapes = gather_export_shapes(s, &components);
    TessOptions tess;
    if (lin_deflection > 0.0) tess.lin_deflection = lin_deflection;
    if (ang_deflection > 0.0) tess.ang_deflection = ang_deflection;
    tess.relative = relative;
    auto [tris, verts] = estimate_tessellation(shapes, tess);
    return json{{"triangles", tris}, {"vertices", verts}, {"bodies", shapes.size()},
                {"components", std::move(components)}};
}

// True iff `path` has the native .step+ container extension (case-insensitive). Used to
// decide whether to read a native document or import a neutral file.
static bool is_stepplus_path(const std::string& path) {
    std::string ext = std::filesystem::path(path).extension().string();
    for (char& c : ext) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return ext == ".step+";
}

// Make `base` unique among the existing feature DISPLAY names in `s` by appending _2, _3, …
// (Issue 10). Importing the same file repeatedly used to add several features all named e.g.
// "HP_Rover_Wheel", which the compiler then flagged with a duplicate-name warning on every
// import. Uniquifying at insert time keeps the tree readable and silences the noise. Caller
// must hold the doc lock (reads s.doc). Empty base is returned as-is.
static std::string unique_feature_name(const Session& s, const std::string& base) {
    if (base.empty()) return base;
    std::set<std::string> used;
    if (s.doc.contains("features") && s.doc["features"].is_array())
        for (const json& f : s.doc["features"])
            if (f.is_object() && f.contains("name") && f["name"].is_string())
                used.insert(f["name"].get<std::string>());
    if (!used.count(base)) return base;
    for (int i = 2;; ++i) {
        std::string cand = base + "_" + std::to_string(i);
        if (!used.count(cand)) return cand;
    }
}

json doc_import_file(const std::string& path, const std::string& into, bool as_solid,
                     const std::string& fmt, const json& body_select) {
    // keep_bodies / exclude_bodies ride through onto the import feature's params, where
    // the compiler's body sub-selection filter (core/doc/document.cpp import branch) reads
    // them. Copied verbatim (arrays of 1-based ordinals or body-name strings) so a later
    // edit_feature on the same params re-filters on the next recompute — the filter lives
    // in the compiler, not here.
    auto apply_body_select = [&body_select](json& params) {
        if (!body_select.is_object()) return;
        for (const char* key : {"keep_bodies", "exclude_bodies"}) {
            auto it = body_select.find(key);
            if (it != body_select.end() && it->is_array()) params[key] = *it;
        }
    };
    // A native .step+ is a DOCUMENT, not a neutral body file: reopen it as a part doc
    // instead of failing on the format sniffer (Bug N-4 — import_part rejected the very
    // extension save_part writes). Route BEFORE taking the lock (doc_open locks g_mtx).
    if (fmt.empty() && is_stepplus_path(path)) {
        if (into.empty())
            return doc_open(path, as_solid);
        // Import a native .step+ INTO an existing doc for assembly (Issue 2). Native is the
        // recommended save format, yet it used to be the one format you couldn't assemble from
        // (the caller had to re-export every part to plain STEP/BREP first). We open the
        // container as a throwaway doc, bake its compiled solids into an embedded-BREP import
        // feature on `into` (the same representation a STEP import uses — survives source
        // deletion), then discard the temp doc. Routed before the lock: doc_open locks g_mtx.
        json opened = doc_open(path, as_solid);
        const std::string tmp_id = opened.value("doc_id", std::string());
        if (tmp_id.empty())
            throw OpError("import_failed", "import_file: could not open .step+ container",
                          {{"path", path}});
        struct CloseImportedDoc {
            std::string id;
            ~CloseImportedDoc() {
                try { doc_close(id); } catch (...) { }
            }
        } close_imported{tmp_id};
        const std::string stem = std::filesystem::path(path).stem().string();
        std::string blob;
        std::size_t n = 0;
        {
            DocLock lk;
            Session& tmp = require_session(tmp_id);
            const auto shapes = gather_export_shapes(tmp);
            TopoDS_Compound comp;
            BRep_Builder bb;
            bb.MakeCompound(comp);
            for (const TopoDS_Shape& shp : shapes)
                if (!shp.IsNull()) { bb.Add(comp, shp); ++n; }
            if (n > 0) blob = shape_to_brep_b64(comp);
        }
        if (n == 0 || blob.empty())
            throw OpError("import_failed", "import_file: .step+ contained no solids to import",
                          {{"path", path}});
        DocLock lk;
        Session& s = require_session(into);
        const std::string fname = unique_feature_name(s, stem);  // Issue 10
        json params = {{"source_path", path}, {"format", "step+"}, {"original_name", stem},
                       {"embedded_brep", blob}, {"embedded_bodies", n}};
        apply_body_select(params);
        json ops = json::array({json{
            {"op", "add_feature"},
            {"feature", {{"type", "import"}, {"name", fname}, {"params", params},
                         {"refs", json::object()}}}}});
        json resp = apply_ops_locked(s, -1, ops, /*coalesce=*/false);
        resp["doc_id"] = s.doc_id;
        return resp;
    }

    DocLock lk;
    if (path.empty())
        throw OpError("invalid_op", "import_file: 'path' is required", {{"reason", "missing path"}});
    // Resolve the format up front so a bad extension errors before we touch a session.
    std::string f = fmt.empty() ? detect_format(path) : fmt;
    const std::string stem = std::filesystem::path(path).stem().string();

    // Prefer SolidWorks COM -> exact STEP when it is available.  Modern SLDPRT/SLDASM files
    // also carry a portable saved-display tessellation, so a missing/broken COM server falls
    // back to that lane instead of making the file unusable on Linux. Parasolid still needs
    // SolidWorks because it has no saved display stream.
    std::string import_path_ = path;
    bool solidworks_tessellation = false;
    if (f == "sldprt" || f == "sldasm") {
        if (solidworks_available()) {
            try {
                import_path_ = sw_export_step(path);
                f = "step";
            } catch (const OpError&) {
                solidworks_tessellation = true;
            }
        } else {
            solidworks_tessellation = true;
        }
    } else if (f == "x_t" || f == "x_b") {
        import_path_ = sw_export_step(path);
        f = "step";
    }

    json params = {{"source_path", import_path_}, {"format", f}, {"original_name", stem}};
    if (solidworks_tessellation) {
        params["import_fidelity"] = "saved_display_tessellation";
        params["editable_as"] = "baked_brep";
    }
    if (as_solid) params["as_solid"] = true;
    apply_body_select(params);
    // BAKE the geometry into the feature at import time so a saved .step+ no longer
    // depends on the source file surviving on disk (Bug N-8). We read the source ONCE
    // here, serialize all imported solids to a lossless BREP blob, and carry it in the
    // feature params. The compiler prefers this embedded geometry; source_path stays as
    // provenance and a rebuild fallback. Fail-soft: if the read throws, we leave the
    // feature path-only (the compiler will then read the source as before).
    try {
        auto bodies = import_bodies(import_path_, f);
        TopoDS_Compound comp;
        BRep_Builder bb;
        bb.MakeCompound(comp);
        std::size_t n = 0;
        json embedded_names = json::array();
        for (auto& [bname, shp] : bodies) {
            if (shp.IsNull()) continue;
            bb.Add(comp, shp);
            ++n;
            embedded_names.push_back(bname);
        }
        if (n > 0) {
            std::string blob = shape_to_brep_b64(comp);
            if (!blob.empty()) {
                params["embedded_brep"] = blob;
                params["embedded_bodies"] = n;
                params["embedded_body_names"] = std::move(embedded_names);
            }
        }
    } catch (...) {
        // leave path-only; compiler falls back to source_path
    }
    Session* s = nullptr;
    if (!into.empty())
        s = &require_session(into);  // append to an existing C++ session
    else
        s = &new_part_session_locked(stem.empty() ? std::string("Imported") : stem);

    // Uniquify the feature name against the target doc (Issue 10) so repeated imports of the
    // same file don't all share one name (and trip the duplicate-name warning every time).
    const std::string fname = unique_feature_name(*s, stem.empty() ? std::string("Imported") : stem);
    json ops = json::array({json{
        {"op", "add_feature"},
        {"feature", {{"type", "import"}, {"name", fname}, {"params", params}, {"refs", json::object()}}}}});

    json resp = apply_ops_locked(*s, -1, ops, /*coalesce=*/false);
    resp["doc_id"] = s->doc_id;
    if (solidworks_tessellation) {
        resp["import_fidelity"] = "saved_display_tessellation";
        resp["feature_resolved"] = false;
        resp["editable_as"] = "baked_brep";
        resp["component_names"] = params.value("embedded_body_names", json::array());
        resp["warning"] =
            "Imported the SolidWorks saved display tessellation (not the original parametric "
            "feature tree/B-rep). Geometry is baked into this native document and can be edited, "
            "exported, and simulated; use SolidWorks feature resolution or STEP when exact "
            "analytic surfaces are required.";
    }
    return resp;
}

// Slug a string the SAME way engine/vendor_parts.py::slug does: trim whitespace, collapse every
// run of chars outside [A-Za-z0-9_.-] to a single '-', strip leading/trailing '-', lowercase;
// empty -> "part". Used to rebuild the vendor index key `slug(supplier)::slug(part_number)`.
static std::string vendor_slug(const std::string& v) {
    size_t b = v.find_first_not_of(" \t\r\n");
    size_t e = v.find_last_not_of(" \t\r\n");
    std::string s = (b == std::string::npos) ? std::string() : v.substr(b, e - b + 1);
    std::string t;
    bool prev_dash = false;
    for (char c : s) {
        const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                        (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '-';
        if (ok) { t += c; prev_dash = false; }
        else if (!prev_dash) { t += '-'; prev_dash = true; }
    }
    size_t lb = t.find_first_not_of('-');
    size_t le = t.find_last_not_of('-');
    t = (lb == std::string::npos) ? std::string() : t.substr(lb, le - lb + 1);
    for (char& c : t) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return t.empty() ? std::string("part") : t;
}

// Insert a registered supplier/vendor CAD part as an `import` feature (port of
// engine/vendor_parts.py::_vendor_part_insert, which is now removed from the Python worker — the
// doc mutation lives here). The vendor REGISTRY stays in Python (register/list/context write
// %APPDATA%/Chudware/vendor_parts/index.json); this reads that SAME index, resolves the cached CAD
// path + metadata, and applies an import op carrying the vendor_* params verbatim. `into` empty ->
// mint a new part doc. Throws OpError on a missing index / unknown part / missing CAD file.
json doc_vendor_part_insert(const std::string& into, const std::string& supplier,
                            const std::string& part_number, const std::string& name) {
    DocLock lk;
    // %APPDATA%/Chudware/vendor_parts/index.json (mirror of engine/paths.py::appdata_root()).
    // Resolve %APPDATA%/Chudware (Windows) or $XDG_DATA_HOME|~/.local/share /Chudware (POSIX) —
    // mirrors agent/paths.cpp::appdata_root() so the core and host agree on the data dir.
    std::filesystem::path chudware_root;
#ifdef _WIN32
    const char* appdata = std::getenv("APPDATA");
    if (appdata && appdata[0]) {
        chudware_root = std::filesystem::path(appdata) / "Chudware";
    } else {
        const char* home = std::getenv("USERPROFILE");
        chudware_root = std::filesystem::path(home && home[0] ? home : ".") / "AppData" / "Roaming" /
                     "Chudware";
    }
#else
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && xdg[0])
        chudware_root = std::filesystem::path(xdg) / "Chudware";
    else if (const char* home = std::getenv("HOME"); home && home[0])
        chudware_root = std::filesystem::path(home) / ".local" / "share" / "Chudware";
    else
        chudware_root = std::filesystem::path(".") / "Chudware";
#endif
    const std::filesystem::path index = chudware_root / "vendor_parts" / "index.json";
    if (!std::filesystem::exists(index))
        throw OpError("invalid_op", "vendor_part_insert: no vendor parts registered",
                      {{"reason", "no vendor index"}, {"index", index.string()}});

    json idx;
    try {
        std::ifstream in(index);
        in >> idx;
    } catch (const std::exception& ex) {
        throw OpError("invalid_op",
                      std::string("vendor_part_insert: cannot read index: ") + ex.what(),
                      {{"reason", "bad index"}});
    }
    const std::string key = vendor_slug(supplier) + "::" + vendor_slug(part_number);
    if (!idx.is_object() || !idx.contains(key))
        throw OpError("invalid_op", "vendor part not registered: " + supplier + " " + part_number,
                      {{"reason", "unknown part"}, {"supplier", supplier},
                       {"part_number", part_number}});
    const json& rec = idx[key];
    std::string cad_path = rec.value("cad_path", std::string());
    if (cad_path.empty() || !std::filesystem::exists(cad_path))
        throw OpError("invalid_op",
                      "registered CAD file is missing for " + supplier + " " + part_number,
                      {{"reason", "missing cad file"}, {"cad_path", cad_path}});

    std::string feat_name = !name.empty()
                                ? name
                                : (rec.value("supplier", std::string()) + " " +
                                   rec.value("part_number", std::string()));
    std::string fmt = rec.value("cad_format", std::string());
    // Prefer COM -> exact STEP, but keep modern SLDPRT/SLDASM on their native format when COM
    // is absent or fails so the portable saved-display tessellation importer can recover them.
    std::string import_path_ = cad_path;
    bool solidworks_tessellation = false;
    if (fmt == "sldprt" || fmt == "sldasm") {
        if (solidworks_available()) {
            try {
                import_path_ = sw_export_step(cad_path);
                fmt = "step";
            } catch (const OpError&) {
                solidworks_tessellation = true;
            }
        } else {
            solidworks_tessellation = true;
        }
    } else if (fmt == "x_t" || fmt == "x_b") {
        import_path_ = sw_export_step(cad_path);
        fmt = "step";
    }
    const std::string original = rec.value("description", std::string()).empty()
                                     ? feat_name
                                     : rec.value("description", std::string());
    json params = {{"source_path", import_path_},
                   {"format", fmt.empty() ? detect_format(cad_path) : fmt},
                   {"original_name", original},
                   {"vendor_supplier", rec.value("supplier", std::string())},
                   {"vendor_part_number", rec.value("part_number", std::string())},
                   {"vendor_product_url", rec.value("product_url", std::string())}};
    if (solidworks_tessellation) {
        params["import_fidelity"] = "saved_display_tessellation";
        params["editable_as"] = "baked_brep";
    }
    json ops = json::array({json{
        {"op", "add_feature"},
        {"feature",
         {{"type", "import"}, {"name", feat_name}, {"params", params}, {"refs", json::object()}}}}});

    Session* s = nullptr;
    if (!into.empty())
        s = &require_session(into);
    else
        s = &new_part_session_locked(feat_name.empty() ? std::string("Vendor Part") : feat_name);

    json resp = apply_ops_locked(*s, -1, ops, /*coalesce=*/false);
    resp["doc_id"] = s->doc_id;
    resp["vendor_part"] = rec;
    return resp;
}

json mesh_read_raw(const std::string& path, const std::string& fmt) {
    if (path.empty())
        throw OpError("invalid_op", "mesh_read_raw: 'path' is required",
                      {{"reason", "missing path"}});
    if (!std::filesystem::exists(path))
        throw OpError("import_failed", "mesh_read_raw: file not found: " + path, {{"path", path}});
    const std::string f = fmt.empty() ? detect_format(path) : fmt;
    json objects = json::array();
    for (const auto& [name, m] : read_mesh_raw(path, f)) {
        json o;
        o["name"] = name;
        o["verts"] = m.verts;  // 3*N doubles (float32 source widened — exact)
        o["tris"] = m.tris;    // 3*M uint32 (0-based)
        objects.push_back(std::move(o));
    }
    return json{{"objects", std::move(objects)}, {"format", f}};
}

json doc_formats() {
    // Stateless — no session needed (the format matrix is a static registry). Byte-parity
    // with engine/doc/formats.py::list_formats (test_formats_registry pins the oracle dump).
    json formats = json::array();
    for (const FormatInfo& fi : format_registry()) {
        formats.push_back(json{{"fmt", fi.fmt},
                               {"label", fi.label},
                               {"exts", fi.exts},
                               {"lane", fi.lane},
                               {"can_import", fi.can_import},
                               {"can_export", fi.can_export}});
    }
    return json{{"formats", std::move(formats)}};
}

// Bake any import feature that still carries only a source_path reference into a
// self-contained `embedded_brep` blob, so the saved .step+ survives the source file
// being deleted (Bug N-8 insurance — covers parts imported before embedding existed, or
// where the import-time embed fail-softed). Reads the SOURCE file; if it is gone we leave
// the feature as-is (nothing we can do without geometry). Returns the number baked.
static int bake_import_features(json& doc) {
    if (!doc.contains("features") || !doc["features"].is_array()) return 0;
    int baked = 0;
    for (json& feat : doc["features"]) {
        if (!feat.is_object() || feat.value("type", std::string()) != "import") continue;
        // Params may be nested (canonical {params}) or flat — write/read either shape.
        json* dst = (feat.contains("params") && feat["params"].is_object()) ? &feat["params"]
                                                                            : &feat;
        auto has_embed = [&](const json& o) {
            auto it = o.find("embedded_brep");
            return it != o.end() && it->is_string() && !it->get<std::string>().empty();
        };
        if (has_embed(feat) || has_embed(*dst)) continue;  // already self-contained
        auto get_str = [&](const char* k) -> std::string {
            if (dst->contains(k) && (*dst)[k].is_string()) return (*dst)[k].get<std::string>();
            if (feat.contains(k) && feat[k].is_string()) return feat[k].get<std::string>();
            return std::string();
        };
        const std::string src = get_str("source_path");
        if (src.empty() || !std::filesystem::exists(src)) continue;
        std::string ifmt = get_str("format");
        try {
            if (ifmt.empty()) ifmt = detect_format(src);
            auto bodies = import_bodies(src, ifmt);
            TopoDS_Compound comp;
            BRep_Builder bb;
            bb.MakeCompound(comp);
            std::size_t n = 0;
            for (auto& [bname, shp] : bodies)
                if (!shp.IsNull()) { bb.Add(comp, shp); ++n; }
            if (n == 0) continue;
            std::string blob = shape_to_brep_b64(comp);
            if (blob.empty()) continue;
            (*dst)["embedded_brep"] = blob;
            (*dst)["embedded_bodies"] = n;
            ++baked;
        } catch (...) {
            // unreadable source — leave path-only
        }
    }
    return baked;
}

json doc_save(const std::string& doc_id, const std::string& path) {
    // W12+: embed every open drawing whose live source is this doc as a drawings/<id>.json
    // entry. Gather them BEFORE taking the session lock — drawings_for_source takes the
    // (separate) drawing lock, and a drawing projection takes drawing->session, so doing it
    // under the session lock would invert the lock order. drawings_for_source touches no
    // session state, so it is safe to call lock-free here.
    std::map<std::string, std::string> drawing_entries = drawings_for_source(doc_id);

    DocLock lk;
    Session& s = require_session(doc_id);
    const std::string target = !path.empty() ? path : s.path;
    if (target.empty())
        throw OpError("io_error",
                      "doc_save: no path given and document has no current path",
                      {{"reason", "missing path"}});
    // Re-write the live drawing set: drop any stale drawings/*.json carried over from a
    // previous open, then add the current ones (mirrors stepplus.py::save).
    for (auto it = s.extra_entries.begin(); it != s.extra_entries.end();) {
        if (it->first.rfind("drawings/", 0) == 0) it = s.extra_entries.erase(it);
        else ++it;
    }
    for (auto& [name, data] : drawing_entries) s.extra_entries[name] = data;

    // Make every imported body self-contained before writing (Bug N-8).
    bake_import_features(s.doc);

    // Persisted geometry cache (fast cold-open): embed the compiled tessellation + ops-response
    // as additive cache.* entries so a later doc_open can skip the full compile_document rebuild.
    // DISPLAY-ONLY + SAFE: document.json (the parametric source of truth) is untouched; open uses
    // the cache ONLY when the doc hash + engine key still match, and the first edit recompiles
    // from features. Always erase first so a failed/again-uncached save never ships a stale cache.
    //
    // geometry.step (contracts/stepplus.md §1) rides the SAME gate and the same erase-first
    // discipline: it is the AP214 snapshot of the last successful rebuild, so it is written only
    // after a fully-ok rebuild of the tree we are about to write as document.json. Carrying an
    // older snapshot forward instead would hand every foreign STEP reader geometry that
    // disagrees with the document beside it — the one thing the member exists to prevent.
    s.extra_entries.erase("cache.json");
    s.extra_entries.erase("cache.aicm");
    s.extra_entries.erase("cache.brep");
    s.extra_entries.erase("geometry.step");
    try {
        if (s.doc.is_object() && s.doc.value("schema", std::string()) == "aicad.part/1") {
            const CompileResult& cr = ensure_compiled(s);
            bool all_ok = !cr.aicm.empty();
            for (const FeatureStatus& st : cr.statuses)
                if (!st.ok) { all_ok = false; break; }
            if (all_ok) {
                json cached = ops_response(s, cr, json::object(), /*with_tess=*/true);
                if (cached.contains("tess") && cached["tess"].is_object())
                    cached["tess"].erase("aicm_path");  // machine/temp-specific; rebuilt on open
                // `solved: true` is the crash-safety gate: it is ONLY ever written here, after
                // a fully-ok rebuild, and stepplus_save is atomic (tmp+rename) — a crash mid-
                // rebuild can never persist a half-built cache. Readers treat absence as false.
                json header = {{"engine", stepplus_cache_engine_key()},
                               {"doc_hash", stepplus_doc_hash(s.doc)},
                               {"solved", true},
                               // cache.aicm is a DISPLAY mesh, so the tier it was meshed at
                               // is part of its validity (contracts/doc_ops.md §7).
                               {"display_quality",
                                ::geom::display_quality_name(s.display_quality)},
                               {"response", cached}};
                s.extra_entries["cache.json"] = header.dump();
                s.extra_entries["cache.aicm"] =
                    std::string(reinterpret_cast<const char*>(cr.aicm.data()), cr.aicm.size());
                // Solved-bodies BREP (with triangulation): lets an assembly load this part as
                // a component without re-running compile_document or BRepMesh.
                s.extra_entries["cache.brep"] = brep_cache_encode(cr.shapes);
                // geometry.step: the AP214 snapshot foreign CAD opens (contracts/stepplus.md
                // §1). The bodies are the ones this fully-ok rebuild just produced.
                std::string geometry_step = stepplus_geometry_step_bytes(cr.shapes);
                if (!geometry_step.empty())
                    s.extra_entries["geometry.step"] = std::move(geometry_step);
            }
        } else if (s.doc.is_object() && s.doc.value("schema", std::string()) == "aicad.asm/1") {
            // A cache-hit open leaves asm_result empty; rebuild here so a plain re-save
            // doesn't strip the cache (components are hot in the load cache, so it's cheap).
            if (!s.asm_result) rebuild_assembly(s);
            // Assembly persisted-geometry cache: the solved, tessellated scene + the component
            // fingerprints it was built from. doc_open replays it only when the doc hash,
            // engine key AND every component file (path+size+mtime) are unchanged — display-
            // only, asm_result stays empty until the first interactive op lazily rebuilds.
            const AssemblyResult& ar = *s.asm_result;
            bool all_ok = !ar.bodies.empty() && ar.converged;
            for (const AsmComponentStatus& cs : ar.component_status)
                if (!cs.ok) { all_ok = false; break; }
            if (all_ok) {
                // geometry.step for an assembly is the FLATTENED placed set: each component
                // body in world coordinates, exactly as gather_export_shapes feeds every
                // other writer (an assembly doc has no features of its own).
                std::string geometry_step =
                    stepplus_geometry_step_bytes(gather_export_shapes(s));
                if (!geometry_step.empty())
                    s.extra_entries["geometry.step"] = std::move(geometry_step);
                std::vector<uint8_t> blob;
                json cached = ops_response_assembly(s, json::object(), &blob);
                if (cached.contains("tess") && cached["tess"].is_object())
                    cached["tess"].erase("aicm_path");
                const std::string base_dir =
                    std::filesystem::path(target).parent_path().string();
                json header = {{"engine", stepplus_cache_engine_key()},
                               {"doc_hash", stepplus_doc_hash(s.doc)},
                               {"solved", true},
                               {"display_quality",
                                ::geom::display_quality_name(s.display_quality)},
                               {"components", asm_component_fingerprints(s.doc, base_dir)},
                               {"response", cached}};
                if (!blob.empty()) {
                    s.extra_entries["cache.json"] = header.dump();
                    s.extra_entries["cache.aicm"] =
                        std::string(reinterpret_cast<const char*>(blob.data()), blob.size());
                }
            }
        }
    } catch (...) {
        s.extra_entries.erase("cache.json");  // never let cache generation break a save
        s.extra_entries.erase("cache.aicm");
        s.extra_entries.erase("cache.brep");
        s.extra_entries.erase("geometry.step");
    }

    std::string out = stepplus_save(s.doc, target, s.extra_entries);
    s.path = out;
    s.dirty = false;
    return json{{"path", out}};
}

json doc_open(const std::string& path, bool as_solid, bool detach_path) {
    // Manual File>Open / File>Import of a NON-native file (.step/.stp/.iges/.brep/.stl/
    // .obj/.ply/.gltf/.glb/.3mf/.3dm/.sldprt/…) is NOT a .step+ container — importing it
    // as a fresh part doc is what the user expects (and is why "open a non-.step+ part"
    // previously did nothing: the zip reader rejected it and nothing fell back). Route it
    // through the import lane, which mints a new part doc with the file's geometry baked in.
    if (!path.empty() && !is_stepplus_path(path))
        return doc_import_file(path, "", as_solid, "");

    std::string opened_doc_id;
    std::map<std::string, std::string> drawing_entries;  // restored after the session lock
    json resp;
    {
    DocLock lk;
    StepPlusDoc loaded = stepplus_open(path);  // throws OpError on a bad file

    // PART and ASSEMBLY containers both open in the C++ core (parts compile via
    // compile_session, assemblies via rebuild_assembly below — see the is_assembly branch).
    // Any OTHER schema (ecad/drawing/cam) has no doc_open session path, so it is rejected
    // BEFORE a session is created — code "unsupported".
    const std::string schema =
        loaded.doc.is_object() ? loaded.doc.value("schema", std::string()) : std::string();
    if (!schema.empty() && schema != "aicad.part/1" && schema != "aicad.asm/1")
        throw OpError("unsupported",
                      "doc_open: the C++ core handles only part/assembly documents (got schema '" +
                          schema + "')",
                      {{"schema", schema}, {"reason", "unsupported document kind"}});

    auto sp = std::make_shared<Session>();
    sp->doc_id = new_doc_id();
    sp->doc = std::move(loaded.doc);
    sp->revision = 0;
    sp->path = detach_path ? std::string() : path;
    // Detached imports must retain relative component references without retaining
    // the source's save destination. Resolve against the container, not process cwd.
    if (detach_path && sp->is_assembly() && sp->doc.contains("components")) {
        const auto base = std::filesystem::absolute(path).parent_path();
        for (json& component : sp->doc["components"]) {
            if (!component.contains("source") || !component["source"].is_object()) continue;
            json& source = component["source"];
            if (!source.contains("path") || !source["path"].is_string()) continue;
            const std::filesystem::path ref(source["path"].get<std::string>());
            if (!ref.empty() && ref.is_relative())
                source["path"] = (base / ref).lexically_normal().string();
        }
    }
    sp->extra_entries = std::move(loaded.extra);
    // Stamp the document's own id to the new session id (mirrors blank_part).
    if (sp->doc.is_object()) sp->doc["id"] = sp->doc_id;
    // Recovery opens re-unique the display name BEFORE the session is registered
    // (unique_doc_name locks every open session's mutex, so it cannot run while we
    // hold this doc's lock). Several retired "Part1" docs recover as Part1/Part2/…
    if (detach_path && sp->doc.is_object()) {
        const std::string want = sp->doc.value("name", std::string());
        if (!want.empty()) sp->doc["name"] = unique_doc_name(want);
    }
    const std::string sid = sp->doc_id;
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        sp->owns_journal = true;
        g_sessions[sid] = sp;
    }
    Session& stored = lk.acquire(sid);

    CompileResult r;
    if (stored.is_assembly()) {
        // Assemblies compile via rebuild_assembly / asm_result — NOT compile_document —
        // exactly as doc_new(kind=assembly) does. base_dir is derived from stored.path
        // inside rebuild_assembly, so the components' saved source paths resolve.
        //
        // Persisted-geometry fast path (assembly flavour of the part cache below): if the
        // container carries a solved cache whose engine key + doc hash match AND every
        // component file is byte-for-byte the one the cache was built from (path+size+mtime
        // fingerprints), replay the saved response + mesh without loading a single
        // component. asm_result stays empty — the first interactive op that needs real
        // geometry lazily rebuilds (every consumer already guards with rebuild_assembly).
        bool asm_cache_hit = false;
        {
            auto cj = stored.extra_entries.find("cache.json");
            auto ca = stored.extra_entries.find("cache.aicm");
            if (cj != stored.extra_entries.end() && ca != stored.extra_entries.end()) {
                try {
                    json header = json::parse(cj->second);
                    const std::string base_dir =
                        std::filesystem::path(path).parent_path().string();
                    if (header.value("engine", std::string()) == stepplus_cache_engine_key() &&
                        header.value("doc_hash", std::string()) ==
                            stepplus_doc_hash(stored.doc) &&
                        header.value("solved", false) &&
                        header.value("display_quality", std::string()) ==
                            ::geom::display_quality_name(stored.display_quality) &&
                        header.contains("response") && header["response"].is_object() &&
                        asm_fingerprints_match(stored.doc, base_dir,
                                               header.value("components", json()))) {
                        resp = header["response"];
                        std::vector<uint8_t> blob(ca->second.begin(), ca->second.end());
                        std::string ap = publish_doc_aicm(stored.doc_id, stored.revision, blob);
                        if (!ap.empty()) {
                            if (!resp.contains("tess") || !resp["tess"].is_object())
                                resp["tess"] = json::object();
                            resp["tess"]["aicm_path"] = ap;
                        }
                        resp["revision"] = stored.revision;
                        resp["tree"] = stored.doc;
                        resp["cache_hit"] = true;
                        asm_cache_hit = true;
                    }
                } catch (...) {
                    asm_cache_hit = false;  // stale/corrupt cache -> full rebuild
                }
            }
        }
        if (!asm_cache_hit) {
            rebuild_assembly(stored);
            resp = ops_response_assembly(stored, json::object());
        }
    } else {
    // Fast cold-open via the persisted geometry cache: if the container carries a valid
    // cache.json/cache.aicm (engine key + doc hash both match the loaded tree), return the
    // cached ops-response + mesh WITHOUT re-running compile_document. stored.compiled is left
    // empty, so the FIRST edit/pick recompiles from features (document.json is authoritative).
    bool cache_hit = false;
    {
        auto cj = stored.extra_entries.find("cache.json");
        auto ca = stored.extra_entries.find("cache.aicm");
        if (cj != stored.extra_entries.end() && ca != stored.extra_entries.end()) {
            try {
                json header = json::parse(cj->second);
                if (header.value("engine", std::string()) == stepplus_cache_engine_key() &&
                    // cache.aicm was meshed at the saving session's display tier; a
                    // different tier here must re-mesh instead of showing a stale mesh.
                    header.value("display_quality", std::string()) ==
                        ::geom::display_quality_name(stored.display_quality) &&
                    header.contains("response") && header["response"].is_object()) {
                    resp = header["response"];
                    // T-PERF10: re-materialize cached mesh via aicm-shm (same handoff as
                    // live rebuilds). Avoids /tmp/chudware_cpp_docs storm on cold open.
                    std::vector<uint8_t> blob(ca->second.begin(), ca->second.end());
                    std::string ap = publish_doc_aicm(stored.doc_id, stored.revision, blob);
                    if (!ap.empty()) {
                        if (!resp.contains("tess") || !resp["tess"].is_object())
                            resp["tess"] = json::object();
                        resp["tess"]["aicm_path"] = ap;
                    }
                    resp["revision"] = stored.revision;  // override the cached (save-time) revision
                    resp["tree"] = stored.doc;
                    resp["cache_hit"] = true;
                    cache_hit = true;
                }
            } catch (...) {
                cache_hit = false;  // any parse/IO hiccup -> fall through to a full rebuild
            }
        }
    }

    if (!cache_hit) {
        r = compile_session(stored);
        resp = warm_and_respond(stored, std::move(r), json::object(), /*with_tess=*/true);
    }
    }  // end part-vs-assembly branch
    resp["doc_id"] = stored.doc_id;
    if (stored.doc.is_object())
        resp["name"] = stored.doc.value("name", std::string());
    opened_doc_id = stored.doc_id;
    // Capture embedded drawings to restore AFTER releasing the session lock (registering
    // a drawing takes the drawing lock; doing it here would invert the lock order).
    for (const auto& [name, data] : stored.extra_entries)
        if (name.rfind("drawings/", 0) == 0) drawing_entries[name] = data;

    // Warnings: container load notes + per-feature rebuild failures (the non-total-
    // failure branch of the Python oracle; frozen-geometry fallback is deferred).
    json warnings = json::array();
    for (const std::string& w : loaded.warnings) warnings.push_back(w);
    std::vector<std::string> failed;
    for (const FeatureStatus& st : r.statuses)
        if (!st.ok) failed.push_back(st.id);
    if (!failed.empty()) {
        std::string msg = std::to_string(failed.size()) + " feature(s) failed to rebuild: [";
        for (std::size_t i = 0; i < failed.size(); ++i) {
            if (i) msg += ", ";
            msg += "'" + failed[i] + "'";
        }
        msg += "]";
        warnings.push_back(msg);
    }
    resp["warnings"] = warnings;
    }  // release the session lock before restoring drawings (lock-order safety)

    // W12+ — restore any embedded drawings as FRESH drawing sessions re-pointed at this
    // reopened part, so they project against the reopened bodies. Fully contained: a
    // malformed / non-drawing entry is skipped, never breaking the part open. The host
    // reads resp["drawings"] to re-create the Drawing tabs (doc_mirror lastOpenedDrawings).
    json drawings = json::array();
    for (const auto& [name, data] : drawing_entries) {
        try {
            json ddoc = json::parse(data);
            drawings.push_back(register_restored_drawing(ddoc, opened_doc_id));
        } catch (const std::exception&) {
            // skip a malformed drawing entry (mirrors stepplus.restore_drawings)
        }
    }
    resp["drawings"] = drawings;
    return resp;
}

// ============================================================ C18 sketch RPCs
// Port of engine/doc/session.py sketch_enter/solve/drag/drag_commit/exit/overview/
// regions/probe/project_ref. doc_id-keyed; operate on the per-session active_sketch.

json doc_sketch_enter(const std::string& doc_id, const std::string& sketch_fid) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    chudware::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
    std::vector<double> frame = plane_frame(plane);

    SolveActive packed = solve_active(s, feat, std::nullopt);
    SketchState st;
    st.sketch_fid = sketch_fid;
    st.solve_state = packed.state;
    st.last_entities = packed.entity_map;
    st.last_statuses = packed.statuses;
    st.last_overall_status = overall_status(packed.statuses, packed.dof, packed.conflicting,
                                            packed.redundant, packed.constraints_by_id);
    st.plane_frame = frame;
    s.active_sketch = std::move(st);

    return sketch_result(feat, packed, frame, s.active_sketch->last_overall_status);
}

json doc_sketch_solve(const std::string& doc_id, const std::string& sketch_fid) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    std::optional<chudware::sketch::SolveState> warm;
    if (s.active_sketch && s.active_sketch->sketch_fid == sketch_fid)
        warm = s.active_sketch->solve_state;
    SolveActive packed = solve_active(s, feat, warm);
    if (!s.active_sketch || s.active_sketch->sketch_fid != sketch_fid) {
        chudware::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
        SketchState st;
        st.sketch_fid = sketch_fid;
        st.plane_frame = plane_frame(plane);
        s.active_sketch = std::move(st);
    }
    s.active_sketch->solve_state = packed.state;
    s.active_sketch->last_entities = packed.entity_map;
    s.active_sketch->last_statuses = packed.statuses;
    s.active_sketch->last_overall_status =
        overall_status(packed.statuses, packed.dof, packed.conflicting, packed.redundant,
                       packed.constraints_by_id);
    return sketch_result(feat, packed, s.active_sketch->plane_frame,
                         s.active_sketch->last_overall_status);
}

json doc_sketch_overview(const std::string& doc_id, const std::string& sketch_fid) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    chudware::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
    SolveActive packed = solve_active(s, feat, std::nullopt);  // never touches active_sketch
    json r;
    r["entities"] = packed.entities;
    r["statuses"] = packed.statuses;
    r["dof"] = packed.dof;
    r["plane_frame"] = plane_frame(plane);
    r["status"] = overall_status(packed.statuses, packed.dof, packed.conflicting,
                                 packed.redundant, packed.constraints_by_id);
    r["skipped_constraints"] = strvec_json(packed.skipped_constraints);
    return r;
}

json doc_sketch_drag(const std::string& doc_id, const std::string& sketch_fid,
                     const std::string& handle, const json& xy, const json& targets) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    // Not entered (or stale / cold) — enter implicitly so drag is still serviceable.
    if (!s.active_sketch || s.active_sketch->sketch_fid != sketch_fid ||
        !s.active_sketch->solve_state) {
        chudware::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
        std::vector<double> frame = plane_frame(plane);
        SolveActive packed = solve_active(s, feat, std::nullopt);
        SketchState st;
        st.sketch_fid = sketch_fid;
        st.solve_state = packed.state;
        st.last_entities = packed.entity_map;
        st.last_statuses = packed.statuses;
        st.last_overall_status = overall_status(packed.statuses, packed.dof, packed.conflicting,
                                                packed.redundant, packed.constraints_by_id);
        st.plane_frame = frame;
        s.active_sketch = std::move(st);
    }
    if (!s.active_sketch->solve_state)
        throw OpError("invalid_op", "sketch could not be solved for drag",
                      {{"reason", "no solve state"}});

    // Drag-path solve cache: reuse ONE built System across the mouse-move frames of a
    // drag. Keyed by the session revision — every committed op batch (and undo/redo)
    // bumps it monotonically, so a stale revision forces a rebuild against the fresh
    // doc. Only the ctx-BUILD frame needs the sketch json + env (the cached System
    // already embeds both); later frames skip that re-extraction entirely.
    SketchState& st = *s.active_sketch;
    if (st.drag_ctx && st.drag_ctx_revision != s.revision) st.drag_ctx.reset();
    json params = sketch_params(feat);
    json env, sk;
    if (!st.drag_ctx) {
        env = env_from_doc(s.doc);
        sk["entities"] = params.contains("entities") ? params["entities"] : json::array();
        sk["constraints"] =
            params.contains("constraints") ? params["constraints"] : json::array();
    }

    std::pair<double, double> target_xy{0.0, 0.0};
    if (xy.is_array() && xy.size() == 2)
        target_xy = {xy[0].get<double>(), xy[1].get<double>()};
    json res = chudware::sketch::drag(*st.solve_state, handle, target_xy, sk, env,
                                   targets.is_null() ? json() : targets, &st.drag_ctx);
    st.drag_ctx_revision = s.revision;

    // Build the entity-coord array from the drag result REGARDLESS of ok/fail. drag() always
    // returns `entities` (live coords on success, frozen coords on a rejected/empty drag), so
    // including them on EVERY return keeps the host overlay alive — a failed drag must never
    // wipe the on-screen sketch (the "sketch disappears while dragging" bug).
    const json res_entities = res.contains("entities") ? res["entities"] : json::object();
    auto construction_of = [&](const std::string& eid) -> bool {
        if (params.contains("entities") && params["entities"].is_array())
            for (const json& e : params["entities"])
                if (e.is_object() && e.value("id", std::string()) == eid)
                    return e.value("construction", false);
        return false;
    };
    json entities = json::array();
    if (res_entities.is_object())
        for (auto it = res_entities.begin(); it != res_entities.end(); ++it) {
            json rec = it.value();
            rec["id"] = it.key();
            rec["construction"] = construction_of(it.key());
            entities.push_back(std::move(rec));
        }

    if (res.is_object() && res.contains("ok") && !res["ok"].get<bool>()) {
        // Failed/empty drag: keep the frozen entities so the host overlay is preserved.
        json err;
        err["ok"] = false;
        err["error"] = res.contains("error") && !res["error"].is_null()
                           ? res["error"] : json("drag target did not resolve");
        err["rejected_handles"] = res.contains("rejected_handles") ? res["rejected_handles"]
                                                                    : json::array();
        err["entities"] = entities;
        err["statuses"] = res.contains("statuses") ? res["statuses"] : json::object();
        err["solve_rejected"] = false;
        err["constraints"] = sketch_constraints(feat, res_entities);
        return err;
    }

    // Remember dragged coords so commit can persist them (transient; no warm churn).
    st.last_entities = res_entities;
    // Statuses are DIAGNOSTICS: the cached drag fast path no longer recomputes them
    // per frame (topology cannot change mid-drag), so keep serving the drag-start
    // snapshot already in st.last_statuses (set by enter/solve/previous commit).
    // Full diagnostics run once in doc_sketch_drag_commit's post-apply re-solve.

    json out;
    out["entities"] = entities;
    out["statuses"] = st.last_statuses;
    out["solve_rejected"] = false;
    out["constraints"] = sketch_constraints(feat, res_entities);
    out["external"] = sketch_external_map(feat);  // keep converted-entity styling alive mid-drag
    return out;
}

json doc_sketch_drag_commit(const std::string& doc_id, const std::string& sketch_fid) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);

    json coords;
    if (s.active_sketch && s.active_sketch->sketch_fid == sketch_fid &&
        s.active_sketch->last_entities.is_object() && !s.active_sketch->last_entities.empty())
        coords = s.active_sketch->last_entities;
    else {
        // Nothing dragged — re-solve and persist solved positions (idempotent commit).
        std::optional<chudware::sketch::SolveState> warm =
            s.active_sketch ? s.active_sketch->solve_state : std::nullopt;
        SolveActive packed = solve_active(s, feat, warm);
        coords = packed.entity_map;
    }

    // Build one sketch_edit_entity op per entity (patch minus "type").
    json ops = json::array();
    for (auto it = coords.begin(); it != coords.end(); ++it) {
        json patch = json::object();
        for (auto kit = it.value().begin(); kit != it.value().end(); ++kit)
            if (kit.key() != "type") patch[kit.key()] = kit.value();
        ops.push_back({{"op", "sketch_edit_entity"}, {"sketch", sketch_fid},
                       {"id", it.key()}, {"params", patch}});
    }

    json resp = apply_ops_locked(s, -1, ops, /*coalesce=*/false);

    // The drag is over: release the cached drag solve context now (the revision bump
    // above already invalidates it — this just frees the built System promptly). The
    // re-solve below is where the FULL diagnostics (analyze + per-entity statuses)
    // run once for the whole drag, so post-release state matches a per-frame solve.
    if (s.active_sketch) s.active_sketch->drag_ctx.reset();

    // Re-solve so warm state matches the freshly persisted doc + refresh constraints.
    if (s.active_sketch && s.active_sketch->sketch_fid == sketch_fid) {
        try {
            json& feat2 = find_sketch_feature(s, sketch_fid);
            std::optional<chudware::sketch::SolveState> warm = s.active_sketch->solve_state;
            SolveActive packed = solve_active(s, feat2, warm);
            s.active_sketch->solve_state = packed.state;
            s.active_sketch->last_entities = packed.entity_map;
            s.active_sketch->last_statuses = packed.statuses;
            s.active_sketch->last_overall_status =
                overall_status(packed.statuses, packed.dof, packed.conflicting,
                               packed.redundant, packed.constraints_by_id);
            // Refresh the doc_status.active_sketch now the status is up to date.
            if (resp.contains("rebuild") && resp["rebuild"].is_object())
                resp["rebuild"]["doc_status"]["active_sketch"] =
                    s.active_sketch->last_overall_status;
            resp["constraints"] = sketch_constraints(feat2, packed.entity_map);
        } catch (...) { /* fall through to the fallback below */ }
    }
    if (!resp.contains("constraints")) {
        json& feat2 = find_sketch_feature(s, sketch_fid);
        json ec = s.active_sketch ? s.active_sketch->last_entities : json::object();
        resp["constraints"] = sketch_constraints(feat2, ec);
    }
    return resp;
}

json doc_sketch_exit(const std::string& doc_id, const std::string& sketch_fid) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);

    if (s.active_sketch && s.active_sketch->sketch_fid == sketch_fid &&
        s.active_sketch->last_entities.is_object() && !s.active_sketch->last_entities.empty()) {
        const json& coords = s.active_sketch->last_entities;
        // Persist last-solved coords IN PLACE; bump revision only if anything changed.
        push_undo(s);
        bool changed = false;
        // Write back into the feature's entities array (canonical params or flat).
        json* entities_ptr = nullptr;
        if (feat.contains("params") && feat["params"].is_object() &&
            feat["params"].contains("entities") && feat["params"]["entities"].is_array())
            entities_ptr = &feat["params"]["entities"];
        else if (feat.contains("entities") && feat["entities"].is_array())
            entities_ptr = &feat["entities"];
        if (entities_ptr)
            for (json& ent : *entities_ptr) {
                if (!ent.is_object() || !ent.contains("id")) continue;
                std::string eid = ent["id"].get<std::string>();
                if (!coords.contains(eid)) continue;
                const json& c = coords[eid];
                for (auto kit = c.begin(); kit != c.end(); ++kit) {
                    if (kit.key() == "type") continue;
                    if (!ent.contains(kit.key()) || ent[kit.key()] != kit.value()) {
                        ent[kit.key()] = kit.value();
                        changed = true;
                    }
                }
            }
        if (changed) {
            s.revision += 1;
            s.dirty = true;
        } else {
            s.undo_stack.pop_back();  // nothing changed — no phantom undo step
        }
    }
    s.active_sketch.reset();
    CompileResult r = compile_session(s);
    return warm_and_respond(s, std::move(r), json::object(), /*with_tess=*/true);
}

namespace {

// ---- doc_sketch_regions support (port of sketch.py compute_regions descriptors) ----
// Loop detection mirrors geom/profile.cpp; here we also build region DESCRIPTORS
// (centroid/area from the OCCT face surface properties, outer/holes polylines, depth/
// is_material) and return EVERY loop as a region (material + void), sorted by centroid.

struct RegEdge {
    TopoDS_Edge edge;
    Eigen::Vector2d p_start, p_end;
    bool closed = false;
    // densely sampled 2D polyline of the edge (for outer/holes overlay), start..end.
    std::vector<Eigen::Vector2d> samples;
    // Exact full-circle params (type=="circle"). Nesting uses these for area /
    // containment so concentric washers classify without sample-ring centroid
    // ambiguity (both centroids at the shared centre → ray-cast fails).
    bool is_circle = false;
    Eigen::Vector2d circle_center{0, 0};
    double circle_radius = 0.0;
};

constexpr double kRegEps = 1e-6;

bool reg_near(const Eigen::Vector2d& a, const Eigen::Vector2d& b) {
    return std::abs(a.x() - b.x()) < kRegEps && std::abs(a.y() - b.y()) < kRegEps;
}

gp_Pnt reg_lift(const chudware::geom::SketchPlane& pl, const Eigen::Vector2d& uv) {
    Eigen::Vector3d w = pl.lift(uv);
    return gp_Pnt(w.x(), w.y(), w.z());
}

bool reg_build_edge(const json& ent, const chudware::geom::SketchPlane& pl, RegEdge& out) {
    if (!ent.is_object() || !ent.contains("type")) return false;
    if (ent.value("construction", false)) return false;
    const std::string t = ent["type"];
    try {
        if (t == "line") {
            Eigen::Vector2d a(ent["p1"][0], ent["p1"][1]), b(ent["p2"][0], ent["p2"][1]);
            if (reg_near(a, b)) return false;
            BRepBuilderAPI_MakeEdge mk(reg_lift(pl, a), reg_lift(pl, b));
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge(); out.p_start = a; out.p_end = b; out.closed = false;
            out.samples = {a, b};
            return true;
        }
        if (t == "circle") {
            Eigen::Vector2d c(ent["center"][0], ent["center"][1]);
            double r = ent["radius"];
            if (r <= kRegEps) return false;
            gp_Ax2 ax(reg_lift(pl, c), gp_Dir(pl.normal.x(), pl.normal.y(), pl.normal.z()),
                      gp_Dir(pl.uAxis.x(), pl.uAxis.y(), pl.uAxis.z()));
            BRepBuilderAPI_MakeEdge mk(gp_Circ(ax, r));
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge(); out.p_start = c; out.p_end = c; out.closed = true;
            out.is_circle = true;
            out.circle_center = c;
            out.circle_radius = r;
            int n = 64;
            for (int i = 0; i < n; ++i) {
                double a = 2.0 * M_PI * i / n;
                out.samples.emplace_back(c.x() + r * std::cos(a), c.y() + r * std::sin(a));
            }
            return true;
        }
        if (t == "arc") {
            Eigen::Vector2d c(ent["center"][0], ent["center"][1]);
            double r = ent["radius"];
            double a0 = ent["start"], a1 = ent["end"];
            if (r <= kRegEps) return false;
            double sweep = a1 - a0;
            while (sweep <= 0.0) sweep += 2.0 * M_PI;
            double amid = a0 + 0.5 * sweep;
            Eigen::Vector2d ps(c.x() + r * std::cos(a0), c.y() + r * std::sin(a0));
            Eigen::Vector2d pe(c.x() + r * std::cos(a1), c.y() + r * std::sin(a1));
            Eigen::Vector2d pm(c.x() + r * std::cos(amid), c.y() + r * std::sin(amid));
            GC_MakeArcOfCircle mkarc(reg_lift(pl, ps), reg_lift(pl, pm), reg_lift(pl, pe));
            if (!mkarc.IsDone()) return false;
            BRepBuilderAPI_MakeEdge mk(mkarc.Value());
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge(); out.p_start = ps; out.p_end = pe; out.closed = false;
            int n = 16;
            for (int i = 0; i <= n; ++i) {
                double a = a0 + sweep * i / n;
                out.samples.emplace_back(c.x() + r * std::cos(a), c.y() + r * std::sin(a));
            }
            return true;
        }
        if (t == "spline" || t == "style_spline") {
            if (!ent.contains("points") || !ent["points"].is_array()) return false;
            std::vector<Eigen::Vector2d> pts;
            for (const json& q : ent["points"]) {
                if (!q.is_array() || q.size() < 2) continue;
                Eigen::Vector2d p(q[0].get<double>(), q[1].get<double>());
                if (!pts.empty() && reg_near(pts.back(), p)) continue;
                pts.push_back(p);
            }
            if (pts.size() < 2) return false;
            const bool self_closed = pts.size() >= 4 && reg_near(pts.front(), pts.back());
            if (self_closed) pts.pop_back();
            const int n = static_cast<int>(pts.size());
            Handle(TColgp_HArray1OfPnt) arr = new TColgp_HArray1OfPnt(1, n);
            for (int i = 0; i < n; ++i) arr->SetValue(i + 1, reg_lift(pl, pts[i]));
            GeomAPI_Interpolate interp(
                arr, self_closed ? Standard_True : Standard_False, 1.0e-6);
            interp.Perform();
            if (!interp.IsDone() || interp.Curve().IsNull()) return false;
            BRepBuilderAPI_MakeEdge mk(interp.Curve());
            if (!mk.IsDone()) return false;
            out.edge = mk.Edge();
            out.p_start = pts.front();
            out.p_end = self_closed ? pts.front() : pts.back();
            out.closed = self_closed;
            out.samples = pts;
            return true;
        }
    } catch (...) { return false; }
    return false;
}

struct RegLoop {
    std::vector<TopoDS_Edge> edges;
    std::vector<Eigen::Vector2d> poly;  // ordered loop polyline (2D, for containment + overlay)
    // Exact circle params when this loop is a single full-circle edge (washer nesting).
    bool is_circle = false;
    Eigen::Vector2d circle_center{0, 0};
    double circle_radius = 0.0;
};

double reg_signed_area(const std::vector<Eigen::Vector2d>& p) {
    const size_t n = p.size();
    if (n < 3) return 0.0;
    double a = 0.0;
    for (size_t i = 0; i < n; ++i) {
        const Eigen::Vector2d& p0 = p[i];
        const Eigen::Vector2d& p1 = p[(i + 1) % n];
        a += p0.x() * p1.y() - p1.x() * p0.y();
    }
    return 0.5 * a;
}

Eigen::Vector2d reg_poly_centroid(const std::vector<Eigen::Vector2d>& p) {
    const size_t n = p.size();
    Eigen::Vector2d mean(0, 0);
    for (const auto& q : p) mean += q;
    if (n) mean /= double(n);
    double a = reg_signed_area(p);
    if (n < 3 || std::abs(a) < 1e-12) return mean;
    double cx = 0, cy = 0;
    for (size_t i = 0; i < n; ++i) {
        const Eigen::Vector2d& p0 = p[i];
        const Eigen::Vector2d& p1 = p[(i + 1) % n];
        double cross = p0.x() * p1.y() - p1.x() * p0.y();
        cx += (p0.x() + p1.x()) * cross;
        cy += (p0.y() + p1.y()) * cross;
    }
    return {cx / (6.0 * a), cy / (6.0 * a)};
}

bool reg_point_in_poly(const Eigen::Vector2d& pt, const std::vector<Eigen::Vector2d>& poly) {
    const size_t n = poly.size();
    if (n < 3) return false;
    bool inside = false;
    size_t j = n - 1;
    for (size_t i = 0; i < n; ++i) {
        double xi = poly[i].x(), yi = poly[i].y(), xj = poly[j].x(), yj = poly[j].y();
        if (((yi > pt.y()) != (yj > pt.y())) &&
            (pt.x() < (xj - xi) * (pt.y() - yi) / (yj - yi + 1e-30) + xi))
            inside = !inside;
        j = i;
    }
    return inside;
}

// Loop area for nesting: exact pi r^2 for full circles, shoelace otherwise.
double reg_loop_area(const RegLoop& lp) {
    if (lp.is_circle && lp.circle_radius > kRegEps)
        return M_PI * lp.circle_radius * lp.circle_radius;
    return std::abs(reg_signed_area(lp.poly));
}

// Representative interior point: circle centre, else polygon centroid.
Eigen::Vector2d reg_loop_centroid(const RegLoop& lp) {
    if (lp.is_circle) return lp.circle_center;
    return reg_poly_centroid(lp.poly);
}

// Does loop `outer` strictly contain the representative point of `inner`?
// Circle-in-circle uses radii (inner fully inside outer when |c_i - c_o| + r_i < r_o).
// Mixed/poly cases fall back to point-in-poly — mirrors geom/profile.cpp::loop_contains.
bool reg_loop_contains(const RegLoop& outer, const RegLoop& inner) {
    if (outer.is_circle && outer.circle_radius > kRegEps) {
        if (inner.is_circle && inner.circle_radius > kRegEps) {
            const double dx = inner.circle_center.x() - outer.circle_center.x();
            const double dy = inner.circle_center.y() - outer.circle_center.y();
            const double d = std::sqrt(dx * dx + dy * dy);
            // Strict containment: inner disc fully inside outer (positive wall thickness).
            return d + inner.circle_radius < outer.circle_radius - kRegEps;
        }
        const Eigen::Vector2d c = reg_loop_centroid(inner);
        const double dx = c.x() - outer.circle_center.x();
        const double dy = c.y() - outer.circle_center.y();
        return (dx * dx + dy * dy) < outer.circle_radius * outer.circle_radius;
    }
    return reg_point_in_poly(reg_loop_centroid(inner), outer.poly);
}

std::vector<RegLoop> reg_detect_loops(const std::vector<RegEdge>& edges) {
    std::vector<RegLoop> loops;
    const size_t n = edges.size();
    std::vector<bool> used(n, false);
    for (size_t i = 0; i < n; ++i) {
        if (!edges[i].closed) continue;
        used[i] = true;
        RegLoop lp;
        lp.edges.push_back(edges[i].edge);
        lp.poly = edges[i].samples;
        if (edges[i].is_circle) {
            lp.is_circle = true;
            lp.circle_center = edges[i].circle_center;
            lp.circle_radius = edges[i].circle_radius;
        }
        loops.push_back(std::move(lp));
    }
    for (size_t start = 0; start < n; ++start) {
        if (used[start]) continue;
        struct Step { size_t idx; bool rev; };
        std::vector<Step> chain{{start, false}};
        used[start] = true;
        const Eigen::Vector2d head = edges[start].p_start;
        Eigen::Vector2d tip = edges[start].p_end;
        bool closed = false;
        while (true) {
            long found = -1; bool frev = false;
            for (size_t j = 0; j < n; ++j) {
                if (used[j]) continue;
                if (reg_near(edges[j].p_start, tip)) { found = (long)j; frev = false; break; }
                if (reg_near(edges[j].p_end, tip)) { found = (long)j; frev = true; break; }
            }
            if (found < 0) break;
            chain.push_back({(size_t)found, frev});
            used[found] = true;
            tip = frev ? edges[found].p_start : edges[found].p_end;
            if (reg_near(tip, head)) { closed = true; break; }
        }
        if (!closed) continue;
        RegLoop lp;
        for (const Step& st : chain) {
            TopoDS_Edge e = edges[st.idx].edge;
            if (st.rev) e = TopoDS::Edge(e.Reversed());
            lp.edges.push_back(e);
            // polyline: edge samples in walk direction, excluding the duplicated joint.
            std::vector<Eigen::Vector2d> seg = edges[st.idx].samples;
            if (st.rev) std::reverse(seg.begin(), seg.end());
            for (size_t k = 0; k + 1 < seg.size(); ++k) lp.poly.push_back(seg[k]);
        }
        loops.push_back(std::move(lp));
    }
    return loops;
}

TopoDS_Wire reg_make_wire(const std::vector<TopoDS_Edge>& edges) {
    BRepBuilderAPI_MakeWire mk;
    for (const TopoDS_Edge& e : edges) mk.Add(e);
    if (!mk.IsDone()) return TopoDS_Wire();
    return mk.Wire();
}

// CCW-orient a loop's edges (matching _orient_ccw); reverse if signed area < 0.
std::vector<TopoDS_Edge> reg_orient_ccw(const RegLoop& lp) {
    if (reg_signed_area(lp.poly) >= 0.0) return lp.edges;
    std::vector<TopoDS_Edge> out;
    for (auto it = lp.edges.rbegin(); it != lp.edges.rend(); ++it)
        out.push_back(TopoDS::Edge(it->Reversed()));
    return out;
}

// Build a region face = outer loop minus its direct child (hole) loops.
TopoDS_Face reg_region_face(const RegLoop& outer, const std::vector<const RegLoop*>& holes) {
    TopoDS_Wire ow = reg_make_wire(reg_orient_ccw(outer));
    if (ow.IsNull()) return TopoDS_Face();
    BRepBuilderAPI_MakeFace mf(ow, true);
    for (const RegLoop* h : holes) {
        TopoDS_Wire hw = reg_make_wire(reg_orient_ccw(*h));
        if (hw.IsNull()) continue;
        mf.Add(TopoDS::Wire(hw.Reversed()));
    }
    if (!mf.IsDone()) return TopoDS_Face();
    return mf.Face();
}

// (area_or_len, centroid[u,v]) from a face's surface properties, centroid projected
// into the plane's local frame (sketch.py _region_descriptor).
void reg_face_descriptor(const TopoDS_Face& face, const chudware::geom::SketchPlane& pl,
                         double& area, double& cu, double& cv) {
    area = 0.0; cu = 0.0; cv = 0.0;
    try {
        GProp_GProps props;
        BRepGProp::SurfaceProperties(face, props);
        area = props.Mass();
        gp_Pnt cm = props.CentreOfMass();
        Eigen::Vector3d diff(cm.X() - pl.origin.x(), cm.Y() - pl.origin.y(),
                             cm.Z() - pl.origin.z());
        cu = diff.dot(pl.uAxis);
        cv = diff.dot(pl.vAxis);
    } catch (...) {}
}

// Hole-aware 2D triangulation of a region face in sketch-local (u,v) coords
// (sketch.py region_triangles_2d).
json reg_region_triangles_2d(const TopoDS_Face& face, const chudware::geom::SketchPlane& pl) {
    json out = json::array();
    try {
        BRepMesh_IncrementalMesh(face, 0.25, false, 0.5, true).Perform();
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
        if (tri.IsNull()) return out;
        gp_Trsf trsf = loc.Transformation();
        bool ident = loc.IsIdentity();
        auto uv = [&](int i) -> std::array<double, 2> {
            gp_Pnt nd = tri->Node(i);
            if (!ident) nd.Transform(trsf);
            Eigen::Vector3d d(nd.X() - pl.origin.x(), nd.Y() - pl.origin.y(),
                              nd.Z() - pl.origin.z());
            return {d.dot(pl.uAxis), d.dot(pl.vAxis)};
        };
        std::vector<std::array<double, 2>> nodes;
        nodes.reserve(tri->NbNodes());
        for (int i = 1; i <= tri->NbNodes(); ++i) nodes.push_back(uv(i));
        for (int ti = 1; ti <= tri->NbTriangles(); ++ti) {
            int a, b, c;
            tri->Triangle(ti).Get(a, b, c);
            out.push_back(json::array({json::array({nodes[a - 1][0], nodes[a - 1][1]}),
                                       json::array({nodes[b - 1][0], nodes[b - 1][1]}),
                                       json::array({nodes[c - 1][0], nodes[c - 1][1]})}));
        }
    } catch (...) {}
    return out;
}

json poly_to_json(const std::vector<Eigen::Vector2d>& p) {
    json a = json::array();
    for (const auto& q : p) a.push_back(json::array({q.x(), q.y()}));
    return a;
}

}  // namespace

// Region enumeration on an ALREADY-LOCKED session (no DocLock). The public RPC is a
// thin locking wrapper; internal callers (T-W2 auto-constrain closure delta) that
// already hold the DocLock call this directly to avoid re-entering the non-recursive
// g_mtx (which would deadlock).
json sketch_regions_locked(Session& s, const std::string& sketch_fid) {
    json& feat = find_sketch_feature(s, sketch_fid);
    chudware::geom::SketchPlane plane = resolve_sketch_plane(s, feat);

    // Solve the sketch to get current entity coords (regions are computed on the
    // SOLVED geometry, same as session.py sketch_regions builds edges off entities).
    SolveActive packed = solve_active(s, feat, std::nullopt);
    const json& solved = packed.entity_map;  // id -> coord dict

    // Build region edges from non-construction solved entities.
    std::vector<RegEdge> edges;
    for (auto it = solved.begin(); it != solved.end(); ++it) {
        RegEdge re;
        // re-attach type from the entity record (solve output carries the params).
        json ent = it.value();
        if (!ent.contains("type")) {
            // recover type from the stored params
            json params = sketch_params(feat);
            if (params.contains("entities") && params["entities"].is_array())
                for (const json& e : params["entities"])
                    if (e.is_object() && e.value("id", std::string()) == it.key()) {
                        if (e.contains("type")) ent["type"] = e["type"];
                        if (e.contains("construction")) ent["construction"] = e["construction"];
                        break;
                    }
        }
        if (reg_build_edge(ent, plane, re)) edges.push_back(std::move(re));
    }

    std::vector<RegLoop> loops = reg_detect_loops(edges);
    const size_t n = loops.size();

    std::vector<double> areas(n);
    std::vector<int> depth(n, 0), parent(n, -1);
    for (size_t i = 0; i < n; ++i) areas[i] = reg_loop_area(loops[i]);
    for (size_t i = 0; i < n; ++i) {
        double best_area = 0.0; int best = -1;
        for (size_t j = 0; j < n; ++j) {
            if (i == j || areas[j] <= areas[i]) continue;
            if (reg_loop_contains(loops[j], loops[i])) {
                depth[i]++;
                if (best < 0 || areas[j] < best_area) { best_area = areas[j]; best = (int)j; }
            }
        }
        parent[i] = best;
    }
    std::vector<std::vector<int>> children(n);
    for (size_t i = 0; i < n; ++i)
        if (parent[i] >= 0) children[parent[i]].push_back((int)i);

    // Region descriptors (every loop, material + void). Paired with their face.
    struct Reg { json desc; };
    std::vector<Reg> regs;
    for (size_t i = 0; i < n; ++i) {
        std::vector<const RegLoop*> holes;
        for (int c : children[i]) holes.push_back(&loops[c]);
        TopoDS_Face face = reg_region_face(loops[i], holes);
        if (face.IsNull()) continue;
        double area, cu, cv;
        reg_face_descriptor(face, plane, area, cu, cv);
        json desc;
        desc["centroid"] = json::array({cu, cv});
        desc["area"] = area;
        desc["depth"] = depth[i];
        desc["is_material"] = (depth[i] % 2 == 0);
        desc["outer"] = poly_to_json(loops[i].poly);
        json holes_j = json::array();
        for (int c : children[i]) holes_j.push_back(poly_to_json(loops[c].poly));
        desc["holes"] = holes_j;
        desc["tris"] = reg_region_triangles_2d(face, plane);
        regs.push_back({std::move(desc)});
    }

    // Sort by centroid (u, then v) for stable indices; stamp index.
    std::sort(regs.begin(), regs.end(), [](const Reg& a, const Reg& b) {
        double au = a.desc["centroid"][0], bu = b.desc["centroid"][0];
        if (au != bu) return au < bu;
        return double(a.desc["centroid"][1]) < double(b.desc["centroid"][1]);
    });
    json regions = json::array();
    for (size_t i = 0; i < regs.size(); ++i) {
        regs[i].desc["index"] = (int)i;
        regions.push_back(regs[i].desc);
    }

    // bbox_diag: span of all outer coords * sqrt(2), floored (session.py sketch_regions).
    std::vector<double> coords;
    for (const json& r : regions)
        for (const json& pt : r["outer"]) { coords.push_back(pt[0]); coords.push_back(pt[1]); }
    double span = 100.0;
    if (!coords.empty()) {
        double mn = coords[0], mx = coords[0];
        for (double c : coords) { mn = std::min(mn, c); mx = std::max(mx, c); }
        span = mx - mn;
    }
    double diag = std::max(span * std::sqrt(2.0), 1e-3);

    json out;
    out["plane_frame"] = plane_frame(plane);
    out["bbox_diag"] = diag;
    out["regions"] = regions;
    return out;
}

json doc_sketch_regions(const std::string& doc_id, const std::string& sketch_fid) {
    DocLock lk;
    Session& s = require_session(doc_id);
    return sketch_regions_locked(s, sketch_fid);
}

json doc_sketch_probe(const std::string& doc_id, const std::string& sketch_fid,
                      const json& action) {
    DocLock lk;
    Session& s = require_session(doc_id);
    // The probe operates on the ACTIVE sketch (same context sketch_solve uses).
    json* featp = nullptr;
    try {
        featp = &find_sketch_feature(s, sketch_fid);
    } catch (const OpError&) {
        return json{{"ok", false}, {"error", "no sketch feature '" + sketch_fid + "'"}};
    }
    json& feat = *featp;
    if (!s.active_sketch || s.active_sketch->sketch_fid != sketch_fid)
        return json{{"ok", false},
                    {"error", "sketch '" + sketch_fid + "' is not the active sketch"}};
    if (!action.is_object() || action.size() != 1)
        return json{{"ok", false}, {"error", "action must be exactly one of "
                    "add_constraint / set_dim / delete_constraint"}};

    json env = env_from_doc(s.doc);
    json base_params = sketch_params(feat);
    json base_sk;
    base_sk["entities"] = base_params.contains("entities") ? base_params["entities"] : json::array();
    base_sk["constraints"] =
        base_params.contains("constraints") ? base_params["constraints"] : json::array();

    // COLD baseline solve of the unmodified copy for dof_before.
    chudware::sketch::SketchSolution before = chudware::sketch::solve_sketch(base_sk, env, std::nullopt);
    int dof_before = before.dof;

    // Deep-copy and mutate the COPY only.
    json params = base_sk;
    if (!params.contains("entities") || !params["entities"].is_array())
        params["entities"] = json::array();
    if (!params.contains("constraints") || !params["constraints"].is_array())
        params["constraints"] = json::array();
    json& cons = params["constraints"];

    static const std::string kProbeCid = "__probe__";
    bool probe_is_dim = false;

    if (action.contains("add_constraint")) {
        const json& con = action["add_constraint"];
        if (!con.is_object())
            return json{{"ok", false}, {"error", "add_constraint requires a constraint object"}};
        json c = con;
        c["id"] = kProbeCid;
        probe_is_dim = is_dim_type(c.value("type", std::string()));
        cons.push_back(std::move(c));
    } else if (action.contains("set_dim")) {
        const json& sd = action["set_dim"];
        if (!sd.is_object() || !sd.contains("id"))
            return json{{"ok", false}, {"error", "set_dim requires an 'id'"}};
        std::string cid = sd["id"].get<std::string>();
        json* target = nullptr;
        for (json& c : cons)
            if (c.is_object() && c.value("id", std::string()) == cid) { target = &c; break; }
        if (!target)
            return json{{"ok", false}, {"error", "set_dim: constraint '" + cid + "' not found"}};
        bool touched = false;
        if (sd.contains("expr") && !sd["expr"].is_null()) {
            double cur = (target->contains("value") && (*target)["value"].is_object() &&
                          (*target)["value"].contains("value"))
                             ? (*target)["value"]["value"].get<double>() : 0.0;
            (*target)["value"] = {{"value", cur}, {"expr", sd["expr"]}};
            touched = true;
        } else if (sd.contains("value") && !sd["value"].is_null()) {
            (*target)["value"] = {{"value", sd["value"].get<double>()}, {"expr", nullptr}};
            touched = true;
        }
        if (sd.contains("placement")) {
            if (sd["placement"].is_null()) {
                target->erase("placement");
            } else if (sd["placement"].is_array() && sd["placement"].size() == 2) {
                (*target)["placement"] = {sd["placement"][0].get<double>(),
                                          sd["placement"][1].get<double>()};
            } else {
                return json{{"ok", false}, {"error", "set_dim.placement must be [u, v] or null"}};
            }
            touched = true;
        }
        if (sd.contains("driven") && !sd["driven"].is_null()) {
            (*target)["driven"] = sd["driven"].get<bool>();
            touched = true;
        }
        if (!touched)
            return json{{"ok", false},
                        {"error", "set_dim requires 'value', 'expr', 'placement', or 'driven'"}};
    } else if (action.contains("delete_constraint")) {
        const json& dc = action["delete_constraint"];
        if (!dc.is_object() || !dc.contains("id"))
            return json{{"ok", false}, {"error", "delete_constraint requires an 'id'"}};
        std::string cid = dc["id"].get<std::string>();
        bool found = false;
        json kept = json::array();
        for (const json& c : cons)
            if (c.is_object() && c.value("id", std::string()) == cid) found = true;
            else kept.push_back(c);
        if (!found)
            return json{{"ok", false}, {"error", "delete_constraint: '" + cid + "' not found"}};
        params["constraints"] = kept;
    } else {
        return json{{"ok", false}, {"error", "unknown action; expected add_constraint / "
                    "set_dim / delete_constraint"}};
    }

    chudware::sketch::SketchSolution after = chudware::sketch::solve_sketch(params, env, std::nullopt);
    int dof_after = after.dof;
    const std::vector<std::string>& conflicting = after.conflicting;
    const std::vector<std::string>& redundant = after.redundant;

    json cons_by_id = json::object();
    for (const json& c : params["constraints"])
        if (c.is_object() && c.contains("id"))
            cons_by_id[c["id"].get<std::string>()] = c;

    auto in = [](const std::vector<std::string>& v, const std::string& x) {
        for (const std::string& s : v) if (s == x) return true;
        return false;
    };
    bool would = in(conflicting, kProbeCid) ||
                 (probe_is_dim && in(redundant, kProbeCid)) ||
                 (probe_is_dim && dof_after >= dof_before) ||
                 !after.converged || !conflicting.empty();

    json statuses = json::object();
    for (const auto& kv : after.statuses) statuses[kv.first] = kv.second;

    json out;
    out["ok"] = true;
    out["status"] = overall_status(statuses, dof_after, conflicting, redundant, cons_by_id);
    out["dof_before"] = dof_before;
    out["dof_after"] = dof_after;
    out["converged"] = after.converged;
    out["conflicting"] = strvec_json(conflicting);
    out["redundant"] = strvec_json(redundant);
    out["would_over_define"] = would;
    return out;
}

// ------------------------------------------------------- T-W2 sketch completion
namespace {

// Is `sketch.auto_coincident` enabled for this doc? Stored in doc["meta"] (the
// doc_meta_set/get key-value store); DEFAULT ON per the Hunter directive — absent or
// non-bool reads as true. Only an explicit `false` disables the on-edit auto pass.
bool sketch_auto_coincident_enabled(const json& doc) {
    if (doc.contains("meta") && doc["meta"].is_object() &&
        doc["meta"].contains("sketch.auto_coincident")) {
        const json& v = doc["meta"]["sketch.auto_coincident"];
        if (v.is_boolean()) return v.get<bool>();
    }
    return true;
}

// Entity id of a handle ("L4.p2" -> "L4"; bare "P3" -> "P3").
std::string handle_eid(const std::string& h) { return h.substr(0, h.find('.')); }

// All handle pairs the sketch's constraints ALREADY coincident-link, as an
// order-normalized "min\x1fmax" hash set. A coincident constraint pins point handles;
// a pair is linked when ONE `coincident` constraint references BOTH handles
// (exact-handle match, the granularity the auto pass proposes at). Built ONCE per
// sketch so the O(E²) proximity loop in coincident_proposals queries in O(1) instead
// of rescanning the constraint list for every candidate pair.
std::unordered_set<std::string> coincident_pair_set(const json& constraints) {
    std::unordered_set<std::string> linked;
    if (!constraints.is_array()) return linked;
    for (const json& c : constraints) {
        if (!c.is_object() || c.value("type", std::string()) != "coincident") continue;
        if (!c.contains("refs") || !c["refs"].is_array()) continue;
        std::vector<std::string> hs;
        for (const json& r : c["refs"])
            if (r.is_string()) hs.push_back(r.get<std::string>());
        for (size_t a = 0; a < hs.size(); ++a)
            for (size_t b = a + 1; b < hs.size(); ++b)
                linked.insert(hs[a] < hs[b] ? hs[a] + '\x1f' + hs[b]
                                            : hs[b] + '\x1f' + hs[a]);
    }
    return linked;
}

// Proximity scan: pairs of coincidence-eligible endpoints within tol_mm that are NOT
// already coincident-constrained. Read-only over the SOLVED coords. Each endpoint joins
// at most one NEW proposal (a greedy union so a shared corner emits one edge, not a
// clique). Endpoints on the SAME entity are never paired (self-coincidence is noise).
//
// T-PERF10: when `touched_eids` is non-empty, only pairs where at least ONE endpoint
// belongs to a touched entity are considered — O(E·T) instead of O(E²) on edit of a
// single entity in a large sketch. Empty set = full scan (explicit auto-constrain RPC).
// Returns [{a:{handle}, b:{handle}, gap_mm}], nearest-first.
json coincident_proposals(const json& solved_coords, const json& constraints,
                          double tol_mm,
                          const std::unordered_set<std::string>& touched_eids = {}) {
    std::vector<chudware::sketch::Endpoint> eps =
        chudware::sketch::entity_endpoints(solved_coords);
    const std::unordered_set<std::string> linked = coincident_pair_set(constraints);
    const bool scope = !touched_eids.empty();
    struct Cand { int i, j; double gap; };
    std::vector<Cand> cands;
    for (size_t i = 0; i < eps.size(); ++i)
        for (size_t j = i + 1; j < eps.size(); ++j) {
            const std::string ei = handle_eid(eps[i].handle);
            const std::string ej = handle_eid(eps[j].handle);
            if (ei == ej) continue;
            if (scope && !touched_eids.count(ei) && !touched_eids.count(ej)) continue;
            double dx = eps[i].x - eps[j].x, dy = eps[i].y - eps[j].y;
            double gap = std::sqrt(dx * dx + dy * dy);
            if (gap > tol_mm) continue;
            const std::string& ha = eps[i].handle;
            const std::string& hb = eps[j].handle;
            if (linked.count(ha < hb ? ha + '\x1f' + hb : hb + '\x1f' + ha)) continue;
            cands.push_back({(int)i, (int)j, gap});
        }
    std::sort(cands.begin(), cands.end(),
              [](const Cand& a, const Cand& b) { return a.gap < b.gap; });
    std::vector<bool> used(eps.size(), false);
    json out = json::array();
    for (const Cand& c : cands) {
        if (used[c.i] || used[c.j]) continue;  // greedy: one new pin per endpoint
        used[c.i] = used[c.j] = true;
        out.push_back({{"a", {{"handle", eps[c.i].handle}}},
                       {"b", {{"handle", eps[c.j].handle}}},
                       {"gap_mm", c.gap}});
    }
    return out;
}

// Entity ids a sketch-mutating op batch touched (for scoped auto-coincide). Covers
// sketch_add_entity / sketch_edit_entity / sketch_delete_entity entity refs, plus every
// entity in a newly-created sketch feature (add_feature of type sketch).
std::unordered_set<std::string> touched_entity_ids(const json& ops, const json& id_map) {
    std::unordered_set<std::string> out;
    if (!ops.is_array()) return out;
    auto add_eid = [&](json e) {
        resolve_placeholders_deep(e, id_map);
        if (e.is_string()) out.insert(e.get<std::string>());
    };
    for (std::size_t i = 0; i < ops.size(); ++i) {
        const json& op = ops[i];
        if (!op.is_object()) continue;
        const std::string k = op.value("op", std::string());
        if (k == "add_feature" && op.contains("feature") && op["feature"].is_object() &&
            op["feature"].value("type", std::string()) == "sketch") {
            // New sketch: every entity is "touched".
            const json* ents = nullptr;
            if (op["feature"].contains("params") && op["feature"]["params"].is_object() &&
                op["feature"]["params"].contains("entities"))
                ents = &op["feature"]["params"]["entities"];
            else if (op["feature"].contains("entities"))
                ents = &op["feature"]["entities"];
            if (ents && ents->is_array())
                for (const json& e : *ents)
                    if (e.is_object() && e.contains("id") && e["id"].is_string())
                        out.insert(e["id"].get<std::string>());
            continue;
        }
        if (k == "sketch_add_entity") {
            if (op.contains("entity") && op["entity"].is_object() &&
                op["entity"].contains("id") && op["entity"]["id"].is_string())
                out.insert(op["entity"]["id"].get<std::string>());
            // Placeholder $N may also map the minted id.
            const std::string ph = "$" + std::to_string(i);
            if (id_map.contains(ph) && id_map[ph].is_string())
                out.insert(id_map[ph].get<std::string>());
            if (op.contains("id")) add_eid(op["id"]);
            continue;
        }
        if (k == "sketch_edit_entity" || k == "sketch_delete_entity") {
            if (op.contains("entity")) add_eid(op["entity"]);
            if (op.contains("id")) add_eid(op["id"]);
            continue;
        }
    }
    return out;
}

// Count closed material regions of a solved sketch WITHOUT the OCCT region machinery
// (health/auto-constrain report closure via loop chains, cheaper than doc_sketch_regions
// and available in the non-OCCT build). A "closed chain" = every open-curve endpoint
// (line/arc/slot/spline p1/p2/start/end) shares its position (within eps) with exactly
// one other endpoint; we report the count of endpoints still dangling (unmatched).
// Returns {closed_chains_ok, open_endpoints:[handles]} — the narration source.
json closure_summary(const json& solved_coords) {
    std::vector<chudware::sketch::Endpoint> eps =
        chudware::sketch::entity_endpoints(solved_coords);
    // circle/point centers are not chain endpoints; keep only p1/p2/start/end handles.
    std::vector<chudware::sketch::Endpoint> chain;
    for (const auto& e : eps) {
        auto dot = e.handle.find('.');
        if (dot == std::string::npos) continue;  // bare point / circle center handled below
        std::string sub = e.handle.substr(dot + 1);
        if (sub == "p1" || sub == "p2" || sub == "start" || sub == "end")
            chain.push_back(e);
    }
    const double eps_mm = 1e-4;
    std::vector<bool> matched(chain.size(), false);
    for (size_t i = 0; i < chain.size(); ++i)
        for (size_t j = i + 1; j < chain.size(); ++j) {
            if (matched[i] && matched[j]) continue;
            double dx = chain[i].x - chain[j].x, dy = chain[i].y - chain[j].y;
            if (std::sqrt(dx * dx + dy * dy) <= eps_mm) { matched[i] = matched[j] = true; }
        }
    json open_eps = json::array();
    for (size_t i = 0; i < chain.size(); ++i)
        if (!matched[i]) open_eps.push_back(chain[i].handle);
    json out;
    out["open_endpoints"] = open_eps;
    out["closed_chains_ok"] = open_eps.empty();
    return out;
}

// The number of CONNECTED MATERIAL REGIONS of the feature's solved sketch — the
// number the agent-facing readback narrates as "CLOSED (N material regions)" and the
// number a profile `hint_index` can address (document.cpp::resolve_profile_faces).
// Caller MUST already hold the DocLock (no re-lock). Returns -1 if regions can't be
// computed (e.g. non-OCCT build); callers fall back to closure_summary.
//
// This used to count doc_sketch_regions' per-LOOP material regions, which is a
// DIFFERENT number: doc_sketch_regions deliberately emits one region per closed loop
// (the GUI region picker needs that granularity, e.g. a washer's void inner disc),
// while extrusion fuses overlapping/touching loops into one solid. Live run
// run-42508173312214 ("lexus_f_keychain"): three OVERLAPPING rectangles narrated as
// "3 material regions", the model asked for regions 1 and 2, and the extruder — which
// had already unioned all three into region 0 — rejected both. The readback was the
// wrong side of that disagreement, so it now counts what extrusion can actually
// address. doc_sketch_regions itself is untouched (GUI index parity).
int material_region_count(Session& s, const std::string& sketch_fid) {
    try {
        json& feat = find_sketch_feature(s, sketch_fid);
        chudware::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
        SolveActive packed = solve_active(s, feat, std::nullopt);
        return static_cast<int>(chudware::geom::connected_material_regions(
            packed.entity_map, plane).size());
    } catch (...) {
        return -1;
    }
}

// Solve `feat` cold and hand back the SketchSolution (for auto-constrain add/analyze).
chudware::sketch::SketchSolution solve_feat_cold(Session& s, const json& feat) {
    json env = env_from_doc(s.doc);
    json params = sketch_params(feat);
    json sk;
    sk["entities"] = params.contains("entities") ? params["entities"] : json::array();
    sk["constraints"] = params.contains("constraints") ? params["constraints"] : json::array();
    return chudware::sketch::solve_sketch(sk, env, std::nullopt);
}

// Add each proposal as a `coincident` constraint into `entities_ref`/`constraints_ref`
// (a mutable params object) with per-proposal add->analyze->rollback-on-CONFLICT safety:
// a proposal that turns the system conflicting is dropped; a proposal that is merely
// REDUNDANT (the loop is already pinned) is KEPT — redundant coincidents are harmless
// and are exactly what closes a touching-corner profile. `gen` mints constraint ids.
// Returns {applied:[cids], skipped_conflicting:int}. Mutates params IN PLACE on success.
// `solve_budget_ms` (optional) caps each trial solve's wall clock — set ONLY by the
// apply_ops edit path (see kInteractiveSolveBudgetMs below); the explicit
// doc_sketch_auto_constrain RPC keeps the unbudgeted default.
json apply_coincident_proposals(json& params, const json& proposals, const json& env,
                                IdGen& gen,
                                std::optional<double> solve_budget_ms = std::nullopt) {
    if (!params.contains("constraints") || !params["constraints"].is_array())
        params["constraints"] = json::array();
    json applied = json::array();
    int skipped = 0;
    for (const json& p : proposals) {
        if (!p.is_object() || !p.contains("a") || !p.contains("b")) continue;
        std::string ha = p["a"].value("handle", std::string());
        std::string hb = p["b"].value("handle", std::string());
        if (ha.empty() || hb.empty()) continue;
        const std::string cid = gen.next("c");
        json c = {{"id", cid}, {"type", "coincident"},
                  {"refs", json::array({ha, hb})}};
        // Trial-add on a COPY, analyze; keep only if it does not become conflicting.
        json trial = params;
        trial["constraints"].push_back(c);
        json sk;
        sk["entities"] = trial["entities"];
        sk["constraints"] = trial["constraints"];
        // Interactive when a wall-clock budget is set (apply_ops auto-coincident);
        // the explicit auto-constrain RPC leaves budget unset so the exact path is used.
        const bool interactive = solve_budget_ms.has_value();
        chudware::sketch::SketchSolution sol =
            chudware::sketch::solve_sketch(sk, env, std::nullopt, solve_budget_ms,
                                        interactive);
        bool conflicts = false;
        for (const std::string& t : sol.conflicting)
            if (t == cid) conflicts = true;
        if (conflicts || !sol.converged) { ++skipped; continue; }
        params["constraints"].push_back(std::move(c));  // commit into live params
        applied.push_back(cid);
    }
    return json{{"applied", applied}, {"skipped_conflicting", skipped}};
}

// Run the auto-coincident pass on every sketch feature `working` touched, IN PLACE,
// before the enclosing apply_ops_locked compiles/persists the batch. Gated on the
// doc's sketch.auto_coincident meta flag (default ON). `sketch_fids` are the sketch
// feature ids sketch-mutating ops in this batch referenced. Silent + best-effort: any
// failure on a feature leaves it untouched (never blocks the user's edit). This is the
// "touching endpoints are already coincident before the model sees a not-closed error"
// path — it adds constraints, so touching-corner profiles close automatically.
// `touched_eids` (T-PERF10) scopes the O(E²) proximity scan to entities this batch
// created/edited; empty = full scan (e.g. brand-new multi-entity sketch).
void maybe_auto_constrain(json& working, const std::vector<std::string>& sketch_fids,
                          IdGen& gen,
                          const std::unordered_set<std::string>& touched_eids) {
    if (!sketch_auto_coincident_enabled(working)) return;
    if (!working.contains("features") || !working["features"].is_array()) return;
    const double tol_mm = 1e-4;
    // Wall-clock budget for the solves this pass runs: it executes INSIDE every
    // sketch-mutating apply_ops batch under the per-doc lock (the drag path has had
    // a 10 ms budget since day one — see sketch.cpp drag). A pathological sketch now
    // degrades to "no auto pins this edit" (best-effort, like every other failure here)
    // instead of stalling this doc's RPC traffic. Parity/oracle-facing solves
    // (check_sketch, doc_sketch_health, the compile pipeline) remain unbudgeted.
    const double kInteractiveSolveBudgetMs = 100.0;
    json env = env_from_doc(working);
    for (const std::string& fid : sketch_fids) {
        for (json& feat : working["features"]) {
            if (!feat.is_object() || feat.value("id", std::string()) != fid) continue;
            if (feat.value("type", std::string()) != "sketch") continue;
            if (!feat.contains("params") || !feat["params"].is_object()) continue;
            try {
                json params = feat["params"];
                json sk;
                sk["entities"] = params.value("entities", json::array());
                sk["constraints"] = params.value("constraints", json::array());
                chudware::sketch::SketchSolution sol =
                    chudware::sketch::solve_sketch(sk, env, std::nullopt,
                                                kInteractiveSolveBudgetMs,
                                                /*interactive=*/true);
                if (!sol.converged) break;  // don't auto-touch a broken sketch
                json proposals = coincident_proposals(sol.entities, sk["constraints"],
                                                      tol_mm, touched_eids);
                if (proposals.empty()) break;
                json res = apply_coincident_proposals(params, proposals, env, gen,
                                                      kInteractiveSolveBudgetMs);
                if (res["applied"].is_array() && !res["applied"].empty())
                    feat["params"] = params;  // write the pinned constraints back
            } catch (...) { /* best-effort: leave the feature untouched */ }
            break;
        }
    }
}

}  // namespace

json doc_sketch_auto_constrain(const std::string& doc_id, const std::string& sketch_fid,
                               const json& opts) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    json env = env_from_doc(s.doc);

    double tol_mm = 1e-4;
    bool apply = true;
    if (opts.is_object()) {
        if (opts.contains("tol_mm") && opts["tol_mm"].is_number())
            tol_mm = opts["tol_mm"].get<double>();
        if (opts.contains("apply") && opts["apply"].is_boolean())
            apply = opts["apply"].get<bool>();
    }

    // Solve current geometry, gather the coincident proposals (read-only either way).
    chudware::sketch::SketchSolution sol = solve_feat_cold(s, feat);
    json params = sketch_params(feat);
    json cons = params.contains("constraints") ? params["constraints"] : json::array();
    json proposals = coincident_proposals(sol.entities, cons, tol_mm);

    json out;
    out["proposals"] = proposals;

    if (!apply) {
        out["applied"] = json::array();
        out["skipped_conflicting"] = 0;
        out["converged"] = sol.converged;
        return out;
    }

    int regions_before = material_region_count(s, sketch_fid);

    // Trial-apply each proposal with rollback-on-conflict into a working params copy.
    json work_params = params;
    if (!work_params.contains("entities")) work_params["entities"] = json::array();
    if (!work_params.contains("constraints")) work_params["constraints"] = json::array();
    IdGen gen(s.doc);
    json applied_res = apply_coincident_proposals(work_params, proposals, env, gen);

    out["applied"] = applied_res["applied"];
    out["skipped_conflicting"] = applied_res["skipped_conflicting"];

    // Persist the accepted constraints as ONE undoable batch (only if any landed).
    if (applied_res["applied"].is_array() && !applied_res["applied"].empty()) {
        push_undo(s);
        // Write the augmented constraints array back into the live feature params.
        json& live = find_sketch_feature(s, sketch_fid);
        if (live.contains("params") && live["params"].is_object())
            live["params"]["constraints"] = work_params["constraints"];
        else
            live["constraints"] = work_params["constraints"];
        s.revision += 1;
        s.dirty = true;
        if (s.active_sketch) s.active_sketch->solve_state.reset();
        try {
            CompileResult r = compile_session(s);
            (void)r;
        } catch (...) { /* closure count below still reflects the constraints */ }
    }

    int regions_after = material_region_count(s, sketch_fid);
    out["regions_before"] = regions_before;
    out["regions_after"] = regions_after;
    out["converged"] = sol.converged;
    return out;
}

json doc_sketch_health(const std::string& doc_id, const std::string& sketch_fid) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    json env = env_from_doc(s.doc);

    json params = sketch_params(feat);
    json sk;
    sk["entities"] = params.contains("entities") ? params["entities"] : json::array();
    sk["constraints"] = params.contains("constraints") ? params["constraints"] : json::array();

    chudware::sketch::SketchSolution sol = chudware::sketch::solve_sketch(sk, env, std::nullopt);

    // ---- closure ------------------------------------------------------------
    json closure = closure_summary(sol.entities);
    int mregions = material_region_count(s, sketch_fid);
    if (mregions >= 0) closure["material_regions"] = mregions;

    // ---- free-variable narration -------------------------------------------
    // Reuse the cold solve above (sol.x) — free_variables rebuilds the identical
    // system, so handing it the solved vector skips its redundant second cold solve.
    std::vector<chudware::sketch::FreeVar> fvs = chudware::sketch::free_variables(sk, env, sol.x);
    json free_vars = json::array();
    // Group per entity for a readable phrase ("line L4: end x, end y").
    std::map<std::string, std::vector<std::string>> by_ent;
    std::vector<std::string> ent_order;
    for (const auto& fv : fvs) {
        if (!by_ent.count(fv.entity)) ent_order.push_back(fv.entity);
        by_ent[fv.entity].push_back(fv.param);
        free_vars.push_back({{"entity", fv.entity}, {"param", fv.param}, {"var", fv.var}});
    }
    std::string dof_narration;
    for (const std::string& eid : ent_order) {
        if (!dof_narration.empty()) dof_narration += "; ";
        std::string et;
        for (const json& e : sk["entities"])
            if (e.value("id", std::string()) == eid) { et = e.value("type", std::string()); break; }
        dof_narration += et + " " + eid + " ";
        const auto& ps = by_ent[eid];
        for (size_t i = 0; i < ps.size(); ++i) {
            if (i) dof_narration += ", ";
            dof_narration += ps[i];
        }
    }
    if (dof_narration.empty())
        dof_narration = (sol.dof <= 0) ? "fully constrained (no free variables)"
                                       : "unconstrained rigid-body freedom";

    // ---- per-entity status --------------------------------------------------
    json statuses = json::object();
    for (const auto& kv : sol.statuses) statuses[kv.first] = kv.second;

    // ---- redundant / conflicting with removal suggestions -------------------
    json cons_by_id = json::object();
    for (const json& c : sk["constraints"])
        if (c.is_object() && c.contains("id"))
            cons_by_id[c["id"].get<std::string>()] = c;
    auto tag_finding = [&](const std::string& cid, const char* kind) -> json {
        json rec;
        rec["id"] = cid;
        std::string ctype = cons_by_id.contains(cid)
                                ? cons_by_id[cid].value("type", std::string("?"))
                                : std::string("?");
        rec["type"] = ctype;
        rec["suggestion"] = std::string("remove ") + kind + " constraint " + cid +
                            " (" + ctype + ")";
        return rec;
    };
    json redundant = json::array();
    for (const std::string& cid : sol.redundant) redundant.push_back(tag_finding(cid, "redundant"));
    json conflicting = json::array();
    for (const std::string& cid : sol.conflicting) conflicting.push_back(tag_finding(cid, "conflicting"));

    // ---- overall verdict ----------------------------------------------------
    std::string status = overall_status(statuses, sol.dof, sol.conflicting, sol.redundant,
                                        cons_by_id);

    // ---- constraint schedule (heuristic: geometric first, then driving dims) --
    // Order the free DOFs into a suggested closing sequence. We do NOT fabricate exact
    // constraints; we phrase actionable steps a driver can turn into add_constraint /
    // sketch_set_dim calls. Geometric relations (coincident endpoints from the open
    // chains) precede dimensions, matching how a person constrains a sketch.
    json schedule = json::array();
    // Step group 1: close open chains via coincident (from the closure gap endpoints).
    if (closure.contains("open_endpoints") && closure["open_endpoints"].is_array() &&
        !closure["open_endpoints"].empty()) {
        json step;
        step["order"] = 1;
        step["kind"] = "geometric";
        step["action"] = "coincident";
        step["why"] = "close the open profile chain at the dangling endpoints";
        step["handles"] = closure["open_endpoints"];
        schedule.push_back(step);
    }
    // Step group 2: pin remaining geometric freedoms (per free variable, grouped).
    int order = 2;
    for (const std::string& eid : ent_order) {
        json step;
        step["order"] = order++;
        step["kind"] = "dimension";
        step["action"] = "add driving dimension / geometric relation";
        std::string why = eid + " is under-constrained in ";
        const auto& ps = by_ent[eid];
        for (size_t i = 0; i < ps.size(); ++i) { if (i) why += ", "; why += ps[i]; }
        step["why"] = why;
        step["entity"] = eid;
        schedule.push_back(step);
    }

    // ---- advisory/1 emittable items -----------------------------------------
    // Frozen aicad.advisory/1 shape (contracts/advisor.md §3): each finding carries
    // severity / check / finding / refs / candidate_fixes / source. Conflicts are
    // blockers, open profiles + under-constrained DOF are actions, a clean full sketch
    // is info. candidate_fixes is non-empty except for the info verdict.
    json advisory = json::array();
    int adv = 0;
    auto adv_id = [&]() { return std::string("adv_skh_") + std::to_string(adv++); };
    for (const json& cf : conflicting) {
        advisory.push_back({
            {"schema", "aicad.advisory/1"}, {"id", adv_id()}, {"stage", "sketch"},
            {"severity", "blocker"}, {"check", "sketch_conflict"},
            {"finding", std::string("constraint ") + cf.value("id", "?") + " (" +
                        cf.value("type", "?") + ") conflicts — the sketch cannot solve"},
            {"refs", {{"doc_id", doc_id}, {"sketch", sketch_fid},
                      {"constraints", json::array({cf.value("id", "?")})}}},
            {"candidate_fixes", json::array({{
                {"action", "delete_constraint"},
                {"params", {{"id", cf.value("id", "?")}}},
                {"basis", "removing the over-defining constraint restores a solvable system"},
                {"predicted_effect", "sketch solves; DOF returns to >= 0"},
                {"confidence", "kernel"}}})},
            {"source", {{"kind", "kernel"}, {"detail", "System::analyze conflicting_tags"}}}});
    }
    if (closure.contains("open_endpoints") && closure["open_endpoints"].is_array() &&
        !closure["open_endpoints"].empty()) {
        advisory.push_back({
            {"schema", "aicad.advisory/1"}, {"id", adv_id()}, {"stage", "sketch"},
            {"severity", "action"}, {"check", "sketch_open_profile"},
            {"finding", std::string("open profile: ") +
                        std::to_string(closure["open_endpoints"].size()) +
                        " endpoint(s) not joined — the loop will not close for extrude"},
            {"refs", {{"doc_id", doc_id}, {"sketch", sketch_fid},
                      {"handles", closure["open_endpoints"]}}},
            {"candidate_fixes", json::array({{
                {"action", "sketch_auto_constrain"},
                {"params", {{"sketch", sketch_fid}, {"apply", true}}},
                {"basis", "touching endpoints within tolerance can be auto-coincident"},
                {"predicted_effect", "dangling endpoints pinned; chains close"},
                {"confidence", "heuristic"}}})},
            {"source", {{"kind", "kernel"}, {"detail", "closure_summary loop endpoints"}}}});
    }
    if (sol.dof > 0 && !free_vars.empty()) {
        advisory.push_back({
            {"schema", "aicad.advisory/1"}, {"id", adv_id()}, {"stage", "sketch"},
            {"severity", "action"}, {"check", "sketch_underconstrained"},
            {"finding", std::string("under-constrained: dof ") + std::to_string(sol.dof) +
                        " — free: " + dof_narration},
            {"refs", {{"doc_id", doc_id}, {"sketch", sketch_fid}}},
            {"numbers", {{"dof", sol.dof}}},
            {"candidate_fixes", json::array({{
                {"action", "apply_constraint_schedule"},
                {"params", {{"schedule", schedule}}},
                {"basis", "geometric relations first, then driving dimensions, per DOF"},
                {"predicted_effect", "dof -> 0 (fully constrained)"},
                {"confidence", "heuristic"}}})},
            {"source", {{"kind", "kernel"}, {"detail", "System::analyze dof + SVD null-space"}}}});
    }
    for (const json& rf : redundant) {
        advisory.push_back({
            {"schema", "aicad.advisory/1"}, {"id", adv_id()}, {"stage", "sketch"},
            {"severity", "hint"}, {"check", "sketch_redundant"},
            {"finding", std::string("redundant constraint ") + rf.value("id", "?") + " (" +
                        rf.value("type", "?") + ") adds no new information"},
            {"refs", {{"doc_id", doc_id}, {"sketch", sketch_fid},
                      {"constraints", json::array({rf.value("id", "?")})}}},
            {"candidate_fixes", json::array({{
                {"action", "delete_constraint"},
                {"params", {{"id", rf.value("id", "?")}}},
                {"basis", "the relation is already implied by other constraints"},
                {"predicted_effect", "no geometry change; a cleaner constraint set"},
                {"confidence", "kernel"}}})},
            {"source", {{"kind", "kernel"}, {"detail", "System::analyze redundant_tags"}}}});
    }
    if (advisory.empty()) {
        advisory.push_back({
            {"schema", "aicad.advisory/1"}, {"id", adv_id()}, {"stage", "sketch"},
            {"severity", "info"}, {"check", "sketch_health"},
            {"finding", "sketch is fully constrained and closed"},
            {"refs", {{"doc_id", doc_id}, {"sketch", sketch_fid}}},
            {"candidate_fixes", json::array()},
            {"source", {{"kind", "kernel"}, {"detail", "System::analyze"}}}});
    }

    json out;
    out["ok"] = true;
    out["sketch"] = sketch_fid;
    out["status"] = status;
    out["dof"] = sol.dof;
    out["converged"] = sol.converged;
    out["closure"] = closure;
    out["dof_narration"] = dof_narration;
    out["free_variables"] = free_vars;
    out["statuses"] = statuses;
    out["redundant"] = redundant;
    out["conflicting"] = conflicting;
    out["schedule"] = schedule;
    out["advisory"] = advisory;
    // T-BL49 — associative Convert Entities status: one row per external ref, so a driver
    // (or the PM panel) sees which converted geometry is live vs stale (source no longer
    // resolves; last-good geometry kept). Empty array when the sketch has no external refs.
    {
        json xrs = json::array();
        if (params.contains("external_refs") && params["external_refs"].is_array())
            for (const json& xr : params["external_refs"]) {
                if (!xr.is_object()) continue;
                json row;
                row["id"] = xr.value("id", std::string());
                row["stale"] = xr.value("stale", false);
                row["entity_ids"] = xr.contains("entity_ids") ? xr["entity_ids"] : json::array();
                const json src = xr.contains("source") ? xr["source"] : json();
                row["kind"] = src.is_object()
                                  ? (src.contains("datum") ? std::string("datum")
                                                           : src.value("kind", std::string()))
                                  : std::string();
                xrs.push_back(std::move(row));
            }
        out["external_refs"] = std::move(xrs);
    }
    return out;
}

namespace {

// Intersection of plane (O2,n2) with the sketch plane (O1,u,v,n1) in sketch (u,v)
// coords. Returns (p_near, t): line point nearest the sketch origin + unit direction.
// Throws unsupported_ref when the planes are parallel (session.py::_plane_plane_line_uv).
std::pair<Eigen::Vector2d, Eigen::Vector2d> plane_plane_line_uv(
    const Eigen::Vector3d& O1, const Eigen::Vector3d& u, const Eigen::Vector3d& v,
    const Eigen::Vector3d& n1, const Eigen::Vector3d& O2, Eigen::Vector3d n2) {
    n2 = n2 / std::max(n2.norm(), 1e-30);
    Eigen::Vector3d d3 = n1.cross(n2);
    double L = d3.norm();
    if (L < 1e-9)
        throw OpError("unsupported_ref", "plane is parallel to the sketch plane",
                      {{"reason", "parallel planes"}});
    double c1 = n1.dot(O1), c2 = n2.dot(O2), k = n1.dot(n2);
    double denom = 1.0 - k * k;
    Eigen::Vector3d P0 = ((c1 - c2 * k) * n1 + (c2 - c1 * k) * n2) / denom;
    Eigen::Vector2d p0((P0 - O1).dot(u), (P0 - O1).dot(v));
    Eigen::Vector3d t3 = d3 / L;
    Eigen::Vector2d t(t3.dot(u), t3.dot(v));
    t = t / std::max(t.norm(), 1e-30);
    Eigen::Vector2d p_near = p0 - p0.dot(t) * t;
    return {p_near, t};
}

// Shared T-BL43 projection core — used by BOTH the sketch_convert op (mutating, ops path)
// and the doc_sketch_project_entities RPC (read-only preview). Compiles `doc`, resolves each
// external ref (datum plane / face / model edge), and projects it orthographically onto
// `plane` via recon.cpp's GeomProjLib kernel. A face contributes ALL its boundary wires;
// a datum plane contributes its intersection LINE with the sketch plane (a plane has no
// boundary to convert). Every emitted entity is construction:true (callers may override).
// Returns {entities:[...(no ids)...], approx:bool, sources:[...]}; throws OpError on a bad
// ref shape, no solid bodies (for face/edge refs), or a datum parallel to the sketch plane.
json project_external_refs(const json& doc, const chudware::geom::SketchPlane& plane,
                           const json& refs, const CompileResult* pre, bool strict) {
    const Eigen::Vector3d O1 = plane.origin, u = plane.uAxis, v = plane.vAxis,
                          n1 = plane.normal;
    // Callers that already compiled the doc (the associative refresh, which walks every
    // ref-bearing sketch) pass the compile in; ad-hoc callers compile here as before.
    CompileResult local;
    if (!pre) local = compile_document(doc, env_from_doc(doc));
    const CompileResult& r = pre ? *pre : local;

    json all_entities = json::array();
    json sources = json::array();
    bool any_approx = false;
    TopoDS_Shape projection_body;
    double projection_diag = 0.0;

    auto append_entities = [&](const std::vector<chudware::doc::ProjectedEntity>& pes) {
        for (const chudware::doc::ProjectedEntity& pe : pes) all_entities.push_back(pe.entity);
    };

    auto project_datum = [&](const std::string& name_in) {
        std::string name = name_in;
        if (name.rfind("plane.", 0) != 0) name = "plane." + name;
        gp_Pln pln = eval_resolve_plane(name);
        Eigen::Vector3d O2(pln.Location().X(), pln.Location().Y(), pln.Location().Z());
        gp_Dir nd = pln.Axis().Direction();
        Eigen::Vector3d n2(nd.X(), nd.Y(), nd.Z());
        auto [p_near, t] = plane_plane_line_uv(O1, u, v, n1, O2, n2);
        double diag = 0.0;
        for (const TopoDS_Shape& sh : r.shapes) diag = std::max(diag, sim_bbox_diag(sh));
        double half = std::max(2.0 * diag, 100.0);
        Eigen::Vector2d p1 = p_near - half * t, p2 = p_near + half * t;
        all_entities.push_back(json{{"type", "line"}, {"construction", true},
                                    {"p1", json::array({p1.x(), p1.y()})},
                                    {"p2", json::array({p2.x(), p2.y()})}});
        sources.push_back(json{{"kind", "datum"}, {"name", name}});
    };

    for (const json& ref : refs) {
        if (!ref.is_object() || (!ref.contains("datum") && !ref.contains("kind")))
            throw OpError("invalid_op",
                          "sketch projection: each ref needs 'datum' or 'kind'+'selector'",
                          {{"reason", "bad ref"}});
        // Per-source entity count (sources[i].count) — lets op_sketch_convert slice the
        // flat entity list back into per-ref groups for associative external_refs records.
        const std::size_t ents_before = all_entities.size();
        auto stamp_count = [&]() {
            if (!sources.empty())
                sources.back()["count"] = static_cast<int>(all_entities.size() - ents_before);
        };
        if (ref.contains("datum")) {
            project_datum(ref["datum"].get<std::string>());
            stamp_count();
            continue;
        }

        if (r.shapes.empty())
            throw OpError("invalid_op", "sketch projection: no solid bodies",
                          {{"reason", "no solid bodies"}});
        // doc_query mints selectors over the whole document. Resolve against that
        // same topology domain, not the last body: a later independent extrusion
        // must not hide the earlier body supplying an associative conversion.
        if (projection_body.IsNull()) {
            if (r.shapes.size() == 1) {
                projection_body = r.shapes.front();
            } else {
                TopoDS_Compound compound;
                BRep_Builder builder;
                builder.MakeCompound(compound);
                for (const TopoDS_Shape& shape : r.shapes)
                    if (!shape.IsNull()) builder.Add(compound, shape);
                projection_body = compound;
            }
            projection_diag = sim_bbox_diag(projection_body);
        }
        const TopoDS_Shape& body = projection_body;
        const double diag = projection_diag;
        json sel = ref.contains("selector") ? ref["selector"] : json(nullptr);
        std::string kind = ref.value("kind", std::string());

        // STRICT (associative refresh) resolution: the scored best-match with the
        // contracts/selectors.md accept thresholds (best < 0.35, margin > 0.05), so a
        // DELETED/DRASTICALLY-MOVED source dangles -> the caller marks the ref stale
        // instead of silently re-projecting the nearest unrelated geometry. Creation-time
        // resolution (strict=false) keeps eval_resolve_ref's forgiving centroid-near pick
        // (the selector was minted seconds ago from live geometry).
        auto resolve_sub = [&](const json& s, const std::string& k) -> TopoDS_Shape {
            if (!strict) return eval_resolve_ref(s, body, diag);
            auto dangle = [&](const ResolveResult& rr) -> OpError {
                return OpError("dangling_ref",
                               "external ref no longer resolves" +
                                   (rr.dangling_reason ? " (" + *rr.dangling_reason + ")"
                                                       : std::string()),
                               {{"reason", "dangling external ref"}});
            };
            if (k == "face") {
                std::vector<TopoInfo> infos = describe_faces(body);
                ResolveResult rr = resolve(s, infos, diag);
                if (!rr.index.has_value()) throw dangle(rr);
                int idx = 0;
                for (TopExp_Explorer ex(body, TopAbs_FACE); ex.More(); ex.Next(), ++idx)
                    if (idx == *rr.index) return ex.Current();
            } else {
                // UNIQUE edges via TopExp::MapShapes — describe_edges visits a shared edge
                // once PER incident face, and those duplicates score identically, tripping
                // resolve()'s margin rule ("symmetric ambiguity") on a perfectly-good edge.
                TopTools_IndexedMapOfShape m;
                TopExp::MapShapes(body, TopAbs_EDGE, m);
                std::vector<TopoInfo> infos;
                infos.reserve(m.Extent());
                for (int i = 1; i <= m.Extent(); ++i)
                    infos.push_back(chudware::doc::edge_info(TopoDS::Edge(m.FindKey(i)), i - 1));
                ResolveResult rr = resolve(s, infos, diag);
                if (!rr.index.has_value()) throw dangle(rr);
                if (*rr.index >= 0 && *rr.index < m.Extent())
                    return m.FindKey(*rr.index + 1);
            }
            throw OpError("dangling_ref", "external ref resolved out of range",
                          {{"reason", "dangling external ref"}});
        };

        if (kind == "face") {
            TopoDS_Shape sub = resolve_sub(sel, "face");
            TopoDS_Face face = TopoDS::Face(sub);
            std::vector<TopoDS_Edge> edges = chudware::doc::face_boundary_edges(face);
            bool approx = false;
            append_entities(chudware::doc::project_edges_to_plane(edges, plane, approx));
            any_approx = any_approx || approx;
            sources.push_back(json{{"kind", "face"}, {"selector", sel},
                                   {"edges", static_cast<int>(edges.size())}});
            stamp_count();
        } else if (kind == "edge") {
            TopoDS_Shape sub = resolve_sub(sel, "edge");
            TopoDS_Edge edge = TopoDS::Edge(sub);
            bool approx = false;
            append_entities(chudware::doc::project_edges_to_plane({edge}, plane, approx));
            any_approx = any_approx || approx;
            sources.push_back(json{{"kind", "edge"}, {"selector", sel}});
            stamp_count();
        } else {
            throw OpError("invalid_op",
                          "sketch projection: ref kind '" + kind + "' must be face|edge",
                          {{"reason", "bad kind"}});
        }
    }

    return json{{"entities", all_entities}, {"approx", any_approx}, {"sources", sources}};
}

// ---------------------------------------------------------------- T-BL49 associative refresh

// Remove one sketch entity + every constraint whose refs touch it (same cascade as
// op_sketch_delete_entity, but callable on a params object during the refresh walk).
void erase_entity_and_constraints(json& params, const std::string& eid) {
    json kept = json::array();
    for (const json& e : params["entities"])
        if (e.value("id", std::string()) != eid) kept.push_back(e);
    params["entities"] = kept;
    json kc = json::array();
    for (const json& c : params["constraints"]) {
        bool touches = false;
        if (c.contains("refs") && c["refs"].is_array())
            for (const json& rref : c["refs"])
                if (rref.is_string()) {
                    const std::string s = rref.get<std::string>();
                    if (s == eid || s.rfind(eid + ".", 0) == 0) { touches = true; break; }
                }
        if (!touches) kc.push_back(c);
    }
    params["constraints"] = kc;
}

// Write a fresh projection back into the entities one external_ref owns, KEEPING STABLE
// ENTITY IDS so user constraints/dims attached to converted geometry survive a rebuild:
//   * index k of the new projection maps onto entity_ids[k] — its geometry (and type, if
//     the projected classification changed, e.g. circle -> ellipse after a plane tilt) is
//     overwritten in place under the SAME id; a user-toggled construction flag survives.
//   * extra projected curves (the source face gained an edge) append under new ids
//     (+ a fix pin when the ref was created with fix).
//   * leftover old entities (the source lost an edge) are deleted, cascading any
//     constraints that touched them (same rule as sketch_delete_entity).
void reconcile_external_ref_entities(json& params, json& xr, const json& new_ents, IdGen& gen) {
    if (!params.contains("entities") || !params["entities"].is_array())
        params["entities"] = json::array();
    if (!params.contains("constraints") || !params["constraints"].is_array())
        params["constraints"] = json::array();
    const bool constr = xr.value("construction", false);
    const bool fix = xr.value("fix", true);
    const json old_ids = xr.contains("entity_ids") && xr["entity_ids"].is_array()
                             ? xr["entity_ids"] : json::array();
    auto find_ent = [&](const std::string& id) -> json* {
        for (json& e : params["entities"])
            if (e.is_object() && e.value("id", std::string()) == id) return &e;
        return nullptr;
    };
    auto append_new = [&](json ne) -> std::string {
        const std::string eid = gen.next("e");
        ne["id"] = eid;
        ne["construction"] = constr;
        params["entities"].push_back(std::move(ne));
        if (fix)
            params["constraints"].push_back(
                json{{"type", "fix"}, {"refs", json::array({eid})}, {"id", gen.next("c")}});
        return eid;
    };

    json kept_ids = json::array();
    std::size_t k = 0;
    for (; k < new_ents.size(); ++k) {
        json ne = new_ents[k];
        if (k < old_ids.size() && old_ids[k].is_string()) {
            const std::string eid = old_ids[k].get<std::string>();
            if (json* e = find_ent(eid)) {
                // TYPE CHANGE (e.g. a line collapsed to a point because the re-resolved
                // plane tilted, or a circle became an ellipse): constraints holding
                // sub-handles of the OLD type ("e5.p1" on what is now a point) would fail
                // document validation and reject the whole edit batch. Drop the touched
                // constraints first (the honest cascade — those relations no longer have a
                // referent), then re-pin with a fresh fix so the entity stays solver-fixed.
                const bool type_changed =
                    e->value("type", std::string()) != ne.value("type", std::string());
                const bool keep_construction = e->value("construction", constr);
                if (type_changed) {
                    erase_entity_and_constraints(params, eid);
                    ne["id"] = eid;
                    ne["construction"] = keep_construction;
                    params["entities"].push_back(std::move(ne));
                    if (fix)
                        params["constraints"].push_back(
                            json{{"type", "fix"}, {"refs", json::array({eid})},
                                 {"id", gen.next("c")}});
                } else {
                    ne["id"] = eid;
                    // A user may have flipped the converted entity to construction; honor it.
                    ne["construction"] = keep_construction;
                    *e = std::move(ne);
                }
                kept_ids.push_back(eid);
                continue;
            }
        }
        kept_ids.push_back(append_new(std::move(ne)));  // grew, or user deleted the old one
    }
    for (; k < old_ids.size(); ++k)  // source shrank — drop the orphaned tail
        if (old_ids[k].is_string())
            erase_entity_and_constraints(params, old_ids[k].get<std::string>());
    xr["entity_ids"] = std::move(kept_ids);
}

// Re-resolve + re-project every sketch's params.external_refs against the current model
// (the associative half of Convert Entities). Semantics:
//   * runs on every mutating doc_apply_ops batch and on doc_rebuild, BEFORE the compile,
//     so the compile already sees converted geometry that follows its source;
//   * selectors resolve exactly like any other geometric reference (contracts/selectors.md
//     scored best-match over the compiled body) — no persistent topological naming;
//   * a ref whose source no longer resolves (owner feature deleted, geometry moved past the
//     score threshold, sketch plane itself dangling) is marked stale:true and its LAST-GOOD
//     entities are left untouched — the rebuild never fails because of a stale ref (the
//     sketch compiles with a feature-status warning; see document.cpp + sketch_health).
// The projection target is the doc compiled ONCE as-of the previous entity state (converted
// geometry only feeds downstream features, so one refresh pass converges in a single step
// for the supported upstream-source cases; a pathological self-referential loop simply
// settles on the next rebuild).
void refresh_sketch_external_refs(json& doc, CompileCache* cache,
                                  ::geom::DisplayQuality quality) {
    if (!doc.contains("features") || !doc["features"].is_array()) return;
    bool any = false;
    for (const json& f : doc["features"]) {
        if (!f.is_object() || f.value("type", std::string()) != "sketch") continue;
        if (f.value("suppressed", false)) continue;
        if (f.contains("params") && f["params"].is_object() &&
            f["params"].contains("external_refs") && f["params"]["external_refs"].is_array() &&
            !f["params"]["external_refs"].empty()) { any = true; break; }
    }
    if (!any) return;

    // One compile of the whole doc serves every ref and per-sketch plane resolution
    // (passed as `&pre`). Projection resolves over all surviving bodies, matching
    // doc_query's selector domain. Use the session's incremental cache when supplied.
    CompileResult pre = compile_document(doc, env_from_doc(doc), cache, quality);
    IdGen gen(doc);
    for (json& f : doc["features"]) {
        if (!f.is_object() || f.value("type", std::string()) != "sketch") continue;
        if (f.value("suppressed", false)) continue;
        if (!f.contains("params") || !f["params"].is_object()) continue;
        json& params = f["params"];
        if (!params.contains("external_refs") || !params["external_refs"].is_array() ||
            params["external_refs"].empty()) continue;

        std::optional<chudware::geom::SketchPlane> plane;
        try {
            plane = resolve_sketch_plane_doc(doc, f, &pre);
        } catch (...) { /* dangling plane -> every ref of this sketch goes stale below */ }

        for (json& xr : params["external_refs"]) {
            if (!xr.is_object() || !xr.contains("source")) continue;
            if (!plane) { xr["stale"] = true; continue; }
            json new_ents;
            try {
                json res = project_external_refs(doc, *plane, json::array({xr["source"]}),
                                                 &pre, /*strict=*/true);
                new_ents = res["entities"];
            } catch (...) {
                xr["stale"] = true;  // selector no longer resolves — keep last-good geometry
                continue;
            }
            if (!new_ents.is_array() || new_ents.empty()) { xr["stale"] = true; continue; }
            if (std::getenv("CHUDWARE_XREF_DEBUG"))
                std::fprintf(stderr, "[xref] sketch=%s ref=%s plane_o=(%g,%g,%g) n=(%g,%g,%g) new=%s\n",
                             f.value("id", std::string()).c_str(),
                             xr.value("id", std::string()).c_str(),
                             plane->origin.x(), plane->origin.y(), plane->origin.z(),
                             plane->normal.x(), plane->normal.y(), plane->normal.z(),
                             new_ents.dump().c_str());
            reconcile_external_ref_entities(params, xr, new_ents, gen);
            xr["stale"] = false;
        }
    }
}

// sketch3d external model references (T-3DXREF): re-resolve every sketch3d feature's
// params.external_refs against the current model and write back last_point/stale.
// v1 targets: vertex -> fixed 3D point (REQUIRED, implemented below). edge/face
// targets are NOT resolved here (gated — see contracts/doc_schema.md §3B); an
// external_ref whose source.kind isn't "vertex" is left stale:true untouched so
// callers get an honest signal instead of a silent no-op.
//
// Resolution target: the FINAL compiled body/bodies of the document (one compile
// serves every ref, mirroring refresh_sketch_external_refs's `pre` convention) —
// NOT the body-as-of-the-sketch3d-feature's position in history. This is a
// documented v1 simplification: a sketch3d authored early in the tree that
// references a vertex from a LATER feature will still resolve (arguably even more
// useful for "chase the final corner"), but it means a self-referential loop
// (sketch3d -> its own downstream sweep -> vertex back on the same sketch3d) is
// not meaningful and isn't guarded against beyond the existing rebuild recursion
// guards elsewhere in this file.
// A sketch3d feature stores its authored fields under "params" (the canonical
// on-disk shape — see add_feature/edit_feature) but may also appear flattened
// (top-level "external_refs") from callers that already normalized it. Return a
// pointer to whichever json array is live, or nullptr if neither is present —
// mirrors sketch_params' params/flat duality without a value-copy (we need to
// MUTATE the array in place).
json* sketch3d_external_refs_slot(json& f) {
    if (f.contains("params") && f["params"].is_object()) {
        json& p = f["params"];
        if (p.contains("external_refs") && p["external_refs"].is_array())
            return &p["external_refs"];
        return nullptr;
    }
    if (f.contains("external_refs") && f["external_refs"].is_array())
        return &f["external_refs"];
    return nullptr;
}

void refresh_sketch3d_external_refs(json& doc, CompileCache* cache,
                                    ::geom::DisplayQuality quality) {
    if (!doc.contains("features") || !doc["features"].is_array()) return;
    bool any = false;
    for (json& f : doc["features"]) {
        if (!f.is_object() || f.value("type", std::string()) != "sketch3d") continue;
        if (f.value("suppressed", false)) continue;
        json* refs = sketch3d_external_refs_slot(f);
        if (refs && !refs->empty()) { any = true; break; }
    }
    if (!any) return;

    CompileResult pre = compile_document(doc, env_from_doc(doc), cache, quality);
    if (pre.shapes.empty()) {
        // No body yet (e.g. sketch3d is the very first feature) — every vertex ref
        // goes stale, kept at its last-good point.
        for (json& f : doc["features"]) {
            if (!f.is_object() || f.value("type", std::string()) != "sketch3d") continue;
            json* refs = sketch3d_external_refs_slot(f);
            if (!refs) continue;
            for (json& xr : *refs)
                if (xr.is_object()) xr["stale"] = true;
        }
        return;
    }

    // Concatenate vertex descriptors across every body (a multi-body doc), tagging
    // each with which shape it came from so the resolved centroid always comes back
    // from the right body.
    std::vector<TopoInfo> infos;
    std::vector<int> shape_of;
    double diag = 0.0;
    for (size_t si = 0; si < pre.shapes.size(); ++si) {
        diag = std::max(diag, bbox_diag(pre.shapes[si]));
        std::vector<TopoInfo> vi = describe_vertices(pre.shapes[si]);
        for (TopoInfo& ti : vi) {
            shape_of.push_back(static_cast<int>(si));
            infos.push_back(std::move(ti));
        }
    }
    if (diag <= 0.0) diag = 1.0;

    for (json& f : doc["features"]) {
        if (!f.is_object() || f.value("type", std::string()) != "sketch3d") continue;
        if (f.value("suppressed", false)) continue;
        json* refs = sketch3d_external_refs_slot(f);
        if (!refs) continue;
        for (json& xr : *refs) {
            if (!xr.is_object() || !xr.contains("source") || !xr["source"].is_object()) continue;
            const json& src = xr["source"];
            if (src.value("kind", std::string()) != "vertex" || !src.contains("selector")) {
                xr["stale"] = true;  // edge/face refs: not yet resolved (v1 gap)
                continue;
            }
            std::optional<int> idx = resolve_with_hint_fallback(src["selector"], infos, diag);
            if (!idx) { xr["stale"] = true; continue; }
            const Eigen::Vector3d& c = infos[*idx].centroid;
            xr["last_point"] = json::array({c.x(), c.y(), c.z()});
            xr["stale"] = false;
        }
    }
}

}  // namespace

// session.py::sketch_project_ref — project a datum plane / planar face / model edge
// into a sketch's (u,v) plane. READ-ONLY (no mutation, no rev bump, no rebuild).
// `ref` is {"datum":"front|top|right"} or {"kind":"face|edge","selector":{...}}.
json doc_sketch_project_ref(const std::string& doc_id, const std::string& sketch_fid,
                            const json& ref) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    chudware::geom::SketchPlane plane = resolve_sketch_plane(s, feat);
    Eigen::Vector3d O1 = plane.origin, u = plane.uAxis, v = plane.vAxis, n1 = plane.normal;

    if (!ref.is_object() || (!ref.contains("datum") && !ref.contains("kind")))
        throw OpError("invalid_op",
                      "sketch_project_ref: 'ref' must carry 'datum' or 'kind'+'selector'",
                      {{"reason", "bad ref"}});

    auto uv = [&](const Eigen::Vector3d& P) -> json {
        Eigen::Vector3d d = P - O1;
        return json::array({d.dot(u), d.dot(v)});
    };

    CompileResult r = compile_session(s);

    // ---- datum plane -> intersection line ----
    if (ref.contains("datum")) {
        std::string name = ref["datum"].get<std::string>();
        if (name.rfind("plane.", 0) != 0) name = "plane." + name;
        gp_Pln pln = eval_resolve_plane(name);  // throws on non-reserved datum
        Eigen::Vector3d O2(pln.Location().X(), pln.Location().Y(), pln.Location().Z());
        gp_Dir nd = pln.Axis().Direction();
        Eigen::Vector3d n2(nd.X(), nd.Y(), nd.Z());
        auto [p_near, t] = plane_plane_line_uv(O1, u, v, n1, O2, n2);
        double diag = 0.0;
        for (const TopoDS_Shape& sh : r.shapes) diag = std::max(diag, sim_bbox_diag(sh));
        double half = std::max(2.0 * diag, 100.0);
        Eigen::Vector2d p1 = p_near - half * t, p2 = p_near + half * t;
        return json{{"entity", {{"type", "line"},
                                {"p1", json::array({p1.x(), p1.y()})},
                                {"p2", json::array({p2.x(), p2.y()})}}},
                    {"source", name}};
    }

    std::string kind = ref.value("kind", std::string());
    if (r.shapes.empty())
        throw OpError("invalid_op", "sketch_project_ref: no solid bodies",
                      {{"reason", "no solid bodies"}});
    // Resolve against the LAST body (single-body parts; multi-body best-match deferred).
    const TopoDS_Shape& body = r.shapes.back();
    double diag = sim_bbox_diag(body);
    json sel = ref.contains("selector") ? ref["selector"] : json(nullptr);

    if (kind == "face") {
        TopoDS_Shape sub = eval_resolve_ref(sel, body, diag);
        TopoDS_Face face = TopoDS::Face(sub);
        BRepAdaptor_Surface ad(face);
        if (ad.GetType() != GeomAbs_Plane)
            throw OpError("unsupported_ref",
                          "sketch_project_ref: only planar faces can be projected",
                          {{"reason", "non-planar face"}});
        GProp_GProps props;
        BRepGProp::SurfaceProperties(face, props);
        gp_Pnt cm = props.CentreOfMass();
        gp_Dir nd = ad.Plane().Axis().Direction();
        auto [p_near, t] = plane_plane_line_uv(O1, u, v, n1,
                                               Eigen::Vector3d(cm.X(), cm.Y(), cm.Z()),
                                               Eigen::Vector3d(nd.X(), nd.Y(), nd.Z()));
        // Span by the face's projected vertices, +5 mm pad.
        std::vector<double> ss;
        TopTools_IndexedMapOfShape vmap;
        TopExp::MapShapes(face, TopAbs_VERTEX, vmap);
        for (int i = 1; i <= vmap.Extent(); ++i) {
            gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(vmap(i)));
            Eigen::Vector3d d(p.X() - O1.x(), p.Y() - O1.y(), p.Z() - O1.z());
            Eigen::Vector2d duv(d.dot(u), d.dot(v));
            ss.push_back((duv - p_near).dot(t));
        }
        if (ss.empty()) ss.push_back(0.0);
        double lo = *std::min_element(ss.begin(), ss.end()) - 5.0;
        double hi = *std::max_element(ss.begin(), ss.end()) + 5.0;
        Eigen::Vector2d p1 = p_near + lo * t, p2 = p_near + hi * t;
        return json{{"entity", {{"type", "line"},
                                {"p1", json::array({p1.x(), p1.y()})},
                                {"p2", json::array({p2.x(), p2.y()})}}},
                    {"source", "planar face"}};
    }

    if (kind == "edge") {
        TopoDS_Shape sub = eval_resolve_ref(sel, body, diag);
        TopoDS_Edge edge = TopoDS::Edge(sub);
        BRepAdaptor_Curve ad(edge);
        GeomAbs_CurveType ct = ad.GetType();
        if (ct == GeomAbs_Line) {
            gp_Pnt Pa = ad.Value(ad.FirstParameter());
            gp_Pnt Pb = ad.Value(ad.LastParameter());
            json a = uv(Eigen::Vector3d(Pa.X(), Pa.Y(), Pa.Z()));
            json b = uv(Eigen::Vector3d(Pb.X(), Pb.Y(), Pb.Z()));
            double dx = double(a[0]) - double(b[0]), dy = double(a[1]) - double(b[1]);
            if (std::hypot(dx, dy) < 1e-6)
                return json{{"entity", {{"type", "point"}, {"p", a}}},
                            {"source", "linear edge (projects to a point)"}};
            return json{{"entity", {{"type", "line"}, {"p1", a}, {"p2", b}}},
                        {"source", "linear edge"}};
        }
        if (ct == GeomAbs_Circle) {
            gp_Circ circ = ad.Circle();
            gp_Dir axd = circ.Axis().Direction();
            Eigen::Vector3d ax3(axd.X(), axd.Y(), axd.Z());
            ax3 = ax3 / std::max(ax3.norm(), 1e-30);
            if (std::abs(ax3.dot(n1)) <= 1.0 - 1e-6)
                throw OpError("unsupported_ref",
                              "edge type not supported for sketch projection",
                              {{"reason", "circle axis not parallel to the sketch normal"}});
            gp_Pnt loc = circ.Location();
            return json{{"entity", {{"type", "circle"},
                                    {"center", uv(Eigen::Vector3d(loc.X(), loc.Y(), loc.Z()))},
                                    {"radius", circ.Radius()}}},
                        {"source", "circular edge"}};
        }
        throw OpError("unsupported_ref", "edge type not supported for sketch projection",
                      {{"reason", "unsupported curve type"}});
    }

    throw OpError("invalid_op",
                  "sketch_project_ref: ref kind '" + kind + "' must be face|edge",
                  {{"reason", "bad kind"}});
}

// session.cpp::doc_sketch_project_entities (T-BL43 "Convert Entities" / external references) —
// project one or more external references (datum plane, planar/any face, model edge) onto the
// ACTIVE sketch's (u,v) plane and return ready-to-insert doc-schema §3.1 reference entities.
//
// Unlike doc_sketch_project_ref (read-only PREVIEW, lines/circles only, returns ONE entity),
// this is the real Convert-Entities kernel: it uses GeomProjLib orthographic projection +
// recon.cpp's full curve classifier, so a face projects ALL its boundary wires (not just a
// plane-intersection line) and a tilted circle correctly becomes an ellipse. Every returned
// entity carries construction:true (reference geometry). This RPC is READ-ONLY (compiles the
// current bodies, no doc mutation); the caller inserts the returned entities into the sketch
// via sketch_add_entity ops through doc_apply_ops, which owns the rebuild/undo/rev-bump and
// makes the projected geometry constrainable by new sketch entities.
//
// `refs` is a LIST of ref objects, each {"datum":"front|top|right"} or
// {"kind":"face"|"edge","selector":{...}} (same selector grammar as sketch_project_ref).
// `body` optionally names which solid body to resolve face/edge selectors against (default:
// the last body). Returns {ok, entities:[...(no id)...], approx, sources:[...], plane_frame}.
// The associativity link back to the source is NOT stored — this is one-shot projection; the
// associative-update follow-up is filed separately (see work/questions / T-BL43 progress log).
json doc_sketch_project_entities(const std::string& doc_id, const std::string& sketch_fid,
                                 const json& refs) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch_feature(s, sketch_fid);
    chudware::geom::SketchPlane plane = resolve_sketch_plane(s, feat);

    if (!refs.is_array() || refs.empty())
        throw OpError("invalid_op",
                      "sketch_project_entities: 'refs' must be a non-empty list of ref objects",
                      {{"reason", "bad refs"}});

    json res = project_external_refs(s.doc, plane, refs);
    res["ok"] = true;
    res["plane_frame"] = plane_frame(plane);
    return res;
}

// ============================================================================
// Cross-document dependency registry (feat(xdoc))
// ============================================================================
//
// Process-global: lives for the lifetime of the loaded libchudware, reset when
// all sessions are closed. Thread-safe: protected by the existing doc mutex
// (DocLock) — same mutex serialises all doc_* calls so no extra lock needed.
//
// Schema:
//   g_dep_registry[source_doc_id] = vector<DepEntry>
// where each DepEntry is one (dep_doc_id, kind, linked_revision, stale) tuple.
// Entries survive as long as the process lives (we do NOT auto-prune on session
// close so a CAM job can track staleness even while only the part is open).

namespace {

struct DepEntry {
    std::string dep_doc_id;
    std::string kind;           // "cam" | "sim" | "drawing" | user-defined
    long linked_revision = -1;  // source revision at the time of last register
    bool stale = false;         // true when source has rebuilt past linked_revision
};

// Outer key: source_doc_id. Value: all dependents registered against it.
static std::map<std::string, std::vector<DepEntry>> g_dep_registry;

}  // namespace

json doc_dep_register(const std::string& source_doc_id,
                      const std::string& dep_doc_id,
                      const std::string& kind,
                      long linked_revision) {
    // Registry-only: g_dep_registry is guarded by g_registry_mtx (T-PERF5).
    std::lock_guard<std::mutex> reg(g_registry_mtx);
    auto& entries = g_dep_registry[source_doc_id];
    // Upsert: find existing entry for (dep_doc_id, kind) and update revision.
    for (auto& e : entries) {
        if (e.dep_doc_id == dep_doc_id && e.kind == kind) {
            e.linked_revision = linked_revision;
            e.stale = false;  // fresh link — no longer stale
            return json{{"registered", true}, {"updated", true}};
        }
    }
    entries.push_back(DepEntry{dep_doc_id, kind, linked_revision, false});
    return json{{"registered", true}, {"updated", false}};
}

json doc_dep_query(const std::string& source_doc_id, const std::string& kind_filter) {
    long src_rev = -1;
    if (auto pin = pin_session(source_doc_id)) {
        std::lock_guard<std::mutex> doc(pin->mtx);
        src_rev = pin->revision;
    }

    json deps = json::array();
    {
        std::lock_guard<std::mutex> reg(g_registry_mtx);
        auto it = g_dep_registry.find(source_doc_id);
        if (it != g_dep_registry.end()) {
            for (const auto& e : it->second) {
                if (!kind_filter.empty() && e.kind != kind_filter) continue;
                bool stale = e.stale;
                // Also check live revision if the source is open.
                if (!stale && src_rev >= 0 && e.linked_revision >= 0 &&
                    src_rev > e.linked_revision)
                    stale = true;
                deps.push_back(json{
                    {"dep_doc_id", e.dep_doc_id},
                    {"kind", e.kind},
                    {"linked_revision", e.linked_revision},
                    {"stale", stale},
                });
            }
        }
    }
    return json{{"source_doc_id", source_doc_id},
                {"source_revision", src_rev},
                {"dependents", deps}};
}

// Internal: mark dependents stale. Takes g_registry_mtx. Called from
// warm_and_respond (which holds the source session lock — registry after
// session is allowed; we never take a session mutex under the registry).
static int dep_notify_unlocked(const std::string& source_doc_id, long new_revision) {
    std::lock_guard<std::mutex> reg(g_registry_mtx);
    int notified = 0;
    auto it = g_dep_registry.find(source_doc_id);
    if (it != g_dep_registry.end()) {
        for (auto& e : it->second) {
            if (e.linked_revision >= 0 && new_revision > e.linked_revision) {
                e.stale = true;
                ++notified;
            }
        }
    }
    return notified;
}

json doc_dep_notify(const std::string& source_doc_id, long new_revision) {
    int notified = dep_notify_unlocked(source_doc_id, new_revision);
    return json{{"notified", notified}, {"source_revision", new_revision}};
}

json doc_dep_unregister(const std::string& dep_doc_id, const std::string& kind) {
    std::lock_guard<std::mutex> reg(g_registry_mtx);
    int removed = 0;
    for (auto& kv : g_dep_registry) {
        auto& vec = kv.second;
        auto before = vec.size();
        vec.erase(std::remove_if(vec.begin(), vec.end(),
            [&](const DepEntry& e) {
                return e.dep_doc_id == dep_doc_id &&
                       (kind.empty() || e.kind == kind);
            }), vec.end());
        removed += (int)(before - vec.size());
    }
    return json{{"unregistered", removed}};
}

// ============================================================ kinematic motion study
// (+ optional rigid-body inverse dynamics on the swept trajectory — T-FIN20;
//  the math lives in doc/motion_dynamics.{h,cpp}, this file only gathers mass
//  properties, resolved mate geometry and poses, and serialises the result.)

namespace {

// Mate type -> the exact wrench subspace represented by the corresponding
// kinematic constraint. Empty means that this inverse-dynamics model cannot
// honestly represent the mate's conditional/coupled reaction.
std::string mate_to_joint_type(const std::string& mate_type,
                               const std::string& geom_a,
                               const std::string& geom_b) {
    if (mate_type == "angle") return "drive_moment";
    if (mate_type == "distance") return "drive_force";
    if (mate_type == "hinge") return "revolute";
    if (mate_type == "concentric") return "cylindrical";
    if (mate_type == "slot") return "prismatic";
    if (mate_type == "coincident") {
        if (geom_a == "plane" && geom_b == "plane") return "planar";
        if (geom_a == "axis" && geom_b == "axis") return "cylindrical";
        return "spherical";
    }
    if (mate_type == "tangent" || mate_type == "cam_follower") return "contact";
    if (mate_type == "parallel") return "parallel";
    if (mate_type == "perpendicular") return "perpendicular";
    if (mate_type == "lock") return "fixed";
    return std::string();
}

[[noreturn]] void motion_invalid(const std::string& message) {
    throw OpError("invalid_op", "simulate_motion: " + message);
}

Eigen::Vector3d motion_vec3(const json& value, const std::string& name) {
    if (!value.is_array() || value.size() != 3)
        motion_invalid(name + " must be a 3-element array");
    Eigen::Vector3d out;
    for (int i = 0; i < 3; ++i) {
        if (!value[static_cast<size_t>(i)].is_number())
            motion_invalid(name + " must contain only numbers");
        out[i] = value[static_cast<size_t>(i)].get<double>();
        if (!std::isfinite(out[i])) motion_invalid(name + " must contain only finite values");
    }
    return out;
}

// Build + solve inverse dynamics from an immutable assembly snapshot and one
// already-resolved sweep result. `poses` translations arrive in mm.
json build_motion_dynamics(
    const json& assembly_doc,
    const AssemblyResult& assembly,
    const std::vector<std::map<std::string, Eigen::Matrix4d>>& poses,
    double dt_s,
    const json& opts) {
    namespace md = chudware::doc::motiondyn;
    if (!opts.is_object()) motion_invalid("dynamics must be an object");
    if (!std::isfinite(dt_s) || dt_s <= 0.0)
        motion_invalid("dynamics time step must be finite and > 0 s");

    json warnings = json::array();
    std::vector<md::BodyProps> bodies;
    std::vector<md::JointDef> joints;
    std::map<std::string, int> body_idx;
    json bodies_j = json::array();
    json joints_j = json::array();
    bool complete_joint_model = true;

    const size_t nf = poses.size();
    if (nf < 3) motion_invalid("dynamics requires at least 3 solved trajectory frames");
    for (const auto& frame : poses)
        if (frame.size() != poses.front().size())
            motion_invalid("every dynamics frame must contain the same components");

    std::map<std::string, bool> fixed_of;
    if (assembly_doc.contains("components") && assembly_doc["components"].is_array())
        for (const json& c : assembly_doc["components"])
            fixed_of[c.value("id", std::string())] = c.value("fixed", false);

    double def_dens = 7.85;
    if (opts.contains("default_density_g_cm3")) {
        if (!opts["default_density_g_cm3"].is_number())
            motion_invalid("default_density_g_cm3 must be numeric");
        def_dens = opts["default_density_g_cm3"].get<double>();
    }
    if (!std::isfinite(def_dens) || def_dens <= 0.0)
        motion_invalid("default_density_g_cm3 must be finite and > 0");

    json densities = json::object();
    json masses = json::object();
    if (opts.contains("densities")) {
        if (!opts["densities"].is_object()) motion_invalid("densities must be an object");
        densities = opts["densities"];
    }
    if (opts.contains("masses")) {
        if (!opts["masses"].is_object()) motion_invalid("masses must be an object");
        masses = opts["masses"];
    }
    std::set<std::string> component_ids;
    for (const AsmBodyOut& body : assembly.bodies)
        if (body.component_id.find('#') == std::string::npos)
            component_ids.insert(body.component_id);
    auto validate_component_scalars = [&](const json& values, const std::string& name) {
        for (auto it = values.begin(); it != values.end(); ++it) {
            if (!component_ids.count(it.key()))
                motion_invalid(name + " references unknown component '" + it.key() + "'");
            if (!it.value().is_number())
                motion_invalid(name + "['" + it.key() + "'] must be numeric");
            const double v = it.value().get<double>();
            if (!std::isfinite(v) || v <= 0.0)
                motion_invalid(name + "['" + it.key() + "'] must be finite and > 0");
        }
    };
    validate_component_scalars(densities, "densities");
    validate_component_scalars(masses, "masses");

    // ---- per-component mass properties (geometry x density) -----------------
    for (const AsmBodyOut& body : assembly.bodies) {
        const std::string& cid = body.component_id;
        if (cid.find('#') != std::string::npos) continue;
        if (!poses.front().count(cid)) continue;
        for (const auto& frame : poses)
            if (!frame.count(cid))
                motion_invalid("component '" + cid + "' is missing from a dynamics frame");

        GProp_GProps vp;
        BRepGProp::VolumeProperties(body.shape_local, vp);
        const double vol_mm3 = vp.Mass();
        if (!std::isfinite(vol_mm3) || vol_mm3 <= 0.0)
            motion_invalid("component '" + cid + "' has no positive solid volume");
        double density_g_mm3 =
            (densities.contains(cid) ? densities[cid].get<double>() : def_dens) * 1e-3;
        double mass_kg = vol_mm3 * density_g_mm3 * 1e-3;
        if (masses.contains(cid)) {
            mass_kg = masses[cid].get<double>();
            density_g_mm3 = mass_kg * 1e3 / vol_mm3;
        }
        if (!std::isfinite(mass_kg) || mass_kg <= 0.0)
            motion_invalid("component '" + cid + "' mass must be finite and > 0 kg");

        const gp_Pnt com = vp.CentreOfMass();
        const Eigen::Vector3d com_m(com.X() * 1e-3, com.Y() * 1e-3, com.Z() * 1e-3);
        // OCCT MatrixOfInertia() is ALREADY about the centre of mass (GProp_GProps.hxx).
        // Subtracting a parallel-axis term here made translated component geometry acquire
        // negative/non-physical inertia.
        const gp_Mat im = vp.MatrixOfInertia();
        Eigen::Matrix3d inertia_com;
        for (int r = 1; r <= 3; ++r)
            for (int c = 1; c <= 3; ++c)
                inertia_com(r - 1, c - 1) =
                    im.Value(r, c) * density_g_mm3 * 1e-9;  // g*mm^2 -> kg*m^2
        inertia_com = 0.5 * (inertia_com + inertia_com.transpose());

        md::BodyProps bp;
        bp.id = cid;
        bp.mass_kg = mass_kg;
        bp.com_local = com_m;
        bp.inertia_local = inertia_com;
        bp.fixed = fixed_of.count(cid) ? fixed_of[cid] : false;
        bp.poses.reserve(nf);
        for (const auto& frame : poses) {
            Eigen::Matrix4d T = frame.at(cid);
            T.block<3, 1>(0, 3) *= 1e-3;  // mm -> m
            bp.poses.push_back(T);
        }
        const bool fixed = bp.fixed;
        body_idx[cid] = static_cast<int>(bodies.size());
        bodies.push_back(std::move(bp));
        json inertia_j = json::array();
        for (int r = 0; r < 3; ++r)
            for (int c = 0; c < 3; ++c) inertia_j.push_back(inertia_com(r, c));
        bodies_j.push_back({{"component", cid},
                            {"mass_kg", mass_kg},
                            {"volume_mm3", vol_mm3},
                            {"density_g_cm3", density_g_mm3 * 1e3},
                            {"com_local_mm", {com.X(), com.Y(), com.Z()}},
                            {"inertia_com_kg_m2", inertia_j},
                            {"fixed", fixed}});
    }
    if (bodies.empty()) motion_invalid("no solid trajectory bodies were available");

    auto world_point = [&](const AsmMateRefGeom& ref, size_t frame) {
        const Eigen::Matrix4d& T = poses[frame].at(ref.component);
        return T.block<3, 3>(0, 0) * ref.geom.point +
               T.block<3, 1>(0, 3);
    };

    // ---- joints from mates + resolved geometry ------------------------------
    const json mates = assembly_doc.contains("mates") && assembly_doc["mates"].is_array()
                           ? assembly_doc["mates"] : json::array();
    for (const json& mate : mates) {
        if (mate.value("suppressed", false)) continue;
        const std::string mid = mate.value("id", std::string());
        const std::string mtype = mate.value("type", std::string());
        auto git = assembly.mate_geometry.find(mid);
        if (git == assembly.mate_geometry.end()) {
            warnings.push_back("mate '" + mid + "' (" + mtype +
                               ") has no resolved geometry — reactions unavailable");
            complete_joint_model = false;
            continue;
        }
        const AsmMateRefGeom* ref0 = nullptr;
        const AsmMateRefGeom* ref1 = nullptr;
        for (const AsmMateRefGeom& ref : git->second) {
            if (ref.ref_index == 0) ref0 = &ref;
            if (ref.ref_index == 1) ref1 = &ref;
        }
        if (!ref0 || !ref1 || !body_idx.count(ref0->component) ||
            !body_idx.count(ref1->component)) {
            warnings.push_back("mate '" + mid + "' (" + mtype +
                               ") does not have two resolved trajectory references");
            complete_joint_model = false;
            continue;
        }
        const std::string jtype =
            mate_to_joint_type(mtype, ref0->geom.kind, ref1->geom.kind);
        if (jtype.empty()) {
            warnings.push_back("mate '" + mid + "' (" + mtype +
                               ") has a conditional/coupled wrench basis that dynamics "
                               "does not model — reaction omitted");
            complete_joint_model = false;
            continue;
        }

        md::JointDef joint;
        joint.id = mid;
        joint.type = jtype;
        joint.body_a = body_idx.at(ref0->component);
        joint.body_b = body_idx.at(ref1->component);
        joint.anchor_local = ref0->geom.point * 1e-3;
        joint.anchor_b_local = ref1->geom.point * 1e-3;
        joint.has_anchor_b = true;

        joint.axis_world_per_frame.reserve(nf);
        for (size_t f = 0; f < nf; ++f) {
            const Eigen::Matrix3d Ra = poses[f].at(ref0->component).block<3, 3>(0, 0);
            const Eigen::Matrix3d Rb = poses[f].at(ref1->component).block<3, 3>(0, 0);
            Eigen::Vector3d axis = Ra * ref0->geom.dir_vector();
            if (jtype == "drive_force") {
                if (ref0->geom.kind == "plane") {
                    axis = Ra * ref0->geom.normal;
                } else if (ref1->geom.kind == "plane") {
                    axis = Rb * ref1->geom.normal;
                } else {
                    axis = world_point(*ref1, f) - world_point(*ref0, f);
                }
            } else if (jtype == "contact") {
                if (ref0->geom.kind == "plane") {
                    axis = Ra * ref0->geom.normal;
                } else if (ref1->geom.kind == "plane") {
                    axis = Rb * ref1->geom.normal;
                } else {
                    axis = world_point(*ref1, f) - world_point(*ref0, f);
                    if (ref0->geom.kind == "axis") {
                        const Eigen::Vector3d cyl_axis = Ra * ref0->geom.direction;
                        axis -= axis.dot(cyl_axis) * cyl_axis;
                    } else if (ref1->geom.kind == "axis") {
                        const Eigen::Vector3d cyl_axis = Rb * ref1->geom.direction;
                        axis -= axis.dot(cyl_axis) * cyl_axis;
                    }
                }
            } else if (jtype == "drive_moment" || jtype == "perpendicular") {
                axis = (Ra * ref0->geom.dir_vector()).cross(
                    Rb * ref1->geom.dir_vector());
                if (axis.norm() <= 1e-10) {
                    const size_t lo = f == 0 ? 0 : f - 1;
                    const size_t hi = f + 1 < nf ? f + 1 : nf - 1;
                    const Eigen::Matrix3d Ra_lo =
                        poses[lo].at(ref0->component).block<3, 3>(0, 0);
                    const Eigen::Matrix3d Ra_hi =
                        poses[hi].at(ref0->component).block<3, 3>(0, 0);
                    const Eigen::Matrix3d Rb_lo =
                        poses[lo].at(ref1->component).block<3, 3>(0, 0);
                    const Eigen::Matrix3d Rb_hi =
                        poses[hi].at(ref1->component).block<3, 3>(0, 0);
                    axis = chudware::mate::mat_to_rotvec(Rb_hi * Rb_lo.transpose()) -
                           chudware::mate::mat_to_rotvec(Ra_hi * Ra_lo.transpose());
                }
            }
            if (!axis.allFinite() || axis.norm() <= 1e-10) {
                warnings.push_back("mate '" + mid +
                                   "' has an indeterminate reaction direction at frame " +
                                   std::to_string(f) + " — reaction omitted");
                complete_joint_model = false;
                joint.axis_world_per_frame.clear();
                break;
            }
            joint.axis_world_per_frame.push_back(axis.normalized());
        }
        if (joint.axis_world_per_frame.size() != nf) continue;
        joint.axis_local =
            poses.front().at(ref0->component).block<3, 3>(0, 0).transpose() *
            joint.axis_world_per_frame.front();
        joints_j.push_back({{"mate_id", mid},
                            {"mate_type", mtype},
                            {"joint_type", joint.type},
                            {"component_a", ref0->component},
                            {"component_b", ref1->component},
                            {"reaction_convention",
                             "force_n/moment_nm act on component_b from component_a"}});
        joints.push_back(std::move(joint));
    }

    // ---- options -------------------------------------------------------------
    md::Options options;
    options.dt = dt_s;
    if (opts.contains("gravity"))
        options.gravity = motion_vec3(opts["gravity"], "dynamics.gravity");
    if (opts.contains("damping")) {
        if (!opts["damping"].is_object()) motion_invalid("damping must be an object");
        const json& damping = opts["damping"];
        if (damping.contains("linear_ns_per_m")) {
            if (!damping["linear_ns_per_m"].is_number())
                motion_invalid("damping.linear_ns_per_m must be numeric");
            options.damping_lin = damping["linear_ns_per_m"].get<double>();
        }
        if (damping.contains("angular_nms_per_rad")) {
            if (!damping["angular_nms_per_rad"].is_number())
                motion_invalid("damping.angular_nms_per_rad must be numeric");
            options.damping_ang = damping["angular_nms_per_rad"].get<double>();
        }
        if (!std::isfinite(options.damping_lin) || options.damping_lin < 0.0 ||
            !std::isfinite(options.damping_ang) || options.damping_ang < 0.0)
            motion_invalid("damping coefficients must be finite and non-negative");
    }
    if (opts.contains("loads")) {
        if (!opts["loads"].is_array()) motion_invalid("loads must be an array");
        for (const json& load : opts["loads"]) {
            if (!load.is_object()) motion_invalid("every load must be an object");
            const std::string cid = load.value("component", std::string());
            auto it = body_idx.find(cid);
            if (it == body_idx.end())
                motion_invalid("applied load references unknown component '" + cid + "'");
            if (bodies[static_cast<size_t>(it->second)].fixed)
                motion_invalid("applied load targets fixed component '" + cid + "'");
            md::AppliedLoad applied;
            applied.body = it->second;
            bool have_load = false;
            if (load.contains("force_n")) {
                applied.force_n = motion_vec3(load["force_n"], "load.force_n");
                have_load = true;
            }
            if (load.contains("torque_nm")) {
                applied.torque_nm = motion_vec3(load["torque_nm"], "load.torque_nm");
                have_load = true;
            }
            if (!have_load)
                motion_invalid("load on component '" + cid +
                               "' must provide force_n and/or torque_nm");
            options.loads.push_back(applied);
        }
    }

    // ---- solve + serialise ---------------------------------------------------
    md::DynResult solved = md::solve_inverse_dynamics(bodies, joints, options);
    for (const std::string& warning : solved.warnings) warnings.push_back(warning);

    json frames_j = json::array();
    json per_joint_worst = json::object();
    json per_joint_worst_moment = json::object();
    json worst_case = nullptr;
    json worst_moment_case = nullptr;
    double worst_force = -1.0;
    double worst_moment = -1.0;
    bool any_nonzero_reaction = false;
    for (size_t f = 0; f < solved.frames.size(); ++f) {
        const md::FrameDyn& frame = solved.frames[f];
        json reactions = json::array();
        for (size_t j = 0; j < joints.size(); ++j) {
            const md::JointReaction& reaction = frame.reactions[j];
            const double force_mag = reaction.force_n.norm();
            const double moment_mag = reaction.moment_nm.norm();
            any_nonzero_reaction =
                any_nonzero_reaction || force_mag > 1e-12 || moment_mag > 1e-12;
            reactions.push_back({
                {"mate_id", joints[j].id},
                {"component_a", joints_j[j]["component_a"]},
                {"component_b", joints_j[j]["component_b"]},
                {"force_n",
                 {reaction.force_n.x(), reaction.force_n.y(), reaction.force_n.z()}},
                {"moment_nm",
                 {reaction.moment_nm.x(), reaction.moment_nm.y(), reaction.moment_nm.z()}},
                {"force_mag_n", force_mag},
                {"moment_mag_nm", moment_mag}});
            const std::string& mid = joints[j].id;
            if (!per_joint_worst.contains(mid) ||
                per_joint_worst[mid].value("force_mag_n", -1.0) < force_mag)
                per_joint_worst[mid] = {
                    {"frame", static_cast<int>(f)}, {"t_s", frame.t},
                    {"force_mag_n", force_mag},
                    {"force_n",
                     {reaction.force_n.x(), reaction.force_n.y(), reaction.force_n.z()}},
                    {"moment_nm",
                     {reaction.moment_nm.x(), reaction.moment_nm.y(), reaction.moment_nm.z()}}};
            if (!per_joint_worst_moment.contains(mid) ||
                per_joint_worst_moment[mid].value("moment_mag_nm", -1.0) < moment_mag)
                per_joint_worst_moment[mid] = {
                    {"frame", static_cast<int>(f)}, {"t_s", frame.t},
                    {"moment_mag_nm", moment_mag},
                    {"moment_nm",
                     {reaction.moment_nm.x(), reaction.moment_nm.y(), reaction.moment_nm.z()}}};
            if (force_mag > worst_force) {
                worst_force = force_mag;
                worst_case = {{"frame", static_cast<int>(f)}, {"t_s", frame.t},
                              {"mate_id", mid}, {"force_mag_n", force_mag}};
            }
            if (moment_mag > worst_moment) {
                worst_moment = moment_mag;
                worst_moment_case = {{"frame", static_cast<int>(f)}, {"t_s", frame.t},
                                     {"mate_id", mid}, {"moment_mag_nm", moment_mag}};
            }
        }
        frames_j.push_back({
            {"i", static_cast<int>(f)}, {"t_s", frame.t},
            {"residual_n", frame.force_residual_n},
            {"force_residual_n", frame.force_residual_n},
            {"moment_residual_nm", frame.moment_residual_nm},
            {"balanced", frame.balanced},
            {"reactions", reactions}});
    }
    if (!any_nonzero_reaction) {
        warnings.push_back("all solved joint reaction wrenches are zero");
        worst_case = nullptr;
        worst_moment_case = nullptr;
    }

    const bool dynamics_ok = solved.ok && complete_joint_model;
    return json{{"ok", dynamics_ok},
                {"force_path_resolved", dynamics_ok},
                {"has_nonzero_reaction", any_nonzero_reaction},
                {"dt_s", dt_s},
                {"duration_s", dt_s * static_cast<double>(nf - 1)},
                {"gravity", {options.gravity.x(), options.gravity.y(), options.gravity.z()}},
                {"bodies", bodies_j},
                {"joints", joints_j},
                {"frames", frames_j},
                {"per_joint_worst", per_joint_worst},
                {"per_joint_worst_moment", per_joint_worst_moment},
                {"worst_case", worst_case},
                {"worst_moment_case", worst_moment_case},
                {"warnings", warnings}};
}

}  // namespace

// Mate types whose scalar `value` field can be swept as a motion driver.
// Mirrors services/mcp/cad_asm_tools.py::_DRIVABLE_MATE_TYPES.
static const std::map<std::string, std::string> k_drivable_mate_types = {
    {"angle", "deg"},
    {"distance", "mm"},
};

// Keep double precision at the RPC seam; viewport consumers may narrow on upload.
// Row-major M[r][c] = flat[r*4+c]; col-major M[r][c] = flat[c*4+r].
static std::array<double, 16> row_to_col_major(const std::vector<double>& rm) {
    std::array<double, 16> cm{};
    if (rm.size() < 16) { cm[0] = 1; cm[5] = 1; cm[10] = 1; cm[15] = 1; return cm; }
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c)
            cm[(size_t)(c * 4 + r)] = rm[(size_t)(r * 4 + c)];
    return cm;
}

struct MotionAxisLine {
    std::string component;
    Eigen::Vector3d point = Eigen::Vector3d::Zero();
    Eigen::Vector3d direction = Eigen::Vector3d::Zero();
    Eigen::Matrix4d base = Eigen::Matrix4d::Identity();
    double value = 0.0;
    double sign = 1.0;
};

static Eigen::Matrix4d motion_matrix(const std::vector<double>& values) {
    Eigen::Matrix4d out;
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c) out(r, c) = values.at(r * 4 + c);
    return out;
}

// A scalar angle supplies ONE rotational equation, not a revolute joint. Motion
// needs a line as well: authored point+axis, the mate's own third axial reference,
// or a concentric/hinge joint on its driven body. Never guess a pivot at the origin.
static MotionAxisLine motion_axis_line(const json& doc, json& driver,
                                       const AssemblyResult& initial) {
    MotionAxisLine line;
    const auto& refs = driver.at("refs");
    const std::string a = refs.at(0).at("component");
    const std::string b = refs.at(1).at("component");
    // Prefer the second measured body; if it is grounded, drive the first with
    // opposite angular sign. Both-floating mechanisms still have a well-defined
    // driver when the mate supplies a line; the full solve and pose check decide
    // whether the network can honor it.
    const bool fb = component_is_fixed(doc, b);
    line.component = fb ? a : b;
    line.sign = fb ? -1.0 : 1.0;
    line.base = motion_matrix(initial.transforms.at(line.component));
    const Eigen::Matrix4d ma = motion_matrix(initial.transforms.at(a));
    json& value = driver["value"];
    if (!value.is_object()) value = {{"value", value}};
    line.value = value.value("value", 0.0);
    const json axis = value.value("axis", value.value("reference_axis", json()));
    const bool authored_axis = !axis.is_null();
    if (authored_axis)
        line.direction = ma.topLeftCorner<3, 3>() * motion_vec3(axis, "value.axis");
    bool have_point = false;
    if (value.contains("axis_point")) {
        line.point = ma.topLeftCorner<3, 3>() *
            motion_vec3(value["axis_point"], "value.axis_point") + ma.topRightCorner<3, 1>();
        have_point = true;
    }
    auto take_axis = [&](const AsmMateRefGeom& ref) {
        if (ref.geom.kind != "axis") return false;
        const Eigen::Matrix4d m = motion_matrix(initial.transforms.at(ref.component));
        Eigen::Vector3d dir = m.topLeftCorner<3, 3>() * ref.geom.direction;
        if (dir.norm() < 1e-12) return false;
        dir.normalize();
        if (authored_axis && line.direction.normalized().cross(dir).norm() > 1e-6)
            return false;
        if (!have_point) {
            line.point = m.topLeftCorner<3, 3>() * ref.geom.point + m.topRightCorner<3, 1>();
            have_point = true;
        }
        if (!authored_axis) {
            // Match the solver's derived-axis sign convention.
            line.direction = ma.topLeftCorner<3, 3>() *
                chudware::mate::canonical_sign(ma.topLeftCorner<3, 3>().transpose() * dir);
        }
        return true;
    };
    const auto own = initial.mate_geometry.find(driver.at("id").get<std::string>());
    if ((!have_point || !authored_axis) && own != initial.mate_geometry.end())
        for (const auto& ref : own->second)
            if (ref.ref_index >= 2 && take_axis(ref)) break;
    if (!have_point || line.direction.norm() < 1e-12)
        for (const json& mate : doc["mates"]) {
            const std::string type = mate.value("type", std::string());
            if (mate.value("suppressed", false) || (type != "concentric" && type != "hinge"))
                continue;
            const auto found = initial.mate_geometry.find(mate.at("id").get<std::string>());
            if (found == initial.mate_geometry.end()) continue;
            bool attached = false;
            for (const auto& ref : found->second)
                attached = attached || ref.component == line.component;
            if (!attached) continue;
            for (const auto& ref : found->second)
                if (ref.component == line.component && take_axis(ref)) break;
            if (have_point && line.direction.norm() >= 1e-12) break;
        }
    if (!have_point || !line.direction.allFinite() || line.direction.norm() < 1e-12)
        motion_invalid("driver_axis_line_unresolved: angle mate has no hinge line; "
                       "provide value.axis and value.axis_point in the first reference's "
                       "local frame, a third axial reference, or a concentric/hinge mate "
                       "on the driven body");
    line.direction.normalize();
    // Freeze the resolved direction for the entire sweep; keep its true 3D direction.
    const Eigen::Vector3d local_axis = ma.topLeftCorner<3, 3>().transpose() * line.direction;
    value["axis"] = {local_axis.x(), local_axis.y(), local_axis.z()};
    return line;
}

static json motion_pose_check(const MotionAxisLine& line, const Eigen::Matrix4d& pose,
                               double requested_value) {
    const Eigen::Matrix4d delta = pose * line.base.inverse();
    const Eigen::Matrix3d r = delta.topLeftCorner<3, 3>();
    // Validate against the requested world line, not the matrix used to drive it.
    const Eigen::Matrix3d expected_rotation = Eigen::AngleAxisd(
        line.sign * (requested_value - line.value) * M_PI / 180.0,
        line.direction).toRotationMatrix();
    const double tol = 1e-6 * std::max(1.0, line.point.norm());
    const double error = (delta.topRightCorner<3, 1>() -
                         (line.point - r * line.point)).norm();
    const bool translation_ok = error <= tol;
    const bool rotation_ok = (r - expected_rotation).cwiseAbs().maxCoeff() <= 1e-6;
    json invariant = nullptr;
    bool invariant_ok = true;
    for (int k = 0; k < 3; ++k)
        if (std::abs(std::abs(line.direction[k]) - 1.0) < 1e-12) {
            Eigen::RowVector4d row = Eigen::RowVector4d::Zero();
            row[k] = 1.0;
            invariant_ok = (delta.row(k) - row).cwiseAbs().maxCoeff() <= 1e-6;
            invariant = {{"axis", std::string(1, "xyz"[k])}, {"ok", invariant_ok}};
        }
    return {{"component", line.component},
            {"axis_point", {line.point.x(), line.point.y(), line.point.z()}},
            {"axis_dir", {line.direction.x(), line.direction.y(), line.direction.z()}},
            {"translation_ok", translation_ok}, {"translation_error_mm", error},
            {"rotation_ok", rotation_ok}, {"invariant_coordinate", invariant},
            {"ok", translation_ok && rotation_ok && invariant_ok}};
}

json doc_simulate_motion(const std::string& doc_id,
                         const std::string& mate_id_in,
                         double start, double stop, int steps,
                         bool check_interference,
                         double min_clearance_mm,
                         const json& dynamics) {
    if (steps < 2)
        motion_invalid("steps must be >= 2");
    if (!std::isfinite(start) || !std::isfinite(stop))
        motion_invalid("start and stop must be finite");
    if (!std::isfinite(min_clearance_mm) || min_clearance_mm < 0.0)
        motion_invalid("min_clearance_mm must be finite and non-negative");
    const bool want_dyn = !dynamics.is_null();
    if (want_dyn && !dynamics.is_object())
        motion_invalid("dynamics must be an object or null");
    if (want_dyn && steps < 3)
        motion_invalid("dynamics requires steps >= 3");
    double duration_s = 1.0;
    if (want_dyn && dynamics.contains("duration_s")) {
        if (!dynamics["duration_s"].is_number())
            motion_invalid("dynamics.duration_s must be numeric");
        duration_s = dynamics["duration_s"].get<double>();
        if (!std::isfinite(duration_s) || duration_s <= 0.0)
            motion_invalid("dynamics.duration_s must be finite and > 0 s");
    }

    // Work on a deep document snapshot and private warm state. A motion study is
    // read-only: it must not bump revision, dirty the document, add undo records,
    // erase a mate expression, or leave the assembly at a sampled pose.
    json sweep_doc;
    std::string base_dir;
    std::optional<chudware::mate::WarmMap> warm;
    {
        DocLock lock;
        Session& live = require_session(doc_id);
        if (!live.is_assembly())
            motion_invalid("document is not an assembly");
        sweep_doc = live.doc;
        if (!live.path.empty())
            base_dir = std::filesystem::path(live.path).parent_path().string();
        if (!live.asm_warm.empty()) warm = live.asm_warm;
    }

    std::string mate_id = mate_id_in;
    std::string driver_type;
    std::string driver_unit;
    json& mates = sweep_doc["mates"];
    if (!mates.is_array() || mates.empty())
        motion_invalid("document has no mates. Build an assembly with mates first.");
    std::vector<json*> drivable;
    for (json& mate : mates) {
        if (mate.value("suppressed", false)) continue;
        if (k_drivable_mate_types.count(mate.value("type", std::string())))
            drivable.push_back(&mate);
    }
    if (mate_id.empty()) {
        if (drivable.empty())
            motion_invalid("no drivable dimensional mate found; add an angle or distance mate");
        if (drivable.size() > 1)
            motion_invalid("multiple drivable mates — pass mate_id explicitly");
        mate_id = drivable.front()->value("id", std::string());
    }
    json* driven = nullptr;
    for (json& mate : mates)
        if (mate.value("id", std::string()) == mate_id) {
            driven = &mate;
            break;
        }
    if (!driven)
        motion_invalid("mate '" + mate_id + "' not found");
    driver_type = driven->value("type", std::string());
    auto driver_it = k_drivable_mate_types.find(driver_type);
    if (driver_it == k_drivable_mate_types.end())
        motion_invalid("mate '" + mate_id + "' (type '" + driver_type +
                       "') has no scalar value that the mate solver actually drives; "
                       "drive an angle or distance mate");
    driver_unit = driver_it->second;

    std::optional<MotionAxisLine> axis_line;
    if (driver_type == "angle") {
        const AssemblyResult initial = compile_assembly(sweep_doc, base_dir, warm);
        if (!initial.converged)
            motion_invalid("driver_initial_pose_unresolved: assembly must solve before driving");
        warm = initial.warm;
        try {
            axis_line = motion_axis_line(sweep_doc, *driven, initial);
        } catch (const std::exception& e) {
            return {{"ok", false}, {"doc_id", doc_id}, {"mate_id", mate_id},
                    {"reason", "driver_axis_line_unresolved"}, {"error", e.what()},
                    {"trajectory", json::array()},
                    {"pose_check", {{"ok", false}, {"reason", "driver_axis_line_unresolved"},
                                    {"frames", json::array()}}}};
        }
    }
    json pose_frames = json::array();
    bool all_pose_ok = true;
    const int drive_dof = angle_drive_dof(sweep_doc);

    std::vector<double> samples;
    samples.reserve(static_cast<size_t>(steps));
    for (int i = 0; i < steps; ++i)
        samples.push_back(start + (stop - start) *
                                    static_cast<double>(i) /
                                    static_cast<double>(steps - 1));

    json trajectory = json::array();
    json reference_bodies = json::array();
    json reference_transforms = json::object();
    json first_collision_step = nullptr;
    json first_collision_value = nullptr;
    json first_collision_detail = json::array();
    bool had_collision = false;
    bool all_converged = true;
    std::vector<std::map<std::string, Eigen::Matrix4d>> dyn_poses;
    dyn_poses.reserve(want_dyn ? samples.size() : 0);
    std::optional<AssemblyResult> dyn_topology;

    for (int i = 0; i < steps; ++i) {
        const double value = samples[static_cast<size_t>(i)];
        // Change only the scalar: axis/axis_point and other mate geometry survive.
        if (!(*driven)["value"].is_object())
            (*driven)["value"] = {{"value", (*driven)["value"]}};
        (*driven)["value"]["value"] = value;
        (*driven)["value"]["expr"] = nullptr;
        Eigen::Matrix3d drive_rotation = Eigen::Matrix3d::Identity();
        if (axis_line) {
            const auto& line = *axis_line;
            drive_rotation = chudware::mate::rodrigues(
                line.direction * (line.sign * (value - line.value) * M_PI / 180.0));
            Eigen::Matrix4d delta = Eigen::Matrix4d::Identity();
            delta.topLeftCorner<3, 3>() = drive_rotation;
            delta.topRightCorner<3, 1>() = line.point - drive_rotation * line.point;
            const Eigen::Matrix4d pose = delta * line.base;
            (*warm)[line.component] = {pose.topRightCorner<3, 1>(),
                chudware::mate::mat_to_rotvec(pose.topLeftCorner<3, 3>())};
        }
        AssemblyResult solved = compile_assembly(sweep_doc, base_dir, warm);
        warm = solved.warm;
        if (i == 0) {
            reference_transforms = solved.transforms;
            for (const AsmBodyOut& body : solved.bodies) {
                const TopoDS_Shape world = placed_world_shape(body);
                if (world.IsNull()) continue;
                Bnd_Box box;
                BRepBndLib::Add(world, box);
                if (box.IsVoid()) continue;
                double xa, ya, za, xb, yb, zb;
                box.Get(xa, ya, za, xb, yb, zb);
                reference_bodies.push_back({
                    {"component", body.component_id},
                    {"bbox", {{"min", {xa, ya, za}}, {"max", {xb, yb, zb}}}}});
            }
        }

        bool converged = solved.converged;
        json issues = json::array();
        for (const AsmComponentStatus& status : solved.component_status)
            if (!status.ok) {
                converged = false;
                issues.push_back("component '" + status.id + "' failed to build");
            }
        for (const AsmMateStatus& status : solved.mate_status)
            if (status.status == "unresolved" || status.status == "conflicting") {
                converged = false;
                issues.push_back("mate '" + status.id + "' is " + status.status);
            }
        all_converged = all_converged && converged;

        json bodies_j = json::array();
        std::map<std::string, Eigen::Matrix4d> frame_poses;
        for (const AsmBodyOut& body : solved.bodies) {
            if (body.transform.size() != 16) {
                converged = false;
                all_converged = false;
                issues.push_back("component '" + body.component_id +
                                 "' returned an invalid transform");
                continue;
            }
            Eigen::Matrix4d transform;
            bool finite = true;
            for (int r = 0; r < 4; ++r)
                for (int c = 0; c < 4; ++c) {
                    const double v = body.transform[static_cast<size_t>(r * 4 + c)];
                    transform(r, c) = v;
                    finite = finite && std::isfinite(v);
                }
            if (!finite) {
                converged = false;
                all_converged = false;
                issues.push_back("component '" + body.component_id +
                                 "' returned a non-finite transform");
                continue;
            }
            auto column_major = row_to_col_major(body.transform);
            json transform_j = json::array();
            for (double entry : column_major) transform_j.push_back(entry);
            bodies_j.push_back({{"name", body.name},
                                {"component", body.component_id},
                                {"transform", transform_j}});
            if (body.component_id.find('#') == std::string::npos)
                frame_poses[body.component_id] = transform;
        }
        json pose_check = {{"ok", true}, {"bodies", json::array()}};
        if (axis_line) {
            const auto it = frame_poses.find(axis_line->component);
            if (it == frame_poses.end()) {
                pose_check["ok"] = false;
                pose_check["reason"] = "driver_pose_missing";
            } else {
                json checked = motion_pose_check(*axis_line, it->second, value);
                pose_check["ok"] = checked["ok"];
                pose_check["bodies"].push_back(std::move(checked));
            }
            if (!pose_check["ok"].get<bool>()) {
                converged = false;
                all_converged = false;
                all_pose_ok = false;
                issues.push_back("driver_pose_mismatch: solved pose does not honor hinge line");
            }
        }
        pose_frames.push_back({{"i", i}, {"ok", pose_check["ok"]},
                               {"bodies", pose_check["bodies"]}});

        json frame = {{"i", i},
                      {"value", value},
                      {"dof", solved.dof + drive_dof},
                      {"residual_dof", solved.dof},
                      {"converged", converged},
                      {"bodies", bodies_j}, {"pose_check", pose_check}};
        if (!issues.empty()) frame["issues"] = issues;

        if (check_interference && converged) {
            std::vector<std::pair<std::string, TopoDS_Shape>> members;
            members.reserve(solved.bodies.size());
            for (const AsmBodyOut& body : solved.bodies)
                members.emplace_back(
                    body.name,
                    eval_interference_member_shape(placed_world_shape(body, std::nullopt)));
            json report = interference_report(
                members, /*include_coincident=*/false, min_clearance_mm, /*quick=*/false);
            json detail = json::array();
            for (const json& pair : report.value("interferences", json::array()))
                detail.push_back(pair);
            for (const json& pair : report.value("clearances", json::array()))
                if (pair.value("warning", std::string()) == "below min clearance")
                    detail.push_back(pair);
            const bool collision = !detail.empty();
            frame["interference"] = collision;
            // Per-step collision list (kinematic sweep evidence): every offending pair at
            // THIS sample, not just the first colliding sample. Compact by construction —
            // the report's overlap_mesh/bbox stay out of a trajectory that can hold
            // hundreds of steps. A below-min-clearance pair is a zero-overlap collision.
            json collisions = json::array();
            for (const json& pair : detail)
                collisions.push_back({{"a", pair.value("a", std::string())},
                                      {"b", pair.value("b", std::string())},
                                      {"overlap_volume_mm3",
                                       pair.value("overlap_volume_mm3", 0.0)}});
            frame["collisions"] = std::move(collisions);
            if (collision && !had_collision) {
                had_collision = true;
                first_collision_step = i;
                first_collision_value = value;
                first_collision_detail = detail;
            }
        }
        trajectory.push_back(std::move(frame));

        if (want_dyn) {
            dyn_poses.push_back(std::move(frame_poses));
            if (!dyn_topology) dyn_topology = solved;
        }
    }

    json interference = {
        {"checked", check_interference},
        {"first_collision_step", first_collision_step},
        {"first_collision_value", first_collision_value},
        {"detail", had_collision ? first_collision_detail : json::array()}};

    char summary_buf[320];
    if (!all_converged) {
        std::snprintf(summary_buf, sizeof(summary_buf),
                      "%d frames | driver %s | one or more frames did not solve",
                      steps, mate_id.c_str());
    } else if (had_collision) {
        std::snprintf(summary_buf, sizeof(summary_buf),
                      "%d frames | driver %s | interference/clearance violation at step %d",
                      steps, mate_id.c_str(), first_collision_step.get<int>());
    } else {
        std::snprintf(summary_buf, sizeof(summary_buf),
                      "%d frames | driver %s | no interference detected",
                      steps, mate_id.c_str());
    }

    json result = {
        {"ok", all_converged},
        {"doc_id", doc_id},
        {"mate_id", mate_id},
        {"driver", {{"type", driver_type},
                    {"unit", driver_unit},
                    {"start", start},
                    {"stop", stop},
                    {"steps", steps}}},
        {"trajectory", trajectory},
        {"reference_bodies", std::move(reference_bodies)},
        {"reference_transforms", std::move(reference_transforms)},
        {"pose_check", {{"ok", all_pose_ok}, {"frames", pose_frames}}},
        {"interference", interference},
        {"summary", std::string(summary_buf)}};
    if (!all_converged)
        result["error"] = "motion sweep contains unresolved or non-converged frames";

    if (want_dyn) {
        if (!all_converged || !dyn_topology) {
            result["dynamics"] = {
                {"ok", false},
                {"force_path_resolved", false},
                {"frames", json::array()},
                {"warnings", json::array(
                    {"dynamics skipped because the kinematic sweep did not fully converge"})}};
        } else {
            const double dt_s = duration_s / static_cast<double>(steps - 1);
            result["dynamics"] = build_motion_dynamics(
                sweep_doc, *dyn_topology, dyn_poses, dt_s, dynamics);
            if (!result["dynamics"].value("ok", false)) {
                result["ok"] = false;
                result["error"] =
                    "motion dynamics did not resolve every requested joint force path";
            }
        }
    }
    return result;
}

// ============================================================ sketch3d RPCs
// 3D free-space sketch (feature type "sketch3d", doc_schema §3B). Both RPCs are
// STATELESS — they solve from the persisted feature params on every call (no
// active-sketch session state; sketch3d has no plane/region machinery). The GUI
// commits a drag by sending doc_apply_ops edit_feature with the full solved
// entities array (contract: contracts/sketch3d_gui_contract.md).

namespace {

// Locate a "sketch3d" feature by id (throws on missing / wrong type).
json& find_sketch3d_feature(Session& s, const std::string& fid) {
    if (s.doc.contains("features") && s.doc["features"].is_array())
        for (json& f : s.doc["features"])
            if (f.is_object() && f.value("id", std::string()) == fid) {
                if (f.value("type", std::string()) != "sketch3d")
                    throw OpError("invalid_op", "feature '" + fid + "' is not a sketch3d",
                                  {{"reason", "not a sketch3d"}});
                return f;
            }
    throw OpError("invalid_op", "no sketch3d feature '" + fid + "'",
                  {{"reason", "unknown sketch3d"}});
}

// The {entities, constraints} dict solve_sketch3d takes, from either the host
// canonical shape (params.entities) or the flat fallback. Also grounds resolved
// external_refs (T-3DXREF) as fixed construction points + implicit fix constraints
// — same injection compile_document's sketch3d branch does, kept in sync here so
// sketch3d_health/sketch3d_drag see the exact same solve.
json sketch3d_solver_input(const json& feat) {
    const json p = sketch_params(feat);  // same params/flat duality as 2D
    json sk;
    sk["entities"] = p.contains("entities") ? p["entities"] : json::array();
    sk["constraints"] = p.contains("constraints") ? p["constraints"] : json::array();
    const json& refs = p.contains("external_refs") ? p["external_refs"] : json();
    if (refs.is_array())
        for (const json& xr : refs) {
            if (!xr.is_object()) continue;
            const std::string rid = xr.value("id", std::string());
            if (rid.empty() || !xr.contains("last_point") || !xr["last_point"].is_array() ||
                xr["last_point"].size() < 3)
                continue;
            sk["entities"].push_back(json{{"id", rid}, {"type", "point"},
                                          {"p", xr["last_point"]}, {"construction", true}});
            sk["constraints"].push_back(json{{"id", "__extref_fix_" + rid},
                                             {"type", "fix"}, {"refs", json::array({rid})}});
        }
    return sk;
}

// id-keyed solved-entity map -> the GUI-friendly array form [{id, type, …}, …],
// preserving each entity's construction flag from the authored params.
json sketch3d_entity_array(const json& coords, const json& sk) {
    auto construction_of = [&](const std::string& eid) -> bool {
        if (sk.contains("entities") && sk["entities"].is_array())
            for (const json& e : sk["entities"])
                if (e.is_object() && e.value("id", std::string()) == eid)
                    return e.value("construction", false);
        return false;
    };
    json out = json::array();
    if (coords.is_object())
        for (auto it = coords.begin(); it != coords.end(); ++it) {
            json rec = it.value();
            rec["id"] = it.key();
            rec["construction"] = construction_of(it.key());
            out.push_back(std::move(rec));
        }
    return out;
}

}  // namespace

json doc_sketch3d_health(const std::string& doc_id, const std::string& sketch_fid) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch3d_feature(s, sketch_fid);
    json env = env_from_doc(s.doc);
    json sk = sketch3d_solver_input(feat);

    chudware::sketch3d::Sketch3DSolution sol = chudware::sketch3d::solve_sketch3d(sk, env);

    json statuses = json::object();
    for (const auto& kv : sol.statuses) statuses[kv.first] = kv.second;
    json redundant = json::array(), conflicting = json::array();
    for (const auto& t : sol.redundant) redundant.push_back(t);
    for (const auto& t : sol.conflicting) conflicting.push_back(t);

    json free_vars = json::array();
    for (const auto& fv : chudware::sketch3d::free_variables3(sk, env))
        free_vars.push_back({{"entity", fv.entity}, {"param", fv.param}, {"var", fv.var}});

    // Wire-chain report: how the solved entities connect (the same chains the
    // compiler hands a sweep as spine/guide, in the same deterministic order).
    json wires = json::array();
    for (const TopoDS_Wire& w : chudware::geom::wires_from_sketch3d(sol.entities)) {
        int n_edges = 0;
        for (TopExp_Explorer ex(w, TopAbs_EDGE); ex.More(); ex.Next()) ++n_edges;
        wires.push_back({{"closed", static_cast<bool>(w.Closed())}, {"n_edges", n_edges}});
    }

    std::string status = "under";
    if (!sol.conflicting.empty() || !sol.converged) status = "over";
    else if (sol.dof == 0) status = "full";

    return json{{"ok", true},
                {"status", status},
                {"dof", sol.dof},
                {"converged", sol.converged},
                {"statuses", statuses},
                {"redundant", redundant},
                {"conflicting", conflicting},
                {"free", free_vars},
                {"entities", sketch3d_entity_array(sol.entities, sk)},
                {"wires", wires}};
}

json doc_sketch3d_drag(const std::string& doc_id, const std::string& sketch_fid,
                       const std::string& handle, const json& xyz, const json& targets) {
    DocLock lk;
    Session& s = require_session(doc_id);
    json& feat = find_sketch3d_feature(s, sketch_fid);
    json env = env_from_doc(s.doc);
    json sk = sketch3d_solver_input(feat);

    // Stateless: a cold solve supplies the warm-start state for this drag step.
    // (Authored coords ARE the last committed positions — doc_schema §3.4 —
    // so the cold solve is already at/near the on-screen configuration.)
    chudware::sketch3d::Sketch3DSolution base = chudware::sketch3d::solve_sketch3d(sk, env);

    Eigen::Vector3d target(0, 0, 0);
    if (xyz.is_array() && xyz.size() >= 3)
        target = Eigen::Vector3d(xyz[0].get<double>(), xyz[1].get<double>(),
                                 xyz[2].get<double>());
    json res = chudware::sketch3d::drag3(base.state, handle, target, sk, env,
                                      targets.is_null() ? json() : targets);

    const json res_entities = res.contains("entities") ? res["entities"] : json::object();
    json entities = sketch3d_entity_array(res_entities, sk);

    if (res.is_object() && res.contains("ok") && !res["ok"].get<bool>()) {
        // Failed/empty drag: keep the frozen entities so the host overlay is
        // preserved (same never-wipe rule as the 2D sketch_drag).
        return json{{"ok", false},
                    {"error", res.contains("error") && !res["error"].is_null()
                                  ? res["error"] : json("drag target did not resolve")},
                    {"rejected_handles", res.contains("rejected_handles")
                                             ? res["rejected_handles"] : json::array()},
                    {"entities", entities},
                    {"statuses", res.contains("statuses") ? res["statuses"] : json::object()}};
    }

    return json{{"ok", true},
                {"entities", entities},
                {"statuses", res.contains("statuses") ? res["statuses"] : json::object()},
                {"rejected_handles", res.contains("rejected_handles")
                                         ? res["rejected_handles"] : json::array()},
                {"converged", res.value("converged", true)},
                {"budget_exceeded", res.value("budget_exceeded", false)}};
}

// --- g_mtx contention reporting (Phase 1b) -----------------------------------------
// Stamp the calling thread's current RPC method so DocLock can attribute lock wait/hold to
// the caller. Pass a pointer with process-lifetime stability (the dispatch boundary passes an
// interned/stack std::string's c_str for the call duration; cleared to nullptr after). The
// pointer is only stored + used while the lock is held during THIS call.
void set_current_rpc_method(const char* m) { t_cur_method = m; }

// Snapshot (and optionally reset) the g_mtx contention meters. `%blocked` is aggregate
// thread-time spent waiting on the lock over the window wall-clock — it can exceed 100% when
// several threads block at once, which is exactly the multi-subagent serialization signal.
nlohmann::json contention_report(bool reset) {
    const auto now = std::chrono::steady_clock::now();
    const double window_ms =
        std::chrono::duration<double, std::milli>(now - g_cont_since).count();
    const unsigned long long acq = g_cont.acquisitions.load(std::memory_order_relaxed);
    const unsigned long long contended = g_cont.contended.load(std::memory_order_relaxed);
    const double total_wait_ms = g_cont.total_wait_ns.load(std::memory_order_relaxed) / 1e6;
    const double total_hold_ms = g_cont.total_hold_ns.load(std::memory_order_relaxed) / 1e6;

    nlohmann::json top = nlohmann::json::array();
    {
        std::lock_guard<std::mutex> lk(g_cont_map_mtx);
        std::vector<std::pair<std::string, MethodContention>> rows(g_cont_map.begin(),
                                                                   g_cont_map.end());
        std::sort(rows.begin(), rows.end(), [](const auto& a, const auto& b) {
            return a.second.wait_ns > b.second.wait_ns;
        });
        int n = 0;
        for (const auto& [name, mc] : rows) {
            if (n++ >= 20) break;
            top.push_back({{"method", name},
                           {"calls", mc.count},
                           {"total_wait_ms", mc.wait_ns / 1e6},
                           {"total_hold_ms", mc.hold_ns / 1e6},
                           {"max_wait_ms", mc.max_wait_ns / 1e6},
                           {"avg_wait_ms", mc.count ? (mc.wait_ns / 1e6) / mc.count : 0.0}});
        }
    }

    nlohmann::json out = {
        {"ok", true},
        {"window_ms", window_ms},
        {"acquisitions", acq},
        {"contended", contended},
        {"contended_pct", acq ? (100.0 * contended / acq) : 0.0},
        {"total_wait_ms", total_wait_ms},
        {"total_hold_ms", total_hold_ms},
        {"avg_wait_ms", acq ? total_wait_ms / acq : 0.0},
        {"avg_hold_ms", acq ? total_hold_ms / acq : 0.0},
        {"max_wait_ms", g_cont.max_wait_ns.load(std::memory_order_relaxed) / 1e6},
        {"max_hold_ms", g_cont.max_hold_ns.load(std::memory_order_relaxed) / 1e6},
        {"pct_wall_blocked", window_ms > 0 ? (100.0 * total_wait_ms / window_ms) : 0.0},
        {"max_waiters", g_cont.max_waiters.load(std::memory_order_relaxed)},
        {"waiters_now", g_cont.waiters.load(std::memory_order_relaxed)},
        {"top_methods", top},
        // Incremental-rebuild cache effectiveness (process-wide, since engine start):
        // full_hits = whole compiles served from cache; features_reused vs _executed =
        // per-feature skip ratio across every compile (document.h CompileCache).
        {"compile_cache", compile_cache_stats()},
    };

    if (reset) {
        g_cont.acquisitions.store(0, std::memory_order_relaxed);
        g_cont.contended.store(0, std::memory_order_relaxed);
        g_cont.total_wait_ns.store(0, std::memory_order_relaxed);
        g_cont.total_hold_ns.store(0, std::memory_order_relaxed);
        g_cont.max_wait_ns.store(0, std::memory_order_relaxed);
        g_cont.max_hold_ns.store(0, std::memory_order_relaxed);
        g_cont.max_waiters.store(0, std::memory_order_relaxed);
        std::lock_guard<std::mutex> lk(g_cont_map_mtx);
        g_cont_map.clear();
        g_cont_since = now;
    }
    return out;
}

}  // namespace chudware::doc
