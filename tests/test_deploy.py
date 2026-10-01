from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_kubernetes_manifest_is_host_network_non_root() -> None:
    text = (ROOT / "deploy" / "kubernetes" / "flowxer.yaml").read_text(encoding="utf-8")
    assert "hostNetwork: true" in text
    assert "dnsPolicy: ClusterFirstWithHostNet" in text
    assert "runAsUser: 1000" in text
    assert "runAsNonRoot: true" in text
    assert "mxl.srf.ch/media" in text
    assert "prometheus.io/scrape" in text
    assert "/livez" in text
    assert "/readyz" in text
    assert "/Volumes/mxl" in text
    assert "FLOWXER_MIXER_URL" in text
    assert "FLOWXER_WEBRTC_UDP_PORT_MIN" in text
    assert "hostPort:" not in text
    monitor = (ROOT / "deploy" / "kubernetes" / "servicemonitor.yaml").read_text(encoding="utf-8")
    assert "kind: ServiceMonitor" in monitor
    assert "path: /metrics" in monitor


def test_host_compose_uses_host_network_and_token() -> None:
    text = (ROOT / "docker-compose.host.yml").read_text(encoding="utf-8")
    assert "network_mode: host" in text
    assert "user: \"1000:1000\"" in text
    assert "FLOWXER_MIXER_URL: http://127.0.0.1:9610" in text
    assert "FLOWXER_API_TOKEN" in text
    assert "/Volumes/mxl:/Volumes/mxl" in text
    assert "FLOWXER_HOST: \"127.0.0.1\"" in text
    assert "/livez" in text


def test_nmos_testing_script_invokes_amwa_cli() -> None:
    text = (ROOT / "scripts" / "nmos-testing.sh").read_text(encoding="utf-8")
    assert "IS-04-01" in text
    assert "IS-05-01" in text
    assert "IS-05-02" in text
    assert "BCP-007-03-01" in text
    assert "nmos-test.py" in text
    assert "--entrypoint python3" in text
    assert "amwa/nmos-testing" in text
    userconfig = (ROOT / "scripts" / "nmos-testing-userconfig.py").read_text(encoding="utf-8")
    assert "ENABLE_DNS_SD = False" in userconfig
