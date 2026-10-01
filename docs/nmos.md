# FlowXer NMOS node

FlowXer exposes an in-process **IS-04 v1.3 Node API** and **IS-05 v1.2 Connection API**
for AMWA **BCP-007-03** MXL. A Qvest (or any) IS-05 controller can route MXL
senders onto mixer inputs and take FlowXer program senders to other nodes.
Nothing is routed by hand-entered flow IDs on the platform.

The Node API is a **separate HTTP server** on `FLOWXER_NMOS_PORT` (default
**3252**), not behind `FLOWXER_API_TOKEN`. Mixer REST stays on 9610.

Disable the node with `FLOWXER_NMOS_ENABLE=false` (tests and local simulate
without a registry). DNS-SD/mDNS is off (`FLOWXER_NMOS_DNS_SD=false`) and is
not implemented.

See `docs/platform-integration-plan.md` for why this is an in-process FastAPI
node (Option C) rather than NVIDIA NvNmos.

## Identity

All IS-04 IDs are **UUIDv5** from `FLOWXER_NMOS_SEED` (default `{hostname}-flowxer`)
plus a stable name, so they survive restarts.

| Resource | Name key |
|---|---|
| Node | `node` |
| Device | `device` (label **FlowXer Vision Mixer**) |
| Receiver | `receiver` / `{input id}` / `video` or `audio` |
| Sender | `sender` / `{panel id}` / `video` or `audio` |
| Source / Flow | `source` or `flow` / `{panel id}` / role |

The IS-04 Flow UUID is **not** the MXL `mxl_flow_id`. Sender IS-05 active
params point at FlowXer's output domain and the current PGM MXL flow UUIDs.
A raster or group-hint change mints new PGM MXL IDs; the sender's
`mxl_flow_id` follows.

Node `href` and `api.endpoints` use `FLOWXER_NMOS_HOST_IP` (default: first
non-loopback IPv4), never `0.0.0.0`. The socket still binds `0.0.0.0` so
controllers can reach it under host networking.

## When resources exist

**One Node, one Device.**

**Receivers** (two per logical input, BCP-007-03 `urn:x-nmos:transport:mxl`):

- Kind `mxl_live` only: `{label} Video` / `{label} Audio`.
- Group hint `urn:x-nmos:tag:grouphint/v1.0` = `{input id}:Video` / `{input id}:Audio`.
- Caps (BCP-004-01): `video/v210` and `video/v210a` at the workspace raster;
  `audio/float32`, 48 kHz, configured channel count.
- Kinds `test`, `black`, `file`, `replay`: **no** receivers.
- Creating/deleting inputs or changing `logical_source_count` adds/removes
  receivers.

**Senders** (per mixer panel / ME program bus): one video and one audio, with
IS-04 Source and Flow. Active params:

- `mxl_domain_id` = FlowXer's output domain id
- `mxl_flow_id` = current PGM video/audio MXL flow UUIDs

MXL has no SDP; `transportfile` is HTTP 204.

## Activation behaviour

IS-05 `PATCH .../receivers/{id}/staged` with `activation.mode = activate_immediate`:

1. Malformed UUIDs, `mxl_flow_id: "auto"`, or a non-null transport file → **400**
   with `{ "code": 400, "error": "...", "debug": "..." }`.
2. Well-formed params are **accepted even if the domain or flow is not on disk
   yet** (on-demand fabrics). The input essence goes to `waiting` and retries
   with exponential backoff (250 ms → 5 s) while `master_enable` is true.
3. `master_enable: false` → `not_routed`, stop reading that essence (black /
   silence).
4. IS-04 receiver `subscription.sender_id` and `subscription.active` are
   updated on every activation. The fabrics agent watches this.
5. Video and audio of one input may come from different senders / domains.
6. Activating a receiver of an on-air input retargets **only that mxlsrc**
   (NULL → set `domain` / flow-id → PLAYING). Program keeps running.

Per-essence state machine:

`not_routed` → `waiting` (ids + enable, domain/flow missing) → `no_signal`
(flow directory exists, no grains yet) → `running` (grains present).

Deleting the flow directory returns the essence to `waiting` without another
PATCH. `GET /console` and `GET /mixer` expose the same states.

Only `activate_immediate` is implemented. Scheduled activations return 400.

## REST ↔ IS-05

They are one control plane:

| Action | Effect |
|---|---|
| IS-05 activation on an input receiver | Same as `PATCH /inputs/{id}` for that essence (`flow_id`, `domain_id`, enable) |
| `PATCH /inputs/{id}` of an `mxl_live` input | Updates that receiver's IS-05 active params and IS-04 `subscription` |
| REST omit `domain_id` | Default = FlowXer's **output** domain |
| Change kind to/from `mxl_live` | Create or remove the two receivers |

## Registry

Set `FLOWXER_NMOS_REGISTRY_URL` (for example `http://10.0.0.5:3210`). FlowXer
POSTs Node, Device, Source, Flow, Sender, Receiver to the Registration API and
heartbeats every 5 s. An empty URL still serves the Node API locally;
`registry_up` stays false.

## GUI

The operator status panel shows registry reachability, node id, and each
live-input receiver state. `GET /api/v1/console` includes a top-level `nmos`
object (also nested under `mixer.nmos`).

## AMWA testing tool

`scripts/nmos-testing.sh` runs the published `amwa/nmos-testing` image in
non-interactive mode against the Node API (default
`http://127.0.0.1:3252`):

- **IS-04-01** Node API v1.3
- **IS-05-01** Connection API v1.2
- **IS-05-02** IS-05 ↔ IS-04 (Node v1.3 + Connection v1.2)
- **BCP-007-03-01** (optional via `NMOS_TESTING_SUITES`)

The image entrypoint starts the web UI; the script overrides it with
`python3 nmos-test.py suite …` and mounts `scripts/nmos-testing-userconfig.py`
(`ENABLE_DNS_SD = False`). GitHub Actions runs this on **PRs into `stage` or
`main`** and on the **Stage** workflow (not on every PR into `dev`). You can
also dispatch **Actions → CI**. IS-04-01 mock-registry discovery tests
(`test_04`, `test_07`–`test_10`) are `--ignore`d: FlowXer uses
`FLOWXER_NMOS_REGISTRY_URL`, not DNS-SD. Revisit nvnmosd only if those suites
fail for Node/Connection API gaps Option C cannot fix.
