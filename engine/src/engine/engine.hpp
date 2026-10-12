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
// - fx-writer: owns the MXL writers: opens grain i, DMA-downloads into it on the download
//   stream, waits for each copy, commits; then the PGM audio of grain i;
// - fx-encode: NVENC + RTSP of the mosaic;
// - fx-stats: CPU per thread and NVML once a second.
#pragma once

#include "core/config.hpp"
#include "core/mixer.hpp"
#include "engine/player.hpp"

#include <memory>
#include <string>

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
    // Clip player k (1-based). Returns an error message, empty when done.
    std::string playerCommand(int k, PlayerCommand const& command);
    // Stinger player k (1-based), and the frames of its loaded stinger (0: none; -1: no such
    // player).
    std::string stingerCommand(int k, PlayerCommand const& command);
    [[nodiscard]] long long stingerFrames(int k) const;

    [[nodiscard]] std::string statusJson() const;
    [[nodiscard]] std::string metricsText() const;
    [[nodiscard]] std::string mosaicMapJson() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace fxeng
