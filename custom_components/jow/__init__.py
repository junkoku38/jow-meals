"""Intégration Jow (non officielle) pour Home Assistant."""

from __future__ import annotations

import logging
from datetime import date, datetime

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CHOICE,
    ATTR_COVERS,
    ATTR_DATE,
    ATTR_LIMIT,
    ATTR_QUERY,
    ATTR_WEEK_OFFSET,
    ATTR_WEEKDAY,
    DEFAULT_COVERS,
    DOMAIN,
    SERVICE_CLEAR_MEAL,
    SERVICE_CLEAR_WEEK,
    SERVICE_PLAN_MEAL,
    SERVICE_REFRESH_SHOPPING_LIST,
    SERVICE_SEARCH,
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
    manager = JowManager(hass, entry.options.get("covers", DEFAULT_COVERS))
    await manager.async_load()
    manager.purge_old()

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
            ):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok
