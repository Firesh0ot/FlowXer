// flowxer-engine: the FlowXer mix engine prototype (P1). See engine/README.md.
#include "core/config.hpp"
#include "engine/api.hpp"
#include "engine/engine.hpp"
#include "engine/gpu.hpp"
#include "engine/http.hpp"
#include "engine/log.hpp"

#include <cuda_runtime_api.h>
#include <nlohmann/json.hpp>
#include <pthread.h>
#include <sys/resource.h>

#include <csignal>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <string>

extern char** environ;

namespace
{
std::map<std::string, std::string> environment()
{
    std::map<std::string, std::string> env;
    for (char** entry = environ; entry != nullptr && *entry != nullptr; ++entry)
    {
        std::string const text(*entry);
        auto const eq = text.find('=');
        if (eq != std::string::npos)
        {
            env.emplace(text.substr(0, eq), text.substr(eq + 1));
        }
    }
    return env;
}

// What the engine will read and write (flow ids that are empty here are generated at start).
nlohmann::json configSummary(fxeng::Config const& cfg)
{
    nlohmann::json inputs = nlohmann::json::array();
    for (auto const& in : cfg.inputs)
    {
        inputs.push_back({{"label", in.label}, {"domain", in.domain}, {"video_flow", in.flow.empty() ? "(black)" : in.flow}, {"audio_domain", in.audioDomain},
            {"audio_flow", in.audioFlow}});
    }
    nlohmann::json outputs = nlohmann::json::array();
    for (std::size_t m = 0; m < cfg.outputs.size(); ++m)
    {
        auto const& o = cfg.outputs[m];
        outputs.push_back({{"me", m + 1}, {"domain", o.domain}, {"domain_id", o.domainId}, {"pgm_video", o.pgmVideo}, {"pgm_audio", o.pgmAudio},
            {"pvw_video", o.pvwVideo}});
    }
    return {{"format", cfg.format}, {"mes", cfg.mes}, {"latency_grains", cfg.latency}, {"inputs", inputs}, {"outputs", outputs},
        {"audio_channels", cfg.audioChannels}, {"http", cfg.httpBind + ":" + std::to_string(cfg.httpPort)},
        {"preview", {{"mode", cfg.ownMediamtx() ? "own" : "shared"}, {"publish", cfg.rtspBase() + "/" + cfg.streamPath("mosaic")}, {"whep_base", cfg.whepBase}}},
        {"tally_tsl", cfg.tallyTsl}};
}

int selfTest()
{
    char const* gpu = std::getenv("FLOWXER_ENGINE_GPU");
    if (cudaSetDevice(gpu != nullptr ? std::atoi(gpu) : 0) != cudaSuccess)
    {
        std::cerr << "no CUDA device\n";
        return 1;
    }
    std::string report;
    bool const ok = fxeng::gpu::selfTest(report);
    std::cout << (ok ? "selftest ok: " : "selftest FAILED: ") << report << "\n";
    return ok ? 0 : 1;
}
} // namespace

int main(int argc, char** argv)
{
    bool checkOnly = false;
    for (int i = 1; i < argc; ++i)
    {
        if (std::strcmp(argv[i], "--check") == 0)
        {
            checkOnly = true;
            continue;
        }
        if (std::strcmp(argv[i], "--help") == 0 || std::strcmp(argv[i], "-h") == 0)
        {
            std::cout << "flowxer-engine " << FXENG_VERSION << "\nUsage: flowxer-engine [--selftest | --check]\n"
                      << "Settings come from the environment (or FLOWXER_ENGINE_CONFIG); see engine/README.md.\n"
                      << "--check prints the resolved inputs, outputs and preview settings and exits.\n";
            return 0;
        }
        if (std::strcmp(argv[i], "--version") == 0)
        {
            std::cout << FXENG_VERSION << "\n";
            return 0;
        }
        if (std::strcmp(argv[i], "--selftest") == 0)
        {
            return selfTest();
        }
    }
    // MXL keeps one descriptor per grain: 8 inputs and 8 outputs pass Docker's soft limit.
    rlimit files{};
    if (getrlimit(RLIMIT_NOFILE, &files) == 0 && files.rlim_cur < files.rlim_max)
    {
        files.rlim_cur = files.rlim_max;
        setrlimit(RLIMIT_NOFILE, &files);
    }
    fxeng::Config cfg;
    try
    {
        cfg = fxeng::loadConfig(environment());
    }
    catch (fxeng::ConfigError const& ex)
    {
        fxeng::logError("config", {{"error", ex.what()}});
        return 78;
    }
    if (checkOnly)
    {
        std::cout << configSummary(cfg).dump(2) << "\n";
        return 0;
    }
    // SIGINT/SIGTERM go to sigwait below: block them before any thread starts.
    sigset_t signals;
    sigemptyset(&signals);
    sigaddset(&signals, SIGINT);
    sigaddset(&signals, SIGTERM);
    pthread_sigmask(SIG_BLOCK, &signals, nullptr);

    fxeng::Engine engine(cfg);
    fxeng::HttpServer server;
    try
    {
        engine.start();
        server.start(cfg.httpBind, cfg.httpPort, [&engine](fxeng::HttpRequest const& request) { return fxeng::handleRequest(engine, request); });
    }
    catch (std::exception const& ex)
    {
        fxeng::logError("start", {{"error", ex.what()}});
        server.stop();
        engine.stop();
        return 75;
    }
    fxeng::logInfo("http_listen", {{"bind", cfg.httpBind}, {"port", cfg.httpPort}, {"version", FXENG_VERSION}});
    int signal = 0;
    sigwait(&signals, &signal);
    fxeng::logInfo("stopping", {{"signal", signal}});
    server.stop();
    engine.stop();
    return 0;
}
