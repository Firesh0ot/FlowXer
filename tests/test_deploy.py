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


def test_pod_network_manifest_follows_the_platform_contract() -> None:
    text = (ROOT / "deploy" / "kubernetes" / "flowxer-pod-network.yaml").read_text(encoding="utf-8")
    assert "hostNetwork" not in text
    assert "hostIPC" not in text
    assert "terminationGracePeriodSeconds: 20" in text
    assert 'SHUTDOWN_TIMEOUT_S: "10"' in text
    assert "mountPath: /config" in text
    assert "fieldPath: status.podIP" in text
    for name in ("MXL_DOMAIN_SCAN_PATH", "MXL_CLEANUP_ON_EXIT", "NMOS_SEED", "NMOS_TAGS", "NMOS_REGISTRY_ADDRESS", "NMOS_HOST_ADDRESS"):
        assert name in text
    assert "path: /livez" in text
    assert "path: /readyz" in text
    assert "/Volumes/mxl" in text


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
    assert "test_04" in text
    assert "--ignore" in text
    userconfig = (ROOT / "scripts" / "nmos-testing-userconfig.py").read_text(encoding="utf-8")
    assert "ENABLE_DNS_SD = False" in userconfig


def test_ci_runs_amwa_on_pull_requests() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "github.event_name == 'pull_request'" in text
    assert "workflow_dispatch" in text
    assert "if: github.event_name == 'workflow_dispatch'\n" not in text
    assert "./.github/actions/nmos-testing" in text
    stage = (ROOT / ".github" / "workflows" / "stage.yml").read_text(encoding="utf-8")
    assert "nmos-testing:" in stage
    assert "./.github/actions/nmos-testing" in stage


def test_mxl_build_fetches_short_sha_via_release_branch() -> None:
    script = (ROOT / "docker" / "build-mxl.sh").read_text(encoding="utf-8")
    assert 'git fetch --depth 1 origin "${MXL_REF}"' in script
    assert "release/v1.1" in script
    assert "couldn't find remote ref" in script
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG MXL_REF=218ddaa0a08c12ffe75fc475ae65aa3d9eef16d7" in dockerfile
