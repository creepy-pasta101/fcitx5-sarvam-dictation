// SPDX-FileCopyrightText: 2026 Suhan G Paradkar
// SPDX-License-Identifier: MIT
#ifndef _FCITX5_SARVAM_DICTATION_CLIPBOARD_H_
#define _FCITX5_SARVAM_DICTATION_CLIPBOARD_H_

#include <string>
#include <vector>

namespace fcitx::sarvam {

/// A way to put text on the clipboard.
struct ClipboardTool {
    std::string name;   ///< the executable, or the D-Bus client for Klipper
    bool viaStdin;      ///< true when the text is fed on standard input
};

/// The tools to try, in preference order: wl-copy, xclip, xsel, then Klipper
/// over D-Bus through qdbus6, qdbus, qdbus-qt5, gdbus and busctl.
///
/// wl-copy is the Wayland path, xclip and xsel the X11 paths, and Klipper is the
/// fallback that every Plasma installation has even when no clipboard utility is
/// installed.
const std::vector<ClipboardTool> &clipboardTools();

/// Build the argv that puts `text` on the clipboard through `tool`.
///
/// The three command-line tools read the text from stdin and get a bare argv
/// (`{"wl-copy"}`, `{"xclip", "-selection", "clipboard"}`,
/// `{"xsel", "--clipboard", "--input"}`); the Klipper variants pass it as the
/// last argument, because Klipper takes it over D-Bus. An unknown or empty tool
/// name yields an empty vector.
std::vector<std::string> buildClipboardCommand(const std::string &tool,
                                               const std::string &text);

/// Put `text` on the clipboard. Resolves the first available tool once for the
/// process lifetime and runs it detached. Returns false when no tool is
/// installed or the command could not be started.
bool copyToClipboard(const std::string &text);

} // namespace fcitx::sarvam

#endif
