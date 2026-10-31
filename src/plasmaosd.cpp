// SPDX-FileCopyrightText: 2026 Suhan G Paradkar
// SPDX-License-Identifier: MIT
//
// Plasma on-screen display for fcitx5-sarvam-dictation, without fcitx5.
//
// The OSD is not a notification: it is Plasma's own org.kde.osdService.showText
// D-Bus slot, the small centred indicator Plasma uses for volume and
// brightness. We do not link Qt or libdbus; we exec a command-line D-Bus client
// (qdbus6, qdbus, qdbus-qt5, gdbus or busctl) through the detached-child helper
// in desktopnotify.cpp, so this file is plain C++17 plus POSIX and can be unit
// tested without a running Plasma, fcitx5 or D-Bus session.
//
// Nothing here prints or logs anything: the OSD text is user-visible state
// ("Listening"), never an API error.

#include "plasmaosd.h"

#include "desktopnotify.h"

#include <cstddef>
#include <string>
#include <vector>

namespace fcitx::sarvam {

namespace {

// The four D-Bus coordinates of the slot we call, in one place so the three
// argv shapes below cannot drift apart.
const char kService[] = "org.kde.plasmashell";
const char kObjectPath[] = "/org/kde/osdService";
const char kInterface[] = "org.kde.osdService";
const char kMethodName[] = "showText";
const char kDottedMethod[] = "org.kde.osdService.showText";

/// ASCII-only case folding, so the answer does not depend on the locale.
char foldAscii(char c) {
    if (c >= 'A' && c <= 'Z') {
        return static_cast<char>(c - 'A' + 'a');
    }
    return c;
}

/// Whole-word, case-insensitive comparison against a lower-case literal.
bool equalsIgnoreCase(const std::string &value, const char *literal) {
    std::size_t index = 0;
    for (; index < value.size(); ++index) {
        if (literal[index] == '\0') {
            return false;
        }
        if (foldAscii(value[index]) != literal[index]) {
            return false;
        }
    }
    return literal[index] == '\0';
}

/// True when `list` contains `value` exactly.
bool contains(const std::vector<std::string> &list, const std::string &value) {
    for (const std::string &item : list) {
        if (item == value) {
            return true;
        }
    }
    return false;
}

/// A resolved client: the tool name (which picks the argv shape) and the
/// absolute path findInPath() returned for it.
struct OsdClient {
    std::string tool;
    std::string path;
};

/// Resolve the client once for the process lifetime.
///
/// A function-local static is initialised on the first call and keeps its value
/// until the process exits, which is what we want: the installed tools do not
/// change under a running fcitx5, and showOsd() runs on every state change. The
/// empty `path` records "no client installed", which is cached too, so a missing
/// client costs one PATH scan and not one per dictation.
const OsdClient &resolvedClient() {
    static const OsdClient client = [] {
        OsdClient found;
        for (const std::string &tool : osdClientTools()) {
            const std::string path = findInPath(tool);
            if (!path.empty()) {
                found.tool = tool;
                found.path = path;
                return found;
            }
        }
        return found;
    }();
    return client;
}

} // namespace

bool isPlasmaDesktop(const std::string &xdgCurrentDesktop,
                     const std::string &kdeFullSession) {
    // KDE_FULL_SESSION is set by Plasma itself for the whole session, so a
    // non-empty value is enough on its own; it is also what saves us when
    // XDG_CURRENT_DESKTOP is empty or was never exported.
    if (!kdeFullSession.empty()) {
        return true;
    }

    // XDG_CURRENT_DESKTOP is a colon-separated list ("KDE:plasma"), so compare
    // whole words: a plain substring search would match "xkdestuff".
    std::size_t start = 0;
    while (true) {
        const std::size_t end = xdgCurrentDesktop.find(':', start);
        const std::string word =
            (end == std::string::npos)
                ? xdgCurrentDesktop.substr(start)
                : xdgCurrentDesktop.substr(start, end - start);
        if (equalsIgnoreCase(word, "kde")) {
            return true;
        }
        if (end == std::string::npos) {
            break;
        }
        start = end + 1;
    }
    return false;
}

const std::vector<std::string> &osdClientTools() {
    // qdbus6 is the KDE Frameworks 6 client and the first choice on Plasma 6;
    // qdbus and qdbus-qt5 cover Plasma 5 and older distributions, gdbus comes
    // with glib and is present almost everywhere, and busctl ships with
    // systemd. Preference is "native to Plasma first".
    static const std::vector<std::string> tools = {"qdbus6", "qdbus",
                                                   "qdbus-qt5", "gdbus",
                                                   "busctl"};
    return tools;
}

std::vector<std::string> buildOsdCommand(const std::string &tool,
                                         const std::string &icon,
                                         const std::string &text) {
    std::vector<std::string> argv;
    if (tool.empty() || !contains(osdClientTools(), tool)) {
        return argv;
    }

    // The three Qt clients take the method as a single dotted argument:
    //   qdbus6 SERVICE PATH INTERFACE.METHOD ICON TEXT
    if (tool == "qdbus6" || tool == "qdbus" || tool == "qdbus-qt5") {
        argv.reserve(6);
        argv.push_back(tool);
        argv.push_back(kService);
        argv.push_back(kObjectPath);
        argv.push_back(kDottedMethod);
        argv.push_back(icon);
        argv.push_back(text);
        return argv;
    }

    // gdbus spells every D-Bus coordinate out as its own flag:
    //   gdbus call --session --dest D --object-path P --method I.M A B
    if (tool == "gdbus") {
        argv.reserve(11);
        argv.push_back(tool);
        argv.push_back("call");
        argv.push_back("--session");
        argv.push_back("--dest");
        argv.push_back(kService);
        argv.push_back("--object-path");
        argv.push_back(kObjectPath);
        argv.push_back("--method");
        argv.push_back(kDottedMethod);
        argv.push_back(icon);
        argv.push_back(text);
        return argv;
    }

    // busctl splits interface and method and needs the argument signature:
    //   busctl --user call D P I METHOD ss A B
    if (tool == "busctl") {
        argv.reserve(10);
        argv.push_back(tool);
        argv.push_back("--user");
        argv.push_back("call");
        argv.push_back(kService);
        argv.push_back(kObjectPath);
        argv.push_back(kInterface);
        argv.push_back(kMethodName);
        argv.push_back("ss");
        argv.push_back(icon);
        argv.push_back(text);
        return argv;
    }

    // Unreachable while osdClientTools() and this function agree; returning an
    // empty argv is the documented answer for a tool we do not know.
    return argv;
}

bool showOsd(const std::string &icon, const std::string &text) {
    const OsdClient &client = resolvedClient();
    if (client.path.empty()) {
        return false;
    }

    std::vector<std::string> argv = buildOsdCommand(client.tool, icon, text);
    if (argv.empty()) {
        return false;
    }

    // runDetached() execv()s argv[0], so it has to be the absolute path we
    // resolved and not the bare tool name. The tool name stays in argv[0] only
    // for the pure buildOsdCommand() result the tests compare.
    argv.front() = client.path;

    // Deliberately no wait and no status check: qdbus6 is known to crash on
    // exit on some Qt versions *after* the OSD has appeared, so a non-zero exit
    // says nothing about whether Plasma showed anything. runDetached() returns
    // false only when the fork itself failed.
    return runDetached(argv);
}

} // namespace fcitx::sarvam
