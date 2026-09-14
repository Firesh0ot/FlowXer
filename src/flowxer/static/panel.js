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
  const [health, mixer, inputs, clips, config] = await Promise.all([
    api("/health"),
    api("/mixer"),
    api("/inputs"),
    api("/storage/clips"),
    api("/config"),
  ]);
  $("health").textContent = `${health.status} · ${mixer.backend} · ${mixer.video_format}`;
  $("pgm").textContent = mixer.program_input_id || "idle";
  $("pvw").textContent = mixer.preview_input_id || "—";
  $("bus").textContent = mixer.program_bus || "";
  $("stinger").textContent = mixer.stinger.phase === "idle"
    ? "idle"
    : `${mixer.stinger.id} ${mixer.stinger.phase} ${mixer.stinger.frame}/${mixer.stinger.frame_count}`;
  $("overlay").textContent = mixer.overlay.enabled ? `KEY ON · ${mixer.overlay.renderer}` : "KEY OFF";
  if (!$("url").value) $("url").value = mixer.overlay.url || config.overlay_url;

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

  const selected = $("clips").value;
  $("clips").innerHTML = clips.length
    ? clips.map((clip) => `<option value="${clip.name}">${clip.name}</option>`).join("")
    : `<option value="">No clips in storage/clips</option>`;
  if (selected) $("clips").value = selected;
}

$("start").onclick = () => api("/mixer/start", { method: "POST", body: "{}" }).then(refresh).catch(alert);
$("stop").onclick = () => api("/mixer/stop", { method: "POST" }).then(refresh).catch(alert);
$("key-on").onclick = () => api("/overlay", {
  method: "POST",
  body: JSON.stringify({
    enabled: true,
    title: $("title").value,
    subtitle: $("subtitle").value,
    url: $("url").value,
  }),
}).then(refresh).catch(alert);
$("key-off").onclick = () => api("/overlay", {
  method: "POST",
  body: JSON.stringify({ enabled: false }),
}).then(refresh).catch(alert);
$("load").onclick = () => api("/replay/load", {
  method: "POST",
  body: JSON.stringify({ file_path: $("clips").value, input_id: "replay" }),
}).then(refresh).catch(alert);
$("to-replay").onclick = () => api("/replay/take", {
  method: "POST",
  body: JSON.stringify({ stinger_id: "replay-wipe" }),
}).then(refresh).catch(alert);
$("to-live").onclick = () => api("/replay/return", {
  method: "POST",
  body: JSON.stringify({ stinger_id: "replay-wipe" }),
}).then(refresh).catch(alert);

refresh().catch((error) => {
  $("health").textContent = error.message;
});
setInterval(() => refresh().catch(() => undefined), 2000);
