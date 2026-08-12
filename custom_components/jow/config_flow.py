"""Config flow de l'intégration Jow."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback

from .const import DEFAULT_COVERS, DOMAIN


class JowConfigFlow(ConfigFlow, domain=DOMAIN):
    """L'API Jow utilisée ici est publique : aucun identifiant n'est demandé."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is not None:
            return self.async_create_entry(
                title="Jow", data={}, options={"covers": user_input["covers"]}
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("covers", default=DEFAULT_COVERS): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=12)
                    )
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return JowOptionsFlow()


class JowOptionsFlow(OptionsFlow):
    """Permet de changer le nombre de couverts par défaut."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get("covers", DEFAULT_COVERS)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("covers", default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=12)
                    )
                }
            ),
        )
