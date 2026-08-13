"""Intégration Jow (non officielle) pour Home Assistant."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CHOICE,
    ATTR_COVERS,
    ATTR_CRITERIA,
    ATTR_DATE,
    ATTR_LIMIT,
    ATTR_QUERY,
    ATTR_WEEK_OFFSET,
    ATTR_WEEKDAY,
    ATTR_ENTRY_NAME,
    ATTR_TO_DATE,
    ATTR_TO_WEEKDAY,
    ATTR_TO_WEEK_OFFSET,
    ATTR_INGREDIENT,
    CONF_AI_ENTITY,
    CONF_ALLERGIES,
    CONF_JOW_REFRESH_TOKEN,
    CONF_JOW_TOKEN,
    CONF_PREFERENCES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COVERS,
    DOMAIN,
    SERVICE_CLEAR_MEAL,
    SERVICE_CLEAR_WEEK,
    SERVICE_PLAN_MEAL,
    SERVICE_REFRESH_SHOPPING_LIST,
    SERVICE_SEARCH,
    SERVICE_SUGGEST,
    SERVICE_SYNC_FAVORITES,
    SERVICE_SYNC_PREFERENCES,
    SERVICE_MEAL_DONE,
    SERVICE_SYNC_PROFILE,
    SERVICE_SYNC_CALORIES,
    SERVICE_SEND_MENU,
    SERVICE_COPY_MEAL,
    SERVICE_SET_COVERS,
    SERVICE_EXCLUDE_INGREDIENT,
    SERVICE_GET_CONTEXT,
    SERVICE_CLEAR_RECENT,
    WEEKDAYS,
)
from .manager import JowManager, _recipe_to_dict

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.TODO]


def _get_manager(hass: HomeAssistant, call: ServiceCall, default_manager: JowManager) -> JowManager:
    """Résout le bon manager selon le paramètre entry_name.

    Si entry_name est fourni, on cherche l'instance correspondante.
    Sinon, on retombe sur l'instance par défaut (la première configurée).
    """
    entry_name = call.data.get("entry_name")
    if not entry_name:
        return default_manager
    for entry_id, manager in hass.data.get(DOMAIN, {}).items():
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry and (entry.title == entry_name or entry.data.get("name") == entry_name):
            return manager
    _LOGGER.warning("Instance Jow « %s » introuvable, utilisation de l'instance par défaut", entry_name)
    return default_manager


def _resolve_date(manager: JowManager, call: ServiceCall) -> date:
    """Accepte soit une date explicite, soit un jour de la semaine."""
    if raw := call.data.get(ATTR_DATE):
        if isinstance(raw, date):
            return raw
        return datetime.fromisoformat(str(raw)).date()

    weekday = call.data.get(ATTR_WEEKDAY)
    offset = call.data.get(ATTR_WEEK_OFFSET, 0)
    if weekday:
        return manager.week_dates(offset)[WEEKDAYS.index(weekday)]
    return date.today()


def _resolve_to_date(manager: JowManager, call: ServiceCall) -> date:
    """Résout la date cible pour copy_meal (to_date ou to_weekday)."""
    if raw := call.data.get(ATTR_TO_DATE):
        if isinstance(raw, date):
            return raw
        return datetime.fromisoformat(str(raw)).date()
    weekday = call.data.get(ATTR_TO_WEEKDAY)
    offset = call.data.get(ATTR_TO_WEEK_OFFSET, call.data.get(ATTR_WEEK_OFFSET, 0))
    if weekday:
        return manager.week_dates(offset)[WEEKDAYS.index(weekday)]
    return date.today()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configure l'intégration depuis l'UI."""
    opts = entry.options
    # L'access token (jow_token) est persisté par le refresh automatique,
    # mais l'utilisateur ne saisit que le refresh token. On le récupère
    # depuis les options ; l'access token vient de la config entry (si
    # un refresh a déjà eu lieu) ou sera généré au démarrage.
    jow_token = opts.get(CONF_JOW_TOKEN, "") or entry.data.get(CONF_JOW_TOKEN, "")
    manager = JowManager(
        hass,
        opts.get("covers", DEFAULT_COVERS),
        allergies=opts.get(CONF_ALLERGIES, ""),
        preferences=opts.get(CONF_PREFERENCES, ""),
        ai_entity=opts.get(CONF_AI_ENTITY, ""),
        weather_entity=opts.get(CONF_WEATHER_ENTITY, ""),
        jow_token=jow_token,
        jow_refresh_token=opts.get(CONF_JOW_REFRESH_TOKEN, ""),
        entry_id=entry.entry_id,
    )
    await manager.async_load()
    manager.purge_old()
    # Si on a un refresh token mais pas d'access token valide, on en
    # génère un immédiatement au démarrage.
    if manager.jow_refresh_token and not manager.is_authenticated:
        await manager.async_refresh_jow_token()
    # Vérifier le token Jow et synchroniser les préférences si valide
    if manager.is_authenticated:
        if await manager.async_check_token_validity():
            await manager.async_sync_preferences_from_jow()
        # Démarrer le rafraîchissement périodique du token
        await manager.async_start_token_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------
    async def handle_plan_meal(call: ServiceCall) -> None:
        mgr = _get_manager(hass, call, manager)
        day = _resolve_date(mgr, call)
        await mgr.async_plan_meal(
            day,
            call.data[ATTR_QUERY],
            covers=call.data.get(ATTR_COVERS),
            choice=call.data.get(ATTR_CHOICE, 1),
        )

    async def handle_clear_meal(call: ServiceCall) -> None:
        mgr = _get_manager(hass, call, manager)
        await mgr.async_clear_meal(_resolve_date(mgr, call))

    async def handle_clear_week(call: ServiceCall) -> None:
        mgr = _get_manager(hass, call, manager)
        await mgr.async_clear_week(call.data.get(ATTR_WEEK_OFFSET, 0))

    async def handle_refresh_list(call: ServiceCall) -> None:
        mgr = _get_manager(hass, call, manager)
        await mgr.async_refresh_shopping_list(
            call.data.get(ATTR_WEEK_OFFSET, 0),
            keep_checked=call.data.get("keep_checked", True),
        )

    async def handle_search(call: ServiceCall) -> ServiceResponse:
        """Renvoie les résultats : utile pour un agent conversationnel."""
        mgr = _get_manager(hass, call, manager)
        results = await mgr.async_search(
            call.data[ATTR_QUERY], limit=call.data.get(ATTR_LIMIT, 5)
        )
        covers = call.data.get(ATTR_COVERS) or mgr.default_covers
        return {"recipes": [_recipe_to_dict(r, covers) for r in results]}

    async def handle_suggest(call: ServiceCall) -> ServiceResponse:
        """Suggère des recettes via l'IA (ai_task) puis recherche sur Jow."""
        mgr = _get_manager(hass, call, manager)
        results = await mgr.async_suggest(
            criteria=call.data.get(ATTR_CRITERIA, ""),
            covers=call.data.get(ATTR_COVERS),
            limit=call.data.get(ATTR_LIMIT, 5),
            weather_entity=call.data.get(CONF_WEATHER_ENTITY),
            ai_entity=call.data.get(CONF_AI_ENTITY),
            weekday=call.data.get(ATTR_WEEKDAY),
            week_offset=call.data.get(ATTR_WEEK_OFFSET, 0),
            ai_prompt=call.data.get("ai_prompt", ""),
        )
        return {"recipes": results}

    async def handle_sync_profile(call: ServiceCall) -> ServiceResponse:
        """Récupère le profil Jow de l'utilisateur connecté."""
        mgr = _get_manager(hass, call, manager)
        profile = await mgr.async_get_jow_profile()
        if profile is None:
            return {"error": "Non authentifié ou token invalide"}
        return {"profile": profile}

    async def handle_sync_favorites(call: ServiceCall) -> ServiceResponse:
        """Récupère les recettes favorites du compte Jow et les met en cache."""
        mgr = _get_manager(hass, call, manager)
        favorites = await mgr.async_get_jow_favorites()
        mgr.favorites = favorites
        # Emettre un signal pour mettre a jour les capteurs
        from .const import SIGNAL_UPDATE
        from homeassistant.helpers.dispatcher import async_dispatcher_send
        async_dispatcher_send(hass, SIGNAL_UPDATE)
        return {"recipes": favorites, "count": len(favorites)}

    async def handle_sync_preferences(call: ServiceCall) -> ServiceResponse:
        """Synchronise allergies et préférences depuis le compte Jow."""
        mgr = _get_manager(hass, call, manager)
        await mgr.async_sync_preferences_from_jow()
        return {
            "allergies": mgr.allergies,
            "preferences": mgr.preferences,
        }

    async def handle_meal_done(call: ServiceCall) -> ServiceResponse:
        """Marque un repas comme fait et retire les ingrédients de la liste."""
        mgr = _get_manager(hass, call, manager)
        day = _resolve_date(mgr, call)
        result = await mgr.async_meal_done(day)
        if result is None:
            return {"error": "Aucun repas planifié pour cette date"}
        return result

    async def handle_copy_meal(call: ServiceCall) -> ServiceResponse:
        """Copie un repas d'un jour vers un autre."""
        mgr = _get_manager(hass, call, manager)
        from_day = _resolve_date(mgr, call)
        to_day = _resolve_to_date(mgr, call)
        result = await mgr.async_copy_meal(from_day, to_day)
        if result is None:
            return {"error": "Aucun repas planifié sur la date source"}
        return result

    async def handle_set_covers(call: ServiceCall) -> ServiceResponse:
        """Change le nombre de couverts d'un repas planifié."""
        mgr = _get_manager(hass, call, manager)
        day = _resolve_date(mgr, call)
        covers = call.data.get(ATTR_COVERS, 2)
        result = await mgr.async_set_covers(day, covers)
        if result is None:
            return {"error": "Aucun repas planifié pour cette date"}
        return result

    async def handle_exclude_ingredient(call: ServiceCall) -> ServiceResponse:
        """Retire un ingrédient de la liste de courses (déjà en stock)."""
        mgr = _get_manager(hass, call, manager)
        ingredient = call.data.get(ATTR_INGREDIENT, "")
        if not ingredient:
            return {"error": "Aucun ingrédient spécifié"}
        result = await mgr.async_exclude_ingredient(ingredient)
        return result

    async def handle_get_context(call: ServiceCall) -> ServiceResponse:
        """Retourne le contexte IA complet (allergies, préférences, plats récents)."""
        mgr = _get_manager(hass, call, manager)
        from datetime import date as _date, timedelta as _td
        cutoff = (_date.today() - _td(weeks=4)).isoformat()
        recent = []
        for day_iso, meal in mgr.plan.items():
            if meal and meal.get("name") and day_iso >= cutoff:
                recent.append({
                    "name": meal["name"],
                    "date": day_iso,
                    "excluded": not meal.get("_no_exclude", False),
                })
        excluded = []
        if mgr.jow_token:
            excluded = await mgr.async_get_excluded_ingredients()
        return {
            "allergies": mgr.allergies or "",
            "preferences": mgr.preferences or "",
            "excluded_ingredients": excluded,
            "recent_meals": recent,
            "jow_connected": bool(mgr.jow_token),
            "default_covers": mgr.default_covers,
        }

    async def handle_clear_recent(call: ServiceCall) -> ServiceResponse:
        """Retire un plat de l'anti-répétition (pourra être re-proposé)."""
        mgr = _get_manager(hass, call, manager)
        date_iso = call.data.get("date", "")
        result = await mgr.async_clear_recent(date_iso)
        return result

    async def handle_sync_calories(call: ServiceCall) -> ServiceResponse:
        """Récupère les calories manquantes pour tous les repas planifiés."""
        mgr = _get_manager(hass, call, manager)
        updated = await mgr.async_sync_calories(
            call.data.get(ATTR_WEEK_OFFSET, 0)
        )
        return {"updated": updated}

    async def handle_send_menu(call: ServiceCall) -> ServiceResponse:
        """Envoie le menu de la semaine au compte Jow (panier)."""
        mgr = _get_manager(hass, call, manager)
        sent = await mgr.async_send_menu_to_jow(
            call.data.get(ATTR_WEEK_OFFSET, 0)
        )
        return {"sent": sent, "message": f"{sent} recettes envoyées à Jow"}

    # Enregistrement des services (toujours ré-enregistrer pour que les
    # handlers pointent vers le manager courant après reload ; _get_manager
    # résout l'instance via entry_name).
    hass.services.async_register(
        DOMAIN, SERVICE_PLAN_MEAL, handle_plan_meal,
        schema=vol.Schema({
            vol.Required(ATTR_QUERY): cv.string,
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
            vol.Optional(ATTR_CHOICE, default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_MEAL, handle_clear_meal,
        schema=vol.Schema({
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_WEEK, handle_clear_week,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_SHOPPING_LIST, handle_refresh_list,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional("keep_checked", default=True): cv.boolean,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEARCH, handle_search,
        schema=vol.Schema({
            vol.Required(ATTR_QUERY): cv.string,
            vol.Optional(ATTR_LIMIT, default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
            vol.Optional(ATTR_COVERS): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SUGGEST, handle_suggest,
        schema=vol.Schema({
            vol.Optional(ATTR_CRITERIA): cv.string,
            vol.Optional(ATTR_LIMIT, default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
            vol.Optional(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
            vol.Optional(CONF_WEATHER_ENTITY): cv.string,
            vol.Optional(CONF_AI_ENTITY): cv.string,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
            vol.Optional("ai_prompt"): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_PROFILE, handle_sync_profile,
        schema=vol.Schema({}, extra=vol.ALLOW_EXTRA), supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_FAVORITES, handle_sync_favorites,
        schema=vol.Schema({}, extra=vol.ALLOW_EXTRA), supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_PREFERENCES, handle_sync_preferences,
        schema=vol.Schema({}, extra=vol.ALLOW_EXTRA), supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_MEAL_DONE, handle_meal_done,
        schema=vol.Schema({
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_COPY_MEAL, handle_copy_meal,
        schema=vol.Schema({
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_TO_DATE): cv.date,
            vol.Optional(ATTR_TO_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_TO_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_COVERS, handle_set_covers,
        schema=vol.Schema({
            vol.Optional(ATTR_DATE): cv.date,
            vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Required(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXCLUDE_INGREDIENT, handle_exclude_ingredient,
        schema=vol.Schema({
            vol.Required(ATTR_INGREDIENT): cv.string,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_CONTEXT, handle_get_context,
        schema=vol.Schema({}, extra=vol.ALLOW_EXTRA),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_RECENT, handle_clear_recent,
        schema=vol.Schema({
            vol.Required("date"): cv.string,
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_CALORIES, handle_sync_calories,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_MENU, handle_send_menu,
        schema=vol.Schema({
            vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
            vol.Optional(ATTR_ENTRY_NAME): cv.string,
        }),
        supports_response=SupportsResponse.ONLY,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge l'intégration."""
    manager: JowManager = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if manager and manager._token_refresh_cancel:
        manager._token_refresh_cancel()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            for service in (
                SERVICE_PLAN_MEAL,
                SERVICE_CLEAR_MEAL,
                SERVICE_CLEAR_WEEK,
                SERVICE_REFRESH_SHOPPING_LIST,
                SERVICE_SEARCH,
                SERVICE_SUGGEST,
                SERVICE_SYNC_PROFILE,
                SERVICE_SYNC_FAVORITES,
                SERVICE_SYNC_PREFERENCES,
                SERVICE_MEAL_DONE,
                SERVICE_SYNC_CALORIES,
                SERVICE_SEND_MENU,
                SERVICE_COPY_MEAL,
                SERVICE_SET_COVERS,
                SERVICE_EXCLUDE_INGREDIENT,
                SERVICE_GET_CONTEXT,
                SERVICE_CLEAR_RECENT,
            ):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok


