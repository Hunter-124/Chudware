PROFILE REGIONS - which part of a sketch an extrude/revolve/loft/sweep/rib uses (refs.profile).

A region selector is {"kind":"region","sketch":"$0"|"fN", ...} with OPTIONAL targeting:
  (nothing)          the WHOLE material profile: every closed material area, holes already
                     nested (two concentric circles = an annulus). This is the default and
                     the right choice for tubes, washers, plates with holes.
  hint_index:0       same as nothing (the whole material profile).
  hint_index:k (k>0) the k-th CONNECTED material region (overlapping/touching loops are one
                     region; an out-of-range k is refused with the region count).
  centroid:[u,v]     pick ONE closed-loop region by its 2D area centroid, in sketch (u,v)
                     coordinates (cad_guide("planes") for each plane's u/v axes).
  index:i            pick ONE closed-loop region by position in the sketch-region list
                     (sorted by centroid u, then v, then smaller area first).

CENTROID SEMANTICS (read before using it):
  - The candidate list holds EVERY closed loop's region, the void inside a loop included
    (the inner disc of a washer is a region of its own).
  - The region whose centroid is nearest [u,v] wins. Concentric regions share a centroid;
    then the LARGER-AREA region wins.
  - So on a thin-walled tube sketch (OD 50 / ID 40) centroid:[0,0] selects the INNER DISC
    (area 1257 mm^2) over the wall (707 mm^2): the extrude builds a solid core, not a tube.
    Omit centroid for a tube - the untargeted profile already gives the annulus.
  - A centroid outside the region nearest to it, a tie between regions at different
    centroids, or two picks that land on the same region are refused.
  Use centroid only to choose between SEPARATE (non-nested) regions, and check volume_mm3 in
  the reply against the area you meant x depth.

Example - extrude only the right-hand of two separate 10x10 squares centred at u=-20 and u=+20:
  "refs":{"profile":[{"kind":"region","sketch":"$0","centroid":[20,0]}]}
