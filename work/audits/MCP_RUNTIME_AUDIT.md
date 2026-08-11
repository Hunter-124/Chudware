# MCP Runtime Audit

**Task:** T-V2  **Agent:** sonnet-vega  **Date:** 2026-07-02

**Library:** `/home/hunter/Repositories/CAD/core/build-linux/bin/libchudware.so`
**Total registered tools:** 184

## Summary

| Verdict | Count |
| ------- | ----- |
| WORKS | 96 |
| DEGRADED | 10 |
| DEAD | 0 |
| INCONCLUSIVE | 71 |
| SKIPPED (side-effect) | 7 |
| **Total** | **184** |

## Family Import Status

| Family | Import | Tools |
| ------ | ------ | ----- |
| cad | OK | 52 |
| gsl | OK | 10 |
| recon | OK | 3 |
| intake | OK | 10 |
| interaction | OK | 1 |
| ecad | OK | 24 |
| cam | OK | 21 |
| doc_diff | OK | 1 |
| req | OK | 4 |
| verify | OK | 2 |
| research | OK | 8 |
| deliverables | OK | 5 |
| sourcing | OK | 2 |
| cost | OK | 2 |
| spec | OK | 3 |
| piping | OK | 7 |
| hydraulic | OK | 6 |
| photo_recon | OK | 2 |
| firmware | OK | 8 |
| structural | OK | 3 |
| parts | OK | 3 |
| serving | OK | 5 |
| regen | OK | 1 |
| code_compliance | OK | 1 |

## DEGRADED (10 tools)

