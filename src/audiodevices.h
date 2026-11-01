// SPDX-FileCopyrightText: 2026 Suhan G Paradkar
// SPDX-License-Identifier: MIT
#ifndef _FCITX5_SARVAM_DICTATION_AUDIODEVICES_H_
#define _FCITX5_SARVAM_DICTATION_AUDIODEVICES_H_

#include <string>
#include <vector>

namespace fcitx::sarvam {

struct AudioDeviceEntry {
    std::string id;     ///< what the daemon passes to the capture tool
    std::string label;  ///< what the configuration page shows
};

/// Run `sarvam-dictation-daemon --list-audio-devices` and parse the
/// `id<TAB>label` lines. Cached for a couple of seconds so building the
/// configuration page does not fork repeatedly, and bounded by a hard deadline
/// so a hung or missing binary can never stall fcitx5. Returns an empty vector
/// on any failure.
std::vector<AudioDeviceEntry> listAudioDevices();

} // namespace fcitx::sarvam

#endif
