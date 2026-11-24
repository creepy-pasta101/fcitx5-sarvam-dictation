/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * The configuration surface of the Sarvam dictation addon.
 *
 * Three FCITX_CONFIGURATION objects, because the config page is three pages.
 * fcitx5 has no per-option visibility API at all -- fcitx-config/option.h
 * carries no "visible"/"enabled" concept, and the frontends build one static
 * widget set per page (fcitx5-configtool configwidget.cpp:191-225) -- so the
 * only live, supported way to give each engine its own settings is a
 * sub-configuration per engine, reached through the enum's SubConfigPath
 * property. See EngineAnnotation below.
 *
 *  - SarvamDictationConfig is the main page: the options both engines share,
 *    plus the addon's own settings.
 *  - SaarasSettings and DictateSettings are the two engine pages, returned by
 *    SarvamDictation::getSubConfig("saaras") / ("dictate").
 *
 * The three objects all marshal into the same file, and which object an option
 * lives in is a UI fact, not a file-layout fact:
 *
 *  - The daemon settings (api.*, audio.*, server.*, dictate.*) are owned by the
 *    Python daemon and stored in ~/.config/sarvam-dictation/config.toml. The
 *    addon keeps no copy: loadDaemonConfig() fills these options from that file
 *    and saveDaemonConfig() writes them back to it, so the TOML file stays the
 *    one source of truth and the API key is never duplicated. The Saaras
 *    options stay under [api] and the Dictate ones under [dictate]; the daemon
 *    reads one file and does not know how many pages the dialog has.
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
 * Which backend the daemon talks to. The stored names are the values of
 * `[api] engine` in config.toml.
 *
 * The two engines differ in what the rest of the settings mean: `saaras` is the
 * plain speech-to-text API (model, keyterms, streaming), `dictate` takes a
 * transcript plus instructions (style, rules, vocabulary, context, ...). Those
 * two groups are separate Configuration objects -- see SaarasSettings and
 * DictateSettings below -- and this enum is what puts the matching one behind
 * the Configure button.
 */
enum class Engine { Saaras, Dictate };
FCITX_CONFIG_ENUM_NAME(Engine, "saaras", "dictate");
SARVAM_DICTATION_ENUM_LABELS(Engine, N_("Saaras (speech-to-text)"),
                             N_("Dictate (speech-to-text with instructions)"));

/*
 * The two engine sub-pages, and the names the addon answers to.
 *
 * The URI is what the description carries and what the frontend asks for:
 * fcitx5's D-Bus module splits `fcitx://config/addon/<addon>[/<subPath>]` and
 * calls AddonInstance::getSubConfig(<subPath>) with everything after the addon
 * name (dbusmodule.cpp:351-378), so the last segment below is also the string
 * SarvamDictation::getSubConfig() compares against.
 */
constexpr char kSaarasSubConfigName[] = "saaras";
constexpr char kDictateSubConfigName[] = "dictate";
constexpr char kSaarasSubConfigUri[] =
    "fcitx://config/addon/sarvamdictation/saaras";
constexpr char kDictateSubConfigUri[] =
    "fcitx://config/addon/sarvamdictation/dictate";

/*
 * The "Configure" button next to the engine combo.
 *
 * fcitx5-configtool honours exactly one value-reactive property on a generated
 * page: the per-enum-value `SubConfigPath/<i>`. The Qt Widgets frontend reads
 * it into the combo's subConfigPathRole and shows the button only while the
 * current entry has a path (configwidgetslib/optionwidget.cpp:344-363); the KCM
 * binds the button's `visible` to the combo's current index
 * (kcm/ui/EnumOption.qml:81-89). One path per engine value therefore gives
 * "Saaras settings or Dictate settings, following the combo", live, using only
 * 5.1.x API. The in-tree precedent is the Classic UI theme selector
 * (src/ui/classic/classicui.h:83-112, ThemeAnnotation).
 *
 * `LaunchSubConfig=True` is what makes the frontend open the URI as a nested
 * generated page rather than launching an external program: it is the one flag
 * that distinguishes SubConfigOption from ExternalOption
 * (fcitx-config/option.cpp:81-84).
 *
 * The `Enum/<i>` names and the `EnumI18n/<i>` labels the combo also needs are
 * written by FCITX_CONFIG_ENUM_NAME's dumpDescriptionHelper and by the label
 * macro, so they are not repeated here; only the paths are ours.
 */
