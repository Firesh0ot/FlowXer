from __future__ import annotations

import uuid
from typing import Any

# Stable namespace so output flow UUIDs are deterministic for a given group hint
# (restarting the mixer reuses the same flow dirs).
FLOWXER_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

VIDEO_MEDIA_TYPE = "video/v210"
AUDIO_MEDIA_TYPE = "audio/float32"


def flow_uuid(
    group_hint: str,
    role: str,
    *,
    seed: str,
    width: int | None = None,
    height: int | None = None,
    frame_rate_num: int | None = None,
    frame_rate_den: int | None = None,
) -> str:
    """Deterministic MXL PGM flow UUID. The NMOS seed keeps two mixers with the
    same group hint apart. Raster is part of the key so a format change mints
    new flow IDs and the NMOS sender's mxl_flow_id follows."""
    key = f"{seed}:{group_hint}:{role}"
    if width and height and frame_rate_num and frame_rate_den:
        key = f"{key}:{width}x{height}:{frame_rate_num}/{frame_rate_den}"
    return str(uuid.uuid5(FLOWXER_NAMESPACE, key))


def output_domain_uuid(seed: str) -> str:
    return str(uuid.uuid5(FLOWXER_NAMESPACE, f"{seed}:mxl-output-domain"))


def seed_short(seed: str) -> str:
    return uuid.uuid5(FLOWXER_NAMESPACE, seed).hex[:8]


def video_flow_def(
    *,
    flow_id: str,
    group_hint: str,
    label: str,
    description: str,
    width: int,
    height: int,
    frame_rate_num: int,
    frame_rate_den: int,
    media_type: str = VIDEO_MEDIA_TYPE,
) -> dict[str, Any]:
    return {
        "description": description,
        "id": flow_id,
        "tags": {"urn:x-nmos:tag:grouphint/v1.0": [f"{group_hint}:Video"]},
        "format": "urn:x-nmos:format:video",
        "label": label,
        "parents": [],
        "media_type": media_type,
        "grain_rate": {"numerator": frame_rate_num, "denominator": frame_rate_den},
        "frame_width": width,
        "frame_height": height,
        "interlace_mode": "progressive",
        "colorspace": "BT709",
        "components": [
            {"name": "Y", "width": width, "height": height, "bit_depth": 10},
            {"name": "Cb", "width": width // 2, "height": height, "bit_depth": 10},
            {"name": "Cr", "width": width // 2, "height": height, "bit_depth": 10},
        ],
    }


def audio_flow_def(
    *,
    flow_id: str,
    group_hint: str,
    label: str,
    description: str,
    channels: int,
    sample_rate: int,
    media_type: str = AUDIO_MEDIA_TYPE,
) -> dict[str, Any]:
    return {
        "description": description,
        "format": "urn:x-nmos:format:audio",
        "tags": {"urn:x-nmos:tag:grouphint/v1.0": [f"{group_hint}:Audio"]},
        "label": label,
        "id": flow_id,
        "media_type": media_type,
        "sample_rate": {"numerator": sample_rate},
        "channel_count": channels,
        "bit_depth": 32,
        "parents": [],
    }
