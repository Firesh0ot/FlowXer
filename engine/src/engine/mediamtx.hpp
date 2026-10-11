// The engine's own MediaMTX (when no shared one is given, PREVIEW_PUBLISH_URL unset): RTSP in
// on localhost, WHEP and HLS out. The config is adapted from mxl-webrtc-monitor
// src/ops/mediamtx.cpp (MIT, Copyright (c) 2026 Adrian Hilber).
#pragma once

#include "core/config.hpp"

#include <sys/types.h>

#include <atomic>
#include <string>

namespace fxeng
{
std::string renderMediamtxConfig(Config const& cfg);

// A MediaMTX child process. Not thread-safe: one owner calls start, poll and stop.
class MediamtxProcess
{
public:
    explicit MediamtxProcess(Config const& cfg);
    ~MediamtxProcess();
    MediamtxProcess(MediamtxProcess const&) = delete;
    MediamtxProcess& operator=(MediamtxProcess const&) = delete;

    // Writes the config to <state dir>/mediamtx.yml and starts the binary. Throws
    // std::runtime_error when it cannot.
    void start();
    // Restarts it when it exited (at most every few seconds). True while it runs.
    bool poll();
    void stop();
    [[nodiscard]] int restarts() const
    {
        return restarts_;
    }

private:
    bool spawn();

    Config const& cfg_;
    std::string configPath_;
    pid_t pid_ = -1;
    std::atomic<int> restarts_{0};
    long long lastStart_ = 0;
};
} // namespace fxeng
