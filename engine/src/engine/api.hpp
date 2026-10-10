// The engine's control API, on localhost only (FlowXer's control plane is the one client):
//   GET  /status, /metrics (Prometheus), /mosaic/map, /livez
//   PUT  /input/{k}   {"video": {"domain", "flow"}, "audio": {"domain", "flow"}}   route live input k
//   PUT  /key/{k}     {"domain", "flow"}                                           route key input k
//   POST /player/{k}/load {"path", "kind": "clip"|"still", "loop", "play_on_cut"} ("" path: empty)
//   POST /player/{k}/cue {"frame"}, /play, /pause, /stop
//   POST /me/{m}/preview {"source"}, /program {"source"}
//   POST /me/{m}/take {"source", "frames", "preview"}   frames 0: cut; preview: Preview afterwards
//   POST /me/{m}/cut, /auto {"frames"}
//   PUT  /dsk/{k} {"on"}, POST /dsk/{k}/auto {"frames"}
// Sources: "in1".., "cp1".., "black", "me2".."me4".
#pragma once

#include "engine/http.hpp"

namespace fxeng
{
class Engine;

HttpResponse handleRequest(Engine& engine, HttpRequest const& request);
} // namespace fxeng
