// Operator-screen widgets, without the console around them:
// /widget/bus?me=<n>[&transition=false][&theme=dark|light|transparent] shows one ME's Program and
// Preview rows with Cut, Auto and the transition type (hidden with transition=false);
// /widget/keys?me=<n>[&theme=...] shows one ME's keyers and DSKs with On/Off and Auto;
// /widget/dme?me=<n>&keyer=<k>[&theme=...] one upstream keyer's box: On/Off, Auto, Run back and
// forth, Stop and the presets. It runs on flowxer-gui's own
// origin (its /api); only the origins in WIDGET_FRAME_ANCESTORS may frame it. It posts
// {type: "widget-ready"} and {type: "widget-size", w, h} to the page that frames it.
import React from "react";
import ReactDOM from "react-dom/client";
import { BusWidget } from "./components/BusWidget";
import { DmeWidget } from "./components/DmeWidget";
import { KeysWidget } from "./components/KeysWidget";
import "./widget.css";

const params = new URLSearchParams(location.search);
const theme = params.get("theme") || "dark";
document.documentElement.dataset.theme = theme;
// A frame is see-through only when its colour scheme matches its parent's; without the meta it is
// the default.
const scheme = document.querySelector('meta[name="color-scheme"]');
if (theme === "transparent") scheme?.remove();
else scheme?.setAttribute("content", theme === "light" ? "light" : "dark");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    {location.pathname.endsWith("/keys") ? (
      <KeysWidget me={Number(params.get("me")) || 1} />
    ) : location.pathname.endsWith("/dme") ? (
      <DmeWidget me={Number(params.get("me")) || 1} keyer={Number(params.get("keyer")) || 1} />
    ) : (
      <BusWidget me={Number(params.get("me")) || 1} transition={params.get("transition") !== "false"} />
    )}
  </React.StrictMode>,
);
