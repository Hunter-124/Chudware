// core/piping/catalog.cpp — port of engine_oracle/piping/catalog.py.
// Schedule tables are vendored (pipe_tables.h, generated from fluids v1.3.1
// schedule_lookup); the fitting CSV is embedded (fittings_data.h).
#include "piping/catalog.h"

#include <algorithm>
#include <cctype>
#include <map>
#include <sstream>

#include "piping/fittings_data.h"
#include "piping/pipe_tables.h"

namespace chudware::piping {
namespace {

using pyutil::jerr;
using pyutil::py_repr;

// Schedule aliases fluids does not key directly -> a concrete schedule it does.
std::string schedule_alias(const std::string& schedule) {
    std::string up = schedule;
    std::transform(up.begin(), up.end(), up.begin(),
                   [](unsigned char c) { return std::toupper(c); });
    if (up == "STD") return "40";
    if (up == "XS") return "80";
    return schedule;
}

const tables::ScheduleTable* find_schedule(const std::string& name) {
    for (int i = 0; i < tables::N_SCHEDULES; ++i)
        if (name == tables::SCHEDULES[i].name) return &tables::SCHEDULES[i];
    return nullptr;
}

// fluids' pipe_too_large_msg (Di/Do lookup past the table end).
const char* kTooLargeMsg = "Pipe input is larger than max of selected schedule";

// Python float(v) failure text (ValueError inside fluids' lookup -> wrapped).
double to_double_py(const ojson& v) {
    try {
        return pyutil::to_double(v);
    } catch (...) {
        throw std::runtime_error("could not convert string to float: " +
                                 py_repr(v));
    }
}

// ── fitting catalog (embedded CSV) ───────────────────────────────────────────

bool is_num_col(const std::string& k) {
    static const char* const cols[] = {"nps",      "nps2",  "center_to_end", "length",
                                       "flange_class", "od", "thickness",
                                       "bolt_circle",  "bolt_holes",       "bolt_dia"};
    for (const char* c : cols)
        if (k == c) return true;
    return false;
}

std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

std::vector<std::string> split_csv_line(const std::string& line) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : line) {
        if (c == ',') {
            out.push_back(cur);
            cur.clear();
        } else {
            cur += c;
        }
    }
    out.push_back(cur);
    return out;
}

const std::vector<ojson>& fitting_rows() {
    static const std::vector<ojson> rows = [] {
        std::vector<ojson> out;
        std::istringstream in(FITTINGS_CSV);
        std::string line;
        std::vector<std::string> header;
        while (std::getline(in, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            if (trim(line).empty()) continue;
            if (header.empty()) {
                for (auto& h : split_csv_line(line)) header.push_back(trim(h));
                continue;
            }
            const auto cells = split_csv_line(line);
            ojson row = ojson::object();
            for (size_t i = 0; i < header.size(); ++i) {
                const std::string key = header[i];
                const std::string val = i < cells.size() ? trim(cells[i]) : "";
                if (is_num_col(key)) {
                    row[key] = val.empty() ? ojson(nullptr) : ojson(std::stod(val));
                } else {
                    row[key] = val.empty() ? ojson(nullptr) : ojson(val);
                }
            }
            out.push_back(std::move(row));
        }
        return out;
    }();
    return rows;
}

}  // namespace

