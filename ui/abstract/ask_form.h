#pragma once
// Transport-neutral model for the interaction contract's ask_user batches
// (contracts/interaction.md §3/§4). Any transport (ui/imgui in-app dialog,
// CLI, MCP elicitation) parses a batch into an AskForm and turns collected
// raw widget values back into a contract-conformant answers envelope.
// Transports are dumb (contract §8/§10): normalization/canonical/provenance
// live in the core (services/agent/interaction.py), not here.

#include <map>
#include <string>
#include <vector>

namespace aicad::ui {

enum class AskFieldKind { Choice, Dimension, Text, FileRequest, Approval };

struct AskField {
    std::string id;
    std::string prompt;
    std::string why;
    AskFieldKind kind = AskFieldKind::Text;
    std::string suggestedJson;         // raw JSON text of "suggested" (same shape as the answer value)
    std::vector<std::string> options;  // choice: option values (label collapsed to value if absent)
    std::string family = "length";     // dimension: "length" | "angle" | "count"
    bool required = false;
};

struct AskForm {
    std::string batchId;
    std::string scope;
    bool blocking = true;
    std::vector<AskField> fields;
};

// Parse a §3 batch envelope JSON string into a transport-neutral AskForm.
// Tolerant per contract §10: unknown question kinds are skipped, malformed
// JSON yields an empty AskForm (batchId empty, fields empty).
AskForm parseAskBatch(const std::string& batchJson);

// Build a §4 answers envelope from collected raw widget strings, one per field id.
// rawValues holes are skipped (unanswered fields don't appear in the envelope).
// Dimension raw values are "<num> <unit>" (e.g. "0.5 in"); the `canonical` field is
// NOT computed here (the Python core normalizes, contract §8/§10) -- only `value`.
std::string buildAnswersJson(const AskForm& form,
                              const std::map<std::string, std::string>& rawValues);

} // namespace aicad::ui
