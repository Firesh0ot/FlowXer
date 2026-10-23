// Operator-screen widgets, without the console around them:
// /widget/bus?me=<n>[&transition=false][&theme=dark|light|transparent] shows one ME's Program and
// Preview rows with Cut, Auto and the transition type (hidden with transition=false);
// /widget/keys?me=<n>[&theme=...] shows one ME's keyers and DSKs with On/Off and Auto;
// /widget/dme?me=<n>&keyer=<k>[&theme=...] one upstream keyer's box: On/Off, Auto, Run back and
// forth, Stop and the presets; /widget/audio?me=<n>[&faders=false][&theme=...] one ME's audio
// strips (meter, MUTE, AFV, fader) and its master meter with the loudness;
// /widget/snapshots[?me=<n>][&all=false][&theme=...] recall buttons for one ME's snapshots and
// (unless all=false) those of all MEs; /widget/multiview[?me=<n>][&pvw=true][&theme=...] a wall of
// the mixer's pictures over WebRTC (each ME's Program, the sources) with labels and the tally of
// ME n (default 1). It runs on flowxer-gui's own
// origin (its /api); only the origins in WIDGET_FRAME_ANCESTORS may frame it. It posts
// {type: "widget-ready"} and {type: "widget-size", w, h} to the page that frames it.
import React from "react";
import ReactDOM from "react-dom/client";
import { AudioWidget } from "./components/AudioWidget";
import { BusWidget } from "./components/BusWidget";
import { DmeWidget } from "./components/DmeWidget";
import { KeysWidget } from "./components/KeysWidget";
import { MultiviewWidget } from "./components/MultiviewWidget";
import { SnapshotsWidget } from "./components/SnapshotsWidget";
import "./widget.css";

const params = new URLSearchParams(location.search);
// nginx serves /widget/<id>; the Vite dev server only /widget.html?widget=<id>&….
const widget = location.pathname.endsWith("/widget.html") ? `/${params.get("widget") ?? "bus"}` : location.pathname;
const theme = params.get("theme") || "dark";
document.documentElement.dataset.theme = theme;
// A frame is see-through only when its colour scheme matches its parent's; without the meta it is
// the default.
const scheme = document.querySelector('meta[name="color-scheme"]');
if (theme === "transparent") scheme?.remove();
else scheme?.setAttribute("content", theme === "light" ? "light" : "dark");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {widget.endsWith("/keys") ? (
      <KeysWidget me={Number(params.get("me")) || 1} />
    ) : widget.endsWith("/audio") ? (
      <AudioWidget me={Number(params.get("me")) || 1} faders={params.get("faders") !== "false"} />
    ) : widget.endsWith("/snapshots") ? (
      <SnapshotsWidget me={Number(params.get("me")) || null} all={params.get("all") !== "false"} />
    ) : widget.endsWith("/multiview") ? (
      <MultiviewWidget me={Number(params.get("me")) || 1} pvw={params.get("pvw") === "true"} />
    ) : widget.endsWith("/dme") ? (
      <DmeWidget me={Number(params.get("me")) || 1} keyer={Number(params.get("keyer")) || 1} />
    ) : (
      <BusWidget me={Number(params.get("me")) || 1} transition={params.get("transition") !== "false"} />
    )}
  </React.StrictMode>,
);
