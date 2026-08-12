"""Client Jow et stockage du planning / de la liste de courses."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_COVERS,
    RECIPE_BASE_URL,
    SIGNAL_UPDATE,
    STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Limites défensives sur les données issues de l'API Jow (non officielle).
_MAX_FIELD_LEN = 2000
_MAX_NAME_LEN = 200
_MAX_ITEMS = 500
_MAX_SUMMARY_LEN = 500
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


def _recipe_to_dict(recipe: Any, covers: int) -> dict:
    """Convertit un JowResult (objet du paquet jow-api) en dict sérialisable.

    On stocke un dict plutôt que l'objet : il doit survivre à un redémarrage
    de Home Assistant et être lisible depuis les templates Jinja.
    """
    ratio = 1.0
    base_covers = getattr(recipe, "coversCount", None) or DEFAULT_COVERS
    if base_covers:
        try:
            ratio = covers / float(base_covers)
        except (TypeError, ValueError, ZeroDivisionError):
            ratio = 1.0

    ingredients = []
    for ing in getattr(recipe, "ingredients", []) or []:
        quantity = getattr(ing, "quantity", None)
        try:
            quantity = round(float(quantity) * ratio, 2) if quantity else quantity
        except (TypeError, ValueError):
            pass
        ingredients.append(
            {
                "name": _truncate(getattr(ing, "name", ""), _MAX_NAME_LEN) or "",
                "quantity": quantity,
                "unit": _truncate(getattr(ing, "unit", ""), _MAX_NAME_LEN) or "",
                "optional": bool(getattr(ing, "isOptional", False)),
            }
        )

    recipe_id = _safe_id(getattr(recipe, "id", None))
    fallback_url = f"{RECIPE_BASE_URL}{recipe_id}" if recipe_id else None
    url = _safe_url(getattr(recipe, "url", None), fallback=fallback_url)
    image = _safe_url(getattr(recipe, "imageUrl", None))
    video = _safe_url(getattr(recipe, "videoUrl", None))

    return {
        "id": recipe_id,
        "name": _truncate(getattr(recipe, "name", "Recette Jow"), _MAX_NAME_LEN)
        or "Recette Jow",
        "url": url,
        "image": image,
        "video": video,
        "description": _truncate(getattr(recipe, "description", None), _MAX_FIELD_LEN),
        "preparation_time": getattr(recipe, "preparationTime", None),
        "cooking_time": getattr(recipe, "cookingTime", None),
        "covers": covers,
        "ingredients": ingredients,
    }


class JowManager:
    """Garde le planning de la semaine et la liste de courses."""

    def __init__(self, hass: HomeAssistant, default_covers: int) -> None:
        self.hass = hass
        self.default_covers = default_covers
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
    async def async_search(self, query: str, limit: int = 5) -> list[Any]:
        """Recherche des recettes sur Jow."""

        def _search():
            from jow_api import Jow  # import tardif : dépendance installée par HA

            return Jow.search(query, limit=limit)

        try:
            return await self.hass.async_add_executor_job(_search) or []
        except Exception as err:  # l'API n'est pas officielle : elle peut casser
            _LOGGER.error("Recherche Jow impossible (%s) : %s", query, err)
            return []

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
