// SPDX-FileCopyrightText: 2026 Suhan G Paradkar
// SPDX-License-Identifier: MIT
//
// The audio device dropdown.
//
// The list of devices lives in the Python daemon, which is the only part of
// this project that knows how to talk to PulseAudio, PipeWire and ALSA. The
// addon therefore does not link a sound library: it asks the daemon once, with
// `sarvam-dictation-daemon --list-audio-devices`, and turns the `id<TAB>label`
// lines it prints into a fcitx5 config description.
//
// Two properties matter more than anything else here, because this code runs
// inside the fcitx5 process:
//
//  - It must never block. The subprocess is bounded by a hard deadline: the
//    pipe is read with poll() against an absolute monotonic deadline, and a
//    child that is still alive when the deadline passes is killed and reaped.
//    A missing binary, a hung daemon or a daemon that prints megabytes all end
//    the same way, in a bounded amount of time and with an empty (or partial)
//    list.
//  - It must never throw and never write to stderr. There is no exception that
//    escapes this file, and nothing is logged or printed: a diagnostic on
//    stderr from inside fcitx5 would end up in the user's session journal for
//    something that is not an error.
//
// The result is cached for about two seconds with a monotonic timestamp, so
// building the configuration page (which asks for the description several
// times while the dialog is created) forks at most once.

#include "audiodevices.h"

#include <cerrno>
#include <cstddef>
#include <string>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "fcitx-config/rawconfig.h"
#include "fcitx-utils/i18n.h"
#include "desktopnotify.h"
#include "sarvamdictationconfig.h"
#include "tomlfile.h"

