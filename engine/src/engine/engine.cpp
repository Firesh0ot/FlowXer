#include "engine/engine.hpp"

#include "core/mosaic_map.hpp"
#include "core/pixel.hpp"
#include "core/tai.hpp"
#include "engine/encoder.hpp"
#include "engine/gpu.hpp"
#include "engine/hostmem.hpp"
#include "engine/log.hpp"
#include "engine/mediamtx.hpp"
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

// How long an input thread blocks for its next grain; bounds how fast stop() ends it.
constexpr std::uint64_t kGrainWaitNs = 40'000'000;
// Output grains in flight between the render and the writer thread.
constexpr int kSlots = 3;
constexpr int kMosaicSlots = 2;
// Samples per statistics window (5 s at 50p).
constexpr int kWindow = 250;

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

// Average and maximum over windows of `size` samples. value() is the last full window, or
// the running one until the first is full.
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
        if (++n_ >= kWindow)
        {
            done_ = Value{sum_ / n_, max_};
            full_ = true;
            sum_ = 0;
            max_ = 0;
            n_ = 0;
        }
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

    [[nodiscard]] json toJson() const
    {
        auto const v = value();
        return json{{"avg", round3(v.avg)}, {"max", round3(v.max)}};
    }

private:
    mutable std::mutex mu_;
    double sum_ = 0;
    double max_ = 0;
    int n_ = 0;
    Value done_;
    bool full_ = false;
};

// One grain of an input on the device, packed v210 as in MXL.
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

    [[nodiscard]] std::size_t size() const
    {
        return all_.size();
    }

private:
    std::mutex mu_;
    std::vector<DevFrame*> free_;
    std::vector<DevFrame*> all_;
};

struct Input
{
    int number = 1;
    InputConfig cfg;
    FramePool pool;
    mutable std::mutex mu;
    // The newest grains, oldest first.
    std::deque<std::shared_ptr<DevFrame>> recent;
    std::string state = "starting";
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
    std::atomic<std::uint64_t> lastGrainAt{0};
    Window upload;
    std::thread thread;
};

struct Output
{
    int me = 1;
    bool preview = false;
    std::string id;
    std::string label;
    mxlFlowWriter writer = nullptr;
    std::uint32_t grainCount = 0;
    std::atomic<std::uint64_t> committed{0};
    std::atomic<std::uint64_t> failed{0};
};

// One output grain in flight: rendered by fx-render, downloaded and committed by fx-writer.
struct Slot
{
    // Packed v210 per output (ME m PGM at 2(m-1), PVW at 2(m-1)+1).
    std::vector<std::uint8_t*> out;
    // The input grains this output grain read; released after the downloads.
    std::vector<std::shared_ptr<DevFrame>> held;
    std::uint64_t index = 0;
    std::uint64_t inputIndex = 0;
    // Render stream: start, unpacked, one per ME (render order), packed, render done.
    std::vector<cudaEvent_t> mark;
    cudaEvent_t downloadStart = nullptr;
    std::vector<cudaEvent_t> downloaded;
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

void nameThread(std::string const& name)
{
    pthread_setname_np(pthread_self(), name.substr(0, 15).c_str());
}
} // namespace

struct Engine::Impl
{
    Config cfg;
    Mixer mixer;
    gpu::WorkFormat work = gpu::WorkFormat::Yuv16;
    int rowBytes = 0;
    std::size_t frameBytes = 0;
    int recentDepth = 4;
    int mosaicDivisor = 2;
    std::atomic<bool> run{false};
    bool started = false;

    std::vector<std::unique_ptr<Input>> inputs;
    std::vector<std::unique_ptr<Output>> outputs;
    mxlInstance outInstance = nullptr;
    std::uint8_t* black = nullptr;
    std::vector<gpu::WorkBuffer> inWork;
    std::vector<gpu::WorkBuffer> pgmWork;
    std::vector<gpu::WorkBuffer> pvwWork;
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

    mutable std::mutex snapMu;
    ThreadCpu::Result cpu;
    GpuSample gpuSample;
    double fps = 0;
    std::uint64_t deviceUsed = 0;

    explicit Impl(Config c)
        : cfg(std::move(c))
        , mixer(cfg.mes, static_cast<int>(cfg.inputs.size()))
    {
    }