struct EngineAnnotation : public EngineI18NAnnotation {
    void dumpDescription(RawConfig &config) const {
        EngineI18NAnnotation::dumpDescription(config);
        config.setValueByPath("LaunchSubConfig", "True");
        // Indexed by the enum's own order, exactly like Enum/<i>:
        // Enum/0 = "saaras" -> SubConfigPath/0, Enum/1 = "dictate" -> 1.
        config.setValueByPath("SubConfigPath/0", kSaarasSubConfigUri);
        config.setValueByPath("SubConfigPath/1", kDictateSubConfigUri);
    }
};

/*
 * Dictation model. The stored name is the value the Sarvam API expects, so the
 * TOML file and the daemon both use "saaras:v4" while the UI shows a sentence.
 */
enum class DictationModel { SaarasV4, SaarasV3, SaarasV3Realtime };
FCITX_CONFIG_ENUM_NAME(DictationModel, "saaras:v4", "saaras:v3",
                       "saaras:v3-realtime");
SARVAM_DICTATION_ENUM_LABELS(DictationModel, N_("Saaras v4 (recommended)"),
                             N_("Saaras v3"),
                             N_("Saaras v3 realtime (streaming)"));

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

/*
 * Presents the free-form device string as a dropdown of the devices that exist
 * on this machine. fcitx5-configtool builds a combobox from Type=Enum plus the
 * Enum/N and EnumI18n/N description entries, and Option::dumpDescription calls
 * the constrain after it has set Type, so a runtime-generated list works.
 *
 * The constrain stores nothing: Type is std::string and check() accepts every
 * value, because a device may be temporarily absent (an unplugged headset) and
 * the setting must survive that. The definition of dumpDescription lives in
 * audiodevices.cpp, next to the enumeration it prints.
 */
struct AudioDeviceConstrain {
    using Type = std::string;
    bool check(const std::string & /*unused*/) const { return true; }
    void dumpDescription(RawConfig &config) const;
};

/*
 * The Saaras page: the options that only mean anything to the speech-to-text
 * engine. They stay under [api] in config.toml.
 *
 * A separate Configuration object rather than a nested Configuration option: a
 * nested one renders *inside* the main page as a group box
 * (fcitx5-configtool configwidget.cpp:213-221, and the KCM flattens it into a
 * section at kcm/main.cpp:158-185), which is exactly what the engine split has
 * to avoid.
 */
FCITX_CONFIGURATION(
    SaarasSettings,
    OptionWithAnnotation<DictationModel, DictationModelI18NAnnotation> model{
        this, "Model", _("Model"), DictationModel::SaarasV4};
    Option<std::vector<std::string>> keyterms{
        this, "Keyterms", _("Keyterms (saaras:v4 only, max 50)"),
        std::vector<std::string>()};
    // Off by default: the REST path stays the default, and the two dictation
    // modes are configured independently, because the protocol already tells
    // them apart -- push-to-talk sends start/stop, record-and-send sends
    // toggle. Only the realtime models have a streaming endpoint, and the
    // daemon forces both of these on for saaras:v3-realtime regardless of the
    // stored values.
    Option<bool> streamingPushToTalk{
        this, "StreamingPushToTalk",
        _("Stream audio while the push-to-talk key is held"), false};
    Option<bool> streamingToggle{
        this, "StreamingToggle",
        _("Stream audio in record-and-send mode"), false};);

