/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * Implementation of the Sarvam dictation addon.
 *
 * Design notes that are not obvious from the code:
 *
 *  - The socket is created SOCK_STREAM and made non-blocking with fcntl rather
 *    than SOCK_NONBLOCK so the code stays portable across the BSDs that fcitx5
 *    also runs on. Nothing here ever calls read()/write() in a way that could
 *    sleep, so a wedged daemon can never freeze the compositor's key handling.
 *
 *  - The protocol (protocol version 1, newline-delimited JSON) is shared with a
 *    Python daemon that we cannot see. It is therefore parsed defensively: an
 *    unparsable line, an unknown field, an unknown message type or a truncated
 *    tail is dropped instead of being reported as an error, because a dictation
 *    addon that spams the user with protocol complaints is worse than one that
 *    quietly ignores a line it does not understand.
 *
 *  - JSON is hand-parsed. A JSON library would be a new dependency for one
 *    small message shape, and the only genuinely tricky part is turning the
 *    \uXXXX escapes of Indic transcripts back into UTF-8, which is a few dozen
 *    lines of well understood code (see appendUtf8 / JsonCursor::parseString).
 */
#include "sarvamdictation.h"

#include <algorithm>
#include <cerrno>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <random>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include "fcitx-utils/event.h"
#include "fcitx-utils/eventloopinterface.h"
#include "fcitx-utils/key.h"
#include "fcitx/addonfactory.h"
#include "fcitx/addonmanager.h"
#include "fcitx/event.h"
#include "fcitx/inputcontext.h"
#include "fcitx/inputpanel.h"
#include "fcitx/instance.h"
#include "fcitx/text.h"
#include "fcitx/userinterface.h"

#include "clipboard.h"
#include "desktopnotify.h"
#include "plasmaosd.h"

namespace fcitx {

namespace {

/// Maximum size of a single protocol line, both directions. A peer that sends
/// more than this is disconnected: buffering it would only grow the heap.
constexpr size_t kMaxLine = 1024 * 1024;
/// Commands queued while the connection is being established. Older ones are
/// dropped, because replaying a stale "toggle" minutes later would start a
/// recording the user has long forgotten about.
constexpr size_t kMaxPendingLines = 8;
/// Ids we still expect a result for; used to drop stale transcripts.
constexpr size_t kMaxPendingIds = 16;
/// 25 attempts x 200 ms = 5 s, the budget from the protocol brief.
constexpr int kRetryBudget = 25;
constexpr uint64_t kRetryIntervalUsec = 200000;
constexpr size_t kReadChunk = 4096;
/// Bounds one IO callback so a peer that streams data cannot starve the loop;
/// the level-triggered source will simply fire again.
constexpr int kMaxReadsPerCallback = 64;
constexpr int32_t kTipTimeout = 3000;
constexpr char kTipId[] = "sarvam-dictation";
constexpr char kAppName[] = "fcitx5-sarvam-dictation";
constexpr char kAppIcon[] = "audio-input-microphone";
/// A push-to-talk hold shorter than this is a tap: it is cancelled instead of
/// being uploaded. 200 ms of room noise transcribes to nothing useful, and a
/// transcript pasted from an accidental tap is worse than no transcript.
constexpr uint64_t kPushToTalkMinHoldUsec = 250 * 1000;
/// How long an error stays in front of the user, as the notify-send
/// expire-time and as the fcitx5 tip timeout. Errors are rare and are the one
/// message that has to be noticed, so it may linger.
constexpr int32_t kErrorNotificationTimeoutMs = 10000;
/// Trailing-edge interval for the partial transcript. The daemon throttles
/// partials to roughly ten a second; holding the newest one this long keeps
/// the newest text while cutting the forked D-Bus clients to about three a
/// second. It also stops Plasma restarting the OSD's fade-in on every partial.
constexpr uint64_t kPartialIntervalUsec = 350 * 1000;
/// Longest partial text handed to the indicator, in bytes. A longer one keeps
/// its tail, because the newest words are the ones being spoken.
constexpr size_t kMaxPartialBytes = 120;

#ifdef MSG_NOSIGNAL
// A daemon that dies mid-write must not kill fcitx5 with SIGPIPE.
constexpr int kSendFlags = MSG_NOSIGNAL;
#else
constexpr int kSendFlags = 0;
#endif

/// Append one Unicode code point to a UTF-8 string, mapping anything that is
/// not a legal scalar value to U+FFFD so the result is always valid UTF-8.
void appendUtf8(uint32_t codePoint, std::string &out) {
    if (codePoint > 0x10FFFFu || (codePoint >= 0xD800u && codePoint <= 0xDFFFu)) {
        codePoint = 0xFFFDu;
    }
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

/// Normalise a partial transcript for the one-line indicator.
///
/// Every run of whitespace (space, tab, newline, carriage return) becomes a
/// single space, every other control character is dropped, and the result is
/// trimmed. A partial that is nothing but noise therefore sanitises to the
/// empty string, which the caller treats as "show nothing".
///
/// The scan is byte-wise, which is safe for the Devanagari and other
/// multi-byte transcripts the service returns: every byte of a multi-byte
/// UTF-8 sequence is 0x80 or above, so neither the whitespace test nor the
/// control-character test can match inside one.
std::string sanitisePartialText(std::string_view text) {
    std::string out;
    out.reserve(text.size());
    bool pendingSpace = false;
    for (const char raw : text) {
        const unsigned char byte = static_cast<unsigned char>(raw);
        if (byte == ' ' || byte == '\t' || byte == '\n' || byte == '\r') {
            // Never emit a leading space, and let a run collapse: the space is
            // written out only when a real character follows it.
            pendingSpace = !out.empty();
            continue;
        }
        if (byte < 0x20u || byte == 0x7Fu) {
            continue;
        }
        if (pendingSpace) {
            out.push_back(' ');
            pendingSpace = false;
        }
        out.push_back(raw);
    }
    // A pending space at the end is the trailing whitespace of the trim.
    return out;
}

/// Keep the newest words of a partial: at most kMaxPartialBytes bytes, cut on
/// a UTF-8 code-point boundary, prefixed with "..." when the head was
/// dropped.
std::string capPartialTail(const std::string &text) {
    if (text.size() <= kMaxPartialBytes) {
        return text;
    }
    size_t start = text.size() - kMaxPartialBytes;
    // 0b10xxxxxx is a continuation byte, the middle of a code point. Step
    // forward to the start of the next one: cutting mid-sequence would put
    // mojibake in the indicator instead of a truncated word.
    while (start < text.size() &&
           (static_cast<unsigned char>(text[start]) & 0xC0u) == 0x80u) {
        ++start;
    }
    while (start < text.size() && text[start] == ' ') {
        ++start;
    }
    std::string out;
    out.reserve(text.size() - start + 3);
    out += "...";
    if (start >= text.size()) {
        // Only reachable if the tail is nothing but continuation bytes, which
        // valid UTF-8 cannot produce. Show the ellipsis alone rather than
        // returning text longer than the cap.
        return out;
    }
    out.append(text, start, std::string::npos);
    return out;
}

/// A minimal, allocation-free cursor over one JSON document.
///
/// Only what the protocol needs is supported: objects, strings, and skipping of
/// anything else (numbers, null, booleans, nested containers). It never throws
/// and never reads out of bounds; every method reports failure instead.
class JsonCursor {
public:
    explicit JsonCursor(std::string_view text) : text_(text) {}

    bool atEnd() const { return pos_ >= text_.size(); }

    char peek() const { return atEnd() ? '\0' : text_[pos_]; }

    void advance() {
        if (!atEnd()) {
            ++pos_;
        }
    }

    void skipSpace() {
        while (!atEnd()) {
            const char c = text_[pos_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                ++pos_;
            } else {
                break;
            }
        }
    }

    /// Consume the given structural character, ignoring leading whitespace.
    bool consume(char expected) {
        skipSpace();
        if (atEnd() || text_[pos_] != expected) {
            return false;
        }
        ++pos_;
        return true;
    }

    /// Parse a JSON string at the cursor into out, decoding all escapes.
    bool parseString(std::string &out) {
        skipSpace();
        if (atEnd() || text_[pos_] != '"') {
            return false;
        }
        ++pos_;
        out.clear();
        while (!atEnd()) {
            const char c = text_[pos_];
            ++pos_;
            if (c == '"') {
                return true;
            }
            if (c != '\\') {
                out.push_back(c);
                continue;
            }
            if (atEnd()) {
                return false;
            }
            const char escape = text_[pos_];
            ++pos_;
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
                uint32_t codePoint = 0;
                if (!readHex4(codePoint)) {
                    return false;
                }
                if (codePoint >= 0xD800u && codePoint <= 0xDBFFu) {
                    // High surrogate: a following low surrogate completes the
                    // pair. Anything else is a lone surrogate, which is not
                    // representable in UTF-8 and becomes U+FFFD.
                    if (pos_ + 1 < text_.size() && text_[pos_] == '\\' &&
                        text_[pos_ + 1] == 'u') {
                        const size_t saved = pos_;
                        pos_ += 2;
                        uint32_t low = 0;
                        if (!readHex4(low)) {
                            return false;
                        }
                        if (low >= 0xDC00u && low <= 0xDFFFu) {
                            codePoint =
                                0x10000u + ((codePoint - 0xD800u) << 10) +
                                (low - 0xDC00u);
                        } else {
                            // Not a pair: rewind so the escape we just read is
                            // decoded on its own on the next iteration.
                            pos_ = saved;
                            codePoint = 0xFFFDu;
                        }
                    } else {
                        codePoint = 0xFFFDu;
                    }
                } else if (codePoint >= 0xDC00u && codePoint <= 0xDFFFu) {
                    codePoint = 0xFFFDu;
                }
                appendUtf8(codePoint, out);
                break;
            }
            default:
                return false;
            }
        }
        // Unterminated string.
        return false;
    }

