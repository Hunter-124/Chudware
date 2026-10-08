// core/hydraulic/motor.h — hydraulic-motor sizing and pump drive (electric
// motor) selection. Pure math, OCCT-free; contract in contracts/drives_schema.md.
//
//   size_motor(params)       hydraulic_size_motor RPC: displacement from torque /
//                            speed / differential pressure, next catalogue step
//                            per motor type, flow and power.
//   pump_drive_size(params)  pump_drive_size RPC: hydraulic or centrifugal pump
//                            shaft power → IEC 60072 kW or NEMA hp motor rating,
//                            pole count, IEC 60034-30-1 efficiency, frame, FLC.
//
// Envelopes: success {ok:true, ..., warnings:[], hint, next_actions};
// refusal {ok:false, reason:"<snake_case>", hint, param?}.
#pragma once

#include "piping/pyutil.h"

namespace chudware::hydraulic {

using pyutil::ojson;

ojson size_motor(const ojson& params);

ojson pump_drive_size(const ojson& params);

}  // namespace chudware::hydraulic
