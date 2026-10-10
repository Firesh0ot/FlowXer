#include "engine/mediamtx.hpp"

#include "engine/log.hpp"

#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

#include <chrono>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <thread>

extern char** environ;

namespace fxeng
{
namespace
{
long long nowSeconds()
{
    return std::chrono::duration_cast<std::chrono::seconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
} // namespace

std::string renderMediamtxConfig(Config const& cfg)
{
    std::string yml;
    yml += "logLevel: warn\n";
    yml += "api: true\n";
    yml += "apiAddress: 127.0.0.1:" + std::to_string(cfg.apiPort) + "\n";
    yml += "metrics: false\n";
    yml += "rtsp: true\n";
    yml += "rtspAddress: 127.0.0.1:" + std::to_string(cfg.rtspPort) + "\n";
    // TCP only: no RTP/RTCP UDP ports (8000/8001), so several instances fit on one host.
    yml += "rtspTransports: [tcp]\n";
    yml += "rtmp: false\n";
    yml += "srt: false\n";
    yml += "moq: false\n";
    yml += "hls: true\n";
    yml += "hlsAddress: :" + std::to_string(cfg.hlsPort) + "\n";
    yml += "hlsAllowOrigins: [\"*\"]\n";
    yml += "hlsVariant: lowLatency\n";
    yml += "hlsSegmentDuration: 1s\n";
    yml += "hlsPartDuration: 200ms\n";
    yml += "webrtc: true\n";
    yml += "webrtcAddress: :" + std::to_string(cfg.whepPort) + "\n";
    yml += "webrtcAllowOrigins: [\"*\"]\n";
    yml += "webrtcLocalUDPAddress: :" + std::to_string(cfg.icePort) + "\n";
    yml += "webrtcLocalTCPAddress: :" + std::to_string(cfg.icePort) + "\n";
    yml += cfg.publicIp.empty() ? "webrtcAdditionalHosts: []\n" : "webrtcAdditionalHosts: [\"" + cfg.publicIp + "\"]\n";
    yml += "webrtcICEServers2: []\n";
    yml += "pathDefaults:\n";
    yml += "  source: publisher\n";
    yml += "paths:\n";
    yml += "  all_others:\n";
    return yml;
}

MediamtxProcess::MediamtxProcess(Config const& cfg)
    : cfg_(cfg)
{
}

MediamtxProcess::~MediamtxProcess()
{
    stop();
}

void MediamtxProcess::start()
{
    std::filesystem::create_directories(cfg_.stateDir);
    configPath_ = (std::filesystem::path(cfg_.stateDir) / "mediamtx.yml").string();
    {
        std::ofstream out(configPath_, std::ios::binary | std::ios::trunc);
        out << renderMediamtxConfig(cfg_);
        if (!out.flush())
        {
            throw std::runtime_error("cannot write " + configPath_);
        }
    }
    if (!spawn())
    {
        throw std::runtime_error("cannot start " + cfg_.mediamtxBin);
    }
}

bool MediamtxProcess::spawn()
{
    // The engine blocks SIGINT/SIGTERM for sigwait; the child gets an empty mask and default
    // handlers so that stop() can end it.
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    sigset_t none;
    sigemptyset(&none);
    sigset_t defaults;
    sigemptyset(&defaults);
    sigaddset(&defaults, SIGINT);
    sigaddset(&defaults, SIGTERM);
    posix_spawnattr_setsigmask(&attr, &none);
    posix_spawnattr_setsigdefault(&attr, &defaults);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
    std::string bin = cfg_.mediamtxBin;
    std::string conf = configPath_;
    char* argv[] = {bin.data(), conf.data(), nullptr};
    int const rc = posix_spawnp(&pid_, bin.c_str(), nullptr, &attr, argv, environ);
    posix_spawnattr_destroy(&attr);
    lastStart_ = nowSeconds();
    if (rc != 0)
    {
        pid_ = -1;
        logError("mediamtx_spawn", {{"bin", bin}, {"error", std::to_string(rc)}});
        return false;
    }
    logInfo("mediamtx_started", {{"pid", pid_}, {"config", conf}});
    return true;
}

bool MediamtxProcess::poll()
{
    if (pid_ > 0)
    {
        int status = 0;
        if (waitpid(pid_, &status, WNOHANG) == 0)
        {
            return true;
        }
        logWarn("mediamtx_exited", {{"pid", pid_}, {"status", status}});
        pid_ = -1;
    }
    if (nowSeconds() - lastStart_ >= 3 && spawn())
    {
        ++restarts_;
        return true;
    }
    return false;
}

void MediamtxProcess::stop()
{
    if (pid_ <= 0)
    {
        return;
    }
    kill(pid_, SIGTERM);
    for (int i = 0; i < 30; ++i)
    {
        if (waitpid(pid_, nullptr, WNOHANG) != 0)
        {
            pid_ = -1;
            return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    kill(pid_, SIGKILL);
    waitpid(pid_, nullptr, 0);
    pid_ = -1;
}
} // namespace fxeng