    /// Skip one complete JSON value of any type, including nested containers.
    bool skipValue(int depth) {
        if (depth > 32) {
            return false;
        }
        skipSpace();
        if (atEnd()) {
            return false;
        }
        const char c = text_[pos_];
        if (c == '"') {
            std::string ignored;
            return parseString(ignored);
        }
        if (c == '{' || c == '[') {
            const char closing = (c == '{') ? '}' : ']';
            ++pos_;
            skipSpace();
            if (!atEnd() && text_[pos_] == closing) {
                ++pos_;
                return true;
            }
            for (;;) {
                if (c == '{') {
                    std::string key;
                    if (!parseString(key)) {
                        return false;
                    }
                    if (!consume(':')) {
                        return false;
                    }
                }
                if (!skipValue(depth + 1)) {
                    return false;
                }
                skipSpace();
                if (atEnd()) {
                    return false;
                }
                if (text_[pos_] == ',') {
                    ++pos_;
                    continue;
                }
                if (text_[pos_] == closing) {
                    ++pos_;
                    return true;
                }
                return false;
            }
        }
        // Number, true, false or null: consume up to the next delimiter.
        const size_t start = pos_;
        while (!atEnd()) {
            const char d = text_[pos_];
            if (d == ',' || d == '}' || d == ']' || d == ' ' || d == '\t' ||
                d == '\r' || d == '\n') {
                break;
            }
            ++pos_;
        }
        return pos_ > start;
    }

private:
    /// Read exactly four hexadecimal digits starting at the cursor.
    bool readHex4(uint32_t &value) {
        if (pos_ + 4 > text_.size()) {
            return false;
        }
        uint32_t result = 0;
        for (size_t i = 0; i < 4; ++i) {
            const char c = text_[pos_ + i];
            result <<= 4;
            if (c >= '0' && c <= '9') {
                result |= static_cast<uint32_t>(c - '0');
            } else if (c >= 'a' && c <= 'f') {
                result |= static_cast<uint32_t>(c - 'a' + 10);
            } else if (c >= 'A' && c <= 'F') {
                result |= static_cast<uint32_t>(c - 'A' + 10);
            } else {
                return false;
            }
        }
        pos_ += 4;
        value = result;
        return true;
    }

    std::string_view text_;
    size_t pos_ = 0;
};

/// The fields of a server -> client message that this addon cares about.
/// Everything else (v, duration_ms, segments) is skipped by the parser.
struct DictationMessage {
    std::string type;
    std::string state;
    std::string text;
    std::string id;
    std::string code;
    std::string message;
    std::string languageCode;
};

/// Store one string-valued top level field, ignoring the ones we do not use.
void applyField(DictationMessage &message, const std::string &key,
                std::string &value) {
    if (key == "type") {
        message.type = std::move(value);
    } else if (key == "state") {
        message.state = std::move(value);
    } else if (key == "text") {
        message.text = std::move(value);
    } else if (key == "id") {
        message.id = std::move(value);
    } else if (key == "code") {
        message.code = std::move(value);
    } else if (key == "message") {
        message.message = std::move(value);
    } else if (key == "language_code") {
        message.languageCode = std::move(value);
    }
}

/// Parse one protocol line. Returns false for anything that is not a JSON
/// object; the caller then ignores the line, which is the safe reaction to a
/// malformed or truncated message.
bool parseDictationMessage(std::string_view line, DictationMessage &message) {
    JsonCursor cursor(line);
    if (!cursor.consume('{')) {
        return false;
    }
    cursor.skipSpace();
    if (!cursor.atEnd() && cursor.peek() == '}') {
        cursor.advance();
        return true;
    }
    for (;;) {
        std::string key;
        if (!cursor.parseString(key)) {
            return false;
        }
        if (!cursor.consume(':')) {
            return false;
        }
        cursor.skipSpace();
        if (cursor.peek() == '"') {
            std::string value;
            if (!cursor.parseString(value)) {
                return false;
            }
            applyField(message, key, value);
        } else if (!cursor.skipValue(0)) {
            return false;
        }
        cursor.skipSpace();
        if (cursor.atEnd()) {
            return false;
        }
        if (cursor.peek() == ',') {
            cursor.advance();
            continue;
        }
        if (cursor.peek() == '}') {
            cursor.advance();
            return true;
        }
        return false;
    }
}

/// 32 lowercase hex characters, used as the protocol command id. Uniqueness
/// only has to hold within one daemon lifetime, so mt19937_64 is plenty.
std::string generateId() {
    static std::mt19937_64 engine(std::random_device{}());
    static constexpr char kHexDigits[] = "0123456789abcdef";
    std::string id;
    id.reserve(32);
    uint64_t value = 0;
    for (int i = 0; i < 32; ++i) {
        if (i % 16 == 0) {
            value = engine();
        }
        id.push_back(kHexDigits[(value >> ((i % 16) * 4)) & 0xFu]);
    }
    return id;
}

bool endsWithWhitespace(const std::string &text) {
    if (text.empty()) {
        return false;
    }
    switch (text.back()) {
    case ' ':
    case '\t':
    case '\n':
    case '\r':
    case '\f':
    case '\v':
        return true;
    default:
        return false;
    }
}

/// Turn a protocol error code into a sentence a user can act on. The daemon's
/// own message is appended by the caller when it adds information.
std::string friendlyErrorText(const std::string &code) {
    if (code == "busy") {
        return _("Another recording is already in progress.");
    }
    if (code == "bad_request") {
        return _("The dictation daemon rejected the request.");
    }
    if (code == "no_api_key") {
        return _("Set your Sarvam API key in "
                 "~/.config/sarvam-dictation/config.toml");
    }
    if (code == "no_audio_device") {
        return _("No microphone was found. Check your audio setup.");
    }
    if (code == "audio_failed") {
        return _("Recording failed. Check your microphone.");
    }
    if (code == "network") {
        return _("Could not reach the Sarvam speech-to-text service.");
    }
    if (code == "api") {
        return _("The Sarvam speech-to-text API returned an error.");
    }
    if (code == "too_long") {
        return _("That recording was too long. Try a shorter dictation.");
    }
    if (code == "internal") {
        return _("The dictation daemon hit an internal error.");
    }
    if (code == "config_invalid") {
        return _("The dictation settings cannot be used with the chosen "
                 "model.");
    }
    return _("Dictation failed.");
}

/// Match an already-normalised event key against a configured key list.
///
/// Deliberately hand-rolled over Key::check rather than KeyEvent::checkKeyList
/// with KeyEventMatchingMode: that API only exists in fcitx 5.2+, and this
/// addon has to build against the 5.1.x series that Debian, Ubuntu and Fedora
/// currently ship. Key::check compares the normalised keysym and modifiers,
/// which is the same semantics the newer helper provides for our use.
bool matchesKeyList(const Key &key, const KeyList &keys) {
    for (const auto &candidate : keys) {
        if (key.check(candidate)) {
            return true;
        }
    }
    return false;
}

/// The value of an environment variable, or an empty string when it is unset.
/// isPlasmaDesktop() takes plain strings, so an unset variable must not be
/// passed as a null pointer.
std::string environmentValue(const char *name) {
    const char *value = std::getenv(name);
    return value ? std::string(value) : std::string();
}

} // namespace

