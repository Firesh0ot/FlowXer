// A clip player: one source of the MEs that plays a clip from the library (GPU decode) or shows
// a still (uploaded once: a frame memory). Its own thread (fx-cp<n>) decodes ahead; the render
// thread takes the frame of each output grain, so a playing clip advances one frame per grain on
// the TAI grid. An empty player is black.
//
// Clips are the library's mezzanine: HEVC 4:4:4 (10-bit) at the engine's raster and rate,
// decoded by NVDEC, plus a raw sound file (float32, interleaved, 48 kHz) of exactly the clip's
// length. Stills are one raw v210 picture at the engine's raster.
#pragma once

#include "core/tai.hpp"

#include <cuda_runtime_api.h>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace fxeng
{
// A clip player command (POST /player/{k}/...).
struct PlayerCommand
{
    enum class Action
    {
        Load,
        Cue,
        Play,
        Pause,
        Stop
    };
    Action action = Action::Cue;
    // Load: the media file ("" unloads: the player shows black) and its kind ("clip" or "still").
    // Loading the file that is loaded already only changes loop and play on cut.
    std::string path;
    std::string kind;
    // Load of a clip: its sound file ("" plays silence), its channels and the clip's frames (0:
    // from the file's duration).
    std::string audio;
    int channels = 0;
    long long frames = 0;
    bool loop = false;
    // Play when the player goes to an ME's Program.
    bool playOnCut = false;
    // Cue: the frame to show (0 = the first).
    long long frame = 0;
};

struct PlayerSetup
{
    int number = 1;
    std::string label;
    int width = 1920;
    int height = 1080;
    Rate rate{50, 1};
    int gpu = 0;
    // Bytes of a packed v210 picture.
    std::size_t frameBytes = 0;
    // Channels of the PGM audio.
    std::size_t channels = 2;
};

// What a clip player shows in one output grain.
struct PlayerFrame
{
    // The output grain.
    std::uint64_t index = 0;
    // Packed v210 on the device; nullptr: black.
    std::uint8_t const* v210 = nullptr;
    // Recorded after the picture was made; the render stream waits on it.
    cudaEvent_t ready = nullptr;
    // The sound of this grain, channel-major (`samples` per channel); empty: silence.
    std::vector<float> audio;
    std::size_t samples = 0;
    std::size_t channels = 0;
    // Keeps the device picture until the grain is written.
    std::shared_ptr<void> hold;
};

// Copies the frame's sound into `dst` (`ch` channels of `n` samples, channel-major); missing
// channels and samples stay silent.
void copyPlayerAudio(PlayerFrame const& frame, std::vector<float>& dst, std::size_t n, std::size_t ch);

// The first sample (48 kHz) of clip frame `frame`: a clip's sound is cut into frames on the
// same grid as the library's conversion (whole frames, no drift over a loop).
std::uint64_t clipSampleAt(Rate rate, long long frame);

class ClipPlayer
{
public:
    explicit ClipPlayer(PlayerSetup setup);
    ~ClipPlayer();
    ClipPlayer(ClipPlayer const&) = delete;
    ClipPlayer& operator=(ClipPlayer const&) = delete;

    // Allocates the picture pool (on the engine's GPU) and starts the decode thread.
    void start();
    void stop();
    // Render thread: the frame of output grain `index`. `onAir` is true while the player is in an
    // ME's Program (play on cut starts it then).
    std::shared_ptr<PlayerFrame> frameFor(std::uint64_t index, bool onAir);
    // An error message, empty when done.
    std::string command(PlayerCommand const& command);
    [[nodiscard]] std::string statusJson() const;
    [[nodiscard]] std::size_t deviceBytes() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace fxeng
