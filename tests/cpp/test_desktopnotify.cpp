/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * Unit tests for the fcitx5-free desktop notification helper.
 *
 * No test framework on purpose: the project has no test dependency, and a plain
 * main() that prints one line per failure is enough here. Run it directly (it
 * exits non-zero when anything fails) or through CTest.
 *
 * The include path is relative so the file compiles whether or not the build
 * puts src/ on the include path.
 *
 * Nothing in this file needs notify-send to be installed: the "executables" the
 * tests run are shell scripts created in a temp directory.
 */
#include "../../src/desktopnotify.h"

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

using fcitx::sarvam::buildNotifySendCommand;
using fcitx::sarvam::findInPath;
using fcitx::sarvam::runDetached;

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
    const std::string base =
        (tmpdir != nullptr && tmpdir[0] != '\0') ? std::string(tmpdir)
                                                 : std::string("/tmp");
    std::string pattern = base + "/sarvam-notify-XXXXXX";
    std::vector<char> buffer(pattern.begin(), pattern.end());
    buffer.push_back('\0');
    if (::mkdtemp(buffer.data()) == nullptr) {
        return std::string();
    }
    return std::string(buffer.data());
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

/// The scripts the tests execute. Plain POSIX sh, no notify-send involved.
const char kQuickScript[] = "#!/bin/sh\nexit 0\n";
const char kSlowScript[] = "#!/bin/sh\nsleep 2\n";

// ---- argv shape ----------------------------------------------------------

void testCommandShapeCritical() {
    const std::vector<std::string> argv = buildNotifySendCommand(
        "/usr/bin/notify-send", "Dictation failed", "daemon unreachable",
        "fcitx5-sarvam-dictation", "audio-input-microphone", true, 10000);

    const std::vector<std::string> want = {
        "/usr/bin/notify-send",
        "--app-name=fcitx5-sarvam-dictation",
        "--icon=audio-input-microphone",
        "--urgency=critical",
        "--expire-time=10000",
        "--",
        "Dictation failed",
        "daemon unreachable",
    };
    checkVector("argv/critical", argv, want);
    checkEqInt("argv/critical/count", static_cast<long long>(argv.size()), 8);
    // argv[0] is the resolved executable, not a literal "notify-send".
    checkEq("argv/critical/argv0", argv.empty() ? std::string() : argv[0],
            "/usr/bin/notify-send");
}

void testCommandShapeNormal() {
    const std::vector<std::string> argv =
        buildNotifySendCommand("/usr/local/bin/notify-send", "summary", "body",
                               "app", "icon", false, 2500);
    const std::vector<std::string> want = {
        "/usr/local/bin/notify-send",
        "--app-name=app",
        "--icon=icon",
        "--urgency=normal",
        "--expire-time=2500",
        "--",
        "summary",
        "body",
    };
    checkVector("argv/normal", argv, want);
}

void testEmptyExecutable() {
    const std::vector<std::string> argv = buildNotifySendCommand(
        "", "summary", "body", "app", "icon", true, 1000);
    check("argv/empty-executable/is-empty", argv.empty());
    checkEqInt("argv/empty-executable/count",
               static_cast<long long>(argv.size()), 0);

    // Empty text, empty metadata and a zero expire time do not empty the
    // vector: the executable is the only input that does.
    check("argv/empty-executable/still-empty-normal",
          buildNotifySendCommand("", "", "", "", "", false, 0).empty());
}