FCITX_DEFINE_LOG_CATEGORY(sarvamdictation_log, "sarvamdictation");

SarvamDictation::SarvamDictation(Instance *instance) : instance_(instance) {
    // PreInputMethod so the hotkey is seen before any input method gets a
    // chance to swallow the key; filterAndAccept keeps it away from the client.
    // The runtime-typed watchEvent(EventType, phase, EventHandler) overload is
    // used deliberately: the templated watchEvent<EventType::...>(phase, cb)
    // form only exists in fcitx 5.2+, and this addon must build on 5.1.x. The
    // cast is sound because we registered for exactly this event type.
    eventHandlers_.emplace_back(instance_->watchEvent(
        EventType::InputContextKeyEvent, EventWatcherPhase::PreInputMethod,
        [this](Event &event) {
            auto &keyEvent = static_cast<KeyEvent &>(event);
            const bool pushToTalkKey = matchesKeyList(
                keyEvent.key(), config_.pushToTalkKey.value());

            // A press and its release both arrive here. The release is the
            // event that ends a push-to-talk hold; the toggle key still acts on
            // the press alone, so a release of anything else stays ignored.
            if (keyEvent.isRelease()) {
                if (pushToTalkKey && pushToTalkActive_) {
                    keyEvent.filterAndAccept();
                    endPushToTalk();
                }
                return;
            }

            if (pushToTalkKey) {
                // Ours either way, so the client never sees the key.
                keyEvent.filterAndAccept();
                if (pushToTalkActive_) {
                    // X11 and Wayland deliver a press on every auto-repeat
                    // interval while the key is held. Starting a session per
                    // repeat would send "start" dozens of times a second and
                    // the daemon would answer "busy" for all but the first.
                    return;
                }
                if (lastState_ == "recording" || lastState_ == "transcribing") {
                    // A recording started with the toggle key is already in
                    // flight. "start" would be answered with "busy", and the
                    // release would then send "cancel" - which is legal in
                    // every state and would therefore silently throw away the
                    // recording the toggle key started. Leave that session
                    // alone; the key press does nothing.
                    FCITX_SARVAMDICTATION_DEBUG()
                        << "push-to-talk ignored, the daemon is already "
                        << lastState_;
                    return;
                }
                startPushToTalk(keyEvent.inputContext());
                return;
            }

            if (!matchesKeyList(keyEvent.key(), config_.triggerKey.value())) {
                return;
            }
            if (pushToTalkActive_) {
                // A hold is in progress. Toggling now would stop the recording
                // that the release is about to stop, so the two modes would
                // interleave; leave the event to the client untouched.
                return;
            }
            keyEvent.filterAndAccept();
            onHotkey(keyEvent.inputContext());
        }));

    // A hold ends on a key release, and the release goes to whatever window has
    // focus at that moment. If focus moves while the key is held - alt-tab, a
    // window closing, a dialog appearing - the key-up never reaches us and the
    // microphone would stay open. Treat the focus change as the release it
    // effectively is. Registered here so eventHandlers_ owns its lifetime.
    eventHandlers_.emplace_back(instance_->watchEvent(
        EventType::InputContextFocusOut, EventWatcherPhase::PreInputMethod,
        [this](Event &) {
            // The composition text lives in the window that had focus, so it
            // goes before anything else: left behind, it would strand a
            // composition in a field nobody is looking at any more.
            clearPreedit();
            if (pushToTalkActive_) {
                FCITX_SARVAMDICTATION_DEBUG()
                    << "focus lost during push-to-talk, ending the recording";
                endPushToTalk();
            }
        }));

    // Two failure modes: some backends throw EventLoopException, others hand
    // back a null source. Handle both — an exception escaping the addon
    // constructor would take fcitx5 down with it.
    try {
        retryTimer_ = instance_->eventLoop().addTimeEvent(
            CLOCK_MONOTONIC, now(CLOCK_MONOTONIC), 0,
            [this](EventSourceTime *, uint64_t) { return onRetryTimer(); });
    } catch (const EventLoopException &) {
        retryTimer_.reset();
    }
    if (retryTimer_) {
        retryTimer_->setEnabled(false);
    }

    // The partial transcript gets its own trailing-edge timer, created the same
    // way and with the same two failure modes handled: a backend that throws
    // EventLoopException, and one that returns a null source. A missing timer
    // is not fatal -- handleLine() falls back to showing a partial at once.
    try {
        partialTimer_ = instance_->eventLoop().addTimeEvent(
            CLOCK_MONOTONIC, now(CLOCK_MONOTONIC), 0,
            [this](EventSourceTime *, uint64_t) { return onPartialTimer(); });
    } catch (const EventLoopException &) {
        partialTimer_.reset();
    }
    if (partialTimer_) {
        partialTimer_->setEnabled(false);
    }

    reloadConfig();
}

SarvamDictation::~SarvamDictation() { closeSocket(); }

void SarvamDictation::reloadConfig() {
    // Two files, one config page: the addon's own settings come from the fcitx5
    // conf, the daemon's settings from the TOML file the daemon reads too.
    readAsIni(config_, "conf/sarvamdictation.conf");
    const std::string path = sarvam::daemonConfigPath();
    std::string error;
    if (!sarvam::loadDaemonConfig(config_, path, &error)) {
        // Never fail the addon over a bad file: the daemon settings keep their
        // defaults and the user sees the reason when they press Apply.
        FCITX_SARVAMDICTATION_DEBUG()
            << "cannot read " << path << ": " << error;
    } else if (!config_.apiKey.value().empty()) {
        // The config page must never show the key. Remember it here and put
        // the sentinel in the option; setConfig() turns the sentinel back
        // into the remembered key before saving. Only a successful load is
        // masked, so a sentinel left in the option by an earlier reload can
        // never be mistaken for a key.
        storedApiKey_ = config_.apiKey.value();
        config_.apiKey.setValue(std::string(kMaskedApiKey));
    }
}

