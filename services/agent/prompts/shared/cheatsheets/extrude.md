EXTRUDE a sketch region into a solid. Reference the sketch's region by selector.

Op shape: one `add_feature` of type `extrude`. params: `mode` ("boss" add | "cut" remove), `end` ("blind"|"through_all"|"mid_plane"), `depth` (dim object, blind). refs.profile = a region selector on the sketch.

A dim is `{"value": <num>, "expr": null}`. A region selector: {"kind":"region","sketch":"<sketch feature id>","centroid":[x,y],"hint_index":0}.

Example A - extrude sketch f1's region 10mm (new boss):
[
 {"op":"add_feature","feature":{"type":"extrude","name":"Extrude1","params":{"mode":"boss","end":"blind","depth":{"value":10,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"f1","centroid":[0,0],"hint_index":0}]}}}
]

Example B - cut a pocket 5mm deep through the existing body:
[
 {"op":"add_feature","feature":{"type":"extrude","name":"Pocket1","params":{"mode":"cut","end":"blind","depth":{"value":5,"expr":null}},"refs":{"profile":[{"kind":"region","sketch":"f2","centroid":[0,0],"hint_index":0}]}}}
]

3 common mistakes:
1. `sketch` in the region selector must be the sketch FEATURE ID (e.g. "f1" or "$0" if in same array), not its name.
2. `depth` is a dim OBJECT {"value":n,"expr":null}, not a bare number.
3. Use mode "cut" to remove material; "boss" only adds. A cut that doesn't overlap the body removes nothing.
