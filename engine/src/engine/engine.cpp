#include "engine/engine.hpp"

#include "core/audio.hpp"
#include "core/audiomix.hpp"
#include "core/meter.hpp"
#include "core/mosaic_map.hpp"
#include "core/pixel.hpp"
#include "core/tai.hpp"
#include "engine/api.hpp"
#include "engine/encoder.hpp"
#include "engine/gpu.hpp"
#include "engine/hostmem.hpp"
#include "engine/log.hpp"
#include "engine/mediamtx.hpp"
#include "engine/player.hpp"
#include "engine/stats.hpp"

#include <mxl/flow.h>
#include <mxl/mxl.h>
#include <mxl/time.h>

#include <cuda_runtime_api.h>
#include <nlohmann/json.hpp>
#include <pthread.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <future>
#include <map>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <vector>

namespace fxeng
{
namespace
{
using json = nlohmann::json;

// How long an input thread blocks for its next grain; bounds how fast stop() or a new route
// reaches it.
constexpr std::uint64_t kGrainWaitNs = 40'000'000;
// Output grains in flight between the render and the writer thread.
constexpr int kSlots = 3;
constexpr int kMosaicSlots = 2;
// Samples per statistics window (5 s at 50p).
constexpr int kWindow = 250;
// Windows in a statistic's `recent` maximum (1 min at 50p): what the latency recommendation uses.
constexpr std::size_t kRecentWindows = 12;
// Output grains after a route (or the start) during which a missing grain or missing audio is a
// start-up window, not counted as missing.
constexpr std::uint64_t kGraceGrains = 4;
// After a (re)route the arrival and audio lag statistics wait this long past the start-up window:
// a mirror or a writer settling then is no steady-state lag (small saw one 546 ms audio peak at a re-route).
constexpr std::uint64_t kLagSettleNs = 2'000'000'000;
// Frame grabs: picked per output grain, in flight (page-locked buffers), kept for GET /grab/{id}.
constexpr std::size_t kGrabsPerGrain = 4;
constexpr std::size_t kGrabBuffers = 8;
constexpr std::size_t kGrabHistory = 32;
// Output grains waiting for fx-audio; when it is this far behind, a grain's job is dropped (the
// next one covers the gap, as a skipped video grain).
constexpr std::size_t kAudioJobs = 8;

// A sample later than this is no arrival but a grain the clock cannot place (index 0 of a mirror
// flow that was just created): it would hold the peak forever (small: 1.79e12 ms on a new mirror).
constexpr double kMaxArrivalMs = 10'000.0;

void check(cudaError_t err, char const* what)
{
    if (err != cudaSuccess)
    {
        throw std::runtime_error(std::string(what) + ": " + cudaGetErrorString(err));
    }
}

double round3(double v)
{
    return std::round(v * 1000.0) / 1000.0;
}

double round2(double v)
{
    return std::round(v * 100.0) / 100.0;
}

// Average and maximum over windows of `size` samples, the maximum of the last kRecentWindows
// windows (`recent`) and the peak since the last reset.
// value() is the last full window, or the running one until the first is full.
class Window
{
public:
    struct Value
    {
        double avg = 0;
        double max = 0;
    };

    void add(double v)
    {
        std::lock_guard lock{mu_};
        sum_ += v;
        max_ = std::max(max_, v);
        peak_ = std::max(peak_, v);
        ++count_;
        if (++n_ >= kWindow)
        {
            done_ = Value{sum_ / n_, max_};
            full_ = true;
            maxima_[next_] = max_;
            next_ = (next_ + 1) % kRecentWindows;
            sum_ = 0;
            max_ = 0;
            n_ = 0;
        }
    }

    void reset()
    {
        std::lock_guard lock{mu_};
        sum_ = 0;
        max_ = 0;
        peak_ = 0;
        n_ = 0;
        count_ = 0;
        full_ = false;
        done_ = Value{};
        maxima_ = {};
        next_ = 0;
    }

    [[nodiscard]] Value value() const
    {
        std::lock_guard lock{mu_};
        if (full_)
        {
            return done_;
        }
        return n_ > 0 ? Value{sum_ / n_, max_} : Value{};
    }

    [[nodiscard]] double peak() const
    {
        std::lock_guard lock{mu_};
        return peak_;
    }

    // The maximum of the running window and the last kRecentWindows full ones.
    [[nodiscard]] double recent() const
    {
        std::lock_guard lock{mu_};
        return std::max(max_, *std::max_element(maxima_.begin(), maxima_.end()));
    }

    [[nodiscard]] std::uint64_t count() const
    {
        std::lock_guard lock{mu_};
        return count_;
    }

    [[nodiscard]] json toJson(bool withPeak = false) const
    {
        auto const v = value();
        json out{{"avg", round3(v.avg)}, {"max", round3(v.max)}};
        if (withPeak)
        {
            out["recent"] = round3(recent());
            out["peak"] = round3(peak());
        }
        return out;
    }

private:
    mutable std::mutex mu_;
    double sum_ = 0;
    double max_ = 0;
    double peak_ = 0;
    int n_ = 0;
    std::uint64_t count_ = 0;
    Value done_;
    bool full_ = false;
    std::array<double, kRecentWindows> maxima_{};
    std::size_t next_ = 0;
};

// One grain of an input on the device, packed v210 as in MXL (v210a: the key plane follows).
struct DevFrame
{
    std::uint8_t* data = nullptr;
    std::uint64_t index = 0;
    // Recorded after the upload; the render stream waits on it.
    cudaEvent_t ready = nullptr;
};

// The device frames of one input, allocated at start and recycled: a frame is free again
// when the input's ring and every output grain that read it have let it go. The writer thread
// lets go only after the downloads, so no GPU work still reads a frame that is handed out.
class FramePool
{
public:
    FramePool() = default;
    FramePool(FramePool const&) = delete;
    FramePool& operator=(FramePool const&) = delete;

    ~FramePool()
    {
        for (auto* frame : all_)
        {
            cudaFree(frame->data);
            cudaEventDestroy(frame->ready);
            delete frame;
        }
    }

    // With a stream the allocation is stream-ordered (a key flow routed on air: the render stream
    // does not wait for it).
    void allocate(std::size_t bytes, int count, cudaStream_t stream = nullptr)
    {
        for (int i = 0; i < count; ++i)
        {
            auto* frame = new DevFrame;
            all_.push_back(frame);
            check(stream != nullptr ? cudaMallocAsync(reinterpret_cast<void**>(&frame->data), bytes, stream)
                                    : cudaMalloc(reinterpret_cast<void**>(&frame->data), bytes),
                "cudaMalloc input frame");
            check(cudaEventCreateWithFlags(&frame->ready, cudaEventDisableTiming), "cudaEventCreate");
            free_.push_back(frame);
        }
        if (stream != nullptr)
        {
            check(cudaStreamSynchronize(stream), "input frames");
        }
    }

    // nullptr when every frame is in use.
    std::shared_ptr<DevFrame> acquire()
    {
        std::lock_guard lock{mu_};
        if (free_.empty())
        {
            return nullptr;
        }
        DevFrame* frame = free_.back();
        free_.pop_back();
        return std::shared_ptr<DevFrame>(frame, [this](DevFrame* done) {
            std::lock_guard back{mu_};
            free_.push_back(done);
        });
    }

private:
    std::mutex mu_;
    std::vector<DevFrame*> free_;
    std::vector<DevFrame*> all_;
};

// A live input (video + audio) or a key input (v210a video only). FlowXer routes it at run
// time; its thread re-opens the reader on a new route.
struct Input
{
    int number = 1;
    // "in3" or "key1".
    std::string name;
    std::string label;
    bool key = false;
    // Device bytes of one grain (v210, or v210 + key plane).
    std::size_t bytes = 0;
    FramePool pool;
    // Its frames exist (a key flow gets them on its first route).
    bool allocated = false;
    mutable std::mutex mu;
    std::condition_variable routed;
    Route video;
    Route audio;
    std::atomic<std::uint64_t> videoGen{0};
    std::atomic<std::uint64_t> audioGen{0};
    // The newest grains, oldest first.
    std::deque<std::shared_ptr<DevFrame>> recent;
    std::string state = "not_routed";
    // Output grains up to this one are a start-up window (route or engine start).
    std::atomic<std::uint64_t> graceUntil{0};
    std::atomic<std::uint64_t> audioGraceUntil{0};
    std::atomic<std::uint64_t> grains{0};
    std::atomic<std::uint64_t> resyncs{0};
    std::atomic<std::uint64_t> catchups{0};
    std::atomic<std::uint64_t> invalid{0};
    std::atomic<std::uint64_t> reopens{0};
    std::atomic<std::uint64_t> exhausted{0};
    std::atomic<std::uint64_t> unpinned{0};
    // Output grains that held this input's previous grain (grain i - L not there in time).
    std::atomic<std::uint64_t> late{0};
    // Output grains that showed black (no grain within the hold time).
    std::atomic<std::uint64_t> missing{0};
    // Output grains without the grain during a start-up window (not counted as late or missing).
    std::atomic<std::uint64_t> startup{0};
    std::atomic<std::uint64_t> lastGrainAt{0};
    Window upload;
    // How late a grain arrives after its TAI time (ms): the latency must cover it.
    Window arrival;
    std::thread thread;
    // PGM audio, read by fx-writer: state (under mu), blocks read, blocks missing (silence).
    std::string audioState = "not_routed";
    std::atomic<std::uint64_t> audioBlocks{0};
    std::atomic<std::uint64_t> audioMissing{0};
    std::atomic<std::uint64_t> audioStartup{0};
    std::atomic<std::uint64_t> audioWaits{0};
    std::atomic<std::uint64_t> audioReopens{0};
    // How far the audio head is behind TAI (ms), sampled once per grain while the head moves.
    Window audioLag;
    // When the audio head last moved (TAI ns; 0: not yet): still for a second is no_signal.
    std::atomic<std::uint64_t> audioMovedAt{0};
    // When the audio head started to move again after standing still (TAI ns; 0: not yet).
    std::atomic<std::uint64_t> audioResumedAt{0};
    // P5: its audio's delay (48 kHz samples) and channel map (empty: 1:1), set by the control API
    // (under mu) and applied by fx-audio with a crossfade over one grain; what its delay line holds
    // (samples) and its flow's channels, published by fx-audio.
    std::uint32_t audioDelay = 0;
    std::vector<int> audioMap;
    std::atomic<std::uint64_t> audioHeld{0};
    std::atomic<std::uint32_t> audioChannels{0};
    // Whether some ME hears its audio in the last grain (AudioMix::heardInputs), published by
    // fx-audio: only then does its audio lag count for recommended_latency_grains.
    std::atomic<bool> audioHeard{false};

    [[nodiscard]] Route videoRoute() const
    {
        std::lock_guard lock{mu};
        return video;
    }
    [[nodiscard]] Route audioRoute() const
    {
        std::lock_guard lock{mu};
        return audio;
    }
};

// One MXL output flow of an ME: PGM video, PVW video or PGM audio.
struct Output
{
    int me = 1;
    bool preview = false;
    // Index of its picture in Slot::out (video).
    std::size_t outIndex = 0;
    std::string id;
    std::string label;
    std::string domain;
    mxlInstance instance = nullptr;
    mxlFlowWriter writer = nullptr;
    // The flow existed and was opened (MXL create-or-open), not created.
    bool existing = false;
    std::uint32_t grainCount = 0;
    std::uint32_t channels = 0;
    std::atomic<std::uint64_t> committed{0};
    std::atomic<std::uint64_t> failed{0};
};

// The audio state of fx-audio (only that thread touches it).
struct AudioRun
{
    struct Reader
    {
        mxlInstance instance = nullptr;
        std::string domain;
        mxlFlowReader reader = nullptr;
        std::size_t channels = 0;
        // The most samples one read may take (MXL's ring).
        std::size_t maxRead = 0;
        std::uint64_t gen = ~0ull;
        Route route;
        std::chrono::steady_clock::time_point retryAt{};
        std::uint64_t head = MXL_UNDEFINED_INDEX;
        // P5: the delay line (every channel of the flow), the first sample written since the reader
        // opened, whether its sound has faded in, the delay and map in use, and its sound of the
        // grain (delayed and mapped; `before`: the old reading during a change).
        DelayLine line;
        std::uint64_t firstIndex = MXL_UNDEFINED_INDEX;
        bool live = false;
        std::uint32_t delay = 0;
        std::vector<int> map;
        std::vector<float> sound;
        std::vector<float> before;
    };
    std::vector<Reader> readers;
    // Per ME its PGM of the grain (channel-major, channels × n).
    std::vector<std::vector<float>> me;
    std::size_t channels = 2;
    std::uint64_t last = 0;
    // P5 meters (docs/engine-p5.md §7): per live input (after its delay and map), clip player and
    // stinger player peak and RMS; per ME PGM (what goes to MXL) also true peak, overs, loudness.
    std::vector<Meter> inputMeters;
    std::vector<Meter> playerMeters;
    std::vector<Meter> stingerMeters;
    std::vector<Meter> meMeters;
};

// The meters of the last finished 100 ms block, published by fx-audio for the API and metrics.
struct MeterSnapshot
{
    bool valid = false;
    std::uint64_t block = 0;
    std::vector<MeterValues> inputs;
    std::vector<MeterValues> players;
    std::vector<MeterValues> stingers;
    std::vector<MeterValues> mes;
    // Per ME its strips (AudioMix::strips order): the gain in PGM and whether heard.
    std::vector<std::vector<StripRef>> stripRefs;
    std::vector<std::vector<StripState>> strips;
};

// One output grain for fx-audio, handed over by fx-writer right after its video commit: the MEs'
// plans and the sound of the clip and stinger players' frames (copied: the frames go with the slot).
struct AudioJob
{
    std::uint64_t index = 0;
    std::vector<MePlan> plans;
    // Channel-major (channels × n); empty: silence.
    std::vector<std::vector<float>> players;
    std::vector<std::vector<float>> stingers;
};

// A frame grab (docs/engine-p4.md §6): queued by the control thread, picked by fx-render for an
// output grain, copied into `host` (page-locked) by fx-writer with that grain's downloads, written
// to `path` by fx-grab. Its fields change under Impl::grabMu.
struct Grab
{
    int id = 0;
    Source source;
    bool preview = false;
    std::string path;
    // pending, copying, writing, done, failed.
    std::string state = "pending";
    std::string error;
    // The output grain, its TAI time, and a live input's own grain (i - L, or the one it held).
    std::uint64_t index = 0;
    std::uint64_t tai = 0;
    std::uint64_t sourceIndex = 0;
    double ms = 0;
    std::chrono::steady_clock::time_point requested;
    std::uint8_t* host = nullptr;

    // input, player, pgm or pvw (the metrics' source label).
    [[nodiscard]] char const* kind() const
    {
        return source.kind == Source::Kind::Input ? "input" : source.kind == Source::Kind::Player ? "player" : preview ? "pvw" : "pgm";
    }
};

// A grab in an output slot: the packed v210 picture it copies (an input's or a clip player's
// device picture, which the slot holds until it is written, or the ME's packed output).
struct SlotGrab
{
    std::shared_ptr<Grab> grab;
    std::uint8_t const* device = nullptr;
    cudaEvent_t ready = nullptr;
};

// One output grain in flight: rendered by fx-render, downloaded and committed by fx-writer.
struct Slot
{
    // Packed v210 per ME output (ME m PGM at 2(m-1), PVW at 2(m-1)+1); every one is packed (the
    // mosaic reads them), only those with an MXL flow are downloaded.
    std::vector<std::uint8_t*> out;
    // The input grains this output grain read; released after the downloads.
    std::vector<std::shared_ptr<DevFrame>> held;
    // The clip player and stinger player frames of this grain (their audio goes into the PGM
    // audio).
    std::vector<std::shared_ptr<PlayerFrame>> players;
    std::vector<std::shared_ptr<PlayerFrame>> stingers;
    std::uint64_t index = 0;
    std::uint64_t inputIndex = 0;
    // Render stream: start, unpacked, one per ME (render order), keyed + packed, render done; per ME
    // (render order) the end of its boxes' resample (it starts with the ME).
    std::vector<cudaEvent_t> mark;
    std::vector<cudaEvent_t> boxed;
    cudaEvent_t downloadStart = nullptr;
    std::vector<cudaEvent_t> downloaded;
    // The ME plans of this grain (render order), for the audio.
    std::vector<MePlan> plans;
    // The frame grabs of this grain, and the event after their copies.
    std::vector<SlotGrab> grabs;
    cudaEvent_t grabbed = nullptr;
    std::atomic<bool> busy{false};
};

struct MosaicSlot
{
    std::uint8_t* nv12 = nullptr;
    cudaEvent_t ready = nullptr;
    std::int64_t pts = 0;
    std::atomic<bool> busy{false};
};

// A stable id for one of the engine's own names (output flows, domain): 128 bits of two
// FNV-1a hashes, formatted as a UUID version 8 (RFC 9562, custom).
std::string nameUuid(std::string const& name)
{
    auto const fnv = [&name](std::uint64_t hash) {
        for (unsigned char c : "flowxer-engine:" + name)
        {
            hash = (hash ^ c) * 1099511628211ull;
        }
        return hash;
    };
    std::uint64_t const hi = (fnv(14695981039346656037ull) & ~0xf000ull) | 0x8000ull;
    std::uint64_t const lo = (fnv(0x6a09e667f3bcc908ull) & ~(3ull << 62)) | (2ull << 62);
    char text[37];
    std::snprintf(text, sizeof(text), "%08x-%04x-%04x-%04x-%012llx", static_cast<unsigned>(hi >> 32), static_cast<unsigned>((hi >> 16) & 0xffff),
        static_cast<unsigned>(hi & 0xffff), static_cast<unsigned>(lo >> 48), static_cast<unsigned long long>(lo & 0xffffffffffffull));
    return text;
}

std::string videoFlowDefinition(std::string const& id, std::string const& label, std::string const& group, int width, int height, Rate rate)
{
    json def{{"id", id},
        {"format", "urn:x-nmos:format:video"},
        {"label", label},
        {"description", label},
        {"tags", {{"urn:x-nmos:tag:grouphint/v1.0", {group}}}},
        {"parents", json::array()},
        {"media_type", "video/v210"},
        {"grain_rate", {{"numerator", rate.num}, {"denominator", rate.den}}},
        {"frame_width", width},
        {"frame_height", height},
        {"interlace_mode", "progressive"},
        {"colorspace", "BT709"},
        {"components",
            {{{"name", "Y"}, {"width", width}, {"height", height}, {"bit_depth", 10}},
                {{"name", "Cb"}, {"width", width / 2}, {"height", height}, {"bit_depth", 10}},
                {{"name", "Cr"}, {"width", width / 2}, {"height", height}, {"bit_depth", 10}}}}};
    return def.dump();
}

std::string audioFlowDefinition(std::string const& id, std::string const& label, std::string const& group, int channels)
{
    json def{{"id", id},
        {"format", "urn:x-nmos:format:audio"},
        {"label", label},
        {"description", label},
        {"tags", {{"urn:x-nmos:tag:grouphint/v1.0", {group}}}},
        {"parents", json::array()},
        {"media_type", "audio/float32"},
        {"sample_rate", {{"numerator", kAudioRate}, {"denominator", 1}}},
        {"channel_count", channels},
        {"bit_depth", 32}};
    return def.dump();
}

void nameThread(std::string const& name)
{
    pthread_setname_np(pthread_self(), name.substr(0, 15).c_str());
}

json routeJson(Route const& route)
{
    return json{{"domain", route.domain}, {"flow", route.flow}};
}
} // namespace

struct Engine::Impl
{
    Config cfg;
    Mixer mixer;
    gpu::WorkFormat work = gpu::WorkFormat::Yuv16;
    int rowBytes = 0;
    std::size_t frameBytes = 0;
    // Bytes of a key grain: the v210 fill and the 10-bit key plane after it.
    std::size_t keyBytes = 0;
    int keyRowBytes = 0;
    int recentDepth = 4;
    int mosaicDivisor = 2;
    std::atomic<bool> run{false};
    bool started = false;

