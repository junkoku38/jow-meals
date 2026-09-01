"""Tests des fonctions pures de manager.py (sans Home Assistant).

manager.py importe homeassistant.* en tête de fichier : on injecte des
stubs minimaux dans sys.modules avant l'import pour tester la logique
métier (agrégation, allergènes, filtres, rayons) en isolation.
"""

from __future__ import annotations

import sys
import types
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock


def _stub_homeassistant() -> None:
    """Crée des modules homeassistant factices si absents."""
    if "homeassistant" in sys.modules:
        return

    def _make(name: str, **attrs) -> types.ModuleType:
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    ha = _make("homeassistant")
    _make("homeassistant.core", HomeAssistant=MagicMock)
    _make(
        "homeassistant.helpers.dispatcher",
        async_dispatcher_send=MagicMock(),
    )
    _make(
        "homeassistant.helpers.storage",
        Store=lambda *a, **k: MagicMock(),
    )
    _make("requests", post=MagicMock(), get=MagicMock(), options=MagicMock())


_stub_homeassistant()

# Charger manager.py comme membre d'un package factice pour que son
# import relatif "from .const import ..." fonctionne, sans exécuter le
# vrai custom_components.jow.__init__ (qui importe tout Home Assistant).
import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

_pkg = types.ModuleType("jow_pkg_under_test")
_jow_dir = Path(__file__).parent.parent / "custom_components" / "jow"
_pkg.__path__ = [str(_jow_dir)]
sys.modules["jow_pkg_under_test"] = _pkg

_const_spec = importlib.util.spec_from_file_location(
    "jow_pkg_under_test.const",
    _jow_dir / "const.py",
)
_const = importlib.util.module_from_spec(_const_spec)
sys.modules["jow_pkg_under_test.const"] = _const
_const_spec.loader.exec_module(_const)

_mgr_spec = importlib.util.spec_from_file_location(
    "jow_pkg_under_test.manager",
    _jow_dir / "manager.py",
)
_mod = importlib.util.module_from_spec(_mgr_spec)
sys.modules["jow_pkg_under_test.manager"] = _mod
_mgr_spec.loader.exec_module(_mod)

JowManager = _mod.JowManager
_deduce_allergens = _mod._deduce_allergens
_jow_ingredient_unit = _mod._jow_ingredient_unit
_recipe_to_dict = _mod._recipe_to_dict
_safe_id = _mod._safe_id
_safe_url = _mod._safe_url
_truncate = _mod._truncate
aisle = _mod._aisle_for


def _manager() -> JowManager:
    m = JowManager(MagicMock(), 2, entry_id="test")
    m.async_save = AsyncMock(return_value=None)
    return m


class _FakeStore:
    """Store jouable pour tester la persistance et la migration legacy."""

    def __init__(self, hass, version, key, data=None):
        self.key = key
        self._data = data

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = data


def _manager_with_stores(new_data=None, legacy_data=None):
    """Manager avec des stores réels factices (clé par instance + legacy)."""
    m = JowManager(MagicMock(), 2, entry_id="abc123")
    m._store = _FakeStore(None, 1, "jow.data.abc123", new_data)
    m._legacy_store = _FakeStore(None, 1, "jow.data", legacy_data)
    return m


# ---------------------------------------------------------------------------
# Migration storage legacy (jow.data -> jow.data.<entry_id>)
# ---------------------------------------------------------------------------

def test_load_migrates_legacy_storage():
    legacy = {
        "plan": {"2026-08-20": {"name": "curry"}},
        "shopping": [{"uid": "1", "summary": "200 g riz", "done": False}],
        "banned_ingredients": ["céleri"],
    }
    m = _manager_with_stores(new_data=None, legacy_data=legacy)
    import asyncio

    asyncio.run(m.async_load())
    assert m.plan == {"2026-08-20": {"name": "curry"}}
    assert m.shopping == [{"uid": "1", "summary": "200 g riz", "done": False}]
    assert m.banned_ingredients == ["céleri"]


