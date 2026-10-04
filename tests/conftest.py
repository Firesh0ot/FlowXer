from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flowxer.app import create_app
from flowxer.engine.mixer import VisionMixer
from flowxer.settings import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    root = tmp_path / "mxl"
    return Settings(
        mxl_root=root,
        mxl_output_domain_dir=root / "flowxer-test",
        mxl_output_domain_id="flowxer-test",
        storage_root=tmp_path / "storage",
        state_dir=tmp_path / "config",
        simulate=True,
        gst_mode="simulate",
        width=64,
        height=36,
        frame_rate_num=50,
        frame_rate_den=1,
        stinger_frame_count=8,
        overlay_url="http://127.0.0.1:9610/graphics/lower-third.html",
        group_hint="FlowXerTest",
        stinger_auto_tick=False,
        nmos_enable=False,
        nmos_bind=False,
        nmos_seed="test-flowxer",
    )


@pytest.fixture
def mixer(settings: Settings) -> VisionMixer:
    return VisionMixer(settings)


@pytest.fixture
def client(settings: Settings, mixer: VisionMixer) -> TestClient:
    app = create_app(settings, mixer)
    return TestClient(app)
