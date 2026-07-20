# Reverse-engineering context pack
# Tools: reverse_engineer, compare_parts, convert_face_to_sketch, project_into_sketch,
#        measure_from_photo, start_capture_session, submit_part_photos, import_part

## Sequence
1. Capture/import first: STEP/mesh import_part, or photo session (start_capture_session → submit_part_photos).
2. reverse_engineer → parametric feature tree; do not freehand over a mesh.
3. compare_parts loop until deviation acceptable; measure_from_photo / datasheet dims for critical features.
4. convert_face_to_sketch / project_into_sketch when a face must become an editable profile.
5. verify_battery before done; ledger source artifact + deviation summary.
