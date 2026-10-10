// Engine settings from the environment, optionally from a JSON file (FLOWXER_ENGINE_CONFIG;
// same key names, the environment wins). P1 has no NMOS: inputs are MXL domain + flow id.
#pragma once

#include "core/tai.hpp"

#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace fxeng
{
struct ConfigError : std::runtime_error
{
    using std::runtime_error::runtime_error;
};

struct InputConfig
{
    // MXL domain directory, e.g. /Volumes/mxl/player-lab.
    std::string domain;
    // Video flow id ("video_flow"); empty: the input is black (keeps its slot, e.g. for the
    // TSL numbering).
    std::string flow;
    std::string label;
    // Audio flow id ("audio_flow", float32), in audioDomain (default: domain); empty: silence.
    std::string audioDomain;
    std::string audioFlow;
};

// The MXL flows of one ME. An empty id is generated (stable for domain + ME + kind); an id
// that already exists in the domain is opened (MXL create-or-open), so the engine can take
// over the flows of a stopped mixer.
struct OutputConfig
{
    std::string domain;
    // id in domain_def.json when the engine creates the domain.
    std::string domainId;
    std::string pgmVideo;
    std::string pgmAudio;
    std::string pvwVideo;
};

struct Config
{
    std::vector<InputConfig> inputs;
    int mes = 4;
    int latency = 2;
    std::string format = "1080p50";
    int width = 1920;
    int height = 1080;
    Rate rate{50, 1};
    // Default MXL output domain of the MEs (PGM video + audio, PVW video).
    std::string outputDomain = "/Volumes/mxl/flowxer-engine";
    std::string outputDomainId;
    // One entry per ME (FLOWXER_ENGINE_OUTPUTS), filled with the defaults.
    std::vector<OutputConfig> outputs;
    // PGM audio channels of a flow the engine creates (an existing flow keeps its own).
    int audioChannels = 2;
    // Raw TSL 5.0 tally (udp://host:port or tcp://host:port); empty: off.
    std::string tallyTsl;
    // Name prefix of the output flow labels ("<label> ME1 PGM").
    std::string label = "FlowXer engine";
    // Internal working format: yuv16 (10-bit 4:2:2 in 16-bit planes) or rgba16f.
    std::string workFormat = "yuv16";
    int gpu = 0;
    std::string httpBind = "127.0.0.1";
    int httpPort = 9630;
    // An input that stops (a stalled or re-created mirror) holds its last grain this long,
    // then turns black; 0: holds it until grains come again.
    int holdGrains = 0;
    bool mosaic = true;
    int mosaicFps = 25;
    int mosaicKbps = 6000;
    // Preview contract (platform spec 11.5): publish to a shared MediaMTX at publishUrl
    // (RTSP), or run an own MediaMTX when it is empty. Streams are <pathPrefix>/<stream>.
    std::string publishUrl;
    std::string pathPrefix = "flowxer-engine";
    // Public bases the UI plays from (<base>/<prefix>/<stream>/whep or /index.m3u8);
    // empty: the own MediaMTX on the page's host.
    std::string whepBase;
    std::string hlsBase;
    // The own MediaMTX (only without publishUrl).
    std::string mediamtxBin = "mediamtx";
    std::string stateDir = "/tmp/flowxer-engine";
    int rtspPort = 8654;
    int whepPort = 8989;
    int hlsPort = 8988;
    int icePort = 8289;
    int apiPort = 9897;
    std::string publicIp;

    [[nodiscard]] bool ownMediamtx() const
    {
        return publishUrl.empty();
    }
    // rtsp://host:port to publish to.
    [[nodiscard]] std::string rtspBase() const
    {
        return ownMediamtx() ? "rtsp://127.0.0.1:" + std::to_string(rtspPort) : publishUrl;
    }
    [[nodiscard]] std::string streamPath(std::string const& stream) const
    {
        return pathPrefix.empty() ? stream : pathPrefix + "/" + stream;
    }
};

// Throws ConfigError with the offending key.
Config loadConfig(std::map<std::string, std::string> const& env);
bool parseFormat(std::string const& token, int& width, int& height, Rate& rate);
} // namespace fxeng