    gpu::WorkBuffer const& workOf(Source source) const
    {
        return source.kind == Source::Kind::Input ? inWork[static_cast<std::size_t>(source.index - 1)] : pgmWork[static_cast<std::size_t>(source.index - 1)];
    }

    void allocDevice(void** ptr, std::size_t bytes, char const* what)
    {
        check(cudaMalloc(ptr, bytes), what);
        allocated += bytes;
    }

    // ---- setup -------------------------------------------------------------------------

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

        allocDevice(reinterpret_cast<void**>(&black), frameBytes, "black frame");
        check(gpu::fillBlack(renderStream, black, rowBytes, cfg.width, cfg.height), "fill black");

        // The ring keeps grains i-L .. newest (+1 for a source a grain ahead); in flight are at
        // most one grain per output slot and the one being uploaded.
        recentDepth = cfg.latency + 2;
        int const poolSize = recentDepth + kSlots + 1;
        int number = 1;
        for (auto const& inputCfg : cfg.inputs)
        {
            auto input = std::make_unique<Input>();
            input->number = number++;
            input->cfg = inputCfg;
            input->pool.allocate(frameBytes, poolSize);
            allocated += frameBytes * static_cast<std::size_t>(poolSize);
            inputs.push_back(std::move(input));
        }
        auto addWork = [&](std::vector<gpu::WorkBuffer>& list, std::size_t count) {
            list.resize(count);
            for (auto& buffer : list)
            {
                check(gpu::allocWork(buffer, work, cfg.width, cfg.height), "work buffer");
                allocated += buffer.bytes;
            }
        };
        addWork(inWork, inputs.size());
        addWork(pgmWork, static_cast<std::size_t>(cfg.mes));
        addWork(pvwWork, static_cast<std::size_t>(cfg.mes));

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

