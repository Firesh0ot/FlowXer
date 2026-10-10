#include "test.hpp"

#include "core/audio.hpp"
#include "core/config.hpp"
#include "core/mixer.hpp"
#include "core/mosaic_map.hpp"
#include "core/pixel.hpp"
#include "core/tai.hpp"
#include "core/tsl.hpp"
#include "core/v210.hpp"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <set>

using namespace fxeng;

namespace
{
void fillRandom(Planar422& p, unsigned seed)
{
    unsigned state = seed;
    auto const next = [&state] {
        state = state * 1664525u + 1013904223u;
        return static_cast<std::uint16_t>((state >> 8) % 1024);
    };
    for (auto& v : p.y)
    {
        v = next();
    }
    for (auto& v : p.cb)
    {
        v = next();
    }
    for (auto& v : p.cr)
    {
        v = next();
    }
}

bool configThrows(std::map<std::string, std::string> const& env)
{
    try
    {
        loadConfig(env);
    }
    catch (ConfigError const&)
    {
        return true;
    }
    return false;
}

std::string const kInputs = R"([{"domain":"/Volumes/mxl/a","flow":"f1","label":"Cam 1"},{"domain":"/Volumes/mxl/b","flow":"f2"}])";
} // namespace

// ---- v210 -------------------------------------------------------------------------------

TEST(v210_group_layout)
{
    std::uint16_t const y[6] = {1, 2, 3, 4, 5, 6};
    std::uint16_t const cb[3] = {7, 8, 9};
    std::uint16_t const cr[3] = {10, 11, 12};
    std::uint32_t w[4];
    packGroup(y, cb, cr, w);
    // SMPTE v210: Cb0 Y0 Cr0 | Y1 Cb1 Y2 | Cr1 Y3 Cb2 | Y4 Cr2 Y5, 10 bits each from bit 0.
    CHECK_EQ(w[0], 7u | (1u << 10) | (10u << 20));
    CHECK_EQ(w[1], 2u | (8u << 10) | (3u << 20));
    CHECK_EQ(w[2], 11u | (4u << 10) | (9u << 20));
    CHECK_EQ(w[3], 5u | (12u << 10) | (6u << 20));
    std::uint16_t y2[6];
    std::uint16_t cb2[3];
    std::uint16_t cr2[3];
    unpackGroup(w, y2, cb2, cr2);
    CHECK(std::memcmp(y, y2, sizeof(y)) == 0);
    CHECK(std::memcmp(cb, cb2, sizeof(cb)) == 0);
    CHECK(std::memcmp(cr, cr2, sizeof(cr)) == 0);
}

TEST(v210_round_trip)
{
    // 1920 is whole groups and blocks; 1280 ends in a partial group and a padded block.
    for (int width : {1920, 1280})
    {
        int const height = 4;
        Planar422 a;
        a.allocate(width, height);
        fillRandom(a, static_cast<unsigned>(width));
        int const rowBytes = v210RowBytes(width);
        std::vector<std::uint8_t> packed(static_cast<std::size_t>(rowBytes) * height, 0xff);
        packV210(a, packed.data(), rowBytes);
        Planar422 b;
        b.allocate(width, height);
        unpackV210(packed.data(), rowBytes, b);
        CHECK(a.y == b.y);
        CHECK(a.cb == b.cb);
        CHECK(a.cr == b.cr);
        // Line padding after the last group is zero.
        int const used = ((width + 5) / 6) * 16;
        for (int x = used; x < rowBytes; ++x)
        {
            CHECK_EQ(static_cast<int>(packed[static_cast<std::size_t>(x)]), 0);
        }
    }
    CHECK_EQ(v210RowBytes(1920), 5120);
    CHECK_EQ(v210RowBytes(1280), 3456);
}

// ---- pixel maths ------------------------------------------------------------------------

