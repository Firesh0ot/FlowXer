from flowxer.engine.webrtc import rewrite_ice_host
from flowxer.settings import Settings


def test_default_host_is_loopback() -> None:
    assert Settings.model_fields["host"].default == "127.0.0.1"


def test_rewrite_ice_host_swaps_candidate_and_connection() -> None:
    sdp = (
        "v=0\r\n"
        "o=- 1 1 IN IP4 172.30.0.2\r\n"
        "c=IN IP4 172.30.0.2\r\n"
        "a=candidate:1 1 UDP 2130706431 172.30.0.2 54321 typ host\r\n"
        "a=ice-ufrag:abc\r\n"
    )
    rewritten = rewrite_ice_host(sdp, "10.0.0.20")
    assert "c=IN IP4 10.0.0.20" in rewritten
    assert "10.0.0.20 54321 typ host" in rewritten
    assert "172.30.0.2 54321" not in rewritten
    assert rewrite_ice_host(sdp, "") == sdp
