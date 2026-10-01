from fastapi.testclient import TestClient

from flowxer.api.schemas import MixerStartRequest


def test_metrics_and_probes(client: TestClient, mixer) -> None:
    live = client.get("/livez")
    assert live.status_code == 200
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    text = client.get("/metrics").text
    assert "flowxer_info" in text
    assert "flowxer_on_air 0" in text
    assert "flowxer_nmos_registry_up" in text
    assert "flowxer_transitions_total{type=\"cut\"}" in text
    api = client.get("/api/v1/metrics")
    assert api.status_code == 200
    assert "flowxer_process_memory_bytes" in api.text
    mixer.start(MixerStartRequest(program_input_id="cam-1"))
    mixer.take("cam-2")
    text = client.get("/metrics").text
    assert "flowxer_on_air 1" in text
    assert "flowxer_program_input{input=\"cam-2\"} 1" in text
    assert "flowxer_transitions_total{type=\"cut\"} 1" in text
    mixer.stop()


def test_metrics_unauthenticated_when_token_set(settings, mixer) -> None:
    from flowxer.app import create_app

    locked = settings.model_copy(update={"api_token": "staging-token-1"})
    client = TestClient(create_app(locked, mixer))
    assert client.get("/metrics").status_code == 200
    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/api/v1/metrics").status_code == 200
    assert client.get("/api/v1/console").status_code == 401
