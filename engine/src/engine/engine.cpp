#include "engine/engine.hpp"

#include "core/audio.hpp"
#include "core/mosaic_map.hpp"
#include "core/pixel.hpp"
#include "core/tai.hpp"
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
// Output grains after a route (or the start) during which a missing grain or missing audio is a
// start-up window, not counted as missing.
constexpr std::uint64_t kGraceGrains = 4;

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

// Average and maximum over windows of `size` samples, and the peak since the last reset.
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

    void allocate(std::size_t bytes, int count)
    {
        for (int i = 0; i < count; ++i)
        {
            auto* frame = new DevFrame;
            all_.push_back(frame);
            check(cudaMalloc(reinterpret_cast<void**>(&frame->data), bytes), "cudaMalloc input frame");
            check(cudaEventCreateWithFlags(&frame->ready, cudaEventDisableTiming), "cudaEventCreate");
            free_.push_back(frame);
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
    // How far the audio head is behind TAI (ms), sampled once per grain.
    Window audioLag;

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

// The PGM audio state of fx-writer (only that thread touches it).
struct AudioRun
{
    struct Reader
    {
        mxlInstance instance = nullptr;
        std::string domain;
        mxlFlowReader reader = nullptr;
        std::size_t channels = 0;
        std::uint64_t gen = ~0ull;
        Route route;
        std::chrono::steady_clock::time_point retryAt{};
    };
    std::vector<Reader> readers;
    // Channel-major buffers of one grain: per source (inputs, then players), per ME Program.
    std::vector<std::vector<float>> in;
    std::vector<std::vector<float>> me;
    std::size_t channels = 2;
    std::uint64_t last = 0;
};

// One output grain in flight: rendered by fx-render, downloaded and committed by fx-writer.
struct Slot
{
    // Packed v210 per ME output (ME m PGM at 2(m-1), PVW at 2(m-1)+1); every one is packed (the
    // mosaic reads them), only those with an MXL flow are downloaded.
    std::vector<std::uint8_t*> out;
    // The input grains this output grain read; released after the downloads.
    std::vector<std::shared_ptr<DevFrame>> held;
    // The clip player frames of this grain (their audio goes into the PGM audio).
    std::vector<std::shared_ptr<PlayerFrame>> players;
    std::uint64_t index = 0;
    std::uint64_t inputIndex = 0;
    // Render stream: start, unpacked, one per ME (render order), keyed + packed, render done.
    std::vector<cudaEvent_t> mark;
    cudaEvent_t downloadStart = nullptr;
    std::vector<cudaEvent_t> downloaded;
    // The ME plans of this grain (render order), for the audio.
    std::vector<MePlan> plans;
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
    std::vector<std::unique_ptr<Input>> keys;
    std::vector<std::unique_ptr<ClipPlayer>> players;
    // Video outputs (PGM of every ME, PVW with pvwOutputs), audio outputs (ME m PGM at m-1).
    std::vector<std::unique_ptr<Output>> outputs;
    std::vector<std::unique_ptr<Output>> audioOutputs;
    // ME m's PVW video output, or nullptr.
    std::vector<Output*> pvwOf;
    std::map<std::string, mxlInstance> outInstances;
    std::uint8_t* black = nullptr;
    // Work pictures of the sources (inputs, then players), and of each ME's buses.
    std::vector<gpu::WorkBuffer> srcWork;
    std::vector<gpu::WorkBuffer> pgmWork;
    std::vector<gpu::WorkBuffer> pvwWork;
    // Black in the working format (the Black source, an empty clip player).
    gpu::WorkBuffer blackWork;
    std::array<Slot, kSlots> slots;
    std::array<MosaicSlot, kMosaicSlots> mosaicSlots;
    std::vector<MosaicTile> tiles;
    cudaStream_t renderStream = nullptr;
    cudaStream_t downloadStream = nullptr;
    std::size_t allocated = 0;
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

    mutable std::mutex snapMu;
    ThreadCpu::Result cpu;
    GpuSample gpuSample;
    double fps = 0;
    std::uint64_t deviceUsed = 0;

    explicit Impl(Config c)
        : cfg(std::move(c))
        , mixer(cfg.mes, static_cast<int>(cfg.inputs.size()), static_cast<int>(cfg.players.size()), dskMes(cfg))
    {
    }

    static std::vector<int> dskMes(Config const& cfg)
    {
        std::vector<int> mes;
        for (auto const& key : cfg.keys)
        {
            mes.push_back(key.me);
        }
        return mes;
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
        number = 1;
        for (auto const& keyCfg : cfg.keys)
        {
            auto key = makeInput(number, "key" + std::to_string(number), keyCfg.label, true, keyBytes, poolSize);
            key->video = keyCfg.route;
            keys.push_back(std::move(key));
            ++number;
        }
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
            for (auto& event : slot.mark)
            {
                check(cudaEventCreate(&event), "cudaEventCreate");
            }
            check(cudaEventCreateWithFlags(&slot.downloadStart, cudaEventDefault), "cudaEventCreate");
            slot.downloaded.resize(outCount);
            for (auto& event : slot.downloaded)
            {
                check(cudaEventCreateWithFlags(&event, cudaEventBlockingSync), "cudaEventCreate");
            }
        }
        for (int m = 0; m < cfg.mes; ++m)
        {
            meMs.push_back(std::make_unique<Window>());
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
            if (next == runtime.headIndex && runtime.lastWriteTime != 0)
            {
                auto const due = indexToTimestamp(cfg.rate, next);
                in.arrival.add(runtime.lastWriteTime > due ? static_cast<double>(runtime.lastWriteTime - due) / 1e6 : 0.0);
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
            if (waiting)
            {
                auto const now = mxlGetTime();
                auto const due = indexToTimestamp(cfg.rate, grain.index);
                in.arrival.add(now > due ? static_cast<double>(now - due) / 1e6 : 0.0);
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
        auto const plan = mixer.frame(index);
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
        // A clip player is on air when it is in an ME's Program (or the incoming source of a Mix).
        std::vector<bool> onAir(players.size(), false);
        for (auto const& p : plans)
        {
            for (auto const& source : {p.a, p.b})
            {
                if (source.kind == Source::Kind::Player && (source == p.a || p.mixing))
                {
                    onAir[static_cast<std::size_t>(source.index - 1)] = true;
                }
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
            need(p.a);
            if (p.mixing)
            {
                need(p.b);
            }
            need(p.preview);
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
        // The key inputs the downstream keyers need now (grain i - L, like the inputs).
        std::vector<std::shared_ptr<DevFrame>> keyFrames(keys.size());
        for (auto const& dsk : plan.dsks)
        {
            auto const k = static_cast<std::size_t>(dsk.key - 1);
            if (dsk.gain > 0.f && k < keys.size() && !keyFrames[k])
            {
                keyFrames[k] = pick(*keys[k], j, index);
                if (keyFrames[k])
                {
                    cudaStreamWaitEvent(st, keyFrames[k]->ready, 0);
                }
            }
        }
        // ME n before ME m (n > m): a re-entered Program is the same output grain (with its keys).
        for (auto const& p : plans)
        {
            auto const me = static_cast<std::size_t>(p.me - 1);
            gpu::mix(st, workOf(p.a), p.mixing ? &workOf(p.b) : nullptr, p.mixing ? p.t : 0.f, pgmWork[me]);
            gpu::mix(st, workOf(p.preview), nullptr, 0.f, pvwWork[me]);
            // Downstream keyers over the ME's Program and Preview (Preview shows what Program
            // will look like after the next transition).
            for (auto const& dsk : plan.dsks)
            {
                auto const& frame = dsk.me == p.me && dsk.gain > 0.f ? keyFrames[static_cast<std::size_t>(dsk.key - 1)] : nullptr;
                if (frame)
                {
                    gpu::keyV210a(st, frame->data, rowBytes, keyRowBytes, dsk.gain, pgmWork[me]);
                    gpu::keyV210a(st, frame->data, rowBytes, keyRowBytes, dsk.gain, pvwWork[me]);
                }
            }
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

    void write(Slot& slot, HostRegistry& registry, AudioRun& audio)
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
            // mark[2 + i] ends the i-th ME in render order (ME mes .. 1).
            meMs[mes - 1 - i]->add(elapsed(mark[1 + i], mark[2 + i]));
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
        slot.held.clear();
        writeAudio(slot, audio);
        slot.players.clear();
        slot.busy.store(false);
    }

    // ---- PGM audio (fx-writer) -----------------------------------------------------------

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

    // The input's audio reader for its current route, (re)opened at most once a second.
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
        // Samples from before the open may be missing: a start-up window.
        in.audioGraceUntil.store(graceFromNow());
        setAudioState(in, "running");
        logInfo("input_audio_open", {{"input", in.name}, {"flow", r.route.flow}, {"channels", r.channels}});
        return true;
    }

    // How far the audio head is behind TAI, once per grain for every routed input.
    void measureAudio(Input& in, AudioRun::Reader& r)
    {
        mxlFlowRuntimeInfo runtime{};
        if (r.reader == nullptr || mxlFlowReaderGetRuntimeInfo(r.reader, &runtime) != MXL_STATUS_OK || runtime.headIndex == MXL_UNDEFINED_INDEX)
        {
            return;
        }
        auto const now = timestampToIndex(Rate{kAudioRate, 1}, mxlGetTime());
        double const behind = now > runtime.headIndex ? static_cast<double>(now - runtime.headIndex) : 0.0;
        in.audioLag.add(behind * 1000.0 / static_cast<double>(kAudioRate));
    }

    // `count` samples of every channel ending at `end`, channel-major into dst (channels the
    // flow does not have stay silent). Samples that are not there yet are waited for until
    // `deadline` (TAI ns), at most audioWaitMs. False when they are not there (late, gone,
    // re-created).
    bool readAudio(Input& in, AudioRun::Reader& r, std::uint64_t end, std::size_t count, std::vector<float>& dst, std::size_t channels, std::uint64_t deadline)
    {
        mxlWrappedMultiBufferSlice slice{};
        auto status = mxlFlowReaderGetSamplesNonBlocking(r.reader, end, count, &slice);
        if (status == MXL_ERR_OUT_OF_RANGE_TOO_EARLY)
        {
            auto const now = mxlGetTime();
            std::uint64_t const budget = static_cast<std::uint64_t>(cfg.audioWaitMs) * 1'000'000ull;
            if (deadline > now && budget > 0)
            {
                auto const started = std::chrono::steady_clock::now();
                ++in.audioWaits;
                status = mxlFlowReaderGetSamples(r.reader, end, count, std::min(budget, deadline - now), &slice);
                audioWaitMs.add(std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count());
            }
        }
        if (status == MXL_ERR_FLOW_INVALID)
        {
            // The writer re-created the flow: open the new one.
            closeAudio(r);
            r.retryAt = {};
            ++in.audioReopens;
            return false;
        }
        if (status != MXL_STATUS_OK)
        {
            return false;
        }
        for (std::size_t c = 0; c < std::min(slice.count, channels); ++c)
        {
            std::size_t filled = 0;
            for (auto const& fragment : slice.base.fragments)
            {
                if (fragment.pointer == nullptr)
                {
                    continue;
                }
                std::size_t const n = std::min(fragment.size / sizeof(float), count - filled);
                std::memcpy(dst.data() + c * count + filled, static_cast<std::uint8_t const*>(fragment.pointer) + c * slice.stride, n * sizeof(float));
                filled += n;
            }
        }
        return true;
    }

    // PGM audio of output grain `index`: the audio of the PGM source, read L grains back on the
    // same TAI grid as the video (a clip player's own audio of the grain), crossfaded (equal
    // power) while a Mix runs.
    void audioGrain(std::uint64_t index, Slot const& slot, AudioRun& audio)
    {
        auto const& plans = slot.plans;
        auto const out = grainSamples(cfg.rate, index);
        auto const src = grainSamples(cfg.rate, inputIndexFor(index, cfg.latency));
        std::size_t const n = out.end - out.first;
        std::uint64_t const delta = out.first - src.first;
        std::size_t const ch = audio.channels;
        std::size_t const nIn = inputs.size();
        // Late samples are waited for until 1 ms before the end of this grain's window.
        auto const deadline = windowStart(cfg.rate, index + 1) - 1'000'000ull;
        std::vector<bool> needed(nIn + players.size(), false);
        for (auto const& plan : plans)
        {
            for (auto const& source : {plan.a, plan.b})
            {
                int const s = sourceSlot(source);
                if (s >= 0 && (source == plan.a || plan.mixing))
                {
                    needed[static_cast<std::size_t>(s)] = true;
                }
            }
        }
        for (std::size_t s = 0; s < needed.size(); ++s)
        {
            if (!needed[s])
            {
                continue;
            }
            auto& buffer = audio.in[s];
            buffer.assign(ch * n, 0.f);
            if (s >= nIn)
            {
                // A clip player: the sound of the frame it shows in this grain.
                auto const& frame = s - nIn < slot.players.size() ? slot.players[s - nIn] : nullptr;
                if (frame && frame->index == index)
                {
                    copyPlayerAudio(*frame, buffer, n, ch);
                }
                continue;
            }
            auto& in = *inputs[s];
            auto& reader = audio.readers[s];
            if (reader.route.flow.empty())
            {
                continue;
            }
            if (reader.reader != nullptr && readAudio(in, reader, out.end - delta, n, buffer, ch, deadline))
            {
                ++in.audioBlocks;
            }
            else
            {
                ++(index <= in.audioGraceUntil.load() ? in.audioStartup : in.audioMissing);
            }
        }
        auto const sourceOf = [&](Source source) -> std::vector<float> const* {
            if (source.kind == Source::Kind::Me)
            {
                return &audio.me[static_cast<std::size_t>(source.index - 1)];
            }
            int const s = sourceSlot(source);
            return s >= 0 ? &audio.in[static_cast<std::size_t>(s)] : nullptr;
        };
        std::vector<float> const silence(ch * n, 0.f);
        // Render order (ME n before ME m): a re-entered Program's audio is ready.
        for (auto const& plan : plans)
        {
            auto& dst = audio.me[static_cast<std::size_t>(plan.me - 1)];
            auto const* a = sourceOf(plan.a);
            a = a != nullptr && a->size() == ch * n ? a : &silence;
            if (plan.mixing)
            {
                auto const* b = sourceOf(plan.b);
                b = b != nullptr && b->size() == ch * n ? b : &silence;
                dst.resize(ch * n);
                for (std::size_t c = 0; c < ch; ++c)
                {
                    crossfadeBlock(a->data() + c * n, b->data() + c * n, dst.data() + c * n, n, plan.tPrev, plan.t);
                }
            }
            else
            {
                dst = *a;
            }
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

    // Audio for every grain since the last one written: a skipped video grain leaves no hole
    // (a hole would replay old samples from the ring). Every routed input's reader is kept open
    // (so a cut to it has its sound at once) and its lag measured.
    void writeAudio(Slot const& slot, AudioRun& audio)
    {
        auto const started = std::chrono::steady_clock::now();
        for (std::size_t k = 0; k < inputs.size(); ++k)
        {
            if (openAudio(*inputs[k], audio.readers[k]))
            {
                measureAudio(*inputs[k], audio.readers[k]);
            }
        }
        std::uint64_t first = slot.index;
        if (audio.last != 0 && slot.index > audio.last && slot.index - audio.last <= 25)
        {
            first = audio.last + 1;
        }
        for (std::uint64_t k = first; k <= slot.index; ++k)
        {
            audioGrain(k, slot, audio);
        }
        audio.last = slot.index;
        audioMs.add(std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count());
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
        AudioRun audio;
        audio.readers.resize(inputs.size());
        audio.in.resize(inputs.size() + players.size());
        audio.me.resize(audioOutputs.size());
        audio.channels = 1;
        for (auto const& output : audioOutputs)
        {
            audio.channels = std::max<std::size_t>(audio.channels, output->channels);
        }
        // Open the routed audio before the first grain.
        for (std::size_t k = 0; k < inputs.size(); ++k)
        {
            openAudio(*inputs[k], audio.readers[k]);
        }
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
            write(slots[static_cast<std::size_t>(k)], registry, audio);
        }
        cudaStreamSynchronize(downloadStream);
        registry.release();
        for (auto& reader : audio.readers)
        {
            closeAudio(reader);
            if (reader.instance != nullptr)
            {
                mxlDestroyInstance(reader.instance);
            }
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
        for (auto& player : players)
        {
            player->start();
        }
        // The render thread starts once the writer has page-locked its grains.
        auto warm = writerWarm.get_future();
        writerThread = std::thread([this] { writerMain(); });
        warm.wait();
        if (encoder)
        {
            encodeThread = std::thread([this] { encodeMain(); });
        }
        statsThread = std::thread([this] { statsMain(); });
        renderThread = std::thread([this] { renderMain(); });
        logInfo("engine_started", {{"gpu", gpuName}, {"inputs", inputs.size()}, {"players", players.size()}, {"keys", keys.size()}, {"mes", cfg.mes},
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
        // Render first (no new grains), then the writer drains its queue.
        for (auto* thread : {&renderThread, &writerThread, &encodeThread, &statsThread})
        {
            if (thread->joinable())
            {
                thread->join();
            }
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
        }
        for (auto& player : players)
        {
            player->stop();
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
        for (auto& slot : slots)
        {
            for (auto* out : slot.out)
            {
                cudaFree(out);
            }
            for (auto event : slot.mark)
            {
                cudaEventDestroy(event);
            }
            for (auto event : slot.downloaded)
            {
                cudaEventDestroy(event);
            }
            cudaEventDestroy(slot.downloadStart);
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
        cudaStreamDestroy(renderStream);
        cudaStreamDestroy(downloadStream);
        logInfo("engine_stopped", {{"rendered", rendered.load()}, {"committed", committed.load()}, {"skipped", skipped.load()}});
    }

    // ---- status --------------------------------------------------------------------------

    // The latency that covers the latest arrival seen since each input was routed: grain i - L
    // must be there when the render of grain i starts, half a grain before its TAI time.
    [[nodiscard]] int recommendedLatency() const
    {
        double worst = 0;
        for (auto const& in : inputs)
        {
            worst = std::max({worst, in->arrival.peak(), in->audioLag.peak()});
        }
        for (auto const& key : keys)
        {
            worst = std::max(worst, key->arrival.peak());
        }
        return latencyFor(worst / periodMs());
    }

    json inputJson(Input const& in, std::uint64_t now) const
    {
        std::string state;
        std::string audioState;
        Route video;
        Route audioRoute;
        {
            std::lock_guard lock{in.mu};
            state = in.state;
            audioState = in.audioState;
            video = in.video;
            audioRoute = in.audio;
        }
        auto const lastAt = in.lastGrainAt.load();
        // No grain for a second: shown as no_signal (the picture holds, see holdGrains).
        if (state == "running" && (lastAt == 0 || now > lastAt + 1'000'000'000ull))
        {
            state = "no_signal";
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
                {"lag_ms", in.audioLag.toJson(true)}};
        }
        return item;
    }

    json status() const
    {
        json out;
        out["engine"] = {{"version", FXENG_VERSION},
            {"format", cfg.format},
            {"latency_grains", cfg.latency},
            {"recommended_latency_grains", recommendedLatency()},
            {"mes", cfg.mes},
            {"inputs", inputs.size()},
            {"players", players.size()},
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
        for (std::size_t m = 0; m < meMs.size(); ++m)
        {
            me[std::to_string(m + 1)] = meMs[m]->toJson();
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
        json keyList = json::array();
        for (auto const& key : keys)
        {
            keyList.push_back(inputJson(*key, now));
        }
        out["keys"] = keyList;
        json playerList = json::array();
        for (auto const& player : players)
        {
            playerList.push_back(json::parse(player->statusJson()));
        }
        out["players"] = playerList;
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
                {"target", sourceName(view.target)},
                {"frames", view.frames},
                {"position", view.position},
                {"pgm", pgm != nullptr ? flow(*pgm) : json(nullptr)},
                {"pvw", pvw != nullptr ? flow(*pvw) : json(nullptr)},
                {"pgm_audio", pgmAudio}});
        }
        out["mes"] = mes;
        json dsks = json::array();
        for (auto const& dsk : mixer.dskView())
        {
            dsks.push_back({{"dsk", dsk.key}, {"key", dsk.key}, {"me", dsk.me}, {"on", dsk.on}, {"transition", dsk.transition}, {"gain", round3(dsk.gain)}});
        }
        out["dsks"] = dsks;
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
        out["audio"] = {{"ms_per_grain", audioMs.toJson()}, {"wait_ms", audioWaitMs.toJson()}, {"max_wait_ms", cfg.audioWaitMs}};
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
    if (k < 1 || k > static_cast<int>(impl_->keys.size()))
    {
        return false;
    }
    return impl_->route(*impl_->keys[static_cast<std::size_t>(k - 1)], route, nullptr);
}

std::string Engine::playerCommand(int k, PlayerCommand const& command)
{
    if (k < 1 || k > static_cast<int>(impl_->players.size()))
    {
        return "no clip player " + std::to_string(k);
    }
    return impl_->players[static_cast<std::size_t>(k - 1)]->command(command);
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
        else if (value.is_object())
        {
            metric("gpu_stage_ms", value["avg"].get<double>(), "stage=\"" + stage + "\",stat=\"avg\"");
            metric("gpu_stage_ms", value["max"].get<double>(), "stage=\"" + stage + "\",stat=\"max\"");
        }
    }
    for (auto const* list : {&s["inputs"], &s["keys"]})
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
