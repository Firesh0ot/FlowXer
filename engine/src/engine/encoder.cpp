#include "engine/encoder.hpp"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_cuda.h>
#include <libavutil/opt.h>
}

#include <cuda.h>
#include <cuda_runtime.h>

#include <chrono>
#include <mutex>

namespace fxeng
{
namespace
{
std::string avError(int code)
{
    char text[AV_ERROR_MAX_STRING_SIZE] = {};
    av_strerror(code, text, sizeof(text));
    return text;
}

using Clock = std::chrono::steady_clock;
} // namespace

struct MosaicEncoder::Impl
{
    EncoderConfig cfg;
    AVBufferRef* device = nullptr;
    AVBufferRef* frames = nullptr;
    AVCodecContext* enc = nullptr;
    AVFormatContext* out = nullptr;
    AVPacket* packet = nullptr;
    bool needIdr = true;
    Clock::time_point retryEncoder{};
    Clock::time_point retryOutput{};
    mutable std::mutex mu;
    EncoderStats stats;

    void fail(std::string const& what)
    {
        std::lock_guard lock{mu};
        ++stats.errors;
        stats.lastError = what;
    }

    void closeEncoder()
    {
        avcodec_free_context(&enc);
        av_buffer_unref(&frames);
        av_buffer_unref(&device);
        std::lock_guard lock{mu};
        stats.open = false;
    }

    void closeOutput()
    {
        if (out != nullptr)
        {
            // The RTSP muxer closes its connection in the trailer (TEARDOWN), even after an error.
            av_write_trailer(out);
            avformat_free_context(out);
            out = nullptr;
        }
        std::lock_guard lock{mu};
        stats.connected = false;
    }

    bool openEncoder()
    {
        AVCodec const* codec = avcodec_find_encoder_by_name("h264_nvenc");
        if (codec == nullptr)
        {
            fail("h264_nvenc is not in this FFmpeg build");
            return false;
        }
        // FFmpeg works in this thread's current context (the CUDA runtime's primary context),
        // so the engine's device pointers are valid in its frames. Its own "primary_ctx" option
        // refuses a primary context that the runtime already initialised with other flags.
        void* getCurrent = nullptr;
        cudaDriverEntryPointQueryResult found{};
        CUcontext context = nullptr;
        if (cudaGetDriverEntryPointByVersion("cuCtxGetCurrent", &getCurrent, 12000, cudaEnableDefault, &found) != cudaSuccess || getCurrent == nullptr ||
            reinterpret_cast<CUresult (*)(CUcontext*)>(getCurrent)(&context) != CUDA_SUCCESS || context == nullptr)
        {
            fail("no current CUDA context");
            return false;
        }
        device = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_CUDA);
        auto* deviceCtx = reinterpret_cast<AVHWDeviceContext*>(device->data);
        static_cast<AVCUDADeviceContext*>(deviceCtx->hwctx)->cuda_ctx = context;
        int rc = av_hwdevice_ctx_init(device);
        if (rc < 0)
        {
            fail("cuda device: " + avError(rc));
            return false;
        }
        frames = av_hwframe_ctx_alloc(device);
        auto* framesCtx = reinterpret_cast<AVHWFramesContext*>(frames->data);
        framesCtx->format = AV_PIX_FMT_CUDA;
        framesCtx->sw_format = AV_PIX_FMT_NV12;
        framesCtx->width = cfg.width;
        framesCtx->height = cfg.height;
        rc = av_hwframe_ctx_init(frames);
        if (rc < 0)
        {
            fail("cuda frames: " + avError(rc));
            return false;
        }
        enc = avcodec_alloc_context3(codec);
        enc->width = cfg.width;
        enc->height = cfg.height;
        enc->time_base = AVRational{1, cfg.fps};
        enc->framerate = AVRational{cfg.fps, 1};
        enc->pix_fmt = AV_PIX_FMT_CUDA;
        enc->hw_frames_ctx = av_buffer_ref(frames);
        enc->bit_rate = static_cast<std::int64_t>(cfg.kbps) * 1000;
        enc->rc_max_rate = enc->bit_rate;
        enc->rc_buffer_size = static_cast<int>(enc->bit_rate / cfg.fps * 2);
        // One second GOP: a new WHEP viewer gets a picture within a second.
        enc->gop_size = cfg.fps;
        enc->max_b_frames = 0;
        // SPS/PPS in the RTSP SDP; MediaMTX repeats them before each IDR for WebRTC readers.
        enc->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
        AVDictionary* options = nullptr;
        av_dict_set(&options, "preset", "p1", 0);
        av_dict_set(&options, "tune", "ull", 0);
        av_dict_set(&options, "rc", "cbr", 0);
        av_dict_set(&options, "zerolatency", "1", 0);
        av_dict_set(&options, "delay", "0", 0);
        av_dict_set(&options, "forced-idr", "1", 0);
        rc = avcodec_open2(enc, codec, &options);
        av_dict_free(&options);
        if (rc < 0)
        {
            fail("h264_nvenc open: " + avError(rc));
            return false;
        }
        packet = packet != nullptr ? packet : av_packet_alloc();
        std::lock_guard lock{mu};
        stats.open = true;
        return true;
    }

