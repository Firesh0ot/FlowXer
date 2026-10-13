// The mix engine: MXL inputs uploaded once to the GPU, the MEs rendered every grain on the TAI
// grid at a fixed latency, PGM video + audio and PVW video per ME written back to MXL, a preview
// mosaic encoded once. FlowXer's control plane starts it, routes its inputs (IS-05) and drives
// the MEs over the control API (engine/api.hpp).
//
// Threads (each owns what it touches; queues between them are bounded):
// - fx-in<n>: reads its routed MXL flow, DMA-uploads each grain on its own CUDA stream into a
//   recycled device frame and publishes it to the input's short ring; re-opens on a new route;
// - fx-key<n>: the same for a key input (v210a fill + key);
// - fx-cp<n>: a clip player's decoder (GPU decode of clips, stills uploaded once);
// - fx-st<n>: a stinger player's decoder (colour and alpha in lockstep, sound);
// - fx-render: one per process, on the TAI grid: picks input grain i - L, renders the MEs
//   (descending, so re-entry is same-grain), keys the DSKs, packs, draws the mosaic, hands the
//   grain slot to the writer;
// - fx-writer: owns the MXL video writers: opens grain i, DMA-downloads into it on the download
//   stream, waits for each copy, commits; hands grain i's plans and player sound to fx-audio;
//   copies the frame grabs of grain i (picked by fx-render) into host memory;
// - fx-audio: the audio readers, the live inputs' delay lines, the MEs' audio mixes
//   (core/audiomix) and the PGM audio writers: the PGM audio of grain i (docs/engine-p5.md);
// - fx-grab: writes each frame grab's raw v210 file;
// - fx-encode: NVENC + RTSP of the mosaic;
// - fx-stats: CPU per thread and NVML once a second.
#pragma once

#include "core/audiomix.hpp"
#include "core/config.hpp"
#include "core/mixer.hpp"
#include "engine/player.hpp"

#include <memory>
#include <optional>
#include <string>
#include <vector>

namespace fxeng
{
class Engine
{
public:
    explicit Engine(Config config);
    ~Engine();
    Engine(Engine const&) = delete;
    Engine& operator=(Engine const&) = delete;

    // Throws std::runtime_error when the GPU, the output domain or a writer cannot be set up.
    void start();
    void stop();

    Mixer& mixer();
    // Routes live input k (1-based): an empty flow closes that reader. False for a bad k.
    bool routeInput(int k, Route const& video, Route const& audio);
    // Routes key input k (1-based).
    bool routeKey(int k, Route const& route);
    // Keyer k's box buffer once its box is enabled (called by the control API after a settings
    // change, so the render thread never allocates).
    void prepareBox(int k);
    // Clip player k (1-based). Returns an error message, empty when done.
    std::string playerCommand(int k, PlayerCommand const& command);
    // Stinger player k (1-based), and the frames of its loaded stinger (0: none; -1: no such
    // player).
    std::string stingerCommand(int k, PlayerCommand const& command);
    [[nodiscard]] long long stingerFrames(int k) const;
    // A frame grab (docs/engine-p4.md §6) of `source` ("in<k>", "cp<k>", "me<n>": its Program, or
    // its Preview with `preview`) as it is in the next output grain, written as raw v210 to `path`.
    // Its id (0 with an error message in `error`); at most 4 grabs per grain, 8 in flight.
    int grab(std::string const& source, bool preview, std::string const& path, std::string& error);
    // A grab's state as JSON ("" for an unknown id; the last 32 are kept).
    [[nodiscard]] std::string grabJson(int id) const;

    // P5 audio (docs/engine-p5.md §8). The MEs' audio mixes: master and strips (after start()).
    AudioMix& audioMix();
    // Live input k's audio delay (ms: 0 … FLOWXER_ENGINE_AUDIO_MAX_DELAY_MS, in whole 48 kHz
    // samples) and channel map (one entry per PGM channel: the input channel, 1-based, 0 silent;
    // empty: 1:1). A change crossfades over one grain. An error message, empty when done ("no
    // input …" for a bad k).
    std::string setInputAudio(int k, std::optional<double> delayMs, std::optional<std::vector<int>> map);
    // Per ME its master and strips, per live input its delay and map (the status's `audio`).
    [[nodiscard]] std::string audioJson() const;
    // The meters of the last finished 100 ms block (GET /audio/meters).
    [[nodiscard]] std::string metersJson() const;
    // ME m's integrated loudness from the next block on; false for a bad m.
    bool resetLoudness(int me);

    [[nodiscard]] std::string statusJson() const;
    [[nodiscard]] std::string metricsText() const;
    [[nodiscard]] std::string mosaicMapJson() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace fxeng