void SarvamDictation::setConfig(const RawConfig &config) {
    // Read before the load, because switching the new option off has to take
    // the composition that is already in the field away now.
    const bool preeditPartialsBefore = config_.preeditPartials.value();
    config_.load(config, true);

    // The API key field is write-only. Unchanged sentinel: put the stored
    // key back. Empty field: the user cleared it, so forget the key. Anything
    // else is a new key and becomes the one we keep. This runs before
    // saveDaemonConfig(), so the sentinel can never reach config.toml.
    if (config_.apiKey.value() == kMaskedApiKey) {
        config_.apiKey.setValue(storedApiKey_);
    } else if (config_.apiKey.value().empty()) {
        storedApiKey_.clear();
    } else {
        storedApiKey_ = config_.apiKey.value();
    }

    // Switching the transcript off while a recording is streaming must take
    // the text that is already on screen away now, rather than leaving it
    // there until the recording ends. The state messages are untouched: they
    // travel through showRecordingIndicator(), not through the partial path.
    // With PreeditPartials on the text lives in the client's field rather than
    // in the indicator, so the switch that owns the visible text is the one
    // that clears it -- and turning PreeditPartials itself off has to clear the
    // composition too, because nothing else would do it until the recording
    // ends, which would leave the field in composition.
    const bool preeditJustDisabled =
        preeditPartialsBefore && !config_.preeditPartials.value();
    const bool textOff =
        !config_.showPartialText.value() && !config_.preeditPartials.value();
    if (preeditJustDisabled || textOff) {
        clearPartialDisplay();
    }

    // Only the addon's own options may reach the fcitx5 conf. Writing the whole
    // Configuration would put the API key into a second file, which is exactly
    // what storing the daemon settings in config.toml is meant to avoid.
    RawConfig addonOnly;
    marshallOption(addonOnly["TriggerKey"], config_.triggerKey.value());
    marshallOption(addonOnly["PushToTalkKey"],
                   config_.pushToTalkKey.value());
    addonOnly.setValueByPath("AutoStartDaemon",
                             config_.autoStartDaemon.value() ? "True" : "False");
    addonOnly.setValueByPath("DaemonCommand", config_.daemonCommand.value());
    addonOnly.setValueByPath("NotifyOnError",
                             config_.notifyOnError.value() ? "True" : "False");
    addonOnly.setValueByPath(
        "ShowRecordingTips",
        config_.showRecordingTips.value() ? "True" : "False");
    addonOnly.setValueByPath(
        "ShowPartialText",
        config_.showPartialText.value() ? "True" : "False");
    addonOnly.setValueByPath(
        "PreeditPartials",
        config_.preeditPartials.value() ? "True" : "False");
    addonOnly.setValueByPath("AppendSpace",
                             config_.appendSpace.value() ? "True" : "False");
    safeSaveAsIni(addonOnly, "conf/sarvamdictation.conf");

    const std::string path = sarvam::daemonConfigPath();
    std::string error;
    if (!sarvam::saveDaemonConfig(config_, path, &error)) {
        FCITX_SARVAMDICTATION_DEBUG()
            << "cannot write " << path << ": " << error;
        showErrorNotification(_("Could not save settings"), error);
        return;
    }

    // Mask the key again straight away. The configuration page reads
    // getConfig() back after Apply, so without this the key the user just typed
    // would stay visible in the field until the next reload.
    if (!storedApiKey_.empty()) {
        config_.apiKey.setValue(std::string(kMaskedApiKey));
    }

    // A daemon that is already running still holds the old values; tell it to
    // re-read the file it just got.
    sendReload();
}

void SarvamDictation::sendReload() {
    const std::string id = generateId();
    queueCommand("{\"v\":1,\"cmd\":\"reload\",\"id\":\"" + id + "\"}");
}

std::string SarvamDictation::socketPath() const {
    const std::string configured = config_.socketPath.value();
    if (!configured.empty()) {
        return configured;
    }
    // Read the environment directly instead of going through StandardPaths so
    // that the C++ and Python sides agree on the path byte for byte.
    const char *runtimeDir = std::getenv("XDG_RUNTIME_DIR");
    if (runtimeDir && *runtimeDir) {
        return std::string(runtimeDir) + "/sarvam-dictation.sock";
    }
    return "/tmp/sarvam-dictation-" + std::to_string(::getuid()) + ".sock";
}

void SarvamDictation::onHotkey(InputContext *inputContext) {
    // Remember who had focus: the transcript may arrive seconds later, after
    // the user switched windows or the client went away entirely. A
    // TrackableObjectReference can only be obtained through watch() — it has
    // no pointer assignment — and it goes null by itself once the client dies.
    if (inputContext) {
        targetInputContext_ = inputContext->watch();
    } else {
        targetInputContext_.unwatch();
    }

    const std::string id = generateId();
    pendingIds_.push_back(id);
    if (pendingIds_.size() > kMaxPendingIds) {
        pendingIds_.erase(pendingIds_.begin());
    }
    FCITX_SARVAMDICTATION_DEBUG() << "hotkey pressed, command id " << id;
    queueCommand("{\"v\":1,\"cmd\":\"toggle\",\"id\":\"" + id + "\"}");
}

void SarvamDictation::startPushToTalk(InputContext *inputContext) {
    pushToTalkActive_ = true;
    pushToTalkStartUsec_ = now(CLOCK_MONOTONIC);
    // Our "start" is about to change the daemon's state. Whatever was broadcast
    // before this press describes a previous recording (or none at all) and must
    // not be what the release decides on.
    lastState_.clear();

    if (inputContext) {
        targetInputContext_ = inputContext->watch();
    } else {
        targetInputContext_.unwatch();
    }

    const std::string id = generateId();
    pendingIds_.push_back(id);
    if (pendingIds_.size() > kMaxPendingIds) {
        pendingIds_.erase(pendingIds_.begin());
    }
    FCITX_SARVAMDICTATION_DEBUG() << "push-to-talk pressed, command id " << id;
    queueCommand("{\"v\":1,\"cmd\":\"start\",\"id\":\"" + id + "\"}");
}

void SarvamDictation::endPushToTalk() {
    if (!pushToTalkActive_) {
        return;
    }
    pushToTalkActive_ = false;
    const uint64_t heldUsec = now(CLOCK_MONOTONIC) - pushToTalkStartUsec_;
    pushToTalkStartUsec_ = 0;

    // Two reasons to cancel rather than stop:
    //  - the hold was a tap, and a tap must not upload 200 ms of silence and
    //    paste the (empty or wrong) transcript that comes back;
    //  - the daemon is not known to be recording, either because "start" failed
    //    or because it already auto-stopped at max_duration_s. "stop" outside
    //    "recording" is answered with "busy", which would put a second error on
    //    top of the real one the user already saw. "cancel" is legal in every
    //    state, so it cannot produce an error of its own.
    const bool tooShort = heldUsec < kPushToTalkMinHoldUsec;
    const bool recording = lastState_ == "recording";
    const std::string id = generateId();
    if (tooShort || !recording) {
        FCITX_SARVAMDICTATION_DEBUG()
            << "push-to-talk released after " << heldUsec
            << " us, last state '" << lastState_ << "', sending cancel";
        // A cancelled recording leaves nothing worth showing, and its partial
        // must not surface on top of whatever comes next.
        clearPartialDisplay();
        queueCommand("{\"v\":1,\"cmd\":\"cancel\",\"id\":\"" + id + "\"}");
        return;
    }
    // A result carries the id of the stop that produced it, so this id has to
    // be remembered or the transcript would be dropped as stale.
    pendingIds_.push_back(id);
    if (pendingIds_.size() > kMaxPendingIds) {
        pendingIds_.erase(pendingIds_.begin());
    }
    FCITX_SARVAMDICTATION_DEBUG()
        << "push-to-talk released after " << heldUsec << " us, sending stop";
    queueCommand("{\"v\":1,\"cmd\":\"stop\",\"id\":\"" + id + "\"}");
}

