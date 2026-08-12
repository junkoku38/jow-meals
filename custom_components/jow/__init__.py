"""Intégration Jow (non officielle) pour Home Assistant."""

from __future__ import annotations

import logging
from datetime import date, datetime

import voluptuous as vol
from homeassistant.components.http import HomeAssistantView
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
    CONF_AI_ENTITY,
    CONF_ALLERGIES,
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
    SERVICE_SYNC_PROFILE,
    WEEKDAYS,
)
from .manager import JowManager, _recipe_to_dict

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.TODO]


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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Configure l'intégration depuis l'UI."""
    opts = entry.options
    manager = JowManager(
        hass,
        opts.get("covers", DEFAULT_COVERS),
        allergies=opts.get(CONF_ALLERGIES, ""),
        preferences=opts.get(CONF_PREFERENCES, ""),
        ai_entity=opts.get(CONF_AI_ENTITY, ""),
        weather_entity=opts.get(CONF_WEATHER_ENTITY, ""),
        jow_token=opts.get(CONF_JOW_TOKEN, ""),
    )
    await manager.async_load()
    manager.purge_old()
    # Démarrer le rafraîchissement automatique du token Jow si configuré
    if manager.is_authenticated:
        await manager.async_start_token_refresh()
        # Synchroniser allergies et préférences depuis le compte Jow
        await manager.async_sync_preferences_from_jow()

    # Enregistrer le endpoint HTTP pour recevoir le JWT depuis le bookmarklet
    hass.http.register_view(JowTokenView(manager))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------
    async def handle_plan_meal(call: ServiceCall) -> None:
        day = _resolve_date(manager, call)
        await manager.async_plan_meal(
            day,
            call.data[ATTR_QUERY],
            covers=call.data.get(ATTR_COVERS),
            choice=call.data.get(ATTR_CHOICE, 1),
        )

    async def handle_clear_meal(call: ServiceCall) -> None:
        await manager.async_clear_meal(_resolve_date(manager, call))

    async def handle_clear_week(call: ServiceCall) -> None:
        await manager.async_clear_week(call.data.get(ATTR_WEEK_OFFSET, 0))

    async def handle_refresh_list(call: ServiceCall) -> None:
        await manager.async_refresh_shopping_list(
            call.data.get(ATTR_WEEK_OFFSET, 0),
            keep_checked=call.data.get("keep_checked", True),
        )

    async def handle_search(call: ServiceCall) -> ServiceResponse:
        """Renvoie les résultats : utile pour un agent conversationnel."""
        results = await manager.async_search(
            call.data[ATTR_QUERY], limit=call.data.get(ATTR_LIMIT, 5)
        )
        covers = call.data.get(ATTR_COVERS) or manager.default_covers
        return {"recipes": [_recipe_to_dict(r, covers) for r in results]}

    async def handle_suggest(call: ServiceCall) -> ServiceResponse:
        """Suggère des recettes via l'IA (ai_task) puis recherche sur Jow."""
        results = await manager.async_suggest(
            criteria=call.data.get(ATTR_CRITERIA, ""),
            covers=call.data.get(ATTR_COVERS),
            limit=call.data.get(ATTR_LIMIT, 5),
            weather_entity=call.data.get(CONF_WEATHER_ENTITY),
            ai_entity=call.data.get(CONF_AI_ENTITY),
        )
        return {"recipes": results}

    async def handle_sync_profile(call: ServiceCall) -> ServiceResponse:
        """Récupère le profil Jow de l'utilisateur connecté."""
        profile = await manager.async_get_jow_profile()
        if profile is None:
            return {"error": "Non authentifié ou token invalide"}
        return {"profile": profile}

    async def handle_sync_favorites(call: ServiceCall) -> ServiceResponse:
        """Récupère les recettes favorites du compte Jow."""
        favorites = await manager.async_get_jow_favorites()
        return {"recipes": favorites}

    async def handle_sync_preferences(call: ServiceCall) -> ServiceResponse:
        """Synchronise allergies et préférences depuis le compte Jow."""
        await manager.async_sync_preferences_from_jow()
        return {
            "allergies": manager.allergies,
            "preferences": manager.preferences,
        }

    if not hass.services.has_service(DOMAIN, SERVICE_PLAN_MEAL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_PLAN_MEAL,
            handle_plan_meal,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_QUERY): cv.string,
                    vol.Optional(ATTR_DATE): cv.date,
                    vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
                    vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
                    vol.Optional(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
                    vol.Optional(ATTR_CHOICE, default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_MEAL,
            handle_clear_meal,
            schema=vol.Schema(
                {
                    vol.Optional(ATTR_DATE): cv.date,
                    vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
                    vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_WEEK,
            handle_clear_week,
            schema=vol.Schema({vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int)}),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_SHOPPING_LIST,
            handle_refresh_list,
            schema=vol.Schema(
                {
                    vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
                    vol.Optional("keep_checked", default=True): cv.boolean,
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEARCH,
            handle_search,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_QUERY): cv.string,
                    vol.Optional(ATTR_LIMIT, default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
                    vol.Optional(ATTR_COVERS): vol.Coerce(int),
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SUGGEST,
            handle_suggest,
            schema=vol.Schema(
                {
                    vol.Optional(ATTR_CRITERIA): cv.string,
                    vol.Optional(ATTR_LIMIT, default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
                    vol.Optional(ATTR_COVERS): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
                    vol.Optional(CONF_WEATHER_ENTITY): cv.string,
                    vol.Optional(CONF_AI_ENTITY): cv.string,
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_PROFILE,
            handle_sync_profile,
            schema=vol.Schema({}),
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_FAVORITES,
            handle_sync_favorites,
            schema=vol.Schema({}),
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_PREFERENCES,
            handle_sync_preferences,
            schema=vol.Schema({}),
            supports_response=SupportsResponse.ONLY,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Décharge l'intégration."""
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
            ):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok


class JowTokenView(HomeAssistantView):
    """Endpoint HTTP pour recevoir le JWT Jow depuis le bookmarklet.

    URL: /api/jow/token
    Méthode: POST
    Body: {"token": "eyJ..."}
    """

    url = "/api/jow/token"
    name = "api:jow:token"
    requires_auth = True

    def __init__(self, manager: JowManager) -> None:
        self._manager = manager

    async def post(self, request):
        """Reçoit le JWT Jow et le stocke dans le manager."""
        from aiohttp import web
        import json

        try:
            body = await request.json()
            token = body.get("token", "")
            if not token or not token.startswith("eyJ"):
                return web.json_response({"error": "Token invalide"}, status=400)

            # Mettre à jour le token dans le manager
            self._manager.jow_token = token
            _LOGGER.info("Token Jow reçu via bookmarklet")

            # Rafraîchir immédiatement pour valider
            ok = await self._manager.async_refresh_jow_token()
            if not ok:
                # Le refresh peut échouer si la session provider a expiré,
                # mais le token lui-même est valide 48h
                ok = await self._manager.async_check_token_validity()

            if ok:
                # Synchroniser les préférences
                await self._manager.async_sync_preferences_from_jow()
                # Démarrer le rafraîchissement auto
                await self._manager.async_start_token_refresh()
                # Notification de succès
                persistent_notification.async_create(
                    self._manager.hass,
                    "Token Jow reçu et validé. Connexion au compte Courses U active.",
                    "Jow - Connexion réussie",
                    "jow_token_received",
                )
                return web.json_response({"status": "ok", "message": "Token valide"})
            else:
                persistent_notification.async_create(
                    self._manager.hass,
                    "Token Jow reçu mais invalide. Vérifiez que vous êtes connecté sur jow.fr.",
                    "Jow - Token invalide",
                    "jow_token_invalid",
                )
                return web.json_response({"error": "Token invalide"}, status=401)

        except Exception as err:
            _LOGGER.error("Erreur lors de la réception du token Jow : %s", err)
            return web.json_response({"error": str(err)}, status=500)