    std::vector<std::unique_ptr<Input>> inputs;
    // Key flows: one reader (and upload) per distinct routed flow, shared by the keyers routed to
    // it; its frames are allocated when it is first routed. keySlot: keyer k's flow, or -1.
    std::vector<std::unique_ptr<Input>> keys;
    mutable std::mutex keyMu;
    std::vector<int> keySlot;
    // A key flow's fill + key unpacked for its linear boxes (gpu::dmeUnpack, once per grain), by
    // slot: allocated by the control thread when a linear box on it is first enabled
    // (prepareBox), kept until the engine stops; under keyMu.
    std::vector<float*> keyPlanes;
    int keyPoolSize = 0;
    std::vector<std::unique_ptr<ClipPlayer>> players;
    // Stinger players: clip players with v210a pictures (alpha) and sound.
    std::vector<std::unique_ptr<ClipPlayer>> stingers;
    // Video outputs (PGM of every ME, PVW with pvwOutputs), audio outputs (ME m PGM at m-1).
    std::vector<std::unique_ptr<Output>> outputs;
    std::vector<std::unique_ptr<Output>> audioOutputs;
    // ME m's PVW video output, or nullptr.
    std::vector<Output*> pvwOf;
    std::map<std::string, mxlInstance> outInstances;
    std::uint8_t* black = nullptr;
    // Black with a full key (v210a): the Fade to Black layer, last over an ME's output.
    std::uint8_t* blackKey = nullptr;
    // Work pictures of the sources (inputs, then players), and of each ME's buses.
    std::vector<gpu::WorkBuffer> srcWork;
    std::vector<gpu::WorkBuffer> pgmWork;
    std::vector<gpu::WorkBuffer> pvwWork;
    // Black in the working format (the Black source, an empty clip player).
    gpu::WorkBuffer blackWork;
    // DME (yuv16): the weight table of the resample, and keyer k's box buffer at k − 1
    // (allocated by the control thread when its box is first enabled: prepareBox).
    gpu::DmeScratch dmeScratch;
    std::mutex boxMu;
    std::vector<std::int16_t*> boxBuffers;
    std::array<Slot, kSlots> slots;
    std::array<MosaicSlot, kMosaicSlots> mosaicSlots;
    std::vector<MosaicTile> tiles;
    cudaStream_t renderStream = nullptr;
    cudaStream_t downloadStream = nullptr;
    std::atomic<std::size_t> allocated{0};
    std::string gpuName;
    std::string pciBus;
    std::unique_ptr<MosaicEncoder> encoder;
    // The own MediaMTX (no shared one given); started and stopped by start()/stop(), polled
    // by the stats thread in between.
    std::unique_ptr<MediamtxProcess> mediamtx;
    std::atomic<bool> mediamtxRunning{false};

    std::mutex writeMu;
    std::condition_variable writeCv;
    std::deque<int> writeQueue;
    std::mutex encodeMu;
    std::condition_variable encodeCv;
    std::deque<int> encodeQueue;
    std::mutex statsMu;
    std::condition_variable statsCv;
    std::thread renderThread;
    std::thread writerThread;
    std::promise<void> writerWarm;
    std::thread encodeThread;
    std::thread statsThread;
    // Frame grabs: waiting for fx-render, copied and waiting for fx-grab, the last kGrabHistory;
    // the page-locked buffers (free, all).
    mutable std::mutex grabMu;
    std::condition_variable grabCv;
    std::deque<std::shared_ptr<Grab>> grabPending;
    std::deque<std::shared_ptr<Grab>> grabWrite;
    std::deque<std::shared_ptr<Grab>> grabRecent;
    std::vector<std::uint8_t*> grabFree;
    std::vector<std::uint8_t*> grabBuffers;
    int grabNext = 1;
    std::thread grabThread;
    std::atomic<std::uint64_t> grabsInput{0};
    std::atomic<std::uint64_t> grabsPlayer{0};
    std::atomic<std::uint64_t> grabsPgm{0};
    std::atomic<std::uint64_t> grabsPvw{0};
    std::atomic<std::uint64_t> grabsFailed{0};
    Window grabMs;

    std::atomic<std::uint64_t> rendered{0};
    std::atomic<std::uint64_t> committed{0};
    std::atomic<std::uint64_t> skipped{0};
    std::atomic<std::uint64_t> writerBusy{0};
    std::atomic<std::uint64_t> late{0};
    std::atomic<std::uint64_t> writeFailed{0};
    std::atomic<std::uint64_t> unpinned{0};
    std::atomic<std::uint64_t> mosaicFrames{0};
    std::atomic<std::uint64_t> mosaicDropped{0};
    std::atomic<std::uint64_t> lastIndex{0};
    std::atomic<std::uint64_t> startIndex{0};
    Window unpackMs;
    std::vector<std::unique_ptr<Window>> meMs;
    // Per ME: the resample of its boxes (the box layers are part of its compose).
    std::vector<std::unique_ptr<Window>> dmeMs;
    Window packMs;
    Window mosaicMs;
    Window downloadMs;
    Window renderMs;
    Window totalMs;
    Window spanMs;
    Window commitMs;
    Window lagGrains;
    Window encodeMs;
    Window audioMs;
    Window audioWaitMs;
    // P5 audio: the mix (made at start, with the PGM flows' channels), its delay lines' length,
    // fx-audio and its queue of grains (and the jobs it is done with, for reuse).
    std::unique_ptr<AudioMix> audioMix;
    std::size_t audioChannels = 2;
    std::size_t delayLineLength = 0;
    std::mutex audioMu;
    std::condition_variable audioCv;
    std::deque<std::unique_ptr<AudioJob>> audioQueue;
    std::vector<std::unique_ptr<AudioJob>> audioFree;
    std::thread audioThread;
    std::atomic<std::uint64_t> audioJobsDropped{0};
    // The meters of the last block (under meterMu), and a loudness reset per ME for fx-audio.
    mutable std::mutex meterMu;
    MeterSnapshot meters;
    std::vector<std::unique_ptr<std::atomic<bool>>> loudnessReset;

    mutable std::mutex snapMu;
    ThreadCpu::Result cpu;
    GpuSample gpuSample;
    double fps = 0;
    std::uint64_t deviceUsed = 0;

    explicit Impl(Config c)
        : cfg(std::move(c))
        , mixer(cfg.mes, static_cast<int>(cfg.inputs.size()), static_cast<int>(cfg.players.size()), keyerSpecs(cfg))
    {
    }

    static std::vector<KeyerSpec> keyerSpecs(Config const& cfg)
    {
        std::vector<KeyerSpec> specs;
        for (auto const& key : cfg.keys)
        {
            specs.push_back(KeyerSpec{key.me, key.upstream});
        }
        return specs;
    }

    [[nodiscard]] std::uint64_t currentIndex() const
    {
        return timestampToIndex(cfg.rate, mxlGetTime());
    }

    [[nodiscard]] double periodMs() const
    {
        return static_cast<double>(periodNs(cfg.rate)) / 1e6;
    }

    // Index of a source picture in srcWork (inputs, then players); -1 for Black and re-entry.
    [[nodiscard]] int sourceSlot(Source source) const
    {
        if (source.kind == Source::Kind::Input)
        {
            return source.index - 1;
        }
        if (source.kind == Source::Kind::Player)
        {
            return static_cast<int>(inputs.size()) + source.index - 1;
        }
        return -1;
    }

    void allocDevice(void** ptr, std::size_t bytes, char const* what)
    {
        check(cudaMalloc(ptr, bytes), what);
        allocated += bytes;
    }

    // Keyer k's box buffer, when its box is enabled and it has none yet: allocated here on the
    // control thread, stream-ordered on the download stream (the render stream does not wait),
    // so the render thread never allocates.
    void prepareBox(int k)
    {
        if (work != gpu::WorkFormat::Yuv16 || k < 1 || k > static_cast<int>(boxBuffers.size()))
        {
            return;
        }
        auto const key = mixer.keyView()[static_cast<std::size_t>(k - 1)];
        if (!key.upstream || !key.settings.dme.enabled)
        {
            return;
        }
        if (!key.settings.luma && !key.settings.pip)
        {
            prepareKeyPlanes(k);
        }
        std::lock_guard lock{boxMu};
        auto& buffer = boxBuffers[static_cast<std::size_t>(k - 1)];
        if (buffer != nullptr)
        {
            return;
        }
        std::size_t const bytes = gpu::dmeBufferBytes(cfg.width, cfg.height);
        void* data = nullptr;
        cudaSetDevice(cfg.gpu);
        if (cudaMallocAsync(&data, bytes, downloadStream) != cudaSuccess || cudaStreamSynchronize(downloadStream) != cudaSuccess)
        {
            logError("box buffer", {{"keyer", k}, {"mb", bytes / 1048576}});
            return;
        }
        buffer = static_cast<std::int16_t*>(data);
        allocated += bytes;
    }

    // The unpacked planes of the key flow keyer k reads (its linear box), if it has none yet.
    void prepareKeyPlanes(int k)
    {
        std::lock_guard lock{keyMu};
        int const s = keySlot[static_cast<std::size_t>(k - 1)];
        if (s < 0 || s >= static_cast<int>(keyPlanes.size()) || keyPlanes[static_cast<std::size_t>(s)] != nullptr)
        {
            return;
        }
        std::size_t const bytes = gpu::dmePlanesBytes(cfg.width, cfg.height);
        void* data = nullptr;
        cudaSetDevice(cfg.gpu);
        if (cudaMallocAsync(&data, bytes, downloadStream) != cudaSuccess || cudaStreamSynchronize(downloadStream) != cudaSuccess)
        {
            logError("key planes", {{"keyer", k}, {"mb", bytes / 1048576}});
            return;
        }
        keyPlanes[static_cast<std::size_t>(s)] = static_cast<float*>(data);
        allocated += bytes;
    }

    // A frame grab of the next output grain (control thread): its id, or 0 and `error`. The
    // page-locked buffer is allocated here (outside the lock the render thread takes), at most
    // kGrabBuffers.
    int requestGrab(std::string const& text, bool preview, std::string const& path, std::string& error)
    {
        auto const source = parseSource(text);
        bool const known = source && ((source->kind == Source::Kind::Input && source->index <= static_cast<int>(inputs.size())) ||
                                         (source->kind == Source::Kind::Player && source->index <= static_cast<int>(players.size())) ||
                                         (source->kind == Source::Kind::Me && source->index <= cfg.mes));
        if (!known)
        {
            error = "source must be an input in<k>, a clip player cp<k> or an ME me<n> of this engine";
            return 0;
        }
        if (preview && source->kind != Source::Kind::Me)
        {
            error = "bus pvw only for an ME";
            return 0;
        }
        if (path.empty())
        {
            error = "a grab needs a path";
            return 0;
        }
        std::uint8_t* buffer = nullptr;
        {
            std::lock_guard lock{grabMu};
            if (!grabFree.empty())
            {
                buffer = grabFree.back();
                grabFree.pop_back();
            }
            else if (grabBuffers.size() >= kGrabBuffers)
            {
                error = "too many grabs in flight (" + std::to_string(kGrabBuffers) + ")";
                return 0;
            }
        }
        if (buffer == nullptr)
        {
            void* data = nullptr;
            cudaSetDevice(cfg.gpu);
            if (cudaHostAlloc(&data, frameBytes, cudaHostAllocPortable) != cudaSuccess)
            {
                error = "cannot allocate a grab buffer";
                return 0;
            }
            buffer = static_cast<std::uint8_t*>(data);
            std::lock_guard lock{grabMu};
            grabBuffers.push_back(buffer);
        }
        auto grab = std::make_shared<Grab>();
        grab->source = *source;
        grab->preview = preview;
        grab->path = path;
        grab->host = buffer;
        grab->requested = std::chrono::steady_clock::now();
        std::lock_guard lock{grabMu};
        grab->id = grabNext++;
        grabPending.push_back(grab);
        grabRecent.push_back(grab);
        while (grabRecent.size() > kGrabHistory)
        {
            grabRecent.pop_front();
        }
        return grab->id;
    }

    [[nodiscard]] std::string grabJson(int id) const
    {
        std::lock_guard lock{grabMu};
        for (auto const& g : grabRecent)
        {
            if (g->id == id)
            {
                return json{{"grab", g->id}, {"source", sourceName(g->source)}, {"bus", g->source.kind == Source::Kind::Me ? (g->preview ? "pvw" : "pgm") : ""},
                    {"path", g->path}, {"state", g->state}, {"error", g->error}, {"index", g->index}, {"tai", g->tai},
                    {"source_index", g->source.kind == Source::Kind::Input ? json(g->sourceIndex) : json(nullptr)}, {"ms", round3(g->ms)}}
                    .dump();
            }
        }
        return {};
    }

    // ---- setup -------------------------------------------------------------------------

    std::unique_ptr<Input> makeInput(int number, std::string name, std::string label, bool key, std::size_t bytes, int poolSize)
    {
        auto input = std::make_unique<Input>();
        input->number = number;
        input->name = std::move(name);
        input->label = std::move(label);
        input->key = key;
        input->bytes = bytes;
        input->pool.allocate(bytes, poolSize);
        input->allocated = poolSize > 0;
        allocated += bytes * static_cast<std::size_t>(poolSize);
        return input;
    }