def test_load_prefers_instance_data_over_legacy():
    inst = {"plan": {"2026-08-25": {"name": "paella"}}}
    legacy = {"plan": {"2026-08-20": {"name": "curry"}}}
    m = _manager_with_stores(new_data=inst, legacy_data=legacy)
    import asyncio

    asyncio.run(m.async_load())
    assert m.plan == {"2026-08-25": {"name": "paella"}}


def test_load_survives_empty_stores():
    m = _manager_with_stores(new_data=None, legacy_data=None)
    import asyncio

    asyncio.run(m.async_load())
    assert m.plan == {} and m.shopping == []


def test_save_persists_favorites():
    m = _manager_with_stores(new_data=None, legacy_data=None)
    import asyncio

    m.favorites = [{"name": "carbonara", "calories": 650}]
    asyncio.run(m.async_save())
    assert m._store._data["favorites"] == [{"name": "carbonara", "calories": 650}]


# ---------------------------------------------------------------------------
# suggest + weekday : sémantique overwrite (bouton « Changer de recette »)
# ---------------------------------------------------------------------------

def test_suggest_overwrite_semantics():
    """Par défaut suggest+weekday écrase (Changer de recette) ;
    overwrite=False préserve un repas déjà planifié."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    m.ai_entity = ""  # pas d'IA : fallback criteria direct
    m.plan = {}
    import asyncio

    jour = m.week_dates(0)[0].isoformat()  # lundi de la semaine courante
    m.plan[jour] = {"id": "r1", "name": "ancien plat"}
    # _recipe_to_dict lit le champ API "title" (pas "name")
    recipes = [{"id": "r2", "title": "nouveau plat"}]

    async def fake_search(q, limit=5):
        return list(recipes)

    async def fake_calories(rid):
        return None

    m.async_search = fake_search
    m.async_fetch_calories = fake_calories

    # Défaut : écrase (bouton Changer de recette)
    res = asyncio.run(m.async_suggest(criteria="curry", weekday="lundi"))
    assert m.plan[jour]["name"] == "nouveau plat"
    assert res and res[0]["id"] == "r2"

    # overwrite=False : préserve l'existant, suggestions renvoyées
    m.plan[jour] = {"id": "r1", "name": "repas préservé"}
    res = asyncio.run(m.async_suggest(criteria="curry", weekday="lundi", overwrite=False))
    assert m.plan[jour]["name"] == "repas préservé"
    assert res and res[0]["id"] == "r2"


def test_suggest_ai_pick_reorders_results():
    """La sélection IA (_ai_pick_recipe) remonte la recette choisie
    en tête de la liste, en tête de planification."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    m.ai_entity = "ai_task.dummy"
    m.plan = {}
    import asyncio

    # 3 candidates : l'API renvoie mexicain en tête, l'IA doit choisir
    # le burger tofu (n°3, plus proche d'une demande « asiatique »).
    api_recipes = [
        {"id": "r1", "title": "Burger au poulet à la mexicaine"},
        {"id": "r2", "title": "Smash burger"},
        {"id": "r3", "title": "Burger au tofu croustillant, sauce siracha"},
    ]

    async def fake_search(q, limit=5, start=0):
        return list(api_recipes)

    async def fake_calories(rid):
        return None

    async def fake_ai_generate(instructions, ai_ent, task_name="jow_recipe_suggest"):
        if task_name == "jow_recipe_pick":
            return "3"  # l'IA choisit le burger tofu
        return "burger asiatique"  # requête générée

    m.async_search = fake_search
    m.async_fetch_calories = fake_calories
    m._ai_generate = fake_ai_generate

    res = asyncio.run(m.async_suggest(criteria="burger asiatique", limit=3))
    assert res and res[0]["id"] == "r3"
    assert res[0]["name"] == "Burger au tofu croustillant, sauce siracha"


