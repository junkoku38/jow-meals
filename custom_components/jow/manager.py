"""Client Jow et stockage du planning / de la liste de courses."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

import requests

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_COVERS,
    JOW_AUTH_URL,
    JOW_FAVORITES_URL,
    JOW_MENU_URL,
    JOW_PROFILE_URL,
    JOW_SHOPPING_URL,
    JOW_TOKEN_REFRESH_INTERVAL,
    RECIPE_BASE_URL,
    SIGNAL_UPDATE,
    STORAGE_KEY,
    STORAGE_VERSION,
    WEEKDAYS,
)

_LOGGER = logging.getLogger(__name__)

# Limites défensives sur les données issues de l'API Jow (non officielle).
_MAX_FIELD_LEN = 2000
_MAX_NAME_LEN = 200
_MAX_ITEMS = 500
_MAX_SUMMARY_LEN = 500

# API Jow (non officielle).
_JOW_SEARCH_URL = "https://api.jow.fr/public/recipe/quicksearch"
_JOW_RECIPE_URL = "https://api.jow.fr/public/recipe"
_JOW_STATIC_URL = "https://static.jow.fr/"
_JOW_HEADERS = {
    "accept": "application/json",
    "accept-language": "fr",
    "content-type": "application/json",
    "x-jow-withmeta": "1",
    "origin": "https://jow.fr",
    "referer": "https://jow.fr/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
_JOW_PARAMS = {"start": "0", "availabilityZoneId": "FR"}
_ALLOWED_URL_SCHEMES = {"http", "https"}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _safe_url(value: Any, fallback: str | None = None) -> str | None:
    """Retourne une URL HTTP(S) sûre, ou fallback, ou None."""
    if not value or not isinstance(value, str):
        return fallback
    parsed = urlparse(value)
    if parsed.scheme in _ALLOWED_URL_SCHEMES and parsed.netloc:
        return value
    _LOGGER.warning("URL Jow rejetée (schéma/host invalide) : %r", value)
    return fallback


def _safe_id(value: Any) -> str | None:
    """Retourne un identifiant alphanumérique safe, ou None."""
    if not value:
        return None
    text = str(value)
    return text if _ID_RE.match(text) else None


def _truncate(value: Any, limit: int) -> str | None:
    """Tronque une chaîne à `limit` caractères ; None si vide."""
    if value is None:
        return None
    text = str(value)
    return text[:limit]


def _jow_ingredient_unit(constituent: dict) -> str:
    """Résout l'unité d'un constituant Jow (naturalUnit ou alternativeUnit)."""
    try:
        unit_id = constituent.get("unit", {}).get("id")
        ing = constituent.get("ingredient", {})
        natural = ing.get("naturalUnit", {})
        if natural.get("_id") == unit_id or natural.get("id") == unit_id:
            return natural.get("name", "")
        for alt in ing.get("alternativeUnits", []):
            u = alt.get("unit", {})
            if u.get("_id") == unit_id or u.get("id") == unit_id:
                return u.get("name", "")
    except (TypeError, AttributeError):
        pass
    return ""


def _recipe_to_dict(recipe: Any, covers: int) -> dict:
    """Convertit une recette Jow (dict JSON de l'API) en dict sérialisable.

    On stocke un dict plutôt que l'objet : il doit survivre à un redémarrage
    de Home Assistant et être lisible depuis les templates Jinja.
    """
    if not isinstance(recipe, dict):
        return {}

    ratio = 1.0
    base_covers = recipe.get("roundedCoversCount") or DEFAULT_COVERS
    if base_covers:
        try:
            ratio = covers / float(base_covers)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = 1.0

    ingredients = []
    for const in recipe.get("constituents", []) or []:
        ing = const.get("ingredient", {})
        quantity = ing.get("quantityPerCover")
        try:
            quantity = round(float(quantity) * ratio, 2) if quantity else quantity
        except (TypeError, ValueError):
            pass
        ingredients.append(
            {
                "name": _truncate(ing.get("name", ""), _MAX_NAME_LEN) or "",
                "quantity": quantity,
                "unit": _truncate(_jow_ingredient_unit(const), _MAX_NAME_LEN) or "",
                "optional": bool(const.get("isOptional", False)),
            }
        )

    recipe_id = _safe_id(recipe.get("_id") or recipe.get("id"))
    url = f"{RECIPE_BASE_URL}{recipe_id}" if recipe_id else None
    image = None
    if recipe.get("imageUrl"):
        image = _safe_url(f"{_JOW_STATIC_URL}{recipe['imageUrl']}")
    video = None
    if recipe.get("videoUrl"):
        video = _safe_url(f"{_JOW_STATIC_URL}{recipe['videoUrl']}")

    return {
        "id": recipe_id,
        "name": _truncate(recipe.get("title", "Recette Jow"), _MAX_NAME_LEN) or "Recette Jow",
        "url": url,
        "image": image,
        "video": video,
        "description": _truncate(recipe.get("description"), _MAX_FIELD_LEN),
        "preparation_time": recipe.get("preparationTime"),
        "cooking_time": recipe.get("cookingTime"),
        "covers": covers,
        "calories": recipe.get("_calories"),
        "ingredients": ingredients,
    }


class JowManager:
    """Garde le planning de la semaine et la liste de courses."""

    def __init__(
        self,
        hass: HomeAssistant,
        default_covers: int,
        allergies: str = "",
        preferences: str = "",
        ai_entity: str = "",
        weather_entity: str = "",
        jow_token: str = "",
    ) -> None:
        self.hass = hass
        self.default_covers = default_covers
        self.allergies = allergies
        self.preferences = preferences
        self.ai_entity = ai_entity
        self.weather_entity = weather_entity
        self.jow_token = jow_token
        self._token_refresh_cancel: Any = None
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # {"2026-08-10": {recette...}}
        self.plan: dict[str, dict] = {}
        # [{"uid": "...", "summary": "200 g de riz", "done": False}]
        self.shopping: list[dict] = []
        # [{"uid": "...", "summary": "sel", "done": False}]
        # Liste approuvée d'articles à fusionner systématiquement avec
        # la liste de courses générée depuis le planning.
        self.approved: list[dict] = []

    # ------------------------------------------------------------------
    # Persistance
    # ------------------------------------------------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.plan = data.get("plan", {})
        self.shopping = data.get("shopping", [])
        self.approved = data.get("approved", [])

    async def async_save(self) -> None:
        await self._store.async_save(
            {"plan": self.plan, "shopping": self.shopping, "approved": self.approved}
        )
        async_dispatcher_send(self.hass, SIGNAL_UPDATE)

    # ------------------------------------------------------------------
    # Appels à Jow (bloquants -> executor)
    # ------------------------------------------------------------------
    async def async_search(self, query: str, limit: int = 5) -> list[dict]:
        """Recherche des recettes sur Jow via l'API HTTP directe."""

        def _search():
            params = {"start": "0", "availabilityZoneId": "FR", "query": query, "limit": str(max(limit, 1))}
            # Preflight OPTIONS (l'API Jow exige un preflight CORS)
            options_headers = {"accept": "*/*", "accept-language": "fr,fr-FR;q=0.9", "access-control-request-method": "POST", "access-control-request-headers": "content-type,x-jow-withmeta"}
            requests.options(_JOW_SEARCH_URL, headers=options_headers, params=params, timeout=10)
            # POST réel
            resp = requests.post(
                _JOW_SEARCH_URL, headers=dict(_JOW_HEADERS), params=params, data="{}", timeout=15
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            return data.get("content", []) if isinstance(data, dict) else []

        try:
            return await self.hass.async_add_executor_job(_search) or []
        except Exception as err:
            _LOGGER.error("Recherche Jow impossible (%s) : %s", query, err)
            return []

    async def async_fetch_calories(self, recipe_id: str) -> int | None:
        """Récupère les calories par portion depuis l'endpoint détail de Jow.

        L'API de recherche ne retourne pas les calories : il faut interroger
        l'endpoint /public/recipe/{id} qui expose nutritionalFacts.
        """
        if not recipe_id or not _ID_RE.match(recipe_id):
            return None

        def _fetch():
            url = f"{_JOW_RECIPE_URL}/{recipe_id}"
            # L'endpoint détail exige x-jow-withmeta: true (et non "1")
            headers = dict(_JOW_HEADERS)
            headers["x-jow-withmeta"] = "true"
            headers["accept"] = "application/json, text/plain, */*"
            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            # nutritionalFacts : [{id: "ENERC", label: "Calories", unit: "kcal", amount: N}, ...]
            facts = data.get("nutritionalFacts", [])
            for fact in facts:
                if fact.get("id") == "ENERC":
                    try:
                        return int(round(float(fact.get("amount", 0))))
                    except (TypeError, ValueError):
                        return None
            return None

        try:
            return await self.hass.async_add_executor_job(_fetch)
        except Exception as err:
            _LOGGER.debug("Calories Jow indisponibles pour %s : %s", recipe_id, err)
            return None

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------
    @staticmethod
    def monday_of(day: date, week_offset: int = 0) -> date:
        return day - timedelta(days=day.weekday()) + timedelta(weeks=week_offset)

    def week_dates(self, week_offset: int = 0) -> list[date]:
        monday = self.monday_of(date.today(), week_offset)
        return [monday + timedelta(days=i) for i in range(7)]

    def get_meal(self, day: date) -> dict | None:
        return self.plan.get(day.isoformat())

    async def async_plan_meal(
        self, day: date, query: str, covers: int | None = None, choice: int = 1
    ) -> dict | None:
        """Cherche une recette et l'épingle sur un jour."""
        covers = covers or self.default_covers
        results = await self.async_search(query, limit=max(choice, 1))
        if not results:
            _LOGGER.warning("Aucune recette Jow trouvée pour « %s »", query)
            return None

        recipe = results[min(choice, len(results)) - 1]
        # Récupérer les calories depuis l'endpoint détail (l'API de recherche
        # ne les fournit pas).
        recipe_id = _safe_id(recipe.get("_id") or recipe.get("id"))
        if recipe_id:
            calories = await self.async_fetch_calories(recipe_id)
            if calories is not None:
                recipe["_calories"] = calories
        stored = _recipe_to_dict(recipe, covers)
        self.plan[day.isoformat()] = stored
        await self.async_save()
        return stored

    async def async_clear_meal(self, day: date) -> None:
        self.plan.pop(day.isoformat(), None)
        await self.async_save()

    async def async_clear_week(self, week_offset: int = 0) -> None:
        for day in self.week_dates(week_offset):
            self.plan.pop(day.isoformat(), None)
        await self.async_save()

    def purge_old(self, keep_days: int = 30) -> None:
        """Supprime les repas trop anciens pour ne pas gonfler le stockage."""
        limit = (date.today() - timedelta(days=keep_days)).isoformat()
        for key in [k for k in self.plan if k < limit]:
            self.plan.pop(key, None)

    # ------------------------------------------------------------------
    # Liste de courses
    # ------------------------------------------------------------------
    def aggregate_ingredients(self, week_offset: int = 0) -> list[str]:
        """Additionne les ingrédients de la semaine (même nom + même unité)."""
        totals: dict[tuple[str, str], float | None] = {}
        order: list[tuple[str, str]] = []

        for day in self.week_dates(week_offset):
            meal = self.get_meal(day)
            if not meal:
                continue
            for ing in meal.get("ingredients", []):
                if ing.get("optional"):
                    continue
                key = (ing["name"].strip().lower(), ing.get("unit", ""))
                if key not in totals:
                    totals[key] = None
                    order.append(key)
                qty = ing.get("quantity")
                try:
                    qty = float(qty)
                except (TypeError, ValueError):
                    continue
                totals[key] = (totals[key] or 0) + qty

        lines = []
        for name, unit in order:
            qty = totals[(name, unit)]
            if qty is None:
                lines.append(name.capitalize())
            else:
                qty_str = f"{qty:g}"
                lines.append(f"{qty_str} {unit} {name}".replace("  ", " ").strip())
        return lines

    async def async_refresh_shopping_list(
        self, week_offset: int = 0, keep_checked: bool = False
    ) -> None:
        """Régénère la liste de courses à partir du planning.

        Fusionne les ingrédients agrégés du planning avec la liste approuvée
        (articles à toujours acheter, hors planning) en dédoublonnant sur le
        libellé normalisé.
        """
        done = {item["summary"] for item in self.shopping if item.get("done")} if keep_checked else set()
        auto_lines = self.aggregate_ingredients(week_offset)

        # Articles approuvés : on conserve leur ordre, on les dédoublonne
        # avec les ingrédients du planning via libellé normalisé.
        seen = {self._norm(line) for line in auto_lines}
        merged: list[dict] = []
        for line in auto_lines:
            merged.append({"uid": uuid.uuid4().hex, "summary": line, "done": line in done})
        for item in self.approved:
            summary = item.get("summary", "")
            if not summary.strip():
                continue
            if self._norm(summary) not in seen:
                seen.add(self._norm(summary))
                merged.append(
                    {"uid": uuid.uuid4().hex, "summary": summary, "done": summary in done}
                )
        self.shopping = merged
        await self.async_save()

    @staticmethod
    def _norm(text: str) -> str:
        """Normalise un libellé pour comparer (minuscules, espaces)."""
        return " ".join(text.lower().split())

    async def async_add_item(self, summary: str) -> None:
        if len(self.shopping) >= _MAX_ITEMS:
            _LOGGER.warning("Liste de courses Jow pleine (%d items) : ajout ignoré", _MAX_ITEMS)
            return
        clean = (summary or "")[:_MAX_SUMMARY_LEN]
        if not clean.strip():
            return
        self.shopping.append({"uid": uuid.uuid4().hex, "summary": clean, "done": False})
        await self.async_save()

    async def async_update_item(self, uid: str, summary: str | None, done: bool | None) -> None:
        for item in self.shopping:
            if item["uid"] == uid:
                if summary is not None:
                    item["summary"] = summary[:_MAX_SUMMARY_LEN]
                if done is not None:
                    item["done"] = done
        await self.async_save()

    async def async_remove_items(self, uids: list[str]) -> None:
        to_remove = set(uids)
        self.shopping = [i for i in self.shopping if i["uid"] not in to_remove]
        await self.async_save()

    # ------------------------------------------------------------------
    # Liste approuvée (articles hors planning, fusionnés systématiquement)
    # ------------------------------------------------------------------
    async def async_add_approved(self, summary: str) -> None:
        if len(self.approved) >= _MAX_ITEMS:
            _LOGGER.warning("Liste approuvée Jow pleine (%d items) : ajout ignoré", _MAX_ITEMS)
            return
        clean = (summary or "")[:_MAX_SUMMARY_LEN]
        if not clean.strip():
            return
        self.approved.append({"uid": uuid.uuid4().hex, "summary": clean, "done": False})
        await self.async_save()

    async def async_update_approved(self, uid: str, summary: str | None, done: bool | None) -> None:
        for item in self.approved:
            if item["uid"] == uid:
                if summary is not None:
                    item["summary"] = summary[:_MAX_SUMMARY_LEN]
                if done is not None:
                    item["done"] = done
        await self.async_save()

    async def async_remove_approved(self, uids: list[str]) -> None:
        to_remove = set(uids)
        self.approved = [i for i in self.approved if i["uid"] not in to_remove]
        await self.async_save()

    # ------------------------------------------------------------------
    # Marquer un repas comme fait + retirer les ingrédients du stock
    # ------------------------------------------------------------------
    async def async_meal_done(self, day: date) -> dict | None:
        """Marque un repas comme fait et retire les ingrédients de la liste de courses.

        - Supprime le repas du planning
        - Régénère la liste de courses sans les ingrédients de ce repas
        - Conserve les items déjà cochés et les items approuvés
        """
        meal = self.get_meal(day)
        if not meal:
            _LOGGER.warning("Aucun repas planifié pour %s", day.isoformat())
            return None

        # Ingrédients du repas terminé (noms normalisés)
        done_ingredients = {
            self._norm(ing.get("name", ""))
            for ing in meal.get("ingredients", [])
            if not ing.get("optional")
        }

        # Retirer les items de la liste de courses correspondant aux ingrédients
        # du repas terminé
        removed = []
        kept = []
        for item in self.shopping:
            if self._norm(item["summary"]) in done_ingredients or any(
                self._norm(ing) in item["summary"].lower() for ing in done_ingredients
            ):
                removed.append(item["summary"])
            else:
                kept.append(item)
        self.shopping = kept

        # Retirer le repas du planning
        self.plan.pop(day.isoformat(), None)
        await self.async_save()

        _LOGGER.info(
            "Repas '%s' marqué comme fait pour %s — %d ingrédients retirés de la liste",
            meal.get("name", ""),
            day.isoformat(),
            len(removed),
        )

        return {
            "meal": meal.get("name", ""),
            "date": day.isoformat(),
            "removed_from_shopping": removed,
        }

    # ------------------------------------------------------------------
    # Suggestion IA (ai_task.generate_data + jow.search)
    # ------------------------------------------------------------------
    async def async_suggest(
        self,
        criteria: str = "",
        covers: int | None = None,
        limit: int = 5,
        weather_entity: str | None = None,
        ai_entity: str | None = None,
        weekday: str | None = None,
    ) -> list[dict]:
        """Génère une requête Jow via l'IA puis cherche les recettes.

        Utilise l'agent ai_task configuré (ou celui passé en paramètre) pour
        formuler une requête de recherche adaptée aux allergies/préférences
        de l'utilisateur et à la météo courante, puis interroge l'API Jow.
        """
        ai_ent = ai_entity or self.ai_entity
        weather_ent = weather_entity or self.weather_entity

        # Contexte météo
        weather_ctx = ""
        if weather_ent:
            state = self.hass.states.get(weather_ent)
            if state and state.state not in (None, "unknown", "unavailable"):
                temp = state.attributes.get("temperature", "?")
                weather_ctx = f"Météo actuelle : {state.state}, {temp}°C. "

        # Contraintes utilisateur
        constraints = ""
        if self.allergies:
            constraints += f"Allergies/interdits : {self.allergies}. "
        if self.preferences:
            constraints += f"Préférences : {self.preferences}. "
        if criteria:
            constraints += f"Demande : {criteria}. "

        instructions = (
            f"{weather_ctx}{constraints}"
            "Génère une requête de recherche de recette courte (2 à 5 mots, "
            "sans guillemets ni ponctuation) adaptée au contexte. "
            "Réponds uniquement avec la requête."
        )

        # Appel ai_task.generate_data
        query = ""
        if ai_ent:
            try:
                response = await self.hass.services.async_call(
                    "ai_task",
                    "generate_data",
                    {
                        "task_name": "jow_recipe_suggest",
                        "instructions": instructions,
                        "entity_id": ai_ent,
                    },
                    blocking=True,
                    return_response=True,
                )
                # Selon la version HA, response peut être:
                # {"conversation_id": ..., "data": "..."}  (via WS)
                # ou {"ai_task.xxx": {"data": "..."}}  (via async_call interne)
                if isinstance(response, dict):
                    data = response.get("data")
                    if not data:
                        data = response.get("response", {}).get("data", "")
                    if not data:
                        for _k, val in response.items():
                            if isinstance(val, dict) and "data" in val:
                                data = val["data"]
                                break
                    query = str(data or "").strip().strip('"').strip("'")
                elif isinstance(response, str):
                    query = response.strip().strip('"').strip("'")
            except Exception as err:
                _LOGGER.warning("ai_task.generate_data a échoué : %s", err)
                query = ""

        # Fallback : utiliser criteria directement
        if not query:
            query = criteria or "recette"

        _LOGGER.info("Requête Jow suggérée par l'IA : %s", query)
        results = await self.async_search(query, limit=max(limit, 1))
        covers = covers or self.default_covers
        recipes = [_recipe_to_dict(r, covers) for r in results]

        # Filtrer les recettes contenant des ingrédients exclus du compte Jow
        if self.jow_token:
            excluded = await self.async_get_excluded_ingredients()
            if excluded:
                excluded_lower = {e.lower().strip() for e in excluded}
                filtered = []
                for recipe in recipes:
                    ings = [i.get("name", "").lower() for i in recipe.get("ingredients", [])]
                    if not any(
                        any(excl in ing for ing in ings) for excl in excluded_lower
                    ):
                        filtered.append(recipe)
                if filtered:
                    recipes = filtered
                    _LOGGER.info(
                        "Recettes filtrées (%d exclues pour %d restantes)",
                        len(results) - len(recipes),
                        len(recipes),
                    )

        # Si un jour de la semaine est fourni, planifier le premier résultat
        if weekday and weekday in WEEKDAYS and recipes:
            from datetime import date
            day_idx = WEEKDAYS.index(weekday)
            target_date = self.week_dates(0)[day_idx]
            self.plan[target_date.isoformat()] = recipes[0]
            await self.async_save()
            _LOGGER.info(
                "Repas '%s' planifié sur %s via suggestion IA",
                recipes[0].get("name", ""),
                weekday,
            )

        return recipes

    # ------------------------------------------------------------------
    # Connexion au compte Jow (token JWT + rafraîchissement automatique)
    # ------------------------------------------------------------------
    def _jow_auth_headers(self) -> dict:
        """Headers d'authentification pour l'API Jow avec le token JWT."""
        return {
            "accept": "application/json",
            "accept-language": "fr",
            "content-type": "application/json",
            "x-jow-withmeta": "1",
            "origin": "https://jow.fr",
            "authorization": f"Bearer {self.jow_token}" if self.jow_token else "",
        }

    @property
    def is_authenticated(self) -> bool:
        """True si un token Jow est configuré."""
        return bool(self.jow_token)

    async def async_refresh_jow_token(self) -> bool:
        """Rafraîchit le token JWT Jow via POST /auth?createIfNotExist=false.

        Le token est valide 48h ; on le rafraîchit toutes les 40h.
        """
        if not self.jow_token:
            return False

        def _refresh():
            headers = self._jow_auth_headers()
            resp = requests.post(
                JOW_AUTH_URL,
                headers=headers,
                params={"createIfNotExist": "false"},
                json={"provider": "coursesu"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            return data.get("accessToken")

        try:
            new_token = await self.hass.async_add_executor_job(_refresh)
            if new_token:
                self.jow_token = new_token
                _LOGGER.info("Token Jow rafraîchi avec succès")
                return True
        except Exception as err:
            _LOGGER.warning("Rafraîchissement token Jow échoué : %s", err)
        return False

    async def async_get_jow_profile(self) -> dict | None:
        """Récupère le profil Jow de l'utilisateur connecté."""
        if not self.jow_token:
            return None

        def _get():
            headers = self._jow_auth_headers()
            resp = requests.get(JOW_PROFILE_URL, headers=headers, timeout=15)
            resp.raise_for_status()
            return resp.json().get("data", {})

        try:
            return await self.hass.async_add_executor_job(_get)
        except Exception as err:
            _LOGGER.warning("Récupération profil Jow échouée : %s", err)
            return None

    async def async_sync_preferences_from_jow(self) -> None:
        """Synchronise allergies et préférences depuis le compte Jow.

        Remplace les champs manuels par les données du profil Jow :
        - eatingHabits → préférences (végétarien, sans gluten, etc.)
        - excludedIngredientTastes → allergies/interdits (ingrédients exclus)
        """
        profile = await self.async_get_jow_profile()
        if not profile:
            return

        # Eating habits → preferences
        habits = profile.get("eatingHabits", {})
        pref_labels = []
        habit_map = {
            "vegetarian": "végétarien",
            "vegan": "végétalien",
            "pescatarian": "pescétarien",
            "glutenFree": "sans gluten",
            "dairyFree": "sans lactose",
            "porkless": "sans porc",
        }
        for key, label in habit_map.items():
            if habits.get(key):
                pref_labels.append(label)
        if pref_labels:
            self.preferences = ", ".join(pref_labels)
            _LOGGER.info("Préférences synchronisées depuis Jow : %s", self.preferences)

        # Excluded ingredients → allergies/interdits
        excluded = profile.get("excludedIngredientTastes", [])
        if excluded:
            allergy_names = [e.get("name", "") for e in excluded if e.get("name")]
            if allergy_names:
                self.allergies = ", ".join(allergy_names)
                _LOGGER.info("Allergies synchronisées depuis Jow : %s", self.allergies)

    async def async_get_excluded_ingredients(self) -> list[str]:
        """Retourne la liste des ingrédients exclus du compte Jow."""
        profile = await self.async_get_jow_profile()
        if not profile:
            return []
        excluded = profile.get("excludedIngredientTastes", [])
        return [e.get("name", "") for e in excluded if e.get("name")]

    async def async_get_jow_favorites(self) -> list[dict]:
        """Récupère les recettes favorites du compte Jow."""
        if not self.jow_token:
            return []

        def _get():
            headers = self._jow_auth_headers()
            resp = requests.get(
                JOW_FAVORITES_URL,
                headers=headers,
                params={"availabilityZoneId": "FR", "limit": 20},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("data", {}).get("recipes", [])

        try:
            return await self.hass.async_add_executor_job(_get) or []
        except Exception as err:
            _LOGGER.warning("Récupération favoris Jow échouée : %s", err)
            return []

    async def async_get_jow_shoppinglist(self) -> dict | None:
        """Récupère la liste de courses du compte Jow."""
        if not self.jow_token:
            return None

        def _get():
            headers = self._jow_auth_headers()
            resp = requests.get(
                JOW_SHOPPING_URL,
                headers=headers,
                params={"availabilityZoneId": "FR"},
                timeout=15,
            )
            if resp.status_code == 204:
                return {}
            resp.raise_for_status()
            return resp.json().get("data", {})

        try:
            return await self.hass.async_add_executor_job(_get)
        except Exception as err:
            _LOGGER.warning("Récupération liste de courses Jow échouée : %s", err)
            return None

    async def async_get_jow_menu(self) -> list[dict]:
        """Récupère le menu de la semaine suggéré par Jow."""
        if not self.jow_token:
            return []

        def _get():
            headers = self._jow_auth_headers()
            resp = requests.get(
                JOW_MENU_URL,
                headers=headers,
                params={"availabilityZoneId": "FR"},
                timeout=15,
            )
            if resp.status_code == 204:
                return []
            resp.raise_for_status()
            return resp.json().get("data", {}).get("recipes", [])

        try:
            return await self.hass.async_add_executor_job(_get) or []
        except Exception as err:
            _LOGGER.warning("Récupération menu Jow échouée : %s", err)
            return []

    async def async_start_token_refresh(self) -> None:
        """Démarre la vérification périodique du token Jow.

        Le token Jow n'est pas requis pour le fonctionnement de base
        (recherche publique de recettes). Il sert uniquement à synchroniser
        les allergènes et préférences du compte Jow. On ne rafraîchit pas
        automatiquement (la session provider expire), on vérifie juste
        la validité périodiquement.
        """
        if not self.jow_token:
            return

        from homeassistant.helpers.event import async_track_time_interval

        if self._token_refresh_cancel:
            self._token_refresh_cancel()
        # Vérifier toutes les 6h si le token est encore valide
        self._token_refresh_cancel = async_track_time_interval(
            self.hass,
            self._async_check_token_callback,
            timedelta(hours=6),
        )

    async def _async_check_token_callback(self, now=None) -> None:
        """Vérifie périodiquement si le token Jow est encore valide.

        Si le token a expiré, notifie l'utilisateur pour qu'il le renouvelle
        via le bookmarklet. Les recettes publiques continuent de fonctionner.
        """
        if not self.jow_token:
            return
        valid = await self.async_check_token_validity()
        if not valid:
            from homeassistant.components import persistent_notification
            persistent_notification.async_create(
                self.hass,
                "Le token Jow a expiré. Les recettes publiques continuent de fonctionner, "
                "mais les allergènes ne sont plus synchronisés. Reconnectez-vous sur jow.fr "
                "et cliquez sur le bookmarklet Jow → HA pour renouveler le token.",
                "Jow - Token expiré",
                "jow_token_expired",
            )
            # Vider le token pour éviter les appels inutiles
            self.jow_token = ""

    async def async_check_token_validity(self) -> bool:
        """Vérifie si le token Jow est encore valide."""
        if not self.jow_token:
            return False
        profile = await self.async_get_jow_profile()
        if profile:
            return True
        _LOGGER.warning("Token Jow expiré ou invalide")
        return False