    void setupGpu()
    {
        // Blocking sync: every wait on an event or stream sleeps instead of spinning a core.
        check(cudaInitDevice(cfg.gpu, cudaDeviceScheduleBlockingSync, 0), "cudaInitDevice");
        check(cudaSetDevice(cfg.gpu), "cudaSetDevice");
        cudaDeviceProp prop{};
        check(cudaGetDeviceProperties(&prop, cfg.gpu), "cudaGetDeviceProperties");
        gpuName = prop.name;
        char bus[32] = {};
        if (cudaDeviceGetPCIBusId(bus, sizeof(bus), cfg.gpu) == cudaSuccess)
        {
            pciBus = bus;
        }
        check(cudaStreamCreateWithFlags(&renderStream, cudaStreamNonBlocking), "render stream");
        check(cudaStreamCreateWithFlags(&downloadStream, cudaStreamNonBlocking), "download stream");
        work = cfg.workFormat == "rgba16f" ? gpu::WorkFormat::Rgba16f : gpu::WorkFormat::Yuv16;
        rowBytes = v210RowBytes(cfg.width);
        frameBytes = static_cast<std::size_t>(rowBytes) * static_cast<std::size_t>(cfg.height);
        keyRowBytes = keyPlaneRowBytes(cfg.width);
        keyBytes = frameBytes + static_cast<std::size_t>(keyRowBytes) * static_cast<std::size_t>(cfg.height);

        allocDevice(reinterpret_cast<void**>(&black), frameBytes, "black frame");
        check(gpu::fillBlack(renderStream, black, rowBytes, cfg.width, cfg.height), "fill black");
        allocDevice(reinterpret_cast<void**>(&blackKey), keyBytes, "fade to black frame");
        check(gpu::fillBlack(renderStream, blackKey, rowBytes, cfg.width, cfg.height), "fill black");
        std::vector<std::uint32_t> const opaque((keyBytes - frameBytes) / 4, 0x3fffffffu);
        check(cudaMemcpyAsync(blackKey + frameBytes, opaque.data(), keyBytes - frameBytes, cudaMemcpyHostToDevice, renderStream), "key plane");
        check(cudaStreamSynchronize(renderStream), "key plane");

        // The ring keeps grains i-L .. newest (+1 for a source a grain ahead); in flight are at
        // most one grain per output slot and the one being uploaded.
        recentDepth = cfg.latency + 2;
        int const poolSize = recentDepth + kSlots + 1;
        int number = 1;
        for (auto const& inputCfg : cfg.inputs)
        {
            auto input = makeInput(number, "in" + std::to_string(number), inputCfg.label, false, frameBytes, poolSize);
            input->video = inputCfg.video;
            input->audio = inputCfg.audio;
            inputs.push_back(std::move(input));
            ++number;
        }
        keyPoolSize = poolSize;
        keySlot.assign(cfg.keys.size(), -1);
        keyPlanes.assign(cfg.keys.size(), nullptr);
        for (std::size_t s = 0; s < cfg.keys.size(); ++s)
        {
            keys.push_back(makeInput(static_cast<int>(s) + 1, "key" + std::to_string(s + 1), "", true, keyBytes, 0));
        }
        for (std::size_t k = 0; k < cfg.keys.size(); ++k)
        {
            routeKeyer(static_cast<int>(k) + 1, cfg.keys[k].route);
        }
        number = 1;
        number = 1;
        for (auto const& playerCfg : cfg.players)
        {
            PlayerSetup setup;
            setup.number = number++;
            setup.label = playerCfg.label;
            setup.width = cfg.width;
            setup.height = cfg.height;
            setup.rate = cfg.rate;
            setup.gpu = cfg.gpu;
            setup.frameBytes = frameBytes;
            setup.channels = static_cast<std::size_t>(cfg.audioChannels);
            players.push_back(std::make_unique<ClipPlayer>(setup));
            allocated += players.back()->deviceBytes();
        }
        for (int s = 1; s <= cfg.stingerPlayers; ++s)
        {
            PlayerSetup setup;
            setup.number = s;
            setup.label = "Stinger " + std::to_string(s);
            setup.width = cfg.width;
            setup.height = cfg.height;
            setup.rate = cfg.rate;
            setup.gpu = cfg.gpu;
            setup.frameBytes = frameBytes;
            setup.keyRowBytes = keyRowBytes;
            setup.channels = static_cast<std::size_t>(cfg.audioChannels);
            stingers.push_back(std::make_unique<ClipPlayer>(setup));
            allocated += stingers.back()->deviceBytes();
        }
        auto addWork = [&](std::vector<gpu::WorkBuffer>& list, std::size_t count) {
            list.resize(count);
            for (auto& buffer : list)
            {
                check(gpu::allocWork(buffer, work, cfg.width, cfg.height), "work buffer");
                allocated += buffer.bytes;
            }
        };
        addWork(srcWork, inputs.size() + players.size());
        addWork(pgmWork, static_cast<std::size_t>(cfg.mes));
        addWork(pvwWork, static_cast<std::size_t>(cfg.mes));
        check(gpu::allocWork(blackWork, work, cfg.width, cfg.height), "work buffer");
        allocated += blackWork.bytes;
        if (work == gpu::WorkFormat::Yuv16)
        {
            check(gpu::allocDmeScratch(dmeScratch, cfg.width, cfg.height), "dme scratch");
            allocated += dmeScratch.bytes;
        }
        boxBuffers.assign(cfg.keys.size(), nullptr);
        check(gpu::unpack(renderStream, black, rowBytes, blackWork), "black work");

        std::size_t const outCount = static_cast<std::size_t>(2 * cfg.mes);
        for (auto& slot : slots)
        {
            slot.out.resize(outCount);
            for (auto& out : slot.out)
            {
                allocDevice(reinterpret_cast<void**>(&out), frameBytes, "output frame");
            }
            slot.mark.resize(static_cast<std::size_t>(4 + cfg.mes));
            slot.boxed.resize(static_cast<std::size_t>(cfg.mes));
            for (auto* list : {&slot.mark, &slot.boxed})
            {
                for (auto& event : *list)
                {
                    check(cudaEventCreate(&event), "cudaEventCreate");
                }
            }
            check(cudaEventCreateWithFlags(&slot.downloadStart, cudaEventDefault), "cudaEventCreate");
            check(cudaEventCreateWithFlags(&slot.grabbed, cudaEventBlockingSync | cudaEventDisableTiming), "cudaEventCreate");
            slot.downloaded.resize(outCount);
            for (auto& event : slot.downloaded)
            {
                check(cudaEventCreateWithFlags(&event, cudaEventBlockingSync), "cudaEventCreate");
            }
        }
        for (int m = 0; m < cfg.mes; ++m)
        {
            meMs.push_back(std::make_unique<Window>());
            dmeMs.push_back(std::make_unique<Window>());
        }

        tiles = mosaicTiles(cfg.width, cfg.height, static_cast<int>(inputs.size()), static_cast<int>(players.size()), cfg.mes);
        mosaicDivisor = std::max(1, static_cast<int>(std::lround(static_cast<double>(cfg.rate.num) / static_cast<double>(cfg.rate.den) / cfg.mosaicFps)));
        if (cfg.mosaic)
        {
            std::size_t const lumaBytes = static_cast<std::size_t>(cfg.width) * static_cast<std::size_t>(cfg.height);
            for (auto& slot : mosaicSlots)
            {
                allocDevice(reinterpret_cast<void**>(&slot.nv12), lumaBytes * 3 / 2, "mosaic frame");
                // Black where no tile is.
                check(cudaMemsetAsync(slot.nv12, 16, lumaBytes, renderStream), "memset");
                check(cudaMemsetAsync(slot.nv12 + lumaBytes, 128, lumaBytes / 2, renderStream), "memset");
                check(cudaEventCreateWithFlags(&slot.ready, cudaEventBlockingSync | cudaEventDisableTiming), "cudaEventCreate");
            }
            EncoderConfig enc;
            enc.width = cfg.width;
            enc.height = cfg.height;
            enc.fps = cfg.mosaicFps;
            enc.kbps = cfg.mosaicKbps;
            enc.url = cfg.rtspBase() + "/" + cfg.streamPath("mosaic");
            enc.gpu = cfg.gpu;
            encoder = std::make_unique<MosaicEncoder>(enc);
        }
        check(cudaStreamSynchronize(renderStream), "setup");
    }

    // The MXL instance of an output domain; creates the domain (and its domain_def.json) when
    // it does not exist.
    mxlInstance outputInstance(std::string const& domain, std::string const& domainId)
    {
        auto const it = outInstances.find(domain);
        if (it != outInstances.end())
        {
            return it->second;
        }
        std::filesystem::create_directories(domain);
        auto const defPath = std::filesystem::path(domain) / "domain_def.json";
        if (!std::filesystem::exists(defPath))
        {
            // BCP-007-03 requires id, label, description and tags.
            std::ofstream out(defPath);
            out << json{{"id", domainId.empty() ? nameUuid("domain:" + domain) : domainId}, {"label", cfg.label}, {"description", "FlowXer outputs"},
                       {"tags", json::object()}}
                       .dump()
                << "\n";
        }
        else if (!domainId.empty())
        {
            std::ifstream in(defPath);
            json const def = json::parse(in, nullptr, false);
            if (def.is_discarded() || def.value("id", std::string{}) != domainId)
            {
                logWarn("domain_id_differs", {{"domain", domain}, {"configured", domainId}, {"file", def.is_discarded() ? "" : def.value("id", std::string{})}});
            }
        }
        mxlInstance const instance = mxlCreateInstance(domain.c_str(), nullptr);
        if (instance == nullptr)
        {
            throw std::runtime_error("cannot open the MXL output domain " + domain);
        }
        // No garbage collection here: the flows a stopped mixer left in its domain are opened
        // as they are (same inode), so their readers keep reading without re-opening.
        outInstances[domain] = instance;
        return instance;
    }

    // Creates the flow, or opens it when it exists (MXL create-or-open: same flow, same inode,
    // so its readers keep reading). An existing flow must have the engine's format and no
    // active writer.
    std::unique_ptr<Output> openOutput(int me, char const* kind, std::string const& configured, OutputConfig const& oc, bool audio)
    {
        auto output = std::make_unique<Output>();
        output->me = me;
        output->preview = std::string(kind) == "PVW";
        output->outIndex = static_cast<std::size_t>(2 * (me - 1) + (output->preview ? 1 : 0));
        output->domain = oc.domain;
        output->instance = outputInstance(oc.domain, oc.domainId);
        std::string const prefix = oc.label.empty() ? cfg.label + " ME" + std::to_string(me) : oc.label;
        std::string const group = oc.group.empty() ? "ME" + std::to_string(me) : oc.group;
        output->id = configured.empty() ? nameUuid(oc.domain + "/me" + std::to_string(me) + "/" + kind + (audio ? "/audio" : "")) : configured;
        output->label = prefix + " " + kind + (audio ? " Audio" : " Video");
        auto const block = grainSamples(cfg.rate, 1);
        std::string const groupHint = group + ":" + kind + (audio ? "Audio" : "Video");
        auto const def = audio ? audioFlowDefinition(output->id, output->label, groupHint, cfg.audioChannels)
                               : videoFlowDefinition(output->id, output->label, groupHint, cfg.width, cfg.height, cfg.rate);
        auto const options = audio ? "{\"maxCommitBatchSizeHint\":" + std::to_string(block.end - block.first) + "}" : std::string("{\"maxCommitBatchSizeHint\":1}");
        // One writer at a time: a flow that another process still writes is not taken over.
        bool active = false;
        if (mxlIsFlowActive(output->instance, output->id.c_str(), &active) == MXL_STATUS_OK && active)
        {
            throw std::runtime_error("the MXL flow " + output->id + " in " + oc.domain + " has an active writer: stop the mixer that writes it first");
        }
        mxlFlowConfigInfo info{};
        bool created = false;
        if (mxlCreateFlowWriter(output->instance, def.c_str(), options.c_str(), &output->writer, &info, &created) != MXL_STATUS_OK)
        {
            throw std::runtime_error("cannot create or open the MXL flow " + output->id + " (" + output->label + ") in " + oc.domain);
        }
        output->existing = !created;
        std::string why;
        if (audio)
        {
            output->channels = info.continuous.channelCount;
            if (info.common.format != MXL_DATA_FORMAT_AUDIO || info.common.grainRate.numerator != kAudioRate || info.common.grainRate.denominator != 1)
            {
                throw std::runtime_error("existing flow " + output->id + " is not 48 kHz audio");
            }
        }
        else
        {
            output->grainCount = info.discrete.grainCount;
            if (!created && !formatMatches(output->instance, output->id, why, false))
            {
                throw std::runtime_error("existing flow " + output->id + " is " + why + ", not v210 " + cfg.format);
            }
        }
        logInfo(created ? "output_flow_created" : "output_flow_opened",
            {{"me", me}, {"kind", std::string(kind) + (audio ? " audio" : " video")}, {"flow", output->id}, {"domain", oc.domain}, {"channels", output->channels}});
        return output;
    }

    void setupOutputs()
    {
        pvwOf.assign(static_cast<std::size_t>(cfg.mes), nullptr);
        for (int m = 1; m <= cfg.mes; ++m)
        {
            auto const& oc = cfg.outputs[static_cast<std::size_t>(m - 1)];
            outputs.push_back(openOutput(m, "PGM", oc.pgmVideo, oc, false));
            if (cfg.pvwOutputs)
            {
                outputs.push_back(openOutput(m, "PVW", oc.pvwVideo, oc, false));
                pvwOf[static_cast<std::size_t>(m - 1)] = outputs.back().get();
            }
            audioOutputs.push_back(openOutput(m, "PGM", oc.pgmAudio, oc, true));
        }
    }

    // ---- input threads -------------------------------------------------------------------

    // The flow carries cfg.width x cfg.height at the engine's rate: v210 (or v210a: its fill
    // comes first), or v210a only for a key input.
    bool formatMatches(mxlInstance instance, std::string const& flowId, std::string& why, bool allowAlpha = true, bool needAlpha = false) const
    {
        std::size_t size = 0;
        mxlGetFlowDef(instance, flowId.c_str(), nullptr, &size);
        std::string text(size, '\0');
        if (size == 0 || mxlGetFlowDef(instance, flowId.c_str(), text.data(), &size) != MXL_STATUS_OK)
        {
            why = "no flow definition";
            return false;
        }
        text.resize(std::strlen(text.c_str()));
        json def = json::parse(text, nullptr, false);
        if (def.is_discarded())
        {
            why = "flow definition is not JSON";
            return false;
        }
        auto const media = def.value("media_type", std::string{});
        auto const width = def.value("frame_width", 0);
        auto const height = def.value("frame_height", 0);
        auto const rate = def.value("grain_rate", json::object());
        auto const num = rate.value("numerator", 0LL);
        auto const den = rate.value("denominator", 1LL);
        bool const mediaOk = needAlpha ? media == "video/v210a" : (media == "video/v210" || (allowAlpha && media == "video/v210a"));
        if (!mediaOk || width != cfg.width || height != cfg.height || num * cfg.rate.den != den * cfg.rate.num)
        {
            why = media + " " + std::to_string(width) + "x" + std::to_string(height) + " " + std::to_string(num) + "/" + std::to_string(den);
            return false;
        }
        return true;
    }

    void setState(Input& in, char const* state)
    {
        std::lock_guard lock{in.mu};
        in.state = state;
    }

    // The start-up window of a source routed (or started) now.
    std::uint64_t graceFromNow() const
    {
        return currentIndex() + static_cast<std::uint64_t>(cfg.latency) + kGraceGrains;
    }

    static void addArrival(Input& in, double ms)
    {
        if (ms < kMaxArrivalMs)
        {
            in.arrival.add(ms);
        }
    }

    // True in a source's start-up window and kLagSettleNs after it: its lag statistics skip that time.
    [[nodiscard]] bool settling(std::uint64_t graceUntil) const
    {
        return graceUntil != 0 && mxlGetTime() < indexToTimestamp(cfg.rate, graceUntil) + kLagSettleNs;
    }

    void inputMain(Input& in)
    {
        nameThread("fx-" + in.name);
        cudaSetDevice(cfg.gpu);
        cudaStream_t stream = nullptr;
        cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking);
        // Upload timing: a pair of events per copy, read two copies later (finished by then).
        std::array<std::array<cudaEvent_t, 2>, 4> timing{};
        for (auto& pair : timing)
        {
            cudaEventCreate(&pair[0]);
            cudaEventCreate(&pair[1]);
        }
        std::uint64_t uploads = 0;
        HostRegistry registry;
        mxlInstance instance = nullptr;
        std::string instanceDomain;
        mxlFlowReader reader = nullptr;
        std::uint64_t next = MXL_UNDEFINED_INDEX;
        std::uint64_t gen = ~0ull;
        Route route;
        int backoffMs = 250;
        auto const pause = [&](int ms) {
            for (int waited = 0; waited < ms && run.load() && in.videoGen.load() == gen; waited += 50)
            {
                std::this_thread::sleep_for(std::chrono::milliseconds(std::min(50, ms - waited)));
            }
        };
        // The registry unlocks the reader's grain memory before the reader unmaps it.
        auto const closeReader = [&] {
            cudaStreamSynchronize(stream);
            registry.release();
            if (reader != nullptr)
            {
                mxlReleaseFlowReader(instance, reader);
            }
            reader = nullptr;
        };
        auto const closeInstance = [&] {
            closeReader();
            if (instance != nullptr)
            {
                mxlDestroyInstance(instance);
            }
            instance = nullptr;
            instanceDomain.clear();
        };
        while (run.load())
        {
            if (in.videoGen.load() != gen)
            {
                {
                    std::lock_guard lock{in.mu};
                    gen = in.videoGen.load();
                    route = in.video;
                    // A new route shows its own grains only (black until the first one).
                    in.recent.clear();
                    in.state = route.flow.empty() ? "not_routed" : "starting";
                }
                closeReader();
                if (route.domain != instanceDomain)
                {
                    closeInstance();
                }
                in.graceUntil.store(graceFromNow());
                in.arrival.reset();
                next = MXL_UNDEFINED_INDEX;
                backoffMs = 250;
                logInfo("input_route", {{"input", in.name}, {"domain", route.domain}, {"flow", route.flow}});
            }
            if (route.flow.empty())
            {
                std::unique_lock lock{in.mu};
                in.routed.wait_for(lock, std::chrono::milliseconds(200), [&] { return !run.load() || in.videoGen.load() != gen; });
                continue;
            }
            if (instance == nullptr)
            {
                instance = mxlCreateInstance(route.domain.c_str(), nullptr);
                if (instance == nullptr)
                {
                    setState(in, "domain_not_found");
                    pause(backoffMs);
                    backoffMs = std::min(5000, backoffMs * 2);
                    continue;
                }
                instanceDomain = route.domain;
            }
            if (reader == nullptr)
            {
                if (mxlCreateFlowReader(instance, route.flow.c_str(), nullptr, &reader) != MXL_STATUS_OK)
                {
                    reader = nullptr;
                    setState(in, "flow_not_found");
                    pause(backoffMs);
                    backoffMs = std::min(5000, backoffMs * 2);
                    continue;
                }
                std::string why;
                if (!formatMatches(instance, route.flow, why, !in.key, in.key))
                {
                    closeReader();
                    setState(in, "format_mismatch");
                    logWarn("input_format_mismatch", {{"input", in.name}, {"flow", route.flow}, {"format", why}});
                    pause(5000);
                    continue;
                }
                setState(in, "running");
                logInfo("input_open", {{"input", in.name}, {"domain", route.domain}, {"flow", route.flow}});
                next = MXL_UNDEFINED_INDEX;
                backoffMs = 250;
            }
            mxlFlowRuntimeInfo runtime{};
            if (mxlFlowReaderGetRuntimeInfo(reader, &runtime) != MXL_STATUS_OK)
            {
                closeReader();
                continue;
            }
            if (runtime.headIndex == MXL_UNDEFINED_INDEX)
            {
                pause(20);
                continue;
            }
            // Start at the head; more than one grain behind it (a stall), jump to it.
            if (next == MXL_UNDEFINED_INDEX || next + 1 < runtime.headIndex)
            {
                if (next != MXL_UNDEFINED_INDEX)
                {
                    ++in.catchups;
                }
                next = runtime.headIndex;
            }
            // The arrival of grain `next`: the head's write time when it is there already, else
            // the moment the blocking read returns it.
            bool const waiting = next > runtime.headIndex;
            if (next == runtime.headIndex && runtime.lastWriteTime != 0 && !settling(in.graceUntil.load()))
            {
                auto const due = indexToTimestamp(cfg.rate, next);
                addArrival(in, runtime.lastWriteTime > due ? static_cast<double>(runtime.lastWriteTime - due) / 1e6 : 0.0);
            }
            mxlGrainInfo grain{};
            std::uint8_t* payload = nullptr;
            auto const status = mxlFlowReaderGetGrain(reader, next, kGrainWaitNs, &grain, &payload);
            if (status == MXL_ERR_OUT_OF_RANGE_TOO_LATE)
            {
                ++in.resyncs;
                next = MXL_UNDEFINED_INDEX;
                continue;
            }
            if (status == MXL_ERR_FLOW_INVALID)
            {
                // The writer re-created the flow (new inode): open the new one.
                ++in.reopens;
                closeReader();
                continue;
            }
            if (status == MXL_ERR_OUT_OF_RANGE_TOO_EARLY || status == MXL_ERR_TIMEOUT)
            {
                // Nothing new within the wait.
                continue;
            }
            if (status != MXL_STATUS_OK || payload == nullptr)
            {
                pause(5);
                continue;
            }
            if (waiting && !settling(in.graceUntil.load()))
            {
                auto const now = mxlGetTime();
                auto const due = indexToTimestamp(cfg.rate, grain.index);
                addArrival(in, now > due ? static_cast<double>(now - due) / 1e6 : 0.0);
            }
            next = grain.index + 1;
            if ((grain.flags & MXL_GRAIN_FLAG_INVALID) != 0 || grain.grainSize < in.bytes)
            {
                ++in.invalid;
                continue;
            }
            auto frame = in.pool.acquire();
            if (!frame)
            {
                ++in.exhausted;
                continue;
            }
            // Straight from the MXL grain to the GPU: one DMA, no CPU copy.
            if (!registry.ensure(payload, in.bytes, true))
            {
                ++in.unpinned;
            }
            auto& events = timing[uploads % timing.size()];
            cudaEventRecord(events[0], stream);
            cudaMemcpyAsync(frame->data, payload, in.bytes, cudaMemcpyHostToDevice, stream);
            cudaEventRecord(events[1], stream);
            cudaEventRecord(frame->ready, stream);
            frame->index = grain.index;
            if (uploads >= 2)
            {
                auto& done = timing[(uploads - 2) % timing.size()];
                float ms = 0;
                if (cudaEventQuery(done[1]) == cudaSuccess && cudaEventElapsedTime(&ms, done[0], done[1]) == cudaSuccess)
                {
                    in.upload.add(ms);
                }
            }
            ++uploads;
            {
                std::lock_guard lock{in.mu};
                if (in.videoGen.load() != gen)
                {
                    // Routed elsewhere while this grain was read: drop it.
                    continue;
                }
                in.recent.push_back(std::move(frame));
                while (in.recent.size() > static_cast<std::size_t>(recentDepth))
                {
                    in.recent.pop_front();
                }
            }
            ++in.grains;
            in.lastGrainAt.store(mxlGetTime());
        }
        closeInstance();
        for (auto& pair : timing)
        {
            cudaEventDestroy(pair[0]);
            cudaEventDestroy(pair[1]);
        }
        cudaStreamDestroy(stream);
    }