TEST(dissolve_maths)
{
    CHECK_EQ(mix10(100, 900, 0.f), 100);
    CHECK_EQ(mix10(100, 900, 1.f), 900);
    CHECK_EQ(mix10(100, 200, 0.5f), 150);
    CHECK_EQ(mix10(64, 940, 0.25f), 283);
    CHECK_EQ(mix10(940, 64, 0.25f), 721);
    // Halfway rounds up; the result stays within 10 bits.
    CHECK_EQ(mix10(0, 1, 0.5f), 1);
    CHECK_EQ(mix10(1023, 1023, 0.7f), 1023);
    // Monotonic over a whole transition.
    int previous = -1;
    for (int k = 0; k <= 25; ++k)
    {
        int const v = mix10(64, 940, static_cast<float>(k) / 25.f);
        CHECK(v >= previous);
        previous = v;
    }
    CHECK_EQ(previous, 940);
}

TEST(ycbcr_rgb_round_trip)
{
    int worst = 0;
    for (int y = 64; y <= 940; y += 17)
    {
        for (int c = 64; c <= 960; c += 32)
        {
            std::uint16_t yy = 0;
            std::uint16_t cb = 0;
            std::uint16_t cr = 0;
            rgbToYcbcr(ycbcrToRgb(static_cast<std::uint16_t>(y), static_cast<std::uint16_t>(c), static_cast<std::uint16_t>(1024 - c)), yy, cb, cr);
            worst = std::max({worst, std::abs(yy - y), std::abs(cb - c), std::abs(cr - (1024 - c))});
        }
    }
    CHECK(worst <= 1);
    Rgb const white = ycbcrToRgb(940, 512, 512);
    CHECK(std::abs(white.r - 1.f) < 1e-4f && std::abs(white.g - 1.f) < 1e-4f && std::abs(white.b - 1.f) < 1e-4f);
    CHECK_EQ(static_cast<int>(to8(940)), 235);
    CHECK_EQ(static_cast<int>(to8(64)), 16);
    CHECK_EQ(static_cast<int>(to8(1023)), 255);
}

// ---- MEs, re-entry, transitions -----------------------------------------------------------

TEST(reentry_order_and_rules)
{
    Mixer mixer(4, 8);
    auto const order = mixer.renderOrder();
    CHECK(order == (std::vector<int>{4, 3, 2, 1}));
    auto const plans = mixer.frame(1000);
    CHECK_EQ(plans.size(), 4u);
    CHECK_EQ(plans.front().me, 4);
    CHECK_EQ(plans.back().me, 1);
    // ME m takes the Program of ME n only when n > m: no loop can exist.
    CHECK(mixer.sourceAllowed(1, Source{Source::Kind::Me, 2}));
    CHECK(mixer.sourceAllowed(1, Source{Source::Kind::Me, 4}));
    CHECK(mixer.sourceAllowed(3, Source{Source::Kind::Me, 4}));
    CHECK(!mixer.sourceAllowed(2, Source{Source::Kind::Me, 2}));
    CHECK(!mixer.sourceAllowed(2, Source{Source::Kind::Me, 1}));
    CHECK(!mixer.sourceAllowed(4, Source{Source::Kind::Me, 5}));
    CHECK(mixer.sourceAllowed(4, Source{Source::Kind::Input, 8}));
    CHECK(!mixer.sourceAllowed(4, Source{Source::Kind::Input, 9}));
    CHECK(!mixer.sourceAllowed(4, Source{Source::Kind::Input, 0}));
    CHECK(mixer.setProgram(1, Source{Source::Kind::Me, 2}) == MixResult::Ok);
    CHECK(mixer.setPreview(2, Source{Source::Kind::Me, 1}) == MixResult::BadSource);
    CHECK(mixer.setPreview(5, Source{Source::Kind::Input, 1}) == MixResult::BadMe);
    // With 2 MEs there is no ME 3 to re-enter.
    Mixer two(2, 8);
    CHECK(!two.sourceAllowed(1, Source{Source::Kind::Me, 3}));
    CHECK(two.sourceAllowed(1, Source{Source::Kind::Me, 2}));
}

TEST(source_names)
{
    CHECK(parseSource("in1").has_value());
    CHECK(parseSource("in8")->index == 8);
    CHECK(parseSource("me2")->kind == Source::Kind::Me);
    CHECK(!parseSource("in0").has_value());
    CHECK(!parseSource("me").has_value());
    CHECK(!parseSource("pgm1").has_value());
    CHECK(!parseSource("in01").has_value());
    CHECK(sourceName(Source{Source::Kind::Me, 3}) == "me3");
}

