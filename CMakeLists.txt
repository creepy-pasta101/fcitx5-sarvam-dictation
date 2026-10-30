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

find_package(Fcitx5Core REQUIRED)
find_package(Fcitx5Module REQUIRED COMPONENTS Notifications)

include("${FCITX_INSTALL_CMAKECONFIG_DIR}/Fcitx5Utils/Fcitx5CompilerSettings.cmake")

add_subdirectory(src)