    // Keyer k (1-based) to a key flow: a flow another keyer reads already is shared; else a free
    // slot reads it (its frames allocated on its first route). A slot no keyer uses is closed.
    bool routeKeyer(int k, Route const& video)
    {
        if (k < 1 || k > static_cast<int>(keySlot.size()))
        {
            return false;
        }
        std::lock_guard lock{keyMu};
        auto const users = [&](int s) { return std::count(keySlot.begin(), keySlot.end(), s); };
        int const old = keySlot[static_cast<std::size_t>(k - 1)];
        if (old >= 0 && !video.flow.empty() && keys[static_cast<std::size_t>(old)]->videoRoute() == video)
        {
            // The same flow again: the reader keeps reading.
            return true;
        }
        keySlot[static_cast<std::size_t>(k - 1)] = -1;
        int shared = -1;
        for (int s = 0; s < static_cast<int>(keys.size()) && !video.flow.empty(); ++s)
        {
            if (users(s) > 0 && keys[static_cast<std::size_t>(s)]->videoRoute() == video)
            {
                shared = s;
            }
        }
        if (old >= 0 && old != shared && users(old) == 0)
        {
            route(*keys[static_cast<std::size_t>(old)], Route{}, nullptr);
        }
        if (video.flow.empty())
        {
            return true;
        }
        int slot = shared;
        for (int s = 0; s < static_cast<int>(keys.size()) && slot < 0; ++s)
        {
            // A free slot, one with frames first.
            if (users(s) == 0 && keys[static_cast<std::size_t>(s)]->allocated)
            {
                slot = s;
            }
        }
        for (int s = 0; s < static_cast<int>(keys.size()) && slot < 0; ++s)
        {
            if (users(s) == 0)
            {
                slot = s;
            }
        }
        auto& in = *keys[static_cast<std::size_t>(slot)];
        if (!in.allocated)
        {
            in.pool.allocate(keyBytes, keyPoolSize, downloadStream);
            in.allocated = true;
            allocated += keyBytes * static_cast<std::size_t>(keyPoolSize);
        }
        keySlot[static_cast<std::size_t>(k - 1)] = slot;
        if (shared < 0)
        {
            in.label = cfg.keys[static_cast<std::size_t>(k - 1)].label;
            route(in, video, nullptr);
        }
        return true;
    }

    bool route(Input& in, Route const& video, Route const* audio)
    {
        {
            std::lock_guard lock{in.mu};
            // The start-up window begins with the command: the render thread may need a grain
            // of the new route before the input thread has opened it.
            if (!(in.video == video))
            {
                in.video = video;
                in.recent.clear();
                in.graceUntil.store(graceFromNow());
                in.videoGen.fetch_add(1);
            }
            if (audio != nullptr && !(in.audio == *audio))
            {
                in.audio = *audio;
                in.audioGraceUntil.store(graceFromNow());
                in.audioGen.fetch_add(1);
            }
        }
        in.routed.notify_all();
        return true;
    }

    // ---- render thread -------------------------------------------------------------------

    // Grain j of an input, else its newest older grain (held, counted late) while it is
    // within the hold time, else nothing (black). `index` is the output grain (start-up windows
    // are not counted).
    std::shared_ptr<DevFrame> pick(Input& in, std::uint64_t j, std::uint64_t index)
    {
        std::lock_guard lock{in.mu};
        if (in.video.flow.empty())
        {
            return nullptr;
        }
        bool const startup = index <= in.graceUntil.load();
        if (in.recent.empty())
        {
            ++(startup ? in.startup : in.missing);
            return nullptr;
        }
        for (auto it = in.recent.rbegin(); it != in.recent.rend(); ++it)
        {
            if ((*it)->index <= j)
            {
                if ((*it)->index == j)
                {
                    return *it;
                }
                if (cfg.holdGrains > 0 && (*it)->index + static_cast<std::uint64_t>(cfg.holdGrains) < j)
                {
                    ++(startup ? in.startup : in.missing);
                    return nullptr;
                }
                ++(startup ? in.startup : in.late);
                return *it;
            }
        }
        // A source ahead of TAI: its oldest grain.
        return in.recent.front();
    }

    void renderMosaic(Slot& slot, std::vector<std::uint8_t const*> const& sourceFrames, std::uint64_t index)
    {
        int free = -1;
        for (int k = 0; k < kMosaicSlots; ++k)
        {
            if (!mosaicSlots[static_cast<std::size_t>(k)].busy.load())
            {
                free = k;
                break;
            }
        }
        if (free < 0)
        {
            ++mosaicDropped;
            return;
        }
        auto& target = mosaicSlots[static_cast<std::size_t>(free)];
        std::vector<gpu::MosaicSource> sources;
        for (auto const& tile : tiles)
        {
            gpu::MosaicSource source;
            source.rowBytes = rowBytes;
            source.width = cfg.width;
            source.height = cfg.height;
            source.x = tile.x;
            source.y = tile.y;
            source.w = tile.w;
            source.h = tile.h;
            auto const k = static_cast<std::size_t>(tile.index - 1);
            switch (tile.kind)
            {
            case MosaicTile::Kind::Input:
                source.v210 = sourceFrames[k];
                break;
            case MosaicTile::Kind::Player:
                source.v210 = sourceFrames[inputs.size() + k];
                break;
            case MosaicTile::Kind::Program:
                source.v210 = slot.out[2 * k];
                break;
            case MosaicTile::Kind::Preview:
                source.v210 = slot.out[2 * k + 1];
                break;
            }
            sources.push_back(source);
        }
        std::size_t const lumaBytes = static_cast<std::size_t>(cfg.width) * static_cast<std::size_t>(cfg.height);
        gpu::mosaic(renderStream, sources.data(), static_cast<int>(sources.size()), target.nv12, cfg.width, target.nv12 + lumaBytes, cfg.width);
        cudaEventRecord(target.ready, renderStream);
        target.pts = static_cast<std::int64_t>(index / static_cast<std::uint64_t>(mosaicDivisor));
        target.busy.store(true);
        {
            std::lock_guard lock{encodeMu};
            encodeQueue.push_back(free);
        }
        encodeCv.notify_one();
        ++mosaicFrames;
    }

    // Queues all GPU work of output grain `index` on the render stream (returns at once).
    void render(std::uint64_t index, Slot& slot)
    {
        std::vector<bool> stingerReady(stingers.size());
        for (std::size_t k = 0; k < stingers.size(); ++k)
        {
            stingerReady[k] = stingers[k]->ready();
        }
        auto const plan = mixer.frame(index, stingerReady);
        auto const& plans = plan.mes;
        std::uint64_t const j = inputIndexFor(index, cfg.latency);
        std::size_t const nIn = inputs.size();
        std::size_t const nSrc = nIn + players.size();
        std::vector<std::shared_ptr<DevFrame>> frames(nIn);
        std::vector<std::shared_ptr<PlayerFrame>> playerFrames(players.size());
        std::vector<std::uint8_t const*> pictures(nSrc, nullptr);
        std::vector<cudaEvent_t> ready(nSrc, nullptr);
        for (std::size_t k = 0; k < nIn; ++k)
        {
            frames[k] = pick(*inputs[k], j, index);
            if (frames[k])
            {
                pictures[k] = frames[k]->data;
                ready[k] = frames[k]->ready;
            }
        }
        // A clip player is on air when it is in an ME's Program (or the incoming source of a Mix),
        // or the source of a luma key or a pip on air.
        std::vector<bool> onAir(players.size(), false);
        for (auto const& key : plan.keys)
        {
            if (key.gain > 0.f && (key.settings.luma || key.settings.pip) && key.settings.source.kind == Source::Kind::Player)
            {
                onAir[static_cast<std::size_t>(key.settings.source.index - 1)] = true;
            }
        }
        // A stinger player is on air while a stinger of it runs.
        std::vector<bool> stingOnAir(stingers.size(), false);
        for (auto const& p : plans)
        {
            for (auto const& source : {p.a, p.b})
            {
                if (source.kind == Source::Kind::Player && (source == p.background() || p.mixing))
                {
                    onAir[static_cast<std::size_t>(source.index - 1)] = true;
                }
            }
            if (p.stinger > 0)
            {
                stingOnAir[static_cast<std::size_t>(p.stinger - 1)] = true;
            }
        }
        for (std::size_t k = 0; k < players.size(); ++k)
        {
            playerFrames[k] = players[k]->frameFor(index, onAir[k]);
            if (playerFrames[k] && playerFrames[k]->v210 != nullptr)
            {
                pictures[nIn + k] = playerFrames[k]->v210;
                ready[nIn + k] = playerFrames[k]->ready;
            }
        }
        std::vector<std::shared_ptr<PlayerFrame>> stingFrames(stingers.size());
        for (std::size_t k = 0; k < stingers.size(); ++k)
        {
            stingFrames[k] = stingers[k]->frameFor(index, stingOnAir[k]);
        }
        std::vector<bool> needed(nSrc, false);
        auto const need = [&](Source source) {
            int const s = sourceSlot(source);
            if (s >= 0)
            {
                needed[static_cast<std::size_t>(s)] = true;
            }
        };
        for (auto const& p : plans)
        {
            need(p.background());
            if (p.mixing)
            {
                need(p.b);
            }
            need(p.preview);
        }
        for (auto const& key : plan.keys)
        {
            if (key.gain > 0.f && (key.settings.luma || key.settings.pip))
            {
                need(key.settings.source);
            }
        }
        cudaStream_t const st = renderStream;
        auto mark = slot.mark.begin();
        cudaEventRecord(*mark++, st);
        // Each source is unpacked once per grain, for every ME that uses it.
        for (std::size_t s = 0; s < nSrc; ++s)
        {
            if (!needed[s])
            {
                continue;
            }
            if (ready[s] != nullptr)
            {
                cudaStreamWaitEvent(st, ready[s], 0);
            }
            gpu::unpack(st, pictures[s] != nullptr ? pictures[s] : black, rowBytes, srcWork[s]);
        }
        cudaEventRecord(*mark++, st);
        auto const workOf = [&](Source source) -> gpu::WorkBuffer const& {
            if (source.kind == Source::Kind::Me)
            {
                return pgmWork[static_cast<std::size_t>(source.index - 1)];
            }
            int const s = sourceSlot(source);
            return s >= 0 ? srcWork[static_cast<std::size_t>(s)] : blackWork;
        };
        // The key flows the linear keyers on air need now (grain i - L, like the inputs), each
        // once.
        std::vector<int> slots;
        std::vector<float*> planes;
        {
            std::lock_guard lock{keyMu};
            slots = keySlot;
            planes = keyPlanes;
        }
        // A key flow's planes, unpacked at its first linear box of this grain.
        std::vector<bool> unpacked(keys.size());
        std::vector<std::shared_ptr<DevFrame>> keyFrames(keys.size());
        for (auto const& key : plan.keys)
        {
            int const s = slots[static_cast<std::size_t>(key.key - 1)];
            if (key.gain > 0.f && !key.settings.luma && !key.settings.pip && s >= 0 && !keyFrames[static_cast<std::size_t>(s)])
            {
                auto& frame = keyFrames[static_cast<std::size_t>(s)];
                frame = pick(*keys[static_cast<std::size_t>(s)], j, index);
                if (frame)
                {
                    cudaStreamWaitEvent(st, frame->ready, 0);
                }
            }
        }
        // ME n before ME m (n > m): a re-entered Program is the same output grain (with its keys).
        auto boxed = slot.boxed.begin();
        for (auto const& p : plans)
        {
            auto const me = static_cast<std::size_t>(p.me - 1);
            // Program in one pass: the background (A, a Mix or a Wipe to B, or a stinger's A or B),
            // the upstream keyers, the stinger, the DSKs, Fade to Black. Keys have no preview
            // (Preview shows only its source).
            gpu::Compose job;
            // The ME's boxes (upstream keyers with DME): resampled first, drawn as layers.
            std::array<DmeDraw, gpu::kMaxBoxes> draws;
            int boxCount = 0;
            job.a = &workOf(p.background());
            job.b = p.mixing ? &workOf(p.b) : nullptr;
            job.t = p.mixing ? p.t : 0.f;
            job.wiping = p.wiping;
            if (p.wiping)
            {
                job.wipe = makeWipe(p.wipe, p.t, cfg.width, cfg.height);
            }
            for (int stage = 0; stage < 3; ++stage)
            {
                if (stage == 1)
                {
                    // The upstream keyers' boxes are resampled (dme_ms; before a stinger's wait).
                    cudaEventRecord(*boxed++, st);
                    // The stinger: over the upstream keys, under the DSKs.
                    auto const& frame = p.stinger > 0 ? stingFrames[static_cast<std::size_t>(p.stinger - 1)] : nullptr;
                    if (frame && frame->v210 != nullptr && job.layerCount < gpu::kMaxLayers)
                    {
                        cudaStreamWaitEvent(st, frame->ready, 0);
                        gpu::KeyLayer layer;
                        layer.v210a = frame->v210;
                        layer.gain = 1.f;
                        job.layers[job.layerCount++] = layer;
                    }
                    continue;
                }
                bool const upstream = stage == 0;
                for (auto const& key : plan.keys)
                {
                    if (key.me != p.me || key.upstream != upstream || key.gain <= 0.f || job.layerCount >= gpu::kMaxLayers)
                    {
                        continue;
                    }
                    auto const& set = key.settings;
                    gpu::KeyLayer layer;
                    layer.gain = key.gain * set.gain;
                    if (upstream && set.dme.enabled)
                    {
                        // A box: yuv16 only.
                        if (work != gpu::WorkFormat::Yuv16 || boxCount >= gpu::kMaxBoxes)
                        {
                            continue;
                        }
                        DmeKind const kind = set.pip ? DmeKind::Pip : set.luma ? DmeKind::Luma : DmeKind::Linear;
                        DmeSource source;
                        if (kind == DmeKind::Linear)
                        {
                            int const s = slots[static_cast<std::size_t>(key.key - 1)];
                            auto const& frame = s >= 0 ? keyFrames[static_cast<std::size_t>(s)] : nullptr;
                            if (!frame)
                            {
                                continue;
                            }
                            source = gpu::dmeSource(frame->data, cfg.width, cfg.height, rowBytes, keyRowBytes);
                            if (float* const unpackTo = planes[static_cast<std::size_t>(s)]; unpackTo != nullptr)
                            {
                                if (!unpacked[static_cast<std::size_t>(s)])
                                {
                                    gpu::dmeUnpack(st, source, unpackTo);
                                    unpacked[static_cast<std::size_t>(s)] = true;
                                }
                                source = gpu::dmeSource(unpackTo, cfg.width, cfg.height);
                            }
                        }
                        else
                        {
                            source = gpu::dmeSource(workOf(set.source));
                        }
                        std::int16_t* buffer = nullptr;
                        {
                            std::lock_guard lock{boxMu};
                            buffer = boxBuffers[static_cast<std::size_t>(key.key - 1)];
                        }
                        if (buffer == nullptr)
                        {
                            continue;
                        }
                        DmeDraw& d = draws[static_cast<std::size_t>(boxCount++)];
                        d.box = makeDmeBox(set.dme, kind, cfg.width, cfg.height);
                        d.buffer = buffer;
                        d.gain = layer.gain;
                        d.clip = set.clip;
                        d.soft = set.soft;
                        d.invert = set.invert;
                        gpu::dmeResample(st, d.box, source, dmeScratch, buffer);
                        layer.dme = &d;
                        job.layers[job.layerCount++] = layer;
                        continue;
                    }
                    if (set.luma)
                    {
                        layer.luma = true;
                        layer.source = &workOf(set.source);
                        layer.clip = set.clip;
                        layer.soft = set.soft;
                        layer.invert = set.invert;
                    }
                    else
                    {
                        int const s = slots[static_cast<std::size_t>(key.key - 1)];
                        auto const& frame = s >= 0 ? keyFrames[static_cast<std::size_t>(s)] : nullptr;
                        if (!frame)
                        {
                            continue;
                        }
                        layer.v210a = frame->data;
                    }
                    job.layers[job.layerCount++] = layer;
                }
            }
            // Fade to Black: last, over everything (the DSKs too).
            if (p.ftb > 0.f && job.layerCount < gpu::kMaxLayers)
            {
                gpu::KeyLayer layer;
                layer.v210a = blackKey;
                layer.gain = p.ftb;
                job.layers[job.layerCount++] = layer;
            }
            gpu::compose(st, job, rowBytes, keyRowBytes, pgmWork[me]);
            gpu::mix(st, workOf(p.preview), nullptr, 0.f, pvwWork[me]);
            cudaEventRecord(*mark++, st);
        }
        for (std::size_t me = 0; me < pgmWork.size(); ++me)
        {
            gpu::pack(st, pgmWork[me], slot.out[2 * me], rowBytes);
            gpu::pack(st, pvwWork[me], slot.out[2 * me + 1], rowBytes);
        }
        cudaEventRecord(*mark++, st);
        if (cfg.mosaic && index % static_cast<std::uint64_t>(mosaicDivisor) == 0)
        {
            renderMosaic(slot, pictures, index);
        }
        cudaEventRecord(*mark, st);
        // Frame grabs of this grain (at most kGrabsPerGrain; more wait for the next one): the
        // picture as this grain uses it. fx-writer copies them with this grain's downloads.
        slot.grabs.clear();
        {
            std::lock_guard lock{grabMu};
            while (!grabPending.empty() && slot.grabs.size() < kGrabsPerGrain)
            {
                auto const& g = grabPending.front();
                SlotGrab item;
                item.grab = g;
                item.device = black;
                auto const k = static_cast<std::size_t>(g->source.index - 1);
                if (g->source.kind == Source::Kind::Input && frames[k])
                {
                    item.device = frames[k]->data;
                    item.ready = frames[k]->ready;
                    g->sourceIndex = frames[k]->index;
                }
                else if (g->source.kind == Source::Kind::Player && playerFrames[k] && playerFrames[k]->v210 != nullptr)
                {
                    item.device = playerFrames[k]->v210;
                    item.ready = playerFrames[k]->ready;
                }
                else if (g->source.kind == Source::Kind::Me)
                {
                    item.device = slot.out[2 * k + (g->preview ? 1 : 0)];
                }
                g->index = index;
                g->tai = indexToTimestamp(cfg.rate, index);
                g->state = "copying";
                slot.grabs.push_back(item);
                grabPending.pop_front();
            }
        }
        slot.held.clear();
        for (auto* list : {&frames, &keyFrames})
        {
            for (auto& frame : *list)
            {
                if (frame)
                {
                    slot.held.push_back(std::move(frame));
                }
            }
        }
        slot.players = std::move(playerFrames);
        slot.stingers = std::move(stingFrames);
        slot.index = index;
        slot.inputIndex = j;
        slot.plans = plans;
    }

