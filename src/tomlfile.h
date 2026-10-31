// SPDX-FileCopyrightText: 2026 Suhan G Paradkar
// SPDX-License-Identifier: MIT
#ifndef _FCITX5_SARVAM_DICTATION_TOMLFILE_H_
#define _FCITX5_SARVAM_DICTATION_TOMLFILE_H_

#include <string>
#include <string_view>
#include <vector>

namespace fcitx::sarvam {

enum class TomlType { String, Bool, Integer, Array };

/// One TOML value: a string, a bool, an integer, or an array of strings.
class TomlValue {
public:
    TomlValue() = default;                                  // empty string
    static TomlValue string(std::string value);
    static TomlValue boolean(bool value);
    static TomlValue integer(long long value);
    static TomlValue array(std::vector<std::string> value);

    TomlType type() const { return type_; }
    const std::string &asString() const;                    // "" when not a String
    bool asBool(bool fallback = false) const;               // fallback when not a Bool
    long long asInteger(long long fallback = 0) const;      // fallback when not an Integer
    const std::vector<std::string> &asArray() const;        // empty when not an Array

    /// The right-hand side only, e.g. `"hi-IN"`, `true`, `300`, `["a", "b"]`.
    std::string serialize() const;

private:
    TomlType type_ = TomlType::String;
    std::string string_;
    bool bool_ = false;
    long long integer_ = 0;
    std::vector<std::string> array_;
};

/// An ordered, case-sensitive TOML document: sections of key/value pairs.
/// Insertion order is preserved on output; a document round-trips through
/// serialize() and parse() unchanged.
class TomlDocument {
public:
    void set(const std::string &section, const std::string &key, TomlValue value);
    bool contains(const std::string &section, const std::string &key) const;
    const TomlValue *get(const std::string &section, const std::string &key) const;
    void remove(const std::string &section, const std::string &key);
    bool empty() const;

    /// Serialize the whole file. Always ends with exactly one "\n".
    std::string serialize() const;

    /// Parse text. Never throws, never fails: lines it cannot understand are
    /// reported through `warnings` (when non-null) and skipped, so a file
    /// written by a newer version still loads.
    static TomlDocument parse(std::string_view text,
                             std::vector<std::string> *warnings = nullptr);

private:
    struct Entry {
        std::string key;
        TomlValue value;
    };

    struct Section {
        std::string name;
        std::vector<Entry> entries;
    };

    // Ordered storage: sections in insertion order, each holding its keys in
    // insertion order. The file's order is the user's order, so a round trip
    // must not shuffle it; a config file has a handful of sections and a few
    // dozen keys, so the linear searches below cost nothing worth measuring.
    std::vector<Section> sections_;

    // The parse loop is a member rather than a free function so it can create
    // a section that has no keys yet: a bare `[section]` header must survive a
    // load/save cycle, and the public API only creates sections via set().
    void parseInto(std::string_view text, std::vector<std::string> &warnings);
};

} // namespace fcitx::sarvam

#endif
