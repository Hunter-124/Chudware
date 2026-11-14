DATUM PLANES - plane.top / plane.front / plane.right (Z-up world, mm).

All three pass through the WORLD ORIGIN. Their names are view names, not faces of the part:
plane.top is the XY mid-plane at z=0, NOT the top face. Primitives are centred on the origin,
so plane.top cuts an unplaced box through its middle and a boss sketched there extrudes into
the part. To sketch ON a face use refs.plane = {"of":"face","side":"top"} (2D origin = that
face's centre; `offset` lifts along the face's outward normal).

  name          plane   normal   sketch 2D axes (u, v)   aliases
  plane.top     XY      +Z       u=+X, v=+Y              "XY"
  plane.front   XZ      -Y       u=+X, v=+Z              "XZ"
  plane.right   YZ      +X       u=+Y, v=+Z              "YZ"

A 2D sketch point (a, b) on plane.front is world (a, 0, b); on plane.right it is (0, a, b).

SKETCH `offset` (mm) moves the sketch plane ALONG ITS NORMAL:
  plane.top   offset:+20 -> z=+20
  plane.front offset:+20 -> y=-20   (the front normal is -Y, so +offset goes toward -Y)
  plane.right offset:+20 -> x=+20
  To sketch at world y=+1385 on plane.front use offset:-1385.
A blind EXTRUDE (boss or cut) runs along the sketch normal: a plane.front sketch extrudes
toward -Y; params.flip:true reverses it. For a simple blind cut with no explicit direction or
flip, the engine tries the opposite direction only when the first removes no material.

Geometry-query side words use the same frame: top=+Z, bottom=-Z, front=-Y, back=+Y,
right=+X, left=-X.

Datum refs elsewhere: {"kind":"datum","feature":"plane.right"} (mirror/split planes),
axes "axis.x"/"axis.y"/"axis.z", point "point.origin". When an ASSEMBLY or motion tool takes a
direction and you give a plane name, the engine uses +Y for "plane.front" there; pass an
explicit vector such as [0,-1,0] whenever the sign matters.
