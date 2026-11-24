/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * Unit tests for the engine split of the configuration surface: the two engine
 * sub-configurations, their marshalling into config.toml, and the description
 * the config page is built from.
 *
 * No test framework on purpose: the project has no test dependency, and a plain
 * main() that prints one line per failure is enough. Run it directly (it exits
 * non-zero when anything fails) or through CTest.
 *
 * The include path is relative so the file compiles whether or not the build
 * puts src/ on the include path.
 *
 * Unlike the other tests in this directory this one needs fcitx5: the thing
 * under test is the marshalling between the fcitx5 options and the TOML file,
 * so the target compiles ../../src/sarvamdictationconfig.cpp and
 * ../../src/tomlfile.cpp next to this file and links Fcitx5::Config and
 * Fcitx5::Utils.
 *
 * What is NOT covered here: AddonInstance::getSubConfig()/setSubConfig(). They
 * are members of SarvamDictation, which cannot be constructed without a live
 * fcitx::Instance (event loop, addon manager, input contexts), and this tree
 * has no harness for that. What those two methods do -- answer both names, load
 * the page partially, write the file -- is tested below through the objects and
 * functions they delegate to; the D-Bus plumbing itself is exercised by hand.
 */
#include "../../src/sarvamdictationconfig.h"
#include "../../src/tomlfile.h"

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include <sys/stat.h>
#include <unistd.h>

using fcitx::RawConfig;
using fcitx::sarvam::DictateSettings;
using fcitx::sarvam::DictateStreamType;
using fcitx::sarvam::DictateStyle;
using fcitx::sarvam::DictationMode;
using fcitx::sarvam::DictationModel;
using fcitx::sarvam::Engine;
using fcitx::sarvam::SaarasSettings;
using fcitx::sarvam::SarvamDictationConfig;
using fcitx::sarvam::TomlDocument;
using fcitx::sarvam::TomlType;
using fcitx::sarvam::TomlValue;
using fcitx::sarvam::kDictateSubConfigUri;
using fcitx::sarvam::kSaarasSubConfigUri;
using fcitx::sarvam::loadDaemonConfig;
using fcitx::sarvam::saveDaemonConfig;

// A stand-in for the engine row of the main page: the same option type with the
// same annotation, in a Configuration of its own. The real
// SarvamDictationConfig cannot be dumped in this test, because its
// AudioDeviceConstrain::dumpDescription lives in src/audiodevices.cpp and a
// description dump of the whole page would have to link that file; the
// annotation is the only thing under test here. Declared in the project's own
// namespace because FCITX_CONFIGURATION specialises a template there.
namespace fcitx::sarvam {
FCITX_CONFIGURATION(EngineRowForTest,
                    OptionWithAnnotation<Engine, EngineAnnotation> engine{
                        this, "Engine", _("Engine"), Engine::Saaras};);
} // namespace fcitx::sarvam

