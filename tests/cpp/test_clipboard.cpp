/*
 * SPDX-FileCopyrightText: 2026 Suhan G Paradkar
 * SPDX-License-Identifier: MIT
 *
 * Unit tests for the fcitx5-free clipboard helper.
 *
 * No test framework on purpose: the project has no test dependency, and a plain
 * main() that prints one line per failure is enough here. Run it directly (it
 * exits non-zero when anything fails) or through CTest.
 *
 * The include paths are relative so the file compiles whether or not the build
 * puts src/ on the include path.
 *
 * Nothing in this file needs wl-copy, xclip, xsel, Klipper or a D-Bus session:
 * the "clipboard tool" the copyToClipboard() success path runs is a shell script
 * created in a temp directory, which writes its standard input to a file. That
 * is the assertion that proves the pipe works. Each copyToClipboard() scenario
 * runs in a forked child, because copyToClipboard() caches the resolved tool in
 * a process-lifetime static and a single process cannot observe both "no tool
 * installed" and "tool installed".
 */
#include "../../src/clipboard.h"
#include "../../src/desktopnotify.h"

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

using fcitx::sarvam::buildClipboardCommand;
using fcitx::sarvam::clipboardTools;
using fcitx::sarvam::ClipboardTool;
using fcitx::sarvam::copyToClipboard;

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
            fail(name, "element " + std::to_string(i) + " got [" +
                           printable(got[i]) + "] want [" +
                           printable(want[i]) + "]");
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
    std::string pattern = base + "/sarvam-clip-XXXXXX";
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

void sleepMs(long milliseconds) {
    struct timespec request;
    request.tv_sec = milliseconds / 1000;
    request.tv_nsec = (milliseconds % 1000) * 1000000L;
    while (::nanosleep(&request, &request) != 0 && errno == EINTR) {
    }
}

/// Poll for `path` to exist. The tool is detached and runs asynchronously, so
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

/// The fake clipboard tool: it records how many arguments it was given and
/// writes its standard input to a file, both through a rename, so the test sees
/// a file appear only once its content is complete.
const char kFakeWlCopy[] =
    "#!/bin/sh\n"
    "printf '%s\\n' \"$#\" > \"$SARVAM_CLIP_ARGC.part\" &&\n"
    "    mv \"$SARVAM_CLIP_ARGC.part\" \"$SARVAM_CLIP_ARGC\" || exit 1\n"
    "cat > \"$SARVAM_CLIP_CAPTURE.part\" &&\n"
    "    mv \"$SARVAM_CLIP_CAPTURE.part\" \"$SARVAM_CLIP_CAPTURE\"\n";

/// The text copied in the success path: a newline and UTF-8 (Devanagari) in it,
/// so the pipe is proved to carry bytes rather than a C string.
const std::string kClipText =
    "namaste\n"
    "\xe0\xa4\xa8\xe0\xa4\xae\xe0\xa4\xb8\xe0\xa5\x8d\xe0\xa4\xa4"
    "\xe0\xa5\x87\n";

// ---- the tool list -------------------------------------------------------

void testToolList() {
    const std::vector<ClipboardTool> &tools = clipboardTools();
    const std::vector<std::string> wantNames = {
        "wl-copy", "xclip",   "xsel", "qdbus6",
        "qdbus",   "qdbus-qt5", "gdbus", "busctl"};
    const std::vector<bool> wantStdin = {true,  true,  true,  false,
                                         false, false, false, false};

    checkEqInt("tools/count", static_cast<long long>(tools.size()),
               static_cast<long long>(wantNames.size()));
    if (tools.size() != wantNames.size()) {
        return;
    }
    for (std::size_t i = 0; i < tools.size(); ++i) {
        checkEq("tools/name/" + std::to_string(i), tools[i].name, wantNames[i]);
        if (tools[i].viaStdin != wantStdin[i]) {
            fail("tools/viaStdin/" + std::to_string(i),
                 "got " + std::string(tools[i].viaStdin ? "true" : "false") +
                     " want " +
                     std::string(wantStdin[i] ? "true" : "false"));
        }
    }

    // The same call twice returns the same object: it is a cached table, not a
    // fresh vector per call, so the address has to match.
    check("tools/stable", &clipboardTools() == &tools);

    // The first three read stdin and the last five are D-Bus clients, in that
    // order: a reordering would change which tool wins on a Plasma machine that
    // also has wl-copy installed.
    checkEq("tools/first", tools.empty() ? std::string() : tools.front().name,
            "wl-copy");
    checkEq("tools/last", tools.empty() ? std::string() : tools.back().name,
            "busctl");
}

