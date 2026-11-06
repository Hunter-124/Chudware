// Composer spell check + word completion, the UI half: loads the word lists, squiggles
// misspelled words under the prompt text, draws the ghost completion at the caret (Tab takes
// it, see drawChatInput) and runs the right-click correction menu. Which words count, the
// suggestions and the completion pick are GL-free in spell_check.cpp.
#include "copilot_internal.h"
#include "ui/copilot/magic_keywords.h"   // maskNonProse: no completion inside code
#include "ui/copilot/spell_check.h"
#include "ui/copilot/text_wrap.h"

#include <climits>
#include <unordered_set>

namespace {

using ui::copilot::SpellDictionary;
using ui::copilot::SpellRange;

struct ComposerSpell {
    bool loaded = false;
    SpellDictionary dict;
    std::string userWordsPath;                  // "Add to dictionary" words, one per line
    std::unordered_set<std::string> ignored;    // "Ignore" (lowercase), this session only
    // Completion vocabulary: the active conversation's words.
    ui::copilot::WordCounts vocab;
    int vocabConv = INT_MIN;
    std::size_t vocabMsgs = SIZE_MAX;
    // Last checked text, as shown and with soft wraps flattened, and what it produced.
    std::string shown, flat, prose;             // prose = flat with code masked
    std::vector<SpellRange> misspelled;
    std::string ghost;                          // completion at the caret ("" = none)
    // Right-click menu target.
    SpellRange menuRange;
    std::string menuWord;
    std::vector<std::string> menuSuggestions;
};

ComposerSpell& spell() {
    static ComposerSpell s;
    return s;
}

std::string slurp(const std::string& path) {
    std::ifstream in(path, std::ios::binary);
    if (!in) return {};
    std::ostringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

std::string lowerAscii(std::string_view w) {
    std::string out(w);
    for (char& c : out)
        if (c >= 'A' && c <= 'Z') c = (char)(c - 'A' + 'a');
    return out;
}

bool isWordByte(char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
           c == '_' || c == '\'' || c == '-' || c == '/' || c == '\\' || c == '.' ||
           (unsigned char)c >= 0x80;
}

// Top-left of byte `p` of the displayed text, relative to the text origin.
ImVec2 textOffset(std::string_view shown, std::size_t p, float lineH) {
    const std::size_t nl = p ? shown.rfind('\n', p - 1) : std::string_view::npos;
    const std::size_t lineStart = nl == std::string_view::npos ? 0 : nl + 1;
    const float line = (float)std::count(shown.begin(), shown.begin() + (std::ptrdiff_t)lineStart, '\n');
    return ImVec2(ImGui::CalcTextSize(shown.data() + lineStart, shown.data() + p).x, line * lineH);
}

void squiggle(ImDrawList* dl, float x0, float x1, float y, ImU32 col) {
    const float amp = ui::metrics::px(1.25f);
    const float step = ui::metrics::px(2.0f);
    ImVec2 pts[256];
    int n = 0;
    for (float x = x0; x < x1 && n < 255; x += step, ++n)
        pts[n] = ImVec2(x, y + ((n & 1) ? amp : -amp));
    pts[n] = ImVec2(x1, y + ((n & 1) ? amp : -amp));
    dl->AddPolyline(pts, n + 1, col, 0, ui::metrics::stroke(1.0f));
}

void addUserWord(ComposerSpell& s, const std::string& word) {
    s.dict.addWord(word, SpellDictionary::kUserTier);
    std::error_code ec;
    std::filesystem::create_directories(std::filesystem::path(s.userWordsPath).parent_path(), ec);
    std::ofstream(s.userWordsPath, std::ios::app) << lowerAscii(word) << '\n';
}

}  // namespace

void composerSpellRefresh(AppState& st) {
    ComposerSpell& s = spell();
    if (!s.loaded) {
        s.loaded = true;
        s.dict.addWordList(slurp(st.assets + "/dict/en_US.txt"));
        s.dict.addWordList(slurp(st.assets + "/dict/engineering.txt"));
        if (s.dict.empty()) {
            std::fprintf(stderr, "[spell] word lists not found under %s/dict\n", st.assets.c_str());
        } else {
            s.userWordsPath = appdataAppDir() + "/spell_words.txt";
            s.dict.addWordList("#tier " + std::to_string(SpellDictionary::kUserTier) + "\n" +
                               slurp(s.userWordsPath));
        }
    }
    if (st.activeConv != s.vocabConv || st.chat.size() != s.vocabMsgs) {
        s.vocabConv = st.activeConv;
        s.vocabMsgs = st.chat.size();
        s.vocab.clear();
        for (const ChatMsg& m : st.chat) s.vocab.addText(m.text);
    }
}

const std::string& composerSpellGhost() { return spell().ghost; }

bool composerSpellPaint(ImGuiID wid, const std::vector<char>& buf, const std::vector<int>& hard,
                        bool allowGhost, ComposerSpellFix& fix) {
    ComposerSpell& s = spell();
    s.ghost.clear();
    if (s.dict.empty()) return false;

    ImGuiContext& g = *ImGui::GetCurrentContext();
    ImGuiWindow* host = g.CurrentWindow;
    if (!host || host->DC.ChildWindows.empty()) return false;
    // InputTextMultiline draws into its own child window: the host's newest child, carrying
    // the item's id (same lookup as chatPaintMagicKeywords).
    ImGuiWindow* child = host->DC.ChildWindows.back();
    if (!child || child->ChildId != ImGui::GetItemID() || child->Hidden) return false;

    const bool active = ImGui::IsItemActive();
    ImGuiInputTextState* ist = active ? ImGui::GetInputTextState(wid) : nullptr;
    const std::string_view shown = (ist && ist->TextA.Data)
        ? std::string_view(ist->TextA.Data, (size_t)ist->TextLen)
        : std::string_view(buf.data(), std::strlen(buf.data()));
    const int caret = ist ? ist->GetCursorPos() : -1;
    const bool selecting = ist && ist->HasSelection();

    // Re-check only when the text changed. Soft wraps flatten back to spaces so a wrapped
    // line never splits a token or fakes a fence line; the copy keeps the shown offsets.
    if (shown != s.shown) {
        s.shown.assign(shown.data(), shown.size());
        s.flat = s.shown;
        for (size_t i = 0; i < s.flat.size(); ++i)
            if (s.flat[i] == '\n' && !ui::copilot::isHardBreak(hard, (int)i)) s.flat[i] = ' ';
        s.prose = ui::copilot::mayHoldNonProse(s.flat) ? ui::copilot::maskNonProse(s.flat) : s.flat;
        s.misspelled.clear();
        for (const SpellRange& r : ui::copilot::spellCheckWords(s.flat)) {
            const std::string_view w(s.flat.data() + r.begin, r.end - r.begin);
            if (!s.dict.knows(w) && !s.ignored.count(lowerAscii(w))) s.misspelled.push_back(r);
        }
    }

    // Where InputTextEx placed the text: the child's content origin minus its scroll.
    const ImGuiStyle& style = ImGui::GetStyle();
    const float scrollX = ist ? ist->Scroll.x : 0.0f;
    const ImVec2 origin(IM_TRUNC(child->Pos.x + style.FramePadding.x - child->Scroll.x - scrollX),
                        IM_TRUNC(child->Pos.y + style.FramePadding.y - child->Scroll.y));
    const float lineH = ImGui::GetTextLineHeight();
    ImDrawList* dl = host->DrawList;
    const ImRect clip = child->InnerClipRect;
    dl->PushClipRect(clip.Min, clip.Max, true);

    // Squiggles. The word the caret is in is still being typed: leave it alone.
    const ImU32 errCol = ImGui::ColorConvertFloat4ToU32(ImVec4(
        ui::palette.statusErr[0], ui::palette.statusErr[1], ui::palette.statusErr[2], 0.95f));
    for (const SpellRange& r : s.misspelled) {
        if (caret >= (int)r.begin && caret <= (int)r.end) continue;
        const ImVec2 at = textOffset(shown, r.begin, lineH);
        const float w = ImGui::CalcTextSize(shown.data() + r.begin, shown.data() + r.end).x;
        squiggle(dl, origin.x + at.x, origin.x + at.x + w,
                 origin.y + at.y + lineH - ui::metrics::px(1.0f), errCol);
    }

    // Ghost completion: caret right after a word, nothing selected, not inside code.
    if (allowGhost && caret >= 0 && !selecting && (size_t)caret <= s.prose.size() &&
        ((size_t)caret == s.prose.size() || !isWordByte(s.prose[(size_t)caret]))) {
        size_t b = (size_t)caret;
        while (b > 0 && std::isalpha((unsigned char)s.prose[b - 1])) --b;
        if ((size_t)caret > b && (b == 0 || !isWordByte(s.prose[b - 1]))) {
            s.ghost = ui::copilot::completionSuffix(
                std::string_view(s.prose.data() + b, (size_t)caret - b), s.dict, s.vocab);
            if (!s.ghost.empty()) {
                const ImVec2 at = textOffset(shown, (size_t)caret, lineH);
                dl->AddText(ImVec2(origin.x + at.x, origin.y + at.y),
                            ImGui::GetColorU32(ImGuiCol_TextDisabled), s.ghost.c_str());
            }
        }
    }
    dl->PopClipRect();

    // Right-click on a squiggled word: corrections, Add to dictionary, Ignore.
    if (ImGui::IsMouseReleased(ImGuiMouseButton_Right) &&
        ImGui::IsWindowHovered(ImGuiHoveredFlags_ChildWindows) &&
        ImGui::IsMouseHoveringRect(clip.Min, clip.Max)) {
        const ImVec2 m = ImGui::GetMousePos();
        for (const SpellRange& r : s.misspelled) {
            const ImVec2 at = textOffset(shown, r.begin, lineH);
            const float x0 = origin.x + at.x, y0 = origin.y + at.y;
            const float x1 = x0 + ImGui::CalcTextSize(shown.data() + r.begin, shown.data() + r.end).x;
            if (m.x < x0 || m.x > x1 || m.y < y0 || m.y > y0 + lineH) continue;
            s.menuRange = r;
            s.menuWord.assign(shown.data() + r.begin, r.end - r.begin);
            s.menuSuggestions = s.dict.suggestions(s.menuWord, 5);
            ImGui::OpenPopup("##composer_spell");
            break;
        }
    }
    bool picked = false;
    if (ImGui::BeginPopup("##composer_spell")) {
        if (s.menuSuggestions.empty()) ImGui::TextDisabled("No suggestions");
        for (const std::string& sug : s.menuSuggestions) {
            if (!ImGui::Selectable(sug.c_str())) continue;
            // The box is inactive while the menu is up, so `shown` is the buffer; make sure
            // the word is still where it was before replacing it.
            if (shown.substr(s.menuRange.begin, s.menuRange.end - s.menuRange.begin) == s.menuWord) {
                fix.begin = (int)s.menuRange.begin;
                fix.end = (int)s.menuRange.end;
                fix.text = sug;
                picked = true;
            }
        }
        ImGui::Separator();
        if (ImGui::Selectable("Add to dictionary")) {
            addUserWord(s, s.menuWord);
            s.shown.clear();
        }
        if (ImGui::Selectable("Ignore")) {
            s.ignored.insert(lowerAscii(s.menuWord));
            s.shown.clear();
        }
        ImGui::EndPopup();
    }
    return picked;
}
