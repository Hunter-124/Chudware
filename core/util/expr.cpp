// C++ port of engine/doc/equations.py `evaluate`. A hand-written recursive-
// descent parser/evaluator over the same grammar and precedence as the Python
// AST path, so arithmetic-expression dims match the oracle within 1e-9.
//
// Precedence (lowest -> highest), matching Python:
//   add    : mul (('+' | '-') mul)*            left-assoc
//   mul    : unary (('*' | '/') unary)*        left-assoc
//   unary  : ('+' | '-') unary | power
//   power  : atom ('**' unary)?                right-assoc (RHS is unary, so
//                                              2**-1 and a**b**c work like Python)
//   atom   : number | name | call | '(' add ')'
//
// The `unary`-above-`power` arrangement reproduces Python's `-2**2 == -4` and
// `2**-2 == 0.25` exactly.
#include "util/expr.h"

#include <cctype>
#include <cmath>
#include <functional>
#include <vector>

namespace aicad::util {
namespace {

// Degrees-in / degrees-out trig, matching equations._deg_in / _deg_out.
constexpr double kPi = 3.14159265358979323846;        // == math.pi
double radians(double d) { return d * (kPi / 180.0); }
double degrees(double r) { return r * (180.0 / kPi); }

// --- tokenizer ---------------------------------------------------------------

enum class Tok { Num, Name, Op, LParen, RParen, Comma, End };

struct Token {
    Tok kind;
    double num = 0.0;     // Num
    std::string text;     // Name (identifier) or Op (operator string)
};

class Lexer {
 public:
    Lexer(const std::string& src, const std::string& orig) : s_(src), orig_(orig) {}

    std::vector<Token> tokenize() {
        std::vector<Token> out;
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (std::isspace(static_cast<unsigned char>(c))) { ++pos_; continue; }
            if (std::isdigit(static_cast<unsigned char>(c)) || c == '.') {
                out.push_back(lex_number());
            } else if (std::isalpha(static_cast<unsigned char>(c)) || c == '_') {
                out.push_back(lex_name());
            } else if (c == '(') { out.push_back({Tok::LParen}); ++pos_; }
            else if (c == ')') { out.push_back({Tok::RParen}); ++pos_; }
            else if (c == ',') { out.push_back({Tok::Comma}); ++pos_; }
            else if (c == '*') {
                // '**' (or normalized '^') is a single power op.
                if (pos_ + 1 < s_.size() && s_[pos_ + 1] == '*') {
                    out.push_back({Tok::Op, 0.0, "**"}); pos_ += 2;
                } else { out.push_back({Tok::Op, 0.0, "*"}); ++pos_; }
            } else if (c == '+' || c == '-' || c == '/') {
                out.push_back({Tok::Op, 0.0, std::string(1, c)}); ++pos_;
            } else {
                throw ExprError("syntax error in '" + orig_ + "': unexpected '" +
                                std::string(1, c) + "'");
            }
        }
        out.push_back({Tok::End});
        return out;
    }

 private:
    Token lex_number() {
        size_t start = pos_;
        bool seen_dot = false, seen_exp = false;
        while (pos_ < s_.size()) {
            char c = s_[pos_];
            if (std::isdigit(static_cast<unsigned char>(c))) { ++pos_; }
            else if (c == '.' && !seen_dot && !seen_exp) { seen_dot = true; ++pos_; }
            else if ((c == 'e' || c == 'E') && !seen_exp && pos_ > start) {
                seen_exp = true; ++pos_;
                if (pos_ < s_.size() && (s_[pos_] == '+' || s_[pos_] == '-')) ++pos_;
            } else { break; }
        }
        std::string tok = s_.substr(start, pos_ - start);
        try {
            size_t consumed = 0;
            double v = std::stod(tok, &consumed);
            if (consumed != tok.size()) throw std::invalid_argument(tok);
            return {Tok::Num, v, tok};
        } catch (const std::exception&) {
            throw ExprError("syntax error in '" + orig_ + "': bad number '" + tok + "'");
        }
    }

    Token lex_name() {
        size_t start = pos_;
        while (pos_ < s_.size() &&
               (std::isalnum(static_cast<unsigned char>(s_[pos_])) || s_[pos_] == '_')) {
            ++pos_;
        }
        return {Tok::Name, 0.0, s_.substr(start, pos_ - start)};
    }

    const std::string& s_;
    const std::string& orig_;
    size_t pos_ = 0;
};

// --- parser + evaluator (single pass: parse-and-evaluate) --------------------

class Parser {
 public:
    Parser(std::vector<Token> toks, const std::map<std::string, double>& env,
           const std::string& orig)
        : toks_(std::move(toks)), env_(env), orig_(orig) {}

    double parse() {
        double v = parse_add();
        if (cur().kind != Tok::End) {
            throw ExprError("syntax error in '" + orig_ + "': trailing tokens");
        }
        return v;
    }

 private:
    const Token& cur() const { return toks_[i_]; }
    void advance() { if (i_ + 1 < toks_.size()) ++i_; }

    bool is_op(const char* op) const {
        return cur().kind == Tok::Op && cur().text == op;
    }

    double parse_add() {
        double v = parse_mul();
        while (is_op("+") || is_op("-")) {
            std::string op = cur().text;
            advance();
            double rhs = parse_mul();
            v = (op == "+") ? v + rhs : v - rhs;
        }
        return v;
    }

