#!/usr/bin/env bash
#
# Build and install the fcitx5-sarvam-dictation addon, and (unless --no-daemon)
# the Python dictation daemon that does the actual recording and transcription.
#
# Every dependency is checked up front: this script stops with a readable
# message instead of limping on to a confusing failure halfway through a build.
set -euo pipefail

SCRIPT_NAME="$(basename -- "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${REPO_ROOT}/build"

# Empty means "let CMake decide": the project defaults the prefix to the prefix
# fcitx5 itself was built for, which is where fcitx5 looks for addons. Passing
# /usr/local here would install the addon somewhere fcitx5 never searches.
PREFIX=""
INSTALL_DAEMON=1
USER_PREFIX=0

MIN_CMAKE="3.19"
MIN_PYTHON="3.10"

usage() {
    cat <<'EOF'
Usage: install.sh [OPTIONS]

Build and install the fcitx5-sarvam-dictation addon into an fcitx5 prefix.

Options:
  --prefix PATH   CMake install prefix (default: the fcitx5 prefix, usually /usr)
  --user          Install into ~/.local instead of a system prefix
  --no-daemon     Install only the addon, skip the Python daemon
  -h, --help      Show this help and exit
EOF
}

info() { printf '%s: %s\n' "${SCRIPT_NAME}" "$*"; }
warn() { printf '%s: warning: %s\n' "${SCRIPT_NAME}" "$*" >&2; }
die() { printf '%s: error: %s\n' "${SCRIPT_NAME}" "$*" >&2; exit 1; }

# version_ge HAVE WANT -> succeeds when HAVE is at least WANT.
version_ge() {
    [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "$1 was not found on PATH. $2"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            [ "$#" -ge 2 ] || die "--prefix requires a path argument"
            PREFIX="$2"
            shift 2
            ;;
        --prefix=*)
            PREFIX="${1#*=}"
            shift
            ;;
        --user)
            USER_PREFIX=1
            shift
            ;;
        --no-daemon)
            INSTALL_DAEMON=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option '$1' (try --help)"
            ;;
    esac
done

if [ "${USER_PREFIX}" -eq 1 ]; then
    PREFIX="${HOME}/.local"
fi

# An empty prefix is the normal case: CMake then defaults it to the fcitx5
# prefix. Only a prefix the user actually asked for has to be absolute.
if [ -n "${PREFIX}" ]; then
    case "${PREFIX}" in
        /*) ;;
        *) die "--prefix must be an absolute path, got '${PREFIX}'" ;;
    esac
fi

[ -f "${REPO_ROOT}/CMakeLists.txt" ] || die "no CMakeLists.txt in ${REPO_ROOT}; run this script from a full checkout of the repository"

# ---------------------------------------------------------------- dependencies

require_cmd cmake "Install it with your distribution's package manager."
CMAKE_VERSION="$(cmake --version | head -n1 | awk '{print $3}')"
version_ge "${CMAKE_VERSION}" "${MIN_CMAKE}" \
    || die "cmake ${MIN_CMAKE} or newer is required, found ${CMAKE_VERSION}."

# fcitx5's development files ship the CMake package config the addon needs;
# without them find_package(Fcitx5Core) fails deep inside the build.
find_fcitx5_dev() {
    if command -v pkg-config >/dev/null 2>&1 && pkg-config --exists fcitx5core; then
        return 0
    fi
    local dir
    for dir in \
        "${PREFIX}/lib/cmake/Fcitx5Core" \
        "${PREFIX}/lib64/cmake/Fcitx5Core" \
        /usr/lib/cmake/Fcitx5Core \
        /usr/lib64/cmake/Fcitx5Core \
        /usr/local/lib/cmake/Fcitx5Core \
        /usr/lib/*/cmake/Fcitx5Core; do
        if [ -f "${dir}/Fcitx5CoreConfig.cmake" ]; then
            return 0
        fi
    done
    return 1
}

find_fcitx5_dev || die "fcitx5 development files not found (no Fcitx5CoreConfig.cmake and no pkg-config 'fcitx5core'). Install your distribution's fcitx5 development package, then re-run this script."

if [ "${INSTALL_DAEMON}" -eq 1 ]; then
    require_cmd python3 "The Python daemon needs Python ${MIN_PYTHON} or newer."
    PY_VERSION="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    version_ge "${PY_VERSION}" "${MIN_PYTHON}" \
        || die "Python ${MIN_PYTHON} or newer is required, found ${PY_VERSION}."

    [ -f "${REPO_ROOT}/daemon/pyproject.toml" ] \
        || die "daemon/pyproject.toml not found under ${REPO_ROOT}; the daemon sources are missing from this checkout"

    if python3 -m pip --version >/dev/null 2>&1; then
        DAEMON_INSTALLER="pip"
    elif command -v pipx >/dev/null 2>&1; then
        DAEMON_INSTALLER="pipx"
    else
        die "installing the daemon needs pip or pipx; neither was found. Install python3-pip or pipx, or re-run with --no-daemon to install only the addon."
    fi