void SarvamDictation::queueCommand(const std::string &line) {
    pendingLines_.push_back(line);
    if (pendingLines_.size() > kMaxPendingLines) {
        pendingLines_.erase(pendingLines_.begin());
    }
    if (fd_ >= 0) {
        if (!connecting_) {
            flushPending();
        }
        return;
    }
    startConnectAttempt(true);
}

void SarvamDictation::startConnectAttempt(bool firstAttempt) {
    if (fd_ >= 0) {
        return;
    }
    if (firstAttempt) {
        // A fresh trigger gets a fresh spawn budget: at most one spawn and one
        // 25 attempt retry window per hotkey press.
        retriesLeft_ = 0;
        spawnAttempted_ = false;
    }

    int error = 0;
    const ConnectResult result = connectOnce(&error);
    if (result == ConnectResult::Connected) {
        queueReloadOnConnect();
        flushPending();
        return;
    }
    if (result == ConnectResult::InProgress) {
        return;
    }

    if (firstAttempt && (error == ENOENT || error == ECONNREFUSED) &&
        config_.autoStartDaemon.value() && !spawnAttempted_) {
        spawnAttempted_ = true;
        FCITX_SARVAMDICTATION_DEBUG()
            << "daemon socket missing, spawning " << config_.daemonCommand.value();
        spawnDaemon();
        retriesLeft_ = kRetryBudget;
        scheduleRetry();
        return;
    }

    if (retriesLeft_ > 0) {
        scheduleRetry();
        return;
    }

    reportUnreachable();
}

SarvamDictation::ConnectResult
SarvamDictation::connectOnce(int *errorOut) {
    if (errorOut) {
        *errorOut = 0;
    }
    if (fd_ >= 0) {
        return ConnectResult::Connected;
    }

    // Release a source left behind by a previous, already closed connection.
    ioEvent_.reset();

    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    const std::string path = socketPath();
    if (path.empty() || path.size() + 1 > sizeof(address.sun_path)) {
        if (errorOut) {
            *errorOut = ENAMETOOLONG;
        }
        return ConnectResult::Failed;
    }
    std::memcpy(address.sun_path, path.c_str(), path.size());

    const int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0) {
        if (errorOut) {
            *errorOut = errno;
        }
        return ConnectResult::Failed;
    }
    const int statusFlags = ::fcntl(fd, F_GETFL, 0);
    if (statusFlags >= 0) {
        ::fcntl(fd, F_SETFL, statusFlags | O_NONBLOCK);
    }
    const int descriptorFlags = ::fcntl(fd, F_GETFD, 0);
    if (descriptorFlags >= 0) {
        ::fcntl(fd, F_SETFD, descriptorFlags | FD_CLOEXEC);
    }

    const int ret = ::connect(fd, reinterpret_cast<sockaddr *>(&address),
                              static_cast<socklen_t>(sizeof(address)));
    if (ret == 0) {
        fd_ = fd;
        connecting_ = false;
        readBuffer_.clear();
        writeBuffer_.clear();
        setupIOEvent(IOEventFlag::In);
        if (!ioEvent_) {
            ::close(fd_);
            fd_ = -1;
            if (errorOut) {
                *errorOut = EIO;
            }
            return ConnectResult::Failed;
        }
        retriesLeft_ = 0;
        spawnAttempted_ = false;
        FCITX_SARVAMDICTATION_DEBUG() << "connected to " << path;
        return ConnectResult::Connected;
    }

    const int error = errno;
    if (error == EINPROGRESS) {
        fd_ = fd;
        connecting_ = true;
        readBuffer_.clear();
        writeBuffer_.clear();
        setupIOEvent(IOEventFlag::Out);
        if (!ioEvent_) {
            ::close(fd_);
            fd_ = -1;
            connecting_ = false;
            if (errorOut) {
                *errorOut = EIO;
            }
            return ConnectResult::Failed;
        }
        return ConnectResult::InProgress;
    }

    ::close(fd);
    if (errorOut) {
        *errorOut = error;
    }
    return ConnectResult::Failed;
}

void SarvamDictation::setupIOEvent(IOEventFlags flags) {
    try {
        ioEvent_ = instance_->eventLoop().addIOEvent(
            fd_, flags,
            [this](EventSourceIO *source, int fd, IOEventFlags eventFlags) {
                return handleIO(source, fd, eventFlags);
            });
    } catch (const EventLoopException &) {
        ioEvent_.reset();
    }
    if (!ioEvent_) {
        // The event loop refused the fd; drop the socket rather than leaking it.
        if (fd_ >= 0) {
            ::close(fd_);
            fd_ = -1;
        }
        connecting_ = false;
    }
}

bool SarvamDictation::handleIO(EventSourceIO *source, int fd,
                               IOEventFlags flags) {
    if (fd_ < 0 || fd != fd_) {
        return false;
    }

    if (connecting_) {
        // The non-blocking connect finished; SO_ERROR carries its verdict.
        int error = 0;
        socklen_t length = static_cast<socklen_t>(sizeof(error));
        if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &length) != 0) {
            error = errno;
        }
        if (error != 0) {
            closeSocket();
            if (retriesLeft_ > 0) {
                scheduleRetry();
            } else {
                reportUnreachable();
            }
            return false;
        }
        connecting_ = false;
        if (source) {
            source->setEvents(IOEventFlag::In);
        }
        FCITX_SARVAMDICTATION_DEBUG() << "connect completed";
        queueReloadOnConnect();
        return flushPending();
    }

    if (flags.test(IOEventFlag::Out) && !writeBuffer_.empty()) {
        if (!writeOut()) {
            return false;
        }
    }
    if (flags.test(IOEventFlag::In) || flags.test(IOEventFlag::Hup) ||
        flags.test(IOEventFlag::Err)) {
        if (!readAvailable()) {
            return false;
        }
    }
    return true;
}

bool SarvamDictation::readAvailable() {
    char chunk[kReadChunk];
    for (int iteration = 0; iteration < kMaxReadsPerCallback; ++iteration) {
        if (fd_ < 0) {
            return false;
        }
        const ssize_t received = ::recv(fd_, chunk, sizeof(chunk), 0);
        if (received > 0) {
            readBuffer_.append(chunk, static_cast<size_t>(received));
            processReadBuffer();
            if (fd_ < 0) {
                return false;
            }
            if (readBuffer_.size() > kMaxLine) {
                // A single line grew past the protocol limit: drop the peer.
                closeSocket();
                return false;
            }
            continue;
        }
        if (received == 0) {
            // Orderly EOF: the daemon went away. The next hotkey reconnects.
            FCITX_SARVAMDICTATION_DEBUG() << "daemon closed the connection";
            closeSocket();
            return false;
        }
        if (errno == EINTR) {
            continue;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            return true;
        }
        closeSocket();
        return false;
    }
    return true;
}

