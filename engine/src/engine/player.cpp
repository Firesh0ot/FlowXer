#include "engine/player.hpp"

#include "core/pixel.hpp"
#include "core/playout.hpp"
#include "engine/gpu.hpp"
#include "engine/log.hpp"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_cuda.h>
#include <libavutil/pixdesc.h>
}

#include <cuda.h>
#include <cuda_runtime.h>
#include <fcntl.h>
#include <nlohmann/json.hpp>
#include <pthread.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstring>
#include <deque>
#include <fstream>
#include <mutex>
#include <thread>

namespace fxeng
{
namespace
{
// Decoded frames queued ahead of the one shown, and the pictures of a player: the queue, the
// one shown, and those the render thread still holds for grains in flight.
constexpr std::size_t kAhead = 5;
constexpr std::size_t kPool = 9;
constexpr std::uint64_t kAudioRate = 48000;
constexpr int kMaxChannels = 16;

using Clock = std::chrono::steady_clock;

std::string avError(int code)
{
    char text[AV_ERROR_MAX_STRING_SIZE] = {};
    av_strerror(code, text, sizeof(text));
    return text;
}

// A device picture of the pool. Free while only the pool holds it.
struct Picture
{
    std::uint8_t* v210 = nullptr;
    cudaEvent_t ready = nullptr;
};

// A decoded clip frame in play order (or the still).
struct Decoded
{
    std::shared_ptr<Picture> picture;
    std::uint64_t generation = 0;
    long long seq = 0;
    long long frame = 0;
    // Channel-major, `samples` per channel.
    std::vector<float> audio;
    std::size_t samples = 0;
};

// What identifies a loaded file: loading the same file again only changes the options.
struct FileId
{
    std::string path;
    std::string audio;
    dev_t dev = 0;
    ino_t ino = 0;
    long long size = 0;
    long long mtimeNs = 0;

    bool operator==(FileId const& o) const
    {
        return path == o.path && audio == o.audio && dev == o.dev && ino == o.ino && size == o.size && mtimeNs == o.mtimeNs;
    }
};

// A loaded clip or still. Opened and checked by the control thread, then used by the decode
// thread only.
struct Media
{
    std::string kind;
    FileId id;
    long long frames = 1;
    // Clip: the demuxer (probed), its video stream and the sound file; a stinger's alpha stream.
    AVFormatContext* format = nullptr;
    int stream = -1;
    AVFormatContext* alphaFormat = nullptr;
    int alphaStream = -1;
    int audioFd = -1;
    int audioChannels = 0;
    // Still: the picture until the decode thread uploads it.
    std::vector<std::uint8_t> still;
    std::atomic<bool> failed{false};

    ~Media()
    {
        avformat_close_input(&format);
        avformat_close_input(&alphaFormat);
        if (audioFd >= 0)
        {
            close(audioFd);
        }
    }
};

// An NVDEC decoder of the loaded clip, or of a stinger's alpha (decode thread only).
struct Decoder
{
    // The demuxer (owned by the Media) and its video stream.
    AVFormatContext* format = nullptr;
    int stream = -1;
    AVCodecContext* codec = nullptr;
    AVBufferRef* device = nullptr;
    AVPacket* packet = nullptr;
    AVFrame* frame = nullptr;
    // The demuxer gave its last packet and the decoder was told so.
    bool flushing = false;