// ---- argv shapes ---------------------------------------------------------

void testStdinToolShapes() {
    // None of these carries the text: it goes down the pipe.
    checkVector("argv/wl-copy", buildClipboardCommand("wl-copy", "clip text"),
                std::vector<std::string>{"wl-copy"});
    checkVector("argv/xclip", buildClipboardCommand("xclip", "clip text"),
                std::vector<std::string>{"xclip", "-selection", "clipboard"});
    checkVector("argv/xsel", buildClipboardCommand("xsel", "clip text"),
                std::vector<std::string>{"xsel", "--clipboard", "--input"});

    // Spelled out: the text must appear in no element of a stdin tool's argv,
    // because the tool would then either copy an empty string (xclip reads
    // stdin regardless) or be handed the text twice.
    const std::vector<std::string> stdinTools = {"wl-copy", "xclip", "xsel"};
    for (const std::string &tool : stdinTools) {
        const std::vector<std::string> argv =
            buildClipboardCommand(tool, "clip text");
        check("argv/" + tool + "/non-empty", !argv.empty());
        for (std::size_t i = 0; i < argv.size(); ++i) {
            check("argv/" + tool + "/no-text/" + std::to_string(i),
                  argv[i].find("clip text") == std::string::npos);
        }
        checkEq("argv/" + tool + "/argv0",
                argv.empty() ? std::string() : argv[0], tool);
    }

    // A text that looks like an option is never an option, because it is not in
    // the argv at all.
    checkVector("argv/xclip/dash-text",
                buildClipboardCommand("xclip", "--help"),
                std::vector<std::string>{"xclip", "-selection", "clipboard"});
}

void testKlipperShapes() {
    // The Qt clients take the method as one dotted argument and the text last.
    const std::vector<std::string> qdbusWant = {
        "qdbus6",
        "org.kde.klipper",
        "/klipper",
        "org.kde.klipper.klipper.setClipboardContents",
        "clip text",
    };
    // The loop variable is a const char* rather than a std::string reference:
    // binding a reference to each element of an initializer_list would construct
    // a temporary per iteration, which -Wrange-loop-construct flags.
    for (const char *toolName : {"qdbus6", "qdbus", "qdbus-qt5"}) {
        const std::string tool = toolName;
        std::vector<std::string> want = qdbusWant;
        want.front() = tool;
        checkVector("argv/" + tool, buildClipboardCommand(tool, "clip text"),
                    want);
    }

    // gdbus spells every D-Bus coordinate out as its own flag.
    const std::vector<std::string> gdbusWant = {
        "gdbus",
        "call",
        "--session",
        "--dest",
        "org.kde.klipper",
        "--object-path",
        "/klipper",
        "--method",
        "org.kde.klipper.klipper.setClipboardContents",
        "clip text",
    };
    checkVector("argv/gdbus", buildClipboardCommand("gdbus", "clip text"),
                gdbusWant);

    // busctl splits interface and method and needs the "s" signature before the
    // text: without it busctl rejects the call, and with it in the wrong place
    // it would send the signature as the string.
    const std::vector<std::string> busctlWant = {
        "busctl",
        "--user",
        "call",
        "org.kde.klipper",
        "/klipper",
        "org.kde.klipper.klipper",
        "setClipboardContents",
        "s",
        "clip text",
    };
    checkVector("argv/busctl", buildClipboardCommand("busctl", "clip text"),
                busctlWant);

    // For every Klipper variant the text is the last element and appears
    // exactly once.
    const std::vector<std::string> klipperTools = {"qdbus6", "qdbus",
                                                   "qdbus-qt5", "gdbus",
                                                   "busctl"};
    for (const std::string &tool : klipperTools) {
        const std::vector<std::string> argv =
            buildClipboardCommand(tool, "clip text");
        check("argv/" + tool + "/non-empty", !argv.empty());
        checkEq("argv/" + tool + "/last",
                argv.empty() ? std::string() : argv.back(), "clip text");
        int occurrences = 0;
        for (const std::string &element : argv) {
            if (element == "clip text") {
                ++occurrences;
            }
        }
        checkEqInt("argv/" + tool + "/once", occurrences, 1);
    }

    // The "s" signature sits immediately before the text and nowhere else.
    const std::vector<std::string> busctl =
        buildClipboardCommand("busctl", "clip text");
    if (busctl.size() == 9) {
        checkEq("argv/busctl/signature", busctl[7], "s");
        checkEq("argv/busctl/interface", busctl[5],
                "org.kde.klipper.klipper");
        checkEq("argv/busctl/method", busctl[6], "setClipboardContents");
    } else {
        fail("argv/busctl/signature", "unexpected argv size " +
                                          std::to_string(busctl.size()));
    }

    // An empty text is passed through, not dropped: Klipper can clear the
    // clipboard that way.
    const std::vector<std::string> empty =
        buildClipboardCommand("qdbus6", std::string());
    checkEqInt("argv/klipper/empty-text/count",
               static_cast<long long>(empty.size()), 5);
    if (empty.size() == 5) {
        checkEq("argv/klipper/empty-text/last", empty.back(), std::string());
    }
}

