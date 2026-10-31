// SPDX-FileCopyrightText: 2026 Suhan G Paradkar
// SPDX-License-Identifier: MIT
//
// Desktop notifications for fcitx5-sarvam-dictation, without fcitx5.
//
// We do not link libnotify: we exec the `notify-send` binary, so this file is
// plain C++17 plus POSIX and can be unit tested without a running fcitx5.
// Nothing here logs or prints anything, and in particular the notification
// body (which can carry an API error message) is never written anywhere except
// into the argv we hand to notify-send.

#include "desktopnotify.h"

#include <cerrno>
#include <cstddef>
#include <cstdlib>
#include <string>
#include <vector>

#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

namespace fcitx::sarvam {

namespace {

/// True when `path` names an existing regular file we are allowed to execute.
/// A directory passes access(X_OK) but cannot be exec'd, so the regular-file
/// test is not redundant.
bool isExecutableRegularFile(const std::string &path) {
    struct stat info {};
    if (::stat(path.c_str(), &info) != 0) {
        return false;
    }
    if (!S_ISREG(info.st_mode)) {
        return false;
    }
    return ::access(path.c_str(), X_OK) == 0;
}

/// execvp falls back to a compiled-in default when PATH is unset; mirror that
/// rather than searching only the current directory.
const char *pathEnvironment() {
    const char *value = std::getenv("PATH");
    if (value != nullptr) {
        return value;
    }
    return "/usr/local/bin:/usr/bin:/bin";
}

} // namespace

std::string findInPath(const std::string &name) {
    if (name.empty()) {
        return std::string();
    }

    // A name with a slash in it is a path, not a lookup: execvp would exec it
    // directly and never consult PATH.
    if (name.find('/') != std::string::npos) {
        return isExecutableRegularFile(name) ? name : std::string();
    }

    const std::string path = pathEnvironment();
    std::size_t start = 0;
    while (true) {
        const std::size_t end = path.find(':', start);
        const std::string element =
            (end == std::string::npos) ? path.substr(start)
                                       : path.substr(start, end - start);
        // An empty element means the current directory, as in execvp. Prefix
        // "./" so the result stays usable as the first argument of execv().
        const std::string candidate =
            element.empty() ? ("./" + name) : (element + "/" + name);
        if (isExecutableRegularFile(candidate)) {
            return candidate;
        }
        if (end == std::string::npos) {
            break;
        }
        start = end + 1;
    }
    return std::string();
}

std::vector<std::string> buildNotifySendCommand(const std::string &executable,
                                                const std::string &summary,
                                                const std::string &body,
                                                const std::string &appName,
                                                const std::string &icon,
                                                bool critical,
                                                int expireMs) {
    std::vector<std::string> argv;
    if (executable.empty()) {
        return argv;
    }

    argv.reserve(8);
    argv.push_back(executable);
    argv.push_back("--app-name=" + appName);
    argv.push_back("--icon=" + icon);
    argv.push_back(std::string("--urgency=") +
                   (critical ? "critical" : "normal"));
    argv.push_back("--expire-time=" + std::to_string(expireMs));
    // End of options: a summary that starts with '-' must reach notify-send as
    // the summary, never be parsed as an option of its own.
    argv.push_back("--");
    argv.push_back(summary);
    argv.push_back(body);
    return argv;
}

bool runDetached(const std::vector<std::string> &argv) {
    if (argv.empty() || argv.front().empty()) {
        return false;
    }

    // Build the NULL-terminated C array *before* the fork. Building it in the
    // child would allocate, and after a fork in a multithreaded process only
    // async-signal-safe calls are allowed (malloc is not one of them).
    std::vector<char *> raw;
    raw.reserve(argv.size() + 1);
    for (const std::string &argument : argv) {
        raw.push_back(const_cast<char *>(argument.c_str()));
    }
    raw.push_back(nullptr);

    const char *path = raw.front();
    char *const *execArgv = raw.data();

    const pid_t intermediate = ::fork();
    if (intermediate < 0) {
        return false;
    }
    if (intermediate == 0) {
        // First child: leave our session and process group, then fork the real
        // worker so it is never a direct child of the addon (nothing to reap,
        // nothing to signal) and the intermediate can exit at once.
        ::setsid();
        const pid_t worker = ::fork();
        if (worker != 0) {
            // Covers the worker (reparented to init) and a failed fork alike:
            // either way this level has nothing left to do.
            ::_exit(0);
        }
        // Only async-signal-safe calls from here on: we are a forked child of
        // a multithreaded process.
        const int devNull = ::open("/dev/null", O_RDWR);
        if (devNull >= 0) {
            ::dup2(devNull, STDIN_FILENO);
            ::dup2(devNull, STDOUT_FILENO);
            ::dup2(devNull, STDERR_FILENO);
            if (devNull > STDERR_FILENO) {
                ::close(devNull);
            }
        }
        ::execv(path, execArgv);
        ::_exit(127);
    }

    // Reap the intermediate child so it does not linger as a zombie. It exits
    // immediately, so this does not meaningfully block the caller; the waitpid
    // is retried on EINTR because a signal may interrupt it.
    int status = 0;
    while (::waitpid(intermediate, &status, 0) < 0 && errno == EINTR) {
    }
    return true;
}

} // namespace fcitx::sarvam
