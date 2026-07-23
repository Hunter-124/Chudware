// core/piping/fittings_data.h — the bundled B16.9/B16.5 fitting dimensions CSV,
// vendored verbatim from engine_oracle/piping/data/fittings.csv (dimensions are facts;
// no ASME text) so the C++ catalog needs no runtime data file.
#pragma once

namespace chudware { namespace piping { inline const char* FITTINGS_CSV = R"CSV(
type,nps,nps2,center_to_end,length,flange_class,od,thickness,bolt_circle,bolt_holes,bolt_dia
elbow,0.5,,38.1,,,,,,,
tee,0.5,,25.4,,,,,,,
elbow,0.75,,38.1,,,,,,,
tee,0.75,,28.45,,,,,,,
elbow,1.0,,38.1,,,,,,,
tee,1.0,,38.1,,,,,,,
elbow,1.5,,57.15,,,,,,,
tee,1.5,,47.75,,,,,,,
elbow,2.0,,76.2,,,,,,,
tee,2.0,,63.5,,,,,,,
elbow,2.5,,95.25,,,,,,,
tee,2.5,,69.85,,,,,,,
elbow,3.0,,114.3,,,,,,,
tee,3.0,,85.85,,,,,,,
elbow,4.0,,152.4,,,,,,,
tee,4.0,,104.65,,,,,,,
elbow,6.0,,228.6,,,,,,,
tee,6.0,,142.75,,,,,,,
elbow,8.0,,304.8,,,,,,,
tee,8.0,,177.8,,,,,,,
elbow,10.0,,381.0,,,,,,,
tee,10.0,,215.9,,,,,,,
elbow,12.0,,457.2,,,,,,,
tee,12.0,,254.0,,,,,,,
reducer,1.5,1.0,,50.8,,,,,,
reducer,2.0,1.0,,76.2,,,,,,
reducer,2.0,1.5,,76.2,,,,,,
reducer,3.0,2.0,,88.9,,,,,,
reducer,4.0,3.0,,101.6,,,,,,
reducer,6.0,4.0,,139.7,,,,,,
reducer,8.0,6.0,,152.4,,,,,,
reducer,10.0,8.0,,177.8,,,,,,
reducer,12.0,10.0,,203.2,,,,,,
flange,0.5,,,,150,88.9,11.18,60.45,4,15.75
flange,0.75,,,,150,98.55,12.7,69.85,4,15.75
flange,1.0,,,,150,107.95,14.22,79.25,4,15.75
flange,1.5,,,,150,127.0,17.53,98.55,4,15.75
flange,2.0,,,,150,152.4,19.05,120.65,4,19.05
flange,3.0,,,,150,190.5,23.88,152.4,4,19.05
flange,4.0,,,,150,228.6,23.88,190.5,8,19.05
flange,6.0,,,,150,279.4,25.4,241.3,8,22.35
flange,8.0,,,,150,342.9,28.45,298.45,8,22.35
flange,10.0,,,,150,406.4,30.23,361.95,12,25.4
flange,12.0,,,,150,482.6,31.75,431.8,12,25.4
)CSV";
}}  // namespace chudware::piping
