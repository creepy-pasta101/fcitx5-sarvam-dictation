/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * Fcitx 5 addon that turns a global hotkey into voice dictation.
 *
 * This is a Module addon, not an input method: it has no candidate list and
 * never owns the key state of an input method. It watches key events at the
 * PreInputMethod phase so the hotkey works in every focused application, and
 * it talks to an out-of-process Python daemon over a Unix stream socket
 * (newline-delimited JSON, protocol version 1) which owns the microphone and
 * the speech-to-text call. Keeping the audio and HTTP work in another process
 * means a hung network call can never stall fcitx5's event loop.
 *
 * Two ways to dictate: the toggle key (press to start, press again to stop) and
 * the push-to-talk key (hold to record, release to transcribe). Push-to-talk
 * needs the key RELEASE, which is why the key watcher no longer returns early on
 * a release event, and it needs a guard against auto-repeat, because a held key
 * delivers one press per repeat interval.
 */
#ifndef _FCITX5_SARVAM_DICTATION_SARVAMDICTATION_H_
#define _FCITX5_SARVAM_DICTATION_SARVAMDICTATION_H_

#include <cstdint>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

#include "fcitx-config/configuration.h"
#include "fcitx-config/iniparser.h"
#include "fcitx-config/option.h"
#include "fcitx-config/rawconfig.h"
#include "fcitx-utils/eventloopinterface.h"
#include "fcitx-utils/i18n.h"
#include "fcitx-utils/key.h"
#include "fcitx-utils/log.h"
#include "fcitx/addoninstance.h"
// Needed by FCITX_ADDON_DEPENDENCY_LOADER below: the macro calls
// AddonManager::addon(), so the type must be complete here, not just declared.
#include "fcitx/addonmanager.h"
#include "fcitx/inputcontext.h"
#include "fcitx/instance.h"
#include "notifications_public.h"
#include "sarvamdictationconfig.h"

namespace fcitx {

// The configuration class lives in fcitx::sarvam with the rest of the settings
// code; the addon keeps using the bare name.
using sarvam::SarvamDictationConfig;

/*
 * The write-only API key field shows this sentinel instead of the key.
 *
 * fcitx5-configtool has no password widget: it renders an
 * Option<std::string> as a plain QLineEdit and understands only the
 * Enum, EnumI18n, IntMin, IntMax and ListConstrain hints -- there is no
 * EchoMode and no Password hint in it (verified in its source). The
 * addon side therefore cannot ask the toolkit to mask the input, and the
 * field is write-only rather than masked: the option carries this
 * sentinel, the real key stays in config.toml (mode 0600), and setConfig
 * maps the sentinel back to the stored key before saving, so it can
 * never reach the file.
 */
constexpr char kMaskedApiKey[] = "********";

/**
 * A dictation addon instance.
 *
 * Owns: the global hotkey handler, the daemon connection (non-blocking Unix
 * socket driven by fcitx5's event loop), and the notification tips. It never
 * blocks: every socket operation is non-blocking and every wait is expressed
 * as an event-loop callback.
 */
class SarvamDictation final : public AddonInstance {
public:
    explicit SarvamDictation(Instance *instance);
    ~SarvamDictation() override;

    Instance *instance() { return instance_; }
    const SarvamDictationConfig &config() const { return config_; }

    void reloadConfig() override;
    const Configuration *getConfig() const override { return &config_; }
    void setConfig(const RawConfig &config) override;

    /// Ask a running daemon to re-read config.toml (after the config page saved
    /// it, or when a connection is first established).
    void sendReload();

    /// Queue the one-shot reload that precedes the first command on a new
    /// connection, so a recording never starts on stale settings.
    void queueReloadOnConnect();

private:
    /// Result of a single non-blocking connect attempt.
    enum class ConnectResult {
        /// The socket is usable right now.
        Connected,
        /// The connect is in flight; completion arrives as an IO event.
        InProgress,
        /// The connect failed immediately; the errno is reported to the caller.
        Failed,
    };

    // Optional runtime dependency: the notifications module. Every use is
    // guarded with "if (auto *n = notifications())".
    FCITX_ADDON_DEPENDENCY_LOADER(notifications, instance_->addonManager());

    std::string socketPath() const;

    void onHotkey(InputContext *inputContext);
    /// Begin a push-to-talk recording: remember the client, stamp the press and
    /// send "start". Only called when no session is active.
    void startPushToTalk(InputContext *inputContext);
    /// End a push-to-talk recording: send "stop" when the daemon broadcast
    /// "recording", otherwise "cancel". Safe to call when no session is active.
    void endPushToTalk();
    void queueCommand(const std::string &line);

    void startConnectAttempt(bool firstAttempt);
    ConnectResult connectOnce(int *errorOut);
    void setupIOEvent(IOEventFlags flags);
    bool handleIO(EventSourceIO *source, int fd, IOEventFlags flags);
    bool readAvailable();
    void processReadBuffer();
    void handleLine(std::string_view line);
    bool flushPending();
    bool writeOut();
    void closeSocket();

