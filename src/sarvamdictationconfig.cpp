/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * Reading and writing the daemon's config.toml from the addon.
 *
 * The mapping between the fcitx5 options and the TOML file is done through
 * fcitx5's own marshalling rather than by hand, because the two sides disagree
 * about the textual form of almost every type:
 *
 *   option type                RawConfig (fcitx5)        TOML
 *   -------------------------  ------------------------  -----------------
 *   std::string                the string                the string
 *   bool                       "True" / "False"          true / false
 *   int                        "300"                     integer 300
 *   enum (FCITX_CONFIG_ENUM)   the stored name           the stored name
 *   std::vector<std::string>   children "0", "1" etc.    an array
 *
 * The enum, integer and string rows are handed to Configuration::load()/save()
 * so those rules are fcitx5's, not a second copy of them. Two rows need explicit
 * conversion here: the string list (array versus indexed children) and the bool,
 * because `[api] streaming` is the one daemon-owned boolean -- TOML spells it
 * true/false while RawConfig spells it "True"/"False". The addon's own booleans
 * still live in the fcitx5 conf and never reach this file.
 *
 * Two behaviours of Configuration::load() drive the design:
 *
 *  - With partial = true an option that is absent from the RawConfig is left
 *    alone, so building a RawConfig that holds only the ten daemon options
 *    cannot disturb the addon options.
 *  - An option whose value fails to unmarshall is RESET TO ITS DEFAULT, not
 *    left alone. A typo in the TOML file would therefore silently revert a
 *    setting, which is why every value is validated here first: a bad value is
 *    reported as a load error and the config is left untouched.
 */
#include "sarvamdictationconfig.h"

#include <cerrno>
#include <cstddef>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <fcntl.h>
#include <pwd.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include "fcitx-config/rawconfig.h"
#include "fcitx-utils/log.h"
#include "tomlfile.h"