    ~Decoder()
    {
        av_frame_free(&frame);
        av_packet_free(&packet);
        avcodec_free_context(&codec);
        av_buffer_unref(&device);
    }
};

AVPixelFormat pickCuda(AVCodecContext* /*codec*/, AVPixelFormat const* formats)
{
    for (auto const* f = formats; *f != AV_PIX_FMT_NONE; ++f)
    {
        if (*f == AV_PIX_FMT_CUDA)
        {
            return *f;
        }
    }
    return AV_PIX_FMT_NONE;
}

// The CUDA runtime's context of this thread: FFmpeg decodes into it, so its pictures are valid
// device pointers for the engine's kernels.
CUcontext currentContext()
{
    void* getCurrent = nullptr;
    cudaDriverEntryPointQueryResult found{};
    CUcontext context = nullptr;
    if (cudaGetDriverEntryPointByVersion("cuCtxGetCurrent", &getCurrent, 12000, cudaEnableDefault, &found) != cudaSuccess || getCurrent == nullptr ||
        reinterpret_cast<CUresult (*)(CUcontext*)>(getCurrent)(&context) != CUDA_SUCCESS)
    {
        return nullptr;
    }
    return context;
}

// The library's clip mezzanine: 4:4:4, 10 bits (12 and 8 decode too).
bool mezzanineFormat(AVPixelFormat format)
{
    return format == AV_PIX_FMT_YUV444P10LE || format == AV_PIX_FMT_YUV444P12LE || format == AV_PIX_FMT_YUV444P;
}

// A stinger's alpha: the luma of HEVC 4:2:0, 10 bits (8 decode too).
bool alphaFormat(AVPixelFormat format)
{
    return format == AV_PIX_FMT_YUV420P10LE || format == AV_PIX_FMT_YUV420P;
}

// Opens and probes an HEVC file: its demuxer and video stream, or an error.
std::string openHevc(std::string const& path, char const* what, AVFormatContext*& format, int& stream)
{
    AVDictionary* options = nullptr;
    av_dict_set(&options, "protocol_whitelist", "file", 0);
    int rc = avformat_open_input(&format, path.c_str(), nullptr, &options);
    av_dict_free(&options);
    if (rc < 0)
    {
        return std::string("cannot open ") + what + " " + path + ": " + avError(rc);
    }
    if ((rc = avformat_find_stream_info(format, nullptr)) < 0)
    {
        return std::string("cannot read ") + what + " " + path + ": " + avError(rc);
    }
    stream = av_find_best_stream(format, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
    if (stream < 0)
    {
        return std::string(what) + " " + path + " has no picture";
    }
    return {};
}
} // namespace

void copyPlayerAudio(PlayerFrame const& frame, std::vector<float>& dst, std::size_t n, std::size_t ch)
{
    dst.assign(ch * n, 0.f);
    std::size_t const samples = std::min(frame.samples, n);
    for (std::size_t c = 0; c < std::min(ch, frame.channels); ++c)
    {
        std::memcpy(dst.data() + c * n, frame.audio.data() + c * frame.samples, samples * sizeof(float));
    }
}

std::uint64_t clipSampleAt(Rate rate, long long frame)
{
    if (frame <= 0 || rate.num <= 0)
    {
        return 0;
    }
    return static_cast<std::uint64_t>(frame) * kAudioRate * static_cast<std::uint64_t>(rate.den) / static_cast<std::uint64_t>(rate.num);
}

struct ClipPlayer::Impl
{
    PlayerSetup setup;
    int rowBytes = 0;
    Playout playout;

    mutable std::mutex mu;
    std::condition_variable wake;
    std::shared_ptr<Media> media;
    // Decoded frames in play order; the still; what the last grain showed.
    std::deque<std::shared_ptr<Decoded>> queue;
    std::shared_ptr<Decoded> still;
    std::shared_ptr<Decoded> shown;
    std::string error;
    std::uint64_t late = 0;
    std::uint64_t decoded = 0;
    double decodeMs = 0.0;

    std::vector<std::shared_ptr<Picture>> pool;
    std::atomic<bool> run{false};
    std::thread thread;

    void fail(Media& m, std::string const& what)
    {
        m.failed = true;
        logError("player_failed", {{"player", setup.number}, {"file", m.id.path}, {"error", what}});
        std::lock_guard lock{mu};
        error = what;
    }