ojson nearest_pipe(const ojson& nps, const std::string& schedule,
                   const ojson& outer_dia) {
    const std::string sched = schedule_alias(schedule);
    const tables::ScheduleTable* tab = find_schedule(sched);

    std::string fail_reason;
    int idx = -1;
    try {
        if (tab == nullptr) {
            fail_reason = "Schedule not recognized";
        } else if (!outer_dia.is_null()) {
            const double do_mm = to_double_py(outer_dia);  // mm (Do*1E3 in the oracle)
            for (int i = 0; i < tab->n; ++i) {
                if (tab->od[tab->n - 1] < do_mm) {
                    fail_reason = kTooLargeMsg;
                    break;
                }
                if (tab->od[i] >= do_mm) {
                    idx = i;
                    break;
                }
            }
            if (fail_reason.empty() && idx < 0) fail_reason = "Do lookup failed";
        } else if (!nps.is_null()) {
            const double want = to_double_py(nps);
            for (int i = 0; i < tab->n; ++i) {
                if (tab->nps[i] == want) {
                    idx = i;
                    break;
                }
            }
            if (idx < 0) fail_reason = "NPS not in list";
        } else {
            return jerr("nearest_pipe requires nps or outer_dia");
        }
    } catch (const std::exception& e) {
        fail_reason = e.what();
    }

    if (!fail_reason.empty()) {
        return jerr("no catalog pipe for nps=" + py_repr(nps) +
                        " schedule=" + py_repr(schedule) + ": " + fail_reason,
                    "try a standard NPS (0.5,0.75,1,1.5,2,3,4,6,8,10,12) + schedule");
    }

    ojson out = ojson::object();
    out["ok"] = true;
    out["nps"] = tab->nps[idx];
    out["outer_dia"] = tab->od[idx];
    out["inner_dia"] = tab->di[idx];
    out["wall"] = tab->wall[idx];
    out["schedule"] = schedule;
    return out;
}

ojson fitting_dims(const std::string& ftype, double nps, const ojson& nps2,
                   const ojson& flange_class) {
    std::vector<const ojson*> rows;
    for (const auto& r : fitting_rows())
        if (r.value("type", ojson(nullptr)) == ftype) rows.push_back(&r);
    if (rows.empty()) {
        return jerr("no catalog rows for fitting type " + py_repr(ftype),
                    "known types: elbow, tee, reducer, flange");
    }

    const ojson* hit = nullptr;
    for (const ojson* r : rows) {
        const ojson rnps = r->value("nps", ojson(nullptr));
        if (!rnps.is_number() || rnps.get<double>() != nps) continue;
        if (ftype == "reducer" && !nps2.is_null()) {
            const ojson r2 = r->value("nps2", ojson(nullptr));
            if (!r2.is_number() || r2.get<double>() != nps2.get<double>()) continue;
        }
        if (ftype == "flange" && !flange_class.is_null()) {
            const ojson rc = r->value("flange_class", ojson(nullptr));
            const long have = rc.is_number() ? static_cast<long>(rc.get<double>()) : 0;
            if (have != static_cast<long>(pyutil::to_double(flange_class))) continue;
        }
        hit = r;
        break;
    }
    if (hit != nullptr) return *hit;

    std::vector<double> avail;
    for (const ojson* r : rows) {
        const ojson rnps = r->value("nps", ojson(nullptr));
        if (rnps.is_number()) avail.push_back(rnps.get<double>());
    }
    std::sort(avail.begin(), avail.end());
    avail.erase(std::unique(avail.begin(), avail.end()), avail.end());
    std::string reason = "no " + ftype + " in catalog for nps=" + py_repr(nps);
    if (ftype == "reducer") reason += " nps2=" + py_repr(nps2);
    if (ftype == "flange") reason += " class=" + py_repr(flange_class);
    if (avail.empty()) return jerr(reason);
    const double nearest = *std::min_element(
        avail.begin(), avail.end(),
        [&](double a, double b) { return std::abs(a - nps) < std::abs(b - nps); });
    return jerr(reason, "nearest available nps=" + pyutil::py_float(nearest));
}

std::vector<double> available_sizes(const std::string& ftype) {
    std::vector<double> out;
    for (const auto& r : fitting_rows()) {
        if (r.value("type", ojson(nullptr)) != ftype) continue;
        const ojson rnps = r.value("nps", ojson(nullptr));
        if (rnps.is_number()) out.push_back(rnps.get<double>());
    }
    std::sort(out.begin(), out.end());
    out.erase(std::unique(out.begin(), out.end()), out.end());
    return out;
}

}  // namespace chudware::piping
