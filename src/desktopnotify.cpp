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
#include <signal.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
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

bool runDetachedWithInput(const std::vector<std::string> &argv,
                          const std::string &input) {
    if (argv.empty() || argv.front().empty()) {
        return false;
    }

    // Built before the fork for the same reason as in runDetached(): after a
    // fork in a multithreaded process only async-signal-safe calls are allowed,
    // and malloc is not one of them.
    std::vector<char *> raw;
    raw.reserve(argv.size() + 1);
    for (const std::string &argument : argv) {
        raw.push_back(const_cast<char *>(argument.c_str()));
    }
    raw.push_back(nullptr);

    const char *path = raw.front();
    char *const *execArgv = raw.data();

    // The pipe has to exist before the fork, so both sides inherit it.
    int ends[2] = {-1, -1};
    if (::pipe(ends) != 0) {
        return false;
    }
    const int readEnd = ends[0];
    const int writeEnd = ends[1];

    const pid_t intermediate = ::fork();
    if (intermediate < 0) {
        ::close(readEnd);
        ::close(writeEnd);
        return false;
    }
    if (intermediate == 0) {
        // This level never writes: its copy of the write end must go, or the
        // worker would wait for an end-of-file that can never arrive.
        ::close(writeEnd);
        // Leave our session and process group, then fork the real worker, so it
        // is never a direct child of the addon. Identical to runDetached().
        ::setsid();
        const pid_t worker = ::fork();
        if (worker != 0) {
            // Covers the worker (reparented to init) and a failed fork alike.
            ::_exit(0);
        }
        // Only async-signal-safe calls from here on.
        if (readEnd != STDIN_FILENO) {
            if (::dup2(readEnd, STDIN_FILENO) < 0) {
                ::_exit(127);
            }
            ::close(readEnd);
        }
        const int devNull = ::open("/dev/null", O_RDWR);
        if (devNull >= 0) {
            ::dup2(devNull, STDOUT_FILENO);
            ::dup2(devNull, STDERR_FILENO);
            if (devNull > STDERR_FILENO) {
                ::close(devNull);
            }
        }
        ::execv(path, execArgv);
        ::_exit(127);
    }

    // Parent: the read end belongs to the child now. Keeping it open here would
    // stop the child from ever seeing end-of-file.
    ::close(readEnd);

    // write() raises SIGPIPE when the reader is already gone, which is not an
    // edge case: a missing binary exits 127 at once. fcitx5 is long-lived and
    // must not die for that, so SIGPIPE is blocked for the duration of the
    // write, the one instance it leaves pending is drained, and the old mask is
    // restored afterwards. The write itself still reports EPIPE, which is
    // reported as failure.
    sigset_t blocked;
    sigset_t previous;
    sigemptyset(&blocked);
    sigaddset(&blocked, SIGPIPE);
    const bool masked = ::sigprocmask(SIG_BLOCK, &blocked, &previous) == 0;

    // A loop, because write() may accept less than the whole buffer; EINTR is
    // retried rather than truncating the text.
    bool written = true;
    const char *data = input.data();
    std::size_t remaining = input.size();
    while (remaining != 0) {
        const ssize_t count = ::write(writeEnd, data, remaining);
        if (count < 0) {
            if (errno == EINTR) {
                continue;
            }
            // EPIPE lands here: the child closed its stdin or never started.
            written = false;
            break;
        }
        if (count == 0) {
            written = false;
            break;
        }
        data += count;
        remaining -= static_cast<std::size_t>(count);
    }
    // Closing the write end is what lets the child finish reading.
    ::close(writeEnd);

    if (masked) {
        // The pending signal has to be consumed before the mask is restored,
        // otherwise restoring it delivers SIGPIPE and kills the process.
        const struct timespec noWait = {0, 0};
        while (::sigtimedwait(&blocked, nullptr, &noWait) < 0 &&
               errno == EINTR) {
        }
        ::sigprocmask(SIG_SETMASK, &previous, nullptr);
    }

    // Reap the intermediate exactly as runDetached() does. The worker is
    // detached and deliberately not waited for.
    int status = 0;
    while (::waitpid(intermediate, &status, 0) < 0 && errno == EINTR) {
    }
    return written;
}

} // namespace fcitx::sarvam