    void renderMain()
    {
        nameThread("fx-render");
        cudaSetDevice(cfg.gpu);
        std::uint64_t next = 0;
        while (run.load())
        {
            // The output index always comes from TAI: late means skip, never slide.
            auto const step = nextOutputIndex(next, currentIndex());
            skipped += step.skipped;
            std::uint64_t const index = step.index;
            auto const start = windowStart(cfg.rate, index);
            if (mxlGetTime() < start)
            {
                mxlSleepUntil(start);
            }
            if (!run.load())
            {
                break;
            }
            next = index + 1;
            auto& slot = slots[index % kSlots];
            if (slot.busy.load())
            {
                // The writer is three grains behind: drop this grain rather than wait.
                ++writerBusy;
                ++skipped;
                continue;
            }
            render(index, slot);
            slot.busy.store(true);
            ++rendered;
            lastIndex.store(index);
            {
                std::lock_guard lock{writeMu};
                writeQueue.push_back(static_cast<int>(index % kSlots));
            }
            writeCv.notify_one();
        }
    }

    // ---- writer thread -------------------------------------------------------------------

    void write(Slot& slot, HostRegistry& registry)
    {
        cudaStream_t const dl = downloadStream;
        cudaStreamWaitEvent(dl, slot.mark.back(), 0);
        cudaEventRecord(slot.downloadStart, dl);
        std::vector<mxlGrainInfo> infos(outputs.size());
        std::vector<bool> open(outputs.size(), false);
        for (std::size_t o = 0; o < outputs.size(); ++o)
        {
            auto& output = *outputs[o];
            std::uint8_t* payload = nullptr;
            open[o] = output.writer != nullptr && mxlFlowWriterOpenGrain(output.writer, slot.index, &infos[o], &payload) == MXL_STATUS_OK && payload != nullptr;
            if (open[o])
            {
                // Straight into the MXL grain: one DMA, no CPU copy.
                if (!registry.ensure(payload, frameBytes, false))
                {
                    ++unpinned;
                }
                cudaMemcpyAsync(payload, slot.out[output.outIndex], frameBytes, cudaMemcpyDeviceToHost, dl);
            }
            cudaEventRecord(slot.downloaded[o], dl);
        }
        // Frame grabs: after this grain's downloads on the same stream (a picture no ME used was
        // not waited for by the render stream: its upload is waited for here).
        for (auto const& item : slot.grabs)
        {
            if (item.ready != nullptr)
            {
                cudaStreamWaitEvent(dl, item.ready, 0);
            }
            cudaMemcpyAsync(item.grab->host, item.device, frameBytes, cudaMemcpyDeviceToHost, dl);
        }
        if (!slot.grabs.empty())
        {
            cudaEventRecord(slot.grabbed, dl);
        }
        for (std::size_t o = 0; o < outputs.size(); ++o)
        {
            auto& output = *outputs[o];
            cudaEventSynchronize(slot.downloaded[o]);
            if (!open[o])
            {
                ++output.failed;
                ++writeFailed;
                continue;
            }
            infos[o].validSlices = infos[o].totalSlices;
            infos[o].flags = 0;
            if (mxlFlowWriterCommitGrain(output.writer, &infos[o]) == MXL_STATUS_OK)
            {
                ++output.committed;
            }
            else
            {
                ++output.failed;
                ++writeFailed;
            }
        }
        auto const committedAt = mxlGetTime();
        auto const elapsed = [](cudaEvent_t a, cudaEvent_t b) {
            float ms = 0;
            cudaEventElapsedTime(&ms, a, b);
            return static_cast<double>(ms);
        };
        auto const& mark = slot.mark;
        std::size_t const mes = static_cast<std::size_t>(cfg.mes);
        unpackMs.add(elapsed(mark[0], mark[1]));
        for (std::size_t i = 0; i < mes; ++i)
        {
            // mark[2 + i] ends the i-th ME in render order (ME mes .. 1), boxed[i] its boxes.
            meMs[mes - 1 - i]->add(elapsed(mark[1 + i], mark[2 + i]));
            dmeMs[mes - 1 - i]->add(elapsed(mark[1 + i], slot.boxed[i]));
        }
        packMs.add(elapsed(mark[1 + mes], mark[2 + mes]));
        mosaicMs.add(elapsed(mark[2 + mes], mark[3 + mes]));
        double const render = elapsed(mark[0], mark[3 + mes]);
        auto const lastDownload = outputs.empty() ? slot.downloadStart : slot.downloaded[outputs.size() - 1];
        double const download = elapsed(slot.downloadStart, lastDownload);
        renderMs.add(render);
        downloadMs.add(download);
        totalMs.add(render + download);
        spanMs.add(elapsed(mark[0], lastDownload));
        auto const current = timestampToIndex(cfg.rate, committedAt);
        lagGrains.add(static_cast<double>(current - slot.inputIndex));
        auto const windowAt = windowStart(cfg.rate, slot.index);
        commitMs.add(committedAt > windowAt ? static_cast<double>(committedAt - windowAt) / 1e6 : 0.0);
        if (current > slot.index)
        {
            ++late;
        }
        ++committed;
        if (!slot.grabs.empty())
        {
            bool const copied = cudaEventSynchronize(slot.grabbed) == cudaSuccess;
            {
                std::lock_guard lock{grabMu};
                for (auto const& item : slot.grabs)
                {
                    item.grab->state = copied ? "writing" : "failed";
                    if (copied)
                    {
                        grabWrite.push_back(item.grab);
                    }
                    else
                    {
                        item.grab->error = "copy failed";
                        grabFree.push_back(item.grab->host);
                        item.grab->host = nullptr;
                        ++grabsFailed;
                    }
                }
            }
            grabCv.notify_one();
            slot.grabs.clear();
        }
        slot.held.clear();
        queueAudio(slot);
        slot.players.clear();
        slot.stingers.clear();
        slot.busy.store(false);
    }

    // ---- PGM audio (fx-audio) ------------------------------------------------------------

    void setAudioState(Input& in, char const* state)
    {
        std::lock_guard lock{in.mu};
        in.audioState = state;
    }

    void closeAudio(AudioRun::Reader& r)
    {
        if (r.reader != nullptr)
        {
            mxlReleaseFlowReader(r.instance, r.reader);
        }
        r.reader = nullptr;
    }

    // The input's audio reader for its current route, (re)opened at most once a second. A new
    // route empties the delay line; an open gives it the flow's channels.
    bool openAudio(Input& in, AudioRun::Reader& r)
    {
        if (in.audioGen.load() != r.gen)
        {
            closeAudio(r);
            if (r.instance != nullptr && in.audioRoute().domain != r.domain)
            {
                mxlDestroyInstance(r.instance);
                r.instance = nullptr;
                r.domain.clear();
            }
            r.gen = in.audioGen.load();
            r.route = in.audioRoute();
            r.retryAt = {};
            in.audioGraceUntil.store(graceFromNow());
            in.audioLag.reset();
            r.head = MXL_UNDEFINED_INDEX;
            r.line.reset(0, 0);
            r.firstIndex = MXL_UNDEFINED_INDEX;
            r.live = false;
            in.audioHeld.store(0);
            in.audioMovedAt.store(0);
            setAudioState(in, r.route.flow.empty() ? "not_routed" : "starting");
        }
        if (r.reader != nullptr)
        {
            return true;
        }
        auto const now = std::chrono::steady_clock::now();
        if (r.route.flow.empty() || now < r.retryAt)
        {
            return false;
        }
        r.retryAt = now + std::chrono::seconds(1);
        if (r.instance == nullptr)
        {
            r.instance = mxlCreateInstance(r.route.domain.c_str(), nullptr);
            r.domain = r.route.domain;
        }
        if (r.instance == nullptr)
        {
            r.domain.clear();
            setAudioState(in, "domain_not_found");
            return false;
        }
        if (mxlCreateFlowReader(r.instance, r.route.flow.c_str(), nullptr, &r.reader) != MXL_STATUS_OK)
        {
            r.reader = nullptr;
            setAudioState(in, "flow_not_found");
            return false;
        }
        mxlFlowConfigInfo info{};
        if (mxlFlowReaderGetConfigInfo(r.reader, &info) != MXL_STATUS_OK || info.common.format != MXL_DATA_FORMAT_AUDIO ||
            info.common.grainRate.numerator != kAudioRate || info.common.grainRate.denominator != 1)
        {
            closeAudio(r);
            setAudioState(in, "format_mismatch");
            return false;
        }
        r.channels = info.continuous.channelCount;
        r.maxRead = 0;
        mxlFlowReaderGetMaxReadLengthSamples(r.reader, &r.maxRead);
        // A (re)opened flow starts its delay line again (and fades in).
        r.line.reset(r.channels, delayLineLength);
        r.firstIndex = MXL_UNDEFINED_INDEX;
        r.live = false;
        in.audioChannels.store(static_cast<std::uint32_t>(r.channels));
        // Samples from before the open may be missing: a start-up window.
        in.audioGraceUntil.store(graceFromNow());
        setAudioState(in, "running");
        logInfo("input_audio_open", {{"input", in.name}, {"flow", r.route.flow}, {"channels", r.channels}, {"max_read", r.maxRead}});
        return true;
    }