    std::string openMedia(PlayerCommand const& c, FileId const& id, std::shared_ptr<Media>& out) const
    {
        auto m = std::make_shared<Media>();
        m->kind = c.kind;
        m->id = id;
        if (c.kind == "still")
        {
            std::ifstream file(c.path, std::ios::binary);
            m->still.resize(setup.frameBytes);
            if (!file.read(reinterpret_cast<char*>(m->still.data()), static_cast<std::streamsize>(m->still.size())) ||
                static_cast<std::size_t>(id.size) != setup.frameBytes)
            {
                return "still " + c.path + " is not one v210 picture of " + std::to_string(setup.width) + "x" + std::to_string(setup.height);
            }
            m->frames = 1;
            out = m;
            return {};
        }
        if (c.kind != "clip" && c.kind != "stinger")
        {
            return "unknown media kind '" + c.kind + "' (clip, still or stinger)";
        }
        if (auto const failed = openHevc(c.path, "clip", m->format, m->stream); !failed.empty())
        {
            return failed;
        }
        AVStream const* stream = m->format->streams[m->stream];
        auto const* par = stream->codecpar;
        auto const pixel = static_cast<AVPixelFormat>(par->format);
        if (par->codec_id != AV_CODEC_ID_HEVC || !mezzanineFormat(pixel))
        {
            char const* name = av_get_pix_fmt_name(pixel);
            return "clip " + c.path + " is " + avcodec_get_name(par->codec_id) + " " + (name != nullptr ? name : "?") +
                   ", not the library's HEVC 4:4:4 mezzanine: convert it again";
        }
        if (par->width != setup.width || par->height != setup.height)
        {
            return "clip " + c.path + " is " + std::to_string(par->width) + "x" + std::to_string(par->height) + ", the mixer runs " +
                   std::to_string(setup.width) + "x" + std::to_string(setup.height) + ": convert it again";
        }
        m->frames = c.frames;
        if (m->frames <= 0)
        {
            // From the duration (Matroska has no frame count).
            double seconds = 0.0;
            if (stream->duration != AV_NOPTS_VALUE)
            {
                seconds = static_cast<double>(stream->duration) * av_q2d(stream->time_base);
            }
            else if (m->format->duration != AV_NOPTS_VALUE)
            {
                seconds = static_cast<double>(m->format->duration) / AV_TIME_BASE;
            }
            m->frames = static_cast<long long>(seconds * static_cast<double>(setup.rate.num) / static_cast<double>(setup.rate.den) + 0.5);
        }
        if (m->frames <= 0)
        {
            return "clip " + c.path + " has no frames";
        }
        if (!c.alpha.empty())
        {
            if (auto const failed = openHevc(c.alpha, "alpha", m->alphaFormat, m->alphaStream); !failed.empty())
            {
                return failed;
            }
            auto const* alpha = m->alphaFormat->streams[m->alphaStream]->codecpar;
            if (alpha->codec_id != AV_CODEC_ID_HEVC || !alphaFormat(static_cast<AVPixelFormat>(alpha->format)) || alpha->width != setup.width ||
                alpha->height != setup.height)
            {
                return "alpha " + c.alpha + " is not the library's HEVC 4:2:0 alpha at " + std::to_string(setup.width) + "x" +
                       std::to_string(setup.height) + ": convert it again";
            }
        }
        if (!c.audio.empty())
        {
            m->audioFd = ::open(c.audio.c_str(), O_RDONLY | O_CLOEXEC);
            if (m->audioFd < 0)
            {
                return "cannot open the sound of clip " + c.path + ": " + c.audio;
            }
            m->audioChannels = std::clamp(c.channels > 0 ? c.channels : 2, 1, kMaxChannels);
        }
        out = m;
        return {};
    }

    // A pool picture that nothing holds any more.
    std::shared_ptr<Picture> freePicture()
    {
        for (auto const& picture : pool)
        {
            if (picture.use_count() == 1)
            {
                return picture;
            }
        }
        return nullptr;
    }

    bool openDecoder(Media& m, Decoder& d, AVFormatContext* format, int index, cudaStream_t stream)
    {
        d.format = format;
        d.stream = index;
        AVStream const* st = format->streams[index];
        AVCodec const* codec = avcodec_find_decoder(st->codecpar->codec_id);
        CUcontext const context = currentContext();
        if (codec == nullptr || context == nullptr)
        {
            fail(m, codec == nullptr ? "no HEVC decoder in this FFmpeg" : "no current CUDA context");
            return false;
        }
        d.device = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_CUDA);
        auto* hw = static_cast<AVCUDADeviceContext*>(reinterpret_cast<AVHWDeviceContext*>(d.device->data)->hwctx);
        hw->cuda_ctx = context;
        // FFmpeg copies NVDEC's pictures on this stream: the conversion that follows on it sees
        // them complete.
        hw->stream = stream;
        int rc = av_hwdevice_ctx_init(d.device);
        if (rc < 0)
        {
            fail(m, "cuda device: " + avError(rc));
            return false;
        }
        d.codec = avcodec_alloc_context3(codec);
        avcodec_parameters_to_context(d.codec, st->codecpar);
        d.codec->pkt_timebase = st->time_base;
        d.codec->hw_device_ctx = av_buffer_ref(d.device);
        d.codec->get_format = pickCuda;
        d.codec->thread_count = 1;
        if ((rc = avcodec_open2(d.codec, codec, nullptr)) < 0)
        {
            fail(m, "HEVC decoder: " + avError(rc));
            return false;
        }
        d.packet = av_packet_alloc();
        d.frame = av_frame_alloc();
        return true;
    }

