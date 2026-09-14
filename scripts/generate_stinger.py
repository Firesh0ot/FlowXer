#!/usr/bin/env python3
"""Generate the default live ↔ replay TGA stinger sequence."""

from __future__ import annotations

import argparse
from pathlib import Path

from flowxer.engine.stinger import generate_replay_wipe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", default="storage/stingers/replay-wipe")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--frames", type=int, default=50)
    args = parser.parse_args()
    info = generate_replay_wipe(
        Path(args.dest),
        width=args.width,
        height=args.height,
        frame_count=args.frames,
    )
    print(f"wrote {info.frame_count} TGA frames, cut at {info.cut_frame}: {info.path}")


if __name__ == "__main__":
    main()
