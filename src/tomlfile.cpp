/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * A dependency-free reader/writer for the small TOML subset that the dictation
 * daemon's config.toml uses.
 *
 * Design notes that are not obvious from the code:
 *
 *  - Hand-parsed, like the JSON in sarvamdictation.cpp. A TOML library would be
 *    a new dependency for one config file, and the only genuinely tricky part
 *    is the escaping of \uXXXX and of Indic text, which is a few dozen lines of
 *    well understood code.
 *
 *  - Everything is done on bytes (std::string / std::string_view), never on
 *    code points, because the file is UTF-8 and the daemon stores Devanagari
 *    keyterms. Valid UTF-8 is copied through untouched on the way out and on
 *    the way in; only the ASCII escapes are interpreted.
 *
 *  - parse() never throws and never fails. It is called from the addon's
 *    reloadConfig(), and a settings page that refuses to open because a user
 *    hand-edited a file is worse than one that shows the defaults and drops the
 *    line it did not understand. Every skipped line becomes a warning string.
 *
 *  - Round-tripping is the point of the class: keys and sections nobody touched
 *    through set()/remove() come back out byte for byte, so an edit made by
 *    fcitx5-configtool preserves the user's extra keys. Two consequences that
 *    are easy to get wrong and are handled explicitly below:
 *      * the root section (keys before any header) is always emitted first,
 *        because a key written after a [header] belongs to that header;
 *      * a key or section name that is not a legal bare key is written quoted,
 *        because a quoted key such as "a b" would otherwise come back as a
 *        syntax error.
 */
#include "tomlfile.h"