FCITX_CONFIGURATION(
    SarvamDictationConfig,
    // ---- Daemon settings, stored in ~/.config/sarvam-dictation/config.toml --
    Option<std::string> apiKey{this, "ApiKey", _("Sarvam API key"), ""};
    // Declared high on the page because it decides which sub-page holds the
    // rest of the settings, and before the shared rows so the combo is the
    // first thing the user reads. The annotation is what puts the Configure
    // button next to it.
    OptionWithAnnotation<Engine, EngineAnnotation> engine{
        this, "Engine", _("Engine"), Engine::Saaras};
    // Shared by both engines. `translate` is a Saaras mode: the daemon rejects
    // it on the Dictate socket (translation there is a `rules` request), and
    // the validation stays in the daemon rather than being duplicated here.
    OptionWithAnnotation<DictationMode, DictationModeI18NAnnotation> mode{
        this, "Mode", _("Output mode"), DictationMode::Transcribe};
    OptionWithAnnotation<DictationLanguage, DictationLanguageI18NAnnotation>
        languageCode{this, "LanguageCode", _("Language"),
                     DictationLanguage::Auto};
    // The value is still a plain std::string: only its presentation changes, so
    // sarvamdictationconfig.cpp keeps reading and writing audio.device as text.
    Option<std::string, AudioDeviceConstrain> audioDevice{
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
    // Streaming sends partial transcripts while the user is still speaking, and
    // the indicator shows them. This keeps the state feedback (Listening,
    // Transcribing) while turning the text itself off.
    Option<bool> showPartialText{
        this, "ShowPartialText", _("Show the transcript in the indicator"), true};
    // Streams the partial transcript into the focused field as IME preedit, so
    // the words appear where they will be typed and are replaced by the final
    // transcript. With this on, the indicator shows only the state.
    Option<bool> preeditPartials{
        this, "PreeditPartials",
        _("Show the transcript in the text field while streaming"), true};
    // On by default: on KDE Plasma the recording indicator is Plasma's own OSD,
    // which needs no configuration and is invisible on every other desktop.
    Option<bool> showOsd{
        this, "ShowOsd", _("Show an on-screen display on KDE Plasma"), true};
    // Where the transcript goes when there is no focused input context to type
    // into. fcitx5 exposes no "a text widget is focused" flag -- capability
    // flags are not a substitute, X11/XIM clients advertise almost nothing --
    // so the only decidable rule is "no focused context means clipboard".
    Option<bool> clipboardFallback{
        this, "ClipboardFallback",
        _("Copy the transcript to the clipboard when nothing is focused to "
          "type into"),
        true};
    // For applications that accept committed text and silently discard it (a
    // browser page with no field focused): nothing can detect that case, so
    // this copies as well, every time.
    Option<bool> clipboardAlways{
        this, "ClipboardAlways",
        _("Always copy the transcript to the clipboard as well"), false};
    Option<bool> appendSpace{
        this, "AppendSpace",
        _("Append a trailing space after the transcript"), true};);

/*
 * Dictate transcript style. `Unset` is the stored name of the "no preference"
 * entry and is written to the file as the empty string, which is what the
 * daemon documents for "let the service choose". The two spellings are
 * translated in sarvamdictationconfig.cpp, the same way `api.language_code`
 * spells auto-detection as an empty string there: an enum entry whose stored
 * name is empty would put an unnamed row in the combo, so the combo keeps a
 * name and only the file uses the empty one.
 */
enum class DictateStyle { Unset, None, Light, Clean };
FCITX_CONFIG_ENUM_NAME(DictateStyle, "unset", "none", "light", "clean");
SARVAM_DICTATION_ENUM_LABELS(
    DictateStyle, N_("No preference (let the service choose)"),
    N_("None (keep the transcript as spoken)"),
    N_("Light (remove fillers and false starts)"),
    N_("Clean (readable, punctuation, casing)"));

/// How the Dictate socket is used.
enum class DictateStreamType { Fast, Balanced, Simulated };
FCITX_CONFIG_ENUM_NAME(DictateStreamType, "fast", "balanced", "simulated");
SARVAM_DICTATION_ENUM_LABELS(DictateStreamType, N_("Fast (lowest latency)"),
                             N_("Balanced (recommended)"),
                             N_("Simulated (one result at the end)"));

/*
 * The Dictate page: the options that only mean anything to that engine. They go
 * to the [dictate] table of config.toml and are ignored by the daemon unless
 * `[api] engine = "dictate"`.
 *
 * The length limits in the labels are the daemon's; the page repeats them
 * because a QLineEdit cannot be told about them, and the daemon is still the
 * one that enforces them (it reports a rejected combination as `config_invalid`
 * at runtime).
 */
FCITX_CONFIGURATION(
    DictateSettings,
    OptionWithAnnotation<DictateStyle, DictateStyleI18NAnnotation> style{
        this, "Style", _("Transcript style"), DictateStyle::Unset};
    Option<std::string> rules{
        this, "Rules", _("Rules (free text, at most 2000 characters)"), ""};
    Option<std::vector<std::string>> vocabulary{
        this, "Vocabulary",
        _("Vocabulary (names and terms, at most 1000 entries of 64 "
          "characters)"),
        std::vector<std::string>()};
    Option<std::string> context{
        this, "Context", _("Context (one line, at most 2000 characters)"), ""};
    Option<std::string> reference{
        this, "Reference",
        _("Reference document of names and terms (at most 50000 characters)"),
        ""};
    Option<std::string> outputRules{
        this, "OutputRules", _("Output rules (at most 4000 characters)"), ""};
    // Free text rather than an enum: a snapshot name is not a list this addon
    // can know, and an empty value means "whatever the service defaults to".
    Option<std::string> model{
        this, "Model", _("Model snapshot (empty = the service default)"), ""};
    OptionWithAnnotation<DictateStreamType, DictateStreamTypeI18NAnnotation>
        streamType{this, "StreamType", _("Stream type"),
                   DictateStreamType::Balanced};);

/// Absolute path of the daemon config file: $XDG_CONFIG_HOME/sarvam-dictation/
/// config.toml when XDG_CONFIG_HOME is set and non-empty, else
/// ~/.config/sarvam-dictation/config.toml.
std::string daemonConfigPath();

/// True when the path exists and is a regular file.
bool daemonConfigExists(const std::string &path);

/// Fill the daemon-owned options of `config`, `saaras` and `dictate` from the
/// TOML file at `path`.
///
/// The three objects are three pages of one file: `config` takes the shared
/// keys plus `[api] engine`, `saaras` takes the rest of `[api]` and `dictate`
/// takes `[dictate]`. Each is loaded on its own RawConfig, so the two `Model`
/// options (the Saaras model enum and the Dictate model string) cannot read
/// each other's value.
///
/// A missing file is not an error: the daemon falls back to its built-in
/// defaults, so the daemon options are reset to their defaults and the call
/// returns true. An unreadable file, a path that is not a regular file, or a
/// value of the wrong type (or outside the option's range) returns false,
/// leaves all three objects completely untouched, and describes the reason in
/// `error` when it is non-null. Parse warnings are logged at debug level and
/// never fail the load. Keys that the file does not mention keep their current
/// value (the load is partial, exactly like the addon's setConfig), so a fresh
/// config keeps the defaults.
bool loadDaemonConfig(SarvamDictationConfig &config, SaarasSettings &saaras,
                      DictateSettings &dictate, const std::string &path,
                      std::string *error = nullptr);

/// Write the daemon-owned options of `config`, `saaras` and `dictate` to
/// `path`.
///
/// Every key of the frozen table is written, from whichever of the three
/// objects owns it, and both engine groups are always written: `[dictate]` is
/// present even while the Saaras engine is selected, so switching engines does
/// not lose the other engine's settings and load -> save -> load is stable.
///
/// Every other key already in the file is preserved. Parent directories are
/// created. The write is atomic - a temporary file in the same directory, an
/// fsync, then a rename - and the result has mode 0600, because the file holds
/// an API key. Returns false and describes the reason in `error` on failure.
bool saveDaemonConfig(const SarvamDictationConfig &config,
                      const SaarasSettings &saaras,
                      const DictateSettings &dictate, const std::string &path,
                      std::string *error = nullptr);

// Declared here rather than at global scope so the name cannot collide with
// the addon's own log category; the definition is in sarvamdictationconfig.cpp.
FCITX_DECLARE_LOG_CATEGORY(sarvamdictationconfig_log);

} // namespace fcitx::sarvam

#define FCITX_SARVAMDICTATIONCONFIG_DEBUG()                                    \
    FCITX_LOGC(::fcitx::sarvam::sarvamdictationconfig_log, Debug)

#endif // _FCITX5_SARVAM_DICTATION_SARVAMDICTATIONCONFIG_H_
