// AI-CAD worker job launcher: spawns design/mission jobs on a background thread and streams
// agent progress events back to the main thread.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

class WorkerClient;
struct JobState;

// --- C22b: launch agent_run_v2 on a background thread ---
// Streams orchestrator events (run_started / stage_started / agent_call / agent_output /
// stage_result / run_finished) through job.events with kind prefixed "orch_" so the
// main-thread drain can route them to the ActivityPanel separately from legacy events.
// `cam` (set when the copilot is launched from the Manufacture workspace) routes generic
// "make a program"-style requests to the core's CAM fast-path on the current part.
// `extra` (O1/M23 + mode overhaul): additional top-level params merged into the RPC payload —
// the GUI threads the model picker's provider/model/reasoning_effort override, composer
// attachments, and an `options` object (mode/plan_only/approved_plan/execute_as/
// corporate_force/scope/advisor) which is deep-merged under params["options"]. Empty object
// = no extra fields.
void launchAgentRunV2(WorkerClient& w, JobState& job,
                      const std::string& request, const std::string& docId, bool cam = false,
                      const nlohmann::json& extra = nlohmann::json::object());

// --- Mission orchestrator launcher ---
// Runs agent_mission_v1 on a background thread: the core decomposes the request into an ordered
// capability chain (design/assembly -> CFD -> verdict), threads the working doc forward, and
// returns {..., verdict, tasks, created_doc?, asm_path?}. Streams the same "orch_"-prefixed events
// as launchAgentRunV2 (plus mission_* kinds) and stashes the verdict into job.missionVerdict.
void launchMissionRun(WorkerClient& w, JobState& job,
                      const std::string& request, const std::string& docId,
                      const nlohmann::json& extra = nlohmann::json::object());