    // How far the audio head is behind TAI, once per grain for every routed input. Only while
    // the head moves: a head that never moved since the route (a left flow) or stood still for a
    // second (no_signal) is no arrival, and its growing distance would hold the peak forever.
    void measureAudio(Input& in, AudioRun::Reader& r)
    {
        mxlFlowRuntimeInfo runtime{};
        if (r.reader == nullptr || mxlFlowReaderGetRuntimeInfo(r.reader, &runtime) != MXL_STATUS_OK || runtime.headIndex == MXL_UNDEFINED_INDEX)
        {
            return;
        }
        auto const nowNs = mxlGetTime();
        if (r.head != MXL_UNDEFINED_INDEX && runtime.headIndex != r.head)
        {
            // A head that starts to move after standing still (a mirror flow written only after
            // the route's settle window) catches up first: no steady-state lag either.
            auto const movedAt = in.audioMovedAt.load();
            if (movedAt == 0 || nowNs > movedAt + 1'000'000'000ull)
            {
                in.audioResumedAt.store(nowNs);
            }
            in.audioMovedAt.store(nowNs);
        }
        r.head = runtime.headIndex;
        if (nowNs > in.audioMovedAt.load() + 1'000'000'000ull || settling(in.audioGraceUntil.load()) ||
            nowNs < in.audioResumedAt.load() + kLagSettleNs)
        {
            return;
        }
        auto const now = timestampToIndex(Rate{kAudioRate, 1}, nowNs);
        double const behind = now > runtime.headIndex ? static_cast<double>(now - runtime.headIndex) : 0.0;
        double const behindMs = behind * 1000.0 / static_cast<double>(kAudioRate);
        if (behindMs < kMaxArrivalMs)
        {
            in.audioLag.add(behindMs);
        }
    }

    // `count` samples of every channel from sample index `from`, out of an MXL slice, into the line.
    static void putSlice(DelayLine& line, mxlWrappedMultiBufferSlice const& slice, std::uint64_t from, std::size_t count)
    {
        for (std::size_t c = 0; c < std::min(slice.count, line.channels()); ++c)
        {
            std::size_t filled = 0;
            for (auto const& fragment : slice.base.fragments)
            {
                if (fragment.pointer == nullptr || filled >= count)
                {
                    continue;
                }
                std::size_t const n = std::min(fragment.size / sizeof(float), count - filled);
                line.put(c, from + filled, reinterpret_cast<float const*>(static_cast<std::uint8_t const*>(fragment.pointer) + c * slice.stride), n);
                filled += n;
            }
        }
    }

    // A live input's new samples into its delay line, up to `end` (the end of its grain i − L). A
    // heard input waits (bounded, until `deadline`, at most audioWaitMs) for what this grain needs
    // (up to `need`, its delayed window's end); the others take what is there and get the rest from
    // MXL's history on the next grain. A gap (samples older than MXL keeps) starts the line again.
    void fillLine(Input& in, AudioRun::Reader& r, std::uint64_t end, std::uint64_t need, bool heard, std::uint64_t deadline, std::size_t n)
    {
        auto& line = r.line;
        if (line.channels() != r.channels || line.length() != delayLineLength)
        {
            line.reset(r.channels, delayLineLength);
            r.firstIndex = MXL_UNDEFINED_INDEX;
        }
        std::uint64_t from = line.started() ? line.end() : (end > n ? end - n : 0);
        if (from >= end)
        {
            return;
        }
        if (r.maxRead > 0 && end - from > r.maxRead)
        {
            from = end - r.maxRead;
        }
        line.begin(from);
        if (r.firstIndex == MXL_UNDEFINED_INDEX)
        {
            r.firstIndex = from;
        }
        auto const take = [&](std::uint64_t to) {
            mxlWrappedMultiBufferSlice slice{};
            auto const status = mxlFlowReaderGetSamplesNonBlocking(r.reader, to, static_cast<std::size_t>(to - from), &slice);
            if (status == MXL_STATUS_OK)
            {
                putSlice(line, slice, from, static_cast<std::size_t>(to - from));
                line.commit(to);
                from = to;
            }
            return status;
        };
        auto status = take(end);
        if (status == MXL_ERR_OUT_OF_RANGE_TOO_EARLY && heard && need > from)
        {
            auto const now = mxlGetTime();
            std::uint64_t const budget = static_cast<std::uint64_t>(cfg.audioWaitMs) * 1'000'000ull;
            if (deadline > now && budget > 0)
            {
                auto const started = std::chrono::steady_clock::now();
                ++in.audioWaits;
                mxlWrappedMultiBufferSlice slice{};
                auto const waited = mxlFlowReaderGetSamples(r.reader, need, static_cast<std::size_t>(need - from), std::min(budget, deadline - now), &slice);
                audioWaitMs.add(std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count());
                if (waited == MXL_STATUS_OK)
                {
                    putSlice(line, slice, from, static_cast<std::size_t>(need - from));
                    line.commit(need);
                    from = need;
                    status = from < end ? take(end) : MXL_STATUS_OK;
                }
                else
                {
                    status = waited;
                }
            }
        }
        if (status == MXL_ERR_OUT_OF_RANGE_TOO_EARLY && from < end)
        {
            // What is there now; the rest comes on the next grain.
            mxlFlowRuntimeInfo runtime{};
            if (mxlFlowReaderGetRuntimeInfo(r.reader, &runtime) == MXL_STATUS_OK && runtime.headIndex != MXL_UNDEFINED_INDEX && runtime.headIndex > from)
            {
                status = take(std::min<std::uint64_t>(runtime.headIndex, end));
            }
        }
        if (status == MXL_ERR_OUT_OF_RANGE_TOO_LATE)
        {
            // Older than MXL still holds: start again with this grain.
            from = end > n ? end - n : 0;
            line.begin(from);
            status = take(end);
        }
        if (status == MXL_ERR_FLOW_INVALID)
        {
            // The writer re-created the flow: open the new one.
            closeAudio(r);
            r.retryAt = {};
            ++in.audioReopens;
        }
    }

    // The sound of output grain `index`: every routed live input into its delay line and out of it
    // delayed and mapped (a change of delay or map crossfades over the grain), the clip and stinger
    // players' frames, the MEs' mixes (core/audiomix), written to each ME's PGM audio flow.
    void audioGrain(std::uint64_t index, AudioJob const& job, AudioRun& audio)
    {
        auto const out = grainSamples(cfg.rate, index);
        std::size_t const n = out.end - out.first;
        std::size_t const ch = audio.channels;
        // Late samples are waited for until 1 ms before the end of this grain's window.
        auto const deadline = windowStart(cfg.rate, index + 1) - 1'000'000ull;
        bool const own = index == job.index;
        auto const heard = audioMix->heardInputs(job.plans);
        MixSources sources;
        sources.inputs.assign(inputs.size(), nullptr);
        auto const span = delayedSamples(cfg.rate, index, cfg.latency, 0);
        for (std::size_t k = 0; k < inputs.size(); ++k)
        {
            auto& in = *inputs[k];
            auto& r = audio.readers[k];
            std::uint32_t delay = 0;
            std::vector<int> map;
            {
                std::lock_guard lock{in.mu};
                delay = in.audioDelay;
                map = in.audioMap;
            }
            auto const window = delayedSamples(cfg.rate, index, cfg.latency, delay);
            bool const isHeard = k < heard.size() && heard[k];
            in.audioHeard.store(isHeard);
            if (r.reader != nullptr)
            {
                fillLine(in, r, span.end, window.end, isHeard, deadline, n);
            }
            in.audioHeld.store(r.line.started() ? r.line.end() - r.line.start() : 0);
            if (r.route.flow.empty())
            {
                // Not routed: silence (a change of delay or map applies when it is).
                r.delay = delay;
                r.map = map;
                continue;
            }
            r.sound.resize(ch * n);
            r.line.readMapped(channelMap(map, ch, r.line.channels()), window.first, n, r.sound.data());
            if ((delay != r.delay || map != r.map) && r.line.started())
            {
                // From the old reading to the new one over this grain (S-shaped, no click).
                r.before.resize(ch * n);
                r.line.readMapped(channelMap(r.map, ch, r.line.channels()), delayedSamples(cfg.rate, index, cfg.latency, r.delay).first, n, r.before.data());
                for (std::size_t c = 0; c < ch; ++c)
                {
                    crossfadeSmooth(r.before.data() + c * n, r.sound.data() + c * n, n, r.sound.data() + c * n);
                }
            }
            r.delay = delay;
            r.map = map;
            bool const held = r.reader != nullptr && r.line.holds(window.first, window.end);
            if (!r.live && r.line.started() && window.end > r.line.start() && window.end > r.firstIndex)
            {
                // A new route fades in over its first grain of sound.
                for (std::size_t c = 0; c < ch; ++c)
                {
                    float* d = r.sound.data() + c * n;
                    for (std::size_t s = 0; s < n; ++s)
                    {
                        d[s] *= static_cast<float>(s + 1) / static_cast<float>(n);
                    }
                }
                r.live = true;
            }
            if (held)
            {
                ++in.audioBlocks;
            }
            else
            {
                // Before the first sample of the route (its delay at the start) or in the start-up
                // window: start-up; later: missing (silence).
                bool const startup = index <= in.audioGraceUntil.load() || (r.firstIndex != MXL_UNDEFINED_INDEX && window.first < r.firstIndex);
                ++(startup ? in.audioStartup : in.audioMissing);
            }
            sources.inputs[k] = r.sound.data();
        }
        for (std::size_t k = 0; k < job.players.size(); ++k)
        {
            sources.players.push_back(own && !job.players[k].empty() ? job.players[k].data() : nullptr);
        }
        for (std::size_t k = 0; k < job.stingers.size(); ++k)
        {
            sources.stingers.push_back(own && !job.stingers[k].empty() ? job.stingers[k].data() : nullptr);
        }
        audioMix->grain(job.plans, sources, n, audio.me);
        // Meters, all on the same grid of 100 ms blocks of the output's sample index.
        bool finished = false;
        for (std::size_t k = 0; k < audio.inputMeters.size(); ++k)
        {
            finished = audio.inputMeters[k].add(sources.inputs[k], n, out.first) || finished;
        }
        for (std::size_t k = 0; k < audio.playerMeters.size(); ++k)
        {
            finished = audio.playerMeters[k].add(k < sources.players.size() ? sources.players[k] : nullptr, n, out.first) || finished;
        }
        for (std::size_t k = 0; k < audio.stingerMeters.size(); ++k)
        {
            finished = audio.stingerMeters[k].add(k < sources.stingers.size() ? sources.stingers[k] : nullptr, n, out.first) || finished;
        }
        for (std::size_t m = 0; m < audio.meMeters.size(); ++m)
        {
            if (loudnessReset[m]->exchange(false))
            {
                audio.meMeters[m].resetLoudness();
            }
            finished = audio.meMeters[m].add(audio.me[m].size() == ch * n ? audio.me[m].data() : nullptr, n, out.first) || finished;
        }
        if (finished)
        {
            publishMeters(audio);
        }
        for (auto const& plan : job.plans)
        {
            auto const& dst = audio.me[static_cast<std::size_t>(plan.me - 1)];
            auto& output = *audioOutputs[static_cast<std::size_t>(plan.me - 1)];
            mxlMutableWrappedMultiBufferSlice slice{};
            if (mxlFlowWriterOpenSamples(output.writer, out.end, n, &slice) != MXL_STATUS_OK)
            {
                ++output.failed;
                continue;
            }
            for (std::size_t c = 0; c < slice.count; ++c)
            {
                std::size_t filled = 0;
                for (auto const& fragment : slice.base.fragments)
                {
                    if (fragment.pointer == nullptr)
                    {
                        continue;
                    }
                    std::size_t const m = std::min(fragment.size / sizeof(float), n - filled);
                    auto* target = reinterpret_cast<float*>(static_cast<std::uint8_t*>(fragment.pointer) + c * slice.stride);
                    if (c < ch)
                    {
                        std::memcpy(target, dst.data() + c * n + filled, m * sizeof(float));
                    }
                    else
                    {
                        std::fill(target, target + m, 0.f);
                    }
                    filled += m;
                }
            }
            if (mxlFlowWriterCommitSamples(output.writer) == MXL_STATUS_OK)
            {
                ++output.committed;
            }
            else
            {
                ++output.failed;
            }
        }
    }

    void publishMeters(AudioRun const& audio)
    {
        MeterSnapshot snap;
        snap.valid = true;
        auto const take = [](std::vector<Meter> const& list, std::vector<MeterValues>& into) {
            for (auto const& meter : list)
            {
                into.push_back(meter.values());
            }
        };
        take(audio.inputMeters, snap.inputs);
        take(audio.playerMeters, snap.players);
        take(audio.stingerMeters, snap.stingers);
        take(audio.meMeters, snap.mes);
        snap.block = snap.mes.empty() ? 0 : snap.mes.front().block;
        for (int m = 1; m <= cfg.mes; ++m)
        {
            snap.stripRefs.push_back(audioMix->strips(m));
            snap.strips.push_back(audioMix->stripStates(m));
        }
        std::lock_guard lock{meterMu};
        meters = std::move(snap);
    }

    // fx-writer, after the video commit of a grain: its job for fx-audio (the plans, and the sound
    // of the clip and stinger players' frames). When fx-audio is kAudioJobs behind, the grain is
    // dropped here and the next job covers it.
    void queueAudio(Slot const& slot)
    {
        std::unique_ptr<AudioJob> job;
        {
            std::lock_guard lock{audioMu};
            if (audioQueue.size() >= kAudioJobs)
            {
                ++audioJobsDropped;
                return;
            }
            if (!audioFree.empty())
            {
                job = std::move(audioFree.back());
                audioFree.pop_back();
            }
        }
        if (!job)
        {
            job = std::make_unique<AudioJob>();
        }
        auto const span = grainSamples(cfg.rate, slot.index);
        std::size_t const n = span.end - span.first;
        job->index = slot.index;
        job->plans = slot.plans;
        job->players.resize(players.size());
        job->stingers.resize(stingers.size());
        for (std::size_t k = 0; k < players.size(); ++k)
        {
            auto const& frame = k < slot.players.size() ? slot.players[k] : nullptr;
            if (frame && frame->index == slot.index)
            {
                // A clip player: the sound of the frame it shows in this grain.
                copyPlayerAudio(*frame, job->players[k], n, audioChannels);
            }
            else
            {
                job->players[k].clear();
            }
        }
        for (std::size_t k = 0; k < stingers.size(); ++k)
        {
            auto const& frame = k < slot.stingers.size() ? slot.stingers[k] : nullptr;
            if (frame && frame->index == slot.index && frame->samples > 0)
            {
                copyPlayerAudio(*frame, job->stingers[k], n, audioChannels);
            }
            else
            {
                job->stingers[k].clear();
            }
        }
        {
            std::lock_guard lock{audioMu};
            audioQueue.push_back(std::move(job));
        }
        audioCv.notify_one();
    }

    // Audio for every grain since the last one: a skipped video grain (or a dropped job) leaves no
    // hole (a hole would replay old samples from the ring). Every routed input's reader is kept
    // open and its lag measured.
    void runAudio(AudioJob const& job, AudioRun& audio)
    {
        auto const started = std::chrono::steady_clock::now();
        for (std::size_t k = 0; k < inputs.size(); ++k)
        {
            if (openAudio(*inputs[k], audio.readers[k]))
            {
                measureAudio(*inputs[k], audio.readers[k]);
            }
        }
        std::uint64_t first = job.index;
        if (audio.last != 0 && job.index > audio.last && job.index - audio.last <= 25)
        {
            first = audio.last + 1;
        }
        for (std::uint64_t k = first; k <= job.index; ++k)
        {
            audioGrain(k, job, audio);
        }
        audio.last = job.index;
        audioMs.add(std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count());
    }

    void audioMain()
    {
        nameThread("fx-audio");
        AudioRun audio;
        audio.readers.resize(inputs.size());
        audio.me.resize(audioOutputs.size());
        audio.channels = audioChannels;
        audio.inputMeters.assign(inputs.size(), Meter(audioChannels, false));
        audio.playerMeters.assign(players.size(), Meter(audioChannels, false));
        audio.stingerMeters.assign(stingers.size(), Meter(audioChannels, false));
        audio.meMeters.assign(audioOutputs.size(), Meter(audioChannels, true));
        // Open the routed audio before the first grain.
        for (std::size_t k = 0; k < inputs.size(); ++k)
        {
            openAudio(*inputs[k], audio.readers[k]);
        }
        while (true)
        {
            std::unique_ptr<AudioJob> job;
            {
                std::unique_lock lock{audioMu};
                audioCv.wait(lock, [&] { return !audioQueue.empty() || !run.load(); });
                if (audioQueue.empty())
                {
                    break;
                }
                job = std::move(audioQueue.front());
                audioQueue.pop_front();
            }
            runAudio(*job, audio);
            std::lock_guard lock{audioMu};
            audioFree.push_back(std::move(job));
        }
        for (auto& reader : audio.readers)
        {
            closeAudio(reader);
            if (reader.instance != nullptr)
            {
                mxlDestroyInstance(reader.instance);
            }
        }
    }

    // Page-locks every grain of every writer before the first output grain: locking a grain
    // takes milliseconds the first time, which made the first output grains late. Opening an
    // old index and cancelling it leaves the new flow as it was.
    void lockWriterGrains(HostRegistry& registry)
    {
        auto const now = currentIndex();
        for (auto& output : outputs)
        {
            for (std::uint64_t k = 0; k < output->grainCount && k < now; ++k)
            {
                mxlGrainInfo info{};
                std::uint8_t* payload = nullptr;
                if (mxlFlowWriterOpenGrain(output->writer, now - output->grainCount + k, &info, &payload) == MXL_STATUS_OK && payload != nullptr)
                {
                    registry.ensure(payload, frameBytes, false);
                }
                mxlFlowWriterCancelGrain(output->writer);
            }
        }
    }

    void writerMain()
    {
        nameThread("fx-writer");
        cudaSetDevice(cfg.gpu);
        HostRegistry registry;
        auto const started = std::chrono::steady_clock::now();
        lockWriterGrains(registry);
        logInfo("writer_grains_locked", {{"grains", registry.lockedCount()},
                                            {"ms", std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - started).count()}});
        writerWarm.set_value();
        while (true)
        {
            int k = 0;
            {
                std::unique_lock lock{writeMu};
                writeCv.wait(lock, [&] { return !writeQueue.empty() || !run.load(); });
                if (writeQueue.empty())
                {
                    break;
                }
                k = writeQueue.front();
                writeQueue.pop_front();
            }
            write(slots[static_cast<std::size_t>(k)], registry);
        }
        cudaStreamSynchronize(downloadStream);
        registry.release();
    }

    // ---- grab thread -----------------------------------------------------------------------

