/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * The configuration surface of the Sarvam dictation addon.
 *
 * One FCITX_CONFIGURATION holds two groups of settings that live in two
 * different files:
 *
 *  - The daemon settings (api.*, audio.*, server.*) are owned by the Python
 *    daemon and stored in ~/.config/sarvam-dictation/config.toml. The addon
 *    keeps no copy: loadDaemonConfig() fills these options from that file and
 *    saveDaemonConfig() writes them back to it, so the TOML file stays the one
 *    source of truth and the API key is never duplicated.
 *
 *  - The addon settings (TriggerKey and friends) are of no interest to the
 *    daemon and keep living in the fcitx5 conf file
 *    (conf/sarvamdictation.conf), exactly as before.
 *
 * The daemon options are declared first, in the order of the frozen table, so
 * the configtool page reads top-down as "how the daemon behaves" and then "how
 * the addon behaves".
 *
 * The four free functions at the bottom are the only TOML-aware entry points;
 * the integrator calls them from reloadConfig()/setConfig().
 */
#ifndef _FCITX5_SARVAM_DICTATION_SARVAMDICTATIONCONFIG_H_
#define _FCITX5_SARVAM_DICTATION_SARVAMDICTATIONCONFIG_H_

#include <cstddef>
#include <string>
#include <type_traits>
#include <vector>

#include "fcitx-config/configuration.h"
#include "fcitx-config/enum.h"
#include "fcitx-config/option.h"
#include "fcitx-config/rawconfig.h"
#include "fcitx-utils/i18n.h"
#include "fcitx-utils/key.h"
#include "fcitx-utils/log.h"

