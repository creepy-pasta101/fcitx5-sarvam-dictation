# sarvamdictation is a Module addon: it installs as libsarvamdictation.so into
# the fcitx5 addon directory and is discovered by fcitx5 at startup from the
# .conf file installed below (Library=libsarvamdictation).
#
# add_fcitx5_addon takes the sources directly: it forwards them to add_library,
# which refuses an empty source list at configure time.
#
# sarvamdictationconfig.cpp holds the configuration surface and the TOML
# load/save entry points; tomlfile.cpp is the dependency-free TOML reader and
# writer it sits on. Both are part of the addon rather than a separate library
# so that the addon stays one .so with no runtime lookup.
add_fcitx5_addon(sarvamdictation
    sarvamdictation.cpp
    sarvamdictationconfig.cpp
    tomlfile.cpp
    desktopnotify.cpp
    plasmaosd.cpp
    clipboard.cpp
    audiodevices.cpp)

target_link_libraries(sarvamdictation
    Fcitx5::Core
    # Header-only interface target from fcitx5-modules-dev; it supplies
    # notifications_public.h and the addon-function signature for showTip.
    Fcitx5::Module::Notifications)

install(TARGETS sarvamdictation DESTINATION "${FCITX_INSTALL_ADDONDIR}")

# .conf.in.in -> .conf (@PROJECT_VERSION@ and @FCITX_ADDON_TYPE@ substituted).
configure_file(sarvamdictation.conf.in.in sarvamdictation.conf @ONLY)

# This project ships no translations, so the file is installed as configured.
# If a po/ directory is ever added, wrap this in fcitx5_translate_desktop_file
# (it needs msgfmt from gettext and a po/ directory to have anything to merge).
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/sarvamdictation.conf"
        DESTINATION "${FCITX_INSTALL_PKGDATADIR}/addon"
        COMPONENT config)
