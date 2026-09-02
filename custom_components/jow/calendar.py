"""Entité calendar Jow : le menu de la semaine dans le calendrier HA.

Chaque repas planifié devient un événement (titre = plat, description =
ingrédients + lien recette). Permet les automatisations natives HA sur
le calendrier (« 30 min avant le repas du soir, rappelle la recette »)
et la vue Calendrier du dashboard.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .manager import JowManager

# Heures conventionnelles des repas (le planning Jow n'a pas d'heure) :
# dîner par défaut 19:00–20:00, en heure LOCALE (dt_util.now() porte la tz
# du serveur HA — HA 2026.8 exige des datetime timezone-aware dans les
# CalendarEvent, des naïfs sont rejetés par le schéma de validation).
_MEAL_START_HOUR = 19
_MEAL_END_HOUR = 20


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: JowManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([JowMenuCalendar(manager, entry)])


class JowMenuCalendar(CalendarEntity):
    """Le menu Jow comme calendrier (un événement par repas planifié)."""

    _attr_has_entity_name = True

    def __init__(self, manager: JowManager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_name = "Menu"
        self._attr_unique_id = f"{entry.entry_id}_menu_calendar"
        instance_name = (
            entry.options.get("name")
            or entry.data.get("name")
            or entry.title
            or "Jow"
        ).strip() or "Jow"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=instance_name,
            manufacturer="Jow (non officiel)",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self._manager.update_signal, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def event(self) -> CalendarEvent | None:
        """Le repas en cours ou à venir le plus proche."""
        now = dt_util.now()
        best: CalendarEvent | None = None
        for ev in self._events_between(now.date(), now.date() + timedelta(days=60)):
            if ev.end > now and (best is None or ev.start < best.start):
                best = ev
        return best

    def _meal_datetime(self, d: date, hour: int) -> datetime:
        """datetime LOCAL timezone-aware pour un jour/heure de repas."""
        local_now = dt_util.now()
        return local_now.replace(
            year=d.year, month=d.month, day=d.day, hour=hour, minute=0, second=0, microsecond=0
        )

    def _events_between(self, start_date: date, end_date: date) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        d = start_date
        while d <= end_date:
            meal = self._manager.get_meal(d)
            if meal:
                desc_parts = []
                ings = meal.get("ingredients") or []
                if ings:
                    desc_parts.append(
                        "Ingrédients : " + ", ".join(
                            i.get("name", "") for i in ings[:10] if i.get("name")
                        )
                    )
                if meal.get("calories"):
                    desc_parts.append(f"{meal['calories']} kcal/pers")
                if meal.get("covers"):
                    desc_parts.append(f"{meal['covers']} couverts")
                events.append(CalendarEvent(
                    start=self._meal_datetime(d, _MEAL_START_HOUR),
                    end=self._meal_datetime(d, _MEAL_END_HOUR),
                    summary=meal.get("name", "Repas Jow"),
                    description="\n".join(desc_parts) or None,
                    uid=f"jow_{d.isoformat()}",
                ))
            d += timedelta(days=1)
        return events

    async def async_get_events(self, hass, start_date, end_date):
        # start_date/end_date arrivent timezone-aware (UTC) : garder la
        # fenêtre en dates locales
        start_local = dt_util.as_local(start_date).date()
        end_local = dt_util.as_local(end_date).date()
        return self._events_between(start_local, end_local)