fi

# ---------------------------------------------------------------------- build

if [ -n "${PREFIX}" ]; then
    info "configuring the addon (prefix: ${PREFIX})"
    PREFIX_ARG=(-DCMAKE_INSTALL_PREFIX="${PREFIX}")
else
    info "configuring the addon (prefix: the fcitx5 prefix)"
    PREFIX_ARG=()
fi
if ! cmake -S "${REPO_ROOT}" -B "${BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        "${PREFIX_ARG[@]}"; then
    die "cmake configuration failed; the messages above say which fcitx5 package is missing"
fi

JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)"
info "building with ${JOBS} job(s)"
if ! cmake --build "${BUILD_DIR}" --parallel "${JOBS}"; then
    die "the addon failed to build; fix the compiler errors above and re-run this script"
fi

# -------------------------------------------------------------------- install

prefix_writable() {
    if [ -d "${PREFIX}" ]; then
        [ -w "${PREFIX}" ]
        return
    fi
    local parent
    parent="$(dirname -- "${PREFIX}")"
    while [ ! -d "${parent}" ] && [ "${parent}" != "/" ]; do
        parent="$(dirname -- "${parent}")"
    done
    [ -w "${parent}" ]
}

run_install() {
    if [ "${USER_PREFIX}" -eq 1 ] || prefix_writable; then
        cmake --install "${BUILD_DIR}"
    elif command -v sudo >/dev/null 2>&1; then
        info "elevating with sudo to write into ${PREFIX}"
        sudo cmake --install "${BUILD_DIR}"
    else
        die "cannot write to ${PREFIX} and sudo is unavailable; re-run with --user or --prefix \"\$HOME/.local\""
    fi
}

info "installing the addon into ${PREFIX}"
if ! run_install; then
    die "installing the addon into ${PREFIX} failed"
fi

# --------------------------------------------------------------------- daemon

if [ "${INSTALL_DAEMON}" -eq 1 ]; then
    info "installing the Python daemon with ${DAEMON_INSTALLER}"
    if [ "${DAEMON_INSTALLER}" = "pip" ]; then
        if ! python3 -m pip install --user --upgrade "${REPO_ROOT}/daemon"; then
            die "pip install --user failed for the daemon. Use a virtualenv or pipx, or re-run with --no-daemon to install only the addon."
        fi
    else
        if ! pipx install --force "${REPO_ROOT}/daemon"; then
            die "pipx install failed for the daemon; see the pipx output above, or re-run with --no-daemon"
        fi
    fi
    command -v sarvam-dictation-daemon >/dev/null 2>&1 \
        || warn "'sarvam-dictation-daemon' is not on PATH yet; add your user bin directory (usually ~/.local/bin) to PATH"
fi

# ---------------------------------------------------------------- next steps

cat <<EOF

Done. The addon is installed into the fcitx5 addon directory under ${PREFIX}.

Next steps
----------
1. Restart fcitx5 so it loads the new addon (replace the running instance
   and daemonize):
       fcitx5 -rd

2. Confirm the addon was picked up:
       fcitx5-diagnose | grep -i sarvam

3. Set the hotkey (default Control+Alt+d) and the other options:
       fcitx5-configtool        # Addons -> Sarvam Dictation

4. Give the daemon a Sarvam API key (never commit this file):
       mkdir -p ~/.config/sarvam-dictation
       printf '[api]\nkey = "YOUR_SARVAM_API_KEY"\n' > ~/.config/sarvam-dictation/config.toml
       chmod 600 ~/.config/sarvam-dictation/config.toml
   (or export SARVAM_API_KEY=YOUR_SARVAM_API_KEY in your session)

5. Check the daemon answers:
       sarvam-dictation-ctl status
EOF

if [ "${INSTALL_DAEMON}" -eq 1 ]; then
    cat <<'EOF'

The addon auto-spawns the daemon on the first hotkey press. To run it as a
systemd user service instead:
       install -Dm644 packaging/sarvam-dictation.service \
           ~/.config/systemd/user/sarvam-dictation.service
       systemctl --user daemon-reload
       systemctl --user enable --now sarvam-dictation.service
EOF
else
    cat <<'EOF'

The Python daemon was skipped (--no-daemon); install it before using the
addon, or make sure 'sarvam-dictation-daemon' is already on PATH.
EOF
fi
