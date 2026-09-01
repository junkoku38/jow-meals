"""Tests des modules de la refonte v1.0 : api.py, calendar, order.

Stubs HA partagés avec test_manager (chargés via conftest du dossier).
"""

import sys
import types
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_HERE = Path(__file__).parent
_MOD_DIR = _HERE.parent / "custom_components" / "jow"

# ---------------------------------------------------------------------------
# Stub HA minimal (identique à test_manager) si pas déjà chargé
# ---------------------------------------------------------------------------
def _stub_homeassistant() -> None:
    if "homeassistant" in sys.modules:
        return
    ha = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = MagicMock
    ha.core = core
    helpers = types.ModuleType("homeassistant.helpers")
    disp = types.ModuleType("homeassistant.helpers.dispatcher")
    disp.async_dispatcher_send = lambda *a, **k: None
    disp.async_dispatcher_connect = lambda *a, **k: (lambda: None)
    helpers.dispatcher = disp
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = MagicMock
    helpers.storage = storage
    ha.helpers = helpers
    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.dispatcher"] = disp
    sys.modules["homeassistant.helpers.storage"] = storage


_stub_homeassistant()

# requests réel : api.py l'utilise directement


# ordre de chargement : const d'abord (les modules font des imports relatifs
# .const/.api) — on simule le package jow_v1 dans sys.modules
_pkg = types.ModuleType("jow_v1")
_pkg.__path__ = [str(_MOD_DIR)]
sys.modules["jow_v1"] = _pkg

def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"jow_v1.{name}", _MOD_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

api = _load("api")


# ---------------------------------------------------------------------------
# api.JowClient
# ---------------------------------------------------------------------------

class _ClientHarness:
    """JowClient sur hass mocké, tokens pilotables, executor direct."""

    def __init__(self, access="tok", refresh="ref"):
        self.access = access
        self.refresh = refresh
        self.refreshed_with = None
        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))
        self.client = api.JowClient(
            hass,
            get_access_token=lambda: self.access,
            get_refresh_token=lambda: self.refresh,
            on_token_refreshed=self._on_refresh,
        )

    async def _on_refresh(self, token, new_refresh=None):
        self.refreshed_with = token
        self.rotated_with = new_refresh
        self.access = token
        if new_refresh:
            self.refresh = new_refresh


def test_client_get_refreshes_on_401():
    """401 → refresh → retry : le deuxième GET porte le nouveau token."""
    import asyncio

    h = _ClientHarness()
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(headers.get("authorization"))
        class R:
            status_code = 401 if len(calls) == 1 else 200
            def json(self):
                return {"data": {"ok": True}}
        return R()

    def fake_refresh_post(url, headers=None, params=None, json=None, timeout=None):
        class R:
            status_code = 200
            def json(self):
                return {"accessToken": "NEW"}
        return R()

    api.requests.get = fake_get
    api.requests.post = fake_refresh_post
    resp = asyncio.run(h.client.get("https://api.jow.fr/public/profile"))
    assert resp.status_code == 200
    assert calls == ["Bearer tok", "Bearer NEW"]
    assert h.refreshed_with == "NEW"


def test_client_refresh_without_authorization_header():
    """Piège documenté : le refresh doit partir SANS authorization."""
    import asyncio

    h = _ClientHarness()
    seen = {}

    def fake_post(url, headers=None, params=None, json=None, timeout=None):
        seen["headers"] = headers
        class R:
            status_code = 200
            def json(self):
                # FORMAT RÉEL (vérifié contre l'API) : tokens à la racine
                return {"accessToken": "FRESH", "refreshToken": "ROTATED"}
        return R()

    api.requests.post = fake_post
    tok = asyncio.run(h.client.refresh_token())
    assert tok == "FRESH"
    assert "authorization" not in seen["headers"]
    assert h.rotated_with == "ROTATED"   # rotation du refresh persistée


def test_client_search_recipes_uses_executor():
    import asyncio

    h = _ClientHarness(access=None)  # anonyme
    seen = {}

    def fake_post(url, headers=None, params=None, data=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {"data": {"content": [{"id": "r1", "title": "T"}]}}
        return R()

    api.requests.post = fake_post
    res = asyncio.run(h.client.search_recipes("curry", limit=5))
    assert res and res[0]["id"] == "r1"
    assert seen["url"].endswith("/recipe/quicksearch")
    assert seen["params"]["query"] == "curry"


def test_order_pay_requires_confirmation():
    """Garde-fou : pay_order refuse sans confirm=True explicite."""
    import asyncio
    order_mod = _load('order')
    JowOrderManager = order_mod.JowOrderManager

    h = _ClientHarness()
    om = JowOrderManager(h.client)
    res = asyncio.run(om.pay_order(order_id="o1", confirm=False))
    assert res["error"] == "confirmation_requise"
    # confirm True sans order_id : refus aussi
    res2 = asyncio.run(om.pay_order(order_id="", confirm=True))
    assert res2["error"] == "order_id_manquant"