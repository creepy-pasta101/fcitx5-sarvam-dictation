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
    void reportUnreachable();
    void spawnDaemon();

    void showTipMessage(const std::string &summary, const std::string &body,
                        int32_t timeout);
    void closeTip();
    void commitTranscript(const std::string &transcript);

    Instance *instance_;
    SarvamDictationConfig config_;

    // Handlers unregister themselves when the vector is destroyed.
    std::vector<std::unique_ptr<fcitx::HandlerTableEntry<fcitx::EventHandler>>>
        eventHandlers_;

    std::unique_ptr<EventSourceIO> ioEvent_;
    std::unique_ptr<EventSourceTime> retryTimer_;

    int fd_ = -1;
    bool connecting_ = false;
    int retriesLeft_ = 0;
    bool spawnAttempted_ = false;
    // Cleared with the connection; keeps the connect-time reload to one per
    // connection instead of one per flushed command.
    bool reloadQueued_ = false;

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
