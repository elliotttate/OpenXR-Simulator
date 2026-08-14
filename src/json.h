// Minimal read-only JSON parser for the simulator's small files: the one-shot
// command files the MCP server writes, and settings.json. Both sides are
// cooperative, so lookups are lenient -- a missing key, a wrong type or a
// truncated file leaves the caller's default in place rather than raising an
// error.
//
// Lookups walk the members of one object only, so a key nested inside "left"
// cannot answer a lookup meant for the document root.
#pragma once

#include <locale.h>
#include <cstdlib>
#include <cstring>
#include <string>

namespace json {

namespace detail {

// This DLL lives in someone else's process and hosts do call setlocale(), so
// parse against the C locale rather than the global one -- otherwise a
// comma-decimal host reads 1.7 as 1.
inline double Strtod(const char* s, char** end) {
    static const _locale_t c = _create_locale(LC_NUMERIC, "C");
    return _strtod_l(s, end, c);
}

inline const char* SkipWs(const char* p) {
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') ++p;
    return p;
}

// Past the closing quote of the string opening at `p`, or nullptr if it never
// closes.
inline const char* SkipString(const char* p) {
    for (++p; *p; ++p) {
        if (*p == '\\' && p[1]) ++p;
        else if (*p == '"') return p + 1;
    }
    return nullptr;
}

// Past the value starting at `p`, whatever its type.
inline const char* SkipValue(const char* p) {
    p = SkipWs(p);
    if (*p == '"') return SkipString(p);
    if (*p == '{' || *p == '[') {
        int depth = 0;
        while (*p) {
            if (*p == '"') {
                p = SkipString(p);
                if (!p) return nullptr;
                continue;
            }
            if (*p == '{' || *p == '[') ++depth;
            else if ((*p == '}' || *p == ']') && --depth == 0) return p + 1;
            ++p;
        }
        return nullptr;
    }
    // A number, true, false or null: runs until the member or its container
    // ends.
    while (*p && !strchr(",}] \t\n\r", *p)) ++p;
    return p;
}

inline void AppendUtf8(std::string& out, unsigned cp) {
    if (cp < 0x80) {
        out += (char)cp;
    } else if (cp < 0x800) {
        out += (char)(0xC0 | (cp >> 6));
        out += (char)(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        out += (char)(0xE0 | (cp >> 12));
        out += (char)(0x80 | ((cp >> 6) & 0x3F));
        out += (char)(0x80 | (cp & 0x3F));
    } else {
        out += (char)(0xF0 | (cp >> 18));
        out += (char)(0x80 | ((cp >> 12) & 0x3F));
        out += (char)(0x80 | ((cp >> 6) & 0x3F));
        out += (char)(0x80 | (cp & 0x3F));
    }
}

inline bool Hex4(const char* p, unsigned& out) {
    out = 0;
    for (int i = 0; i < 4; ++i) {
        char c = p[i];
        unsigned d;
        if (c >= '0' && c <= '9') d = (unsigned)(c - '0');
        else if (c >= 'a' && c <= 'f') d = (unsigned)(c - 'a') + 10;
        else if (c >= 'A' && c <= 'F') d = (unsigned)(c - 'A') + 10;
        else return false;
        out = out * 16 + d;
    }
    return true;
}

// Unescapes the string opening at `p`. Anything malformed ends the string
// where it went wrong, keeping what came before it.
inline std::string DecodeString(const char* p) {
    std::string out;
    for (++p; *p && *p != '"'; ) {
        if (*p != '\\') { out += *p++; continue; }
        switch (*++p) {
            case '"':  out += '"';  ++p; break;
            case '\\': out += '\\'; ++p; break;
            case '/':  out += '/';  ++p; break;
            case 'b':  out += '\b'; ++p; break;
            case 'f':  out += '\f'; ++p; break;
            case 'n':  out += '\n'; ++p; break;
            case 'r':  out += '\r'; ++p; break;
            case 't':  out += '\t'; ++p; break;
            case 'u': {
                unsigned cp;
                if (!Hex4(p + 1, cp)) return out;
                p += 5;
                // A leading surrogate needs its trailing half from the next escape.
                if (cp >= 0xD800 && cp <= 0xDBFF && p[0] == '\\' && p[1] == 'u') {
                    unsigned lo;
                    if (Hex4(p + 2, lo) && lo >= 0xDC00 && lo <= 0xDFFF) {
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        p += 6;
                    }
                }
                AppendUtf8(out, cp);
                break;
            }
            default: return out;
        }
    }
    return out;
}

} // namespace detail

// A view over one JSON object. Copyable and non-owning: the text has to
// outlive every Object pointing into it.
class Object {
public:
    // The empty object, which every lookup misses.
    Object() = default;

    // Views the first object in `text`, so callers can hand over a whole file
    // buffer without trimming it.
    explicit Object(const char* text) : begin_(text ? strchr(text, '{') : nullptr) {}

    // False when the text held no object at all.
    bool valid() const { return begin_ != nullptr; }

    // Distinguishes an omitted key from one explicitly set to 0 or false.
    bool has(const char* key) const { return Find(key) != nullptr; }

    double number(const char* key, double def) const {
        const char* v = Find(key);
        if (!v) return def;
        char* end = nullptr;
        double d = detail::Strtod(v, &end);
        return end == v ? def : d;
    }
    float number(const char* key, float def) const {
        return (float)number(key, (double)def);
    }
    int number(const char* key, int def) const {
        return (int)number(key, (double)def);
    }

    bool boolean(const char* key, bool def) const {
        const char* v = Find(key);
        if (!v) return def;
        if (strncmp(v, "true", 4) == 0) return true;
        if (strncmp(v, "false", 5) == 0) return false;
        return def;
    }

    std::string string(const char* key, const char* def = "") const {
        const char* v = Find(key);
        return (v && *v == '"') ? detail::DecodeString(v) : std::string(def);
    }

    Object object(const char* key) const {
        Object o;
        const char* v = Find(key);
        if (v && *v == '{') o.begin_ = v;
        return o;
    }

private:
    // The value text for `key`, or nullptr if this object has no such member.
    // Member names are matched raw, which is exact for the plain keys the
    // simulator uses and near enough for anything else.
    const char* Find(const char* key) const {
        if (!begin_) return nullptr;
        size_t keyLen = strlen(key);
        const char* p = detail::SkipWs(begin_ + 1);
        while (*p && *p != '}') {
            if (*p != '"') return nullptr;
            const char* name = p + 1;
            const char* afterName = detail::SkipString(p);
            if (!afterName) return nullptr;
            size_t nameLen = (size_t)(afterName - 1 - name);

            p = detail::SkipWs(afterName);
            if (*p != ':') return nullptr;

            const char* value = detail::SkipWs(p + 1);
            if (nameLen == keyLen && strncmp(name, key, keyLen) == 0) return value;

            p = detail::SkipValue(value);
            if (!p) return nullptr;
            p = detail::SkipWs(p);
            if (*p == ',') p = detail::SkipWs(p + 1);
        }
        return nullptr;
    }

    const char* begin_ = nullptr;
};

} // namespace json