    void seek(Decoder& d, long long frame)
    {
        AVStream const* st = d.format->streams[d.stream];
        std::int64_t const start = st->start_time != AV_NOPTS_VALUE ? st->start_time : 0;
        std::int64_t const ts = start + av_rescale_q(frame, AVRational{static_cast<int>(setup.rate.den), static_cast<int>(setup.rate.num)}, st->time_base);
        av_seek_frame(d.format, d.stream, ts, AVSEEK_FLAG_BACKWARD);
        avcodec_flush_buffers(d.codec);
        d.flushing = false;
    }

    // The next decoded picture in d.frame (decode order = play order: the mezzanine has no
    // B-frames). Returns 0, AVERROR_EOF at the end, or an error.
    int decodeNext(Decoder& d)
    {
        while (true)
        {
            int rc = avcodec_receive_frame(d.codec, d.frame);
            if (rc != AVERROR(EAGAIN))
            {
                return rc;
            }
            if (d.flushing)
            {
                return AVERROR_EOF;
            }
            rc = av_read_frame(d.format, d.packet);
            if (rc == AVERROR_EOF)
            {
                d.flushing = true;
                avcodec_send_packet(d.codec, nullptr);
                continue;
            }
            if (rc < 0)
            {
                return rc;
            }
            if (d.packet->stream_index == d.stream)
            {
                rc = avcodec_send_packet(d.codec, d.packet);
            }
            av_packet_unref(d.packet);
            if (rc < 0 && rc != AVERROR(EAGAIN))
            {
                return rc;
            }
        }
    }

    long long frameNumber(Decoder const& d) const
    {
        AVFrame const* frame = d.frame;
        AVStream const* st = d.format->streams[d.stream];
        std::int64_t const start = st->start_time != AV_NOPTS_VALUE ? st->start_time : 0;
        std::int64_t const pts = frame->best_effort_timestamp != AV_NOPTS_VALUE ? frame->best_effort_timestamp : frame->pts;
        if (pts == AV_NOPTS_VALUE)
        {
            return -1;
        }
        return av_rescale_q_rnd(pts - start, st->time_base, AVRational{static_cast<int>(setup.rate.den), static_cast<int>(setup.rate.num)},
            AV_ROUND_NEAR_INF);
    }

    // The sound of clip frame `frame`, channel-major in the PGM's channels (a mono clip on all).
    void readAudio(Media const& m, long long frame, Decoded& out) const
    {
        if (m.audioFd < 0)
        {
            return;
        }
        auto const first = clipSampleAt(setup.rate, frame);
        auto const n = static_cast<std::size_t>(clipSampleAt(setup.rate, frame + 1) - first);
        auto const fileCh = static_cast<std::size_t>(m.audioChannels);
        std::vector<float> interleaved(n * fileCh, 0.f);
        auto const bytes = static_cast<ssize_t>(interleaved.size() * sizeof(float));
        ssize_t const got = pread(m.audioFd, interleaved.data(), static_cast<std::size_t>(bytes), static_cast<off_t>(first * fileCh * sizeof(float)));
        if (got <= 0)
        {
            return;
        }
        std::size_t const ch = setup.channels;
        out.audio.assign(ch * n, 0.f);
        out.samples = n;
        for (std::size_t c = 0; c < ch; ++c)
        {
            std::size_t const from = fileCh == 1 ? 0 : c;
            if (from >= fileCh)
            {
                continue;
            }
            for (std::size_t i = 0; i < n; ++i)
            {
                out.audio[c * n + i] = interleaved[i * fileCh + from];
            }
        }
    }

