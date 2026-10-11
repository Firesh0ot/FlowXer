// Engine settings from a JSON file (FLOWXER_ENGINE_CONFIG, written by FlowXer's control plane)
// and the environment (same key names; the environment wins). FlowXer starts the engine with
// the structure (inputs, clip players, key inputs, MEs, output flows) and routes the inputs at
// run time over the control API.
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

// An MXL route: domain directory + flow id. An empty flow is "not routed".
struct Route
{
    std::string domain;
    std::string flow;

    bool operator==(Route const& other) const
    {
        return domain == other.domain && flow == other.flow;
    }
};

struct InputConfig
{
    std::string label;
    // Initial routes (FlowXer routes the inputs at run time; IS-05).
    Route video;
    Route audio;
};

struct PlayerConfig
{
    std::string label;
};

// A key input (v210a fill + key in one flow) and the DSK that keys it over an ME's outputs.
struct KeyConfig
{
    std::string label;
    Route route;
    // The ME whose Program the DSK keys (P2: ME 1); keys have no preview.
    int me = 1;
};

// The MXL flows of one ME. An empty id is generated (stable for domain + ME + kind); an id
// that already exists in the domain is opened (MXL create-or-open), so the engine takes over
// the flows of a stopped mixer and their readers keep reading.
struct OutputConfig
{
    std::string domain;
    // id in domain_def.json when the engine creates the domain.
    std::string domainId;
    std::string pgmVideo;
    std::string pgmAudio;
    std::string pvwVideo;
    // Flow labels "<label> PGM Video" etc. and NMOS group hint "<group>:Video"; empty: the
    // engine's label and "ME<m>".
    std::string label;
    std::string group;
};

constexpr int kMaxMes = 4;
// Live inputs and clip players together (with Black 24 sources, as FlowXer's console).
constexpr int kMaxSources = 23;
constexpr int kMaxKeys = 4;
constexpr int kMaxLatency = 10;

struct Config
{
    std::vector<InputConfig> inputs;
    std::vector<PlayerConfig> players;
    std::vector<KeyConfig> keys;
    // Rendering MEs (1-4); every one renders every grain.
    int mes = 1;
    int latency = 4;
    std::string format = "1080p50";
    int width = 1920;
    int height = 1080;
    Rate rate{50, 1};
    // Default MXL output domain of the MEs (PGM video + audio, PVW video).
    std::string outputDomain = "/Volumes/mxl/flowxer-engine";
    std::string outputDomainId;
    // One entry per ME (FLOWXER_ENGINE_OUTPUTS), filled with the defaults.
    std::vector<OutputConfig> outputs;
    // PVW video flows; false: PVW is rendered for the mosaic only, never downloaded.
    bool pvwOutputs = true;
    // PGM audio channels of a flow the engine creates (an existing flow keeps its own).
    int audioChannels = 2;
    // Prefix of generated flow labels.
    std::string label = "FlowXer";
    // Internal working format: yuv16 (10-bit 4:2:2 in 16-bit planes) or rgba16f.
    std::string workFormat = "yuv16";
    int gpu = 0;
    std::string httpBind = "127.0.0.1";
    int httpPort = 9630;
    // An input that stops (a stalled or re-created mirror) holds its last grain this long,
    // then turns black; 0: holds it until grains come again.
    int holdGrains = 0;
    // Longest wait (ms) for input audio that is not there yet when its grain is written.
    int audioWaitMs = 8;
    bool mosaic = true;
    int mosaicFps = 25;
    int mosaicKbps = 6000;
    // Preview contract (platform spec 11.5): publish to a shared MediaMTX at publishUrl
    // (RTSP), or run an own MediaMTX when it is empty. Streams are <pathPrefix>/<stream>.
    std::string publishUrl;
    std::string pathPrefix = "flowxer";
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
