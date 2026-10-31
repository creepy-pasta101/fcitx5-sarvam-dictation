/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * Unit tests for the fcitx5-free Plasma OSD helper.
 *
 * No test framework on purpose: the project has no test dependency, and a plain
 * main() that prints one line per failure is enough here. Run it directly (it
 * exits non-zero when anything fails) or through CTest.
 *
 * The include paths are relative so the file compiles whether or not the build
 * puts src/ on the include path.
 *
 * Nothing in this file needs qdbus6, gdbus, busctl or a D-Bus session: the
 * "clients" the showOsd() tests run are shell scripts created in a temp
 * directory. Each showOsd() scenario runs in a forked child, because showOsd()
 * caches the resolved client in a process-lifetime static and a single process
 * cannot observe both "no client installed" and "client installed".
 */
#include "../../src/desktopnotify.h"
#include "../../src/plasmaosd.h"

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include <dirent.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

using fcitx::sarvam::buildOsdCommand;
using fcitx::sarvam::findInPath;
using fcitx::sarvam::isPlasmaDesktop;
using fcitx::sarvam::osdClientTools;
using fcitx::sarvam::showOsd;

namespace {

int failures = 0;

void fail(const std::string &name, const std::string &detail) {
    std::printf("FAIL %s :: %s\n", name.c_str(), detail.c_str());
    ++failures;
}

void check(const std::string &name, bool condition) {
    if (!condition) {
        fail(name, "condition was false");
    }
}

void checkEq(const std::string &name, const std::string &got,
             const std::string &want) {
    if (got != want) {
        fail(name, "got [" + got + "] want [" + want + "]");
    }
}

void checkEqInt(const std::string &name, long long got, long long want) {
    if (got != want) {
        fail(name, "got " + std::to_string(got) + " want " +
                       std::to_string(want));
    }
}

std::string describe(const std::vector<std::string> &items) {
    std::string out;
    for (std::size_t i = 0; i < items.size(); ++i) {
        if (i != 0) {
            out += "|";
        }
        out += items[i];
    }
    return out;
}

/// Compare a vector element by element, reporting the joined form so a
/// mismatch shows the whole argv in one line.
void checkVector(const std::string &name, const std::vector<std::string> &got,
                 const std::vector<std::string> &want) {
    if (got.size() != want.size()) {
        fail(name, "size " + std::to_string(got.size()) + " want " +
                       std::to_string(want.size()) + " :: got [" +
                       describe(got) + "]");
        return;
    }
    for (std::size_t i = 0; i < got.size(); ++i) {
        if (got[i] != want[i]) {
            fail(name, "element " + std::to_string(i) + " got [" + got[i] +
                           "] want [" + want[i] + "]");
            return;
        }
    }
}

// ---- temp directory helpers ----------------------------------------------

std::string makeTempDir() {
    const char *tmpdir = std::getenv("TMPDIR");
    const std::string base = (tmpdir != nullptr && tmpdir[0] != '\0')
                                 ? std::string(tmpdir)
                                 : std::string("/tmp");
    std::string pattern = base + "/sarvam-osd-XXXXXX";
    std::vector<char> buffer(pattern.begin(), pattern.end());
    buffer.push_back('\0');
    if (::mkdtemp(buffer.data()) == nullptr) {
        return std::string();
    }
    return std::string(buffer.data());
}

bool makeDir(const std::string &path) {
    return ::mkdir(path.c_str(), 0755) == 0;
}

bool writeFile(const std::string &path, const std::string &content,
               mode_t mode) {
    std::FILE *file = std::fopen(path.c_str(), "wb");
    if (file == nullptr) {
        return false;
    }
    const std::size_t written =
        content.empty()
            ? static_cast<std::size_t>(0)
            : std::fwrite(content.data(), 1, content.size(), file);
    const bool ok = (written == content.size());
    if (std::fclose(file) != 0) {
        return false;
    }
    if (::chmod(path.c_str(), mode) != 0) {
        return false;
    }
    return ok;
}

/// Remove `path` and everything below it, so the test leaves no temp files.
void removeTree(const std::string &path) {
    DIR *dir = ::opendir(path.c_str());
    if (dir != nullptr) {
        while (struct dirent *entry = ::readdir(dir)) {
            const std::string name(entry->d_name);
            if (name == "." || name == "..") {
                continue;
            }
            removeTree(path + "/" + name);
        }
        ::closedir(dir);
        ::rmdir(path.c_str());
        return;
    }
    ::unlink(path.c_str());
}

/// Read a text file as one entry per line, without the trailing newline.
std::vector<std::string> readLines(const std::string &path) {
    std::vector<std::string> lines;
    std::FILE *file = std::fopen(path.c_str(), "rb");
    if (file == nullptr) {
        return lines;
    }
    std::string current;
    int character = 0;
    while ((character = std::fgetc(file)) != EOF) {
        if (character == '\n') {
            lines.push_back(current);
            current.clear();
        } else {
            current.push_back(static_cast<char>(character));
        }
    }
    if (!current.empty()) {
        lines.push_back(current);
    }
    std::fclose(file);
    return lines;
}

/// Wait up to `milliseconds` for `path` to appear. The client runs in a
/// detached grandchild, so the test has to give it a moment to write.
bool waitForFile(const std::string &path, long milliseconds) {
    const long stepMs = 10;
    for (long waited = 0; waited <= milliseconds; waited += stepMs) {
        struct stat info {};
        if (::stat(path.c_str(), &info) == 0 && S_ISREG(info.st_mode)) {
            return true;
        }
        struct timespec pause {};
        pause.tv_sec = 0;
        pause.tv_nsec = stepMs * 1000000L;
        ::nanosleep(&pause, nullptr);
    }
    return false;
}

/// Run `scenario` in a forked child so it starts with an uninitialised copy of
/// showOsd()'s cache. The child prints its own FAIL lines (stdout is
/// inherited) and exits 0 on success; the parent reports a non-zero exit as a
/// failure of its own.
void runInChild(const std::string &name, const std::string &tmp,
                bool (*scenario)(const std::string &)) {
    std::fflush(stdout);
    const pid_t pid = ::fork();
    if (pid < 0) {
        fail(name + " [child]", "fork failed");
        return;
    }
    if (pid == 0) {
        const bool ok = scenario(tmp);
        std::fflush(stdout);
        ::_exit(ok ? 0 : 1);
    }
    int status = 0;
    while (::waitpid(pid, &status, 0) < 0) {
        // A signal may interrupt the wait; retry. Anything else means we
        // cannot learn the child's verdict, which is a failure in itself.
        if (errno != EINTR) {
            fail(name + " [child]", "waitpid failed");
            return;
        }
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fail(name + " [child]", "child exited non-zero (scenario failed)");
    }
}

// ---- isPlasmaDesktop -----------------------------------------------------

void expectPlasma(const std::string &name, const std::string &xdg,
                  const std::string &kde, bool want) {
    const bool got = isPlasmaDesktop(xdg, kde);
    if (got != want) {
        fail("plasma/" + name,
             std::string("got ") + (got ? "true" : "false") + " want " +
                 (want ? "true" : "false"));
    }
}

void testIsPlasmaDesktop() {
    // Matches: "KDE" as a whole colon-separated word, in any case.
    expectPlasma("kde", "KDE", "", true);
    expectPlasma("kde-plasma", "KDE:plasma", "", true);
    expectPlasma("lowercase", "kde", "", true);
    expectPlasma("ubuntu-kde", "ubuntu:KDE", "", true);
    expectPlasma("mixed-case", "Ubuntu:Kde:plasma", "", true);
    expectPlasma("trailing-colon", "KDE:", "", true);
    expectPlasma("leading-colon", ":KDE", "", true);

    // The other signal alone is enough, including with no desktop at all.
    expectPlasma("full-session-empty-desktop", "", "1", true);
    expectPlasma("full-session-true", "", "true", true);
    expectPlasma("full-session-with-other-desktop", "GNOME", "1", true);

    // No match.
    expectPlasma("gnome", "GNOME", "", false);
    expectPlasma("xfce", "XFCE", "", false);
    expectPlasma("unity", "unity", "", false);
    expectPlasma("lxqt", "lxqt", "", false);
    expectPlasma("both-empty", "", "", false);
    expectPlasma("kde-inside-a-word", "xkdestuff", "", false);
    expectPlasma("kde-as-a-suffix", "XKDE", "", false);
    expectPlasma("kde-with-space", " KDE", "", false);
    expectPlasma("kde-with-space-after", "KDE ", "", false);
    expectPlasma("kde-suffixed-word", "KDEish", "", false);
    expectPlasma("kde-in-word-in-list", "GNOME:xkdestuff", "", false);
}

// ---- osdClientTools ------------------------------------------------------

void testClientTools() {
    const std::vector<std::string> want = {"qdbus6", "qdbus", "qdbus-qt5",
                                           "gdbus", "busctl"};
    checkVector("tools/order", osdClientTools(), want);
    checkEqInt("tools/count", static_cast<long long>(osdClientTools().size()),
               5);
    // A reference to a static: two calls must name the same object, and the
    // first entry must still be the Plasma 6 client.
    check("tools/stable-address", &osdClientTools() == &osdClientTools());
    check("tools/first-is-qdbus6",
          !osdClientTools().empty() && osdClientTools().front() == "qdbus6");
    check("tools/last-is-busctl",
          !osdClientTools().empty() && osdClientTools().back() == "busctl");
}

// ---- buildOsdCommand -----------------------------------------------------

void testCommandShapes() {
    const std::string icon = "audio-input-microphone";
    const std::string text = "Listening";

    // qdbus6 / qdbus / qdbus-qt5: method as one dotted argument.
    const std::vector<std::string> wantQt = {
        "", // replaced per tool below
        "org.kde.plasmashell",
        "/org/kde/osdService",
        "org.kde.osdService.showText",
        icon,
        text,
    };
    const char *qtTools[] = {"qdbus6", "qdbus", "qdbus-qt5"};
    for (const char *tool : qtTools) {
        std::vector<std::string> want = wantQt;
        want[0] = tool;
        checkVector(std::string("argv/") + tool,
                    buildOsdCommand(tool, icon, text), want);
    }

    // gdbus: --dest / --object-path / --method flags.
    const std::vector<std::string> wantGdbus = {
        "gdbus",
        "call",
        "--session",
        "--dest",
        "org.kde.plasmashell",
        "--object-path",
        "/org/kde/osdService",
        "--method",
        "org.kde.osdService.showText",
        icon,
        text,
    };
    checkVector("argv/gdbus", buildOsdCommand("gdbus", icon, text), wantGdbus);
    checkEqInt("argv/gdbus/count",
               static_cast<long long>(
                   buildOsdCommand("gdbus", icon, text).size()),
               11);

    // busctl: interface and method split, "ss" signature before the strings.
    const std::vector<std::string> wantBusctl = {
        "busctl",
        "--user",
        "call",
        "org.kde.plasmashell",
        "/org/kde/osdService",
        "org.kde.osdService",
        "showText",
        "ss",
        icon,
        text,
    };
    checkVector("argv/busctl", buildOsdCommand("busctl", icon, text),
                wantBusctl);
    checkEqInt("argv/busctl/count",
               static_cast<long long>(
                   buildOsdCommand("busctl", icon, text).size()),
               10);

    // An empty icon still occupies its argument slot.
    const std::vector<std::string> wantEmptyIcon = {
        "qdbus6",
        "org.kde.plasmashell",
        "/org/kde/osdService",
        "org.kde.osdService.showText",
        "",
        text,
    };
    const std::vector<std::string> emptyIcon =
        buildOsdCommand("qdbus6", "", text);
    checkVector("argv/empty-icon", emptyIcon, wantEmptyIcon);
    checkEqInt("argv/empty-icon/count",
               static_cast<long long>(emptyIcon.size()), 6);
    if (emptyIcon.size() == 6) {
        check("argv/empty-icon/slot-is-empty", emptyIcon[4].empty());
        checkEq("argv/empty-icon/text-after", emptyIcon[5], text);
    }

    // The empty icon slot survives in the other two shapes as well.
    const std::vector<std::string> gdbusEmpty =
        buildOsdCommand("gdbus", "", text);
    if (gdbusEmpty.size() == 11) {
        check("argv/gdbus/empty-icon/slot-is-empty", gdbusEmpty[9].empty());
        checkEq("argv/gdbus/empty-icon/text-after", gdbusEmpty[10], text);
    } else {
        fail("argv/gdbus/empty-icon", "unexpected argv size");
    }
    const std::vector<std::string> busctlEmpty =
        buildOsdCommand("busctl", "", text);
    if (busctlEmpty.size() == 10) {
        check("argv/busctl/empty-icon/slot-is-empty", busctlEmpty[8].empty());
        checkEq("argv/busctl/empty-icon/text-after", busctlEmpty[9], text);
    } else {
        fail("argv/busctl/empty-icon", "unexpected argv size");
    }

    // An empty text is a slot too, not a reason to drop the argument.
    checkEqInt("argv/empty-text/count",
               static_cast<long long>(
                   buildOsdCommand("qdbus6", icon, "").size()),
               6);

    // Unknown and empty tool names yield an empty vector.
    check("argv/unknown-tool/is-empty",
          buildOsdCommand("notify-send", icon, text).empty());
    check("argv/unknown-dbus-send-tool/is-empty",
          buildOsdCommand("dbus-send", icon, text).empty());
    check("argv/empty-tool/is-empty",
          buildOsdCommand("", icon, text).empty());
    // A near miss of a known name is unknown, not a prefix match.
    check("argv/near-miss-tool/is-empty",
          buildOsdCommand("qdbus6x", icon, text).empty());
    check("argv/upper-case-tool/is-empty",
          buildOsdCommand("QDBUS6", icon, text).empty());
}

// ---- showOsd -------------------------------------------------------------

/// No client installed at all: showOsd() reports false and does not run
/// anything. PATH points at an empty directory, so a client installed on the
/// machine running the tests cannot make this pass or fail by accident.
bool scenarioNoClient(const std::string &tmp) {
    const std::string dir = tmp + "/empty-bin";
    if (!makeDir(dir)) {
        fail("showosd/no-client/setup", "mkdir failed");
        return false;
    }
    if (::setenv("PATH", dir.c_str(), 1) != 0) {
        fail("showosd/no-client/setup", "setenv PATH failed");
        return false;
    }

    bool ok = true;
    check("showosd/no-client/findInPath-empty", findInPath("qdbus6").empty());
    if (showOsd("audio-input-microphone", "Listening")) {
        fail("showosd/no-client/returns-false",
             "showOsd returned true with no client on PATH");
        ok = false;
    }
    // A second call is answered from the same cached "nothing installed".
    if (showOsd("audio-input-microphone", "Transcribing")) {
        fail("showosd/no-client/returns-false-again",
             "showOsd returned true on the second call");
        ok = false;
    }
    return ok;
}

/// The fake client script. It records its argv, one entry per line, in the file
/// named by $SARVAM_OSD_TEST_RECORD, then exits non-zero on purpose: a client
/// that crashes after the OSD has appeared must not be reported as a failure.
const char kFakeClientScript[] =
    "#!/bin/sh\n"
    "{\n"
    "  printf '%s\\n' \"$0\"\n"
    "  for argument in \"$@\"; do printf '%s\\n' \"$argument\"; done\n"
    "} > \"$SARVAM_OSD_TEST_RECORD\"\n"
    "exit 1\n";

bool scenarioFakeClient(const std::string &tmp) {
    const std::string dir = tmp + "/fake-bin";
    const std::string script = dir + "/qdbus6";
    const std::string record = tmp + "/record.txt";
    if (!makeDir(dir)) {
        fail("showosd/fake/setup", "mkdir failed");
        return false;
    }
    if (!writeFile(script, kFakeClientScript, 0755)) {
        fail("showosd/fake/setup", "could not write the fake client");
        return false;
    }
    if (::setenv("PATH", dir.c_str(), 1) != 0) {
        fail("showosd/fake/setup", "setenv PATH failed");
        return false;
    }
    if (::setenv("SARVAM_OSD_TEST_RECORD", record.c_str(), 1) != 0) {
        fail("showosd/fake/setup", "setenv record path failed");
        return false;
    }

    bool ok = true;
    const std::string icon = "audio-input-microphone";
    const std::string text = "Listening";
    // The script exits 1, and showOsd must still answer true: we never wait for
    // the client and its exit status says nothing about the OSD.
    if (!showOsd(icon, text)) {
        fail("showosd/fake/returns-true",
             "showOsd returned false with a client on PATH");
        ok = false;
    }

    if (!waitForFile(record, 2000)) {
        fail("showosd/fake/recorded", "the fake client wrote no record");
        return false;
    }

    // argv[0] is the resolved absolute path, then the qdbus6 argv shape.
    const std::vector<std::string> want = {
        script,
        "org.kde.plasmashell",
        "/org/kde/osdService",
        "org.kde.osdService.showText",
        icon,
        text,
    };
    const std::vector<std::string> got = readLines(record);
    checkVector("showosd/fake/argv-reached-client", got, want);
    checkEqInt("showosd/fake/argc-reached-client",
               static_cast<long long>(got.size()), 6);
    if (got.size() == 6) {
        checkEq("showosd/fake/icon-slot", got[4], icon);
        checkEq("showosd/fake/text-slot", got[5], text);
    }
    return ok;
}

/// The client is resolved once: after the script is deleted the cached path is
/// still used, so showOsd() keeps answering true instead of re-scanning PATH.
bool scenarioCachedClient(const std::string &tmp) {
    const std::string dir = tmp + "/cache-bin";
    const std::string script = dir + "/qdbus6";
    const std::string record = tmp + "/cache-record.txt";
    if (!makeDir(dir)) {
        fail("showosd/cache/setup", "mkdir failed");
        return false;
    }
    if (!writeFile(script, kFakeClientScript, 0755)) {
        fail("showosd/cache/setup", "could not write the fake client");
        return false;
    }
    if (::setenv("PATH", dir.c_str(), 1) != 0) {
        fail("showosd/cache/setup", "setenv PATH failed");
        return false;
    }
    if (::setenv("SARVAM_OSD_TEST_RECORD", record.c_str(), 1) != 0) {
        fail("showosd/cache/setup", "setenv record path failed");
        return false;
    }

    bool ok = true;
    if (!showOsd("audio-input-microphone", "Listening")) {
        fail("showosd/cache/first-call",
             "showOsd returned false with a client on PATH");
        ok = false;
    }
    if (!waitForFile(record, 2000)) {
        fail("showosd/cache/first-call-recorded",
             "the fake client wrote no record");
        return false;
    }

    // Take the client away and point PATH somewhere empty: a cached resolution
    // still answers true, a fresh scan would answer false.
    if (::unlink(script.c_str()) != 0) {
        fail("showosd/cache/setup", "could not remove the fake client");
        return false;
    }
    if (::setenv("PATH", tmp.c_str(), 1) != 0) {
        fail("showosd/cache/setup", "setenv PATH failed");
        return false;
    }
    check("showosd/cache/no-longer-on-path", findInPath("qdbus6").empty());
    if (!showOsd("audio-input-microphone", "Transcribing")) {
        fail("showosd/cache/second-call",
             "showOsd re-resolved the client instead of using the cache");
        ok = false;
    }
    return ok;
}

void testShowOsd(const std::string &tmp) {
    runInChild("showosd/no-client", tmp, scenarioNoClient);
    runInChild("showosd/fake", tmp, scenarioFakeClient);
    runInChild("showosd/cache", tmp, scenarioCachedClient);
}

} // namespace

int main() {
    testIsPlasmaDesktop();
    testClientTools();
    testCommandShapes();

    const std::string tmp = makeTempDir();
    if (tmp.empty()) {
        fail("setup", "mkdtemp failed, the showOsd tests cannot run");
    } else {
        testShowOsd(tmp);
        removeTree(tmp);
        struct stat info {};
        check("setup/temp-dir-removed", ::stat(tmp.c_str(), &info) != 0);
    }

    if (failures != 0) {
        std::printf("%d failure(s)\n", failures);
        return 1;
    }
    std::printf("all plasmaosd checks passed\n");
    return 0;
}