void SarvamDictation::processReadBuffer() {
    size_t newline = 0;
    while (fd_ >= 0 && (newline = readBuffer_.find('\n')) != std::string::npos) {
        if (newline > kMaxLine) {
            closeSocket();
            return;
        }
        std::string line = readBuffer_.substr(0, newline);
        readBuffer_.erase(0, newline + 1);
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (!line.empty()) {
            handleLine(line);
        }
    }
    if (fd_ >= 0 && readBuffer_.size() > kMaxLine) {
        closeSocket();
    }
}

void SarvamDictation::handleLine(std::string_view line) {
    DictationMessage message;
    if (!parseDictationMessage(line, message)) {
        FCITX_SARVAMDICTATION_DEBUG() << "ignoring unparsable protocol line";
        return;
    }

    if (message.type == "partial") {
        // Either destination alone is reason enough to keep the text: the
        // composition in the field, or the text on the indicator.
        const bool preedit = config_.preeditPartials.value();
        if (!preedit && !config_.showPartialText.value()) {
            // The user asked for state feedback without the text, in the field
            // and in the indicator alike. Nothing is stored and no timer is
            // armed, so a partial costs nothing at all.
            return;
        }
        // A live partial transcript of the recording in progress. It is not
        // shown here: it arrives about ten times a second, every indicator
        // update forks a D-Bus client, and Plasma restarts the OSD's fade-in
        // on each call, so showing each one makes the indicator flash. The
        // text is sanitised and kept in pendingPartial_, and the trailing-edge
        // timer shows it at most about three times a second with the newest
        // text always landing. This path builds no notification and logs
        // nothing.
        //
        // The text is kept whole, not tail-capped: the field is the real
        // destination, and a tail that starts with "..." would be wrong there,
        // so the cap is applied in onPartialTimer(), on the indicator copy.
        const std::string text = sanitisePartialText(message.text);
        if (text.empty()) {
            // Nothing but whitespace or control characters. Show nothing, and
            // leave lastShownPartial_ untouched so a later identical redraw is
            // still suppressed.
            return;
        }
        pendingPartial_ = text;
        if (!partialTimer_) {
            // The event loop refused the timer, so there is no trailing edge
            // to wait for: show it now rather than lose the indicator.
            onPartialTimer();
            return;
        }
        if (!partialTimerArmed_) {
            armPartialTimer();
        }
        return;
    }

    if (message.type == "state") {
        // The daemon broadcasts its state to every client, so this is the only
        // authoritative answer to "am I recording?". The push-to-talk release
        // reads it; see endPushToTalk().
        lastState_ = message.state;
        // Whatever partial was on screen described the recording that just
        // ended, so the next one starts from a clean slate: the pending text,
        // the timer, the remembered text and any composition in the field all
        // go. This runs for every state, so "idle" leaves no composition
        // behind either.
        clearPartialDisplay();
        if (message.state == "recording") {
            showRecordingIndicator(kAppIcon, _("Listening"),
                                   _("Press the hotkey again to stop."));
        } else if (message.state == "transcribing") {
            showRecordingIndicator(kAppIcon, _("Transcribing"),
                                   _("Converting your speech to text."));
        } else if (message.state == "idle") {
            // The OSD has no handle to close: Plasma's indicator fades out on
            // its own, so only the tip is dismissed here.
            closeTip();
        }
        return;
    }

    if (message.type == "result") {
        // The final transcript supersedes any partial of this recording.
        clearPartialDisplay();
        // Only act on a transcript we asked for. An empty id cannot be matched
        // and is accepted, because a daemon that lost its id is still telling
        // us about the recording we started.
        bool ours = message.id.empty();
        if (!message.id.empty()) {
            auto iter =
                std::find(pendingIds_.begin(), pendingIds_.end(), message.id);
            if (iter != pendingIds_.end()) {
                pendingIds_.erase(iter);
                ours = true;
            }
        }
        if (!ours) {
            FCITX_SARVAMDICTATION_DEBUG() << "dropping stale result";
            return;
        }
        commitTranscript(message.text);
        return;
    }

    if (message.type == "error") {
        // The recording this error belongs to is over, and nothing further of
        // it will arrive to clear a composition: the pending partial goes too,
        // so the trailing-edge timer cannot compose it after the error.
        clearPartialDisplay();
        if (!message.id.empty()) {
            auto iter =
                std::find(pendingIds_.begin(), pendingIds_.end(), message.id);
            if (iter != pendingIds_.end()) {
                pendingIds_.erase(iter);
            }
        }
        std::string body = friendlyErrorText(message.code);
        if (!message.message.empty()) {
            body += " (";
            body += message.message;
            body += ")";
        }
        showErrorNotification(_("Sarvam dictation error"), body);
        return;
    }

    // "pong" and anything from a newer protocol version: nothing to do.
}

bool SarvamDictation::flushPending() {
    if (fd_ < 0 || connecting_) {
        return false;
    }
    if (!pendingLines_.empty()) {
        for (const auto &line : pendingLines_) {
            writeBuffer_ += line;
            writeBuffer_ += '\n';
        }
        pendingLines_.clear();
    }
    return writeOut();
}

bool SarvamDictation::writeOut() {
    if (fd_ < 0) {
        return false;
    }
    size_t offset = 0;
    while (offset < writeBuffer_.size()) {
        const ssize_t sent = ::send(fd_, writeBuffer_.data() + offset,
                                    writeBuffer_.size() - offset, kSendFlags);
        if (sent > 0) {
            offset += static_cast<size_t>(sent);
            continue;
        }
        if (sent < 0 && errno == EINTR) {
            continue;
        }
        if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            // The daemon is not draining its socket right now; ask the event
            // loop to tell us when it is.
            break;
        }
        closeSocket();
        return false;
    }
    if (offset > 0) {
        writeBuffer_.erase(0, offset);
    }
    if (writeBuffer_.size() > kMaxLine) {
        // The peer never drains; give up rather than grow without bound.
        closeSocket();
        return false;
    }
    if (ioEvent_) {
        if (writeBuffer_.empty()) {
            ioEvent_->setEvents(IOEventFlag::In);
        } else {
            ioEvent_->setEvents(IOEventFlags{IOEventFlag::In, IOEventFlag::Out});
        }
    }
    return fd_ >= 0;
}

void SarvamDictation::closeSocket() {
    if (ioEvent_) {
        // Disable rather than destroy: this may be running inside the IO
        // callback itself. The object is released by the next connect attempt
        // or by the destructor.
        ioEvent_->setEnabled(false);
    }
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
    connecting_ = false;
    readBuffer_.clear();
    writeBuffer_.clear();
    // Queued commands refer to a connection that no longer exists; replaying a
    // stale "toggle" later would be worse than dropping it.
    pendingLines_.clear();
    reloadQueued_ = false;
}

void SarvamDictation::queueReloadOnConnect() {
    if (reloadQueued_) {
        return;
    }
    reloadQueued_ = true;
    // A daemon started long before this fcitx5 session may still be running on
    // an older config.toml, and the settings may have been changed in
    // fcitx5-configtool while it was not running. Re-read the file before the
    // queued command starts a recording, hence the insert at the front.
    const std::string id = generateId();
    pendingLines_.insert(
        pendingLines_.begin(),
        "{\"v\":1,\"cmd\":\"reload\",\"id\":\"" + id + "\"}");
}

