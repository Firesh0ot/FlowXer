// The engine's control API, on localhost only (FlowXer's control plane is the one client):
//   GET  /status, /metrics (Prometheus), /mosaic/map, /livez
//   PUT  /input/{k}   {"video": {"domain", "flow"}, "audio": {"domain", "flow"}}   route live input k
//   PUT  /key/{k}     {"domain", "flow"}                                           route keyer k's key input
//   POST /player/{k}/load {"path", "kind": "clip"|"still", "loop", "play_on_cut"} ("" path: empty)
//   POST /player/{k}/cue {"frame"}, /play, /pause, /stop
//   POST /me/{m}/preview {"source"}, /program {"source"}
//   POST /me/{m}/transition {"type", "frames", "pattern", "reverse", "soft", "border", "border_color",
//                            "ties": [keyer k, ...]}   the upstream keyers Cut, Auto and the T-bar take along
//   POST /me/{m}/take {"source", "frames", "preview", "type", "ties"}   frames 0: cut; preview: Preview
//                     afterwards; ties: true takes the ME's tied keyers along
//   POST /me/{m}/cut, /auto {"frames"}, /position {"t"}, /ftb {"on"}, /ftb/auto {"frames"}
//   POST /keyer/{k} {"on"}, /keyer/{k}/auto {"frames"},
//        /keyer/{k}/settings {"mode": "linear"|"luma"|"pip", "source", "clip", "soft", "gain", "invert", "fly",
//                             "dme": the box fields to change}
//   PUT  /keyer/{k}/keyframes {"keyframes": [{box fields, "frames", "ease"}, ...]}   up to 8
//   POST /keyer/{k}/run {"to", "reverse", "loop": "off"|"wrap"|"pingpong", "frames": 0}, /keyer/{k}/stop
//   POST /grab {"source": "in<k>"|"cp<k>"|"me<n>", "bus": "pgm"|"pvw", "path"}   a frame grab of the next
//        grain as raw v210 into path; answers its id at once
//   GET  /grab/{id}   its state (pending, copying, writing, done, failed), output grain, TAI time, ms
// Sources: "in1".., "cp1".., "black", "me2".."me4".
#pragma once

#include "core/mixer.hpp"
#include "engine/http.hpp"

#include <nlohmann/json.hpp>

namespace fxeng
{
class Engine;

HttpResponse handleRequest(Engine& engine, HttpRequest const& request);
// A keyer as /status and the keyer commands show it: on air, mode, fly, its box (the last grain
// rendered), its keyframe count and its run.
nlohmann::json keyerJson(KeyView const& key);
} // namespace fxeng
