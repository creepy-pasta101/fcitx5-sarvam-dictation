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

#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

using fcitx::sarvam::buildNotifySendCommand;
using fcitx::sarvam::findInPath;
using fcitx::sarvam::runDetached;
using fcitx::sarvam::runDetachedWithInput;

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

// ---- runDetachedWithInput ------------------------------------------------

/// The fake clipboard tool. It records how many arguments it was given and
/// writes its standard input to a file, both through a rename, so the test sees
/// a file appear only once its content is complete.
const char kCaptureScript[] =
    "#!/bin/sh\n"
    "printf '%s\\n' \"$#\" > \"$SARVAM_CAPTURE_ARGC.part\" &&\n"
    "    mv \"$SARVAM_CAPTURE_ARGC.part\" \"$SARVAM_CAPTURE_ARGC\" || exit 1\n"
    "cat > \"$SARVAM_CAPTURE.part\" &&\n"
    "    mv \"$SARVAM_CAPTURE.part\" \"$SARVAM_CAPTURE\"\n";

/// A tool that drains its stdin and then lingers, to prove the caller does not
/// wait for the child.
const char kSlowCaptureScript[] = "#!/bin/sh\ncat > /dev/null\nsleep 2\n";

void sleepMs(long milliseconds) {
    struct timespec request;
    request.tv_sec = milliseconds / 1000;
    request.tv_nsec = (milliseconds % 1000) * 1000000L;
    while (::nanosleep(&request, &request) != 0 && errno == EINTR) {
    }
}

/// Poll for `path` to exist. The child is detached and runs asynchronously, so
/// the capture file cannot be assumed to be there when the call returns.
bool waitForFile(const std::string &path, long timeoutMs) {
    for (long waited = 0; waited <= timeoutMs; waited += 10) {
        if (::access(path.c_str(), F_OK) == 0) {
            return true;
        }
        sleepMs(10);
    }
    return false;
}

bool readWholeFile(const std::string &path, std::string &content) {
    content.clear();
    std::FILE *file = std::fopen(path.c_str(), "rb");
    if (file == nullptr) {
        return false;
    }
    char buffer[4096];
    std::size_t count = 0;
    while ((count = std::fread(buffer, 1, sizeof(buffer), file)) != 0) {
        content.append(buffer, count);
    }
    const bool ok = std::ferror(file) == 0;
    if (std::fclose(file) != 0) {
        return false;
    }
    return ok;
}

/// A form that survives being printed on a single line.
std::string printable(const std::string &value) {
    std::string out;
    for (const char character : value) {
        const unsigned char byte = static_cast<unsigned char>(character);
        if (character == '\n') {
            out += "\\n";
        } else if (byte < 0x20 || byte == 0x7f) {
            out += '.';
        } else {
            out += character;
        }
    }
    return out;
}

/// Run `argv` through runDetachedWithInput() with `input` and compare what the
/// child actually read with `input`, plus the number of arguments it was given.
bool expectCaptured(const std::string &name, const std::string &tmp,
                    const std::vector<std::string> &argv,
                    const std::string &input, long wantArgc) {
    const std::string capture = tmp + "/sarvam-capture.txt";
    const std::string argcFile = tmp + "/sarvam-capture-argc.txt";
    ::unlink(capture.c_str());
    ::unlink(argcFile.c_str());
    ::unlink((capture + ".part").c_str());
    ::unlink((argcFile + ".part").c_str());
    if (::setenv("SARVAM_CAPTURE", capture.c_str(), 1) != 0 ||
        ::setenv("SARVAM_CAPTURE_ARGC", argcFile.c_str(), 1) != 0) {
        fail(name + "/setenv", "could not set the capture variables");
        return false;
    }

    if (!runDetachedWithInput(argv, input)) {
        fail(name + "/return", "runDetachedWithInput returned false");
        return false;
    }
    if (!waitForFile(capture, 5000)) {
        fail(name + "/capture", "the fake tool never wrote its stdin");
        return false;
    }

    std::string got;
    if (!readWholeFile(capture, got)) {
        fail(name + "/read", "could not read the capture file");
        return false;
    }
    if (got != input) {
        fail(name + "/stdin", "got [" + printable(got) + "] want [" +
                                  printable(input) + "]");
        return false;
    }

    std::string argc;
    if (!readWholeFile(argcFile, argc)) {
        fail(name + "/argc-read", "could not read the argument count file");
        return false;
    }
    if (argc != std::to_string(wantArgc) + "\n") {
        fail(name + "/argc", "the tool was given [" + printable(argc) +
                                 "] arguments, want [" +
                                 std::to_string(wantArgc) + "]");
        return false;
    }
    return true;
}