void SarvamDictation::scheduleRetry() {
    if (!retryTimer_) {
        return;
    }
    retryTimer_->setTime(now(CLOCK_MONOTONIC) + kRetryIntervalUsec);
    retryTimer_->setOneShot();
    retryTimer_->setEnabled(true);
}

bool SarvamDictation::onRetryTimer() {
    if (fd_ >= 0) {
        return false;
    }
    if (pendingLines_.empty()) {
        retriesLeft_ = 0;
        return false;
    }
    if (retriesLeft_ > 0) {
        --retriesLeft_;
    }
    int error = 0;
    const ConnectResult result = connectOnce(&error);
    if (result == ConnectResult::Connected) {
        flushPending();
        return false;
    }
    if (result == ConnectResult::InProgress) {
        return false;
    }
    if (retriesLeft_ > 0) {
        scheduleRetry();
        return false;
    }
    reportUnreachable();
    return false;
}

void SarvamDictation::armPartialTimer() {
    if (!partialTimer_) {
        return;
    }
    partialTimer_->setTime(now(CLOCK_MONOTONIC) + kPartialIntervalUsec);
    partialTimer_->setOneShot();
    partialTimer_->setEnabled(true);
    partialTimerArmed_ = true;
}

bool SarvamDictation::onPartialTimer() {
    // Disarm before showing anything: showRecordingIndicator() runs D-Bus
    // code, and the next partial must be able to arm a fresh timer even if it
    // arrives while that runs.
    partialTimerArmed_ = false;
    if (partialTimer_) {
        partialTimer_->setEnabled(false);
    }
    if (pendingPartial_.empty() || pendingPartial_ == lastShownPartial_) {
        return false;
    }
    // Remember the text before showing it, so an identical partial that
    // arrives afterwards costs nothing.
    lastShownPartial_ = pendingPartial_;
    if (config_.preeditPartials.value()) {
        // The field already has the words, so the indicator keeps the state
        // text it last showed and is not touched here at all: every indicator
        // update forks a D-Bus client, and there is nothing new to say.
        setPreeditText(lastShownPartial_);
        return false;
    }
    // The indicator is one line, so it gets the newest words only.
    showRecordingIndicator(kAppIcon, capPartialTail(lastShownPartial_),
                           _("Release the key to finish."));
    return false;
}

void SarvamDictation::clearPartialDisplay() {
    // The composition goes first: this is the path every end of a recording
    // runs through, and a composition left behind would leave the user's field
    // stuck in composition, which is the worst failure this addon could have.
    clearPreedit();
    pendingPartial_.clear();
    lastShownPartial_.clear();
    partialTimerArmed_ = false;
    if (partialTimer_) {
        partialTimer_->setEnabled(false);
    }
}

void SarvamDictation::setPreeditText(const std::string &text) {
    InputContext *inputContext = targetInputContext_.get();
    if (!inputContext || !inputContext->hasFocus()) {
        // No focused field to compose into. Writing the preedit anyway would
        // put the words in a window the user is not looking at.
        return;
    }

    // The two branches are not decoration. InputContext::updatePreedit()
    // returns immediately when the client lacks CapabilityFlag::Preedit, so a
    // client without preedit support only ever shows the text through
    // setPreedit() plus updateUserInterface(InputPanel), which draws fcitx5's
    // own composing window.
    const bool useClientPreedit =
        inputContext->capabilityFlags().test(CapabilityFlag::Preedit);

    Text preedit;
    if (!text.empty()) {
        preedit.append(text, useClientPreedit ? TextFormatFlag::HighLight
                                              : TextFormatFlag::NoFlag);
    }
    if (useClientPreedit) {
        preedit.setCursor(0);
        inputContext->inputPanel().setClientPreedit(preedit);
    } else {
        preedit.setCursor(static_cast<int>(text.size()));
        inputContext->inputPanel().setPreedit(preedit);
    }
    inputContext->updatePreedit();
    inputContext->updateUserInterface(UserInterfaceComponent::InputPanel);
    preeditActive_ = true;
}

void SarvamDictation::clearPreedit() {
    if (!preeditActive_) {
        // Nothing of ours is composed, so there is nothing to take away. This
        // is what makes the call safe on every exit path and safe twice in a
        // row: an empty preedit sent to a client that never had one would only
        // be a spurious input-panel update.
        return;
    }
    preeditActive_ = false;
    // The words are no longer in the field, so the next partial that carries
    // the same text has to be composed again instead of being suppressed as a
    // duplicate of what is on screen.
    lastShownPartial_.clear();

    InputContext *inputContext = targetInputContext_.get();
    if (!inputContext) {
        // The window is gone and the composition went with it. Nothing left to
        // tell: there is no panel to clear and no client to notify.
        return;
    }
    // Clearing is the same call with an empty Text in both slots, followed by
    // both updates, and it is done whether or not the context still has focus:
    // the focus-out watcher runs precisely when it may not.
    inputContext->inputPanel().setClientPreedit(Text());
    inputContext->inputPanel().setPreedit(Text());
    inputContext->updatePreedit();
    inputContext->updateUserInterface(UserInterfaceComponent::InputPanel);
}

void SarvamDictation::reportUnreachable() {
    // The daemon is gone, so no message will ever arrive to clear a composition
    // this recording put in the field, and the pending partial would be
    // composed again by the trailing-edge timer a moment from now.
    clearPartialDisplay();
    pendingLines_.clear();
    pendingIds_.clear();
    retriesLeft_ = 0;
    spawnAttempted_ = false;
    FCITX_SARVAMDICTATION_DEBUG() << "daemon unreachable, giving up";
    showErrorNotification(
        _("Sarvam dictation: daemon not reachable"),
        _("Start sarvam-dictation-daemon, or enable AutoStartDaemon in the "
          "addon configuration."));
}

void SarvamDictation::spawnDaemon() {
    const std::string command = config_.daemonCommand.value();
    if (command.empty()) {
        return;
    }

    const pid_t intermediate = ::fork();
    if (intermediate < 0) {
        return;
    }
    if (intermediate == 0) {
        // First child: leave fcitx5's session and process group so the daemon
        // survives a fcitx5 restart, then fork the real daemon so it is never
        // a direct child of fcitx5 (nothing to reap, nothing to signal).
        ::setsid();
        const pid_t daemon = ::fork();
        if (daemon != 0) {
            ::_exit(0);
        }
        const int devNull = ::open("/dev/null", O_RDWR);
        if (devNull >= 0) {
            ::dup2(devNull, STDIN_FILENO);
            ::dup2(devNull, STDOUT_FILENO);
            ::dup2(devNull, STDERR_FILENO);
            if (devNull > STDERR_FILENO) {
                ::close(devNull);
            }
        }
        // Only async-signal-safe calls from here on: we are a forked child of a
        // multithreaded process.
        ::execl("/bin/sh", "sh", "-c", command.c_str(),
                static_cast<char *>(nullptr));
        ::_exit(127);
    }

    // Reap the intermediate child so it does not linger as a zombie. It exits
    // immediately, so this does not meaningfully block the event loop.
    int status = 0;
    while (::waitpid(intermediate, &status, 0) < 0 && errno == EINTR) {
    }
}

