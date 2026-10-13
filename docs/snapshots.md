# Snapshots

A snapshot is a named memory of one ME, or of all MEs at once, that an operator recalls with one
button. Operators asked for it after a test day; the user decided the details on 2026-10-13.

## What a snapshot holds

Per ME:

| | |
|---|---|
| Buses | Program and Preview |
| FTB | on or off |
| Transition | type, rate, wipe settings, stinger slot, keyer ties, BKGD |
| Keyers and DSKs | on air or not, rate, mode, source, clip, soft, gain, invert, box (DME), keyframes, `fly` |
| Audio | the master and the strips (fader, mute, AFV); none for an ME that did not render |

Not in a snapshot: the workspace (structure), the inputs and what the clip players play, NMOS
routes, the library, keyer labels, and the input delays and channel maps.

- **Number and name.** Every snapshot has a number from 1 to 99 (its id; one number space for ME
  and all-MEs snapshots) and a name of up to 40 characters. Storing without a number takes the
  lowest free one; the default name is `ME 1 #3` or `All MEs #4`. No folders.
- **One ME or all MEs.** `panel_id` is `me-<n>` or `all`. An all-MEs snapshot holds every ME the
  console had when it was stored.
- **Store over.** Storing into a used number replaces its contents; its name and options stay
  unless the request gives new ones.

## Recall

A recall goes through the same paths as the single API changes (Preview, the transition, the
keyers, a cut, FTB, the audio), so the engine, tally and the saved state stay consistent.

| | Always | Program/on-air (`program`) | Audio (`audio`) |
|---|---|---|---|
| Preview | set | set | |
| Transition | set | set | |
| Settings of keyers that are off air | set | set | |
| Settings of keyers on air | kept | set (on air, at the next frame) | |
| Program | kept | **cut** | |
| Keyers' and DSKs' on or off | kept | **cut** on or off | |
| FTB | kept | **cut** on or off | |
| Strips and master | kept | kept | set, each **ramps** over one grain |

- **Options.** `program` (include Program/on-air) and `audio` are both off by default. Each
  snapshot stores them as the defaults of its recall; a recall can override either one.
- **Off means nothing on air changes.** Without Program/on-air a recall does not change what is on
  air: Program, the keys' on-air state and FTB stay, and so do the settings of a keyer that is on
  air (keys have no preview, so a new source or box would show on Program at once). A keyer counts
  as on air while it is on, fading or flying off, or tied to a transition the T-bar holds (as the
  raw tally). The keyer is listed in `skipped`.
- **Cuts, not fades.** With Program/on-air, Program cuts (no auto-stinger, no transition) and keys
  cut on or off at the next frame, as `PATCH /keyers/{id}` with `enabled` does. A keyer that flies
  cuts on with the box it had when stored. FTB cuts. Fades would be a second meaning for the
  ME's rate and the keyers' rates and could not show the new look at once.
- **Order.** FTB going on comes first, then keyers that go off, Program, Preview, the transition
  and the keyers' settings, then keyers that go on and FTB going off last. Each is one engine
  command: they land within a frame or two of each other.
- **All MEs.** The highest ME first, so a lower ME that re-enters one takes its new Program.
- **What no longer fits** (an input, ME, keyer or stinger slot that is gone, a tie to a keyer that is
  gone) is skipped and named in `skipped`; the rest is recalled. A tie to a missing keyer is
  dropped (BKGD comes back on when no tie is left). A T-bar that holds a transition keeps the ME's
  transition (skipped); with Program/on-air the cut ends it first.
- **Audio.** Strips the snapshot does not name (at their defaults when stored, or new since) go to
  their defaults: 0 dB, not muted, AFV on. Faders jumping on air are dangerous; hence the option.
- **Off air** a recall only changes the state; the engine gets it when it starts.

## Start-up snapshot

`PUT /snapshots/startup` names one snapshot (or none) that is recalled with its own options
whenever the mixer starts (`POST /mixer/start`, `FLOWXER_PROGRAM_AUTOSTART`, and the starts that a
take, a Cut or Preview off-air make). With Program/on-air it sets the start look; without, it sets
Preview, the transitions and the keyers that are off, and Program stays the saved one. It is part of
the saved state and the export. Deleting the snapshot clears it.

**Start keeps ME 1's buses.** Before, `POST /mixer/start` without `program_input_id` (the GUI sends
`{}`) put the first input on ME 1 Program and Preview; the other MEs kept theirs. Now each bus is the
request's, else the saved one (while ME 1 can still take it), else the first input (Preview:
Program). Preview off-air starts with the saved Program. A start-up snapshot then wins over all of
them. `FLOWXER_PROGRAM_AUTOSTART` keeps its documented buses (the first two inputs).

## API

| | |
|---|---|
| `GET /snapshots` | `{startup, snapshots: [{number, name, panel_id, options, stored_at}]}` (no contents); also `GET /console` `snapshots` |
| `POST /snapshots` | store the current state: `{panel_id: "me-<n>" or "all", number?, name?, options?: {program?, audio?}}` → the snapshot |
| `GET /snapshots/{number}` | the snapshot with its contents (`mes`: per panel id `program_input_id`, `preview_input_id`, `ftb`, `transition`, `keyers`, `audio`) |
| `PATCH /snapshots/{number}` | `{name?, options?: {program?, audio?}}`: rename, the recall's defaults |
| `DELETE /snapshots/{number}` | 204 |
| `POST /snapshots/{number}/recall` | `{program?, audio?}` override the defaults for this recall → `{snapshot, options, skipped}` |
| `PUT /snapshots/startup` | `{number}` or `{number: null}` → as `GET /snapshots` |

Errors: an unknown snapshot or panel 404; a number outside 1–99 422; no free number, an empty name,
or an ME snapshot of an ME the console no longer has 409. The changes are saved like every other
API change.

## GUI

**SNAP** in the keyer row (next to AUDIO) opens a small panel over the console: the active ME's
snapshots, then those of all MEs, each as a row with its number and name (START when it is the
start-up snapshot; double click renames), **PGM** and **AUD** (its recall's defaults; click to
change them), **RECALL** (with those defaults) and **⋯**: recall once *Preview only*, *with
Program/on-air*, *with audio* or *with both*, *Rename*, *Store the current look here* (asks first),
*Recall when the mixer starts* and *Delete* (asks first). **STORE** in each heading stores the
active ME, or all MEs, into the lowest free number. A toast names the recall and what it kept or
skipped.

## Widget

`/widget/snapshots[?me=<n>][&all=false][&theme=dark|light|transparent]`, minimum 240 × 120: a
recall button per snapshot of ME `n` and, unless `all=false`, per snapshot of all MEs (without
`me`: those only). Each button shows the number, the name and PGM / AUD / ALL; a click recalls with
the snapshot's own options, and the last recall from this widget is lit; what it kept or skipped
shows at the bottom. The same contract as `bus`, `keys`, `dme` and `audio`: listed in `/widgets`,
framed only by `WIDGET_FRAME_ANCESTORS`, `widget-ready` and `widget-size` to the parent; a bad
parameter answers 400.

## Saved state and upgrade

- The saved state and `GET /config/export` gain `snapshots`: `{startup, items: [...]}` with the
  full contents. `POST /config/import` validates it (numbers 1–99 and unique, the start-up snapshot
  exists) and answers 422 otherwise.
- A state or export from before snapshots loads with none.
- Size: a snapshot of one ME with two keyers and a DSK is about 3.5 kB in `state.json`; a keyer with 8
  keyframes adds about 5 kB. The state is written after every API change.
- Rollback: an older FlowXer ignores the `snapshots` key (the snapshots are lost when it saves).