    // Decodes clip frame `want` (from where the decoder is) into `picture`. Returns the frame it
    // made (`want`, or a later one when the file skips), -1 at the end of the file, -2 on error.
    long long produce(Media& m, Decoder& d, long long want, Picture& picture, cudaStream_t stream)
    {
        while (true)
        {
            int const rc = decodeNext(d);
            if (rc == AVERROR_EOF)
            {
                return -1;
            }
            if (rc < 0)
            {
                fail(m, "decode: " + avError(rc));
                return -2;
            }
            long long const number = frameNumber(d);
            if (number >= 0 && number < want)
            {
                av_frame_unref(d.frame);
                continue;
            }
            auto const* frames = d.frame->hw_frames_ctx != nullptr ? reinterpret_cast<AVHWFramesContext const*>(d.frame->hw_frames_ctx->data) : nullptr;
            AVPixelFormat const sw = frames != nullptr ? frames->sw_format : AV_PIX_FMT_NONE;
            if (d.frame->format != AV_PIX_FMT_CUDA || (sw != AV_PIX_FMT_YUV444P16 && sw != AV_PIX_FMT_YUV444P))
            {
                char const* name = av_get_pix_fmt_name(sw);
                fail(m, std::string("NVDEC gave ") + (name != nullptr ? name : "no GPU picture") + ", 4:4:4 expected");
                av_frame_unref(d.frame);
                return -2;
            }
            if (d.frame->width != setup.width || d.frame->height != setup.height)
            {
                fail(m, "decoded picture has the wrong size");
                av_frame_unref(d.frame);
                return -2;
            }
            gpu::from444(stream, d.frame->data[0], d.frame->data[1], d.frame->data[2], d.frame->linesize[0], sw == AV_PIX_FMT_YUV444P16, setup.width,
                setup.height, picture.v210, rowBytes);
            // The surface goes back to FFmpeg's pool; its next copy is queued on the same stream,
            // after this conversion.
            av_frame_unref(d.frame);
            return number >= 0 ? number : want;
        }
    }

    // A stinger frame's key plane (after the picture): the luma of the alpha stream's frame
    // `number`, or a full key without an alpha stream (or past its end). False on an error.
    bool keyPlane(Media& m, Decoder* alpha, long long number, Picture& picture, cudaStream_t stream)
    {
        std::uint8_t* key = picture.v210 + setup.frameBytes;
        while (alpha != nullptr)
        {
            int const rc = decodeNext(*alpha);
            if (rc == AVERROR_EOF)
            {
                break;
            }
            if (rc < 0)
            {
                fail(m, "alpha decode: " + avError(rc));
                return false;
            }
            long long const n = frameNumber(*alpha);
            if (n >= 0 && n < number)
            {
                av_frame_unref(alpha->frame);
                continue;
            }
            auto const* frames = alpha->frame->hw_frames_ctx != nullptr ? reinterpret_cast<AVHWFramesContext const*>(alpha->frame->hw_frames_ctx->data) : nullptr;
            AVPixelFormat const sw = frames != nullptr ? frames->sw_format : AV_PIX_FMT_NONE;
            bool const deep = sw == AV_PIX_FMT_P010LE || sw == AV_PIX_FMT_P016LE;
            if (alpha->frame->format != AV_PIX_FMT_CUDA || (!deep && sw != AV_PIX_FMT_NV12) || alpha->frame->width != setup.width ||
                alpha->frame->height != setup.height)
            {
                fail(m, "NVDEC gave no 4:2:0 alpha picture of the raster");
                av_frame_unref(alpha->frame);
                return false;
            }
            gpu::alphaToKey(stream, alpha->frame->data[0], alpha->frame->linesize[0], deep, setup.width, setup.height, key, setup.keyRowBytes);
            av_frame_unref(alpha->frame);
            return true;
        }
        gpu::alphaToKey(stream, nullptr, 0, false, setup.width, setup.height, key, setup.keyRowBytes);
        return true;
    }

    [[nodiscard]] std::size_t pictureBytes() const
    {
        return setup.frameBytes + static_cast<std::size_t>(setup.keyRowBytes) * static_cast<std::size_t>(setup.height);
    }