namespace {

int failures = 0;

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

void checkEqList(const std::string &name, const std::vector<std::string> &got,
                 const std::vector<std::string> &want) {
    if (got != want) {
        fail(name,
             "got [" + describe(got) + "] want [" + describe(want) + "]");
    }
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
    checkEqList(name, value->asArray(), want);
}

/// The value at `path` of a RawConfig description, or "" when it is absent.
std::string textAt(const RawConfig &raw, const char *path) {
    const std::string *value = raw.valueByPath(path);
    return value ? *value : std::string();
}

bool hasNode(const RawConfig &raw, const char *path) {
    return static_cast<bool>(raw.get(path));
}

// ---- temp directory helpers ----------------------------------------------

std::string makeTempDir() {
    const char *tmpdir = std::getenv("TMPDIR");
    const std::string base = (tmpdir != nullptr && tmpdir[0] != '\0')
                                 ? std::string(tmpdir)
                                 : std::string("/tmp");
    std::string pattern = base + "/sarvam-engine-XXXXXX";
    std::vector<char> buffer(pattern.begin(), pattern.end());
    buffer.push_back('\0');
    if (::mkdtemp(buffer.data()) == nullptr) {
        return std::string();
    }
    return std::string(buffer.data());
}

bool writeFile(const std::string &path, const std::string &content) {
    std::FILE *file = std::fopen(path.c_str(), "wb");
    if (file == nullptr) {
        return false;
    }
    const std::size_t written =
        std::fwrite(content.data(), 1, content.size(), file);
    const bool closed = std::fclose(file) == 0;
    return written == content.size() && closed;
}

std::string readFile(const std::string &path) {
    std::FILE *file = std::fopen(path.c_str(), "rb");
    if (file == nullptr) {
        return std::string();
    }
    std::string contents;
    char buffer[4096];
    std::size_t received = 0;
    while ((received = std::fread(buffer, 1, sizeof(buffer), file)) > 0) {
        contents.append(buffer, received);
    }
    std::fclose(file);
    return contents;
}

std::string configPath(const std::string &dir) { return dir + "/config.toml"; }

void cleanupTempDir(const std::string &dir) {
    ::unlink(configPath(dir).c_str());
    ::rmdir(dir.c_str());
}

// ---- the three pages ------------------------------------------------------

/// The three Configuration objects the addon keeps in memory, in the order the
/// marshalling functions take them: the main page, the Saaras page, the Dictate
/// page. The daemon writes one file out of all three.
struct Pages {
    SarvamDictationConfig main;
    SaarasSettings saaras;
    DictateSettings dictate;
};

bool load(Pages &pages, const std::string &path, std::string *error) {
    return loadDaemonConfig(pages.main, pages.saaras, pages.dictate, path,
                            error);
}

bool save(const Pages &pages, const std::string &path, std::string *error) {
    return saveDaemonConfig(pages.main, pages.saaras, pages.dictate, path,
                            error);
}

// ---- the tests ------------------------------------------------------------

void testDefaults() {
    Pages pages;
    check("defaults/engine",
          pages.main.engine.value() == Engine::Saaras);

    check("defaults/saaras-model",
          pages.saaras.model.value() == DictationModel::SaarasV4);
    check("defaults/saaras-keyterms", pages.saaras.keyterms.value().empty());
    check("defaults/saaras-push-to-talk",
          !pages.saaras.streamingPushToTalk.value());
    check("defaults/saaras-toggle", !pages.saaras.streamingToggle.value());

    check("defaults/dictate-style",
          pages.dictate.style.value() == DictateStyle::Unset);
    checkEq("defaults/dictate-rules", pages.dictate.rules.value(), "");
    check("defaults/dictate-vocabulary",
          pages.dictate.vocabulary.value().empty());
    checkEq("defaults/dictate-context", pages.dictate.context.value(), "");
    checkEq("defaults/dictate-reference", pages.dictate.reference.value(), "");
    checkEq("defaults/dictate-output-rules",
            pages.dictate.outputRules.value(), "");
    checkEq("defaults/dictate-model", pages.dictate.model.value(), "");
    check("defaults/dictate-stream-type",
          pages.dictate.streamType.value() == DictateStreamType::Balanced);
}

void testMissingFileResets(const std::string &dir) {
    // No file at all: the daemon runs on its built-in defaults, so every page
    // must show them, including the engine.
    Pages pages;
    pages.main.engine.setValue(Engine::Dictate);
    pages.dictate.rules.setValue("stale");
    pages.saaras.model.setValue(DictationModel::SaarasV3Realtime);

    std::string error;
    check("missing/load", load(pages, dir + "/absent.toml", &error));
    check("missing/error-empty", error.empty());
    check("missing/engine", pages.main.engine.value() == Engine::Saaras);
    checkEq("missing/rules", pages.dictate.rules.value(), "");
    check("missing/model",
          pages.saaras.model.value() == DictationModel::SaarasV4);
}

void testEngineRoundTrip(const std::string &dir) {
    const std::string path = configPath(dir);
    Pages pages;
    pages.main.apiKey.setValue("test-key");
    pages.main.engine.setValue(Engine::Dictate);

    std::string error;
    check("engine/save", save(pages, path, &error));
    checkEq("engine/save-error", error, "");

    const TomlDocument written = TomlDocument::parse(readFile(path));
    expectString("engine/file", written, "api", "engine", "dictate");
    // The key is the shared one and stays where it was.
    expectString("engine/key", written, "api", "key", "test-key");

    Pages loaded;
    check("engine/load", load(loaded, path, &error));
    check("engine/value", loaded.main.engine.value() == Engine::Dictate);

    // And back: the other stored name round-trips too.
    loaded.main.engine.setValue(Engine::Saaras);
    check("engine/save-saaras", save(loaded, path, &error));
    const TomlDocument again = TomlDocument::parse(readFile(path));
    expectString("engine/file-saaras", again, "api", "engine", "saaras");

    Pages reloaded;
    check("engine/reload", load(reloaded, path, &error));
    check("engine/reload-value",
          reloaded.main.engine.value() == Engine::Saaras);
}

void testDictateTableRoundTrip(const std::string &dir) {
    const std::string path = configPath(dir);
    Pages pages;
    pages.main.engine.setValue(Engine::Dictate);
    pages.dictate.style.setValue(DictateStyle::Light);
    pages.dictate.rules.setValue("Always use sentence case.");
    pages.dictate.vocabulary.setValue(
        std::vector<std::string>{"Sarvam", "Bengaluru"});
    pages.dictate.context.setValue("A meeting about the dictation addon.");
    pages.dictate.reference.setValue("Sarvam AI\nSuhan G Paradkar");
    pages.dictate.outputRules.setValue("No markdown.");
    pages.dictate.model.setValue("dictate-v1-2026-11-04");
    pages.dictate.streamType.setValue(DictateStreamType::Fast);

    std::string error;
    check("dictate/save", save(pages, path, &error));
    checkEq("dictate/save-error", error, "");

    const TomlDocument written = TomlDocument::parse(readFile(path));
    expectString("dictate/file-style", written, "dictate", "style", "light");
    expectString("dictate/file-rules", written, "dictate", "rules",
                 "Always use sentence case.");
    expectArray("dictate/file-vocabulary", written, "dictate", "vocabulary",
                std::vector<std::string>{"Sarvam", "Bengaluru"});
    expectString("dictate/file-context", written, "dictate", "context",
                 "A meeting about the dictation addon.");
    expectString("dictate/file-reference", written, "dictate", "reference",
                 "Sarvam AI\nSuhan G Paradkar");
    expectString("dictate/file-output-rules", written, "dictate",
                 "output_rules", "No markdown.");
    expectString("dictate/file-model", written, "dictate", "model",
                 "dictate-v1-2026-11-04");
    expectString("dictate/file-stream-type", written, "dictate", "stream_type",
                 "fast");

    Pages loaded;
    check("dictate/load", load(loaded, path, &error));
    check("dictate/style", loaded.dictate.style.value() == DictateStyle::Light);
    checkEq("dictate/rules", loaded.dictate.rules.value(),
            "Always use sentence case.");
    checkEqList("dictate/vocabulary", loaded.dictate.vocabulary.value(),
                std::vector<std::string>{"Sarvam", "Bengaluru"});
    checkEq("dictate/context", loaded.dictate.context.value(),
            "A meeting about the dictation addon.");
    checkEq("dictate/reference", loaded.dictate.reference.value(),
            "Sarvam AI\nSuhan G Paradkar");
    checkEq("dictate/output-rules", loaded.dictate.outputRules.value(),
            "No markdown.");
    checkEq("dictate/model", loaded.dictate.model.value(),
            "dictate-v1-2026-11-04");
    check("dictate/stream-type",
          loaded.dictate.streamType.value() == DictateStreamType::Fast);
    check("dictate/engine",
          loaded.main.engine.value() == Engine::Dictate);

    // load -> save -> load is stable: the second write is byte-identical.
    const std::string first = readFile(path);
    check("dictate/resave", save(loaded, path, &error));
    checkEq("dictate/stable", readFile(path), first);
}

void testVocabularySerialisesAsStrings(const std::string &dir) {
    const std::string path = configPath(dir);
    Pages pages;
    pages.saaras.keyterms.setValue(
        std::vector<std::string>{"saaras", "dictation"});
    pages.dictate.vocabulary.setValue(
        std::vector<std::string>{"Sarvam", "Bengaluru", "with \"quotes\""});

    std::string error;
    check("vocabulary/save", save(pages, path, &error));
    const std::string text = readFile(path);
    // A TOML array of quoted strings, not the indexed children fcitx5 uses
    // internally, and not a bare word.
    check("vocabulary/array-shape",
          text.find("vocabulary = [\"Sarvam\", \"Bengaluru\", "
                    "\"with \\\"quotes\\\"\"]") != std::string::npos);
    check("vocabulary/keyterms-shape",
          text.find("keyterms = [\"saaras\", \"dictation\"]") !=
              std::string::npos);

    const TomlDocument written = TomlDocument::parse(text);
    expectArray("vocabulary/parsed", written, "dictate", "vocabulary",
                std::vector<std::string>{"Sarvam", "Bengaluru",
                                         "with \"quotes\""});

    Pages loaded;
    check("vocabulary/load", load(loaded, path, &error));
    checkEqList("vocabulary/round-trip", loaded.dictate.vocabulary.value(),
                std::vector<std::string>{"Sarvam", "Bengaluru",
                                         "with \"quotes\""});
    checkEqList("vocabulary/keyterms-round-trip",
                loaded.saaras.keyterms.value(),
                std::vector<std::string>{"saaras", "dictation"});

    // An empty list is still written, so clearing it in the page clears it in
    // the file instead of leaving the old entries behind.
    Pages cleared;
    check("vocabulary/cleared-save", save(cleared, path, &error));
    check("vocabulary/cleared-shape",
          readFile(path).find("vocabulary = []") != std::string::npos);
}

void testStyleSpellings(const std::string &dir) {
    const std::string path = configPath(dir);

    // "No preference" is the empty string in the file, the `unset` entry in the
    // combo.
    Pages pages;
    std::string error;
    check("style/unset-save", save(pages, path, &error));
    const TomlDocument written = TomlDocument::parse(readFile(path));
    expectString("style/unset-file", written, "dictate", "style", "");

    // A hand-written empty string loads as `unset` ...
    Pages fromEmpty;
    check("style/empty-load", load(fromEmpty, path, &error));
    check("style/empty-value",
          fromEmpty.dictate.style.value() == DictateStyle::Unset);

    // ... and so does the combo's own spelling.
    check("style/hand-written",
          writeFile(path, "[dictate]\nstyle = \"unset\"\n"));
    Pages fromName;
    check("style/name-load", load(fromName, path, &error));
    check("style/name-value",
          fromName.dictate.style.value() == DictateStyle::Unset);

    check("style/clean", writeFile(path, "[dictate]\nstyle = \"clean\"\n"));
    Pages fromClean;
    check("style/clean-load", load(fromClean, path, &error));
    check("style/clean-value",
          fromClean.dictate.style.value() == DictateStyle::Clean);

    // A value the option does not know is a load error, not a silent reset:
    // Configuration::load would put the option back to its default instead.
    check("style/bogus", writeFile(path, "[dictate]\nstyle = \"shouty\"\n"));
    Pages fromBogus;
    fromBogus.dictate.style.setValue(DictateStyle::Clean);
    std::string bogusError;
    check("style/bogus-load", !load(fromBogus, path, &bogusError));
    check("style/bogus-error",
          bogusError.find("dictate.style") != std::string::npos);
    check("style/bogus-untouched",
          fromBogus.dictate.style.value() == DictateStyle::Clean);
}

void testPartialLoadKeepsWhatWasNotSent() {
    // What setConfig() receives after the main page is applied: only the rows
    // of the page that was open. Everything else has to survive untouched,
    // because the other engine's page was never shown and sent nothing.
    Pages pages;
    pages.main.engine.setValue(Engine::Dictate);
    pages.main.mode.setValue(DictationMode::Verbatim);
    pages.saaras.model.setValue(DictationModel::SaarasV3Realtime);
    pages.dictate.rules.setValue("keep me");
    pages.dictate.vocabulary.setValue(std::vector<std::string>{"Sarvam"});

    RawConfig sent;
    sent.setValueByPath("ApiKey", "typed-key");

    pages.main.load(sent, /*partial=*/true);
    check("partial/engine", pages.main.engine.value() == Engine::Dictate);
    check("partial/mode", pages.main.mode.value() == DictationMode::Verbatim);
    checkEq("partial/api-key", pages.main.apiKey.value(), "typed-key");

    pages.saaras.load(sent, /*partial=*/true);
    check("partial/saaras-model",
          pages.saaras.model.value() == DictationModel::SaarasV3Realtime);

    pages.dictate.load(sent, /*partial=*/true);
    checkEq("partial/rules", pages.dictate.rules.value(), "keep me");
    checkEqList("partial/vocabulary", pages.dictate.vocabulary.value(),
                std::vector<std::string>{"Sarvam"});

    // The contrast that makes partial=true load-bearing: a full load resets
    // every option the RawConfig does not mention (configuration.cpp:109-118).
    SarvamDictationConfig full;
    full.engine.setValue(Engine::Dictate);
    full.mode.setValue(DictationMode::Verbatim);
    full.load(sent);
    check("partial/full-resets-engine",
          full.engine.value() == Engine::Saaras);
    check("partial/full-resets-mode",
          full.mode.value() == DictationMode::Transcribe);
}

void testAbsentDictateTable(const std::string &dir) {
    const std::string path = configPath(dir);
    check("absent/write",
          writeFile(path, "[api]\nkey = \"k\"\nengine = \"dictate\"\n"));

    // A fresh page keeps its defaults for every key the file does not mention.
    Pages fresh;
    std::string error;
    check("absent/load", load(fresh, path, &error));
    check("absent/engine", fresh.main.engine.value() == Engine::Dictate);
    check("absent/style", fresh.dictate.style.value() == DictateStyle::Unset);
    checkEq("absent/rules", fresh.dictate.rules.value(), "");
    check("absent/vocabulary", fresh.dictate.vocabulary.value().empty());
    check("absent/stream-type",
          fresh.dictate.streamType.value() == DictateStreamType::Balanced);
    // The Saaras page is untouched by a file without its keys too.
    check("absent/saaras-model",
          fresh.saaras.model.value() == DictationModel::SaarasV4);

    // And a page that already holds values keeps them: an absent table is
    // "not mentioned", not "reset" (the load is partial).
    Pages held;
    held.dictate.rules.setValue("still here");
    held.dictate.streamType.setValue(DictateStreamType::Simulated);
    check("absent/held-load", load(held, path, &error));
    checkEq("absent/held-rules", held.dictate.rules.value(), "still here");
    check("absent/held-stream-type",
          held.dictate.streamType.value() == DictateStreamType::Simulated);
}

void testHandWrittenTableSurvivesApply(const std::string &dir) {
    const std::string path = configPath(dir);
    const std::string source =
        "# written by hand\n"
        "[api]\n"
        "key = \"hand-key\"\n"
        "engine = \"dictate\"\n"
        "future_api_key = \"from a newer daemon\"\n"
        "\n"
        "[dictate]\n"
        "style = \"clean\"\n"
        "rules = \"keep my rules\"\n"
        "vocabulary = [\"Sarvam\", \"dictate\"]\n"
        "context = \"one line\"\n"
        "reference = \"\"\n"
        "output_rules = \"no markdown\"\n"
        "model = \"\"\n"
        "stream_type = \"simulated\"\n"
        "future_key = \"from a newer daemon\"\n";
    check("hand/write", writeFile(path, source));

    Pages pages;
    std::string error;
    check("hand/load", load(pages, path, &error));
    checkEq("hand/key", pages.main.apiKey.value(), "hand-key");
    check("hand/engine", pages.main.engine.value() == Engine::Dictate);
    check("hand/style", pages.dictate.style.value() == DictateStyle::Clean);
    checkEq("hand/rules", pages.dictate.rules.value(), "keep my rules");
    checkEqList("hand/vocabulary", pages.dictate.vocabulary.value(),
                std::vector<std::string>{"Sarvam", "dictate"});
    checkEq("hand/context", pages.dictate.context.value(), "one line");
    checkEq("hand/output-rules", pages.dictate.outputRules.value(),
            "no markdown");
    checkEq("hand/model", pages.dictate.model.value(), "");
    check("hand/stream-type",
          pages.dictate.streamType.value() == DictateStreamType::Simulated);

    // An Apply writes the whole file back.
    check("hand/save", save(pages, path, &error));
    const std::string applied = readFile(path);
    const TomlDocument written = TomlDocument::parse(applied);
    expectString("hand/kept-key", written, "api", "key", "hand-key");
    expectString("hand/kept-engine", written, "api", "engine", "dictate");
    expectString("hand/kept-style", written, "dictate", "style", "clean");
    expectString("hand/kept-rules", written, "dictate", "rules",
                 "keep my rules");
    expectArray("hand/kept-vocabulary", written, "dictate", "vocabulary",
                std::vector<std::string>{"Sarvam", "dictate"});
    expectString("hand/kept-context", written, "dictate", "context",
                 "one line");
    expectString("hand/kept-output-rules", written, "dictate", "output_rules",
                 "no markdown");
    expectString("hand/kept-stream-type", written, "dictate", "stream_type",
                 "simulated");
    // Keys this build does not know are not ours to drop.
    expectString("hand/kept-api-unknown", written, "api", "future_api_key",
                 "from a newer daemon");
    expectString("hand/kept-unknown", written, "dictate", "future_key",
                 "from a newer daemon");

    // A second load -> save cycle changes nothing.
    Pages again;
    check("hand/reload", load(again, path, &error));
    check("hand/resave", save(again, path, &error));
    checkEq("hand/stable", readFile(path), applied);
}

void testDescription() {
    // The engine row, as fcitx5-configtool sees it. The frontend shows the
    // Configure button only for an enum value whose SubConfigPath/<i> is set
    // (optionwidget.cpp:344-363, EnumOption.qml:81-89), and the index is the
    // enum's own order: Enum/0 = saaras -> SubConfigPath/0.
    EngineRowForTest row;
    RawConfig desc;
    row.dumpDescription(desc);

    checkEq("desc/sub-config-saaras",
            textAt(desc, "EngineRowForTest/Engine/SubConfigPath/0"),
            kSaarasSubConfigUri);
    checkEq("desc/sub-config-dictate",
            textAt(desc, "EngineRowForTest/Engine/SubConfigPath/1"),
            kDictateSubConfigUri);
    checkEq("desc/launch-sub-config",
            textAt(desc, "EngineRowForTest/Engine/LaunchSubConfig"), "True");
    checkEq("desc/enum-0", textAt(desc, "EngineRowForTest/Engine/Enum/0"),
            "saaras");
    checkEq("desc/enum-1", textAt(desc, "EngineRowForTest/Engine/Enum/1"),
            "dictate");
    check("desc/enum-i18n-0",
          !textAt(desc, "EngineRowForTest/Engine/EnumI18n/0").empty());
    check("desc/enum-i18n-1",
          !textAt(desc, "EngineRowForTest/Engine/EnumI18n/1").empty());
    checkEq("desc/default-value",
            textAt(desc, "EngineRowForTest/Engine/DefaultValue"), "saaras");

    // The Saaras page holds the options that used to be on the main page. That
    // they are gone from the main page is enforced by the compiler -- the
    // SarvamDictationConfig members no longer exist -- rather than by a runtime
    // check; what a runtime check can still show is that each page carries its
    // own rows.
    SaarasSettings saaras;
    RawConfig saarasDesc;
    saaras.dumpDescription(saarasDesc);
    check("desc/saaras-model", hasNode(saarasDesc, "SaarasSettings/Model"));
    check("desc/saaras-keyterms",
          hasNode(saarasDesc, "SaarasSettings/Keyterms"));
    check("desc/saaras-push-to-talk",
          hasNode(saarasDesc, "SaarasSettings/StreamingPushToTalk"));
    check("desc/saaras-toggle",
          hasNode(saarasDesc, "SaarasSettings/StreamingToggle"));
    // The shared rows are not repeated on an engine page.
    check("desc/saaras-no-engine",
          !hasNode(saarasDesc, "SaarasSettings/Engine"));
    check("desc/saaras-no-api-key",
          !hasNode(saarasDesc, "SaarasSettings/ApiKey"));
    check("desc/saaras-no-mode",
          !hasNode(saarasDesc, "SaarasSettings/Mode"));
    checkEq("desc/saaras-model-enum-0",
            textAt(saarasDesc, "SaarasSettings/Model/Enum/0"), "saaras:v4");

    DictateSettings dictate;
    RawConfig dictateDesc;
    dictate.dumpDescription(dictateDesc);
    check("desc/dictate-style", hasNode(dictateDesc, "DictateSettings/Style"));
    check("desc/dictate-rules", hasNode(dictateDesc, "DictateSettings/Rules"));
    check("desc/dictate-vocabulary",
          hasNode(dictateDesc, "DictateSettings/Vocabulary"));
    check("desc/dictate-context",
          hasNode(dictateDesc, "DictateSettings/Context"));
    check("desc/dictate-reference",
          hasNode(dictateDesc, "DictateSettings/Reference"));
    check("desc/dictate-output-rules",
          hasNode(dictateDesc, "DictateSettings/OutputRules"));
    check("desc/dictate-model", hasNode(dictateDesc, "DictateSettings/Model"));
    check("desc/dictate-stream-type",
          hasNode(dictateDesc, "DictateSettings/StreamType"));
    // The style combo has a labelled entry for "no preference", whose stored
    // name is `unset` and whose file spelling is "".
    checkEq("desc/dictate-style-enum-0",
            textAt(dictateDesc, "DictateSettings/Style/Enum/0"), "unset");
    check("desc/dictate-style-i18n-0",
          !textAt(dictateDesc, "DictateSettings/Style/EnumI18n/0").empty());
    checkEq("desc/dictate-stream-type-enum-1",
            textAt(dictateDesc, "DictateSettings/StreamType/Enum/1"),
            "balanced");
    // The Dictate page offers no `translate` mode: mode is a shared row, and
    // the Dictate page does not repeat it at all.
    check("desc/dictate-no-mode",
          !hasNode(dictateDesc, "DictateSettings/Mode"));
}

} // namespace

int main() {
    const std::string tmp = makeTempDir();
    if (tmp.empty()) {
        std::printf("FAIL tempdir :: could not create a temporary directory\n");
        return 1;
    }

    testDefaults();
    testDescription();
    testMissingFileResets(tmp);
    testEngineRoundTrip(tmp);
    testDictateTableRoundTrip(tmp);
    testVocabularySerialisesAsStrings(tmp);
    testStyleSpellings(tmp);
    testPartialLoadKeepsWhatWasNotSent();
    testAbsentDictateTable(tmp);
    testHandWrittenTableSurvivesApply(tmp);

    cleanupTempDir(tmp);

    if (failures != 0) {
        std::printf("%d failure(s)\n", failures);
        return 1;
    }
    std::printf("all engine config checks passed\n");
    return 0;
}