void testDoubleDashBeforeSummary() {
    // A summary that looks like an option must land after "--", or notify-send
    // would treat it as one of its own options.
    const std::vector<std::string> argv = buildNotifySendCommand(
        "/usr/bin/notify-send", "--help", "-x", "app", "icon", true, 1000);

    checkEqInt("argv/dash/count", static_cast<long long>(argv.size()), 8);
    int dashDashIndex = -1;
    int dashDashCount = 0;
    for (std::size_t i = 0; i < argv.size(); ++i) {
        if (argv[i] == "--") {
            dashDashIndex = static_cast<int>(i);
            ++dashDashCount;
        }
    }
    checkEqInt("argv/dash/single", dashDashCount, 1);
    checkEqInt("argv/dash/index", dashDashIndex, 5);
    check("argv/dash/before-summary", dashDashIndex >= 0 && dashDashIndex < 6);
    if (argv.size() == 8) {
        checkEq("argv/dash/summary-after", argv[6], "--help");
        checkEq("argv/dash/body-after", argv[7], "-x");
    } else {
        fail("argv/dash/summary-after", "unexpected argv size");
    }

    // A summary beginning with a single hyphen is the same hazard.
    const std::vector<std::string> single =
        buildNotifySendCommand("/usr/bin/notify-send", "-u", "-c", "app", "i",
                               false, 10);
    if (single.size() == 8) {
        checkEq("argv/dash/single-hyphen-summary", single[6], "-u");
        checkEq("argv/dash/single-hyphen-body", single[7], "-c");
    } else {
        fail("argv/dash/single-hyphen", "unexpected argv size");
    }
}

void testEmptyTextAndExpire() {
    const std::vector<std::string> argv =
        buildNotifySendCommand("/n/s", "", "", "app", "icon", true, 0);
    checkEqInt("argv/empty-text/count", static_cast<long long>(argv.size()), 8);
    if (argv.size() == 8) {
        checkEq("argv/empty-text/summary", argv[6], "");
        checkEq("argv/empty-text/body", argv[7], "");
        checkEq("argv/empty-text/expire-zero", argv[4], "--expire-time=0");
        checkEq("argv/empty-text/dash", argv[5], "--");
    }
    // Large and negative expire values are formatted, not rejected.
    const std::vector<std::string> negative =
        buildNotifySendCommand("/n/s", "s", "b", "a", "i", false, -1);
    if (negative.size() == 8) {
        checkEq("argv/expire-negative", negative[4], "--expire-time=-1");
    }
}

// ---- findInPath ----------------------------------------------------------

void testFindInPath(const std::string &tmp) {
    const std::string script = tmp + "/sarvam-fake-notify-send";
    check("find/setup-script", writeFile(script, kQuickScript, 0755));

    const std::string plain = tmp + "/sarvam-plain-file";
    check("find/setup-plain", writeFile(plain, "data\n", 0644));

    const char *oldPathEnv = std::getenv("PATH");
    const std::string oldPath = oldPathEnv != nullptr ? oldPathEnv : "";

    const std::string extended = tmp + ":" + oldPath;
    check("find/setenv", ::setenv("PATH", extended.c_str(), 1) == 0);

    checkEq("find/found", findInPath("sarvam-fake-notify-send"), script);
    checkEq("find/missing", findInPath("sarvam-no-such-binary-xyz"),
            std::string());
    checkEq("find/empty-name", findInPath(""), std::string());
    checkEq("find/not-executable", findInPath("sarvam-plain-file"),
            std::string());
    checkEq("find/directory-is-not-a-binary", findInPath("sarvam-notify-dir"),
            std::string());

    // A name containing a slash is used as given, never looked up in PATH.
    checkEq("find/slash-absolute", findInPath(script), script);
    checkEq("find/slash-not-executable", findInPath(plain), std::string());
    checkEq("find/slash-missing", findInPath(tmp + "/nope"), std::string());
    checkEq("find/slash-relative", findInPath("./sarvam-fake-notify-send"),
            std::string());

    // First match on PATH wins: the same name exists in a second directory, but
    // the directory listed first is the one that is returned.
    const std::string second = tmp + "/second";
    check("find/setup-second-dir", ::mkdir(second.c_str(), 0700) == 0);
    const std::string shadow = second + "/sarvam-fake-notify-send";
    check("find/setup-shadow", writeFile(shadow, kQuickScript, 0755));
    const std::string ordered = tmp + ":" + second + ":" + oldPath;
    check("find/setenv-ordered", ::setenv("PATH", ordered.c_str(), 1) == 0);
    checkEq("find/first-match-wins", findInPath("sarvam-fake-notify-send"),
            script);
    const std::string reordered = second + ":" + tmp + ":" + oldPath;
    check("find/setenv-reordered", ::setenv("PATH", reordered.c_str(), 1) == 0);
    checkEq("find/order-follows-path", findInPath("sarvam-fake-notify-send"),
            shadow);

    // An empty PATH element means the current directory.
    char cwdBuffer[4096];
    const bool haveCwd = ::getcwd(cwdBuffer, sizeof(cwdBuffer)) != nullptr;
    check("find/getcwd", haveCwd);
    if (haveCwd) {
        const std::string savedCwd(cwdBuffer);
        check("find/chdir", ::chdir(tmp.c_str()) == 0);
        const std::string cwdOnly = ":" + oldPath;
        check("find/setenv-cwd", ::setenv("PATH", cwdOnly.c_str(), 1) == 0);
        checkEq("find/empty-element-is-cwd",
                findInPath("sarvam-fake-notify-send"),
                "./sarvam-fake-notify-send");
        check("find/chdir-back", ::chdir(savedCwd.c_str()) == 0);
    }

    // Restore the environment for whatever runs after this test.
    if (oldPathEnv != nullptr) {
        ::setenv("PATH", oldPath.c_str(), 1);
    } else {
        ::unsetenv("PATH");
    }

    // Clean up the extra directory we made.
    ::unlink(shadow.c_str());
    ::rmdir(second.c_str());
}

