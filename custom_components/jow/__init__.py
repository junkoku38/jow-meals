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
    ATTR_ENTRY_NAME,
    CONF_AI_ENTITY,
    CONF_ALLERGIES,
    CONF_JOW_TOKEN,
    CONF_PREFERENCES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COVERS,
    DOMAIN,
    GOOGLE_CLIENT_ID,
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
    # Vérifier le token Jow et synchroniser les préférences si valide
    if manager.is_authenticated:
        if await manager.async_check_token_validity():
            await manager.async_sync_preferences_from_jow()
        # Démarrer la vérification périodique du token
        await manager.async_start_token_refresh()

    # Enregistrer le endpoint HTTP pour recevoir le JWT depuis le bookmarklet
    hass.http.register_view(JowTokenView(manager))
    # Enregistrer les endpoints pour le flow OAuth2 Google
    hass.http.register_view(JowGoogleAuthView())
    hass.http.register_view(JowGoogleCallbackView(hass.data.get(DOMAIN, {})))

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

    async def handle_meal_done(call: ServiceCall) -> ServiceResponse:
        """Marque un repas comme fait et retire les ingrédients de la liste."""
        day = _resolve_date(manager, call)
        result = await manager.async_meal_done(day)
        if result is None:
            return {"error": "Aucun repas planifié pour cette date"}
        return result

    async def handle_sync_calories(call: ServiceCall) -> ServiceResponse:
        """Récupère les calories manquantes pour tous les repas planifiés."""
        updated = await manager.async_sync_calories(
            call.data.get(ATTR_WEEK_OFFSET, 0)
        )
        return {"updated": updated}

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
                    vol.Optional(ATTR_ENTRY_NAME): cv.string,
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
                    vol.Optional(ATTR_ENTRY_NAME): cv.string,
                }
            ),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_WEEK,
            handle_clear_week,
            schema=vol.Schema({vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int), vol.Optional(ATTR_ENTRY_NAME): cv.string}),
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_SHOPPING_LIST,
            handle_refresh_list,
            schema=vol.Schema(
                {
                    vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
                    vol.Optional("keep_checked", default=True): cv.boolean,
                    vol.Optional(ATTR_ENTRY_NAME): cv.string,
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
                    vol.Optional(ATTR_ENTRY_NAME): cv.string,
                }
            ),
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
                    vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
                    vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
                    vol.Optional(ATTR_ENTRY_NAME): cv.string,
                }
            ),
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
        hass.services.async_register(
            DOMAIN,
            SERVICE_MEAL_DONE,
            handle_meal_done,
            schema=vol.Schema(
                {
                    vol.Optional(ATTR_DATE): cv.date,
                    vol.Optional(ATTR_WEEKDAY): vol.In(WEEKDAYS),
                    vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
                }
            ),
            supports_response=SupportsResponse.ONLY,
        )

    # sync_calories est enregistré en dehors du bloc has_service pour
    # garantir qu'il est disponible même après un reload de l'intégration.
    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_CALORIES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SYNC_CALORIES,
            handle_sync_calories,
            schema=vol.Schema(
                {
                    vol.Optional(ATTR_WEEK_OFFSET, default=0): vol.Coerce(int),
                }
            ),
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
                SERVICE_MEAL_DONE,
                SERVICE_SYNC_CALORIES,
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

            # Vérifier la validité du token
            ok = await self._manager.async_check_token_validity()

            if ok:
                # Synchroniser les préférences (allergènes, habitudes)
                await self._manager.async_sync_preferences_from_jow()
                # Démarrer la vérification périodique
                await self._manager.async_start_token_refresh()
                # Notification de succès
                persistent_notification.async_create(
                    self._manager.hass,
                    "Token Jow reçu et validé. Allergènes et préférences synchronisés depuis votre compte Jow.",
                    "Jow - Connexion réussie",
                    "jow_token_received",
                )
                return web.json_response({"status": "ok", "message": "Token valide"})
            else:
                persistent_notification.async_create(
                    self._manager.hass,
                    "Token Jow reçu mais invalide ou expiré. Vérifiez que vous êtes connecté sur jow.fr.",
                    "Jow - Token invalide",
                    "jow_token_invalid",
                )
                return web.json_response({"error": "Token invalide"}, status=401)

        except Exception as err:
            _LOGGER.error("Erreur lors de la réception du token Jow : %s", err)
            return web.json_response({"error": str(err)}, status=500)