    void allocPool()
    {
        for (std::size_t k = 0; k < kPool; ++k)
        {
            auto picture = std::shared_ptr<Picture>(new Picture, [](Picture* p) {
                cudaFree(p->v210);
                if (p->ready != nullptr)
                {
                    cudaEventDestroy(p->ready);
                }
                delete p;
            });
            if (cudaMalloc(reinterpret_cast<void**>(&picture->v210), pictureBytes()) != cudaSuccess ||
                cudaEventCreateWithFlags(&picture->ready, cudaEventDisableTiming) != cudaSuccess)
            {
                logError("player_pool", {{"player", setup.number}, {"error", "out of GPU memory"}});
                break;
            }
            pool.push_back(std::move(picture));
        }
    }

    void decodeMain()
    {
        pthread_setname_np(pthread_self(), ((setup.keyRowBytes > 0 ? "fx-st" : "fx-cp") + std::to_string(setup.number)).c_str());
        cudaSetDevice(setup.gpu);
        cudaStream_t stream = nullptr;
        cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking);
        allocPool();
        std::shared_ptr<Media> loaded;
        std::unique_ptr<Decoder> decoder;
        std::unique_ptr<Decoder> alpha;
        std::uint64_t generation = 0;
        long long nextSeq = 0;
        long long nextFrame = 0;
        bool needSeek = true;
        bool atEnd = false;
        while (run.load())
        {
            std::shared_ptr<Media> want;
            Playout::Cursor cursor;
            bool full = false;
            {
                std::unique_lock lock{mu};
                want = media;
                cursor = playout.cursor();
                full = queue.size() >= kAhead;
                bool const idle = want == loaded && (want == nullptr || want->failed || want->kind == "still" || cursor.empty ||
                                                        (cursor.generation == generation && (full || (atEnd && !cursor.loop))));
                if (idle)
                {
                    wake.wait_for(lock, std::chrono::milliseconds(20));
                    continue;
                }
            }
            if (want != loaded)
            {
                decoder.reset();
                alpha.reset();
                loaded = want;
                generation = 0;
                if (loaded != nullptr && loaded->kind == "still")
                {
                    auto picture = freePicture();
                    if (picture == nullptr)
                    {
                        loaded.reset();
                        std::this_thread::sleep_for(std::chrono::milliseconds(5));
                        continue;
                    }
                    cudaMemcpyAsync(picture->v210, loaded->still.data(), loaded->still.size(), cudaMemcpyHostToDevice, stream);
                    cudaEventRecord(picture->ready, stream);
                    cudaStreamSynchronize(stream);
                    auto decodedStill = std::make_shared<Decoded>();
                    decodedStill->picture = std::move(picture);
                    std::lock_guard lock{mu};
                    if (media == loaded)
                    {
                        still = std::move(decodedStill);
                        loaded->still = {};
                    }
                    continue;
                }
            }
            if (loaded == nullptr || loaded->kind == "still" || loaded->failed || cursor.empty)
            {
                continue;
            }
            if (cursor.generation != generation)
            {
                generation = cursor.generation;
                nextSeq = 0;
                nextFrame = cursor.start;
                needSeek = true;
                atEnd = false;
                std::lock_guard lock{mu};
                while (!queue.empty() && queue.front()->generation < generation)
                {
                    queue.pop_front();
                }
            }
            if (full)
            {
                continue;
            }
            if (nextFrame >= cursor.frames || atEnd)
            {
                if (!cursor.loop)
                {
                    atEnd = true;
                    continue;
                }
                nextFrame = 0;
                needSeek = true;
                atEnd = false;
            }
            if (decoder == nullptr)
            {
                decoder = std::make_unique<Decoder>();
                if (loaded->alphaFormat != nullptr)
                {
                    alpha = std::make_unique<Decoder>();
                }
                if (!openDecoder(*loaded, *decoder, loaded->format, loaded->stream, stream) ||
                    (alpha != nullptr && !openDecoder(*loaded, *alpha, loaded->alphaFormat, loaded->alphaStream, stream)))
                {
                    decoder.reset();
                    alpha.reset();
                    continue;
                }
                needSeek = true;
            }
            auto picture = freePicture();
            if (picture == nullptr)
            {
                std::unique_lock lock{mu};
                wake.wait_for(lock, std::chrono::milliseconds(2));
                continue;
            }
            if (needSeek)
            {
                seek(*decoder, nextFrame);
                if (alpha != nullptr)
                {
                    seek(*alpha, nextFrame);
                }
                needSeek = false;
            }
            auto const t0 = Clock::now();
            long long const made = produce(*loaded, *decoder, nextFrame, *picture, stream);
            if (made == -1)
            {
                // The file ends before the frame count: wrap (loop) or hold the last frame.
                atEnd = true;
                continue;
            }
            if (made < 0 || (setup.keyRowBytes > 0 && !keyPlane(*loaded, alpha.get(), made, *picture, stream)))
            {
                continue;
            }
            cudaEventRecord(picture->ready, stream);
            auto out = std::make_shared<Decoded>();
            out->picture = std::move(picture);
            out->generation = generation;
            out->seq = nextSeq + (made - nextFrame);
            out->frame = made;
            readAudio(*loaded, made, *out);
            cudaEventSynchronize(out->picture->ready);
            double const ms = std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
            nextSeq = out->seq + 1;
            nextFrame = made + 1;
            std::lock_guard lock{mu};
            ++decoded;
            decodeMs = decodeMs == 0.0 ? ms : decodeMs * 0.95 + ms * 0.05;
            if (playout.cursor().generation == generation && media == loaded)
            {
                queue.push_back(std::move(out));
            }
        }
        decoder.reset();
        alpha.reset();
        loaded.reset();
        cudaStreamSynchronize(stream);
        cudaStreamDestroy(stream);
    }
};