TEST(mix_follows_the_grain_index)
{
    Mixer mixer(1, 8);
    CHECK(mixer.setProgram(1, Source{Source::Kind::Input, 1}) == MixResult::Ok);
    CHECK(mixer.setPreview(1, Source{Source::Kind::Input, 2}) == MixResult::Ok);
    CHECK(mixer.autoMix(1, 0) == MixResult::BadFrames);
    CHECK(mixer.autoMix(1, 4) == MixResult::Ok);
    CHECK(mixer.autoMix(1, 4) == MixResult::Busy);
    CHECK(mixer.setPreview(1, Source{Source::Kind::Input, 3}) == MixResult::Busy);
    auto p = mixer.frame(100);
    CHECK(p[0].mixing);
    CHECK(p[0].t == 0.25f);
    CHECK(p[0].a == (Source{Source::Kind::Input, 1}));
    CHECK(p[0].b == (Source{Source::Kind::Input, 2}));
    // Grain 101 was skipped: the Mix still ends on time.
    p = mixer.frame(102);
    CHECK(p[0].t == 0.75f);
    p = mixer.frame(103);
    CHECK(p[0].mixing && p[0].t == 1.f);
    // After the last grain the buses have swapped.
    p = mixer.frame(104);
    CHECK(!p[0].mixing);
    CHECK(p[0].a == (Source{Source::Kind::Input, 2}));
    CHECK(p[0].preview == (Source{Source::Kind::Input, 1}));
}

TEST(cut_swaps_and_ends_a_mix)
{
    Mixer mixer(1, 8);
    CHECK(mixer.cut(1) == MixResult::Ok);
    auto p = mixer.frame(1);
    CHECK(p[0].a == (Source{Source::Kind::Input, 2}));
    CHECK(p[0].preview == (Source{Source::Kind::Input, 1}));
    CHECK(mixer.autoMix(1, 50) == MixResult::Ok);
    mixer.frame(2);
    CHECK(mixer.view()[0].mixing);
    CHECK(mixer.cut(1) == MixResult::Ok);
    p = mixer.frame(3);
    CHECK(!p[0].mixing);
    CHECK(p[0].a == (Source{Source::Kind::Input, 1}));
}

// ---- mosaic map ------------------------------------------------------------------------

TEST(mosaic_tile_map)
{
    auto const tiles = mosaicTiles(1920, 1080, 8, 4);
    CHECK_EQ(tiles.size(), 16u);
    std::set<std::string> ids;
    std::set<std::pair<int, int>> corners;
    for (auto const& t : tiles)
    {
        ids.insert(t.id);
        corners.insert({t.x, t.y});
        CHECK_EQ(t.w, 480);
        CHECK_EQ(t.h, 270);
        CHECK(t.x % 2 == 0 && t.y % 2 == 0);
        CHECK(t.x >= 0 && t.y >= 0 && t.x + t.w <= 1920 && t.y + t.h <= 1080);
    }
    // 16 different tiles on 16 different grid cells: they cover the canvas without overlap.
    CHECK_EQ(ids.size(), 16u);
    CHECK_EQ(corners.size(), 16u);
    CHECK(tiles[0].id == "in1" && tiles[0].x == 0 && tiles[0].y == 0);
    CHECK(tiles[7].id == "in8" && tiles[7].x == 1440 && tiles[7].y == 270);
    auto const find = [&](std::string const& id) {
        for (auto const& t : tiles)
        {
            if (t.id == id)
            {
                return t;
            }
        }
        return MosaicTile{};
    };
    CHECK(find("me1-pgm").y == 540 && find("me1-pgm").x == 0);
    CHECK(find("me4-pvw").y == 810 && find("me4-pvw").x == 1440);
    CHECK_EQ(mosaicTiles(1920, 1080, 3, 1).size(), 5u);
}

// ---- TAI grid --------------------------------------------------------------------------

