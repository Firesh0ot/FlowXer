// The mix engine: MXL inputs uploaded once to the GPU, 4 MEs rendered every grain on the TAI
// grid at a fixed latency, PGM + PVW per ME written back to MXL, a preview mosaic encoded once.
//
// Threads (each owns what it touches; queues between them are bounded):
// - fx-in<n>: reads its MXL flow, DMA-uploads each grain on its own CUDA stream into a
//   recycled device frame and publishes it to the input's short ring;
// - fx-render: one per process, on the TAI grid: picks input grain i - L, renders the MEs
//   (descending, so re-entry is same-grain), packs, draws the mosaic, hands the grain slot
//   to the writer;
// - fx-writer: owns the MXL writers: opens grain i, DMA-downloads into it on the download
//   stream, waits for each copy, commits;
// - fx-encode: NVENC + RTSP of the mosaic;
// - fx-stats: CPU per thread and NVML once a second.
#pragma once

#include "core/config.hpp"
#include "core/mixer.hpp"

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
    [[nodiscard]] std::string statusJson() const;
    [[nodiscard]] std::string metricsText() const;
    [[nodiscard]] std::string mosaicMapJson() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace fxeng