void testUnknownTool() {
    // An unknown name and an empty name both yield nothing, and never a
    // half-built argv.
    check("argv/unknown/is-empty",
          buildClipboardCommand("sarvam-no-such-tool", "clip text").empty());
    check("argv/empty-name/is-empty",
          buildClipboardCommand(std::string(), "clip text").empty());
    check("argv/unknown/no-text",
          buildClipboardCommand("xclip-", "clip text").empty());

    // Case matters: the names are the executables on PATH.
    check("argv/uppercase/is-empty",
          buildClipboardCommand("WL-COPY", "clip text").empty());
}

// ---- copyToClipboard -----------------------------------------------------

/// Run `scenario` in a forked child so it starts with an uninitialised copy of
/// copyToClipboard()'s cache. The child prints its own FAIL lines (stdout is
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
        if (errno != EINTR) {
            fail(name + " [child]", "waitpid failed");
            return;
        }
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fail(name + " [child]", "child exited non-zero (scenario failed)");
    }
}

/// No clipboard tool anywhere on PATH: the answer is false, not a fork of an
/// empty command.
bool scenarioNoTool(const std::string &tmp) {
    const std::string emptyDir = tmp + "/empty";
    ::mkdir(emptyDir.c_str(), 0700);
    if (::setenv("PATH", emptyDir.c_str(), 1) != 0) {
        fail("copy/no-tool/setenv", "could not set PATH");
        return false;
    }
    if (copyToClipboard("clip text")) {
        fail("copy/no-tool", "copyToClipboard returned true with an empty PATH");
        return false;
    }
    // The empty text is not a special case: no tool is still no tool.
    if (copyToClipboard(std::string())) {
        fail("copy/no-tool/empty-text",
             "copyToClipboard returned true with an empty PATH");
        return false;
    }
    return true;
}