TEST(tai_index_arithmetic)
{
    for (Rate rate : {Rate{50, 1}, Rate{25, 1}, Rate{60000, 1001}})
    {
        for (std::uint64_t index : {1ull, 2ull, 88'000'000'000ull, 88'000'000'001ull})
        {
            CHECK_EQ(timestampToIndex(rate, indexToTimestamp(rate, index)), index);
            auto const start = windowStart(rate, index);
            CHECK_EQ(timestampToIndex(rate, start), index);
            CHECK_EQ(timestampToIndex(rate, start - 1), index - 1);
        }
    }
    CHECK_EQ(periodNs(Rate{50, 1}), 20'000'000ull);
    // Grain i is current from T(i) - 10 ms to T(i) + 10 ms at 50p (MXL rounds).
    CHECK_EQ(windowStart(Rate{50, 1}, 100), indexToTimestamp(Rate{50, 1}, 100) - 10'000'000ull);
    CHECK_EQ(inputIndexFor(100, 2), 98ull);
    CHECK_EQ(inputIndexFor(1, 2), 0ull);
}

TEST(skip_on_late_never_slides)
{
    // Start: the current grain, nothing counted.
    auto s = nextOutputIndex(0, 500);
    CHECK_EQ(s.index, 500ull);
    CHECK_EQ(s.skipped, 0ull);
    // On time: the next grain (its window has not started yet).
    s = nextOutputIndex(501, 500);
    CHECK_EQ(s.index, 501ull);
    CHECK_EQ(s.skipped, 0ull);
    s = nextOutputIndex(501, 501);
    CHECK_EQ(s.index, 501ull);
    // Late by three grains: render the current one, count three skipped.
    s = nextOutputIndex(501, 504);
    CHECK_EQ(s.index, 504ull);
    CHECK_EQ(s.skipped, 3ull);
    // The clock stepped back: restart at the current grain.
    s = nextOutputIndex(900, 500);
    CHECK_EQ(s.index, 500ull);
    CHECK_EQ(s.skipped, 0ull);
}

// ---- configuration ---------------------------------------------------------------------

TEST(config_defaults_and_errors)
{
    auto const cfg = loadConfig({{"FLOWXER_ENGINE_INPUTS", kInputs}});
    CHECK_EQ(cfg.inputs.size(), 2u);
    CHECK(cfg.inputs[0].label == "Cam 1");
    CHECK(cfg.inputs[1].label == "Input 2");
    CHECK_EQ(cfg.mes, 4);
    CHECK_EQ(cfg.latency, 2);
    CHECK_EQ(cfg.width, 1920);
    CHECK_EQ(cfg.rate.num, 50);
    CHECK_EQ(cfg.httpPort, 9630);
    CHECK(cfg.httpBind == "127.0.0.1");
    CHECK(cfg.workFormat == "yuv16");
    auto const other = loadConfig({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_FORMAT", "1080p5994"}, {"FLOWXER_ENGINE_MES", "2"}});
    CHECK_EQ(other.rate.num, 60000);
    CHECK_EQ(other.rate.den, 1001);
    CHECK_EQ(other.mes, 2);
    CHECK(configThrows({}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", "[]"}}));
    // An input without a flow is a black slot.
    CHECK(loadConfig({{"FLOWXER_ENGINE_INPUTS", R"([{"domain":"/x"}])"}}).inputs[0].flow.empty());
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_MES", "5"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_LATENCY", "two"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_FORMAT", "1080i50"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_WORK_FORMAT", "rgb8"}}));
}

TEST(preview_contract)
{
    // Without PREVIEW_PUBLISH_URL: the own MediaMTX on localhost, streams under the own name.
    auto const own = loadConfig({{"FLOWXER_ENGINE_INPUTS", kInputs}});
    CHECK(own.ownMediamtx());
    CHECK(own.rtspBase() == "rtsp://127.0.0.1:8654");
    CHECK(own.streamPath("mosaic") == "flowxer-engine/mosaic");
    auto const shared = loadConfig({{"FLOWXER_ENGINE_INPUTS", kInputs},
        {"PREVIEW_PUBLISH_URL", "rtsp://mxl-mediamtx.mxl-platform.svc:8554/"},
        {"PREVIEW_PATH_PREFIX", "/show/vmix1/"},
        {"PREVIEW_WHEP_URL", "https://preview.example/whep"}});
    CHECK(!shared.ownMediamtx());
    CHECK(shared.rtspBase() == "rtsp://mxl-mediamtx.mxl-platform.svc:8554");
    CHECK(shared.streamPath("mosaic") == "show/vmix1/mosaic");
    CHECK(shared.whepBase == "https://preview.example/whep");
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"PREVIEW_PUBLISH_URL", "http://x:8554"}}));
}

