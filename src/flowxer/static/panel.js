const api = (path, options = {}) =>
  fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  }).then(async (response) => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || response.statusText);
    return body;
  });

const $ = (id) => document.getElementById(id);

async function refresh() {
  const [health, mixer, inputs] = await Promise.all([api("/health"), api("/mixer"), api("/inputs")]);
  $("health").textContent = `${health.status} · ${mixer.backend} · ${mixer.video_format}`;
  $("pgm").textContent = mixer.program_input_id || "idle";
  $("pvw").textContent = mixer.preview_input_id || "—";
  $("bus").textContent = mixer.program_bus || "";
  const engine = mixer.engine || {};
  $("engine").textContent = engine.fps != null ? `${engine.state} · ${engine.fps} fps · L ${engine.latency_grains}` : engine.state || "stopped";
  const dsk = (mixer.keyers || [])[0];
  $("dsk").textContent = dsk ? (dsk.enabled ? "ON" : "OFF") : "none";

  $("inputs").innerHTML = inputs
    .map((input) => {
      const active = input.id === mixer.program_input_id ? "pgm" : "";
      const essences = [
        input.video?.flow_id ? `V ${String(input.video.flow_id).slice(0, 8)}` : "V bundled",
        input.audio?.flow_id ? `A ${String(input.audio.flow_id).slice(0, 8)}` : "A bundled",
      ].join(" · ");
      return `<button class="${active}" data-id="${input.id}">
        <strong>${input.label}</strong><br /><span class="muted">${input.kind} · ${essences}</span>
      </button>`;
    })
    .join("");
  $("inputs").querySelectorAll("button").forEach((button) => {
    button.onclick = () => api("/mixer/take", {
      method: "POST",
      body: JSON.stringify({ input_id: button.dataset.id, transition: "cut" }),
    }).then(refresh).catch(alert);
  });
}

$("start").onclick = () => api("/mixer/start", { method: "POST", body: "{}" }).then(refresh).catch(alert);
$("stop").onclick = () => api("/mixer/stop", { method: "POST" }).then(refresh).catch(alert);
const dsk = (enabled) =>
  api("/keyers/dsk-1", { method: "PATCH", body: JSON.stringify({ enabled }) }).then(refresh).catch(alert);
$("key-on").onclick = () => dsk(true);
$("key-off").onclick = () => dsk(false);

refresh().catch((error) => {
  $("health").textContent = error.message;
});
setInterval(() => refresh().catch(() => undefined), 2000);
