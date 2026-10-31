# packaging/packaging.cmake
#
# Included by the root CMakeLists.txt at the very end, after add_subdirectory(src).
# It adds the daemon-side install rules (the addon's own rules live in
# src/CMakeLists.txt) and then configures CPack to emit a TGZ and an RPM.
#
# One package ships both halves: the fcitx5 addon records nothing without the
# daemon, and the daemon has no user interface without the addon.
#
# Everything installed here is relative to CMAKE_INSTALL_PREFIX, because the RPM
# is generated from the install tree with CPACK_SET_DESTDIR and
# CPACK_INSTALL_PREFIX left at their defaults.

include_guard(GLOBAL)

# ---------------------------------------------------------------------------
# 1. Daemon-side install rules
# ---------------------------------------------------------------------------

# The interpreter is only used to find out where the Python package belongs and
# to fill in the launcher's shebang; nothing is built with it.
find_package(Python3 COMPONENTS Interpreter)
if(NOT Python3_Interpreter_FOUND)
    message(FATAL_ERROR
        "packaging.cmake needs a Python 3 interpreter to locate site-packages "
        "for the sarvam-dictation daemon. Install python3 (or pass "
        "-DPython3_EXECUTABLE=/path/to/python3) and re-run CMake.")
endif()

# Where the daemon package goes.
#
# Python3_SITEARCH is absolute, and it must be installed *relative to the prefix*
# rather than verbatim: CPack cannot relocate an absolute DESTINATION, so it would
# try to write into the build host's real /usr and fail with "Permission denied"
# for any non-root build.
#
# file(RELATIVE_PATH) is what makes this correct, because it compares path
# components instead of strings. With a /usr prefix and an interpreter in
# /usr/local it yields "local/lib/python3.12/site-packages", which is exactly
# where that interpreter looks; a naive strip would give the same answer here but
# breaks the other way round. When the interpreter is outside the prefix
# altogether the result starts with "..", and only then do we fall back to the
# conventional layout. Note the deliberate "lib": Python never uses lib64 or a
# multiarch directory, so CMAKE_INSTALL_LIBDIR is the wrong variable here.
set(_sarvam_python_fallback "lib/python${Python3_VERSION_MAJOR}.${Python3_VERSION_MINOR}/site-packages")
set(_sarvam_python_site "${_sarvam_python_fallback}")

if(Python3_SITEARCH)
    file(RELATIVE_PATH _sarvam_python_site_rel "${CMAKE_INSTALL_PREFIX}" "${Python3_SITEARCH}")
    if(IS_ABSOLUTE "${_sarvam_python_site_rel}" OR _sarvam_python_site_rel MATCHES "^\.\.")
        message(WARNING
            "The interpreter's site-packages (${Python3_SITEARCH}) is outside "
            "CMAKE_INSTALL_PREFIX (${CMAKE_INSTALL_PREFIX}), so the daemon is "
            "installed to ${_sarvam_python_fallback} instead. Pass "
            "-D_sarvam_python_site_rel=<relative path> to override.")
    else()
        set(_sarvam_python_site "${_sarvam_python_site_rel}")
    endif()
else()
    message(WARNING
        "Python3_SITEARCH is empty; installing the daemon to ${_sarvam_python_fallback}. "
        "Pass -D_sarvam_python_site_rel=<relative path> if that is wrong here.")
endif()

message(STATUS "sarvam-dictation: Python package destination: ${_sarvam_python_site}")

# 1a. The Python package. Only the sources are packaged: FILES_MATCHING alone
#     still creates the directories it walks, so __pycache__ has to be excluded
#     explicitly or the package ships an empty (or stale) byte-code directory.
install(DIRECTORY "${PROJECT_SOURCE_DIR}/daemon/sarvam_dictation/"
        DESTINATION "${_sarvam_python_site}/sarvam_dictation"
        FILES_MATCHING
        PATTERN "*.py"
        PATTERN "__pycache__" EXCLUDE
        PATTERN "*.pyc" EXCLUDE
        PATTERN "*.pyo" EXCLUDE)

# 1b. The launcher. It is installed as exactly `sarvam-dictation-daemon`: that is
#     the addon's DaemonCommand default and the name the systemd user unit runs.
configure_file("${PROJECT_SOURCE_DIR}/packaging/sarvam-dictation-daemon.in"
               "${CMAKE_CURRENT_BINARY_DIR}/sarvam-dictation-daemon"
               @ONLY)
install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/sarvam-dictation-daemon"
        DESTINATION "${CMAKE_INSTALL_BINDIR}")

# 1c. The control client.
install(PROGRAMS "${PROJECT_SOURCE_DIR}/scripts/sarvam-dictation-ctl"
        DESTINATION "${CMAKE_INSTALL_BINDIR}")

