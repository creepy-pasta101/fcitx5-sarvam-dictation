/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * Unit tests for the dependency-free TOML subset reader/writer.
 *
 * No test framework on purpose: the project has no test dependency, and a plain
 * main() that prints one line per failure is enough for a file format. Run it
 * directly (it exits non-zero when anything fails) or through CTest.
 *
 * The include path is relative so the file compiles whether or not the build
 * puts src/ on the include path.
 */
#include "../../src/tomlfile.h"

#include <cstdio>
#include <string>
#include <vector>

using fcitx::sarvam::TomlDocument;
using fcitx::sarvam::TomlType;
using fcitx::sarvam::TomlValue;

namespace {

int failures = 0;

const char kHeaderLine1[] =
    "# Written by fcitx5-configtool via the fcitx5-sarvam-dictation addon.";
const char kHeaderLine2[] =
    "# The dictation daemon reads this file; edit it here or in fcitx5-configtool.";

// Byte-exact literals: this file stays ASCII, and the bytes below are the real
// UTF-8 the daemon stores (Devanagari keyterms, an emoji, U+FFFD, U+00E9).
const char kHindi[] =
    "\xe0\xa4\xa8\xe0\xa4\xae\xe0\xa4\xb8\xe0\xa5\x8d\xe0\xa4\xa4\xe0\xa5\x87"; // namaste
const char kEmoji[] = "\xf0\x9f\x98\x80";       // U+1F600
const char kReplacement[] = "\xef\xbf\xbd";     // U+FFFD
const char kEAcute[] = "\xc3\xa9";              // U+00E9

void fail(const std::string &name, const std::string &detail) {
    std::printf("FAIL %s :: %s\n", name.c_str(), detail.c_str());
    ++failures;
}

void check(const std::string &name, bool condition) {
    if (!condition) {
        fail(name, "condition was false");
    }
}

void checkEq(const std::string &name, const std::string &got,
             const std::string &want) {
    if (got != want) {
        fail(name, "got [" + got + "] want [" + want + "]");
    }
}

void checkEqInt(const std::string &name, long long got, long long want) {
    if (got != want) {
        fail(name, "got " + std::to_string(got) + " want " +
                       std::to_string(want));
    }
}

std::string describe(const std::vector<std::string> &items) {
    std::string out;
    for (std::size_t i = 0; i < items.size(); ++i) {
        if (i != 0) {
            out += "|";
        }
        out += items[i];
    }
    return out;
}

// ---- expectations ---------------------------------------------------------

void expectString(const std::string &name, const TomlDocument &doc,
                  const char *section, const char *key,
                  const std::string &want) {
    const TomlValue *value = doc.get(section, key);
    if (value == nullptr) {
        fail(name, "key is missing");
        return;
    }
    if (value->type() != TomlType::String) {
        fail(name, "value is not a String");
        return;
    }
    checkEq(name, value->asString(), want);
}

void expectBool(const std::string &name, const TomlDocument &doc,
                const char *section, const char *key, bool want) {
    const TomlValue *value = doc.get(section, key);
    if (value == nullptr) {
        fail(name, "key is missing");
        return;
    }
    if (value->type() != TomlType::Bool) {
        fail(name, "value is not a Bool");
        return;
    }
    if (value->asBool(!want) != want) {
        fail(name, "wrong bool value");
    }
}

void expectInteger(const std::string &name, const TomlDocument &doc,
                   const char *section, const char *key, long long want) {
    const TomlValue *value = doc.get(section, key);
    if (value == nullptr) {
        fail(name, "key is missing");
        return;
    }
    if (value->type() != TomlType::Integer) {
        fail(name, "value is not an Integer");
        return;
    }
    checkEqInt(name, value->asInteger(), want);
}

void expectArray(const std::string &name, const TomlDocument &doc,
                 const char *section, const char *key,
                 const std::vector<std::string> &want) {
    const TomlValue *value = doc.get(section, key);
    if (value == nullptr) {
        fail(name, "key is missing");
        return;
    }
    if (value->type() != TomlType::Array) {
        fail(name, "value is not an Array");
        return;
    }
    if (value->asArray() != want) {
        fail(name, "got [" + describe(value->asArray()) + "] want [" +
                       describe(want) + "]");
    }
}

void expectMissing(const std::string &name, const TomlDocument &doc,
                   const char *section, const char *key) {
    if (doc.get(section, key) != nullptr) {
        fail(name, "key should be absent but is present");
    }
}

// ---- round trip -----------------------------------------------------------

/// Parse `source`, serialize it, parse the result and serialize again. The two
/// serializations must be identical, the second parse must be warning-free
/// (we only ever write what we can read) and the output must end with exactly
/// one newline. Returns the first document.
TomlDocument roundTrip(const std::string &name, const std::string &source) {
    std::vector<std::string> warnings;
    TomlDocument first = TomlDocument::parse(source, &warnings);
    const std::string text = first.serialize();

    std::vector<std::string> secondWarnings;
    TomlDocument second = TomlDocument::parse(text, &secondWarnings);
    const std::string again = second.serialize();

    if (text != again) {
        fail(name + " [stable]", "[" + text + "] != [" + again + "]");
    }
    if (!secondWarnings.empty()) {
        fail(name + " [own output]", "re-parsing our own output warned: " +
                                         secondWarnings.front());
    }
    if (text.empty() || text[text.size() - 1] != '\n') {
        fail(name + " [newline]", "output does not end with a newline");
    }
    if (text.size() >= 2 && text[text.size() - 2] == '\n') {
        fail(name + " [newline]", "output ends with more than one newline");
    }
    return first;
}

/// A value outside the subset must produce exactly one warning and no key.
void expectSkip(const std::string &name, const std::string &source) {
    std::vector<std::string> warnings;
    TomlDocument doc = TomlDocument::parse(source, &warnings);
    if (doc.get("a", "x") != nullptr) {
        fail(name, "value was not skipped");
    }
    if (warnings.size() != 1) {
        fail(name, "expected 1 warning, got " + std::to_string(warnings.size()));
    }
}

// ---- the tests ------------------------------------------------------------

void testTypesRoundTrip() {
    const std::string source =
        "[api]\n"
        "key = \"hi-IN\"\n"
        "model = \"saaras:v4\"\n"
        "flag = true\n"
        "off = false\n"
        "n = 300\n"
        "terms = [\"a\", \"b\"]\n";
    TomlDocument doc = roundTrip("types", source);
    expectString("types/string", doc, "api", "key", "hi-IN");
    expectBool("types/bool-true", doc, "api", "flag", true);
    expectBool("types/bool-false", doc, "api", "off", false);
    expectInteger("types/integer", doc, "api", "n", 300);
    expectArray("types/array", doc, "api", "terms",
                std::vector<std::string>{"a", "b"});

    // TomlValue::serialize() is the right-hand side only.
    checkEq("value/serialize-string",
            TomlValue::string("hi-IN").serialize(), "\"hi-IN\"");
    checkEq("value/serialize-bool-true", TomlValue::boolean(true).serialize(),
            "true");
    checkEq("value/serialize-bool-false", TomlValue::boolean(false).serialize(),
            "false");
    checkEq("value/serialize-integer", TomlValue::integer(300).serialize(),
            "300");
    checkEq("value/serialize-integer-negative",
            TomlValue::integer(-7).serialize(), "-7");
    checkEq("value/serialize-array",
            TomlValue::array(std::vector<std::string>{"a", "b"}).serialize(),
            "[\"a\", \"b\"]");
    checkEq("value/serialize-array-empty",
            TomlValue::array(std::vector<std::string>{}).serialize(), "[]");

    // Accessors on the wrong type answer with their documented fallbacks.
    const TomlValue empty;
    checkEq("value/default-is-empty-string", empty.asString(), "");
    checkEq("value/default-type", std::to_string(static_cast<int>(empty.type())),
            std::to_string(static_cast<int>(TomlType::String)));
    check("value/fallback-bool", empty.asBool(true));
    checkEqInt("value/fallback-integer", empty.asInteger(42), 42);
    check("value/fallback-array", empty.asArray().empty());
    checkEq("value/string-asBool-fallback",
            TomlValue::string("x").asBool(false) ? "true" : "false", "false");
    checkEqInt("value/bool-asInteger-fallback",
               TomlValue::boolean(true).asInteger(9), 9);
}

void testEscapes() {
    const std::string source =
        "[s]\n"
        "q = \"a\\\"b\"\n"
        "bs = \"a\\\\b\"\n"
        "sl = \"a\\/b\"\n"
        "ctl = \"\\b\\f\\n\\r\\t\"\n"
        "e = \"\\u00e9\"\n"
        "emoji = \"\\uD83D\\uDE00\"\n"
        "lone = \"\\uD800\"\n"
        "loneLow = \"\\uDC00\"\n"
        "hindi = \"" + std::string(kHindi) + "\"\n"
        "A = \"\\u0041\"\n"
        "hash = \"v#x\"\n";
    TomlDocument doc = roundTrip("escapes", source);

    expectString("escapes/quote", doc, "s", "q", "a\"b");
    expectString("escapes/backslash", doc, "s", "bs", "a\\b");
    expectString("escapes/slash", doc, "s", "sl", "a/b");
    expectString("escapes/control", doc, "s", "ctl",
                 std::string("\b\f\n\r\t", 5));
    expectString("escapes/u00e9", doc, "s", "e", kEAcute);
    expectString("escapes/surrogate-pair", doc, "s", "emoji", kEmoji);
    expectString("escapes/lone-high-surrogate", doc, "s", "lone", kReplacement);
    expectString("escapes/lone-low-surrogate", doc, "s", "loneLow",
                 kReplacement);
    expectString("escapes/hindi-passthrough", doc, "s", "hindi", kHindi);
    expectString("escapes/u0041", doc, "s", "A", "A");
    expectString("escapes/hash-in-string", doc, "s", "hash", "v#x");

    // And back out again, byte for byte.
    checkEq("serialize/quote", TomlValue::string("a\"b").serialize(),
            "\"a\\\"b\"");
    checkEq("serialize/backslash", TomlValue::string("a\\b").serialize(),
            "\"a\\\\b\"");
    checkEq("serialize/newline", TomlValue::string("a\nb").serialize(),
            "\"a\\nb\"");
    checkEq("serialize/tab", TomlValue::string("a\tb").serialize(),
            "\"a\\tb\"");
    checkEq("serialize/carriage-return", TomlValue::string("a\rb").serialize(),
            "\"a\\rb\"");
    checkEq("serialize/control-below-0x20",
            TomlValue::string(std::string("\x01\x1f", 2)).serialize(),
            "\"\\u0001\\u001F\"");
    checkEq("serialize/nul", TomlValue::string(std::string("\0", 1)).serialize(),
            "\"\\u0000\"");
    checkEq("serialize/hindi-raw", TomlValue::string(kHindi).serialize(),
            "\"" + std::string(kHindi) + "\"");
    checkEq("serialize/emoji-raw", TomlValue::string(kEmoji).serialize(),
            "\"" + std::string(kEmoji) + "\"");
    checkEq("serialize/slash-not-escaped", TomlValue::string("a/b").serialize(),
            "\"a/b\"");
}

void testCommentsAndLineEndings() {
    const std::string source =
        "# leading comment\r\n"
        "\r\n"
        "[a] # comment after the header\r\n"
        "k = \"v#x\" # and after the value\r\n"
        "   \t \r\n"
        "bare = 1\r\n";
    TomlDocument doc = roundTrip("comments", source);
    expectString("comments/value", doc, "a", "k", "v#x");
    expectInteger("comments/second", doc, "a", "bare", 1);
    check("comments/no-stray-key", !doc.contains("a", "#"));

    // A '#' inside a literal string is data, not a comment.
    TomlDocument literal = roundTrip("comments/literal", "[a]\nk = 'x#y'\n");
    expectString("comments/literal-value", literal, "a", "k", "x#y");
}

void testLiteralStrings() {
    const std::string source =
        "[a]\n"
        "path = 'C:\\Users\\me'\n"
        "esc = 'no \\\"escapes\\\" here'\n"
        "empty = ''\n";
    TomlDocument doc = roundTrip("literal", source);
    expectString("literal/backslashes", doc, "a", "path", "C:\\Users\\me");
    expectString("literal/no-escapes", doc, "a", "esc", "no \\\"escapes\\\" here");
    expectString("literal/empty", doc, "a", "empty", "");
}

void testIntegers() {
    const std::string source =
        "[a]\n"
        "grouped = 1_000\n"
        "plus = +42\n"
        "minus = -7\n"
        "zero = 0\n"
        "big = 9223372036854775807\n"
        "small = -9223372036854775808\n";
    TomlDocument doc = roundTrip("integers", source);
    expectInteger("integers/underscore", doc, "a", "grouped", 1000);
    expectInteger("integers/plus", doc, "a", "plus", 42);
    expectInteger("integers/minus", doc, "a", "minus", -7);
    expectInteger("integers/zero", doc, "a", "zero", 0);
    expectInteger("integers/max", doc, "a", "big", 9223372036854775807LL);
    expectInteger("integers/min", doc, "a", "small", -9223372036854775807LL - 1);

    // Underscores are dropped on output: the value round-trips, the spelling
    // does not.
    check("integers/underscore-normalised",
          doc.serialize().find("grouped = 1000") != std::string::npos);
}

void testArrays() {
    const std::string source =
        "[a]\n"
        "spaced = [ ]\n"
        "compact = []\n"
        "multi = [\n"
        "  \"x\", # first\n"
        "  \"y\",\n"
        "]\n"
        "trailing = [\"c\",]\n"
        "escaped = [\"a\\tb\", \"c\"]\n"
        "hindi = [\"" + std::string(kHindi) + "\", \"Sarvam AI\"]\n";
    TomlDocument doc = roundTrip("arrays", source);
    expectArray("arrays/spaced-empty", doc, "a", "spaced", {});
    expectArray("arrays/compact-empty", doc, "a", "compact", {});
    expectArray("arrays/multi-line", doc, "a", "multi", {"x", "y"});
    expectArray("arrays/trailing-comma", doc, "a", "trailing", {"c"});
    expectArray("arrays/escaped-element", doc, "a", "escaped",
                {std::string("a\tb"), "c"});
    expectArray("arrays/hindi-element", doc, "a", "hindi",
                {std::string(kHindi), "Sarvam AI"});

    // Multi-line arrays are written back on one line, which still parses to the
    // same document.
    check("arrays/multi-line-normalised",
          doc.serialize().find("multi = [\"x\", \"y\"]") != std::string::npos);

    // The frozen API stores elements as std::string, so a bool or an integer
    // element keeps its text form.
    TomlDocument mixed = roundTrip("arrays/mixed", "[a]\nm = [1, true]\n");
    expectArray("arrays/mixed-elements", mixed, "a", "m", {"1", "true"});
}

void testUnsupportedValues() {
    expectSkip("skip/float", "[a]\nx = 3.5\n");
    expectSkip("skip/date", "[a]\nx = 1979-05-27\n");
    expectSkip("skip/time", "[a]\nx = 07:32:00\n");
    expectSkip("skip/inline-table", "[a]\nx = {a = 1}\n");
    expectSkip("skip/nested-array", "[a]\nx = [[1]]\n");
    expectSkip("skip/unterminated-string", "[a]\nx = \"unterminated\n");
    expectSkip("skip/bad-underscore", "[a]\nx = 1__0\n");
    expectSkip("skip/trailing-underscore", "[a]\nx = 1_\n");
    expectSkip("skip/overflow", "[a]\nx = 99999999999999999999999\n");
    expectSkip("skip/bare-word", "[a]\nx = yes\n");
    expectSkip("skip/unknown-escape", "[a]\nx = \"a\\qb\"\n");
    expectSkip("skip/trailing-junk", "[a]\nx = \"v\" junk\n");
    expectSkip("skip/missing-value", "[a]\nx =\n");

    // A skipped key must not take the rest of the file with it.
    std::vector<std::string> warnings;
    TomlDocument doc = TomlDocument::parse(
        "[a]\nx = 3.5\ny = \"kept\"\n", &warnings);
    expectString("skip/neighbour-kept", doc, "a", "y", "kept");
    check("skip/one-warning", warnings.size() == 1);
    check("skip/warning-has-line-number",
          !warnings.empty() && warnings.front().find("line 2: ") == 0);
}

void testDottedHeader() {
    std::vector<std::string> warnings;
    const std::string source =
        "[a.b]\n"
        "k = \"dropped\"\n"
        "[c]\n"
        "j = \"kept\"\n";
    TomlDocument doc = TomlDocument::parse(source, &warnings);
    expectMissing("dotted/key-dropped", doc, "a.b", "k");
    expectMissing("dotted/no-flattening", doc, "a", "b");
    expectString("dotted/next-section-kept", doc, "c", "j", "kept");
    check("dotted/one-warning", warnings.size() == 1);

    // The dropped keys must not leak into the preceding section either.
    std::vector<std::string> after;
    TomlDocument doc2 = TomlDocument::parse(
        "[z]\nq = 1\n[a.b]\nk = 2\n", &after);
    expectMissing("dotted/no-leak", doc2, "z", "k");
    expectInteger("dotted/before-kept", doc2, "z", "q", 1);
}

void testRootSection() {
    const std::string source =
        "k = \"v\"\n"
        "n = 1\n"
        "\n"
        "[a]\n"
        "j = \"w\"\n";
    TomlDocument doc = roundTrip("root", source);
    expectString("root/value", doc, "", "k", "v");
    expectInteger("root/integer", doc, "", "n", 1);
    expectString("root/section", doc, "a", "j", "w");

    const std::string text = doc.serialize();
    check("root/no-empty-header", text.find("[]") == std::string::npos);
    check("root/header-first", text.compare(0, std::string(kHeaderLine1).size(),
                                            kHeaderLine1) == 0);
    check("root/keys-before-any-header",
          text.find("k = \"v\"") < text.find("[a]"));

    // A root section created after other sections must still be written first,
    // or its keys would be read back as members of the last section.
    TomlDocument late;
    late.set("api", "k", TomlValue::string("v"));
    late.set("", "root", TomlValue::integer(1));
    const std::string lateText = late.serialize();
    std::vector<std::string> warnings;
    TomlDocument reparsed = TomlDocument::parse(lateText, &warnings);
    check("root/late-no-warnings", warnings.empty());
    expectInteger("root/late-key", reparsed, "", "root", 1);
    expectString("root/late-other-section", reparsed, "api", "k", "v");
    expectMissing("root/late-not-in-wrong-section", reparsed, "api", "root");
    checkEq("root/late-stable", reparsed.serialize(), lateText);
}

void testDuplicateKey() {
    std::vector<std::string> warnings;
    TomlDocument doc = TomlDocument::parse("[a]\nk = 1\nk = 2\n", &warnings);
    expectInteger("duplicate/last-wins", doc, "a", "k", 2);
    check("duplicate/no-warning", warnings.empty());
    // Replacing in place keeps the original position, so the file does not
    // shuffle under the user.
    check("duplicate/one-entry",
          doc.serialize().find("k = 2\n") != std::string::npos &&
              doc.serialize().find("k = 1") == std::string::npos);
}

void testPreservation() {
    const std::string source =
        "[api]\n"
        "key = \"old\"\n"
        "future_thing = \"keep me\"\n"
        "\n"
        "[unknown]\n"
        "zz = 1\n"
        "\n"
        "[empty_placeholder]\n";
    TomlDocument doc = roundTrip("preserve", source);
    doc.set("api", "key", TomlValue::string("new"));
    const std::string text = doc.serialize();

    std::vector<std::string> warnings;
    TomlDocument reloaded = TomlDocument::parse(text, &warnings);
    check("preserve/no-warnings", warnings.empty());
    expectString("preserve/edited", reloaded, "api", "key", "new");
    expectString("preserve/unknown-key", reloaded, "api", "future_thing",
                 "keep me");
    expectInteger("preserve/unknown-section", reloaded, "unknown", "zz", 1);
    check("preserve/empty-section-header",
          text.find("[empty_placeholder]") != std::string::npos);
    checkEq("preserve/stable", reloaded.serialize(), text);
}

void testQuotedNames() {
    const std::string source =
        "[\"api\"]\n"
        "\"key\" = \"v\"\n"
        "\"a b\" = \"sp\"\n"
        "'lit' = \"w\"\n";
    TomlDocument doc = roundTrip("quoted", source);
    expectString("quoted/section", doc, "api", "key", "v");
    expectString("quoted/key-with-space", doc, "api", "a b", "sp");
    expectString("quoted/literal-key", doc, "api", "lit", "w");

    // A name that is not a legal bare key has to be quoted again on output.
    const std::string text = doc.serialize();
    check("quoted/key-requoted", text.find("\"a b\" = \"sp\"") != std::string::npos);
    check("quoted/bare-key-stays-bare",
          text.find("key = \"v\"") != std::string::npos);

    // A quoted section name containing a dot is a literal name, not a dotted
    // path, and survives.
    TomlDocument dotted = roundTrip("quoted/dot-section", "[\"a.b\"]\nk = \"v\"\n");
    expectString("quoted/dot-section-value", dotted, "a.b", "k", "v");
    check("quoted/dot-section-requoted",
          dotted.serialize().find("[\"a.b\"]") != std::string::npos);
}

void testSerializeShape() {
    TomlDocument doc;
    doc.set("api", "key", TomlValue::string(""));
    doc.set("audio", "device", TomlValue::string(""));
    const std::string want = std::string(kHeaderLine1) + "\n" + kHeaderLine2 +
                             "\n[api]\nkey = \"\"\n\n[audio]\ndevice = \"\"\n";
    checkEq("shape/exact-bytes", doc.serialize(), want);
    check("shape/blank-line-between-sections",
          doc.serialize().find("\n\n[audio]") != std::string::npos);
    check("shape/single-space-around-equals",
          doc.serialize().find("key = \"\"") != std::string::npos);

    TomlDocument emptyDoc;
    checkEq("shape/empty-document",
            emptyDoc.serialize(),
            std::string(kHeaderLine1) + "\n" + kHeaderLine2 + "\n");
    check("shape/empty-flag", emptyDoc.empty());
    std::vector<std::string> warnings;
    TomlDocument reparsed = TomlDocument::parse(emptyDoc.serialize(), &warnings);
    check("shape/empty-reparses-empty", reparsed.empty() && warnings.empty());
}

void testDocumentApi() {
    TomlDocument doc;
    check("api/empty-initially", doc.empty());
    check("api/get-missing", doc.get("a", "b") == nullptr);
    check("api/contains-missing", !doc.contains("a", "b"));

    doc.set("", "a", TomlValue::integer(1));
    doc.set("", "b", TomlValue::integer(2));
    doc.set("", "a", TomlValue::integer(3)); // replace, keep position
    checkEq("api/set-order", doc.serialize(),
            std::string(kHeaderLine1) + "\n" + kHeaderLine2 + "\na = 3\nb = 2\n");
    check("api/not-empty", !doc.empty());

    doc.set("z", "k", TomlValue::integer(1));
    doc.set("a", "j", TomlValue::integer(2));
    doc.set("m", "i", TomlValue::integer(3));
    // Sections keep insertion order, root first.
    const std::string text = doc.serialize();
    check("api/section-order",
          text.find("[z]") < text.find("[a]") && text.find("[a]") < text.find("[m]"));
    check("api/root-first", text.find("a = 3") < text.find("[z]"));

    doc.remove("a", "j");
    check("api/remove", !doc.contains("a", "j"));
    check("api/remove-missing-section", !doc.contains("nosuch", "x"));
    doc.remove("nosuch", "x"); // must not crash or create anything
    // The emptied section keeps its header, so the user's file is not rewritten
    // more than the edit requires.
    check("api/section-header-survives-removal",
          doc.serialize().find("[a]") != std::string::npos);

    TomlDocument small;
    small.set("", "a", TomlValue::integer(1));
    check("api/not-empty", !small.empty());
    small.remove("", "a");
    check("api/empty-after-removal", small.empty());
    checkEq("api/empty-serializes-header-only", small.serialize(),
            std::string(kHeaderLine1) + "\n" + kHeaderLine2 + "\n");
}

void testRealisticFile() {
    // The shape the daemon actually reads, with a Hindi keyterm and a comment
    // inside a multi-line array.
    const std::string source =
        "# Written by hand\n"
        "\n"
        "[api]\n"
        "key = \"YOUR_SARVAM_API_KEY\"\n"
        "model = \"saaras:v4\"\n"
        "mode = \"transcribe\"\n"
        "language_code = \"hi-IN\"\n"
        "keyterms = [\n"
        "  \"" + std::string(kHindi) + "\",   # namaste\n"
        "  \"Sarvam AI\",\n"
        "]\n"
        "\n"
        "[audio]\n"
        "device = \"\"\n"
        "sample_rate = 16000\n"
        "backend = \"auto\"\n"
        "\n"
        "[server]\n"
        "socket_path = \"\"\n"
        "max_duration_s = 300\n";
    std::vector<std::string> warnings;
    TomlDocument doc = TomlDocument::parse(source, &warnings);
    check("real/no-warnings", warnings.empty());
    expectString("real/key", doc, "api", "key", "YOUR_SARVAM_API_KEY");
    expectString("real/model", doc, "api", "model", "saaras:v4");
    expectString("real/language", doc, "api", "language_code", "hi-IN");
    expectArray("real/keyterms", doc, "api", "keyterms",
                {std::string(kHindi), "Sarvam AI"});
    expectInteger("real/sample-rate", doc, "audio", "sample_rate", 16000);
    expectString("real/backend", doc, "audio", "backend", "auto");
    expectInteger("real/max-duration", doc, "server", "max_duration_s", 300);

    // What saveDaemonConfig does: touch the managed keys, leave the rest alone.
    doc.set("api", "key", TomlValue::string("YOUR_OTHER_PLACEHOLDER"));
    doc.set("api", "keyterms",
            TomlValue::array(std::vector<std::string>{std::string(kHindi)}));
    const std::string text = doc.serialize();
    std::vector<std::string> again;
    TomlDocument reloaded = TomlDocument::parse(text, &again);
    check("real/reload-no-warnings", again.empty());
    expectString("real/reload-key", reloaded, "api", "key",
                 "YOUR_OTHER_PLACEHOLDER");
    expectArray("real/reload-keyterms", reloaded, "api", "keyterms",
                {std::string(kHindi)});
    expectString("real/reload-untouched", reloaded, "audio", "backend", "auto");
    checkEq("real/stable", reloaded.serialize(), text);
}

void testParseNeverThrows() {
    // Garbage in, empty (or partial) document out, never an exception.
    const char *inputs[] = {
        "",
        "\n\n\n",
        "[",
        "]",
        "[]",
        "= 1",
        "a =",
        "a = [",
        "a = \"",
        "a = '",
        "a = [\"x\",",
        "[\"unterminated]\n",
        "[[a]]\n",
        "a = = b\n",
        "\xff\xfe binary garbage \x00\x01\n",
        "a = \"\\u12\"\n",
        "a = \"\\uD83D\"\n",
        "a = \"\\uD83D\\u0041\"\n",
    };
    for (const char *input : inputs) {
        std::vector<std::string> warnings;
        TomlDocument doc = TomlDocument::parse(input, &warnings);
        const std::string text = doc.serialize();
        if (text.empty() || text.back() != '\n') {
            fail("robust/newline", std::string("input [") + input +
                                       "] serialized without a trailing "
                                       "newline");
        }
        std::vector<std::string> ignored;
        TomlDocument again = TomlDocument::parse(text, &ignored);
        checkEq("robust/stable", again.serialize(), text);
    }

    // A null warnings pointer must be accepted.
    TomlDocument doc = TomlDocument::parse("a = 1\n", nullptr);
    expectInteger("robust/null-warnings", doc, "", "a", 1);
}

} // namespace

int main() {
    testTypesRoundTrip();
    testEscapes();
    testCommentsAndLineEndings();
    testLiteralStrings();
    testIntegers();
    testArrays();
    testUnsupportedValues();
    testDottedHeader();
    testRootSection();
    testDuplicateKey();
    testPreservation();
    testQuotedNames();
    testSerializeShape();
    testDocumentApi();
    testRealisticFile();
    testParseNeverThrows();

    if (failures != 0) {
        std::printf("%d failure(s)\n", failures);
        return 1;
    }
    std::printf("all tomlfile checks passed\n");
    return 0;
}
