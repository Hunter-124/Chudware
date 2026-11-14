TRANSFORM - move and/or rotate an existing BODY (type "transform"; any solid, not only primitives).

params:
  translate  [dx,dy,dz] mm.
  angle      degrees (number or dim object). Rotation axis: rotate:[ax,ay,az] (or axis:[...]),
             or the object form rotate:{"axis":[ax,ay,az],"angle":deg}. Default axis +Z. The
             axis is a direction (any length; zero length is refused).
  pivot      [x,y,z] point the rotation turns about (default the world origin).
  copy       true keeps the original and adds the moved duplicate as a new body; default
             false moves the body in place.
  body       WHICH body - see below. Default: the current (most recently built live) body.
Order: rotate about `pivot` first, then translate. A move with no translate and no angle is
a no-op that succeeds with a warning.

BODY SELECTOR - one spelling family, all equivalent (feature level or params.body):
  "body":"f3"                                     the body built by feature f3 (ids from get_part)
  "body":{"kind":"body","owner_feature":"f3"}     the same, as a body selector
  "body":"$1"                                     an op EARLIER IN THE SAME add_features call
NOT accepted: refs.body (transform has no ref slots -> "transform takes no refs"); body ids
"b1".."bN" from list_world_bodies/check_interference/set_material (-> "ref 'body' -> 'b1' not
found"); "$N" from a previous call (each call numbers its ops from $0); bare integers.
After an in-place transform, the original id still works: a later transform of "f3" follows
it to the moved body and composes on it. "consumed by a later feature" means a boss/boolean/
pattern fused that body into another one; target the feature that produced the fused body
(the later feature in get_part(summary=True) that merged it), or rebuild the operands with
merge:false if they must stay separate.

Examples:
  {"op":"add_feature","feature":{"type":"transform","name":"PlaceWing","params":{"translate":[120,0,8]},"body":"f4"}}
  {"op":"add_feature","feature":{"type":"transform","name":"Yaw30","params":{"angle":{"value":30,"expr":null},"rotate":[0,0,1],"pivot":[0,0,0]},"body":"f4"}}
  {"op":"add_feature","feature":{"type":"transform","name":"Copy50","params":{"translate":[50,0,0],"copy":true},"body":"f2"}}

ASSEMBLIES: in an assembly document a transform with body:"<component id>" (e.g. "k3")
composes a measured world-space move with that component's placement; mate-owned poses
refuse. To set an absolute component pose use asm_add_component(at, rotate, angle) or the
asm_edit_component op: cad_guide("asm_edit_component").