        tiles = mosaicTiles(cfg.width, cfg.height, static_cast<int>(inputs.size()), cfg.mes);
        mosaicDivisor = std::max(1, static_cast<int>(std::lround(static_cast<double>(cfg.rate.num) / static_cast<double>(cfg.rate.den) / cfg.mosaicFps)));
        if (cfg.mosaic)
        {
            std::size_t const lumaBytes = static_cast<std::size_t>(cfg.width) * static_cast<std::size_t>(cfg.height);
            for (auto& slot : mosaicSlots)
            {
                allocDevice(reinterpret_cast<void**>(&slot.nv12), lumaBytes * 3 / 2, "mosaic frame");
                // Black where no tile is (fewer than 8 inputs or 4 MEs).
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

    void setupOutputs()
    {
        std::filesystem::create_directories(cfg.outputDomain);
        auto const defPath = std::filesystem::path(cfg.outputDomain) / "domain_def.json";
        if (!std::filesystem::exists(defPath))
        {
            // BCP-007-03 requires id, label, description and tags.
            std::ofstream out(defPath);
            out << json{{"id", nameUuid("domain:" + cfg.outputDomain)}, {"label", cfg.label}, {"description", "FlowXer engine outputs"}, {"tags", json::object()}}.dump()
                << "\n";
        }
        outInstance = mxlCreateInstance(cfg.outputDomain.c_str(), nullptr);
        if (outInstance == nullptr)
        {
            throw std::runtime_error("cannot open the MXL output domain " + cfg.outputDomain);
        }
        mxlGarbageCollectFlows(outInstance);
        for (int m = 1; m <= cfg.mes; ++m)
        {
            for (bool preview : {false, true})
            {
                auto output = std::make_unique<Output>();
                output->me = m;
                output->preview = preview;
                std::string const kind = preview ? "PVW" : "PGM";
                output->id = nameUuid(cfg.outputDomain + "/me" + std::to_string(m) + "/" + kind);
                output->label = cfg.label + " ME" + std::to_string(m) + " " + kind;
                auto const def = videoFlowDefinition(output->id, output->label, "ME" + std::to_string(m) + " " + kind + ":Video", cfg.width, cfg.height, cfg.rate);
                mxlFlowConfigInfo info{};
                if (mxlCreateFlowWriter(outInstance, def.c_str(), "{\"maxCommitBatchSizeHint\":1}", &output->writer, &info, nullptr) != MXL_STATUS_OK)
                {
                    throw std::runtime_error("cannot create the MXL writer for " + output->label);
                }
                output->grainCount = info.discrete.grainCount;
                outputs.push_back(std::move(output));
            }
        }
    }

    // ---- input threads -------------------------------------------------------------------

    // The flow carries cfg.width x cfg.height v210 (or v210a: its fill comes first) at the
    // engine's rate.
    bool formatMatches(mxlInstance instance, std::string const& flowId, std::string& why) const
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
        if ((media != "video/v210" && media != "video/v210a") || width != cfg.width || height != cfg.height || num * cfg.rate.den != den * cfg.rate.num)
        {
            why = media + " " + std::to_string(width) + "x" + std::to_string(height) + " " + std::to_string(num) + "/" + std::to_string(den);
            return false;
        }
        return true;
    }

    void inputMain(Input& in)
    {
        nameThread("fx-in" + std::to_string(in.number));
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
        mxlFlowReader reader = nullptr;
        std::uint64_t next = MXL_UNDEFINED_INDEX;
        int backoffMs = 250;
        auto const setState = [&](char const* state) {
            std::lock_guard lock{in.mu};
            in.state = state;
        };
        auto const pause = [&](int ms) {
            for (int waited = 0; waited < ms && run.load(); waited += 50)
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
        while (run.load())
        {
            if (instance == nullptr)
            {
                instance = mxlCreateInstance(in.cfg.domain.c_str(), nullptr);
                if (instance == nullptr)
                {
                    setState("domain_not_found");
                    pause(backoffMs);
                    backoffMs = std::min(5000, backoffMs * 2);
                    continue;
                }
            }
            if (reader == nullptr)
            {
                if (mxlCreateFlowReader(instance, in.cfg.flow.c_str(), nullptr, &reader) != MXL_STATUS_OK)
                {
                    reader = nullptr;
                    setState("flow_not_found");
                    pause(backoffMs);
                    backoffMs = std::min(5000, backoffMs * 2);
                    continue;
                }
                std::string why;
                if (!formatMatches(instance, in.cfg.flow, why))
                {
                    closeReader();
                    setState("format_mismatch");
                    logWarn("input_format_mismatch", {{"input", in.number}, {"flow", in.cfg.flow}, {"format", why}});
                    pause(5000);
                    continue;
                }
                setState("running");
                logInfo("input_open", {{"input", in.number}, {"domain", in.cfg.domain}, {"flow", in.cfg.flow}});
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
            next = grain.index + 1;
            if ((grain.flags & MXL_GRAIN_FLAG_INVALID) != 0 || grain.grainSize < frameBytes)
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
            if (!registry.ensure(payload, frameBytes, true))
            {
                ++in.unpinned;
            }
            auto& events = timing[uploads % timing.size()];
            cudaEventRecord(events[0], stream);
            cudaMemcpyAsync(frame->data, payload, frameBytes, cudaMemcpyHostToDevice, stream);
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
                in.recent.push_back(std::move(frame));
                while (in.recent.size() > static_cast<std::size_t>(recentDepth))
                {
                    in.recent.pop_front();
                }
            }
            ++in.grains;
            in.lastGrainAt.store(mxlGetTime());
        }
        closeReader();
        if (instance != nullptr)
        {
            mxlDestroyInstance(instance);
        }
        for (auto& pair : timing)
        {
            cudaEventDestroy(pair[0]);
            cudaEventDestroy(pair[1]);
        }
        cudaStreamDestroy(stream);
    }

    // ---- render thread -------------------------------------------------------------------

    // Grain j of an input, else its newest older grain (held, counted late) while it is
    // within the hold time, else nothing (black).
    std::shared_ptr<DevFrame> pick(Input& in, std::uint64_t j)
    {
        std::lock_guard lock{in.mu};
        if (in.recent.empty())
        {
            ++in.missing;
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
                if ((*it)->index + static_cast<std::uint64_t>(cfg.holdGrains) < j)
                {
                    ++in.missing;
                    return nullptr;
                }
                ++in.late;
                return *it;
            }
        }
        // A source ahead of TAI: its oldest grain.
        return in.recent.front();
    }

    void renderMosaic(Slot& slot, std::vector<std::shared_ptr<DevFrame>> const& frames, std::uint64_t index)
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
            if (tile.kind == MosaicTile::Kind::Input)
            {
                source.v210 = frames[k] ? frames[k]->data : nullptr;
            }
            else
            {
                source.v210 = slot.out[2 * k + (tile.kind == MosaicTile::Kind::Preview ? 1 : 0)];
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
        auto const plans = mixer.frame(index);
        std::uint64_t const j = inputIndexFor(index, cfg.latency);
        std::vector<std::shared_ptr<DevFrame>> frames(inputs.size());
        for (std::size_t k = 0; k < inputs.size(); ++k)
        {
            frames[k] = pick(*inputs[k], j);
        }
        std::vector<bool> needed(inputs.size(), false);
        auto const need = [&](Source source) {
            if (source.kind == Source::Kind::Input)
            {
                needed[static_cast<std::size_t>(source.index - 1)] = true;
            }
        };
        for (auto const& plan : plans)
        {
            need(plan.a);
            if (plan.mixing)
            {
                need(plan.b);
            }
            need(plan.preview);
        }
        cudaStream_t const st = renderStream;
        auto mark = slot.mark.begin();
        cudaEventRecord(*mark++, st);
        // Each input is unpacked once per grain, for every ME that uses it.
        for (std::size_t k = 0; k < inputs.size(); ++k)
        {
            if (!needed[k])
            {
                continue;
            }
            if (frames[k])
            {
                cudaStreamWaitEvent(st, frames[k]->ready, 0);
            }
            gpu::unpack(st, frames[k] ? frames[k]->data : black, rowBytes, inWork[k]);
        }
        cudaEventRecord(*mark++, st);
        // ME n before ME m (n > m): a re-entered Program is the same output grain.
        for (auto const& plan : plans)
        {
            auto const me = static_cast<std::size_t>(plan.me - 1);
            gpu::mix(st, workOf(plan.a), plan.mixing ? &workOf(plan.b) : nullptr, plan.mixing ? plan.t : 0.f, pgmWork[me]);
            gpu::mix(st, workOf(plan.preview), nullptr, 0.f, pvwWork[me]);
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
            renderMosaic(slot, frames, index);
        }
        cudaEventRecord(*mark, st);
        slot.held.clear();
        for (auto& frame : frames)
        {
            if (frame)
            {
                slot.held.push_back(std::move(frame));
            }
        }
        slot.index = index;
        slot.inputIndex = j;
    }

    void renderMain()
    {
        nameThread("fx-render");
        cudaSetDevice(cfg.gpu);
        std::uint64_t next = 0;
        while (run.load())
        {
            // The output index always comes from TAI: late means skip, never slide.
            auto const step = nextOutputIndex(next, timestampToIndex(cfg.rate, mxlGetTime()));
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
                cudaMemcpyAsync(payload, slot.out[o], frameBytes, cudaMemcpyDeviceToHost, dl);
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
        double const download = elapsed(slot.downloadStart, slot.downloaded.back());
        renderMs.add(render);
        downloadMs.add(download);
        totalMs.add(render + download);
        spanMs.add(elapsed(mark[0], slot.downloaded.back()));
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
        slot.busy.store(false);
    }

    // Page-locks every grain of every writer before the first output grain: locking a grain
    // takes milliseconds the first time, which made the first output grains late. Opening an
    // old index and cancelling it leaves the new flow as it was.
    void lockWriterGrains(HostRegistry& registry)
    {
        auto const now = timestampToIndex(cfg.rate, mxlGetTime());
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
            statsCv.wait_for(lock, std::chrono::seconds(1), [&] { return !run.load(); });
            if (mediamtx && run.load())
            {
                mediamtxRunning.store(mediamtx->poll());
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
        for (auto& input : inputs)
        {
            Input* in = input.get();
            in->thread = std::thread([this, in] { inputMain(*in); });
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
        logInfo("engine_started", {{"gpu", gpuName}, {"inputs", inputs.size()}, {"mes", cfg.mes}, {"latency", cfg.latency}, {"work_format", cfg.workFormat},
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
        for (auto& input : inputs)
        {
            if (input->thread.joinable())
            {
                input->thread.join();
            }
            input->recent.clear();
        }
        for (auto& slot : slots)
        {
            slot.held.clear();
        }
        for (auto& output : outputs)
        {
            if (output->writer != nullptr)
            {
                mxlReleaseFlowWriter(outInstance, output->writer);
                output->writer = nullptr;
            }
        }
        if (outInstance != nullptr)
        {
            mxlDestroyInstance(outInstance);
            outInstance = nullptr;
        }
        encoder.reset();
        if (mediamtx)
        {
            mediamtx->stop();
            mediamtx.reset();
        }
        cudaDeviceSynchronize();
        for (auto* list : {&inWork, &pgmWork, &pvwWork})
        {
            for (auto& buffer : *list)
            {
                gpu::freeWork(buffer);
            }
        }
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
        cudaStreamDestroy(renderStream);
        cudaStreamDestroy(downloadStream);
        logInfo("engine_stopped", {{"rendered", rendered.load()}, {"committed", committed.load()}, {"skipped", skipped.load()}});
    }

    // ---- status --------------------------------------------------------------------------

    json status() const
    {
        json out;
        out["engine"] = {{"format", cfg.format},
            {"latency_grains", cfg.latency},
            {"mes", cfg.mes},
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
        json list = json::array();
        auto const now = mxlGetTime();
        for (auto const& input : inputs)
        {
            std::string state;
            {
                std::lock_guard lock{input->mu};
                state = input->state;
            }
            auto const lastAt = input->lastGrainAt.load();
            if (state == "running" && (lastAt == 0 || now > lastAt + periodNs(cfg.rate) * static_cast<std::uint64_t>(cfg.holdGrains)))
            {
                state = "no_signal";
            }
            list.push_back({{"input", input->number},
                {"label", input->cfg.label},
                {"domain", input->cfg.domain},
                {"flow", input->cfg.flow},
                {"state", state},
                {"grains", input->grains.load()},
                {"late", input->late.load()},
                {"missing", input->missing.load()},
                {"resyncs", input->resyncs.load()},
                {"catchups", input->catchups.load()},
                {"invalid", input->invalid.load()},
                {"reopens", input->reopens.load()},
                {"pool_exhausted", input->exhausted.load()},
                {"unpinned_uploads", input->unpinned.load()},
                {"upload_ms", input->upload.toJson()}});
        }
        out["inputs"] = list;
        json mes = json::array();
        for (auto const& view : mixer.view())
        {
            auto const& pgm = *outputs[static_cast<std::size_t>(2 * (view.me - 1))];
            auto const& pvw = *outputs[static_cast<std::size_t>(2 * (view.me - 1) + 1)];
            mes.push_back({{"me", view.me},
                {"program", sourceName(view.program)},
                {"preview", sourceName(view.preview)},
                {"mixing", view.mixing},
                {"frames", view.frames},
                {"position", view.position},
                {"pgm", {{"flow", pgm.id}, {"label", pgm.label}, {"committed", pgm.committed.load()}, {"failed", pgm.failed.load()}}},
                {"pvw", {{"flow", pvw.id}, {"label", pvw.label}, {"committed", pvw.committed.load()}, {"failed", pvw.failed.load()}}}});
        }
        out["mes"] = mes;
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
    for (auto const& input : s["inputs"])
    {
        std::string const l = "input=\"" + std::to_string(input["input"].get<int>()) + "\"";
        metric("input_grains_total", input["grains"].get<double>(), l);
        metric("input_late_total", input["late"].get<double>(), l);
        metric("input_missing_total", input["missing"].get<double>(), l);
        metric("input_resyncs_total", input["resyncs"].get<double>(), l);
        metric("input_reopens_total", input["reopens"].get<double>(), l);
        metric("input_running", input["state"] == "running" ? 1.0 : 0.0, l);
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
        if (tile.kind == MosaicTile::Kind::Input)
        {
            kind = "input";
            label = cfg.inputs[static_cast<std::size_t>(tile.index - 1)].label;
        }
        else
        {
            kind = tile.kind == MosaicTile::Kind::Program ? "program" : "preview";
            label = "ME" + std::to_string(tile.index) + (tile.kind == MosaicTile::Kind::Program ? " PGM" : " PVW");
        }
        tiles.push_back({{"id", tile.id}, {"kind", kind}, {"label", label}, {"x", tile.x}, {"y", tile.y}, {"w", tile.w}, {"h", tile.h}});
    }
    // The page plays <base>/<path>/whep; an empty base means the own MediaMTX on the page's host.
    return json{{"width", cfg.width}, {"height", cfg.height}, {"fps", cfg.mosaicFps}, {"path", cfg.streamPath("mosaic")}, {"whep_base", cfg.whepBase},
        {"hls_base", cfg.hlsBase}, {"whep_port", cfg.whepPort}, {"hls_port", cfg.hlsPort}, {"tiles", tiles}}
        .dump(2);
}
} // namespace fxeng