# 1d. The systemd *user* unit. systemd looks in /usr/lib/systemd/user on every
#     distribution -- not in lib64, not in the multiarch libdir, and not in
#     share/systemd -- so this path is spelled out instead of using
#     CMAKE_INSTALL_LIBDIR, which would put it in /usr/lib64/systemd/user on
#     Fedora where systemd never looks.
install(FILES "${PROJECT_SOURCE_DIR}/packaging/sarvam-dictation.service"
        DESTINATION "lib/systemd/user")

# 1e. Licence and README, into the usual documentation directory.
install(FILES "${PROJECT_SOURCE_DIR}/LICENSE"
              "${PROJECT_SOURCE_DIR}/README.md"
        DESTINATION "${CMAKE_INSTALL_DATADIR}/doc/fcitx5-sarvam-dictation")

# ---------------------------------------------------------------------------
# 2. CPack
# ---------------------------------------------------------------------------

set(CPACK_PACKAGE_NAME "fcitx5-sarvam-dictation")
set(CPACK_PACKAGE_VENDOR "Suhan G Paradkar")
set(CPACK_PACKAGE_VERSION "${PROJECT_VERSION}")
set(CPACK_PACKAGE_DESCRIPTION_SUMMARY
    "Fcitx 5 voice-dictation addon with the Python daemon it drives")
string(CONCAT CPACK_PACKAGE_DESCRIPTION
    "Fcitx 5 voice-dictation addon plus the Python daemon it drives. "
    "Press a hotkey anywhere, speak, press it again, and the transcript is typed "
    "into the focused window. The C++ addon owns the hotkey, the desktop "
    "notifications and the text insertion and never speaks HTTP; the Python "
    "daemon owns audio capture and calls Sarvam AI's Saaras speech-to-text model "
    "over HTTPS, and the two talk over a Unix socket with a newline-delimited "
    "JSON protocol. Both halves are installed by this package, because neither is "
    "useful without the other. The sarvamai Python SDK is not bundled: it is "
    "distributed only on PyPI, so run 'pip install sarvamai' after installing.")
set(CPACK_PACKAGE_HOMEPAGE_URL "https://github.com/creepy-pasta101/fcitx5-sarvam-dictation")
set(CPACK_PACKAGE_CONTACT "Suhan G Paradkar <suhangp2002@gmail.com>")
set(CPACK_RESOURCE_FILE_LICENSE "${PROJECT_SOURCE_DIR}/LICENSE")
set(CPACK_RESOURCE_FILE_README "${PROJECT_SOURCE_DIR}/README.md")
set(CPACK_GENERATOR "TGZ;RPM")

# RPM-specific. The release number is fixed at 1; the architecture is whatever
# CPack detects on the build host.
set(CPACK_RPM_PACKAGE_NAME "fcitx5-sarvam-dictation")
set(CPACK_RPM_PACKAGE_LICENSE "MIT")
set(CPACK_RPM_PACKAGE_RELEASE "1")
set(CPACK_RPM_PACKAGE_SUMMARY "Voice dictation for Fcitx 5 via Sarvam AI")
set(CPACK_RPM_PACKAGE_DESCRIPTION "${CPACK_PACKAGE_DESCRIPTION}")
set(CPACK_RPM_PACKAGE_GROUP "System Environment/Libraries")
set(CPACK_RPM_PACKAGE_URL "${CPACK_PACKAGE_HOMEPAGE_URL}")

# The explicit list below is the whole dependency set, so rpmbuild's automatic
# requires (which would pick up every shared library the .so happens to link and
# noise from the Python files) are switched off.
set(CPACK_RPM_PACKAGE_AUTOREQ OFF)

# Only packages that exist in the Fedora and openSUSE repositories: fcitx5 (the
# input-method framework the addon is loaded into) and python3 (the interpreter
# the daemon and the launcher need). A requirement on a package that does not
# exist in the repositories would make the RPM uninstallable.
set(CPACK_RPM_PACKAGE_REQUIRES "fcitx5, python3")
# The sarvamai SDK is PyPI-only -- there is no python3-sarvamai in Fedora or
# openSUSE -- so it is deliberately NOT a hard requirement: 'pip install sarvamai'
# is documented in the README instead. Uncomment only if you build against a
# repository that does carry it:
# set(CPACK_RPM_PACKAGE_REQUIRES "fcitx5, python3, python3-sarvamai")

# RPM-DEFAULT gives fcitx5-sarvam-dictation-0.1.0-1.<arch>.rpm.
set(CPACK_RPM_FILE_NAME "RPM-DEFAULT")

# Note: the systemd unit and the addon's .conf are package-owned files and are
# shipped as plain files. They are configuration the user is expected to change
# through the fcitx5 configuration tool, so no %config(noreplace) handling is
# added here; an upgrade replaces them.

include(CPack)