namespace fcitx::sarvam {

/*
 * Display labels for a stored-name list.
 *
 * fcitx5 5.1.12 ships FCITX_CONFIG_ENUM_I18N_ANNOTATION, but it cannot be used
 * together with FCITX_CONFIG_ENUM_NAME here. The macro ends with
 *
 *     static_assert(TYPE##I18NAnnotation::equal(0), "Enum mismatch");
 *
 * and its `equal()` helper compares the stored name of every entry with its
 * label using fcitx::stringutils::literalEqual(), which walks the two strings
 * character by character. That only holds when the label and the stored name
 * are the same literal (which is how fcitx5 itself uses the macro, as in
 * FCITX_CONFIG_ENUM_NAME_WITH_I18N(QuickPhraseChooseModifier, N_("None"),
 * N_("Alt"), N_("Control"), N_("Super"))).
 *
 * Our stored names are TOML values such as "saaras:v4" while the labels are
 * prose ("Saaras v4 (recommended)"), so the fcitx5 macro is a guaranteed
 * compile error.
 *
 * This macro therefore expands to the same struct the fcitx5 macro generates -
 * same member names and the same EnumI18n/<n> description keys the configtool
 * reads, so the UI behaves identically - with the labels taken from the list
 * below instead of from the stored-name list. The only extra check kept is the
 * one that is actually true and useful: both lists must have the same length.
 */
#define SARVAM_DICTATION_ENUM_LABELS(TYPE, ...)                                \
    struct TYPE##I18NAnnotation {                                              \
        static constexpr const char *labels[] = {__VA_ARGS__};                 \
        static constexpr size_t enumLength = FCITX_ARRAY_SIZE(labels);         \
        bool skipDescription() const { return false; }                         \
        bool skipSave() const { return false; }                                \
        void dumpDescription(fcitx::RawConfig &config) const {                 \
            for (size_t i = 0; i < enumLength; i++) {                          \
                config.setValueByPath("EnumI18n/" + std::to_string(i),         \
                                      _(labels[i]));                           \
            }                                                                  \
        }                                                                      \
        static std::string toString(TYPE value) {                              \
            return _(labels[static_cast<std::underlying_type_t<TYPE>>(         \
                value)]);                                                      \
        }                                                                      \
        static_assert(FCITX_ARRAY_SIZE(labels) ==                              \
                          FCITX_ARRAY_SIZE(_##TYPE##_Names),                   \
                      "Enum label count mismatch");                            \
    }

/*
 * Dictation model. The stored name is the value the Sarvam API expects, so the
 * TOML file and the daemon both use "saaras:v4" while the UI shows a sentence.
 */
enum class DictationModel { SaarasV4, SaarasV3 };
FCITX_CONFIG_ENUM_NAME(DictationModel, "saaras:v4", "saaras:v3");
SARVAM_DICTATION_ENUM_LABELS(DictationModel, N_("Saaras v4 (recommended)"),
                             N_("Saaras v3"));

/// What the daemon does with the recording.
enum class DictationMode { Transcribe, Translate, Verbatim, Translit, Codemix };
FCITX_CONFIG_ENUM_NAME(DictationMode, "transcribe", "translate", "verbatim",
                       "translit", "codemix");
SARVAM_DICTATION_ENUM_LABELS(DictationMode, N_("Transcribe (spoken language)"),
                             N_("Translate to English"),
                             N_("Verbatim (keep fillers)"),
                             N_("Transliterate to Latin script"),
                             N_("Code-mixed"));

/*
 * Spoken language. The stored names are BCP-47-ish tags and are passed through
 * to the daemon untouched; the daemon validates the code itself. od-IN and
 * or-IN are both listed because the daemon accepts both Odia tags.
 */
enum class DictationLanguage {
    Auto,
    Hindi,
    Bengali,
    Gujarati,
    Kannada,
    Malayalam,
    Marathi,
    Odia,
    Punjabi,
    Tamil,
    Telugu,
    Assamese,
    Bodo,
    Dogri,
    Konkani,
    Kashmiri,
    Maithili,
    Manipuri,
    Nepali,
    Oriya,
    Sanskrit,
    Santali,
    Sindhi,
    Urdu,
    EnglishIndia,
};
FCITX_CONFIG_ENUM_NAME(DictationLanguage, "auto", "hi-IN", "bn-IN", "gu-IN",
                       "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN",
                       "te-IN", "as-IN", "brx-IN", "doi-IN", "kok-IN", "ks-IN",
                       "mai-IN", "mni-IN", "ne-IN", "or-IN", "sa-IN", "sat-IN",
                       "sd-IN", "ur-IN", "en-IN");
SARVAM_DICTATION_ENUM_LABELS(
    DictationLanguage, N_("Auto-detect"), N_("Hindi"), N_("Bengali"),
    N_("Gujarati"), N_("Kannada"), N_("Malayalam"), N_("Marathi"), N_("Odia"),
    N_("Punjabi"), N_("Tamil"), N_("Telugu"), N_("Assamese"), N_("Bodo"),
    N_("Dogri"), N_("Konkani"), N_("Kashmiri"), N_("Maithili"),
    N_("Manipuri"), N_("Nepali"), N_("Odia (or-IN)"), N_("Sanskrit"),
    N_("Santali"), N_("Sindhi"), N_("Urdu"), N_("English (India)"));

/*
 * Capture sample rate. The daemon stores an integer number of hertz, so the
 * stored names are the decimal spellings; the TOML side stays an integer and
 * the two are converted in sarvamdictationconfig.cpp.
 */
enum class DictationSampleRate { Hz16000, Hz8000 };
FCITX_CONFIG_ENUM_NAME(DictationSampleRate, "16000", "8000");
SARVAM_DICTATION_ENUM_LABELS(DictationSampleRate, N_("16000 Hz (recommended)"),
                             N_("8000 Hz (telephony)"));

/// Which command-line recorder the daemon drives.
enum class DictationBackend { Auto, Pipewire, Pulse, Alsa };
FCITX_CONFIG_ENUM_NAME(DictationBackend, "auto", "pipewire", "pulse", "alsa");
SARVAM_DICTATION_ENUM_LABELS(DictationBackend, N_("Auto-detect"),
                             N_("PipeWire (pw-record)"),
                             N_("PulseAudio (parec)"), N_("ALSA (arecord)"));

FCITX_CONFIGURATION(
    SarvamDictationConfig,
    // ---- Daemon settings, stored in ~/.config/sarvam-dictation/config.toml --
    Option<std::string> apiKey{this, "ApiKey", _("Sarvam API key"), ""};
    OptionWithAnnotation<DictationModel, DictationModelI18NAnnotation> model{
        this, "Model", _("Model"), DictationModel::SaarasV4};
    OptionWithAnnotation<DictationMode, DictationModeI18NAnnotation> mode{
        this, "Mode", _("Output mode"), DictationMode::Transcribe};
    OptionWithAnnotation<DictationLanguage, DictationLanguageI18NAnnotation>
        languageCode{this, "LanguageCode", _("Language"),
                     DictationLanguage::Auto};
    Option<std::vector<std::string>> keyterms{
        this, "Keyterms", _("Keyterms (saaras:v4 only, max 50)"),
        std::vector<std::string>()};
    Option<std::string> audioDevice{
        this, "AudioDevice", _("Audio device (empty = default source)"), ""};
    OptionWithAnnotation<DictationSampleRate, DictationSampleRateI18NAnnotation>
        sampleRate{this, "SampleRate", _("Sample rate"),
                   DictationSampleRate::Hz16000};
    OptionWithAnnotation<DictationBackend, DictationBackendI18NAnnotation>
        backend{this, "Backend", _("Capture backend"),
                DictationBackend::Auto};
    Option<std::string> socketPath{
        this, "SocketPath", _("Socket path (empty = default)"), ""};
    Option<int, IntConstrain> maxDurationS{
        this, "MaxDurationS", _("Max recording length (seconds)"), 300,
        IntConstrain(5, 3600)};
    // ---- Addon settings, stored in conf/sarvamdictation.conf ---------------
    KeyListOption triggerKey{this,
                             "TriggerKey",
                             _("Trigger key"),
                             {Key("Control+Alt+d")},
                             KeyListConstrain()};
    Option<bool> autoStartDaemon{
        this, "AutoStartDaemon", _("Start the daemon automatically"), true};
    Option<std::string> daemonCommand{this,
                                      "DaemonCommand",
                                      _("Daemon command"),
                                      "sarvam-dictation-daemon"};
    KeyListOption pushToTalkKey{this,
                                "PushToTalkKey",
                                _("Push-to-talk key (hold to record)"),
                                {Key("Control+Alt+space")},
                                KeyListConstrain()};
    // Quiet by default: nothing is shown while dictating. Failures still reach the
    // user, through a desktop notification rather than a tip -- see
    // SarvamDictation::showErrorNotification().
    Option<bool> notifyOnError{
        this, "NotifyOnError", _("Notify on errors (desktop notification)"),
        true};
    Option<bool> showRecordingTips{
        this, "ShowRecordingTips", _("Show a tip while recording"), false};
    // On by default: on KDE Plasma the recording indicator is Plasma's own OSD,
    // which needs no configuration and is invisible on every other desktop.
    Option<bool> showOsd{
        this, "ShowOsd", _("Show an on-screen display on KDE Plasma"), true};
    Option<bool> appendSpace{
        this, "AppendSpace",
        _("Append a trailing space after the transcript"), true};);

/// Absolute path of the daemon config file: $XDG_CONFIG_HOME/sarvam-dictation/
/// config.toml when XDG_CONFIG_HOME is set and non-empty, else
/// ~/.config/sarvam-dictation/config.toml.
std::string daemonConfigPath();

/// True when the path exists and is a regular file.
bool daemonConfigExists(const std::string &path);

/// Fill the daemon-owned options of `config` from the TOML file at `path`.
///
/// A missing file is not an error: the daemon falls back to its built-in
/// defaults, so the daemon options are reset to their defaults and the call
/// returns true. An unreadable file, a path that is not a regular file, or a
/// value of the wrong type (or outside the option's range) returns false,
/// leaves `config` completely untouched, and describes the reason in `error`
/// when it is non-null. Parse warnings are logged at debug level and never fail
/// the load. Keys that the file does not mention keep their current value, so a
/// fresh config keeps the defaults.
bool loadDaemonConfig(SarvamDictationConfig &config, const std::string &path,
                      std::string *error = nullptr);

/// Write the daemon-owned options of `config` to `path`.
///
/// Every other key already in the file is preserved. Parent directories are
/// created. The write is atomic - a temporary file in the same directory, an
/// fsync, then a rename - and the result has mode 0600, because the file holds
/// an API key. Returns false and describes the reason in `error` on failure.
bool saveDaemonConfig(const SarvamDictationConfig &config,
                      const std::string &path, std::string *error = nullptr);

// Declared here rather than at global scope so the name cannot collide with
// the addon's own log category; the definition is in sarvamdictationconfig.cpp.
FCITX_DECLARE_LOG_CATEGORY(sarvamdictationconfig_log);

} // namespace fcitx::sarvam

#define FCITX_SARVAMDICTATIONCONFIG_DEBUG()                                    \
    FCITX_LOGC(::fcitx::sarvam::sarvamdictationconfig_log, Debug)

#endif // _FCITX5_SARVAM_DICTATION_SARVAMDICTATIONCONFIG_H_
