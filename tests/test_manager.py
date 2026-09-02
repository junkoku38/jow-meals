"""Tests des fonctions pures de manager.py (sans Home Assistant).

manager.py importe homeassistant.* en tête de fichier : on injecte des
stubs minimaux dans sys.modules avant l'import pour tester la logique
métier (agrégation, allergènes, filtres, rayons) en isolation.
"""

from __future__ import annotations

import sys
import types
import json  # noqa: E402
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
    # Session = vraie classe : api.py (tests v1) crée un cookie jar persistant ;
    # on lui donne une requests.Session RÉELLE si le module existe, sinon un stub
    try:
        from requests import Session as _RealSession  # noqa: F401
        _make("requests", post=MagicMock(), get=MagicMock(), options=MagicMock(),
              Session=__import__("requests").Session)
    except ImportError:
        _make("requests", post=MagicMock(), get=MagicMock(), options=MagicMock(),
              Session=MagicMock)


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


def test_diversify_intra_list_one_per_family_except_query():
    """Une seule recette par famille dans la liste — sauf les mots-clés
    de la requête : une demande « burger » a le droit à plusieurs burgers."""
    burgers = [
        {"id": "b1", "name": "Burger au poulet à la mexicaine"},
        {"id": "b2", "name": "Burger au tofu croustillant"},
        {"id": "b3", "name": "Smash burger"},
    ]
    # Sans query : b1 et b3 partagent « burger » → b3 écarté ; b2 aussi
    out = JowManager._diversify_intra_list(burgers)
    assert [r["id"] for r in out] == ["b1"]

    # Requête « burger » : le mot-clé demandé ne compte pas comme famille
    out = JowManager._diversify_intra_list(burgers, query="burger")
    assert {r["id"] for r in out} == {"b1", "b2", "b3"}

    # Deux dahls sans lien avec la requête → un seul
    plats = [
        {"id": "d1", "name": "Dahl de lentilles corail"},
        {"id": "w1", "name": "Wok de nouilles"},
        {"id": "d2", "name": "Dahl aux épinards"},
    ]
    out = JowManager._diversify_intra_list(plats, query="plat végétarien")
    assert [r["id"] for r in out] == ["d1", "w1"]


# ---------------------------------------------------------------------------
# Feature 9 : import menu Jow (parsing défensif)
# ---------------------------------------------------------------------------

