// The preview mosaic encoder: NV12 frames in device memory, one NVENC H.264 session (FFmpeg
// h264_nvenc on CUDA frames, no copy to the host), published over RTSP/TCP to MediaMTX,
// which serves WHEP. Encoder settings as in mxl-webrtc-monitor (MIT, same author): CBR,
// preset p1, ultra-low-latency tune, no B-frames.
#pragma once

#include <cuda_runtime_api.h>

#include <cstdint>
#include <memory>
#include <string>

namespace fxeng
{
struct EncoderConfig
{
    int width = 1920;
    int height = 1080;
    int fps = 25;
    int kbps = 6000;
    // rtsp://host:port/path
    std::string url;
    int gpu = 0;
};

struct EncoderStats
{
    bool open = false;
    bool connected = false;
    std::uint64_t frames = 0;
    std::uint64_t bytes = 0;
    std::uint64_t connects = 0;
    std::uint64_t errors = 0;
    std::string lastError;
};

// Used by the encode thread only.
class MosaicEncoder
{
public:
    explicit MosaicEncoder(EncoderConfig config);
    ~MosaicEncoder();
    MosaicEncoder(MosaicEncoder const&) = delete;
    MosaicEncoder& operator=(MosaicEncoder const&) = delete;

    // Copies the NV12 picture into an encoder frame on `stream`, encodes it and sends the
    // packets. Opens the encoder and (re)connects the RTSP output as needed; while there is no
    // connection the packets are dropped and the next frame after a reconnect is an IDR.
    void encode(std::uint8_t const* luma, int lumaPitch, std::uint8_t const* chroma, int chromaPitch, std::int64_t pts, cudaStream_t stream);
    [[nodiscard]] EncoderStats stats() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace fxeng