# ---------------------------------------------------------------------------
# Allergènes INCO (déduction heuristique depuis les tastes)
# ---------------------------------------------------------------------------

def _constituent(name: str, tastes: list[str]) -> dict:
    return {
        "ingredient": {
            "name": name,
            "tastes": [{"name": t} for t in tastes],
            "quantityPerCover": 100,
        },
        "unit": {"id": "u1"},
    }


def test_deduce_allergens_lait_fromage():
    recipe = {
        "constituents": [
            _constituent("emmental", ["Fromage"]),
            _constituent("pâtes", ["Pâtes fraîches"]),
        ]
    }
    codes, source = _deduce_allergens(recipe)
    assert 7 in codes  # fromage -> lait
    assert 1 in codes  # pâtes -> gluten
    assert source == "estimated"


def test_deduce_allergens_bleu_fromage_pas_ble():
    # "ble" (blé, gluten) ne doit pas matcher "Bleu de brebis" (fromage)
    # ni inversement "bleu" ne doit pas donner gluten.
    recipe = {"constituents": [_constituent("bleu de brebis", ["Fromage bleu"])]}
    codes, _ = _deduce_allergens(recipe)
    assert codes == [7]  # lait uniquement


def test_deduce_allergens_vide():
    codes, source = _deduce_allergens({})
    assert codes == []
    assert source == "estimated"


# ---------------------------------------------------------------------------
# _recipe_to_dict
# ---------------------------------------------------------------------------

def test_recipe_to_dict_quantities_ratio():
    recipe = {
        "_id": "abc-123_XY",
        "title": "Curry de poulet",
        "roundedCoversCount": 4,
        "constituents": [
            {
                "ingredient": {
                    "name": "poulet",
                    "quantityPerCover": 150,
                    "naturalUnit": {"_id": "u1", "name": "g"},
                },
                "unit": {"id": "u1"},
            }
        ],
        "imageUrl": "//img/curry.jpg",
        "videoUrl": "javascript:alert(1)",
    }
    out = _recipe_to_dict(recipe, covers=2)  # ratio 0.5
    assert out["name"] == "Curry de poulet"
    ing = out["ingredients"][0]
    assert ing["quantity"] == 75.0  # 150 * 0.5
    assert ing["unit"] == "g"
    assert out["url"].endswith("abc-123_XY")
    # http(s) uniquement : l'URL javascript: est rejetée
    assert out["video"] is None


def test_recipe_to_dict_non_dict():
    assert _recipe_to_dict("pas un dict", 2) == {}


def test_safe_url_rejette_javascript():
    assert _safe_url("javascript:alert(1)") is None
    assert _safe_url("https://jow.fr/x") == "https://jow.fr/x"
    assert _safe_url(None) is None


def test_safe_static_url_fragments():
    safe_static = _mod._safe_static_url
    # chemin relatif : assemblé derrière static.jow.fr
    assert safe_static("img/x.jpg") == "https://static.jow.fr/img/x.jpg"
    # schéma-relatif : https explicite
    assert safe_static("//cdn.jow.fr/x.jpg") == "https://cdn.jow.fr/x.jpg"
    # https complet : conservé tel quel
    assert safe_static("https://static.jow.fr/img/y.jpg") == "https://static.jow.fr/img/y.jpg"
    # fragments malicieux : rejetés AVANT concaténation
    assert safe_static("javascript:alert(1)") is None
    assert safe_static("data:text/html,<script>") is None
    assert safe_static("vbscript:x") is None
    assert safe_static("") is None


def test_safe_id():
    assert _safe_id("abc_123-X") == "abc_123-X"
    assert _safe_id("../etc/passwd") is None
    assert _safe_id("") is None


def test_truncate():
    assert _truncate(None, 5) is None
    assert _truncate("abcdef", 3) == "abc"


# ---------------------------------------------------------------------------
# Agrégation des ingrédients
# ---------------------------------------------------------------------------