// ---- runDetached ---------------------------------------------------------

void testRunDetached(const std::string &tmp) {
    // Nothing to run: a false return without forking.
    check("detach/empty-argv", !runDetached(std::vector<std::string>()));
    check("detach/empty-executable",
          !runDetached(std::vector<std::string>{""}));

    const std::string script = tmp + "/sarvam-fake-notify-send";
    const std::vector<std::string> argv = {script, "--app-name=test", "--",
                                           "summary", "body"};
    check("detach/script", runDetached(argv));

    // A missing binary is not an error: the worker exec fails, exits 127, and
    // the caller still gets true.
    check("detach/missing-binary",
          runDetached({tmp + "/definitely-not-here", "--", "s", "b"}));

    // The intermediate child must have been reaped, so this process has no
    // children left to collect.
    int status = 0;
    const pid_t leftover = ::waitpid(-1, &status, WNOHANG);
    check("detach/no-zombie", leftover < 0);

    // Never blocks: the worker sleeps, but the caller must not wait for it.
    const std::string slow = tmp + "/sarvam-slow";
    check("detach/setup-slow", writeFile(slow, kSlowScript, 0755));
    const auto started = std::chrono::steady_clock::now();
    const bool ok = runDetached({slow});
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                             std::chrono::steady_clock::now() - started)
                             .count();
    check("detach/slow-script", ok);
    if (elapsed >= 1000) {
        fail("detach/non-blocking",
             "runDetached took " + std::to_string(elapsed) + " ms");
    }
}

// ---- cleanup -------------------------------------------------------------

void cleanupTempDir(const std::string &tmp) {
    const char *names[] = {"sarvam-fake-notify-send", "sarvam-plain-file",
                           "sarvam-slow", "sarvam-notify-dir"};
    for (const char *name : names) {
        const std::string path = tmp + "/" + name;
        ::unlink(path.c_str());
        ::rmdir(path.c_str());
    }
    ::rmdir(tmp.c_str());
}

} // namespace

int main() {
    testCommandShapeCritical();
    testCommandShapeNormal();
    testEmptyExecutable();
    testDoubleDashBeforeSummary();
    testEmptyTextAndExpire();

    const std::string tmp = makeTempDir();
    if (tmp.empty()) {
        fail("setup/mkdtemp", "could not create a temp directory");
    } else {
        // A directory named like the script, to prove findInPath rejects it.
        ::mkdir((tmp + "/sarvam-notify-dir").c_str(), 0755);
        testFindInPath(tmp);
        testRunDetached(tmp);
        cleanupTempDir(tmp);
    }

    if (failures != 0) {
        std::printf("%d failure(s)\n", failures);
        return 1;
    }
    std::printf("all desktopnotify checks passed\n");
    return 0;
}
