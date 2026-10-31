cmake_minimum_required(VERSION 3.19)

project(fcitx5-sarvam-dictation VERSION 0.1.0 LANGUAGES CXX)

# The addon is C++17 and must stay warning-clean; Fcitx5CompilerSettings.cmake
# (included below) adds -Wall -Wextra for us, but only if the standard is set
# before the targets are created.
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE "Release" CACHE STRING "Build type" FORCE)
endif()

# fcitx5 only loads addons from the prefix it was built for. Two things have to
# be true for a system install, and both depend on CMAKE_INSTALL_PREFIX being
# right *before* find_package() runs:
#
#  - Fcitx5UtilsConfig.cmake compares CMAKE_INSTALL_PREFIX against its own
#    FCITX_SYS_INSTALL_PREFIX (/usr) and, only when they match, adopts the
#    distribution path scheme -- otherwise the addon goes to <prefix>/lib/fcitx5,
#    where a distribution fcitx5 never looks.
#  - FCITX_INSTALL_PREFIX is *derived from* CMAKE_INSTALL_PREFIX, so defaulting to
#    it would be a no-op; FCITX_SYS_INSTALL_PREFIX is the real answer, and it is
#    only defined after the package is found.
#
# Hence: default to /usr (or to FCITX_SYS_INSTALL_PREFIX when a previous configure
# already cached it), and leave an explicit -DCMAKE_INSTALL_PREFIX alone.
if(CMAKE_INSTALL_PREFIX_INITIALIZED_TO_DEFAULT)
    if(DEFINED FCITX_SYS_INSTALL_PREFIX AND NOT FCITX_SYS_INSTALL_PREFIX STREQUAL "")
        set(_sarvam_default_prefix "${FCITX_SYS_INSTALL_PREFIX}")
    else()
        set(_sarvam_default_prefix "/usr")
    endif()
    set(CMAKE_INSTALL_PREFIX "${_sarvam_default_prefix}" CACHE PATH
        "Install path prefix, prepended onto install directories." FORCE)
    message(STATUS
        "sarvam-dictation: CMAKE_INSTALL_PREFIX defaulted to "
        "${_sarvam_default_prefix} (the fcitx5 system prefix); pass "
        "-DCMAKE_INSTALL_PREFIX=... to install elsewhere")
endif()

find_package(Fcitx5Core REQUIRED)
find_package(Fcitx5Module REQUIRED COMPONENTS Notifications)

# Provides CMAKE_INSTALL_LIBDIR / BINDIR / DATADIR, which packaging.cmake uses.
include(GNUInstallDirs)

include("${FCITX_INSTALL_CMAKECONFIG_DIR}/Fcitx5Utils/Fcitx5CompilerSettings.cmake")

add_subdirectory(src)

# include(CTest) defines the BUILD_TESTING option (default ON) and calls
# enable_testing(), which is what makes add_test() below visible to ctest.
include(CTest)

if(BUILD_TESTING)
    # The TOML subset has no fcitx5 includes at all, so its unit test builds
    # against the plain standard library: it needs neither Fcitx5::Core nor the
    # addon target, and it must be buildable on a machine that has no fcitx5
    # development package installed.
    add_executable(tomlfile_test
        tests/cpp/test_tomlfile.cpp
        src/tomlfile.cpp)

    target_include_directories(tomlfile_test PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/src"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/cpp"
        "${CMAKE_CURRENT_SOURCE_DIR}")

    add_test(NAME tomlfile COMMAND tomlfile_test)

    # The notify-send helper is fcitx5-free too, so it is tested the same way:
    # the argument vector it builds and its PATH lookup, without a desktop.
    add_executable(desktopnotify_test
        tests/cpp/test_desktopnotify.cpp
        src/desktopnotify.cpp)

    target_include_directories(desktopnotify_test PRIVATE
        "${CMAKE_CURRENT_SOURCE_DIR}/src"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/cpp")

    add_test(NAME desktopnotify COMMAND desktopnotify_test)
endif()

# Daemon-side install rules plus the CPack configuration (TGZ and RPM). Included
# last so that every install rule in the project is known to CPack by the time it
# reads the install tree, and so that PROJECT_VERSION and the Fcitx5 install
# directories are already defined.
include(packaging/packaging.cmake)