/// A fake wl-copy on PATH: the success path, and the assertion that proves the
/// text reaches the tool on standard input.
bool scenarioFakeWlCopy(const std::string &tmp) {
    const std::string capture = tmp + "/capture.txt";
    const std::string argcFile = tmp + "/capture-argc.txt";
    if (::setenv("SARVAM_CLIP_CAPTURE", capture.c_str(), 1) != 0 ||
        ::setenv("SARVAM_CLIP_ARGC", argcFile.c_str(), 1) != 0) {
        fail("copy/fake/setenv", "could not set the capture variables");
        return false;
    }

    // The temp directory comes first so the fake always wins, even if the
    // machine has a real wl-copy; /usr/bin:/bin are there only so the script
    // can find cat and mv.
    const std::string path = tmp + "/bin:/usr/bin:/bin";
    if (::setenv("PATH", path.c_str(), 1) != 0) {
        fail("copy/fake/setenv-path", "could not set PATH");
        return false;
    }

    if (!copyToClipboard(kClipText)) {
        fail("copy/fake/return",
             "copyToClipboard returned false with a fake wl-copy on PATH");
        return false;
    }
    if (!waitForFile(capture, 5000)) {
        fail("copy/fake/capture", "the fake wl-copy never wrote its stdin");
        return false;
    }

    std::string got;
    if (!readWholeFile(capture, got)) {
        fail("copy/fake/read", "could not read the capture file");
        return false;
    }
    if (got != kClipText) {
        fail("copy/fake/stdin", "got [" + printable(got) + "] want [" +
                                    printable(kClipText) + "]");
        return false;
    }

    // wl-copy takes no arguments, so the text cannot have travelled in argv.
    std::string argc;
    if (!readWholeFile(argcFile, argc)) {
        fail("copy/fake/argc-read", "could not read the argument count file");
        return false;
    }
    if (argc != "0\n") {
        fail("copy/fake/argc", "wl-copy was given [" + printable(argc) +
                                   "] arguments, want [0]");
        return false;
    }

    // An empty text still copies, and still closes the pipe.
    ::unlink(capture.c_str());
    ::unlink((capture + ".part").c_str());
    if (!copyToClipboard(std::string())) {
        fail("copy/fake/empty-text/return",
             "copyToClipboard returned false for an empty text");
        return false;
    }
    if (!waitForFile(capture, 5000)) {
        fail("copy/fake/empty-text/capture",
             "the fake wl-copy never wrote its stdin");
        return false;
    }
    std::string emptyGot;
    if (!readWholeFile(capture, emptyGot)) {
        fail("copy/fake/empty-text/read", "could not read the capture file");
        return false;
    }
    if (!emptyGot.empty()) {
        fail("copy/fake/empty-text/stdin",
             "got [" + printable(emptyGot) + "] want []");
        return false;
    }
    return true;
}

void testCopyToClipboard(const std::string &tmp) {
    const std::string bin = tmp + "/bin";
    if (::mkdir(bin.c_str(), 0700) != 0) {
        fail("copy/setup-bin", "could not create the fake bin directory");
        return;
    }
    const std::string fake = bin + "/wl-copy";
    if (!writeFile(fake, kFakeWlCopy, 0755)) {
        fail("copy/setup-fake", "could not write the fake wl-copy");
        return;
    }

    runInChild("copy/no-tool", tmp, scenarioNoTool);
    runInChild("copy/fake-wl-copy", tmp, scenarioFakeWlCopy);
}

// ---- cleanup -------------------------------------------------------------

void cleanupTempDir(const std::string &tmp) {
    const char *names[] = {"bin/wl-copy", "capture.txt", "capture.txt.part",
                           "capture-argc.txt", "capture-argc.txt.part"};
    for (const char *name : names) {
        const std::string path = tmp + "/" + name;
        ::unlink(path.c_str());
    }
    ::rmdir((tmp + "/bin").c_str());
    ::rmdir((tmp + "/empty").c_str());
    ::rmdir(tmp.c_str());
}

} // namespace

int main() {
    testToolList();
    testStdinToolShapes();
    testKlipperShapes();
    testUnknownTool();

    const std::string tmp = makeTempDir();
    if (tmp.empty()) {
        fail("setup/mkdtemp", "could not create a temp directory");
    } else {
        testCopyToClipboard(tmp);
        cleanupTempDir(tmp);
    }

    if (failures != 0) {
        std::printf("%d failure(s)\n", failures);
        return 1;
    }
    std::printf("all clipboard checks passed\n");
    return 0;
}