ClipPlayer::ClipPlayer(PlayerSetup setup)
    : impl_(std::make_unique<Impl>())
{
    impl_->setup = std::move(setup);
    impl_->rowBytes = v210RowBytes(impl_->setup.width);
}

ClipPlayer::~ClipPlayer()
{
    stop();
}

void ClipPlayer::start()
{
    if (impl_->run.exchange(true))
    {
        return;
    }
    impl_->thread = std::thread([this] { impl_->decodeMain(); });
}

void ClipPlayer::stop()
{
    impl_->run = false;
    impl_->wake.notify_all();
    if (impl_->thread.joinable())
    {
        impl_->thread.join();
    }
    std::lock_guard lock{impl_->mu};
    impl_->queue.clear();
    impl_->still.reset();
    impl_->shown.reset();
    impl_->media.reset();
    impl_->pool.clear();
}

std::shared_ptr<PlayerFrame> ClipPlayer::frameFor(std::uint64_t index, bool onAir)
{
    auto& s = *impl_;
    if (s.setup.keyRowBytes > 0 && !onAir && (s.playout.state() == Playout::State::Playing || s.playout.state() == Playout::State::Ended))
    {
        s.playout.cue(0);
    }
    auto const step = s.playout.advance(index, onAir);
    auto out = std::make_shared<PlayerFrame>();
    out->index = index;
    std::shared_ptr<Decoded> show;
    bool isStill = false;
    {
        std::lock_guard lock{s.mu};
        if (step.frame < 0)
        {
            s.shown.reset();
            return out;
        }
        if (s.still != nullptr)
        {
            show = s.still;
            isStill = true;
        }
        else
        {
            // Frames before this step were shown or skipped: they go.
            while (!s.queue.empty() &&
                   (s.queue.front()->generation < step.generation || (s.queue.front()->generation == step.generation && s.queue.front()->seq < step.seq)))
            {
                s.queue.pop_front();
            }
            if (!s.queue.empty() && s.queue.front()->generation == step.generation && s.queue.front()->seq == step.seq)
            {
                show = s.queue.front();
            }
            else
            {
                // The decoder is late (or still seeking after a cue): the last picture again.
                if (step.playing)
                {
                    ++s.late;
                }
                show = s.shown;
            }
        }
        s.shown = show;
    }
    s.wake.notify_one();
    if (show == nullptr || show->picture == nullptr)
    {
        return out;
    }
    out->v210 = show->picture->v210;
    out->ready = show->picture->ready;
    if (step.playing && !isStill && show->generation == step.generation && show->seq == step.seq && show->samples > 0)
    {
        out->audio = show->audio;
        out->samples = show->samples;
        out->channels = s.setup.channels;
    }
    out->hold = std::move(show);
    return out;
}