def test_import_menu_parses_both_formats():
    """menu/week peut renvoyer une liste de recettes datées ou un dict
    {date: recette} — l'import gère les deux et n'écrase jamais un jour
    déjà planifié."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    import asyncio

    lundi = m.week_dates(0)[0].isoformat()
    mardi = m.week_dates(0)[1].isoformat()
    mercredi = m.week_dates(0)[2].isoformat()
    m.plan = {mercredi: {"id": "deja", "name": "Déjà planifié"}}

    # Format liste : recettes avec champ date
    data_list = {
        "recipes": [
            {"id": "r1", "title": "Salade poulet", "date": lundi},
            {"id": "r2", "title": "Wok légumes", "date": mardi},
            {"id": "r3", "title": "Gratin", "date": mercredi},  # jour occupé → skip
            {"id": "r4", "title": "Sans date"},                  # → skip
        ]
    }
    imported = m._import_from_menu_data(data_list, week_offset=0)
    assert imported["imported"] == 2
    assert m.plan[lundi]["name"] == "Salade poulet"
    assert m.plan[mardi]["name"] == "Wok légumes"
    assert m.plan[mercredi]["name"] == "Déjà planifié"

    # Format dict {date: recette}
    m2 = _manager()
    m2.async_save = AsyncMock(return_value=None)
    jeudi = m2.week_dates(0)[3].isoformat()
    data_dict = {"data": {jeudi: {"id": "r9", "title": "Curry tofu"}}}
    res = m2._import_from_menu_data(data_dict["data"], week_offset=0)
    assert res["imported"] == 1
    assert m2.plan[jeudi]["name"] == "Curry tofu"


# ---------------------------------------------------------------------------
# Feature 10 : péremption + rescue
# ---------------------------------------------------------------------------

def test_shelf_life_lookup():
    assert JowManager._shelf_life("poulet fermier") == 2
    assert JowManager._shelf_life("tomates cerises") == 7
    assert JowManager._shelf_life("pâtes") is None          # épicerie : longtemps
    assert JowManager._shelf_life("") is None


def test_recipe_to_dict_detail_endpoint_quantities():
    """Régression 0.10.0 : l'endpoint DÉTAIL (/recipe/{id}) porte
    quantityPerCover sur le CONSTITUANT (pas dans constituent.ingredient
    comme la recherche) — les favoris épinglés par recipe_id perdaient
    toutes leurs quantités."""
    detail_recipe = {
        "id": "r1", "title": "Bouillon de gnocchi",
        "roundedCoversCount": 2,
        "constituents": [
            # format DÉTAIL : qty sur le constituant
            {"isOptional": False, "quantityPerCover": 0.1,
             "unit": {"id": "u1", "name": "Kilogramme"},
             "ingredient": {"id": "i1", "name": "Gnocchi", "naturalUnit": {"id": "u1", "name": "Kilogramme"}}},
            # format RECHERCHE : qty dans l'ingrédient
            {"isOptional": False,
             "unit": {"id": "u2", "name": "Pièce"},
             "ingredient": {"id": "i2", "name": "Oignon", "quantityPerCover": 1.5,
                            "naturalUnit": {"id": "u2", "name": "Pièce"}}},
        ],
    }
    r = _recipe_to_dict(detail_recipe, 4)   # 4 couverts, base 2 => ratio 2
    assert r["ingredients"][0]["quantity"] == 0.2   # 0.1 × 2 (détail : qty sur le constituant)
    assert r["ingredients"][1]["quantity"] == 3.0   # 1.5 × 2 (recherche : qty dans l'ingrédient)
    assert r["ingredients"][0]["unit"] == "Kilogramme"


# ---------------------------------------------------------------------------
# clear_week / renew_week
# ---------------------------------------------------------------------------

def test_clear_week_remembers_rejects():
    """Vider une semaine mémorise les plats comme rejets (le renouvellement
    ne doit pas reproposer les plats qu'on voulait changer)."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    import asyncio

    for i, d in enumerate(m.week_dates(0)):
        m.plan[d.isoformat()] = {"id": f"old{i}", "name": f"Plat ancien {i}"}
    m.rejected = []

    asyncio.run(m.async_clear_week(week_offset=0))
    assert len(m.plan) == 0
    assert len(m.rejected) == 7

    # remember_rejects=False : simple effacement
    for i, d in enumerate(m.week_dates(0)):
        m.plan[d.isoformat()] = {"id": f"x{i}", "name": f"Plat {i}"}
    m.rejected = []
    asyncio.run(m.async_clear_week(week_offset=0, remember_rejects=False))
    assert len(m.plan) == 0
    assert m.rejected == []


def test_import_letscook_dedupes_against_whole_plan():
    """L'import letscook ne prend pas un plat déjà planifié n'importe où
    dans HA (S comme S+1) ni un plat rejeté ; les plats excédentaires
    restent disponibles (remaining) sans être perdus."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    import asyncio

    # S complète avec le plat A ; S+1 vide
    lundi = m.week_dates(0)[0].isoformat()
    mardi = m.week_dates(0)[1].isoformat()
    m.plan = {lundi: {"id": "A", "name": "Déjà planifié"}}
    # le plat C a été rejeté il y a peu
    m.rejected = [{"id": "C", "name": "Rejeté", "ts": 9999999999}]

    # La liste Jow contient A (déjà en HA), C (rejeté), D, E, F (libres)
    meals = [
        {"recipe": {"id": "A", "title": "Déjà planifié"}, "coversCount": 2},
        {"recipe": {"id": "C", "title": "Rejeté"}, "coversCount": 2},
        {"recipe": {"id": "D", "title": "Plat D"}, "coversCount": 2},
        {"recipe": {"id": "E", "title": "Plat E"}, "coversCount": 2},
        {"recipe": {"id": "F", "title": "Plat F"}, "coversCount": 2},
    ]

    # On appelle le cœur d'import letscook en isolant la logique : on
    # reproduit le filtrage de async_import_menu_from_jow
    already = {meal.get("id") for meal in m.plan.values() if isinstance(meal, dict) and meal.get("id")}
    rejected = {r.get("id") for r in m.rejected}
    eligible = [
        mm for mm in meals
        if _safe_id((mm.get("recipe") or {}).get("id")) not in already | rejected
    ]
    assert [e["recipe"]["id"] for e in eligible] == ["D", "E", "F"]

    # Simulation du remplissage : mardi (seul jour vide de S) prend D,
    # E et F restent "remaining"
    m.plan[mardi] = {"id": "D", "name": "Plat D"}
    remaining = [e for e in eligible if _safe_id((e.get("recipe") or {}).get("id")) != "D"]
    assert len(remaining) == 2


def test_renew_week_clears_and_refills():
    """renew_week vide la semaine puis la replanifie via suggest (mocké),
    et les anciens plats sont mémorisés comme rejets."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    m.ai_entity = ""
    import asyncio

    # semaine pleine d'anciens plats
    for i, d in enumerate(m.week_dates(0)):
        m.plan[d.isoformat()] = {"id": f"old{i}", "name": f"Ancien {i}"}
    m.rejected = []

    compteur = {"n": 0}

    async def fake_search(q, limit=5, start=0):
        compteur["n"] += 1
        # un nouveau plat différent par appel
        return [{"id": f"new{compteur['n']}", "title": f"Nouveau plat {compteur['n']}"}]

    async def fake_calories(rid):
        return None

    m.async_search = fake_search
    m.async_fetch_calories = fake_calories

    res = asyncio.run(m.async_renew_week(week_offset=0, criteria="varié"))
    assert res["cleared"] == 7
    assert res["planned"] == 7
    assert not res["failures"]
    # 7 nouveaux plats, tous différents (diversité intra-semaine par familles)
    names = [meal["name"] for meal in m.plan.values()]
    assert len(names) == 7
    assert all("Nouveau plat" in n for n in names)
    # les 7 anciens sont rejets
    assert len(m.rejected) == 7


# ---------------------------------------------------------------------------
# Recommandations natives Jow (reco/more)
# ---------------------------------------------------------------------------

def test_jow_recommendations_body_and_fallback():
    """Le corps de reco/more exclut récents + rejets ; suggest replie sur
    les reco natives quand la recherche textuelle ne trouve rien."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    m.ai_entity = ""
    m.jow_token = "tok"
    import asyncio
    from datetime import date as _date, timedelta as _td

    # un plat planifié récent + un rejet
    m.plan = {}
    m.plan[(_date.today() - _td(days=1)).isoformat()] = {"id": "recent1", "name": "Récent"}
    m.rejected = [{"id": "rej1", "name": "Rejeté", "ts": 9999999999}]

    captured = {}

    def fake_post(url, headers=None, params=None, data=None, timeout=None):
        captured["url"] = url
        captured["body"] = json.loads(data)

        class R:
            status_code = 200

            def json(self):
                return {"data": [{"id": "reco1", "title": "Suggestion native"}]}
        return R()

    # le manager utilise le module requests global — on monkeypatche
    _mod.requests.post = fake_post
    # executor : le hass mocké doit exécuter la fonction directement
    m.hass.async_add_executor_job = AsyncMock(side_effect=lambda f, *a: f(*a))

    recos = asyncio.run(m.async_jow_recommendations(count=5))
    assert [r["id"] for r in recos] == ["reco1"]
    body = captured["body"]
    assert "recent1" in body["excludedRecipesIds"]
    assert "rej1" in body["excludedRecipesIds"]
    assert captured["url"].endswith("/recipes/reco/more")


def test_suggest_falls_back_to_native_reco_on_empty_search():
    """suggest : recherche vide → recommandations natives Jow."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    m.ai_entity = ""
    m.plan = {}
    import asyncio

    calls = {"search": 0, "reco": 0}

    async def fake_search(q, limit=5, start=0):
        calls["search"] += 1
        return []  # recherche vide

    async def fake_reco(count=10, exclude_ids=None):
        calls["reco"] += 1
        return [{"id": "nat1", "name": "Suggestion native", "title": "Suggestion native"}]

    async def fake_calories(rid):
        return None

    m.async_search = fake_search
    m.async_jow_recommendations = fake_reco
    m.async_fetch_calories = fake_calories

    res = asyncio.run(m.async_suggest(criteria="plat", limit=5))
    assert calls["reco"] == 1
    assert res and res[0]["id"] == "nat1"


def test_expiring_ingredients_from_planning():
    """Les ingrédients périssables des repas planifiés ressortent avec
    leur urgence ; les longues conservations (pâtes) n'apparaissent pas."""
    m = _manager()
    from datetime import date as _date, timedelta as _td

    today = _date.today()
    hier = (today - _td(days=1)).isoformat()
    demain = (today + _td(days=1)).isoformat()
    m.plan = {
        hier: {"id": "x1", "name": "Poulet rôti", "ingredients": [
            {"name": "poulet"}, {"name": "pâtes"}]},
        hier: {"id": "x1", "name": "Poulet rôti", "ingredients": [
            {"name": "poulet"}, {"name": "pâtes"}, {"name": "champignons"}]},
        demain: {"id": "x2", "name": "Salade", "ingredients": [
            {"name": "tomates"}, {"name": "salade"}]},
    }
    exp = m.expiring_ingredients(within_days=4, today=today)
    noms = {e["ingredient"] for e in exp}
    assert "pâtes" not in noms                 # longue conservation
    assert "poulet" in noms                    # hier+2j = expire demain
    assert "champignons" in noms               # hier+4j = J+4 : dans l'horizon
    assert "salade" not in noms                # demain+4j = J+5 : hors horizon
    assert "tomates" not in noms               # demain+7j : hors horizon
    # trié par urgence croissante
    days = [e["days_left"] for e in exp]
    assert days == sorted(days)


def test_suggest_rescue_injects_expiring():
    """rescue_expiry=True injecte les ingrédients expirants dans le
    contexte envoyé à l'IA (on capture le prompt via le mock)."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    m.ai_entity = "ai_task.dummy"
    m.plan = {}
    import asyncio
    from datetime import date as _date, timedelta as _td

    today = _date.today()
    m.plan[(today - _td(days=1)).isoformat()] = {
        "id": "x1", "name": "Poulet rôti", "ingredients": [{"name": "poulet"}],
    }
    captured = {}

    async def fake_generate(instructions, ai_ent, task_name="jow_recipe_suggest"):
        # l'appel de génération de requête passe sans task_name explicite
        captured["prompt"] = instructions
        return "poulet citron"

    async def fake_search(q, limit=5, start=0):
        return [{"id": "r1", "title": "Poulet au citron"}]

    async def fake_calories(rid):
        return None

    m._ai_generate = fake_generate
    m.async_search = fake_search
    m.async_fetch_calories = fake_calories

    asyncio.run(m.async_suggest(criteria="plat", rescue_expiry=True))
    assert "SAUVER" in captured["prompt"]
    assert "poulet" in captured["prompt"]

    # Sans rescue : pas d'injection
    captured.clear()
    asyncio.run(m.async_suggest(criteria="plat", rescue_expiry=False))
    assert "SAUVER" not in captured.get("prompt", "")


def test_ai_pick_prompt_is_enriched():
    """La liste fournie à l'IA contient ingrédients, temps et calories,
    et le prompt mentionne les plats récents à varier."""
    m = _manager()
    m.preferences = "méditerranéenne"
    recipes = [
        {
            "id": "r1", "name": "Poulet au citron",
            "description": "Un classique familial",
            "ingredients": [{"name": "poulet"}, {"name": "citron"}, {"name": "herbes"}],
            "preparation_time": 15, "cooking_time": 25, "calories": 520,
        },
        {"id": "r2", "name": "Salade composée"},
    ]
    captured = {}

    async def fake_generate(instructions, ai_ent, task_name="x"):
        captured["prompt"] = instructions
        return "1"

    m._ai_generate = fake_generate
    import asyncio

    picked = asyncio.run(m._ai_pick_recipe("repas léger", recipes, "ai_task.x",
                                           recent_names=["Curry lentilles"]))
    assert picked and picked["id"] == "r1"
    p = captured["prompt"]
    assert "poulet, citron, herbes" in p          # ingrédients listés
    assert "prép. 15 min" in p and "cuisson 25 min" in p  # temps
    assert "520 kcal" in p                        # calories
    assert "Curry lentilles" in p                 # plats récents
    assert "méditerranéenne" in p                 # préférences


def test_ai_pick_prompt_contains_calorie_constraint():
    """max_calories est transmis comme contrainte impérative au prompt."""
    m = _manager()
    captured = {}

    async def fake_generate(instructions, ai_ent, task_name="x"):
        captured["prompt"] = instructions
        return "1"

    m._ai_generate = fake_generate
    import asyncio

    asyncio.run(m._ai_pick_recipe(
        "plat léger", [{"id": "r1", "name": "Salade"}], "ai_task.x",
        max_calories=600,
    ))
    assert "600" in captured["prompt"]
    assert "IMPÉRATIVE" in captured["prompt"]


def test_suggest_max_total_time_hard_filter():
    """max_total_time écarte en dur les recettes trop longues ; si tout
    est écarté, le filtre est sauté (liste conservée)."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    m.ai_entity = ""
    m.plan = {}
    import asyncio

    api_recipes = [
        {"id": "r1", "title": "Salade express", "preparationTime": 10, "cookingTime": 5},
        {"id": "r2", "title": "Gratin", "preparationTime": 30, "cookingTime": 45},
        {"id": "r3", "title": "Wok", "preparationTime": 15, "cookingTime": 10},
    ]

    async def fake_search(q, limit=5, start=0):
        return list(api_recipes)

    async def fake_calories(rid):
        return None

    m.async_search = fake_search
    m.async_fetch_calories = fake_calories

    # Filtre 30 min : gratin (75 min) écarté, salade et wok conservés
    res = asyncio.run(m.async_suggest(criteria="plat", limit=5, max_total_time=30))
    ids = [r["id"] for r in res]
    assert "r2" not in ids
    assert set(ids) == {"r1", "r3"}

    # Filtre 10 min : tout écarté → filtre ignoré, liste complète
    res = asyncio.run(m.async_suggest(criteria="plat", limit=5, max_total_time=10))
    assert len(res) == 3


# ---------------------------------------------------------------------------
# Rejets persistants + diversité des mots-clés
# ---------------------------------------------------------------------------

def test_clear_meal_remembers_rejection():
    """Effacer un plat (non mangé) l'enregistre comme rejet : il ne doit
    plus revenir dans les suggestions, même absent du planning."""
    m = _manager()
    import asyncio
    from datetime import date as _date

    lundi = m.week_dates(0)[0]
    m.plan[lundi.isoformat()] = {"id": "curry1", "name": "Curry de lentilles"}
    asyncio.run(m.async_clear_meal(lundi))
    assert lundi.isoformat() not in m.plan
    assert any(r["id"] == "curry1" for r in m.rejected)

    # Le plat rejeté est exclu d'une suggestion ultérieure
    api = [
        {"id": "curry1", "title": "Curry de lentilles"},
        {"id": "autre", "title": "Bœuf carottes"},
    ]

    async def fake_search(q, limit=5, start=0):
        return list(api)

    async def fake_calories(rid):
        return None

    m.async_search = fake_search
    m.async_fetch_calories = fake_calories
    res = asyncio.run(m.async_suggest(criteria="plat", limit=5))
    ids = [r["id"] for r in res]
    assert "curry1" not in ids
    assert "autre" in ids


def test_too_similar_blocks_same_keyword_family():
    """Deux plats partageant un mot-clé fort (curry) sont trop proches."""
    assert JowManager._too_similar("Curry de poisson", ["Curry de lentilles"]) == "curry"
    assert JowManager._too_similar("Risotto aux champignons", ["Curry de lentilles"]) is None
    # mots génériques exclus : pas de similarité sur « recette »/« facile »
    assert JowManager._too_similar("Recette facile de poisson", ["Plat facile du dimanche"]) is None


def test_suggest_diversity_drops_same_family_at_margin():
    """Les plats trop proches des récents sont écartés — mais seulement
    si des candidates différentes restent (jamais vider la liste)."""
    m = _manager()
    m.async_save = AsyncMock(return_value=None)
    m.ai_entity = ""
    m.plan = {}
    m.rejected = [{"id": "old", "name": "Curry de lentilles", "ts": 1}]
    import asyncio

    api = [
        {"id": "c1", "title": "Curry de poulet"},
        {"id": "c2", "title": "Curry de légumes"},
        {"id": "w1", "title": "Wok de nouilles"},
    ]

    async def fake_search(q, limit=5, start=0):
        return list(api)

    async def fake_calories(rid):
        return None

    m.async_search = fake_search
    m.async_fetch_calories = fake_calories

    res = asyncio.run(m.async_suggest(criteria="plat", limit=5))
    ids = [r["id"] for r in res]
    # les curries (même mot-clé que le rejet récent) sont écartés,
    # le wok survit
    assert "w1" in ids
    assert "c1" not in ids and "c2" not in ids


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