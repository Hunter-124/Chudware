// AI-CAD worker job launcher: spawns build/design/chat jobs on a background thread and streams
// agent progress events back to the main thread.
#pragma once

#include <string>

class WorkerClient;
struct JobState;

// Spawn a worker job on a background thread. Design/chat jobs stream live iteration events
// into job.events (drained on the main thread); `reset` starts a fresh agent session and
// `plan` lets the agent draft a build plan before writing code (plan mode).
void launchJob(WorkerClient& w, JobState& job, int kind, std::string payload, bool reset,
               bool plan);