void SarvamDictation::showRecordingIndicator(const std::string &icon,
                                             const std::string &summary,
                                             const std::string &body) {
    if (config_.showOsd.value()) {
        // The desktop cannot change under a running fcitx5, so the answer is
        // resolved once, in a function-local static: lazy (no getenv at
        // startup), thread-safe without a lock of our own, and this runs on
        // every state change, so caching it is worth the one static.
        static const bool plasma = sarvam::isPlasmaDesktop(
            environmentValue("XDG_CURRENT_DESKTOP"),
            environmentValue("KDE_FULL_SESSION"));
        if (plasma) {
            // An OSD is a small centred indicator, not a paragraph: the body is
            // tip material and is never sent to Plasma.
            sarvam::showOsd(icon, summary);
            return;
        }
    }
    if (config_.showRecordingTips.value()) {
        showTipMessage(summary, body, kTipTimeout);
    }
}

void SarvamDictation::showTipMessage(const std::string &summary,
                                     const std::string &body,
                                     int32_t timeout) {
    if (!config_.showRecordingTips.value()) {
        return;
    }
    // The notifications module is an optional runtime dependency.
    if (auto *n = notifications()) {
        n->call<INotifications::showTip>(std::string(kTipId),
                                         std::string(kAppName),
                                         std::string(kAppIcon), summary, body,
                                         timeout);
    }
}

void SarvamDictation::closeTip() {
    if (!config_.showRecordingTips.value()) {
        return;
    }
    // An empty summary and body is how a tip is dismissed.
    if (auto *n = notifications()) {
        n->call<INotifications::showTip>(std::string(kTipId),
                                         std::string(kAppName),
                                         std::string(kAppIcon), std::string(),
                                         std::string(), 0);
    }
}

void SarvamDictation::showErrorNotification(const std::string &summary,
                                            const std::string &body) {
    // notify-send is resolved once, on the first error, through a function-local
    // static: that is lazy (no $PATH walk at startup), thread-safe without a
    // lock of our own, and needs no member to reset. $PATH cannot change under a
    // running fcitx5, so caching the answer for the process lifetime is right.
    if (config_.notifyOnError.value()) {
        static const std::string notifySend = sarvam::findInPath("notify-send");
        if (!notifySend.empty()) {
            const std::vector<std::string> argv =
                sarvam::buildNotifySendCommand(notifySend, summary, body,
                                               kAppName, kAppIcon,
                                               /*critical=*/true,
                                               kErrorNotificationTimeoutMs);
            if (!argv.empty() && sarvam::runDetached(argv)) {
                return;
            }
        }
    }

    // No desktop notification, either because the option is off or because
    // notify-send is missing or could not be run. Fall back to the fcitx5 tip
    // regardless of ShowRecordingTips: a failed dictation that says nothing is
    // indistinguishable from a hotkey that does not work, and the error is the
    // one event that must always reach the user.
    if (auto *n = notifications()) {
        n->call<INotifications::showTip>(
            std::string(kTipId), std::string(kAppName), std::string(kAppIcon),
            summary, body, kErrorNotificationTimeoutMs);
        return;
    }

    // Neither channel exists: no notify-send, no notifications module, or the
    // option switched off. Log at warning level so the failure is at least
    // diagnosable; the body is never logged, it can carry the daemon's error
    // text.
    FCITX_LOGC(::fcitx::sarvamdictation_log, Warn)
        << "no way to show a notification, dropping: " << summary;
}

void SarvamDictation::deliverToClipboard(const std::string &transcript) {
    // The raw transcript, without the appended trailing space: that space
    // exists to separate typed words, and on the clipboard it is only noise.
    if (sarvam::copyToClipboard(transcript)) {
        // A copy that worked is not an error, so it is announced through the
        // recording indicator -- the Plasma OSD when that is on, the tip when
        // that is on, nothing otherwise -- and never through
        // showErrorNotification().
        showRecordingIndicator(
            kAppIcon, _("Copied to clipboard"),
            _("Nothing was focused to type into, so the transcript is on your "
              "clipboard."));
        return;
    }

    // No clipboard tool could be run and nothing was typed either, so the text
    // really is lost: this has to reach the user as an error, with the reason.
    showErrorNotification(
        _("Could not copy the transcript"),
        _("Nothing was focused to type into and no clipboard tool (wl-copy, "
          "xclip, xsel or Klipper over D-Bus) could be run, so the transcript "
          "was lost."));
}

void SarvamDictation::commitTranscript(const std::string &transcript) {
    if (transcript.empty()) {
        // An empty transcript is legal in the protocol and commits nothing.
        return;
    }

    if (config_.preeditPartials.value()) {
        // With composition the destination is not a guess: the words were being
        // composed into the context that had focus when the recording started.
        // If that context is gone or unfocused -- the user switched windows, the
        // window closed, or the field never took the composition -- there is
        // nothing left to put the final into. mostRecentInputContext() is
        // deliberately not consulted here: after a focus change it is whatever
        // is focused now, which may be a different application, and typing a
        // transcript into the wrong window is exactly the failure to avoid. So
        // the final goes to the clipboard, or is reported as an error, but is
        // never typed into a guess.
        InputContext *composed = targetInputContext_.get();
        if (!composed || !composed->hasFocus()) {
            if (config_.clipboardFallback.value()) {
                deliverToClipboard(transcript);
                return;
            }
            showErrorNotification(
                _("Nothing to type into"),
                _("The window the transcript was being composed into is gone, "
                  "and copying to the clipboard is turned off, so the "
                  "transcript was lost."));
            return;
        }

        std::string text = transcript;
        if (config_.appendSpace.value() && !endsWithWhitespace(text)) {
            text.push_back(' ');
        }
        if (config_.clipboardAlways.value()) {
            // Copy before committing, for the same reason as the legacy path:
            // if the commit does something unexpected the copy already exists.
            deliverToClipboard(transcript);
        }
        composed->commitString(text);
        return;
    }

    InputContext *inputContext = targetInputContext_.get();
    if (!inputContext) {
        // The original client is gone. mostRecentInputContext() is still worth
        // a look, but it may just as well belong to another application, so it
        // is only typed into when it actually has focus.
        inputContext = instance_->mostRecentInputContext();
    }
    const bool canType = inputContext && inputContext->hasFocus();

    // No focused input context means the window went away or nothing accepts
    // input. Committing into a stale context would put the text into whatever
    // window happens to be most recent, possibly another application's, so the
    // clipboard is the only safe destination. fcitx5 offers no capability flag
    // that could tell us whether a focused client is really a text field, and
    // treating "no surrounding text" as "not a text field" would redirect
    // every terminal and most X11 clients here -- so the rule stays "no focus
    // means clipboard".
    if (!canType && config_.clipboardFallback.value()) {
        deliverToClipboard(transcript);
        return;
    }

    std::string text = transcript;
    if (config_.appendSpace.value() && !endsWithWhitespace(text)) {
        text.push_back(' ');
    }

    bool copyAttempted = false;
    if (config_.clipboardAlways.value()) {
        // Copy before committing: if the commit path does something
        // unexpected, the clipboard copy has already happened.
        deliverToClipboard(transcript);
        copyAttempted = true;
    }

    if (inputContext) {
        inputContext->commitString(text);
        return;
    }

    // There is no input context at all, so nothing was typed. When the copy has
    // not been made yet it is the last resort, and its failure is reported by
    // deliverToClipboard() as an error -- exactly one notification per failure,
    // because a transcript must never disappear without the user being told.
    if (!copyAttempted) {
        deliverToClipboard(transcript);
    }
}

class SarvamDictationFactory : public AddonFactory {
public:
    AddonInstance *create(AddonManager *manager) override {
        return new SarvamDictation(manager->instance());
    }
};

} // namespace fcitx

FCITX_ADDON_FACTORY(fcitx::SarvamDictationFactory);
