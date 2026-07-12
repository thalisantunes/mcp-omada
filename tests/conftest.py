from __future__ import annotations

import httpx
import pytest

from mcp_omada.client import OmadaClient
from mcp_omada.config import AuthMode, Settings

from .fakes import FakeOmadaController

BASE_URL = "https://omada.example.test"


@pytest.fixture
def fake_controller() -> FakeOmadaController:
    return FakeOmadaController()


@pytest.fixture
def transport(fake_controller: FakeOmadaController) -> httpx.MockTransport:
    return httpx.MockTransport(fake_controller.handler)


@pytest.fixture
def settings_legacy(fake_controller: FakeOmadaController) -> Settings:
    return Settings(
        base_url=BASE_URL,
        auth_mode=AuthMode.LEGACY,
        omadac_id=fake_controller.omadac_id,
        site_id=None,
        username=fake_controller.legacy_username,
        password=fake_controller.legacy_password,
        verify_tls=False,
    )


@pytest.fixture
def settings_openapi(fake_controller: FakeOmadaController) -> Settings:
    return Settings(
        base_url=BASE_URL,
        auth_mode=AuthMode.OPENAPI,
        omadac_id=fake_controller.omadac_id,
        site_id=fake_controller.site_id,
        client_id=fake_controller.openapi_client_id,
        client_secret=fake_controller.openapi_client_secret,
        verify_tls=False,
    )


@pytest.fixture
def legacy_client(settings_legacy: Settings, transport: httpx.MockTransport) -> OmadaClient:
    return OmadaClient(settings_legacy, transport=transport)


@pytest.fixture
def openapi_client(settings_openapi: Settings, transport: httpx.MockTransport) -> OmadaClient:
    return OmadaClient(settings_openapi, transport=transport)
