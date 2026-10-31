// SPDX-FileCopyrightText: 2026 Suhan G Paradkar
// SPDX-License-Identifier: MIT
#ifndef _FCITX5_SARVAM_DICTATION_PLASMAOSD_H_
#define _FCITX5_SARVAM_DICTATION_PLASMAOSD_H_

#include <string>
#include <vector>

namespace fcitx::sarvam {

/// True when the session is KDE Plasma.
///
/// Either signal is enough: XDG_CURRENT_DESKTOP contains "KDE" as one of its
/// colon-separated words, compared case-insensitively (so "KDE", "plasma" in
/// "KDE:plasma" and "kde" all match), or KDE_FULL_SESSION is non-empty.
bool isPlasmaDesktop(const std::string &xdgCurrentDesktop,
                     const std::string &kdeFullSession);

/// The command-line D-Bus clients to try, in preference order:
/// qdbus6, qdbus, qdbus-qt5, gdbus, busctl.
const std::vector<std::string> &osdClientTools();

/// Build the argv that shows `text` with `icon` on the Plasma OSD through
/// `tool`, using the shapes in the table above. An empty `tool`, or one that is
/// not in osdClientTools(), yields an empty vector.
std::vector<std::string> buildOsdCommand(const std::string &tool,
                                         const std::string &icon,
                                         const std::string &text);

/// Show a Plasma OSD message.
///
/// Resolves a client once for the process lifetime, builds the command and runs
/// it detached. Returns false when no client tool is installed. A client that
/// exits non-zero is not reported as a failure: qdbus6 is known to crash on exit
/// on some Qt versions *after* the OSD has appeared, and we never wait for it.
bool showOsd(const std::string &icon, const std::string &text);

} // namespace fcitx::sarvam

#endif
