// SPDX-FileCopyrightText: 2026 Suhan G Paradkar
// SPDX-License-Identifier: MIT
//
// Clipboard delivery for fcitx5-sarvam-dictation, without fcitx5.
//
// We do not link Qt or libdbus: we exec a clipboard utility (wl-copy on
// Wayland, xclip or xsel on X11) and feed it the text on standard input, or we
// ask Klipper to take the text over D-Bus through a command-line D-Bus client.
// Both go through the detached-child helpers in desktopnotify.cpp, so this file
// is plain C++17 plus POSIX and can be unit tested without a desktop session.
//
// Nothing here logs or prints anything: the text being copied is the user's
// transcript and is never written anywhere except into the pipe or the argv we
// hand to the tool.

#include "clipboard.h"

#include "desktopnotify.h"

#include <string>
#include <vector>

namespace fcitx::sarvam {

namespace {

// The four D-Bus coordinates of the Klipper slot we call, in one place so the
// three argv shapes below cannot drift apart.
const char kKlipperService[] = "org.kde.klipper";
const char kKlipperPath[] = "/klipper";
const char kKlipperInterface[] = "org.kde.klipper.klipper";
const char kKlipperMethod[] = "setClipboardContents";
const char kKlipperDottedMethod[] = "org.kde.klipper.klipper.setClipboardContents";

/// The entry for `name`, or nullptr when the tool is not one we know.
const ClipboardTool *findTool(const std::string &name) {
    if (name.empty()) {
        return nullptr;
    }
    for (const ClipboardTool &tool : clipboardTools()) {
        if (tool.name == name) {
            return &tool;
        }
    }
    return nullptr;
}

/// A resolved tool: the name (which picks the argv shape) and the absolute path
/// findInPath() returned for it.
struct ResolvedTool {
    std::string name;
    std::string path;
};

/// Resolve the first installed tool once for the process lifetime.
///
/// A function-local static is initialised on the first call and keeps its value
/// until the process exits, which is what we want: the installed tools do not
/// change under a running fcitx5, and copyToClipboard() may run on every
/// dictation. The empty `path` records "no tool installed", which is cached
/// too, so a missing tool costs one PATH scan and not one per transcript.
const ResolvedTool &resolvedTool() {
    static const ResolvedTool resolved = [] {
        ResolvedTool found;
        for (const ClipboardTool &tool : clipboardTools()) {
            const std::string path = findInPath(tool.name);
            if (!path.empty()) {
                found.name = tool.name;
                found.path = path;
                return found;
            }
        }
        return found;
    }();
    return resolved;
}

} // namespace

const std::vector<ClipboardTool> &clipboardTools() {
    // wl-copy is the Wayland path and needs no arguments at all; xclip and xsel
    // are the X11 paths, and both default to the primary selection, so the
    // clipboard has to be named explicitly. Klipper comes last because it is the
    // fallback every Plasma installation has even when no clipboard utility is
    // installed. Among the D-Bus clients the preference is the same as in
    // plasmaosd.cpp: native to Plasma first, then glib, then systemd.
    static const std::vector<ClipboardTool> tools = {
        {"wl-copy", true},  {"xclip", true},   {"xsel", true},
        {"qdbus6", false},  {"qdbus", false},  {"qdbus-qt5", false},
        {"gdbus", false},   {"busctl", false},
    };
    return tools;
}

std::vector<std::string> buildClipboardCommand(const std::string &tool,
                                               const std::string &text) {
    std::vector<std::string> argv;
    const ClipboardTool *entry = findTool(tool);
    if (entry == nullptr) {
        return argv;
    }

    // The command-line tools read the text from standard input, so their argv
    // carries no text at all: it is written into the child's stdin instead.
    // Putting the text here as well would copy nothing or copy it twice.
    if (entry->viaStdin) {
        argv.push_back(tool);
        if (tool == "xclip") {
            argv.push_back("-selection");
            argv.push_back("clipboard");
        } else if (tool == "xsel") {
            argv.push_back("--clipboard");
            argv.push_back("--input");
        }
        // wl-copy takes no arguments.
        return argv;
    }

    // gdbus spells every D-Bus coordinate out as its own flag:
    //   gdbus call --session --dest D --object-path P --method I.M TEXT
    if (tool == "gdbus") {
        argv.reserve(10);
        argv.push_back(tool);
        argv.push_back("call");
        argv.push_back("--session");
        argv.push_back("--dest");
        argv.push_back(kKlipperService);
        argv.push_back("--object-path");
        argv.push_back(kKlipperPath);
        argv.push_back("--method");
        argv.push_back(kKlipperDottedMethod);
        argv.push_back(text);
        return argv;
    }

    // busctl splits interface and method and needs the argument signature, which
    // here is a single string: the "s" must precede the text.
    //   busctl --user call D P I METHOD s TEXT
    if (tool == "busctl") {
        argv.reserve(10);
        argv.push_back(tool);
        argv.push_back("--user");
        argv.push_back("call");
        argv.push_back(kKlipperService);
        argv.push_back(kKlipperPath);
        argv.push_back(kKlipperInterface);
        argv.push_back(kKlipperMethod);
        argv.push_back("s");
        argv.push_back(text);
        return argv;
    }

    // The three Qt clients take the method as a single dotted argument:
    //   qdbus6 SERVICE PATH INTERFACE.METHOD TEXT
    argv.reserve(5);
    argv.push_back(tool);
    argv.push_back(kKlipperService);
    argv.push_back(kKlipperPath);
    argv.push_back(kKlipperDottedMethod);
    argv.push_back(text);
    return argv;
}

bool copyToClipboard(const std::string &text) {
    const ResolvedTool &resolved = resolvedTool();
    if (resolved.path.empty()) {
        return false;
    }

    std::vector<std::string> argv = buildClipboardCommand(resolved.name, text);
    if (argv.empty()) {
        return false;
    }

    // The detached helpers execv() argv[0], so it has to be the absolute path we
    // resolved and not the bare tool name. The name stays in argv[0] only for
    // the pure buildClipboardCommand() result the tests compare.
    argv.front() = resolved.path;

    const ClipboardTool *entry = findTool(resolved.name);
    if (entry != nullptr && entry->viaStdin) {
        // The text goes down the pipe: this is the call that proves stdin works.
        return runDetachedWithInput(argv, text);
    }
    // Klipper got the text in argv, so a plain detached run with stdin on
    // /dev/null is right.
    return runDetached(argv);
}

} // namespace fcitx::sarvam