    bool openOutput()
    {
        int rc = avformat_alloc_output_context2(&out, nullptr, "rtsp", cfg.url.c_str());
        if (rc < 0 || out == nullptr)
        {
            fail("rtsp output: " + avError(rc));
            out = nullptr;
            return false;
        }
        AVStream* stream = avformat_new_stream(out, nullptr);
        avcodec_parameters_from_context(stream->codecpar, enc);
        stream->time_base = enc->time_base;
        AVDictionary* options = nullptr;
        av_dict_set(&options, "rtsp_transport", "tcp", 0);
        // Socket timeout (microseconds): a stalled MediaMTX does not block the encode thread for long.
        av_dict_set(&options, "timeout", "2000000", 0);
        rc = avformat_write_header(out, &options);
        av_dict_free(&options);
        if (rc < 0)
        {
            fail("rtsp connect " + cfg.url + ": " + avError(rc));
            avformat_free_context(out);
            out = nullptr;
            return false;
        }
        needIdr = true;
        std::lock_guard lock{mu};
        stats.connected = true;
        return true;
    }
};

MosaicEncoder::MosaicEncoder(EncoderConfig config)
    : impl_(std::make_unique<Impl>())
{
    impl_->cfg = std::move(config);
}

MosaicEncoder::~MosaicEncoder()
{
    impl_->closeOutput();
    impl_->closeEncoder();
    av_packet_free(&impl_->packet);
}

void MosaicEncoder::encode(std::uint8_t const* luma, int lumaPitch, std::uint8_t const* chroma, int chromaPitch, std::int64_t pts, cudaStream_t stream)
{
    auto& s = *impl_;
    auto const now = Clock::now();
    if (s.enc == nullptr)
    {
        if (now < s.retryEncoder)
        {
            return;
        }
        if (!s.openEncoder())
        {
            s.closeEncoder();
            s.retryEncoder = now + std::chrono::seconds(5);
            return;
        }
    }
    if (s.out == nullptr && now >= s.retryOutput)
    {
        if (!s.openOutput())
        {
            s.retryOutput = now + std::chrono::seconds(2);
        }
        else
        {
            std::lock_guard lock{s.mu};
            ++s.stats.connects;
        }
    }
    AVFrame* frame = av_frame_alloc();
    int rc = av_hwframe_get_buffer(s.frames, frame, 0);
    if (rc < 0)
    {
        s.fail("frame: " + avError(rc));
        av_frame_free(&frame);
        return;
    }
    bool const copied =
        cudaMemcpy2DAsync(frame->data[0], static_cast<std::size_t>(frame->linesize[0]), luma, static_cast<std::size_t>(lumaPitch),
            static_cast<std::size_t>(s.cfg.width), static_cast<std::size_t>(s.cfg.height), cudaMemcpyDeviceToDevice, stream) == cudaSuccess &&
        cudaMemcpy2DAsync(frame->data[1], static_cast<std::size_t>(frame->linesize[1]), chroma, static_cast<std::size_t>(chromaPitch),
            static_cast<std::size_t>(s.cfg.width), static_cast<std::size_t>(s.cfg.height / 2), cudaMemcpyDeviceToDevice, stream) == cudaSuccess &&
        cudaStreamSynchronize(stream) == cudaSuccess;
    if (!copied)
    {
        s.fail("frame copy failed");
        av_frame_free(&frame);
        return;
    }
    frame->pts = pts;
    if (s.needIdr && s.out != nullptr)
    {
        frame->pict_type = AV_PICTURE_TYPE_I;
        s.needIdr = false;
    }
    rc = avcodec_send_frame(s.enc, frame);
    av_frame_free(&frame);
    if (rc < 0)
    {
        s.fail("encode: " + avError(rc));
        return;
    }
    while (avcodec_receive_packet(s.enc, s.packet) == 0)
    {
        std::uint64_t const size = static_cast<std::uint64_t>(s.packet->size);
        if (s.out != nullptr)
        {
            s.packet->stream_index = 0;
            av_packet_rescale_ts(s.packet, s.enc->time_base, s.out->streams[0]->time_base);
            rc = av_interleaved_write_frame(s.out, s.packet);
            if (rc < 0)
            {
                s.fail("rtsp write: " + avError(rc));
                s.closeOutput();
                s.retryOutput = now + std::chrono::seconds(1);
            }
        }
        av_packet_unref(s.packet);
        std::lock_guard lock{s.mu};
        s.stats.bytes += size;
    }
    std::lock_guard lock{s.mu};
    ++s.stats.frames;
}

EncoderStats MosaicEncoder::stats() const
{
    std::lock_guard lock{impl_->mu};
    return impl_->stats;
}
} // namespace fxeng