class JowGoogleAuthView(HomeAssistantView):
    """Démarre le flow OAuth2 Google pour récupérer un credential.

    URL: /api/jow/google_auth
    Redirige vers Google, puis Google redirige vers /api/jow/google_callback
    avec le credential (ID token).
    """

    url = "/api/jow/google_auth"
    name = "api:jow:google_auth"
    requires_auth = False  # l'utilisateur clique sur ce lien depuis HA

    async def get(self, request):
        from aiohttp import web
        from urllib.parse import urlencode

        # L'URL de callback (vers HA)
        ha_url = f"{request.scheme}://{request.host}"
        callback_url = f"{ha_url}/api/jow/google_callback"

        # Construire l'URL d'autorisation Google
        params = urlencode({
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": callback_url,
            "response_type": "code",
            "scope": "openid email profile",
            "prompt": "consent",
        })
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{params}"
        return web.HTTPFound(auth_url)


class JowGoogleCallbackView(HomeAssistantView):
    """Reçoit le credential Google et l'échange contre un token Jow.

    URL: /api/jow/google_callback
    Google redirige ici avec ?code=... qu'on échange contre un ID token,
    puis on envoie l'ID token à Jow pour obtenir le JWT Jow.
    """

    url = "/api/jow/google_callback"
    name = "api:jow:google_callback"
    requires_auth = False

    def __init__(self, managers: dict) -> None:
        self._managers = managers

    async def get(self, request):
        from aiohttp import web
        import requests as req

        code = request.query.get("code")
        if not code:
            return web.Response(text="Code d'autorisation manquant", status=400)

        ha_url = f"{request.scheme}://{request.host}"
        callback_url = f"{ha_url}/api/jow/google_callback"

        # 1. Échanger le code contre un ID token Google
        token_resp = await request.app["hass"].async_add_executor_job(
            lambda: req.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": GOOGLE_CLIENT_ID,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": callback_url,
                },
                timeout=15,
            )
        )

        if token_resp.status_code != 200:
            _LOGGER.error("Échange code Google échoué : %s", token_resp.text[:200])
            return web.Response(text="Échange Google échoué", status=400)

        token_data = token_resp.json()
        id_token = token_data.get("id_token")
        if not id_token:
            return web.Response(text="ID token Google manquant", status=400)

        # 2. Envoyer l'ID token à Jow pour obtenir un JWT Jow
        jow_resp = await request.app["hass"].async_add_executor_job(
            lambda: req.post(
                "https://api.jow.fr/public/auth",
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "origin": "https://jow.fr",
                    "referer": "https://jow.fr/",
                    "x-jow-withmeta": "true",
                },
                params={"createIfNotExist": "true"},
                json={"googleIdToken": id_token},
                timeout=15,
            )
        )

        if jow_resp.status_code != 200:
            _LOGGER.error("Auth Jow Google échoué : %s", jow_resp.text[:200])
            return web.Response(text="Auth Jow échouée", status=400)

        jow_data = jow_resp.json().get("data", {})
        jow_token = jow_data.get("accessToken")
        if not jow_token:
            return web.Response(text="Token Jow manquant dans la réponse", status=400)

        # 3. Stocker le token dans le premier manager disponible
        hass = request.app["hass"]
        manager = None
        for entry_id, mgr in hass.data.get(DOMAIN, {}).items():
            manager = mgr
            break

        if not manager:
            return web.Response(text="Aucune instance Jow configurée", status=400)

        manager.jow_token = jow_token
        ok = await manager.async_check_token_validity()
        if ok:
            await manager.async_sync_preferences_from_jow()
            await manager.async_start_token_refresh()
            persistent_notification.async_create(
                hass,
                "Connexion Google réussie ! Token Jow récupéré automatiquement. "
                "Allergènes et préférences synchronisés.",
                "Jow - Connexion Google réussie",
                "jow_google_auth_success",
            )
            return web.Response(text="✅ Connexion Google réussie ! Token Jow récupéré. Vous pouvez fermer cette page.", content_type="text/html")
        else:
            persistent_notification.async_create(
                hass,
                "Token Jow reçu mais invalide après auth Google.",
                "Jow - Token invalide",
                "jow_google_auth_invalid",
            )
            return web.Response(text="Token Jow invalide", status=400)