void testRunDetachedWithInput(const std::string &tmp) {
    // Nothing to run: false without forking, exactly as in runDetached().
    check("input/empty-argv",
          !runDetachedWithInput(std::vector<std::string>(), "text"));
    check("input/empty-executable",
          !runDetachedWithInput(std::vector<std::string>{""}, "text"));

    const std::string script = tmp + "/sarvam-capture-tool";
    check("input/setup-script", writeFile(script, kCaptureScript, 0755));

    // A transcript as it really looks: a newline and UTF-8 (Devanagari) in it,
    // so the pipe is proved to carry bytes rather than a C string.
    const std::string text =
        "namaste\n"
        "\xe0\xa4\xa8\xe0\xa4\xae\xe0\xa4\xb8\xe0\xa5\x8d\xe0\xa4\xa4"
        "\xe0\xa5\x87\n";

    check("input/non-empty",
          expectCaptured("input/non-empty", tmp, {script}, text, 0));

    // An empty text still closes the pipe, so the tool sees an empty document
    // and exits instead of waiting for input that will never come.
    check("input/empty-text",
          expectCaptured("input/empty-text", tmp, {script}, "", 0));

    // Arguments are passed through untouched, and the text still arrives on
    // stdin: this is the shape xclip and xsel use.
    check("input/with-arguments",
          expectCaptured("input/with-arguments", tmp,
                         {script, "-selection", "clipboard"}, text, 2));

    // A text far larger than one write is likely to carry, to exercise the
    // partial-write loop.
    std::string large;
    for (int line = 0; line < 200; ++line) {
        large += "line ";
        large += std::to_string(line);
        large += " of the transcript\n";
    }
    check("input/large-text",
          expectCaptured("input/large-text", tmp, {script}, large, 0));

    // The intermediate child must have been reaped, so this process has no
    // children left to collect.
    int status = 0;
    check("input/no-zombie", ::waitpid(-1, &status, WNOHANG) < 0);

    // Never blocks: the tool drains stdin and then sleeps, but the caller must
    // not wait for it.
    const std::string slow = tmp + "/sarvam-slow-capture";
    check("input/setup-slow", writeFile(slow, kSlowCaptureScript, 0755));
    const auto started = std::chrono::steady_clock::now();
    const bool ok = runDetachedWithInput({slow}, text);
    const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                             std::chrono::steady_clock::now() - started)
                             .count();
    check("input/slow-tool", ok);
    if (elapsed >= 1000) {
        fail("input/non-blocking",
             "runDetachedWithInput took " + std::to_string(elapsed) + " ms");
    }
}

// ---- cleanup -------------------------------------------------------------

void cleanupTempDir(const std::string &tmp) {
    const char *names[] = {"sarvam-fake-notify-send", "sarvam-plain-file",
                           "sarvam-slow", "sarvam-notify-dir",
                           "sarvam-capture-tool", "sarvam-slow-capture",
                           "sarvam-capture.txt", "sarvam-capture.txt.part",
                           "sarvam-capture-argc.txt",
                           "sarvam-capture-argc.txt.part"};
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
        testRunDetachedWithInput(tmp);
        cleanupTempDir(tmp);
    }

    if (failures != 0) {
        std::printf("%d failure(s)\n", failures);
        return 1;
    }
    std::printf("all desktopnotify checks passed\n");
    return 0;
}
