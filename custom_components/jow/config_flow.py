"""Config flow de l'intégration Jow."""

from __future__ import annotations

import base64
import json
import logging
import re

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType
import voluptuous as vol

from .const import (
    CONF_AI_ENTITY,
    CONF_ALLERGIES,
    CONF_JOW_REFRESH_TOKEN,
    CONF_PREFERENCES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COVERS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _validate_refresh_token(token: str) -> str | None:
    """Vérifie la forme d'un refresh token Jow (JWT décodable, type refresh).

    Retourne un message d'erreur explicite, ou None si le token semble
    valide. Attrape les deux erreurs de collage les plus courantes :
    token tronqué/corrompu (payload non décodable) et access token collé
    à la place du refresh token (vive 48 h au lieu de ~6 mois).
    """
    token = (token or "").strip()
    if not token:
        return None  # champ vide = pas de compte Jow, légitime
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
        return "invalid_format"
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, json.JSONDecodeError):
        return "corrupted"
    if data.get("type") == "access":
        return "wrong_type"
    if data.get("type") != "refresh":
        return "invalid_format"
    return None


class JowConfigFlow(ConfigFlow, domain=DOMAIN):
    """L'API Jow utilisée ici est publique : aucun identifiant n'est demandé.

    Plusieurs instances sont supportées : chaque instance porte un nom
    (ex. « Paul », « Camille ») qui sert d'identifiant unique.
    """

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            err = _validate_refresh_token(user_input.get(CONF_JOW_REFRESH_TOKEN, ""))
            if err:
                errors[CONF_JOW_REFRESH_TOKEN] = err
            else:
                name = user_input.get("name", "Jow").strip() or "Jow"
                await self.async_set_unique_id(f"{DOMAIN}_{name.lower()}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={"name": name},
                    options={
                        "covers": user_input["covers"],
                        CONF_ALLERGIES: user_input.get(CONF_ALLERGIES, ""),
                        CONF_PREFERENCES: user_input.get(CONF_PREFERENCES, ""),
                        CONF_AI_ENTITY: user_input.get(CONF_AI_ENTITY, ""),
                        CONF_WEATHER_ENTITY: user_input.get(CONF_WEATHER_ENTITY, ""),
                        CONF_JOW_REFRESH_TOKEN: user_input.get(CONF_JOW_REFRESH_TOKEN, ""),
                    },
                )

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("name", default="Jow"): cv.string,
                    vol.Required("covers", default=DEFAULT_COVERS): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=12)
                    ),
                    vol.Optional(CONF_ALLERGIES, default=""): cv.string,
                    vol.Optional(CONF_PREFERENCES, default=""): cv.string,
                    vol.Optional(CONF_AI_ENTITY, default=""): cv.string,
                    vol.Optional(CONF_WEATHER_ENTITY, default=""): cv.string,
                    vol.Optional(CONF_JOW_REFRESH_TOKEN, default=""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return JowOptionsFlow()


class JowOptionsFlow(OptionsFlow):
    """Permet de changer le nombre de couverts, allergies, préférences, agent IA."""

    async def async_step_init(self, user_input=None):
        errors = {}
        if user_input is not None:
            err = _validate_refresh_token(user_input.get(CONF_JOW_REFRESH_TOKEN, ""))
            if err:
                errors[CONF_JOW_REFRESH_TOKEN] = err
            else:
                return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "name",
                        default=opts.get("name", self.config_entry.data.get("name", "Jow")),
                    ): cv.string,
                    vol.Required("covers", default=opts.get("covers", DEFAULT_COVERS)): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=12)
                    ),
                    vol.Optional(CONF_ALLERGIES, default=opts.get(CONF_ALLERGIES, "")): cv.string,
                    vol.Optional(CONF_PREFERENCES, default=opts.get(CONF_PREFERENCES, "")): cv.string,
                    vol.Optional(CONF_AI_ENTITY, default=opts.get(CONF_AI_ENTITY, "")): cv.string,
                    vol.Optional(CONF_WEATHER_ENTITY, default=opts.get(CONF_WEATHER_ENTITY, "")): cv.string,
                    vol.Optional(CONF_JOW_REFRESH_TOKEN, default=opts.get(CONF_JOW_REFRESH_TOKEN, "")): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
        )