    void scheduleRetry();
    bool onRetryTimer();
    /// Arm the trailing-edge partial timer for kPartialIntervalUsec, unless it
    /// is already armed. Does nothing when the event loop refused the timer.
    void armPartialTimer();
    /// Trailing-edge handler for the partial transcript: show the pending text
    /// when it differs from what is already on the indicator, then disarm.
    /// Always returns false, because the source is one-shot.
    bool onPartialTimer();
    /// Drop the pending partial, disarm the timer and forget what was shown,
    /// so the next recording never starts from a stale tail.
    void clearPartialDisplay();
    void reportUnreachable();
    void spawnDaemon();

    /// Show the recording indicator: the Plasma OSD when this is Plasma and
    /// ShowOsd is on, otherwise the fcitx5 tip when ShowRecordingTips is on,
    /// otherwise nothing. The OSD gets the summary only; `body` is tip material.
    void showRecordingIndicator(const std::string &icon,
                                const std::string &summary,
                                const std::string &body);
    void showTipMessage(const std::string &summary, const std::string &body,
                        int32_t timeout);
    void closeTip();
    /// Deliver an error to the user. Prefers a desktop notification via
    /// notify-send so it is seen even when the fcitx5 notification daemon is
    /// not running, and falls back to an fcitx5 tip when notify-send is not
    /// installed.
    void showErrorNotification(const std::string &summary,
                               const std::string &body);
    void commitTranscript(const std::string &transcript);
    /// Put the raw transcript on the clipboard. Announces a successful copy
    /// through showRecordingIndicator() -- a copy is not an error -- and
    /// reports a failure through showErrorNotification(), because a failed copy
    /// after nothing could be typed means the text really is lost.
    void deliverToClipboard(const std::string &transcript);

    Instance *instance_;
    SarvamDictationConfig config_;

    // The real API key, held in memory only while the ApiKey option shows
    // kMaskedApiKey. Filled by reloadConfig(), consulted and refreshed by
    // setConfig(); it is never written to any file other than the
    // daemon's config.toml.
    std::string storedApiKey_;

    // Handlers unregister themselves when the vector is destroyed.
    std::vector<std::unique_ptr<fcitx::HandlerTableEntry<fcitx::EventHandler>>>
        eventHandlers_;

    std::unique_ptr<EventSourceIO> ioEvent_;
    std::unique_ptr<EventSourceTime> retryTimer_;
    // Trailing-edge timer for the partial transcript. The daemon emits about
    // ten partials a second and every indicator update forks a D-Bus client,
    // so the newest text waits here and is shown at most about three times a
    // second. Created exactly like retryTimer_, and null if the event loop
    // refused it.
    std::unique_ptr<EventSourceTime> partialTimer_;

    int fd_ = -1;
    bool connecting_ = false;
    int retriesLeft_ = 0;
    bool spawnAttempted_ = false;
    // Cleared with the connection; keeps the connect-time reload to one per
    // connection instead of one per flushed command.
    bool reloadQueued_ = false;

    // A push-to-talk session is open between the press that sends "start" and
    // the release (or the focus loss) that ends it. While it is true a second
    // press of the same key is auto-repeat and is swallowed.
    bool pushToTalkActive_ = false;
    // CLOCK_MONOTONIC microseconds of the press, so the hold can be measured
    // without being disturbed by wall-clock changes.
    uint64_t pushToTalkStartUsec_ = 0;
    // The state of the last "state" message the daemon broadcast. The release
    // sends "stop" only when this is "recording" and "cancel" otherwise; see
    // endPushToTalk().
    std::string lastState_;
    // The newest partial transcript, waiting for the trailing-edge timer,
    // and the text currently on the indicator. A partial that sanitises to
    // the same text as the latter is not shown again, so an unchanged
    // transcript costs no D-Bus fork at all. Both are cleared when a
    // "result", a "state" message or a cancel ends the recording they
    // described.
    std::string pendingPartial_;
    std::string lastShownPartial_;
    // True between arming partialTimer_ and it firing. Tracked here rather
    // than read back with EventSource::isEnabled(), because what a one-shot
    // source reports after it has fired depends on the event-loop backend
    // fcitx5 was built with.
    bool partialTimerArmed_ = false;

    std::string readBuffer_;
    std::string writeBuffer_;
    std::vector<std::string> pendingLines_;
    std::vector<std::string> pendingIds_;

    // The input context that had focus when the hotkey was pressed. The client
    // may disappear long before the transcript arrives, so it must be tracked
    // rather than held as a raw pointer.
    TrackableObjectReference<InputContext> targetInputContext_;
};

FCITX_DECLARE_LOG_CATEGORY(sarvamdictation_log);

#define FCITX_SARVAMDICTATION_DEBUG()                                         \
    FCITX_LOGC(::fcitx::sarvamdictation_log, Debug)

} // namespace fcitx

#endif // _FCITX5_SARVAM_DICTATION_SARVAMDICTATION_H_