    // fx-grab: each copied grab as raw v210 (written to path.part, then renamed), off the render
    // and writer threads.
    void grabMain()
    {
        nameThread("fx-grab");
        while (true)
        {
            std::shared_ptr<Grab> grab;
            {
                std::unique_lock lock{grabMu};
                grabCv.wait(lock, [&] { return !grabWrite.empty() || !run.load(); });
                if (grabWrite.empty())
                {
                    break;
                }
                grab = grabWrite.front();
                grabWrite.pop_front();
            }
            std::string const part = grab->path + ".part";
            std::FILE* file = std::fopen(part.c_str(), "wb");
            bool ok = file != nullptr && std::fwrite(grab->host, 1, frameBytes, file) == frameBytes;
            if (file != nullptr)
            {
                ok = std::fclose(file) == 0 && ok;
            }
            ok = ok && std::rename(part.c_str(), grab->path.c_str()) == 0;
            int const failure = errno;
            if (!ok)
            {
                std::remove(part.c_str());
            }
            double const ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - grab->requested).count();
            {
                std::lock_guard lock{grabMu};
                grab->state = ok ? "done" : "failed";
                grab->error = ok ? "" : "cannot write " + grab->path + ": " + std::strerror(failure);
                grab->ms = ms;
                grabFree.push_back(grab->host);
                grab->host = nullptr;
            }
            if (!ok)
            {
                ++grabsFailed;
                logWarn("grab_failed", {{"grab", grab->id}, {"path", grab->path}, {"error", std::strerror(failure)}});
                continue;
            }
            auto const kind = std::string(grab->kind());
            ++(kind == "input" ? grabsInput : kind == "player" ? grabsPlayer : kind == "pgm" ? grabsPgm : grabsPvw);
            grabMs.add(ms);
            logInfo("grab", {{"grab", grab->id}, {"source", sourceName(grab->source)}, {"bus", kind}, {"index", grab->index}, {"ms", round3(ms)}});
        }
    }

    // ---- encode and stats threads --------------------------------------------------------

    void encodeMain()
    {
        nameThread("fx-encode");
        cudaSetDevice(cfg.gpu);
        cudaStream_t stream = nullptr;
        cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking);
        std::size_t const lumaBytes = static_cast<std::size_t>(cfg.width) * static_cast<std::size_t>(cfg.height);
        while (true)
        {
            int k = 0;
            {
                std::unique_lock lock{encodeMu};
                encodeCv.wait(lock, [&] { return !encodeQueue.empty() || !run.load(); });
                if (encodeQueue.empty())
                {
                    break;
                }
                k = encodeQueue.front();
                encodeQueue.pop_front();
            }
            auto& slot = mosaicSlots[static_cast<std::size_t>(k)];
            cudaEventSynchronize(slot.ready);
            auto const started = std::chrono::steady_clock::now();
            encoder->encode(slot.nv12, cfg.width, slot.nv12 + lumaBytes, cfg.width, slot.pts, stream);
            encodeMs.add(std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count());
            slot.busy.store(false);
        }
        cudaStreamDestroy(stream);
    }

    void statsMain()
    {
        nameThread("fx-stats");
        cudaSetDevice(cfg.gpu);
        Nvml nvml(pciBus);
        ThreadCpu threadCpu;
        std::uint64_t lastCommitted = committed.load();
        auto lastAt = std::chrono::steady_clock::now();
        std::unique_lock lock{statsMu};
        while (run.load())
        {
            // The own MediaMTX every 200 ms (a killed one is started again at once), the rest once a
            // second.
            for (int k = 0; k < 5 && run.load(); ++k)
            {
                statsCv.wait_for(lock, std::chrono::milliseconds(200), [&] { return !run.load(); });
                if (mediamtx && run.load())
                {
                    mediamtxRunning.store(mediamtx->poll());
                }
            }
            auto const cpuNow = threadCpu.sample();
            auto const gpuNow = nvml.sample();
            std::size_t freeBytes = 0;
            std::size_t totalBytes = 0;
            cudaMemGetInfo(&freeBytes, &totalBytes);
            auto const now = std::chrono::steady_clock::now();
            auto const count = committed.load();
            double const seconds = std::chrono::duration<double>(now - lastAt).count();
            std::lock_guard snap{snapMu};
            cpu = cpuNow;
            gpuSample = gpuNow;
            deviceUsed = totalBytes - freeBytes;
            fps = seconds > 0 ? static_cast<double>(count - lastCommitted) / seconds : 0.0;
            lastCommitted = count;
            lastAt = now;
        }
    }

    // ---- start / stop --------------------------------------------------------------------

    void start()
    {
        setupGpu();
        setupOutputs();
        // P5 audio: the PGM's channels are its flows' (an existing flow keeps its own); a delay line
        // holds the longest delay and L + 3 grains.
        audioChannels = 1;
        for (auto const& output : audioOutputs)
        {
            audioChannels = std::max<std::size_t>(audioChannels, output->channels);
        }
        audioMix = std::make_unique<AudioMix>(cfg.mes, static_cast<int>(inputs.size()), static_cast<int>(players.size()), audioChannels);
        for (int m = 0; m < cfg.mes; ++m)
        {
            loudnessReset.push_back(std::make_unique<std::atomic<bool>>(false));
        }
        auto const grainMax = static_cast<std::size_t>((kAudioRate * cfg.rate.den + cfg.rate.num - 1) / cfg.rate.num) + 1;
        delayLineLength = static_cast<std::size_t>(cfg.audioMaxDelayMs) * static_cast<std::size_t>(kAudioRate / 1000) +
            static_cast<std::size_t>(cfg.latency + 3) * grainMax;
        if (cfg.mosaic && cfg.ownMediamtx())
        {
            mediamtx = std::make_unique<MediamtxProcess>(cfg);
            mediamtx->start();
            mediamtxRunning.store(true);
        }
        run.store(true);
        started = true;
        startIndex.store(currentIndex());
        for (auto* list : {&inputs, &keys})
        {
            for (auto& input : *list)
            {
                Input* in = input.get();
                // The configured route counts as a change (the thread opens it).
                in->videoGen.fetch_add(1);
                in->audioGen.fetch_add(1);
                in->thread = std::thread([this, in] { inputMain(*in); });
            }
        }
        for (auto* list : {&players, &stingers})
        {
            for (auto& player : *list)
            {
                player->start();
            }
        }
        // The render thread starts once the writer has page-locked its grains.
        auto warm = writerWarm.get_future();
        writerThread = std::thread([this] { writerMain(); });
        warm.wait();
        audioThread = std::thread([this] { audioMain(); });
        if (encoder)
        {
            encodeThread = std::thread([this] { encodeMain(); });
        }
        statsThread = std::thread([this] { statsMain(); });
        grabThread = std::thread([this] { grabMain(); });
        renderThread = std::thread([this] { renderMain(); });
        logInfo("engine_started", {{"gpu", gpuName}, {"inputs", inputs.size()}, {"players", players.size()}, {"stinger_players", stingers.size()},
                                      {"keys", keys.size()}, {"mes", cfg.mes},
                                      {"latency", cfg.latency}, {"pvw_outputs", cfg.pvwOutputs}, {"work_format", cfg.workFormat},
                                      {"device_mb", allocated / 1048576}});
    }

    void stop()
    {
        if (!started)
        {
            return;
        }
        started = false;
        run.store(false);
        writeCv.notify_all();
        encodeCv.notify_all();
        statsCv.notify_all();
        audioCv.notify_all();
        // Render first (no new grains), then the writer drains its queue, then fx-audio its grains,
        // then the grabs the writer copied are written.
        for (auto* thread : {&renderThread, &writerThread, &audioThread, &encodeThread, &statsThread})
        {
            if (thread->joinable())
            {
                thread->join();
            }
        }
        grabCv.notify_all();
        if (grabThread.joinable())
        {
            grabThread.join();
        }
        for (auto* list : {&inputs, &keys})
        {
            for (auto& input : *list)
            {
                input->routed.notify_all();
                if (input->thread.joinable())
                {
                    input->thread.join();
                }
                input->recent.clear();
            }
        }
        for (auto& slot : slots)
        {
            slot.held.clear();
            slot.players.clear();
            slot.stingers.clear();
        }
        for (auto* list : {&players, &stingers})
        {
            for (auto& player : *list)
            {
                player->stop();
            }
        }
        // The last writer's release deletes a flow (MXL): its readers see it again when the
        // next writer (this engine or the mixer it replaced) creates it.
        for (auto* list : {&outputs, &audioOutputs})
        {
            for (auto& output : *list)
            {
                if (output->writer != nullptr)
                {
                    mxlReleaseFlowWriter(output->instance, output->writer);
                    output->writer = nullptr;
                }
            }
        }
        for (auto& [domain, instance] : outInstances)
        {
            mxlDestroyInstance(instance);
        }
        outInstances.clear();
        encoder.reset();
        if (mediamtx)
        {
            mediamtx->stop();
            mediamtx.reset();
        }
        cudaDeviceSynchronize();
        for (auto* list : {&srcWork, &pgmWork, &pvwWork})
        {
            for (auto& buffer : *list)
            {
                gpu::freeWork(buffer);
            }
        }
        gpu::freeWork(blackWork);
        gpu::freeDmeScratch(dmeScratch);
        for (auto* buffer : boxBuffers)
        {
            if (buffer != nullptr)
            {
                cudaFree(buffer);
            }
        }
        boxBuffers.clear();
        for (auto* p : keyPlanes)
        {
            if (p != nullptr)
            {
                cudaFree(p);
            }
        }
        keyPlanes.clear();
        for (auto& slot : slots)
        {
            for (auto* out : slot.out)
            {
                cudaFree(out);
            }
            for (auto* list : {&slot.mark, &slot.boxed})
            {
                for (auto event : *list)
                {
                    cudaEventDestroy(event);
                }
            }
            for (auto event : slot.downloaded)
            {
                cudaEventDestroy(event);
            }
            cudaEventDestroy(slot.downloadStart);
            cudaEventDestroy(slot.grabbed);
            slot.grabs.clear();
        }
        {
            std::lock_guard lock{grabMu};
            for (auto const& grab : grabRecent)
            {
                if (grab->state != "done" && grab->state != "failed")
                {
                    grab->state = "failed";
                    grab->error = "the engine stopped";
                }
                grab->host = nullptr;
            }
            grabPending.clear();
            grabWrite.clear();
            grabFree.clear();
            for (auto* buffer : grabBuffers)
            {
                cudaFreeHost(buffer);
            }
            grabBuffers.clear();
        }
        for (auto& slot : mosaicSlots)
        {
            cudaFree(slot.nv12);
            if (slot.ready != nullptr)
            {
                cudaEventDestroy(slot.ready);
            }
        }
        cudaFree(black);
        inputs.clear();
        keys.clear();
        players.clear();
        stingers.clear();
        cudaStreamDestroy(renderStream);
        cudaStreamDestroy(downloadStream);
        logInfo("engine_stopped", {{"rendered", rendered.load()}, {"committed", committed.load()}, {"skipped", skipped.load()}});
    }

    // ---- status --------------------------------------------------------------------------

    json inputJson(Input const& in, std::uint64_t now) const
    {
        std::string state;
        std::string audioState;
        Route video;
        Route audioRoute;
        std::uint32_t delay = 0;
        std::vector<int> map;
        {
            std::lock_guard lock{in.mu};
            state = in.state;
            audioState = in.audioState;
            video = in.video;
            audioRoute = in.audio;
            delay = in.audioDelay;
            map = in.audioMap;
        }
        auto const lastAt = in.lastGrainAt.load();
        // No grain for a second: shown as no_signal (the picture holds, see holdGrains).
        if (state == "running" && (lastAt == 0 || now > lastAt + 1'000'000'000ull))
        {
            state = "no_signal";
        }
        // Audio: its head stood still for a second.
        auto const movedAt = in.audioMovedAt.load();
        if (audioState == "running" && (movedAt == 0 || now > movedAt + 1'000'000'000ull))
        {
            audioState = "no_signal";
        }
        json item{{in.key ? "key" : "input", in.number},
            {"name", in.name},
            {"label", in.label},
            {"route", routeJson(video)},
            {"state", state},
            {"grains", in.grains.load()},
            {"late", in.late.load()},
            {"missing", in.missing.load()},
            {"startup", in.startup.load()},
            {"resyncs", in.resyncs.load()},
            {"catchups", in.catchups.load()},
            {"invalid", in.invalid.load()},
            {"reopens", in.reopens.load()},
            {"pool_exhausted", in.exhausted.load()},
            {"unpinned_uploads", in.unpinned.load()},
            {"upload_ms", in.upload.toJson()},
            {"arrival_ms", in.arrival.toJson(true)}};
        if (!in.key)
        {
            item["audio"] = {{"route", routeJson(audioRoute)},
                {"state", audioState},
                {"blocks", in.audioBlocks.load()},
                {"missing", in.audioMissing.load()},
                {"startup", in.audioStartup.load()},
                {"waits", in.audioWaits.load()},
                {"reopens", in.audioReopens.load()},
                {"lag_ms", in.audioLag.toJson(true)},
                {"heard", in.audioHeard.load()},
                {"delay_ms", round3(static_cast<double>(delay) * 1000.0 / static_cast<double>(kAudioRate))},
                {"map", map},
                {"channels", in.audioChannels.load()},
                {"held_ms", round3(static_cast<double>(in.audioHeld.load()) * 1000.0 / static_cast<double>(kAudioRate))}};
        }
        return item;
    }

    // Per ME its master and strips (settings, and each strip's gain and whether it is heard after the
    // last grain), per live input its delay, map and delay line.
    json audioSettings() const
    {
        json mes = json::array();
        for (int m = 1; audioMix && m <= cfg.mes; ++m)
        {
            json strips = json::object();
            auto const refs = audioMix->strips(m);
            auto const states = audioMix->stripStates(m);
            for (std::size_t s = 0; s < refs.size(); ++s)
            {
                auto const set = audioMix->strip(m, refs[s]).value_or(StripSettings{});
                json item{{"fader_db", set.faderDb}, {"mute", set.mute}};
                if (refs[s].kind != StripRef::Kind::Stinger)
                {
                    item["afv"] = set.afv;
                }
                item["gain_db"] = round3(gainToDb(s < states.size() ? states[s].gain : 0.f));
                item["heard"] = s < states.size() && states[s].heard;
                strips[stripName(refs[s])] = item;
            }
            mes.push_back({{"me", m}, {"master_db", audioMix->master(m)}, {"strips", strips}});
        }
        json list = json::array();
        for (auto const& input : inputs)
        {
            std::lock_guard lock{input->mu};
            list.push_back({{"input", input->number}, {"name", input->name},
                {"delay_ms", round3(static_cast<double>(input->audioDelay) * 1000.0 / static_cast<double>(kAudioRate))},
                {"delay_samples", input->audioDelay}, {"map", input->audioMap}, {"channels", input->audioChannels.load()},
                {"held_ms", round3(static_cast<double>(input->audioHeld.load()) * 1000.0 / static_cast<double>(kAudioRate))}});
        }
        return json{{"channels", audioChannels}, {"max_delay_ms", cfg.audioMaxDelayMs}, {"mes", mes}, {"inputs", list}};
    }

    // GET /audio/meters (docs/engine-p5.md §7): the last finished 100 ms block. Sources: in<k>
    // (after the delay and map), cp<k>, st<k> (stinger players): peak and RMS per channel. MEs:
    // also the true peak, the loudness per channel pair, the overs and per strip its gain and
    // whether it is heard. dB values below −120 are −120.
    json metersJson() const
    {
        MeterSnapshot snap;
        {
            std::lock_guard lock{meterMu};
            snap = meters;
        }
        if (!snap.valid)
        {
            return json{{"block", nullptr}, {"channels", audioChannels}, {"sources", json::object()}, {"mes", json::object()}};
        }
        auto const db = [](std::vector<float> const& values) {
            json out = json::array();
            for (float v : values)
            {
                out.push_back(round2(levelDb(v)));
            }
            return out;
        };
        json sources = json::object();
        auto const add = [&](std::vector<MeterValues> const& list, char const* prefix) {
            for (std::size_t k = 0; k < list.size(); ++k)
            {
                sources[prefix + std::to_string(k + 1)] = {{"peak_dbfs", db(list[k].peak)}, {"rms_dbfs", db(list[k].rms)}};
            }
        };
        add(snap.inputs, "in");
        add(snap.players, "cp");
        add(snap.stingers, "st");
        json mes = json::object();
        for (std::size_t m = 0; m < snap.mes.size(); ++m)
        {
            auto const& v = snap.mes[m];
            json loudness = json::array();
            for (std::size_t p = 0; p < v.loudness.size(); ++p)
            {
                loudness.push_back({{"pair", p + 1}, {"momentary", round2(v.loudness[p].momentary)}, {"short_term", round2(v.loudness[p].shortTerm)},
                    {"integrated", round2(v.loudness[p].integrated)}});
            }
            json strips = json::object();
            if (m < snap.stripRefs.size())
            {
                for (std::size_t s = 0; s < snap.stripRefs[m].size() && s < snap.strips[m].size(); ++s)
                {
                    strips[stripName(snap.stripRefs[m][s])] = {{"gain_db", round2(gainToDb(snap.strips[m][s].gain))}, {"heard", snap.strips[m][s].heard}};
                }
            }
            mes[std::to_string(m + 1)] = {{"peak_dbfs", db(v.peak)}, {"true_peak_dbtp", db(v.truePeak)}, {"rms_dbfs", db(v.rms)}, {"loudness", loudness},
                {"overs", v.overs}, {"strips", strips}};
        }
        return json{{"block", snap.block}, {"channels", audioChannels}, {"sources", sources}, {"mes", mes}};
    }

    json audioStatus() const
    {
        auto out = audioSettings();
        out["ms_per_grain"] = audioMs.toJson();
        out["wait_ms"] = audioWaitMs.toJson();
        out["max_wait_ms"] = cfg.audioWaitMs;
        out["jobs_dropped"] = audioJobsDropped.load();
        return out;
    }

    // Live input k's delay and channel map (control thread); fx-audio applies a change with a
    // crossfade over one grain.
    std::string setInputAudio(int k, std::optional<double> delayMs, std::optional<std::vector<int>> map)
    {
        if (k < 1 || k > static_cast<int>(inputs.size()))
        {
            return "no input " + std::to_string(k);
        }
        std::uint32_t samples = 0;
        if (delayMs)
        {
            if (!(*delayMs >= 0.0 && *delayMs <= static_cast<double>(cfg.audioMaxDelayMs)))
            {
                return "delay_ms must be 0.." + std::to_string(cfg.audioMaxDelayMs);
            }
            samples = static_cast<std::uint32_t>(std::llround(*delayMs * static_cast<double>(kAudioRate) / 1000.0));
        }
        if (map)
        {
            if (!map->empty() && map->size() != audioChannels)
            {
                return "map needs one entry per PGM channel (" + std::to_string(audioChannels) + "), or none for 1:1";
            }
            for (int v : *map)
            {
                if (v < 0 || v > 64)
                {
                    return "map entries are input channels 1..64, or 0 for silence";
                }
            }
        }
        auto& in = *inputs[static_cast<std::size_t>(k - 1)];
        std::lock_guard lock{in.mu};
        if (delayMs)
        {
            in.audioDelay = samples;
        }
        if (map)
        {
            in.audioMap = *map;
        }
        return {};
    }

    json status() const
    {
        json out;
        out["engine"] = {{"version", FXENG_VERSION},
            {"format", cfg.format},
            {"latency_grains", cfg.latency},
            {"mes", cfg.mes},
            {"inputs", inputs.size()},
            {"players", players.size()},
            {"stinger_players", stingers.size()},
            {"keys", keys.size()},
            {"pvw_outputs", cfg.pvwOutputs},
            {"work_format", cfg.workFormat},
            {"output_domain", cfg.outputDomain},
            {"gpu", {{"index", cfg.gpu}, {"name", gpuName}, {"pci_bus", pciBus}}},
            {"device_allocated_mb", round3(static_cast<double>(allocated) / 1048576.0)}};
        double fpsNow = 0;
        ThreadCpu::Result cpuNow;
        GpuSample gpuNow;
        std::uint64_t used = 0;
        {
            std::lock_guard lock{snapMu};
            fpsNow = fps;
            cpuNow = cpu;
            gpuNow = gpuSample;
            used = deviceUsed;
        }
        out["output"] = {{"index", lastIndex.load()},
            {"fps", round3(fpsNow)},
            {"rendered", rendered.load()},
            {"committed", committed.load()},
            {"skipped", skipped.load()},
            {"writer_busy", writerBusy.load()},
            {"late", late.load()},
            {"write_failed", writeFailed.load()},
            {"unpinned_downloads", unpinned.load()},
            {"lag_grains", lagGrains.toJson()},
            {"commit_ms", commitMs.toJson()}};
        json me = json::object();
        json dme = json::object();
        for (std::size_t m = 0; m < meMs.size(); ++m)
        {
            me[std::to_string(m + 1)] = meMs[m]->toJson();
            dme[std::to_string(m + 1)] = dmeMs[m]->toJson();
        }
        Window::Value uploadSum;
        for (auto const& input : inputs)
        {
            auto const v = input->upload.value();
            uploadSum.avg += v.avg;
            uploadSum.max = std::max(uploadSum.max, v.max);
        }
        out["gpu_ms"] = {{"upload_per_input", {{"avg", round3(inputs.empty() ? 0.0 : uploadSum.avg / static_cast<double>(inputs.size()))}, {"max", round3(uploadSum.max)}}},
            {"upload_all_inputs", round3(uploadSum.avg)},
            {"unpack", unpackMs.toJson()},
            {"me", me},
            {"dme", dme},
            {"pack", packMs.toJson()},
            {"mosaic", mosaicMs.toJson()},
            {"download", downloadMs.toJson()},
            {"render", renderMs.toJson()},
            {"total", totalMs.toJson()},
            {"span", spanMs.toJson()}};
        auto const now = mxlGetTime();
        json list = json::array();
        for (auto const& input : inputs)
        {
            list.push_back(inputJson(*input, now));
        }
        out["inputs"] = list;
        // Per keyer its key flow's state (FlowXer's view), and per key flow in use its reader.
        std::vector<int> slots;
        {
            std::lock_guard lock{keyMu};
            slots = keySlot;
        }
        json keyList = json::array();
        json flowList = json::array();
        for (std::size_t k = 0; k < slots.size(); ++k)
        {
            json item{{"key", k + 1}, {"label", cfg.keys[k].label}, {"state", "not_routed"}, {"flow_slot", nullptr}};
            if (slots[k] >= 0)
            {
                auto const flow = inputJson(*keys[static_cast<std::size_t>(slots[k])], now);
                item["state"] = flow["state"];
                item["arrival_ms"] = flow["arrival_ms"];
                item["route"] = flow["route"];
                item["flow_slot"] = slots[k] + 1;
            }
            keyList.push_back(item);
        }
        for (std::size_t s = 0; s < keys.size(); ++s)
        {
            if (std::find(slots.begin(), slots.end(), static_cast<int>(s)) != slots.end())
            {
                flowList.push_back(inputJson(*keys[s], now));
            }
        }
        out["keys"] = keyList;
        out["key_flows"] = flowList;
        // Grain i - L must be there when the render of grain i starts, half a grain before its TAI time.
        out["engine"]["recommended_latency_grains"] = recommendedLatency(out, periodMs());
        json playerList = json::array();
        for (auto const& player : players)
        {
            playerList.push_back(json::parse(player->statusJson()));
        }
        out["players"] = playerList;
        json stingerList = json::array();
        for (auto const& player : stingers)
        {
            stingerList.push_back(json::parse(player->statusJson()));
        }
        out["stingers"] = {{"players", stingerList}, {"played", mixer.stingersPlayed()}, {"waits", mixer.stingerWaits()}, {"cuts", mixer.stingerCuts()}};
        json mes = json::array();
        auto const flow = [](Output const& o) {
            return json{{"flow", o.id}, {"domain", o.domain}, {"label", o.label}, {"existing", o.existing}, {"committed", o.committed.load()},
                {"failed", o.failed.load()}};
        };
        for (auto const& view : mixer.view())
        {
            Output const* pgm = nullptr;
            for (auto const& output : outputs)
            {
                if (output->me == view.me && !output->preview)
                {
                    pgm = output.get();
                }
            }
            auto const* pvw = pvwOf[static_cast<std::size_t>(view.me - 1)];
            auto const& aud = *audioOutputs[static_cast<std::size_t>(view.me - 1)];
            json pgmAudio = flow(aud);
            pgmAudio["channels"] = aud.channels;
            mes.push_back({{"me", view.me},
                {"program", sourceName(view.program)},
                {"preview", sourceName(view.preview)},
                {"mixing", view.mixing},
                {"wiping", view.wiping},
                {"ftb", round3(mixer.ftbGain(view.me))},
                {"manual", view.manual},
                {"t", round3(view.t)},
                {"stinger", view.stinger},
                {"start", view.start},
                {"ties", view.next.ties},
                {"tied", view.tied},
                {"next_transition", transitionName(view.next.type)},
                {"target", sourceName(view.target)},
                {"frames", view.frames},
                {"position", view.position},
                {"pgm", pgm != nullptr ? flow(*pgm) : json(nullptr)},
                {"pvw", pvw != nullptr ? flow(*pvw) : json(nullptr)},
                {"pgm_audio", pgmAudio}});
        }
        out["mes"] = mes;
        json keyers = json::array();
        for (auto const& key : mixer.keyView())
        {
            keyers.push_back(keyerJson(key));
        }
        out["keyers"] = keyers;
        json mosaic = {{"enabled", cfg.mosaic}, {"fps", cfg.mosaicFps}, {"frames", mosaicFrames.load()}, {"dropped", mosaicDropped.load()}, {"encode_ms", encodeMs.toJson()}};
        // The preview contract: where each stream goes and whether it is being published.
        json streams = json::array();
        if (encoder)
        {
            auto const s = encoder->stats();
            mosaic["encoder"] = {{"open", s.open}, {"connected", s.connected}, {"frames", s.frames}, {"bytes", s.bytes}, {"connects", s.connects},
                {"errors", s.errors}, {"last_error", s.lastError}};
            streams.push_back({{"stream", "mosaic"}, {"path", cfg.streamPath("mosaic")}, {"url", cfg.rtspBase() + "/" + cfg.streamPath("mosaic")},
                {"publishing", s.connected}, {"connects", s.connects}, {"last_error", s.lastError}});
        }
        out["preview"] = {{"mode", cfg.ownMediamtx() ? "own" : "shared"},
            {"publish_url", cfg.rtspBase()},
            {"path_prefix", cfg.pathPrefix},
            {"whep_base", cfg.whepBase},
            {"hls_base", cfg.hlsBase},
            {"own_mediamtx", mediamtx ? json{{"running", mediamtxRunning.load()}, {"restarts", mediamtx->restarts()}} : json(nullptr)},
            {"streams", streams}};
        out["mosaic"] = mosaic;
        out["audio"] = audioStatus();
        {
            std::lock_guard lock{grabMu};
            out["grabs"] = {{"total", {{"input", grabsInput.load()}, {"player", grabsPlayer.load()}, {"pgm", grabsPgm.load()}, {"pvw", grabsPvw.load()}}},
                {"failed", grabsFailed.load()}, {"pending", grabPending.size()}, {"buffers", grabBuffers.size()}, {"ms", grabMs.toJson()}};
        }
        json threads = json::object();
        for (auto const& [name, percent] : cpuNow.threads)
        {
            threads[name] = round3(percent);
        }
        out["cpu"] = {{"process_percent", round3(cpuNow.process)}, {"threads_percent", threads}};
        out["gpu"] = {{"nvml", gpuNow.valid},
            {"utilization_percent", gpuNow.utilization},
            {"encoder_percent", gpuNow.encoder},
            {"encoder_sessions", gpuNow.encoderSessions},
            {"device_used_mb", round3(static_cast<double>(used) / 1048576.0)},
            {"device_total_mb", round3(static_cast<double>(gpuNow.memoryTotal) / 1048576.0)}};
        return out;
    }
};

Engine::Engine(Config config)
    : impl_(std::make_unique<Impl>(std::move(config)))
{
}

Engine::~Engine()
{
    stop();
}

void Engine::start()
{
    impl_->start();
}

void Engine::stop()
{
    impl_->stop();
}

Mixer& Engine::mixer()
{
    return impl_->mixer;
}

bool Engine::routeInput(int k, Route const& video, Route const& audio)
{
    if (k < 1 || k > static_cast<int>(impl_->inputs.size()))
    {
        return false;
    }
    return impl_->route(*impl_->inputs[static_cast<std::size_t>(k - 1)], video, &audio);
}

bool Engine::routeKey(int k, Route const& route)
{
    bool const ok = impl_->routeKeyer(k, route);
    // A linear box on another key flow: that flow's planes.
    impl_->prepareBox(k);
    return ok;
}

void Engine::prepareBox(int k)
{
    impl_->prepareBox(k);
}

std::string Engine::playerCommand(int k, PlayerCommand const& command)
{
    if (k < 1 || k > static_cast<int>(impl_->players.size()))
    {
        return "no clip player " + std::to_string(k);
    }
    return impl_->players[static_cast<std::size_t>(k - 1)]->command(command);
}

std::string Engine::stingerCommand(int k, PlayerCommand const& command)
{
    if (k < 1 || k > static_cast<int>(impl_->stingers.size()))
    {
        return "no stinger player " + std::to_string(k);
    }
    return impl_->stingers[static_cast<std::size_t>(k - 1)]->command(command);
}

int Engine::grab(std::string const& source, bool preview, std::string const& path, std::string& error)
{
    return impl_->requestGrab(source, preview, path, error);
}

std::string Engine::grabJson(int id) const
{
    return impl_->grabJson(id);
}

AudioMix& Engine::audioMix()
{
    return *impl_->audioMix;
}

std::string Engine::metersJson() const
{
    return impl_->metersJson().dump();
}

bool Engine::resetLoudness(int me)
{
    if (me < 1 || me > static_cast<int>(impl_->loudnessReset.size()))
    {
        return false;
    }
    impl_->loudnessReset[static_cast<std::size_t>(me - 1)]->store(true);
    return true;
}

std::string Engine::setInputAudio(int k, std::optional<double> delayMs, std::optional<std::vector<int>> map)
{
    return impl_->setInputAudio(k, delayMs, std::move(map));
}

std::string Engine::audioJson() const
{
    return impl_->audioSettings().dump(2);
}

long long Engine::stingerFrames(int k) const
{
    if (k < 1 || k > static_cast<int>(impl_->stingers.size()))
    {
        return -1;
    }
    return impl_->stingers[static_cast<std::size_t>(k - 1)]->frames();
}

std::string Engine::statusJson() const
{
    return impl_->status().dump(2);
}

std::string Engine::metricsText() const
{
    auto const s = impl_->status();
    std::ostringstream out;
    auto const metric = [&](std::string const& name, double value, std::string const& labels = {}) {
        out << "flowxer_engine_" << name;
        if (!labels.empty())
        {
            out << '{' << labels << '}';
        }
        out << ' ' << value << '\n';
    };
    auto const& o = s["output"];
    metric("output_fps", o["fps"].get<double>());
    metric("output_grains_rendered_total", o["rendered"].get<double>());
    metric("output_grains_committed_total", o["committed"].get<double>());
    metric("output_grains_skipped_total", o["skipped"].get<double>());
    metric("output_grains_late_total", o["late"].get<double>());
    metric("output_write_failed_total", o["write_failed"].get<double>());
    metric("output_lag_grains", o["lag_grains"]["avg"].get<double>(), "stat=\"avg\"");
    metric("output_lag_grains", o["lag_grains"]["max"].get<double>(), "stat=\"max\"");
    metric("output_commit_ms", o["commit_ms"]["avg"].get<double>(), "stat=\"avg\"");
    metric("output_commit_ms", o["commit_ms"]["max"].get<double>(), "stat=\"max\"");
    metric("latency_grains", s["engine"]["latency_grains"].get<double>());
    metric("recommended_latency_grains", s["engine"]["recommended_latency_grains"].get<double>());
    for (auto const& [stage, value] : s["gpu_ms"].items())
    {
        if (stage == "me")
        {
            for (auto const& [me, v] : value.items())
            {
                metric("gpu_stage_ms", v["avg"].get<double>(), "stage=\"me\",me=\"" + me + "\",stat=\"avg\"");
                metric("gpu_stage_ms", v["max"].get<double>(), "stage=\"me\",me=\"" + me + "\",stat=\"max\"");
            }
        }
        else if (stage == "dme")
        {
            // The resample of each ME's boxes (P4).
            for (auto const& [me, v] : value.items())
            {
                metric("dme_ms", v["avg"].get<double>(), "me=\"" + me + "\",stat=\"avg\"");
                metric("dme_ms", v["max"].get<double>(), "me=\"" + me + "\",stat=\"max\"");
            }
        }
        else if (value.is_object())
        {
            metric("gpu_stage_ms", value["avg"].get<double>(), "stage=\"" + stage + "\",stat=\"avg\"");
            metric("gpu_stage_ms", value["max"].get<double>(), "stage=\"" + stage + "\",stat=\"max\"");
        }
    }
    for (auto const* list : {&s["inputs"], &s["key_flows"]})
    {
        for (auto const& input : *list)
        {
            std::string const l = "input=\"" + input["name"].get<std::string>() + "\"";
            metric("input_grains_total", input["grains"].get<double>(), l);
            metric("input_late_total", input["late"].get<double>(), l);
            metric("input_missing_total", input["missing"].get<double>(), l);
            metric("input_resyncs_total", input["resyncs"].get<double>(), l);
            metric("input_reopens_total", input["reopens"].get<double>(), l);
            metric("input_running", input["state"] == "running" ? 1.0 : 0.0, l);
            metric("input_arrival_ms", input["arrival_ms"]["avg"].get<double>(), l + ",essence=\"video\",stat=\"avg\"");
            metric("input_arrival_ms", input["arrival_ms"]["max"].get<double>(), l + ",essence=\"video\",stat=\"max\"");
            metric("input_upload_ms", input["upload_ms"]["avg"].get<double>(), l + ",stat=\"avg\"");
            metric("input_upload_ms", input["upload_ms"]["max"].get<double>(), l + ",stat=\"max\"");
            if (input.contains("audio"))
            {
                auto const& a = input["audio"];
                metric("input_audio_blocks_total", a["blocks"].get<double>(), l);
                metric("input_audio_missing_total", a["missing"].get<double>(), l);
                metric("input_audio_waits_total", a["waits"].get<double>(), l);
                metric("input_arrival_ms", a["lag_ms"]["avg"].get<double>(), l + ",essence=\"audio\",stat=\"avg\"");
                metric("input_arrival_ms", a["lag_ms"]["max"].get<double>(), l + ",essence=\"audio\",stat=\"max\"");
            }
        }
    }
    for (auto const& player : s["players"])
    {
        std::string const l = "player=\"" + std::to_string(player["player"].get<int>()) + "\"";
        metric("player_playing", player["state"] == "playing" ? 1.0 : 0.0, l);
        metric("player_frames_late_total", player.value("late", 0.0), l);
        metric("player_frames_decoded_total", player.value("decoded", 0.0), l);
        metric("player_decode_ms", player.value("decode_ms", 0.0), l + ",stat=\"avg\"");
    }
    // Stingers (P3): played, grains waited for a first frame, fallbacks to a cut; per player.
    auto const& stingers = s["stingers"];
    metric("stingers_played_total", stingers["played"].get<double>());
    metric("stinger_wait_grains_total", stingers["waits"].get<double>());
    metric("stinger_cuts_total", stingers["cuts"].get<double>());
    for (auto const& player : stingers["players"])
    {
        std::string const l = "player=\"" + std::to_string(player["player"].get<int>()) + "\"";
        for (char const* state : {"empty", "cued", "playing", "paused", "ended"})
        {
            metric("stinger_player_state", player["state"] == state ? 1.0 : 0.0, l + ",state=\"" + state + "\"");
        }
        metric("stinger_frames_late_total", player.value("late", 0.0), l);
        metric("stinger_frames_decoded_total", player.value("decoded", 0.0), l);
        metric("stinger_decode_ms", player.value("decode_ms", 0.0), l + ",stat=\"avg\"");
    }
    // Keyers (P3): on air, and whether the key flow of a linear key runs (else NO KEY).
    auto const& keyList = s["keys"];
    for (auto const& key : s["keyers"])
    {
        auto const k = key["key"].get<std::size_t>();
        std::string const label = k >= 1 && k <= keyList.size() ? keyList[k - 1]["label"].get<std::string>() : std::to_string(k);
        std::string const l = "keyer=\"" + label + "\",me=\"" + std::to_string(key["me"].get<int>()) + "\",stage=\"" +
            key["stage"].get<std::string>() + "\",mode=\"" + key["mode"].get<std::string>() + "\"";
        metric("keyer_on_air", key["on"].get<bool>() ? 1.0 : 0.0, l);
        metric("keyer_key_running", k >= 1 && k <= keyList.size() && keyList[k - 1]["state"] == "running" ? 1.0 : 0.0, l);
    }
    // P5: fx-audio's time per grain (read, mix, meters, write) and its bounded waits.
    auto const& audio = s["audio"];
    metric("audio_ms", audio["ms_per_grain"]["avg"].get<double>(), "stat=\"avg\"");
    metric("audio_ms", audio["ms_per_grain"]["max"].get<double>(), "stat=\"max\"");
    metric("audio_wait_ms", audio["wait_ms"]["avg"].get<double>(), "stat=\"avg\"");
    metric("audio_wait_ms", audio["wait_ms"]["max"].get<double>(), "stat=\"max\"");
    metric("audio_jobs_dropped_total", audio["jobs_dropped"].get<double>());
    // P5 meters over the last second (values below −120 dB are −120).
    MeterSnapshot meters;
    {
        std::lock_guard lock{impl_->meterMu};
        meters = impl_->meters;
    }
    if (meters.valid)
    {
        auto const levels = [&](std::vector<MeterValues> const& list, char const* prefix) {
            for (std::size_t k = 0; k < list.size(); ++k)
            {
                for (std::size_t c = 0; c < list[k].peak1s.size(); ++c)
                {
                    std::string const l = "source=\"" + std::string(prefix) + std::to_string(k + 1) + "\",channel=\"" + std::to_string(c + 1) + "\"";
                    metric("audio_level_dbfs", levelDb(list[k].peak1s[c]), l + ",stat=\"peak\"");
                    metric("audio_level_dbfs", levelDb(list[k].rms1s[c]), l + ",stat=\"rms\"");
                }
            }
        };
        levels(meters.inputs, "in");
        levels(meters.players, "cp");
        levels(meters.stingers, "st");
        for (std::size_t m = 0; m < meters.mes.size(); ++m)
        {
            auto const& v = meters.mes[m];
            std::string const me = "me=\"" + std::to_string(m + 1) + "\"";
            for (std::size_t c = 0; c < v.peak1s.size(); ++c)
            {
                std::string const l = me + ",channel=\"" + std::to_string(c + 1) + "\"";
                metric("pgm_audio_level_dbfs", levelDb(v.peak1s[c]), l + ",stat=\"peak\"");
                metric("pgm_audio_level_dbfs", levelDb(v.truePeak1s[c]), l + ",stat=\"true_peak\"");
                metric("pgm_audio_level_dbfs", levelDb(v.rms1s[c]), l + ",stat=\"rms\"");
            }
            for (std::size_t p = 0; p < v.loudness.size(); ++p)
            {
                std::string const l = me + ",pair=\"" + std::to_string(p + 1) + "\"";
                metric("pgm_loudness_lufs", v.loudness[p].momentary, l + ",window=\"momentary\"");
                metric("pgm_loudness_lufs", v.loudness[p].shortTerm, l + ",window=\"short_term\"");
                metric("pgm_loudness_lufs", v.loudness[p].integrated, l + ",window=\"integrated\"");
            }
            metric("pgm_audio_overs_total", static_cast<double>(v.overs), me);
            if (m < meters.stripRefs.size())
            {
                for (std::size_t s = 0; s < meters.stripRefs[m].size() && s < meters.strips[m].size(); ++s)
                {
                    metric("audio_strip_heard", meters.strips[m][s].heard ? 1.0 : 0.0, me + ",strip=\"" + stripName(meters.stripRefs[m][s]) + "\"");
                }
            }
        }
    }
    // Frame grabs (P4): per source kind, failures, ms from the request to the file.
    auto const& grabs = s["grabs"];
    for (auto const& [kind, count] : grabs["total"].items())
    {
        metric("grabs_total", count.get<double>(), "source=\"" + kind + "\"");
    }
    metric("grab_failed_total", grabs["failed"].get<double>());
    metric("grab_ms", grabs["ms"]["avg"].get<double>(), "stat=\"avg\"");
    metric("grab_ms", grabs["ms"]["max"].get<double>(), "stat=\"max\"");
    auto const& mosaic = s["mosaic"];
    metric("mosaic_frames_total", mosaic["frames"].get<double>());
    metric("mosaic_dropped_total", mosaic["dropped"].get<double>());
    metric("mosaic_encode_ms", mosaic["encode_ms"]["avg"].get<double>(), "stat=\"avg\"");
    if (mosaic.contains("encoder"))
    {
        metric("mosaic_connected", mosaic["encoder"]["connected"].get<bool>() ? 1.0 : 0.0);
        metric("mosaic_bytes_total", mosaic["encoder"]["bytes"].get<double>());
    }
    // The built-in MediaMTX (own mode): running and how often it was started again.
    auto const& own = s["preview"]["own_mediamtx"];
    if (own.is_object())
    {
        metric("preview_mediamtx_running", own["running"].get<bool>() ? 1.0 : 0.0);
        metric("preview_mediamtx_restarts_total", own["restarts"].get<double>());
    }
    metric("cpu_percent", s["cpu"]["process_percent"].get<double>(), "thread=\"process\"");
    for (auto const& [thread, value] : s["cpu"]["threads_percent"].items())
    {
        metric("cpu_percent", value.get<double>(), "thread=\"" + thread + "\"");
    }
    metric("gpu_utilization_percent", s["gpu"]["utilization_percent"].get<double>());
    metric("gpu_encoder_percent", s["gpu"]["encoder_percent"].get<double>());
    metric("gpu_encoder_sessions", s["gpu"]["encoder_sessions"].get<double>());
    metric("gpu_device_used_mb", s["gpu"]["device_used_mb"].get<double>());
    metric("gpu_engine_allocated_mb", s["engine"]["device_allocated_mb"].get<double>());
    return out.str();
}

std::string Engine::mosaicMapJson() const
{
    auto const& cfg = impl_->cfg;
    json tiles = json::array();
    for (auto const& tile : impl_->tiles)
    {
        std::string label;
        std::string kind;
        std::string source;
        switch (tile.kind)
        {
        case MosaicTile::Kind::Input:
            kind = "input";
            label = cfg.inputs[static_cast<std::size_t>(tile.index - 1)].label;
            source = "in" + std::to_string(tile.index);
            break;
        case MosaicTile::Kind::Player:
            kind = "player";
            label = cfg.players[static_cast<std::size_t>(tile.index - 1)].label;
            source = "cp" + std::to_string(tile.index);
            break;
        case MosaicTile::Kind::Program:
        case MosaicTile::Kind::Preview:
            kind = tile.kind == MosaicTile::Kind::Program ? "program" : "preview";
            label = "ME" + std::to_string(tile.index) + (tile.kind == MosaicTile::Kind::Program ? " PGM" : " PVW");
            source = "me" + std::to_string(tile.index);
            break;
        }
        tiles.push_back({{"id", tile.id}, {"kind", kind}, {"source", source}, {"label", label}, {"x", tile.x}, {"y", tile.y}, {"w", tile.w}, {"h", tile.h}});
    }
    // The GUI plays <base>/<path>/whep; an empty base means the own MediaMTX on the page's host.
    return json{{"width", cfg.width}, {"height", cfg.height}, {"fps", cfg.mosaicFps}, {"enabled", cfg.mosaic}, {"path", cfg.streamPath("mosaic")},
        {"mode", cfg.ownMediamtx() ? "own" : "shared"}, {"whep_base", cfg.whepBase}, {"hls_base", cfg.hlsBase}, {"whep_port", cfg.whepPort},
        {"hls_port", cfg.hlsPort}, {"tiles", tiles}}
        .dump(2);
}
} // namespace fxeng