namespace fcitx::sarvam {

namespace {

/// The daemon launcher, installed under exactly this name by packaging.
constexpr const char *kDaemonBinary = "sarvam-dictation-daemon";

/// The flag the daemon answers with one `id<TAB>label` line per device.
constexpr const char *kListFlag = "--list-audio-devices";

/// Hard deadline for the whole subprocess conversation, in milliseconds.
constexpr int kDeadlineMs = 500;

/// How long a result stays fresh. Building the config page forks once.
constexpr long long kCacheTtlMs = 2000;

/// Grace period for reaping after SIGKILL, in milliseconds.
constexpr int kKillGraceMs = 100;

/// Upper bound on what we accept from the child. A device list is a few
/// hundred bytes; anything beyond this is a daemon bug and we stop reading.
constexpr std::size_t kMaxOutputBytes = 64 * 1024;

/// Upper bound on the config.toml we are willing to read, in bytes.
constexpr std::size_t kMaxConfigBytes = 1024 * 1024;

/// Monotonic milliseconds, or 0 when the clock cannot be read.
long long monotonicMs() {
    struct timespec now {};
    if (::clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return static_cast<long long>(now.tv_sec) * 1000 +
           static_cast<long long>(now.tv_nsec) / 1000000;
}

/// Sleep for a couple of milliseconds without spinning. A short sleep keeps
/// the waitpid(WNOHANG) poll from burning a core while the child exits.
void shortSleep() {
    struct timespec remaining {};
    remaining.tv_sec = 0;
    remaining.tv_nsec = 2 * 1000 * 1000;
    while (::nanosleep(&remaining, &remaining) != 0 && errno == EINTR) {
        // Interrupted by a signal: continue with the time that is left.
    }
}

/// Wait for `pid` to exit, without ever waiting past `deadline`.
///
/// Returns true when the child has been reaped (or when there is nothing left
/// to reap), false when the deadline passed first. `status` receives the wait
/// status when the child was reaped.
bool reapUntil(pid_t pid, long long deadline, int *status) {
    for (;;) {
        const pid_t reaped = ::waitpid(pid, status, WNOHANG);
        if (reaped == pid) {
            return true;
        }
        if (reaped < 0 && errno != EINTR) {
            // ECHILD: someone else already reaped it, which is as good as done.
            return true;
        }
        if (monotonicMs() >= deadline) {
            return false;
        }
        shortSleep();
    }
}

/// Run the daemon's device listing and return its raw stdout.
///
/// Never throws, never prints, and returns an empty string on every failure.
/// The child's stdout is a pipe we read with poll() against an absolute
/// deadline; the child's stdin and stderr are /dev/null, so nothing the daemon
/// prints can reach our stderr.
std::string runListAudioDevices(const std::string &executable) {
    int pipeFds[2] = {-1, -1};
    if (::pipe(pipeFds) != 0) {
        return std::string();
    }
    const int readFd = pipeFds[0];
    const int writeFd = pipeFds[1];
    // The read end must not survive the exec, or the child would hold the pipe
    // open itself and we would never see EOF.
    (void)::fcntl(readFd, F_SETFD, FD_CLOEXEC);

    const long long deadline = monotonicMs() + kDeadlineMs;
    const pid_t pid = ::fork();
    if (pid < 0) {
        (void)::close(readFd);
        (void)::close(writeFd);
        return std::string();
    }

    if (pid == 0) {
        // Child. Only async-signal-safe calls from here on, because the parent
        // may have other threads.
        //
        // The read end goes first: if the parent's standard descriptors were
        // closed, pipe() can have handed out 0, 1 or 2, and closing the read
        // end after the dup2 below could close the very descriptor the child
        // writes to.
        (void)::close(readFd);
        ::dup2(writeFd, STDOUT_FILENO);
        const int devNull = ::open("/dev/null", O_RDWR);
        if (devNull >= 0) {
            ::dup2(devNull, STDIN_FILENO);
            ::dup2(devNull, STDERR_FILENO);
            if (devNull > STDERR_FILENO) {
                (void)::close(devNull);
            }
        }
        if (writeFd != STDOUT_FILENO) {
            (void)::close(writeFd);
        }
        // Own process group, so an overrunning child can be killed together
        // with anything it spawned (a shell wrapper, a pactl helper).
        (void)::setpgid(0, 0);

        char *const argv[] = {const_cast<char *>(executable.c_str()),
                              const_cast<char *>(kListFlag), nullptr};
        ::execv(executable.c_str(), argv);
        ::_exit(127);
    }

    // Parent. Our copy of the write end must go, or read() never sees EOF.
    (void)::close(writeFd);

    std::string output;
    char buffer[4096];
    // Every way out of this loop keeps whatever was already read: a child that
    // overruns is killed below, and the lines it managed to print are still
    // well formed.
    for (;;) {
        if (output.size() >= kMaxOutputBytes) {
            break;
        }
        const long long remaining = deadline - monotonicMs();
        if (remaining <= 0) {
            break;
        }
        struct pollfd pfd {};
        pfd.fd = readFd;
        pfd.events = POLLIN;
        const int ready = ::poll(&pfd, 1, static_cast<int>(remaining));
        if (ready < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        if (ready == 0) {
            break; // poll timed out: the child overran the deadline.
        }
        const ssize_t received = ::read(readFd, buffer, sizeof(buffer));
        if (received == 0) {
            break; // EOF: the child closed stdout and is finishing.
        }
        if (received < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        output.append(buffer, static_cast<std::size_t>(received));
    }
    (void)::close(readFd);

    // Reap without ever waiting past the deadline; kill the whole group if the
    // child is still running. Note the order: a child that has already been
    // reaped is never signalled, because its pid could have been reused.
    int status = 0;
    if (!reapUntil(pid, deadline, &status)) {
        (void)::kill(-pid, SIGKILL);
        (void)::kill(pid, SIGKILL);
        if (!reapUntil(pid, monotonicMs() + kKillGraceMs, &status)) {
            // SIGKILL cannot be caught, so this returns at once in every case
            // except a child stuck in uninterruptible sleep. Reaping it is
            // still the right thing to attempt.
            while (::waitpid(pid, &status, 0) < 0 && errno == EINTR) {
            }
        }
    }

    return output;
}

/// Parse `id<TAB>label` lines. Anything else is skipped, a blank line is
/// ignored, and the first occurrence of an id wins, so a duplicated device
/// cannot produce a duplicated combobox entry.
std::vector<AudioDeviceEntry> parseDeviceList(const std::string &text) {
    std::vector<AudioDeviceEntry> devices;
    std::size_t start = 0;
    while (start < text.size()) {
        std::size_t end = text.find('\n', start);
        if (end == std::string::npos) {
            end = text.size();
        }
        std::string line = text.substr(start, end - start);
        start = end + 1;
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        if (line.empty()) {
            continue;
        }
        const std::size_t tab = line.find('\t');
        if (tab == std::string::npos) {
            continue;
        }
        std::string id = line.substr(0, tab);
        if (id.empty()) {
            continue;
        }
        bool duplicate = false;
        for (const auto &device : devices) {
            if (device.id == id) {
                duplicate = true;
                break;
            }
        }
        if (duplicate) {
            continue;
        }
        devices.push_back(
            AudioDeviceEntry{std::move(id), line.substr(tab + 1)});
    }
    return devices;
}

/// The cache behind listAudioDevices(). Function-local so the vector is built
/// on first use and no static-initialisation order question arises.
struct DeviceCache {
    bool valid = false;
    long long stamp = 0;
    std::vector<AudioDeviceEntry> devices;
};

DeviceCache &deviceCache() {
    static DeviceCache cache;
    return cache;
}

/// Read a small regular file into `text`. Returns false when the path is not a
/// readable regular file, which is the case for a missing config file.
bool readSmallFile(const std::string &path, std::string *text) {
    const int fd = ::open(path.c_str(), O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return false;
    }
    struct stat info {};
    if (::fstat(fd, &info) != 0 || !S_ISREG(info.st_mode)) {
        (void)::close(fd);
        return false;
    }
    std::string data;
    char buffer[4096];
    bool ok = true;
    for (;;) {
        const ssize_t received = ::read(fd, buffer, sizeof(buffer));
        if (received == 0) {
            break;
        }
        if (received < 0) {
            if (errno == EINTR) {
                continue;
            }
            ok = false;
            break;
        }
        data.append(buffer, static_cast<std::size_t>(received));
        if (data.size() >= kMaxConfigBytes) {
            break;
        }
    }
    (void)::close(fd);
    if (!ok) {
        return false;
    }
    *text = std::move(data);
    return true;
}

/// The device stored in the daemon's config.toml, or an empty string when the
/// file or the key is missing (or the value is not a string).
std::string configuredDevice() {
    const std::string path = daemonConfigPath();
    if (path.empty()) {
        return std::string();
    }
    std::string text;
    if (!readSmallFile(path, &text)) {
        return std::string();
    }
    const TomlDocument document = TomlDocument::parse(text);
    const TomlValue *value = document.get("audio", "device");
    if (value == nullptr || value->type() != TomlType::String) {
        return std::string();
    }
    return value->asString();
}

/// True when `id` is one of `devices`.
bool containsDevice(const std::vector<AudioDeviceEntry> &devices,
                    const std::string &id) {
    for (const auto &device : devices) {
        if (device.id == id) {
            return true;
        }
    }
    return false;
}

} // namespace

std::vector<AudioDeviceEntry> listAudioDevices() {
    try {
        DeviceCache &cache = deviceCache();
        const long long now = monotonicMs();
        if (cache.valid && now >= cache.stamp &&
            now - cache.stamp < kCacheTtlMs) {
            return cache.devices;
        }

        std::vector<AudioDeviceEntry> devices;
        const std::string executable = findInPath(kDaemonBinary);
        if (!executable.empty()) {
            devices = parseDeviceList(runListAudioDevices(executable));
        }

        // The stamp is taken before the fork, so a slow call does not extend
        // how long the (possibly empty) result is served.
        cache.devices = devices;
        cache.stamp = now;
        cache.valid = true;
        return cache.devices;
    } catch (...) {
        // Nothing here is expected to throw; a std::bad_alloc must still not
        // escape into fcitx5's configuration code.
        return std::vector<AudioDeviceEntry>();
    }
}

void AudioDeviceConstrain::dumpDescription(RawConfig &config) const {
    try {
        // Option::dumpDescription has already written Type from
        // OptionTypeName<std::string>::get(), i.e. "String", and
        // dumpDescriptionHelper is a no-op for a plain std::string, so this
        // value is the one the configtool ends up reading. That is what turns
        // a free-text field into a combobox.
        config.setValueByPath("Type", "Enum");

        // Index 0 is the empty string, the documented "follow the desktop"
        // default, so the setting can always be cleared from the dropdown.
        config.setValueByPath("Enum/0", std::string());
        config.setValueByPath("EnumI18n/0",
                              _("System default (follow the desktop)"));

        const std::vector<AudioDeviceEntry> devices = listAudioDevices();
        std::size_t index = 1;
        for (const auto &device : devices) {
            if (device.id.empty()) {
                continue; // index 0 already covers the empty value
            }
            config.setValueByPath("Enum/" + std::to_string(index), device.id);
            config.setValueByPath("EnumI18n/" + std::to_string(index),
                                  device.label);
            ++index;
        }

        // A device that was unplugged must not silently change the setting, so
        // the stored value is kept in the list and marked as missing.
        const std::string current = configuredDevice();
        if (!current.empty() && !containsDevice(devices, current)) {
            config.setValueByPath("Enum/" + std::to_string(index), current);
            config.setValueByPath("EnumI18n/" + std::to_string(index),
                                  current + " " +
                                      _("(not currently available)"));
            ++index;
        }
    } catch (...) {
        // Never throw out of a description dump: the configtool would lose the
        // whole page. A partially filled description is still usable.
    }
}

} // namespace fcitx::sarvam