#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace fcitx::sarvam {
namespace {

constexpr char kHeaderLine1[] =
    "# Written by fcitx5-configtool via the fcitx5-sarvam-dictation addon.";
constexpr char kHeaderLine2[] =
    "# The dictation daemon reads this file; edit it here or in fcitx5-configtool.";

bool isSpaceOrTab(char c) { return c == ' ' || c == '\t'; }

/// Whitespace inside a value, where a newline is ordinary whitespace because a
/// multi-line array is joined into one buffer before it is parsed.
bool isValueSpace(char c) {
    return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

bool isAsciiDigit(char c) { return c >= '0' && c <= '9'; }

bool isBareKeyChar(char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
           (c >= '0' && c <= '9') || c == '-' || c == '_';
}

bool isBareKey(std::string_view text) {
    if (text.empty()) {
        return false;
    }
    for (const char c : text) {
        if (!isBareKeyChar(c)) {
            return false;
        }
    }
    return true;
}

std::string_view trim(std::string_view text) {
    std::size_t begin = 0;
    std::size_t end = text.size();
    while (begin < end && isSpaceOrTab(text[begin])) {
        ++begin;
    }
    while (end > begin && isSpaceOrTab(text[end - 1])) {
        --end;
    }
    return text.substr(begin, end - begin);
}

/// Append one code point as UTF-8. Lone surrogates never reach here; the
/// string parser replaces them with U+FFFD before calling this.
void appendUtf8(std::uint32_t codePoint, std::string &out) {
    if (codePoint < 0x80u) {
        out.push_back(static_cast<char>(codePoint));
    } else if (codePoint < 0x800u) {
        out.push_back(static_cast<char>(0xC0u | (codePoint >> 6)));
        out.push_back(static_cast<char>(0x80u | (codePoint & 0x3Fu)));
    } else if (codePoint < 0x10000u) {
        out.push_back(static_cast<char>(0xE0u | (codePoint >> 12)));
        out.push_back(static_cast<char>(0x80u | ((codePoint >> 6) & 0x3Fu)));
        out.push_back(static_cast<char>(0x80u | (codePoint & 0x3Fu)));
    } else {
        out.push_back(static_cast<char>(0xF0u | (codePoint >> 18)));
        out.push_back(static_cast<char>(0x80u | ((codePoint >> 12) & 0x3Fu)));
        out.push_back(static_cast<char>(0x80u | ((codePoint >> 6) & 0x3Fu)));
        out.push_back(static_cast<char>(0x80u | (codePoint & 0x3Fu)));
    }
}

int hexDigitValue(char c) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

/// Read exactly four hex digits at `pos`. On failure `value` and `next` are
/// left alone and the caller reports the whole string as unparsable.
bool readHex4(std::string_view text, std::size_t pos, std::uint32_t &value,
              std::size_t &next) {
    if (pos + 4 > text.size()) {
        return false;
    }
    std::uint32_t result = 0;
    for (std::size_t i = 0; i < 4; ++i) {
        const int digit = hexDigitValue(text[pos + i]);
        if (digit < 0) {
            return false;
        }
        result = (result << 4) | static_cast<std::uint32_t>(digit);
    }
    value = result;
    next = pos + 4;
    return true;
}

/// Parse a basic ("double quoted") string starting at `pos`, which the caller
/// has already checked holds '"'. Decodes every escape the subset supports and
/// reports the index just past the closing quote.
bool parseBasicString(std::string_view text, std::size_t pos, std::string &out,
                      std::size_t &next) {
    ++pos;
    out.clear();
    const std::size_t size = text.size();
    while (pos < size) {
        const char c = text[pos];
        ++pos;
        if (c == '"') {
            next = pos;
            return true;
        }
        if (c != '\\') {
            out.push_back(c);
            continue;
        }
        if (pos >= size) {
            return false;
        }
        const char escape = text[pos];
        ++pos;
        switch (escape) {
        case '"':
            out.push_back('"');
            break;
        case '\\':
            out.push_back('\\');
            break;
        case '/':
            out.push_back('/');
            break;
        case 'b':
            out.push_back('\b');
            break;
        case 'f':
            out.push_back('\f');
            break;
        case 'n':
            out.push_back('\n');
            break;
        case 'r':
            out.push_back('\r');
            break;
        case 't':
            out.push_back('\t');
            break;
        case 'u': {
            std::uint32_t codePoint = 0;
            std::size_t after = 0;
            if (!readHex4(text, pos, codePoint, after)) {
                return false;
            }
            pos = after;
            if (codePoint >= 0xD800u && codePoint <= 0xDBFFu) {
                // High surrogate: a following low surrogate completes the pair.
                if (pos + 1 < size && text[pos] == '\\' && text[pos + 1] == 'u') {
                    std::uint32_t low = 0;
                    std::size_t afterLow = 0;
                    if (!readHex4(text, pos + 2, low, afterLow)) {
                        return false;
                    }
                    if (low >= 0xDC00u && low <= 0xDFFFu) {
                        codePoint = 0x10000u +
                                    ((codePoint - 0xD800u) << 10) +
                                    (low - 0xDC00u);
                        pos = afterLow;
                    } else {
                        // Not a pair: leave `pos` where it is so the second
                        // escape is decoded on its own next time round.
                        codePoint = 0xFFFDu;
                    }
                } else {
                    // A lone surrogate is not representable in UTF-8.
                    codePoint = 0xFFFDu;
                }
            } else if (codePoint >= 0xDC00u && codePoint <= 0xDFFFu) {
                codePoint = 0xFFFDu;
            }
            appendUtf8(codePoint, out);
            break;
        }
        default:
            // Not an escape this subset knows (including \U, \x and the
            // multi-line line-ending backslash). Treat the whole string as
            // unparsable rather than guessing at it.
            return false;
        }
    }
    return false; // unterminated
}

/// Parse a literal ('single quoted') string starting at `pos`. No escapes at
/// all, which is exactly why the subset supports it: a Windows path or a regex
/// goes in as written.
bool parseLiteralString(std::string_view text, std::size_t pos,
                        std::string &out, std::size_t &next) {
    ++pos;
    out.clear();
    const std::size_t start = pos;
    const std::size_t size = text.size();
    while (pos < size) {
        if (text[pos] == '\'') {
            out.assign(text.substr(start, pos - start));
            next = pos + 1;
            return true;
        }
        ++pos;
    }
    return false; // unterminated
}

/// Append `value` as a quoted, escaped basic string.
///
/// Bytes below 0x20 must be escaped or the file would not be valid TOML; the
/// three that have a short form get it and the rest become \u00XX. Everything
/// else, including all of UTF-8, is copied through unchanged so Hindi keyterms
/// stay readable in the file.
void appendEscaped(std::string_view value, std::string &out) {
    static const char kHexDigits[] = "0123456789ABCDEF";
    out.push_back('"');
    for (const char ch : value) {
        const unsigned char c = static_cast<unsigned char>(ch);
        switch (c) {
        case '"':
            out += "\\\"";
            break;
        case '\\':
            out += "\\\\";
            break;
        case '\n':
            out += "\\n";
            break;
        case '\t':
            out += "\\t";
            break;
        case '\r':
            out += "\\r";
            break;
        default:
            if (c < 0x20u) {
                out += "\\u00";
                out.push_back(kHexDigits[(c >> 4) & 0x0Fu]);
                out.push_back(kHexDigits[c & 0x0Fu]);
            } else {
                out.push_back(ch);
            }
            break;
        }
    }
    out.push_back('"');
}

/// Write a key or a section name: bare when that is legal, quoted otherwise.
std::string serializeKey(std::string_view name) {
    if (isBareKey(name)) {
        return std::string(name);
    }
    std::string out;
    appendEscaped(name, out);
    return out;
}

/// Parse a bare integer token: optional sign, digits, '_' only between digits.
/// Overflow is a parse failure rather than a wrapped value, because silently
/// turning 99999999999999999999999 into garbage would be worse than skipping.
bool parseInteger(std::string_view token, long long &out) {
    const std::size_t size = token.size();
    if (size == 0) {
        return false;
    }
    std::size_t pos = 0;
    bool negative = false;
    if (token[pos] == '+' || token[pos] == '-') {
        negative = (token[pos] == '-');
        ++pos;
    }
    if (pos >= size) {
        return false;
    }
    const unsigned long long limit =
        negative ? 0x8000000000000000ULL : 0x7FFFFFFFFFFFFFFFULL;
    unsigned long long accumulator = 0;
    std::size_t digits = 0;
    bool previousUnderscore = false;
    while (pos < size) {
        const char c = token[pos];
        if (c == '_') {
            // A separator needs a digit on both sides.
            if (digits == 0 || previousUnderscore || pos + 1 >= size ||
                !isAsciiDigit(token[pos + 1])) {
                return false;
            }
            previousUnderscore = true;
            ++pos;
            continue;
        }
        if (!isAsciiDigit(c)) {
            return false;
        }
        const unsigned long long digit = static_cast<unsigned long long>(c - '0');
        if (accumulator > (limit - digit) / 10ULL) {
            return false;
        }
        accumulator = accumulator * 10ULL + digit;
        ++digits;
        previousUnderscore = false;
        ++pos;
    }
    if (digits == 0) {
        return false;
    }
    if (negative) {
        out = (accumulator == 0x8000000000000000ULL)
                  ? std::numeric_limits<long long>::min()
                  : -static_cast<long long>(accumulator);
    } else {
        out = static_cast<long long>(accumulator);
    }
    return true;
}

/// Parse one scalar (string, bool or integer) at `pos`.
bool parseScalar(std::string_view text, std::size_t pos, TomlValue &out,
                 std::size_t &next) {
    const std::size_t size = text.size();
    if (pos >= size) {
        return false;
    }
    const char c = text[pos];
    if (c == '"') {
        std::string value;
        std::size_t after = 0;
        if (!parseBasicString(text, pos, value, after)) {
            return false;
        }
        out = TomlValue::string(std::move(value));
        next = after;
        return true;
    }
    if (c == '\'') {
        std::string value;
        std::size_t after = 0;
        if (!parseLiteralString(text, pos, value, after)) {
            return false;
        }
        out = TomlValue::string(std::move(value));
        next = after;
        return true;
    }
    std::size_t end = pos;
    while (end < size && !isValueSpace(text[end]) && text[end] != ',' &&
           text[end] != ']') {
        ++end;
    }
    const std::string_view token = text.substr(pos, end - pos);
    if (token == "true") {
        out = TomlValue::boolean(true);
        next = end;
        return true;
    }
    if (token == "false") {
        out = TomlValue::boolean(false);
        next = end;
        return true;
    }
    long long integer = 0;
    if (parseInteger(token, integer)) {
        out = TomlValue::integer(integer);
        next = end;
        return true;
    }
    return false;
}

/// Parse an array starting at `pos`, which the caller has checked holds '['.
bool parseArray(std::string_view text, std::size_t pos, TomlValue &out,
                std::size_t &next) {
    ++pos;
    std::vector<std::string> items;
    const std::size_t size = text.size();
    for (;;) {
        while (pos < size && isValueSpace(text[pos])) {
            ++pos;
        }
        if (pos >= size) {
            return false;
        }
        if (text[pos] == ']') {
            out = TomlValue::array(std::move(items));
            next = pos + 1;
            return true;
        }
        TomlValue element;
        std::size_t after = 0;
        if (!parseScalar(text, pos, element, after)) {
            // Nested arrays, inline tables and bare floats land here; the
            // caller turns that into one warning for the whole key.
            return false;
        }
        pos = after;
        // The frozen API stores array elements as std::string, so a bool or an
        // integer element keeps its canonical text form and comes back quoted.
        if (element.type() == TomlType::String) {
            items.push_back(element.asString());
        } else {
            items.push_back(element.serialize());
        }
        while (pos < size && isValueSpace(text[pos])) {
            ++pos;
        }
        if (pos >= size) {
            return false;
        }
        if (text[pos] == ',') {
            ++pos;
            continue;
        }
        if (text[pos] == ']') {
            out = TomlValue::array(std::move(items));
            next = pos + 1;
            return true;
        }
        return false;
    }
}

/// Index just past the ']' that closes the array opening at `pos`, or npos when
/// the text ends first. Strings are skipped whole so a ']' inside one does not
/// count; a nested '[' is tracked so an array of arrays is at least delimited
/// correctly (parseArray then rejects it).
std::size_t scanArrayEnd(std::string_view text, std::size_t pos) {
    int depth = 0;
    const std::size_t size = text.size();
    while (pos < size) {
        const char c = text[pos];
        if (c == '"' || c == '\'') {
            std::string ignored;
            std::size_t after = 0;
            const bool ok = (c == '"')
                                ? parseBasicString(text, pos, ignored, after)
                                : parseLiteralString(text, pos, ignored, after);
            if (!ok) {
                return std::string_view::npos;
            }
            pos = after;
            continue;
        }
        if (c == '[') {
            ++depth;
        } else if (c == ']') {
            --depth;
            if (depth == 0) {
                return pos + 1;
            }
        }
        ++pos;
    }
    return std::string_view::npos;
}

/// Cut the line at the first '#' that is not inside a string.
std::string_view stripComment(std::string_view line) {
    std::size_t pos = 0;
    const std::size_t size = line.size();
    char quote = '\0';
    while (pos < size) {
        const char c = line[pos];
        if (quote == '"') {
            if (c == '\\') {
                pos += 2; // skip the escaped byte; '#' may follow it
                continue;
            }
            if (c == '"') {
                quote = '\0';
            }
            ++pos;
            continue;
        }
        if (quote == '\'') {
            if (c == '\'') {
                quote = '\0';
            }
            ++pos;
            continue;
        }
        if (c == '"' || c == '\'') {
            quote = c;
            ++pos;
            continue;
        }
        if (c == '#') {
            return line.substr(0, pos);
        }
        ++pos;
    }
    return line;
}

/// Index of the first '=' that is not inside a string, or npos.
std::size_t findTopLevelEquals(std::string_view line) {
    std::size_t pos = 0;
    const std::size_t size = line.size();
    char quote = '\0';
    while (pos < size) {
        const char c = line[pos];
        if (quote == '"') {
            if (c == '\\') {
                pos += 2;
                continue;
            }
            if (c == '"') {
                quote = '\0';
            }
            ++pos;
            continue;
        }
        if (quote == '\'') {
            if (c == '\'') {
                quote = '\0';
            }
            ++pos;
            continue;
        }
        if (c == '"' || c == '\'') {
            quote = c;
            ++pos;
            continue;
        }
        if (c == '=') {
            return pos;
        }
        ++pos;
    }
    return std::string_view::npos;
}

} // namespace

// -------------------------------------------------------------- TomlValue --

TomlValue TomlValue::string(std::string value) {
    TomlValue result;
    result.type_ = TomlType::String;
    result.string_ = std::move(value);
    return result;
}

TomlValue TomlValue::boolean(bool value) {
    TomlValue result;
    result.type_ = TomlType::Bool;
    result.bool_ = value;
    return result;
}

TomlValue TomlValue::integer(long long value) {
    TomlValue result;
    result.type_ = TomlType::Integer;
    result.integer_ = value;
    return result;
}

TomlValue TomlValue::array(std::vector<std::string> value) {
    TomlValue result;
    result.type_ = TomlType::Array;
    result.array_ = std::move(value);
    return result;
}

const std::string &TomlValue::asString() const {
    // Only the String factory fills string_, so this is already "" for the
    // other kinds; returning the member keeps the call allocation-free.
    return string_;
}

bool TomlValue::asBool(bool fallback) const {
    return type_ == TomlType::Bool ? bool_ : fallback;
}

long long TomlValue::asInteger(long long fallback) const {
    return type_ == TomlType::Integer ? integer_ : fallback;
}

const std::vector<std::string> &TomlValue::asArray() const { return array_; }

std::string TomlValue::serialize() const {
    switch (type_) {
    case TomlType::String: {
        std::string out;
        appendEscaped(string_, out);
        return out;
    }
    case TomlType::Bool:
        return bool_ ? std::string("true") : std::string("false");
    case TomlType::Integer:
        return std::to_string(integer_);
    case TomlType::Array: {
        std::string out;
        out.push_back('[');
        for (std::size_t i = 0; i < array_.size(); ++i) {
            if (i != 0) {
                out += ", ";
            }
            appendEscaped(array_[i], out);
        }
        out.push_back(']');
        return out;
    }
    }
    return std::string();
}

// ----------------------------------------------------------- TomlDocument --

void TomlDocument::set(const std::string &section, const std::string &key,
                       TomlValue value) {
    for (Section &existing : sections_) {
        if (existing.name != section) {
            continue;
        }
        for (Entry &entry : existing.entries) {
            if (entry.key == key) {
                entry.value = std::move(value); // duplicate: replace in place
                return;
            }
        }
        existing.entries.push_back(Entry{key, std::move(value)});
        return;
    }
    Section fresh;
    fresh.name = section;
    fresh.entries.push_back(Entry{key, std::move(value)});
    sections_.push_back(std::move(fresh));
}

bool TomlDocument::contains(const std::string &section,
                            const std::string &key) const {
    return get(section, key) != nullptr;
}

const TomlValue *TomlDocument::get(const std::string &section,
                                   const std::string &key) const {
    for (const Section &existing : sections_) {
        if (existing.name != section) {
            continue;
        }
        for (const Entry &entry : existing.entries) {
            if (entry.key == key) {
                return &entry.value;
            }
        }
        return nullptr;
    }
    return nullptr;
}

void TomlDocument::remove(const std::string &section, const std::string &key) {
    for (Section &existing : sections_) {
        if (existing.name != section) {
            continue;
        }
        for (auto it = existing.entries.begin(); it != existing.entries.end();
             ++it) {
            if (it->key == key) {
                existing.entries.erase(it);
                break; // keys are unique per section
            }
        }
        // The section itself stays, even when it is now empty: it is the
        // user's, and dropping the header would rewrite their file.
        return;
    }
}

bool TomlDocument::empty() const {
    for (const Section &section : sections_) {
        if (!section.entries.empty()) {
            return false;
        }
    }
    return true;
}

std::string TomlDocument::serialize() const {
    std::string out;
    out += kHeaderLine1;
    out += '\n';
    out += kHeaderLine2;
    out += '\n';

    bool firstBlock = true;
    const auto emitBlock = [&out, &firstBlock](const std::string &block) {
        if (!firstBlock) {
            out += '\n'; // one blank line between blocks
        }
        firstBlock = false;
        out += block;
        out += '\n';
    };

    // The root section goes first and has no header of its own. It must not be
    // emitted anywhere else: a key written after a [header] is read back as a
    // member of that header, which would silently move the user's keys into
    // the preceding section.
    for (const Section &section : sections_) {
        if (!section.name.empty() || section.entries.empty()) {
            continue;
        }
        std::string block;
        for (const Entry &entry : section.entries) {
            block += serializeKey(entry.key);
            block += " = ";
            block += entry.value.serialize();
            block += '\n';
        }
        block.pop_back(); // blocks are joined with newlines, not terminated
        emitBlock(block);
    }

    for (const Section &section : sections_) {
        if (section.name.empty()) {
            continue;
        }
        // An empty section still gets its header, so a `[placeholder]` the user
        // added survives a save.
        std::string block;
        block += '[';
        block += serializeKey(section.name);
        block += ']';
        for (const Entry &entry : section.entries) {
            block += '\n';
            block += serializeKey(entry.key);
            block += " = ";
            block += entry.value.serialize();
        }
        emitBlock(block);
    }

    return out;
}

TomlDocument TomlDocument::parse(std::string_view text,
                                std::vector<std::string> *warnings) {
    TomlDocument document;
    std::vector<std::string> sink;
    std::vector<std::string> &report = (warnings != nullptr) ? *warnings : sink;
    try {
        document.parseInto(text, report);
    } catch (...) {
        // Nothing below throws deliberately; the only possible exception is
        // std::bad_alloc from the containers. The header promises that no
        // exception escapes, so a partially filled document is returned and the
        // caller still gets a usable (if empty) set of defaults.
    }
    return document;
}

void TomlDocument::parseInto(std::string_view text,
                             std::vector<std::string> &warnings) {
    const auto warn = [&warnings](std::size_t lineNumber, const char *message) {
        warnings.push_back("line " + std::to_string(lineNumber) + ": " +
                           message);
    };

    std::string current;   // current section name; empty is the root section
    bool dropping = false; // inside an unsupported header: its keys are dropped
    std::size_t pos = 0;
    std::size_t lineNumber = 0;

    while (pos < text.size()) {
        const std::size_t newline = text.find('\n', pos);
        std::string_view raw;
        if (newline == std::string_view::npos) {
            raw = text.substr(pos);
            pos = text.size();
        } else {
            raw = text.substr(pos, newline - pos);
            pos = newline + 1;
        }
        ++lineNumber;
        if (!raw.empty() && raw.back() == '\r') {
            raw.remove_suffix(1); // CRLF
        }
        const std::string_view line = trim(stripComment(raw));
        if (line.empty()) {
            continue;
        }

        // ---- [section] ---------------------------------------------------
        if (line.front() == '[') {
            const std::string_view body = line.substr(1);
            std::string name;
            bool named = false;
            if (!body.empty() && (body.front() == '"' || body.front() == '\'')) {
                std::string value;
                std::size_t after = 0;
                const bool ok =
                    (body.front() == '"')
                        ? parseBasicString(body, 0, value, after)
                        : parseLiteralString(body, 0, value, after);
                if (!ok || trim(body.substr(after)) != "]") {
                    warn(lineNumber, "malformed section header; skipped");
                } else {
                    name = std::move(value);
                    named = true;
                }
            } else {
                const std::size_t close = body.find(']');
                if (close == std::string_view::npos) {
                    warn(lineNumber, "malformed section header; skipped");
                } else if (!trim(body.substr(close + 1)).empty()) {
                    warn(lineNumber,
                         "trailing text after section header; skipped");
                } else {
                    const std::string_view bare = trim(body.substr(0, close));
                    if (bare.empty()) {
                        warn(lineNumber, "empty section name; skipped");
                    } else if (bare.find('.') != std::string_view::npos) {
                        // [a.b] is a nested table, which this subset does not
                        // model. Warn once and drop the keys that follow, so
                        // they do not land in the wrong section.
                        warn(lineNumber,
                             "dotted section header is not supported; keys "
                             "skipped");
                        dropping = true;
                        current.clear();
                    } else if (!isBareKey(bare)) {
                        warn(lineNumber, "malformed section header; skipped");
                    } else {
                        name.assign(bare);
                        named = true;
                    }
                }
            }
            if (named) {
                dropping = false;
                current = std::move(name);
                bool present = false;
                for (const Section &section : sections_) {
                    if (section.name == current) {
                        present = true;
                        break;
                    }
                }
                if (!present) {
                    Section fresh;
                    fresh.name = current;
                    sections_.push_back(std::move(fresh));
                }
            }
            continue;
        }

        // ---- key = value -------------------------------------------------
        const std::size_t equals = findTopLevelEquals(line);
        if (equals == std::string_view::npos) {
            if (!dropping) {
                warn(lineNumber, "not a key/value line; skipped");
            }
            continue;
        }
        const std::string_view rawKey = trim(line.substr(0, equals));
        const std::string_view valueText = trim(line.substr(equals + 1));

        std::string key;
        bool haveKey = false;
        if (!rawKey.empty() && (rawKey.front() == '"' || rawKey.front() == '\'')) {
            std::string value;
            std::size_t after = 0;
            const bool ok =
                (rawKey.front() == '"')
                    ? parseBasicString(rawKey, 0, value, after)
                    : parseLiteralString(rawKey, 0, value, after);
            if (ok && trim(rawKey.substr(after)).empty()) {
                key = std::move(value);
                haveKey = true;
            }
        } else if (isBareKey(rawKey)) {
            key.assign(rawKey);
            haveKey = true;
        }
        if (!haveKey) {
            if (!dropping) {
                warn(lineNumber, "malformed key; skipped");
            }
            continue;
        }
        if (dropping) {
            continue;
        }
        if (valueText.empty()) {
            warn(lineNumber, "missing value; skipped");
            continue;
        }

        // ---- array, possibly spanning several lines ----------------------
        if (valueText.front() == '[') {
            std::string buffer(valueText);
            std::size_t end = scanArrayEnd(buffer, 0);
            while (end == std::string_view::npos) {
                if (pos >= text.size()) {
                    break;
                }
                const std::size_t nextNewline = text.find('\n', pos);
                std::string_view continuation;
                if (nextNewline == std::string_view::npos) {
                    continuation = text.substr(pos);
                    pos = text.size();
                } else {
                    continuation = text.substr(pos, nextNewline - pos);
                    pos = nextNewline + 1;
                }
                ++lineNumber;
                if (!continuation.empty() && continuation.back() == '\r') {
                    continuation.remove_suffix(1);
                }
                // Comments are dropped per line; a string may not span lines,
                // so each continuation line starts outside a string.
                buffer.push_back('\n');
                buffer.append(stripComment(continuation));
                end = scanArrayEnd(buffer, 0);
            }
            if (end == std::string_view::npos) {
                warn(lineNumber, "unterminated array; skipped");
                continue;
            }
            TomlValue value;
            std::size_t after = 0;
            if (!parseArray(buffer, 0, value, after) ||
                !trim(std::string_view(buffer).substr(after)).empty()) {
                warn(lineNumber, "unsupported array value; skipped");
                continue;
            }
            set(current, key, std::move(value));
            continue;
        }

        // ---- any other value --------------------------------------------
        TomlValue value;
        std::size_t after = 0;
        if (!parseScalar(valueText, 0, value, after) ||
            !trim(valueText.substr(after)).empty()) {
            // Floats, dates, times, inline tables and anything else outside
            // the subset: one warning, the key is skipped, the rest of the
            // file still loads.
            warn(lineNumber, "unsupported value; skipped");
            continue;
        }
        set(current, key, std::move(value));
    }
}

} // namespace fcitx::sarvam