| Tool | Family | Detail |
| ---- | ------ | ------ |
| `code_check` | code_compliance | ([textcontent(type='text', text='{\n  "ok": true,\n  "jurisdiction": "ca",\n  "jurisdiction_meta": {\n    "ibc_edition": |
| `describe_toolpaths` | gsl | ([textcontent(type='text', text='{\n  "ok": true,\n  "text": "cam job \\"cam job\\" \\u2014 0 setup(s), 0 operation(s) t |
| `import_part` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "file not found: /nonexistent/probe.step"\n}', annotation |
| `part_get` | parts | ([textcontent(type='text', text='{"ok": false, "reason": "part_id not found: \'_audit_\'", "hint": "use part_search to d |
| `part_place` | parts | ([textcontent(type='text', text='{"ok": false, "domain": "unknown", "doc_id": null, "entity_id": null, "editable": false |
| `req_report` | req | ([textcontent(type='text', text='{"ok": false, "error": "requirements doc not found. use req_create to start one."}', an |
| `req_update` | req | ([textcontent(type='text', text='{"ok": false, "error": "requirements doc not found. call req_create first."}', annotati |
| `run_cfd_accurate` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "openfoam is not available on this machine. provision it  |
| `struct_analyze` | structural | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "[errno 2] no such file or directory: \'/nonexistent/_aud |
| `struct_check` | structural | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "[errno 2] no such file or directory: \'/nonexistent/_aud |

## INCONCLUSIVE (78 tools)

| Tool | Family | Detail |
| ---- | ------ | ------ |
| `add_datum` | cad | arg validation: Error executing tool add_datum: 4 validation errors for add_datumArguments
view_id
  Field required [typ |
| `add_detail_view` | cad | arg validation: Error executing tool add_detail_view: 4 validation errors for add_detail_viewArguments
parent_view_id
   |
| `add_dimension` | cad | arg validation: Error executing tool add_dimension: 3 validation errors for add_dimensionArguments
view_id
  Field requi |
| `add_gdt` | cad | arg validation: Error executing tool add_gdt: 4 validation errors for add_gdtArguments
view_id
  Field required [type=mi |
| `add_surface_finish` | cad | arg validation: Error executing tool add_surface_finish: 4 validation errors for add_surface_finishArguments
view_id
  F |
| `ask_user` | interaction | arg validation: Error executing tool ask_user: 1 validation error for ask_userArguments
questions
  Field required [type |
| `bom_price` | sourcing | arg validation: Error executing tool bom_price: 1 validation error for bom_priceArguments
doc_path
  Field required [typ |
| `bom_rollup` | sourcing | arg validation: Error executing tool bom_rollup: 1 validation error for bom_rollupArguments
doc_path
  Field required [t |
| `cam_add_operation` | cam | arg validation: Error executing tool cam_add_operation: 1 validation error for cam_add_operationArguments
tool_number
   |
| `cam_add_tool` | cam | arg validation: Error executing tool cam_add_tool: 1 validation error for cam_add_toolArguments
number
  Field required  |
| `cam_cost_estimate` | cost | arg validation: Error executing tool cam_cost_estimate: 1 validation error for cam_cost_estimateArguments
doc_path
  Fie |
| `cam_remove_operation` | cam | arg validation: Error executing tool cam_remove_operation: 1 validation error for cam_remove_operationArguments
op_id
   |
| `cam_save` | cam | arg validation: Error executing tool cam_save: 1 validation error for cam_saveArguments
path
  Field required [type=miss |
| `cam_set_stock` | cam | arg validation: Error executing tool cam_set_stock: 2 validation errors for cam_set_stockArguments
setup_id
  Field requ |
| `cam_update_operation` | cam | arg validation: Error executing tool cam_update_operation: 1 validation error for cam_update_operationArguments
op_id
   |
| `check_sketch` | cad | arg validation: Error executing tool check_sketch: 1 validation error for check_sketchArguments
sketch
  Field required  |
| `confirm_dimensions` | intake | arg validation: Error executing tool confirm_dimensions: 1 validation error for confirm_dimensionsArguments
confirmation |
| `contact_sheet` | cad | unexpected ToolError: Error executing tool contact_sheet: No current part. Call new_part(...) first (or pass doc_id=). |
| `describe_sketch` | gsl | arg validation: Error executing tool describe_sketch: 1 validation error for describe_sketchArguments
sketch
  Field req |
| `doc_diff_tool` | doc_diff | arg validation: Error executing tool doc_diff_tool: 2 validation errors for doc_diff_toolArguments
rev_a
  Field require |
| `ecad_design` | ecad | arg validation: Error executing tool ecad_design: 1 validation error for ecad_designArguments
request
  Field required [ |
| `ecad_export` | ecad | arg validation: Error executing tool ecad_export: 1 validation error for ecad_exportArguments
kind
  Field required [typ |
| `ecad_label` | ecad | arg validation: Error executing tool ecad_label: 1 validation error for ecad_labelArguments
text
  Field required [type= |
| `ecad_place_symbol` | ecad | arg validation: Error executing tool ecad_place_symbol: 1 validation error for ecad_place_symbolArguments
libid
  Field  |
| `ecad_sweep` | ecad | arg validation: Error executing tool ecad_sweep: 1 validation error for ecad_sweepArguments
param
  Field required [type |
| `ecad_tf` | ecad | arg validation: Error executing tool ecad_tf: 2 validation errors for ecad_tfArguments
output
  Field required [type=mis |
| `ecad_wire` | ecad | arg validation: Error executing tool ecad_wire: 4 validation errors for ecad_wireArguments
from_ref
  Field required [ty |
| `fw_build` | firmware | arg validation: Error executing tool fw_build: 1 validation error for fw_buildArguments
project_id
  Field required [typ |
| `fw_debug_halt` | firmware | skipped — side-effecty (starts process/port/GUI) |
| `fw_debug_read_mem` | firmware | skipped — side-effecty (starts process/port/GUI) |
| `fw_debug_start` | firmware | skipped — side-effecty (starts process/port/GUI) |
| `fw_flash` | firmware | skipped — side-effecty (starts process/port/GUI) |
| `fw_sim_run` | firmware | arg validation: Error executing tool fw_sim_run: 1 validation error for fw_sim_runArguments
project_id
  Field required  |
| `fw_sources` | firmware | arg validation: Error executing tool fw_sources: 1 validation error for fw_sourcesArguments
project_id
  Field required  |
| `hover` | gsl | arg validation: Error executing tool hover: 1 validation error for hoverArguments
sid
  Field required [type=missing, in |
| `hydraulic_add_hose` | hydraulic | arg validation: Error executing tool hydraulic_add_hose: 2 validation errors for hydraulic_add_hoseArguments
net_name
   |
| `hydraulic_size_cylinder` | hydraulic | arg validation: Error executing tool hydraulic_size_cylinder: 1 validation error for hydraulic_size_cylinderArguments
pr |
| `make_fit_coupon` | intake | arg validation: Error executing tool make_fit_coupon: 1 validation error for make_fit_couponArguments
interface_key
  Fi |
| `make_presentation` | deliverables | arg validation: Error executing tool make_presentation: 1 validation error for make_presentationArguments
spec
  Field r |
| `make_report` | deliverables | arg validation: Error executing tool make_report: 1 validation error for make_reportArguments
spec
  Field required [typ |
| `make_standard_part` | cad | arg validation: Error executing tool make_standard_part: 2 validation errors for make_standard_partArguments
family
  Fi |
| `make_workbook` | deliverables | arg validation: Error executing tool make_workbook: 1 validation error for make_workbookArguments
spec
  Field required  |
| `match_part_number` | spec | arg validation: Error executing tool match_part_number: 1 validation error for match_part_numberArguments
mpn
  Field re |
| `measure_between` | cad | arg validation: Error executing tool measure_between: 1 validation error for measure_betweenArguments
a_query
  Field re |
| `measure_from_photo` | intake | arg validation: Error executing tool measure_from_photo: 5 validation errors for measure_from_photoArguments
photo_id
   |
| `piping_cfd_handoff` | piping | arg validation: Error executing tool piping_cfd_handoff: 2 validation errors for piping_cfd_handoffArguments
doc
  Field |
| `piping_fluid` | piping | arg validation: Error executing tool piping_fluid: 1 validation error for piping_fluidArguments
name
  Field required [t |
| `piping_internal_volume` | piping | arg validation: Error executing tool piping_internal_volume: 1 validation error for piping_internal_volumeArguments
doc
 |
| `piping_operating_point` | piping | arg validation: Error executing tool piping_operating_point: 1 validation error for piping_operating_pointArguments
pump |
| `piping_solve` | piping | arg validation: Error executing tool piping_solve: 2 validation errors for piping_solveArguments
boundary.str
  Input sh |
| `piping_verify_cfd` | piping | arg validation: Error executing tool piping_verify_cfd: 2 validation errors for piping_verify_cfdArguments
solve_result
 |
| `query_geometry` | cad | arg validation: Error executing tool query_geometry: 1 validation error for query_geometryArguments
query
  Field requir |
| `regen_diagnose` | regen | arg validation: Error executing tool regen_diagnose: 1 validation error for regen_diagnoseArguments
document_json
  Fiel |
| `req_status` | req | arg validation: Error executing tool req_status: 2 validation errors for req_statusArguments
requirement_id
  Field requ |
| `request_part_photos` | intake | arg validation: Error executing tool request_part_photos: 2 validation errors for request_part_photosArguments
label
  F |
| `request_quote` | cost | arg validation: Error executing tool request_quote: 2 validation errors for request_quoteArguments
doc_path
  Field requ |
| `research_datasheet_fetch` | research | arg validation: Error executing tool research_datasheet_fetch: 1 validation error for research_datasheet_fetchArguments
 |
| `research_standard_section` | research | arg validation: Error executing tool research_standard_section: 1 validation error for research_standard_sectionArgument |
| `research_thread_spec` | research | arg validation: Error executing tool research_thread_spec: 1 validation error for research_thread_specArguments
designat |
| `research_unit_convert` | research | arg validation: Error executing tool research_unit_convert: 1 validation error for research_unit_convertArguments
unit
  |
| `resolve_process` | intake | arg validation: Error executing tool resolve_process: 1 validation error for resolve_processArguments
request
  Field re |
| `reverse_engineer` | recon | arg validation: Error executing tool reverse_engineer: 1 validation error for reverse_engineerArguments
source
  Field r |
| `review_drawing` | cad | unexpected ToolError: Error executing tool review_drawing: Drawing render failed: not a drawing document |
| `run_fea_modal` | cad | arg validation: Error executing tool run_fea_modal: 1 validation error for run_fea_modalArguments
fixtures
  Field requi |
| `run_fea_static` | cad | arg validation: Error executing tool run_fea_static: 2 validation errors for run_fea_staticArguments
fixtures
  Field re |
| `serving_evaluate` | serving | arg validation: Error executing tool serving_evaluate: 2 validation errors for serving_evaluateArguments
insertion_point |
| `serving_promote` | serving | arg validation: Error executing tool serving_promote: 2 validation errors for serving_promoteArguments
insertion_point
  |
| `serving_rollback` | serving | arg validation: Error executing tool serving_rollback: 1 validation error for serving_rollbackArguments
insertion_point
 |
| `serving_set_canary` | serving | arg validation: Error executing tool serving_set_canary: 1 validation error for serving_set_canaryArguments
insertion_po |
| `set_config_override` | cad | arg validation: Error executing tool set_config_override: 2 validation errors for set_config_overrideArguments
config
   |
| `set_config_suppression` | cad | arg validation: Error executing tool set_config_suppression: 2 validation errors for set_config_suppressionArguments
con |
| `set_titleblock` | cad | arg validation: Error executing tool set_titleblock: 1 validation error for set_titleblockArguments
fields
  Field requi |
| `set_view` | cad | arg validation: Error executing tool set_view: 1 validation error for set_viewArguments
view_id
  Field required [type=m |
| `show_in_app` | cad | skipped — side-effecty (starts process/port/GUI) |
| `spec_sheet_match` | spec | arg validation: Error executing tool spec_sheet_match: 3 validation errors for spec_sheet_matchArguments
text_or_url
  F |
| `start_capture_session` | photo_recon | skipped — side-effecty (starts process/port/GUI) |
| `stop_capture_session` | photo_recon | skipped — side-effecty (starts process/port/GUI) |
| `struct_estimate` | structural | arg validation: Error executing tool struct_estimate: 1 validation error for struct_estimateArguments
doc_path
  Field r |

## WORKS (96 tools)

| Tool | Family | Detail |
| ---- | ------ | ------ |
| `activate_configuration` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `add_configuration` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `add_drawing_view` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `add_features` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `add_section_view` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `cad_guide` | cad | ([textcontent(type='text', text='ai-cad modeling guide (c++ parametric engine — same as the desktop app).\n\nworkflow\n  |
| `cam_add_setup` | cam | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "valueerror: no cam job. call cam_new_job(...) first."\n} |
| `cam_extract` | cam | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "valueerror: no current part and no path=. build/open a p |
| `cam_generate` | cam | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "valueerror: no cam job. call cam_new_job(...) first."\n} |
| `cam_job_tree` | cam | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "valueerror: no cam job. call cam_new_job(...) first."\n} |
| `cam_materials` | cam | ([textcontent(type='text', text='{\n  "materials": [\n    {\n      "cut_feed_ref": 0.0,\n      "density_g_cm3": 2.7,\n   |
| `cam_new_job` | cam | ([textcontent(type='text', text='{\n  "doc_id": "cam1",\n  "revision": 1,\n  "tree": {\n    "id": "cam1",\n    "name": " |
| `cam_op_types` | cam | ([textcontent(type='text', text='{\n  "op_types": [\n    {\n      "id": "face"\n    },\n    {\n      "id": "contour2d"\n |
| `cam_open` | cam | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "cannot open .cam+ file: /nonexistent/_audit_.cam",\n  "d |
| `cam_post` | cam | ([textcontent(type='text', text='{\n  "error": "",\n  "est_time_s": 0.0,\n  "gcode": "",\n  "line_count": 0,\n  "ok": tr |
| `cam_post_dialects` | cam | ([textcontent(type='text', text='{\n  "dialects": [\n    {\n      "canned_drill": false,\n      "description": "grbl 1.1 |
| `cam_redo` | cam | ([textcontent(type='text', text='{\n  "noop": true,\n  "revision": 1,\n  "tree": {\n    "id": "cam1",\n    "name": "cam  |
| `cam_setup_sheet` | cam | ([textcontent(type='text', text='{\n  "html": "<html><head><meta charset=\'utf-8\'><style>body{font-family:sans-serif;ma |
| `cam_simulate` | cam | ([textcontent(type='text', text='{\n  "results": []\n}', annotations=none, meta=none)], {'result': '{\n  "results": []\n |
| `cam_tool_types` | cam | ([textcontent(type='text', text='{\n  "tool_types": [\n    {\n      "class": "endmill",\n      "id": "endmill_flat"\n    |
| `cam_undo` | cam | ([textcontent(type='text', text='{\n  "noop": true,\n  "revision": 1,\n  "tree": {\n    "id": "cam1",\n    "name": "cam  |
| `check_feature_overlap` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `check_interference` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `check_metadata` | verify | ([textcontent(type='text', text='[{"name": "cam_simulate", "cost": "seconds", "platform": "any", "reward_shape": "scalar |
| `check_symmetry` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `close_part` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `compare_parts` | recon | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no such document: _a_",\n  "code": "unknown_doc",\n  "de |
| `convert_face_to_sketch` | recon | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `describe_assembly` | gsl | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `describe_board` | gsl | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no active ecad document: no open ecad document \'\' \\u2 |
| `describe_part` | gsl | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `describe_project` | gsl | ([textcontent(type='text', text='{\n  "ok": true,\n  "text": "no dag run data supplied \\u2014 pass run=<dag_run_dict> f |
| `describe_results` | gsl | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `describe_scene` | gsl | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `dfm_check` | intake | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `draft_analysis` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `ecad_add_power` | ecad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no active schematic \\u2014 call ecad_new_schematic(name |
| `ecad_add_zone` | ecad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no active board \\u2014 call ecad_new_pcb(name) first",\ |
| `ecad_autoroute` | ecad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no active board \\u2014 call ecad_new_pcb(name) first",\ |
| `ecad_check_diff_impedance` | ecad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no active board \\u2014 call ecad_new_pcb(name) first",\ |
| `ecad_check_impedance` | ecad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no active board \\u2014 call ecad_new_pcb(name) first",\ |
| `ecad_fill_zones` | ecad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no active board \\u2014 call ecad_new_pcb(name) first",\ |
| `ecad_find_part` | ecad | ([textcontent(type='text', text='{\n  "ok": true,\n  "query": "100nf cap",\n  "count": 6,\n  "hits": [\n    {\n      "ki |
| `ecad_make_board` | ecad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no active schematic -- build one first",\n  "warnings":  |
| `ecad_new_pcb` | ecad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "c7cc75803709",\n  "kind": "pcb",\n  "name": "_audit_pcb_ |
| `ecad_new_schematic` | ecad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "becba7cad97c",\n  "kind": "sch",\n  "name": "_audit_sch_ |
| `ecad_run_dfm` | ecad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "c7cc75803709",\n  "revision": 0,\n  "errors": 0,\n  "war |
| `ecad_run_drc` | ecad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "c7cc75803709",\n  "revision": 0,\n  "errors": 0,\n  "war |
| `ecad_run_erc` | ecad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "becba7cad97c",\n  "revision": 0,\n  "errors": 0,\n  "war |
| `ecad_simulate` | ecad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "nothing to simulate \\u2014 the netlist has no simulatab |
| `ecad_status` | ecad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "becba7cad97c",\n  "kind": "sch",\n  "name": "_audit_sch_ |
| `ecad_to_3d` | ecad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "3d export failed: boardgeometryerror: board has no edge. |
| `ecad_update_from_sch` | ecad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "c7cc75803709",\n  "sch_doc_id": "becba7cad97c",\n  "kind |
| `export_drawing` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `export_part` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `fit_report` | intake | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `fw_new_project` | firmware | ([textcontent(type='text', text='{\n  "code": "invalid_request",\n  "message": "new_project: no `mcu` given and none der |
| `get_job` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "unknown job_id \'_audit_none_\'"\n}', annotations=none,  |
| `get_part` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `hydraulic_add_component` | hydraulic | ([textcontent(type='text', text='{\n  "ok": false,\n  "reason": "no active hydraulic circuit",\n  "hint": "call hydrauli |
| `hydraulic_bom` | hydraulic | ([textcontent(type='text', text='{\n  "ok": false,\n  "reason": "no active hydraulic circuit",\n  "hint": "call hydrauli |
| `hydraulic_new` | hydraulic | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc": {\n    "schema": "aicad.hydraulic/1",\n    "id": "6d5468b5-d |
| `hydraulic_size_pump` | hydraulic | ([textcontent(type='text', text='{\n  "ok": true,\n  "flow_lpm": 10.0,\n  "pressure_bar": 100.0,\n  "efficiency": 0.85,\ |
| `inspect_section` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `intake_status` | intake | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `list_artifacts` | deliverables | {
  "ok": true,
  "count": 0,
  "artifacts": []
} |
| `list_configurations` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `list_parts` | cad | ([textcontent(type='text', text='{\n  "docs": []\n}', annotations=none, meta=none)], {'result': '{\n  "docs": []\n}'}) |
| `list_spec_templates` | spec | ([textcontent(type='text', text='{\n  "ok": true,\n  "templates": [\n    {\n      "name": "capacitor_radial",\n      "re |
| `list_standard_parts` | cad | ([textcontent(type='text', text='{\n  "families": [\n    {\n      "category": "screws",\n      "description": "cylindric |
| `list_world_bodies` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `make_drawing` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `measure_part` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "no current part. call new_part(...) first (or pass doc_i |
| `new_part` | cad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "doc-cpp00000001",\n  "name": "_audit_probe_",\n  "revisi |
| `part_search` | parts | ([textcontent(type='text', text='{"ok": true, "count": 20, "has_live": false, "results": [{"part_id": "fastener:iso 4017 |
| `piping_report` | piping | ([textcontent(type='text', text='{\n  "ok": false,\n  "reason": "no prior piping_solve to report on",\n  "hint": "call p |
| `query_adjacency` | gsl | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "document has no bodies to adjacency",\n  "code": "invali |
| `recover_parts` | cad | ([textcontent(type='text', text='{\n  "ok": true,\n  "recovered": [\n    {\n      "path": "/home/hunter/.local/share/ai- |
| `redo` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "nothing to redo",\n  "code": "invalid_op",\n  "detail":  |
| `render_document` | deliverables | {
  "ok": false,
  "reason": "artifact_not_found",
  "hint": "no file at /nonexistent/probe.pdf"
} |
| `render_part` | cad | type='image' data='ivborw0kggoaaaansuheugaaagaaaagqcaiaaabxle9zaacmmuleqvr42u292zod5zgo/v/epu7banttm9majebci5rnez6fzqebi |
| `req_create` | req | ([textcontent(type='text', text='{\n  "ok": true,\n  "req_id": "1a15b012-f962-416b-95ec-ac687163dbf6",\n  "location": "( |
| `research_eng_calc` | research | ([textcontent(type='text', text='{\n  "ok": false,\n  "reason": "not_found",\n  "hint": "unknown formula \'f = m * a\';  |
| `research_fetch_page` | research | ([textcontent(type='text', text='{\n  "ok": true,\n  "answer": "example domain \\u2014 from cache (131 chars)",\n  "unit |
| `research_material_lookup` | research | ([textcontent(type='text', text='{\n  "ok": true,\n  "answer": "aluminium 6061-t6 (metal): e_mpa=69000, yield_mpa=276, u |
| `research_web_search` | research | ([textcontent(type='text', text='{\n  "ok": true,\n  "answer": null,\n  "units": null,\n  "source_url": "https://www.eng |
| `run_cfd_flow` | cad | ([textcontent(type='text', text='{\n  "ok": true,\n  "job_id": "job-d9ccb5b215e1",\n  "status": "running",\n  "auto_back |
| `save_part` | cad | ([textcontent(type='text', text='{\n  "ok": true,\n  "format": "step+",\n  "native": true,\n  "doc_id": "doc-cpp0000001d |
| `serving_status` | serving | ([textcontent(type='text', text='{\n  "ok": true,\n  "routes": []\n}', annotations=none, meta=none)], {'result': '{\n  " |
| `set_material` | cad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "doc-cpp0000001d",\n  "bodies": [\n    "b1"\n  ],\n  "app |
| `set_process` | intake | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "unknown process \'cnc_milling\'. known processes: [\'fdm |
| `solve_mates` | cad | ([textcontent(type='text', text='{\n  "ok": false,\n  "error": "doc_solve_mates: document is not an assembly",\n  "code" |
| `submit_part_photos` | intake | ([textcontent(type='text', text='{\n  "ok": true,\n  "photos": [],\n  "measured_part": null,\n  "note": "photos register |
| `undo` | cad | ([textcontent(type='text', text='{\n  "datums": [],\n  "id_map": {},\n  "owner_map": {\n    "f1": {\n      "bodies": [\n |
| `verify_battery` | verify | ([textcontent(type='text', text='{"scores": {"check_sketch": 0.0, "dim_coverage": 0.0, "ecad_check_diff_impedance": 0.0, |
| `wall_thickness` | cad | ([textcontent(type='text', text='{\n  "ok": true,\n  "doc_id": "doc-cpp0000001d",\n  "summary": "thinnest wall = 99.93 m |

## Skipped (side-effect tools)

- `fw_debug_halt`: skipped — side-effecty (starts process/port/GUI)
- `fw_debug_read_mem`: skipped — side-effecty (starts process/port/GUI)
- `fw_debug_start`: skipped — side-effecty (starts process/port/GUI)
- `fw_flash`: skipped — side-effecty (starts process/port/GUI)
- `show_in_app`: skipped — side-effecty (starts process/port/GUI)
- `start_capture_session`: skipped — side-effecty (starts process/port/GUI)
- `stop_capture_session`: skipped — side-effecty (starts process/port/GUI)

## Declared-But-Not-Installed Packages

Packages in `requirements.txt` but absent from the running mcp-venv:

_(none — venv is fully provisioned)_

## Undeclared-But-Installed Dependencies

Packages imported by `services/`+`engine_oracle/`+`services/mcp/` that are
installed in the venv but absent from `requirements.txt`:

_(none found)_

## Test-Suite Pollution (Sweep Analysis)

_(See `scripts/mcp_runtime_audit.py --test-pollution` output below,
generated separately to avoid OOM on the same process.)_

### Root Causes Identified

See `## Test Pollution Analysis` section appended after running:
```
python scripts/mcp_runtime_audit.py --test-pollution
```

---
*Generated by `scripts/mcp_runtime_audit.py` (T-V2)*