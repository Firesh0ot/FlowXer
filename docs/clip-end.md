# Clip end: back to Preview

When a clip player that is on an ME's Program reaches the end of its clip, that ME can take its
Preview by itself. The user asked for this on 2026-10-13.

## The setting

A clip player's `on_end`, next to `loop` and `play_on_cut`:

| `on_end` | What the ME does |
|---|---|
| `off` (default) | nothing: the clip holds its last frame (as before) |
| `cut` | cuts to Preview |
| `auto` | the ME's transition (its mix or wipe) at its rate; an ME set to stinger mixes |
| `stinger` | the ME's stinger: its slot (the slot by direction without one), as a STING Auto plays it |

It acts only when all of these hold:
- The clip player does not loop.
- It is on the Program of a rendering ME.
- That ME's Preview is set and is a different source.

With the player on the Program of several MEs, each of them acts.

## Timing

Let E be the output grain that shows the clip's last frame.

| Action | Starts in grain | So that |
|---|---|---|
| `cut` | E + 1 | the clip plays to its last frame; Preview is on Program from the next grain |
| `auto` | E − rate + 1 | the transition's last grain (the new source in full) is E |
| `stinger` | E − cut | the stinger's cut point (where the background switches) is E |

- **A clip shorter than the transition** (or an end action found late) starts at once.
- **A manual take, cut, Auto or stinger** on that ME before the action is sent cancels it, and so do a stop, pause, cue or play of the player. After it is sent (3 grains before its grain), a take on the ME is a take during a transition.
- **A transition already running** on the ME (or a held T-bar) delays the action. It is retried every 100 ms and then starts at once, for up to 50 grains after E.
- **A stinger that cannot play** (no mezzanine yet, every stinger player busy) becomes a cut in grain E + 1.

## It is a normal take

The end action goes through the same paths as Cut, Auto and a stinger take:
- Preview and Program swap.
- The ME's tied keyers go along (with a cut at once, with an Auto along its curve, with a stinger at its cut point).
- Tally shows both sources during a mix.
- Audio follows video as in any transition.
- The transition counters count it.
- The new buses are saved.

BKGD off (key only) does not apply: the end action always takes the background.

## How it is timed

The control plane decides; the engine makes the take exactly in its grain.

1. While a clip player with an end action is on a rendering ME's Program, the control plane reads the engine's `GET /players` every 100 ms. The reply holds the last output grain planned and, per player, its `end_index` (E, the grain of its last frame while it plays without loop, or the grain it ended in).
2. Within 12 grains of the action's start, a timer sends the take 3 grains early with `at`: the start grain.
3. The engine holds that take and makes it right before it plans grain `at`. A grain already planned means the next one.
4. Any other command for that ME, or for a keyer, makes a held take first, so the engine sees the commands in the control plane's order.
5. Tally and the control plane's buses change when the take is sent, about 3 grains (60 ms at 50p) before the engine makes it.

A stinger for an end action is cued on its stinger player as soon as the clip is on Program, so its first frame is decoded in time.

## API

- `PATCH /inputs/{id}` with `on_end`: `off`, `cut`, `auto` or `stinger`. Also returned by `GET /inputs` and `GET /console`.
- Engine, localhost only:
  - `GET /players` returns `{index, players: [{player, state, frame, frames, loop, end_index, …}]}`.
  - `POST /me/{m}/take` takes `at`, an output grain.
  - `GET /status` shows per ME `deferred_at` (a take held for its grain) and `deferred_made` (the grain the last held take was made in; 0 when another command made it early). Each player also has `end_index`.

## GUI

- **The source's ⚙ (clip players):** *At the end*, with Off, Cut, Auto or Stinger.
- **The clip player's tile:** an **END CUT**, **END AUTO** or **END STING** mark next to LOOP and AUTO. It is dimmed while the clip loops (the action does not apply then).

## Saved state and upgrade

- `on_end` is part of each input in the saved state and the export.
- A state from before loads with `off`, so clip players behave as before.

## Checked

- **Engine unit tests:**
  - a playing clip's `end_index`, including after skipped grains, a pause, at its end and in a loop;
  - a held take made in its grain: a cut shows its target there, and a Mix or a stinger starts there;
  - a newer held take replaces the older, and another command makes it early.
- **pytest (`tests/test_clip_end.py`):**
  - the setting, the export and an old state;
  - the start grain of each action and the take sent with `at`, with its tied keyer;
  - a clip shorter than the transition;
  - a stinger (cued early, with its cut point);
  - the conditions (loop, same Preview, off, an end long past);
  - a manual take or a stop cancelling it;
  - a running transition delaying it;
  - two MEs;
  - the 3-grain lead.
- **Lab (A16 GPU 3, 2026-10-13):** a 4 s clip (200 grains at 50p) played on cut, checked against the engine's `end_index` and `/status`, 3 runs:
  - a cut made in grain E + 1;
  - a mix of 25 starting at E − 24, and a wipe of 10 at E − 9;
  - a stinger starting at E − cut;
  - a manual take and a stop before the end each cancelling it;
  - two MEs both cutting in E + 1.
  - Every grain was exact, with **0 skipped and 0 late grains** (about 1700 grains per run).
- **Console in headless Edge:** the setting in the ⚙ and the END mark (6 of 6 checks).
