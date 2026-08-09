// core/cam/stock_sim.h — material-removal simulation + cycle-time estimation +
// collision/gouge verification over a toolpath. Uses a top-down Z-dexel grid
// (HeightField) of the stock that each cutting move lowers; OCCT-free.
#pragma once

#include <string>
#include <vector>

#include "cam/heightfield.h"
#include "cam/model.h"

namespace chudware {
namespace cam {

struct SimOptions {
    double resolution = 0.5;   // dexel grid spacing (mm)
    bool check_gouge = true;   // compare removed stock against the part surface
    bool check_rapid = true;   // flag rapid moves that pass through stock
    bool check_holder = false; // holder/shank collision (needs tool stickout)
    double gouge_tol = 0.05;   // allowed overcut below the part surface (mm)
    // >0: snapshot the remaining field at this many equal time intervals
    // (clamped to 64; large grids downsampled in the frames payload only).
    int frames = 0;
};

struct SimResult {
    bool ok = true;
    std::string error;
    double removed_volume_mm3 = 0;     // material removed by cutting moves
    double remaining_volume_mm3 = 0;   // stock left after the program
    double stock_volume_mm3 = 0;
    double machined_fraction = 0;      // removed / (stock - part)
    double cycle_time_s = 0;           // total estimated machining time
    double cut_time_s = 0;
    double rapid_time_s = 0;
    int gouges = 0;                    // # cells cut below part - gouge_tol
    double max_gouge_mm = 0;
    int rapid_collisions = 0;          // rapid moves intersecting remaining stock
    int holder_collisions = 0;         // cutting moves whose shank/holder body hits stock
    std::vector<std::string> warnings;
    std::string stock_lineage_in;
    std::string stock_lineage_out;
    // the simulated remaining-stock heightfield (for display / stock-to-stock rest)
    HeightField remaining;
    // equal-interval snapshots (aicad.cam_sim_frames/1); empty when options.frames==0
    std::vector<double> frame_times_s;
    std::vector<HeightField> frames;
    bool frames_clamped = false;       // true when count or grid was reduced
    json to_json(bool include_field = false) const;
    void frames_to_json(json& j) const;
};

// Simulate one setup: build the stock dexel field, sweep every toolpath's cutting
// moves, accumulate removal + time, and (optionally) verify against `part_mesh`.
SimResult simulate_setup(const Setup&, const ToolLibrary&,
                         const std::vector<Toolpath>&, const TriMesh& part_mesh,
                         const SimOptions& = {});

// Cycle-time-only estimate for one toolpath (feed-limited; optional trapezoidal
// accel when machine.accel > 0). Cheap; used when a full sim is overkill.
double estimate_time(const Toolpath&, const Machine&);

// Classify a toolpath's moves into colored polylines for the viewport backplot:
// returns {"rapid":[[x,y,z,...]], "cut":[...], "lead":[...], "plunge":[...],
//          "arc":[...]} as flat XYZ arrays per class.
json backplot(const Toolpath&);

}  // namespace cam
}  // namespace chudware