def test_aggregate_same_name_and_unit():
    m = _manager()
    # Semer les repas sur le lundi et le mardi de la SEMAINE COURANTE :
    # aggregate_ingredients ne parcourt que week_dates(0).
    monday = date.today() - timedelta(days=date.today().weekday())
    m.plan = {
        monday.isoformat(): {
            "name": "A",
            "ingredients": [
                {"name": "riz", "quantity": 100, "unit": "g"},
                {"name": "lait", "quantity": 0.5, "unit": "l"},
            ],
        },
        (monday + timedelta(days=1)).isoformat(): {
            "name": "B",
            "ingredients": [
                {"name": "Riz", "quantity": 50, "unit": "g"},  # même clé normalisée
                {"name": "beurre", "quantity": None, "unit": ""},
            ],
        },
    }
    lines = m.aggregate_ingredients()
    joined_lower = " | ".join(lines).lower()
    assert "150 g riz" in joined_lower  # 100 + 50
    assert "beurre" in joined_lower    # sans quantité : nom seul


def test_aggregate_ignores_optional():
    m = _manager()
    m.plan = {
        date.today().isoformat(): {
            "name": "A",
            "ingredients": [{"name": "parmesan", "quantity": 10, "unit": "g", "optional": True}],
        }
    }
    assert m.aggregate_ingredients() == []


# ---------------------------------------------------------------------------
# Rayons
# ---------------------------------------------------------------------------

def test_aisle_for():
    assert aisle("Tomates cerises") == "Fruits & Légumes"
    assert aisle("Filet de boeuf") == "Boucherie"
    assert aisle("Saumon fumé") == "Poissonnerie"
    assert aisle("Lait demi-écrémé") == "Crémerie"
    assert aisle("Spaghetti n°5") == "Épicerie salée"
    assert aisle("Chocolat noir") == "Épicerie sucrée"
    assert aisle("Éponge magique") == "Autre"


# ---------------------------------------------------------------------------
# Unités Jow
# ---------------------------------------------------------------------------

def test_jow_ingredient_unit():
    const = {
        "unit": {"id": "u2"},
        "ingredient": {
            "naturalUnit": {"_id": "u1", "name": "g"},
            "alternativeUnits": [{"unit": {"_id": "u2", "name": "cs"}}],
        },
    }
    assert _jow_ingredient_unit(const) == "cs"
    assert _jow_ingredient_unit({}) == ""


# ---------------------------------------------------------------------------
# Persistance interdits / à éviter
# ---------------------------------------------------------------------------

def test_add_remove_banned_normalizes_and_dedupes():
    m = _manager()
    import asyncio

    m.banned_ingredients = []
    m.avoid_ingredients = []

    asyncio.run(m.async_add_banned_ingredient("  CÉLERI "))
    assert m.banned_ingredients == ["céleri"]
    # doublon (déjà présent, normalisé) : pas d'ajout
    asyncio.run(m.async_add_banned_ingredient("céleri"))
    assert m.banned_ingredients == ["céleri"]
    # chaîne vide ignorée
    asyncio.run(m.async_add_banned_ingredient("   "))
    assert m.banned_ingredients == ["céleri"]
    # retrait
    asyncio.run(m.async_remove_banned_ingredient("CÉLERI"))
    assert m.banned_ingredients == []

    asyncio.run(m.async_add_avoid_ingredient(" Coriandre "))
    assert m.avoid_ingredients == ["coriandre"]
    asyncio.run(m.async_remove_avoid_ingredient("coriandre"))
    assert m.avoid_ingredients == []


def test_purge_old():
    m = _manager()
    import asyncio

    old = (date.today() - timedelta(days=40)).isoformat()
    recent = (date.today() - timedelta(days=3)).isoformat()
    m.plan = {old: {"name": "vieux"}, recent: {"name": "récent"}}
    asyncio.run(m.async_purge_old())
    assert old not in m.plan
    assert recent in m.plan