// ---- drop-in: flows, audio, tally --------------------------------------------------------

TEST(outputs_and_input_audio)
{
    std::string const inputs = R"([{"domain":"/d","flow":"v1","audio":"a1","label":"Cam 1"},{"label":"black"},{"domain":"/d","flow":"v3","audio":"a3","audio_domain":"/e"}])";
    auto const cfg = loadConfig({{"FLOWXER_ENGINE_INPUTS", inputs},
        {"FLOWXER_ENGINE_MES", "2"},
        {"FLOWXER_ENGINE_OUTPUT_DOMAIN", "/Volumes/mxl/out"},
        {"FLOWXER_ENGINE_OUTPUTS", R"([{"me":1,"domain":"/Volumes/mxl/vmix1","domain_id":"fba22e51-0000-4000-8000-000000000000","pgm_video":"3924b34c-4532-57ab-8e21-cb87bccfa4e3","pgm_audio":"5ac3feb3-0d22-5835-8b07-fe175fc4d640"}])"}});
    CHECK(cfg.inputs[0].audioFlow == "a1" && cfg.inputs[0].audioDomain == "/d");
    CHECK(cfg.inputs[1].flow.empty() && cfg.inputs[1].label == "black");
    CHECK(cfg.inputs[2].audioDomain == "/e");
    auto const named = loadConfig({{"FLOWXER_ENGINE_INPUTS", R"([{"domain":"/d","video_flow":"v","audio_flow":"a","label":"Cam"}])"}});
    CHECK(named.inputs[0].flow == "v" && named.inputs[0].audioFlow == "a");
    CHECK_EQ(named.holdGrains, 0);
    CHECK_EQ(cfg.outputs.size(), 2u);
    CHECK(cfg.outputs[0].domain == "/Volumes/mxl/vmix1" && cfg.outputs[0].pgmVideo == "3924b34c-4532-57ab-8e21-cb87bccfa4e3");
    CHECK(cfg.outputs[0].pgmAudio == "5ac3feb3-0d22-5835-8b07-fe175fc4d640" && cfg.outputs[0].pvwVideo.empty());
    CHECK(cfg.outputs[1].domain == "/Volumes/mxl/out" && cfg.outputs[1].pgmVideo.empty());
    CHECK_EQ(cfg.audioChannels, 2);
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", R"([{"flow":"v1"}])"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_ENGINE_OUTPUTS", R"([{"me":5}])"}}));
    CHECK(configThrows({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_TALLY_TSL", "mxl-tally:8910"}}));
    CHECK(loadConfig({{"FLOWXER_ENGINE_INPUTS", kInputs}, {"FLOWXER_TALLY_TSL", "udp://mxl-tally:8910"}}).tallyTsl == "udp://mxl-tally:8910");
}

TEST(audio_crossfade)
{
    // Equal power: a² + b² = 1 all through the Mix.
    for (int k = 0; k <= 10; ++k)
    {
        auto const g = crossfade(static_cast<float>(k) / 10.f);
        CHECK(std::abs(g.a * g.a + g.b * g.b - 1.f) < 1e-5f);
    }
    CHECK(crossfade(0.f).a == 1.f && crossfade(0.f).b == 0.f);
    CHECK(crossfade(1.f).a == 0.f && crossfade(1.f).b == 1.f);
    // One grain of a 4-grain Mix ramps from 0.25 to 0.5; the next starts where it ended.
    std::vector<float> a(960, 1.f);
    std::vector<float> b(960, 0.f);
    std::vector<float> out(960);
    crossfadeBlock(a.data(), b.data(), out.data(), out.size(), 0.25f, 0.5f);
    CHECK(std::abs(out.back() - crossfade(0.5f).a) < 1e-6f);
    CHECK(out.front() < crossfade(0.25f).a && out.front() > crossfade(0.5f).a);
    for (std::size_t s = 1; s < out.size(); ++s)
    {
        CHECK(out[s] <= out[s - 1]);
    }
    // 48 kHz samples on the video grid: 960 per grain at 50p, the same sample index as MXL.
    auto const r = grainSamples(Rate{50, 1}, 1000);
    CHECK_EQ(r.first, 960000ull);
    CHECK_EQ(r.end - r.first, 960ull);
    std::uint64_t total = 0;
    for (std::uint64_t i = 0; i < 5; ++i)
    {
        auto const g = grainSamples(Rate{60000, 1001}, 1000 + i);
        total += g.end - g.first;
    }
    CHECK_EQ(total, 4004ull);
}

TEST(tsl_packets)
{
    std::vector<TslDisplay> displays{{0, "Cam 1", kTslRed, kTslOff}, {1, "Cam 2", kTslOff, kTslGreen}};
    auto const packets = tslPackets(2, displays);
    CHECK_EQ(packets.size(), 1u);
    auto const& p = packets[0];
    auto const u16 = [&](std::size_t at) { return static_cast<unsigned>(static_cast<unsigned char>(p[at]) | (static_cast<unsigned char>(p[at + 1]) << 8)); };
    // PBC counts everything after itself; VER 0, FLAGS 1 (UTF-16LE), SCREEN 2.
    CHECK_EQ(u16(0), static_cast<unsigned>(p.size() - 2));
    CHECK_EQ(static_cast<int>(p[2]), 0);
    CHECK_EQ(static_cast<int>(p[3]), 1);
    CHECK_EQ(u16(4), 2u);
    // Display 0: index 0, LH red (bits 4-5), brightness 3 (bits 6-7), 10 bytes of UTF-16LE.
    CHECK_EQ(u16(6), 0u);
    CHECK_EQ(u16(8), (1u << 4) | (3u << 6));
    CHECK_EQ(u16(10), 10u);
    CHECK(p[12] == 'C' && p[13] == 0);
    // Display 1 follows: RH green (bits 0-1).
    CHECK_EQ(u16(22), 1u);
    CHECK_EQ(u16(24), 2u | (3u << 6));
    // More displays than fit in 2048 bytes go into further packets.
    std::vector<TslDisplay> many(100, TslDisplay{0, std::string(20, 'x'), kTslOff, kTslOff});
    auto const split = tslPackets(1, many);
    // 100 displays of 46 bytes: 44 fit in one packet.
    CHECK_EQ(split.size(), 3u);
    for (auto const& packet : split)
    {
        CHECK(packet.size() <= 2048);
    }
    // TCP: DLE/STX, DLE doubled, DLE/ETX.
    auto const wrapped = tslWrapTcp(std::string("\xfe\x01", 2));
    CHECK(wrapped == std::string("\xfe\x02\xfe\xfe\x01\xfe\x03", 7));
}

TEST(tally_from_the_mixer)
{
    Mixer mixer(2, 3);
    mixer.setProgram(1, Source{Source::Kind::Me, 2});
    mixer.setPreview(1, Source{Source::Kind::Input, 3});
    mixer.setProgram(2, Source{Source::Kind::Input, 2});
    mixer.setPreview(2, Source{Source::Kind::Input, 1});
    auto const screens = tallyDisplays(mixer, {"A", "B", "C"});
    CHECK_EQ(screens.size(), 2u);
    auto const& me1 = screens[0].second;
    // Inputs 0..2 and the re-entry of ME 2 (index 1002).
    CHECK_EQ(me1.size(), 4u);
    CHECK(screens[0].first == 1 && me1[2].rh == kTslGreen && me1[3].index == 1002 && me1[3].lh == kTslRed);
    CHECK(me1[0].lh == kTslOff && me1[1].lh == kTslOff);
    auto const& me2 = screens[1].second;
    CHECK_EQ(me2.size(), 3u);
    CHECK(me2[1].lh == kTslRed && me2[0].rh == kTslGreen && me2[1].label == "B");
    // While a Mix runs, both of its sources are red.
    mixer.autoMix(2, 10);
    mixer.frame(100);
    auto const mixing = tallyDisplays(mixer, {"A", "B", "C"})[1].second;
    CHECK(mixing[0].lh == kTslRed && mixing[1].lh == kTslRed);
}
