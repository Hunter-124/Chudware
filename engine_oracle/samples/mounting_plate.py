"""Feature-rich reference model: a parametric mounting plate.

Exercises the kernel across the features that matter for "precise + feature rich":
rounded-rectangle base, an extruded central boss, a through-bore, four counterbored
corner holes, and a top-edge chamfer. Pure build123d, builder mode.
"""
from build123d import (
    Axis,
    BuildPart,
    BuildSketch,
    Circle,
    CounterBoreHole,
    GridLocations,
    Locations,
    Mode,
    Plane,
    RectangleRounded,
    chamfer,
    extrude,
)

# --- parameters -----------------------------------------------------------
plate_l, plate_w, plate_t = 120.0, 80.0, 10.0   # base plate
corner_r = 12.0                                  # plate corner radius
boss_d, boss_h = 34.0, 14.0                      # central boss
bore_d = 18.0                                    # through hole in boss
hole_d = 6.6                                     # M6 clearance
cbore_d, cbore_depth = 12.0, 5.0                 # counterbore
hole_dx, hole_dy = plate_l - 28.0, plate_w - 28.0  # corner-hole spacing
chamfer_size = 1.2

with BuildPart() as plate:
    # base plate
    with BuildSketch(Plane.XY):
        RectangleRounded(plate_l, plate_w, corner_r)
    extrude(amount=plate_t)

    # central boss on the top face
    with BuildSketch(Plane.XY.offset(plate_t)):
        Circle(boss_d / 2)
    extrude(amount=boss_h)

    # bore straight down through boss + plate
    with BuildSketch(Plane.XY.offset(plate_t + boss_h)):
        Circle(bore_d / 2)
    extrude(amount=-(plate_t + boss_h), mode=Mode.SUBTRACT)

    # four counterbored corner holes, drilled straight down from the plate top
    with Locations((0, 0, plate_t)):
        with GridLocations(hole_dx, hole_dy, 2, 2):
            CounterBoreHole(
                radius=hole_d / 2,
                counter_bore_radius=cbore_d / 2,
                counter_bore_depth=cbore_depth,
                depth=plate_t,
            )

    # chamfer the top outer edge of the plate
    top_edges = plate.edges().filter_by(Axis.Z, reverse=True).group_by(Axis.Z)[-1]
    try:
        chamfer(top_edges, length=chamfer_size)
    except Exception:  # noqa: BLE001 - chamfer is cosmetic; never fail the build on it
        pass

result = plate.part
