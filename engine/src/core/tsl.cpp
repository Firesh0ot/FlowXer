#include "core/tsl.hpp"

#include <cstdint>

namespace fxeng
{
namespace
{
constexpr std::size_t kMaxPacket = 2048;
constexpr unsigned char kDle = 0xfe;

void put16(std::string& out, unsigned value)
{
    out.push_back(static_cast<char>(value & 0xffu));
    out.push_back(static_cast<char>((value >> 8) & 0xffu));
}

std::string utf16le(std::string const& utf8)
{
    std::string out;
    for (std::size_t i = 0; i < utf8.size();)
    {
        auto const c = static_cast<unsigned char>(utf8[i]);
        unsigned code = c;
        int extra = 0;
        if (c >= 0xf0)
        {
            code = c & 0x07u;
            extra = 3;
        }
        else if (c >= 0xe0)
        {
            code = c & 0x0fu;
            extra = 2;
        }
        else if (c >= 0xc0)
        {
            code = c & 0x1fu;
            extra = 1;
        }
        ++i;
        for (int k = 0; k < extra && i < utf8.size(); ++k, ++i)
        {
            code = (code << 6) | (static_cast<unsigned char>(utf8[i]) & 0x3fu);
        }
        if (code >= 0x10000)
        {
            code -= 0x10000;
            put16(out, 0xd800u + (code >> 10));
            put16(out, 0xdc00u + (code & 0x3ffu));
        }
        else
        {
            put16(out, code);
        }
    }
    return out;
}
} // namespace

std::vector<std::string> tslPackets(int screen, std::vector<TslDisplay> const& displays)
{
    std::vector<std::string> bodies(1);
    for (auto const& display : displays)
    {
        std::string message;
        auto const text = utf16le(display.label);
        put16(message, static_cast<unsigned>(display.index));
        // CONTROL: RH bits 0-1, text tally 2-3 (off), LH 4-5, brightness 6-7 (3).
        put16(message, static_cast<unsigned>((display.rh & 3) | ((display.lh & 3) << 4) | (3 << 6)));
        put16(message, static_cast<unsigned>(text.size()));
        message += text;
        // 6 header bytes: PBC, VER, FLAGS, SCREEN.
        if (!bodies.back().empty() && 6 + bodies.back().size() + message.size() > kMaxPacket)
        {
            bodies.emplace_back();
        }
        bodies.back() += message;
    }
    std::vector<std::string> packets;
    for (auto const& body : bodies)
    {
        std::string packet;
        put16(packet, static_cast<unsigned>(4 + body.size()));
        packet.push_back(0);  // VER 5.00
        packet.push_back(1);  // FLAGS: UTF-16LE
        put16(packet, static_cast<unsigned>(screen));
        packets.push_back(packet + body);
    }
    return packets;
}

std::string tslWrapTcp(std::string const& packet)
{
    std::string out{static_cast<char>(kDle), 0x02};
    for (char c : packet)
    {
        out.push_back(c);
        if (static_cast<unsigned char>(c) == kDle)
        {
            out.push_back(c);
        }
    }
    out.push_back(static_cast<char>(kDle));
    out.push_back(0x03);
    return out;
}

std::vector<std::pair<int, std::vector<TslDisplay>>> tallyDisplays(Mixer const& mixer, std::vector<std::string> const& inputLabels)
{
    std::vector<std::pair<int, std::vector<TslDisplay>>> screens;
    for (auto const& view : mixer.view())
    {
        auto const lit = [&](Source source) {
            TslDisplay display;
            if (source.kind == Source::Kind::Input)
            {
                display.index = source.index - 1;
                display.label = inputLabels[static_cast<std::size_t>(source.index - 1)];
            }
            else
            {
                display.index = 1000 + source.index;
                display.label = "ME " + std::to_string(source.index);
            }
            bool const program = source == view.program || (view.mixing && source == view.preview);
            display.lh = program ? kTslRed : kTslOff;
            display.rh = source == view.preview ? kTslGreen : kTslOff;
            return display;
        };
        std::vector<TslDisplay> displays;
        for (int i = 1; i <= static_cast<int>(inputLabels.size()); ++i)
        {
            displays.push_back(lit(Source{Source::Kind::Input, i}));
        }
        for (int n = view.me + 1; n <= mixer.mes(); ++n)
        {
            displays.push_back(lit(Source{Source::Kind::Me, n}));
        }
        screens.emplace_back(view.me, std::move(displays));
    }
    return screens;
}
} // namespace fxeng