namespace fcitx::sarvam {

FCITX_DEFINE_LOG_CATEGORY(sarvamdictationconfig_log, "sarvamdictationconfig");

namespace {

// The RawConfig paths of the daemon-owned options, i.e. the option names of the
// frozen table. They are used as the keys of the RawConfig that
// loadDaemonConfig builds and that saveDaemonConfig reads back.
constexpr char kApiKey[] = "ApiKey";
constexpr char kModel[] = "Model";
constexpr char kMode[] = "Mode";
constexpr char kStreaming[] = "Streaming";
constexpr char kLanguageCode[] = "LanguageCode";
constexpr char kKeyterms[] = "Keyterms";
constexpr char kAudioDevice[] = "AudioDevice";
constexpr char kSampleRate[] = "SampleRate";
constexpr char kBackend[] = "Backend";
constexpr char kSocketPath[] = "SocketPath";
constexpr char kMaxDurationS[] = "MaxDurationS";

// The bounds of the MaxDurationS option's IntConstrain, repeated here so that
// an out-of-range value can be reported by name instead of being rejected
// silently (Configuration::load would reset the option to its default). Keep in
// sync with the IntConstrain in sarvamdictationconfig.h.
constexpr int kMinDurationSeconds = 5;
constexpr int kMaxDurationSeconds = 3600;

void setError(std::string *error, const std::string &message) {
    if (error) {
        *error = message;
    }
}

std::string describeErrno(int errorCode) {
    return std::string(std::strerror(errorCode));
}

/// "a/b/c.toml" -> "a/b"; "c.toml" -> ""; "/c.toml" -> "/".
std::string directoryOf(const std::string &path) {
    const std::string::size_type slash = path.find_last_of('/');
    if (slash == std::string::npos) {
        return std::string();
    }
    if (slash == 0) {
        return "/";
    }
    return path.substr(0, slash);
}

/// Create `directory` and every missing parent, with mode 0700 (the directory
/// ends up holding an API key). Existing directories are left as they are.
bool makeDirectories(const std::string &directory, std::string *error) {
    if (directory.empty()) {
        return true;
    }
    std::string current;
    std::string::size_type position = 0;
    if (directory.front() == '/') {
        current = "/";
        position = 1;
    }
    while (position < directory.size()) {
        const std::string::size_type slash = directory.find('/', position);
        const std::string component =
            directory.substr(position, slash == std::string::npos
                                           ? std::string::npos
                                           : slash - position);
        if (!component.empty()) {
            if (current.empty()) {
                current = component;
            } else if (current == "/") {
                current += component;
            } else {
                current += '/';
                current += component;
            }
            if (::mkdir(current.c_str(), S_IRWXU) != 0 && errno != EEXIST) {
                setError(error, "cannot create directory " + current + ": " +
                                    describeErrno(errno));
                return false;
            }
        }
        if (slash == std::string::npos) {
            break;
        }
        position = slash + 1;
    }
    return true;
}

/// Read a whole file into `contents`. Reports the reason in `error` on failure.
bool readWholeFile(const std::string &path, std::string &contents,
                   std::string *error) {
    const int fd = ::open(path.c_str(), O_RDONLY);
    if (fd < 0) {
        setError(error, "cannot open " + path + ": " + describeErrno(errno));
        return false;
    }
    contents.clear();
    char buffer[8192];
    for (;;) {
        const ssize_t received = ::read(fd, buffer, sizeof(buffer));
        if (received > 0) {
            contents.append(buffer, static_cast<size_t>(received));
            continue;
        }
        if (received == 0) {
            break;
        }
        if (errno == EINTR) {
            continue;
        }
        const int readError = errno;
        ::close(fd);
        setError(error,
                 "cannot read " + path + ": " + describeErrno(readError));
        return false;
    }
    if (::close(fd) != 0) {
        setError(error, "cannot close " + path + ": " + describeErrno(errno));
        return false;
    }
    return true;
}

/// Replace `path` with `contents` atomically: a temporary file in the same
/// directory, fsync, then rename. The file ends up with mode 0600 whatever the
/// umask says, because it holds an API key.
bool writeAtomically(const std::string &path, const std::string &contents,
                     std::string *error) {
    const std::string directory = directoryOf(path);
    if (!directory.empty() && !makeDirectories(directory, error)) {
        return false;
    }

    std::vector<char> nameTemplate(path.begin(), path.end());
    static const char kSuffix[] = ".tmpXXXXXX";
    nameTemplate.insert(nameTemplate.end(), kSuffix, kSuffix + sizeof(kSuffix));

    const int fd = ::mkstemp(nameTemplate.data());
    if (fd < 0) {
        setError(error, "cannot create a temporary file next to " + path +
                            ": " + describeErrno(errno));
        return false;
    }
    const std::string temporaryPath(nameTemplate.data());

    bool ok = true;
    // mkstemp already creates the file with mode 0600; set it explicitly so a
    // restrictive umask cannot produce anything wider, and so the intent is
    // visible in the code rather than implied by the standard.
    if (::fchmod(fd, S_IRUSR | S_IWUSR) != 0) {
        setError(error, "cannot set the mode of " + temporaryPath + ": " +
                            describeErrno(errno));
        ok = false;
    }
    size_t offset = 0;
    while (ok && offset < contents.size()) {
        const ssize_t written = ::write(fd, contents.data() + offset,
                                        contents.size() - offset);
        if (written > 0) {
            offset += static_cast<size_t>(written);
            continue;
        }
        if (written < 0 && errno == EINTR) {
            continue;
        }
        setError(error, "cannot write " + temporaryPath + ": " +
                            describeErrno(written < 0 ? errno : EIO));
        ok = false;
    }
    if (ok && ::fsync(fd) != 0) {
        setError(error, "cannot flush " + temporaryPath + ": " +
                            describeErrno(errno));
        ok = false;
    }
    if (::close(fd) != 0 && ok) {
        setError(error, "cannot close " + temporaryPath + ": " +
                            describeErrno(errno));
        ok = false;
    }
    if (!ok) {
        (void)::unlink(temporaryPath.c_str());
        return false;
    }
    if (::rename(temporaryPath.c_str(), path.c_str()) != 0) {
        setError(error, "cannot replace " + path + ": " + describeErrno(errno));
        (void)::unlink(temporaryPath.c_str());
        return false;
    }
    // Best effort: make the rename itself durable too. A filesystem that cannot
    // fsync a directory has already accepted the rename, so this is not fatal.
    const int directoryFd =
        ::open(directory.empty() ? "." : directory.c_str(), O_RDONLY);
    if (directoryFd >= 0) {
        (void)::fsync(directoryFd);
        (void)::close(directoryFd);
    }
    return true;
}

/// Put the ten daemon options back to their defaults, leaving the addon options
/// alone. Used when the TOML file does not exist at all: the daemon then runs
/// on its built-in defaults, and the config page must show the same.
void resetDaemonOptions(SarvamDictationConfig &config) {
    config.apiKey.reset();
    config.model.reset();
    config.mode.reset();
    config.languageCode.reset();
    config.keyterms.reset();
    config.audioDevice.reset();
    config.sampleRate.reset();
    config.backend.reset();
    config.socketPath.reset();
    config.maxDurationS.reset();
}

/// TOML string -> RawConfig value of a std::string option.
bool applyString(const TomlDocument &document, const char *section,
                 const char *key, const char *option, RawConfig &raw,
                 std::string *error) {
    const TomlValue *value = document.get(section, key);
    if (!value) {
        return true;
    }
    if (value->type() != TomlType::String) {
        setError(error, std::string(section) + "." + key +
                            ": expected a string value");
        return false;
    }
    raw.setValueByPath(option, value->asString());
    return true;
}

/// TOML string -> RawConfig value of an enum option.
///
/// The stored name of an fcitx5 enum option IS its RawConfig value, so the TOML
/// string is handed to the very unmarshaller the option uses. A name the option
/// would reject is reported here instead of being handed to
/// Configuration::load(), which would silently reset the option to its default.
template <typename Enum>
bool applyEnum(const TomlDocument &document, const char *section,
               const char *key, const char *option, RawConfig &raw,
               std::string *error) {
    const TomlValue *value = document.get(section, key);
    if (!value) {
        return true;
    }
    if (value->type() != TomlType::String) {
        setError(error, std::string(section) + "." + key +
                            ": expected a string value");
        return false;
    }
    const std::string text = value->asString();
    RawConfig probe;
    probe.setValue(text);
    Enum parsed{};
    if (!unmarshallOption(parsed, probe, false)) {
        setError(error, std::string(section) + "." + key +
                            ": unknown value \"" + text + "\"");
        return false;
    }
    raw.setValueByPath(option, text);
    return true;
}

/// TOML language code -> the enum's stored name.
///
/// The daemon spells auto-detection as an empty `language_code`; the config page
/// spells it as the `auto` entry of the language list. Both spellings are
/// accepted on read so a hand-edited file cannot produce a value the daemon
/// would forward to the API verbatim.
bool applyLanguageCode(const TomlDocument &document, RawConfig &raw,
                       std::string *error) {
    const TomlValue *value = document.get("api", "language_code");
    if (!value) {
        return true;
    }
    if (value->type() != TomlType::String) {
        setError(error, "api.language_code: expected a string value");
        return false;
    }
    std::string text = value->asString();
    if (text.empty() || text == "auto") {
        text = "auto";
    }
    RawConfig probe;
    probe.setValue(text);
    DictationLanguage parsed{};
    if (!unmarshallOption(parsed, probe, false)) {
        setError(error, "api.language_code: unknown value \"" + text + "\"");
        return false;
    }
    raw.setValueByPath(kLanguageCode, text);
    return true;
}

/// TOML array of strings -> the indexed children of a std::vector<std::string>
/// option. An empty array still creates the node, so that the option really
/// becomes empty instead of being left untouched.
/// TOML boolean -> the "True"/"False" text fcitx5's RawConfig uses.
bool applyBool(const TomlDocument &document, const char *section,
               const char *key, const char *option, RawConfig &raw,
               std::string *error) {
    const TomlValue *value = document.get(section, key);
    if (!value) {
        return true;
    }
    if (value->type() != TomlType::Bool) {
        setError(error, std::string(section) + "." + key +
                            ": expected true or false");
        return false;
    }
    raw.setValueByPath(option, value->asBool() ? "True" : "False");
    return true;
}

bool applyKeyterms(const TomlDocument &document, RawConfig &raw,
                   std::string *error) {
    const TomlValue *value = document.get("api", "keyterms");
    if (!value) {
        return true;
    }
    if (value->type() != TomlType::Array) {
        setError(error,
                 "api.keyterms: expected an array of strings, for example "
                 "[\"Sarvam\", \"dictation\"]");
        return false;
    }
    const std::vector<std::string> &terms = value->asArray();
    raw.setValueByPath(kKeyterms, std::string());
    for (size_t index = 0; index < terms.size(); ++index) {
        const std::string childPath =
            std::string(kKeyterms) + "/" + std::to_string(index);
        raw.setValueByPath(childPath, terms[index]);
    }
    return true;
}

/// TOML integer -> the stored name of the sample rate enum ("16000"/"8000").
bool applySampleRate(const TomlDocument &document, RawConfig &raw,
                     std::string *error) {
    const TomlValue *value = document.get("audio", "sample_rate");
    if (!value) {
        return true;
    }
    if (value->type() != TomlType::Integer) {
        setError(error,
                 "audio.sample_rate: expected an integer number of hertz");
        return false;
    }
    const std::string text = std::to_string(value->asInteger());
    RawConfig probe;
    probe.setValue(text);
    DictationSampleRate parsed{};
    if (!unmarshallOption(parsed, probe, false)) {
        setError(error, "audio.sample_rate: unsupported value " + text +
                            " (use 16000 or 8000)");
        return false;
    }
    raw.setValueByPath(kSampleRate, text);
    return true;
}

/// TOML integer -> RawConfig value of the IntConstrain option, range checked so
/// that the user is told what is wrong instead of seeing the value revert.
bool applyMaxDuration(const TomlDocument &document, RawConfig &raw,
                      std::string *error) {
    const TomlValue *value = document.get("server", "max_duration_s");
    if (!value) {
        return true;
    }
    if (value->type() != TomlType::Integer) {
        setError(
            error,
            "server.max_duration_s: expected an integer number of seconds");
        return false;
    }
    const long long seconds = value->asInteger();
    if (seconds < kMinDurationSeconds || seconds > kMaxDurationSeconds) {
        setError(error, "server.max_duration_s: must be between " +
                            std::to_string(kMinDurationSeconds) + " and " +
                            std::to_string(kMaxDurationSeconds) + " seconds");
        return false;
    }
    raw.setValueByPath(kMaxDurationS, std::to_string(seconds));
    return true;
}

/// The RawConfig value of an option, as text. Empty when the option has no
/// value, which is what an empty string option looks like.
std::string optionText(const RawConfig &raw, const char *option) {
    const std::string *value = raw.valueByPath(option);
    return value ? *value : std::string();
}

/// The value to write for `language_code`: the enum's `auto` entry becomes the
/// empty string the daemon documents as auto-detect.
std::string languageCodeForFile(const RawConfig &raw) {
    const std::string text = optionText(raw, kLanguageCode);
    return text == "auto" ? std::string() : text;
}

/// The RawConfig value of a boolean option, read back as a bool.
bool optionBool(const RawConfig &raw, const char *option, bool fallback) {
    const std::string *value = raw.valueByPath(option);
    if (!value || value->empty()) {
        return fallback;
    }
    return *value == "True";
}

/// The RawConfig value of an integer option, parsed back into a number.
long long optionInteger(const RawConfig &raw, const char *option,
                        long long fallback) {
    const std::string *value = raw.valueByPath(option);
    if (!value || value->empty()) {
        return fallback;
    }
    errno = 0;
    char *end = nullptr;
    const long long parsed = std::strtoll(value->c_str(), &end, 10);
    if (errno != 0 || end == value->c_str() || *end != '\0') {
        return fallback;
    }
    return parsed;
}

/// The indexed children of the Keyterms option, in order. fcitx5 marshals a
/// std::vector<std::string> as the children "0", "1" and so on, and stops at
/// the first missing index, exactly as its own unmarshaller does.
std::vector<std::string> keytermsFromRaw(const RawConfig &raw) {
    std::vector<std::string> terms;
    auto node = raw.get(kKeyterms);
    if (!node) {
        return terms;
    }
    for (size_t index = 0;; ++index) {
        auto child = node->get(std::to_string(index));
        if (!child) {
            break;
        }
        terms.push_back(child->value());
    }
    return terms;
}

} // namespace

std::string daemonConfigPath() {
    // Read the environment directly rather than going through StandardPaths so
    // that the C++ and Python sides agree on the path byte for byte; the daemon
    // does the same in its own default_config_path().
    const char *xdgConfigHome = std::getenv("XDG_CONFIG_HOME");
    std::string base;
    if (xdgConfigHome && *xdgConfigHome) {
        base = xdgConfigHome;
    } else {
        const char *home = std::getenv("HOME");
        if (home && *home) {
            base = home;
        } else if (const passwd *entry = ::getpwuid(::getuid());
                   entry && entry->pw_dir && *entry->pw_dir) {
            // fcitx5 can be started from a session that never set HOME.
            base = entry->pw_dir;
        }
        if (base.empty()) {
            return std::string();
        }
        base += "/.config";
    }
    while (base.size() > 1 && base.back() == '/') {
        base.pop_back();
    }
    return base + "/sarvam-dictation/config.toml";
}

bool daemonConfigExists(const std::string &path) {
    if (path.empty()) {
        return false;
    }
    struct stat info {};
    if (::stat(path.c_str(), &info) != 0) {
        return false;
    }
    return S_ISREG(info.st_mode) != 0;
}

bool loadDaemonConfig(SarvamDictationConfig &config, const std::string &path,
                      std::string *error) {
    if (path.empty()) {
        setError(error, "the daemon config path is empty");
        return false;
    }

    struct stat info {};
    if (::stat(path.c_str(), &info) != 0) {
        if (errno == ENOENT || errno == ENOTDIR) {
            // No file at all: the daemon falls back to its built-in defaults.
            FCITX_SARVAMDICTATIONCONFIG_DEBUG()
                << "no daemon config at " << path << ", using defaults";
            resetDaemonOptions(config);
            return true;
        }
        setError(error, "cannot access " + path + ": " + describeErrno(errno));
        return false;
    }
    if (!S_ISREG(info.st_mode)) {
        setError(error, path + " is not a regular file");
        return false;
    }

    std::string text;
    if (!readWholeFile(path, text, error)) {
        return false;
    }

    std::vector<std::string> warnings;
    const TomlDocument document = TomlDocument::parse(text, &warnings);
    for (const auto &warning : warnings) {
        // A file written by a newer daemon may hold keys this build does not
        // understand; TomlDocument keeps them and the user's file stays usable.
        FCITX_SARVAMDICTATIONCONFIG_DEBUG()
            << path << ": ignoring: " << warning;
    }

    // Build the RawConfig completely before touching `config`: if anything in
    // the file is wrong, the caller keeps its current values.
    RawConfig raw;
    if (!applyString(document, "api", "key", kApiKey, raw, error) ||
        !applyEnum<DictationModel>(document, "api", "model", kModel, raw,
                                   error) ||
        !applyEnum<DictationMode>(document, "api", "mode", kMode, raw, error) ||
        !applyBool(document, "api", "streaming", kStreaming, raw, error) ||
        !applyLanguageCode(document, raw, error) ||
        !applyKeyterms(document, raw, error) ||
        !applyString(document, "audio", "device", kAudioDevice, raw, error) ||
        !applySampleRate(document, raw, error) ||
        !applyEnum<DictationBackend>(document, "audio", "backend", kBackend,
                                     raw, error) ||
        !applyString(document, "server", "socket_path", kSocketPath, raw,
                     error) ||
        !applyMaxDuration(document, raw, error)) {
        return false;
    }

    // partial = true: options that are absent from `raw` - the addon settings,
    // and any daemon key the file does not mention - keep their current value.
    config.load(raw, /*partial=*/true);
    return true;
}

bool saveDaemonConfig(const SarvamDictationConfig &config,
                      const std::string &path, std::string *error) {
    if (path.empty()) {
        setError(error, "the daemon config path is empty");
        return false;
    }

    // Start from the existing file so that every key this function does not
    // manage survives: the user's extra keys round-trip through TomlDocument.
    // A file we cannot read is a hard failure - overwriting it would throw the
    // user's settings away.
    TomlDocument document;
    struct stat info {};
    if (::stat(path.c_str(), &info) == 0) {
        if (!S_ISREG(info.st_mode)) {
            setError(error, path + " is not a regular file");
            return false;
        }
        std::string existing;
        if (!readWholeFile(path, existing, error)) {
            return false;
        }
        std::vector<std::string> warnings;
        document = TomlDocument::parse(existing, &warnings);
        for (const auto &warning : warnings) {
            FCITX_SARVAMDICTATIONCONFIG_DEBUG()
                << path << ": ignoring: " << warning;
        }
    } else if (errno != ENOENT && errno != ENOTDIR) {
        setError(error, "cannot access " + path + ": " + describeErrno(errno));
        return false;
    }

    // `language_code` is written in the daemon's spelling: empty means
    // auto-detect, not the config page's `auto` entry. Writing `auto` here would
    // be forwarded to the API as a language tag and rejected.

    // save() gives the values in exactly the form the options marshall them, so
    // the enums arrive as their stored names and the integers as decimal text.
    RawConfig raw;
    config.save(raw);

    document.set("api", "key", TomlValue::string(optionText(raw, kApiKey)));
    document.set("api", "model", TomlValue::string(optionText(raw, kModel)));
    document.set("api", "mode", TomlValue::string(optionText(raw, kMode)));
    document.set("api", "streaming",
                 TomlValue::boolean(optionBool(raw, kStreaming, false)));
    document.set("api", "language_code",
                 TomlValue::string(languageCodeForFile(raw)));
    document.set("api", "keyterms", TomlValue::array(keytermsFromRaw(raw)));
    document.set("audio", "device",
                 TomlValue::string(optionText(raw, kAudioDevice)));
    document.set("audio", "sample_rate",
                 TomlValue::integer(optionInteger(raw, kSampleRate, 16000)));
    document.set("audio", "backend",
                 TomlValue::string(optionText(raw, kBackend)));
    document.set("server", "socket_path",
                 TomlValue::string(optionText(raw, kSocketPath)));
    document.set("server", "max_duration_s",
                 TomlValue::integer(optionInteger(raw, kMaxDurationS, 300)));

    const std::string serialized = document.serialize();
    if (!writeAtomically(path, serialized, error)) {
        return false;
    }
    FCITX_SARVAMDICTATIONCONFIG_DEBUG()
        << "wrote " << serialized.size() << " bytes to " << path;
    return true;
}

} // namespace fcitx::sarvam
