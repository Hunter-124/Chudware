# Reverse-engineering context pack
# Tools: reverse_engineer, compare_parts, reveng_status, reveng_start_session,
#  reveng_add_images, reveng_ingest_datasheet, reveng_reconstruct, reveng_set_scale,
#  reveng_fit_primitives, reveng_build_parametric, get_reveng_deviation, reveng_attach_result
LANE FIRST: file or doc_id (STEP/STL/OBJ) -> A; physical part + camera -> B.
A: reverse_engineer -> compare_parts loop per residual -> verify_battery; no mesh freehand.
B: reveng_status FIRST - backends absent in stock installs (backend_not_provisioned).
reveng_start_session -> reveng_print_scale_mat (print 100%, ArUco mat in frame) ->
reveng_add_images/_add_video/_ingest_datasheet -> reveng_reconstruct -> reveng_set_scale ->
[SCALE-CONFIRM GATE] -> reveng_fit_primitives -> list_reveng_dims/confirm_dimensions ->
[DIM-CONFIRM GATE] -> reveng_build_parametric -> get_reveng_deviation -> reveng_attach_result.
approximate (single-photo) = preview only, never a dimension. Tiers 2%/5%/10% hi/med/low.