std::string ClipPlayer::command(PlayerCommand const& c)
{
    auto& s = *impl_;
    std::string const name = "clip player " + std::to_string(s.setup.number);
    switch (c.action)
    {
    case PlayerCommand::Action::Load: {
        if (c.path.empty())
        {
            {
                std::lock_guard lock{s.mu};
                s.media.reset();
                s.still.reset();
                s.queue.clear();
                s.error.clear();
                s.playout.unload();
            }
            s.wake.notify_all();
            return {};
        }
        FileId id;
        id.path = c.path;
        id.audio = c.audio;
        struct stat info{};
        if (::stat(c.path.c_str(), &info) != 0)
        {
            return name + ": no file " + c.path;
        }
        id.dev = info.st_dev;
        id.ino = info.st_ino;
        id.size = static_cast<long long>(info.st_size);
        id.mtimeNs = static_cast<long long>(info.st_mtim.tv_sec) * 1'000'000'000LL + info.st_mtim.tv_nsec;
        {
            // The same file again (the control plane repeats loads): only the options change.
            std::lock_guard lock{s.mu};
            if (s.media != nullptr && !s.media->failed && s.media->kind == c.kind && s.media->id == id)
            {
                s.playout.setLoop(c.loop);
                s.playout.setPlayOnCut(c.playOnCut);
                return {};
            }
        }
        std::shared_ptr<Media> media;
        auto const failed = s.openMedia(c, id, media);
        if (!failed.empty())
        {
            logWarn("player_load_failed", {{"player", s.setup.number}, {"error", failed}});
            return name + ": " + failed;
        }
        {
            std::lock_guard lock{s.mu};
            s.media = media;
            s.still.reset();
            s.queue.clear();
            s.error.clear();
            s.playout.load(media->frames, c.loop, c.playOnCut);
        }
        logInfo("player_loaded", {{"player", s.setup.number}, {"kind", c.kind}, {"file", c.path}, {"frames", media->frames}});
        s.wake.notify_all();
        return {};
    }
    case PlayerCommand::Action::Cue:
        if (s.playout.state() == Playout::State::Empty)
        {
            return name + " has no media";
        }
        s.playout.cue(c.frame);
        break;
    case PlayerCommand::Action::Play:
        if (s.playout.state() == Playout::State::Empty)
        {
            return name + " has no media";
        }
        s.playout.play();
        break;
    case PlayerCommand::Action::Pause:
        s.playout.pause();
        break;
    case PlayerCommand::Action::Stop:
        s.playout.stop();
        break;
    }
    s.wake.notify_all();
    return {};
}

bool ClipPlayer::ready() const
{
    auto const& s = *impl_;
    std::lock_guard lock{s.mu};
    auto const cursor = s.playout.cursor();
    return s.media != nullptr && !s.media->failed && s.playout.state() == Playout::State::Cued && cursor.start == 0 && !s.queue.empty() &&
           s.queue.front()->generation == cursor.generation && s.queue.front()->seq == 0;
}

long long ClipPlayer::frames() const
{
    auto const& s = *impl_;
    std::lock_guard lock{s.mu};
    return s.media != nullptr && !s.media->failed ? s.playout.frames() : 0;
}

std::string ClipPlayer::statusJson() const
{
    auto const& s = *impl_;
    std::lock_guard lock{s.mu};
    nlohmann::json out{{"player", s.setup.number}, {"label", s.setup.label}, {"state", stateName(s.playout.state())}, {"frame", s.playout.position()},
        {"frames", s.playout.frames()}, {"loop", s.playout.loop()}, {"play_on_cut", s.playout.playOnCut()}, {"ahead", s.queue.size()},
        {"late", s.late}, {"decoded", s.decoded}, {"decode_ms", s.decodeMs}};
    auto const end = s.playout.endIndex();
    out["end_index"] = end ? nlohmann::json(*end) : nlohmann::json(nullptr);
    out["kind"] = s.media != nullptr ? s.media->kind : "";
    out["file"] = s.media != nullptr ? s.media->id.path : "";
    if (s.setup.keyRowBytes > 0)
    {
        out["alpha"] = s.media != nullptr && s.media->alphaFormat != nullptr;
    }
    out["error"] = s.error;
    return out.dump();
}

std::size_t ClipPlayer::deviceBytes() const
{
    return kPool * impl_->pictureBytes();
}
} // namespace fxeng