    double parse_mul() {
        double v = parse_unary();
        while (is_op("*") || is_op("/")) {
            std::string op = cur().text;
            advance();
            double rhs = parse_unary();
            if (op == "*") {
                v = v * rhs;
            } else {
                if (rhs == 0.0) {
                    throw ExprError("division by zero in '" + orig_ + "'");
                }
                v = v / rhs;
            }
        }
        return v;
    }

    double parse_unary() {
        if (is_op("-")) { advance(); return -parse_unary(); }
        if (is_op("+")) { advance(); return parse_unary(); }
        return parse_power();
    }

    double parse_power() {
        double base = parse_atom();
        if (is_op("**")) {
            advance();
            double exp = parse_unary();   // right-assoc; allows 2**-1 like Python
            return std::pow(base, exp);
        }
        return base;
    }

    double parse_atom() {
        const Token& t = cur();
        if (t.kind == Tok::Num) { double v = t.num; advance(); return v; }
        if (t.kind == Tok::LParen) {
            advance();
            double v = parse_add();
            if (cur().kind != Tok::RParen) {
                throw ExprError("syntax error in '" + orig_ + "': expected ')'");
            }
            advance();
            return v;
        }
        if (t.kind == Tok::Name) {
            std::string name = t.text;
            advance();
            if (cur().kind == Tok::LParen) {
                return parse_call(name);
            }
            return resolve_name(name);
        }
        throw ExprError("syntax error in '" + orig_ + "': unexpected token");
    }

    double resolve_name(const std::string& name) {
        if (name == "pi") return kPi;                 // _CONSTS
        auto it = env_.find(name);
        if (it != env_.end()) return it->second;
        throw ExprError("unknown name '" + name + "' in '" + orig_ + "'");
    }

    double parse_call(const std::string& name) {
        // cur() is the '('.
        advance();
        std::vector<double> args;
        if (cur().kind != Tok::RParen) {
            args.push_back(parse_add());
            while (cur().kind == Tok::Comma) {
                advance();
                args.push_back(parse_add());
            }
        }
        if (cur().kind != Tok::RParen) {
            throw ExprError("syntax error in '" + orig_ + "': expected ')' in call");
        }
        advance();
        return apply_func(name, args);
    }

    double apply_func(const std::string& fn, const std::vector<double>& a) {
        auto need1 = [&]() {
            if (a.size() != 1) {
                throw ExprError("math error in '" + orig_ + "': " + fn +
                                "() takes 1 argument");
            }
        };
        // Whitelisted function set, matching equations._FUNCS exactly.
        if (fn == "sin") { need1(); return std::sin(radians(a[0])); }
        if (fn == "cos") { need1(); return std::cos(radians(a[0])); }
        if (fn == "tan") { need1(); return std::tan(radians(a[0])); }
        if (fn == "asin") {
            need1();
            if (a[0] < -1.0 || a[0] > 1.0) {
                throw ExprError("math error in '" + orig_ + "': asin domain");
            }
            return degrees(std::asin(a[0]));
        }
        if (fn == "acos") {
            need1();
            if (a[0] < -1.0 || a[0] > 1.0) {
                throw ExprError("math error in '" + orig_ + "': acos domain");
            }
            return degrees(std::acos(a[0]));
        }
        if (fn == "atan") { need1(); return degrees(std::atan(a[0])); }
        if (fn == "sqrt") {
            need1();
            if (a[0] < 0.0) {
                throw ExprError("math error in '" + orig_ + "': sqrt domain");
            }
            return std::sqrt(a[0]);
        }
        if (fn == "abs") { need1(); return std::fabs(a[0]); }
        if (fn == "floor") { need1(); return std::floor(a[0]); }
        if (fn == "ceil") { need1(); return std::ceil(a[0]); }
        if (fn == "min" || fn == "max") {
            if (a.empty()) {
                throw ExprError("math error in '" + orig_ + "': " + fn +
                                "() arg is empty");
            }
            double r = a[0];
            for (double v : a) {
                if (fn == "min") { if (v < r) r = v; }
                else { if (v > r) r = v; }
            }
            return r;
        }
        throw ExprError("unknown function in '" + orig_ + "'");
    }

    std::vector<Token> toks_;
    const std::map<std::string, double>& env_;
    const std::string& orig_;
    size_t i_ = 0;
};

// Normalize '^' to '**' textually, exactly like equations._normalize.
std::string normalize(const std::string& expr) {
    std::string out;
    out.reserve(expr.size() + 8);
    for (char c : expr) {
        if (c == '^') out += "**";
        else out += c;
    }
    return out;
}

}  // namespace

double evaluate(const std::string& expr, const std::map<std::string, double>& env) {
    // Reject empty / whitespace-only, mirroring equations._parse.
    bool nonblank = false;
    for (char c : expr) {
        if (!std::isspace(static_cast<unsigned char>(c))) { nonblank = true; break; }
    }
    if (!nonblank) throw ExprError("empty expression");

    std::string norm = normalize(expr);
    Lexer lex(norm, expr);
    Parser parser(lex.tokenize(), env, expr);
    double result = parser.parse();
    if (!std::isfinite(result)) {
        throw ExprError("non-finite result from '" + expr + "'");
    }
    return result;
}

}  // namespace aicad::util
