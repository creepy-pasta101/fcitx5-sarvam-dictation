// SPDX-FileCopyrightText: 2026 Suhan G Paradkar
// SPDX-License-Identifier: MIT
#ifndef _FCITX5_SARVAM_DICTATION_DESKTOPNOTIFY_H_
#define _FCITX5_SARVAM_DICTATION_DESKTOPNOTIFY_H_

#include <string>
#include <vector>

namespace fcitx::sarvam {

/// Resolve an executable name against $PATH, the way execvp would.
/// Returns the absolute path, or an empty string when it is not on the path.
std::string findInPath(const std::string &name);

/// Build the argv for a `notify-send` call. Pure: no I/O, no fork.
///
/// The result is always
///   {"notify-send", "--app-name=<appName>", "--icon=<icon>",
///    "--urgency=<critical|normal>", "--expire-time=<ms>", "--", summary, body}
/// and is empty when `executable` is empty. `executable` is the resolved path
/// from findInPath(), so the caller decides whether the binary exists.
std::vector<std::string> buildNotifySendCommand(const std::string &executable,
                                                const std::string &summary,
                                                const std::string &body,
                                                const std::string &appName,
                                                const std::string &icon,
                                                bool critical,
                                                int expireMs);

/// Run argv in a detached child (double fork + setsid), with stdin, stdout and
/// stderr on /dev/null. Never blocks the caller and never throws. Returns false
/// only when the fork itself failed; a missing binary or a failing notify-send
/// is not an error here, the notification is simply lost.
bool runDetached(const std::vector<std::string> &argv);

} // namespace fcitx::sarvam

#endif
