"""Capteurs Jow : un par jour de la semaine, plus le repas du jour."""

from __future__ import annotations

from datetime import date

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_change

from .const import DOMAIN, SIGNAL_UPDATE, WEEKDAYS
from .manager import JowManager


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: JowManager = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        JowDaySensor(manager, entry, index) for index in range(7)
    ]
    entities.append(JowTodaySensor(manager, entry))
    async_add_entities(entities)


class JowBaseSensor(SensorEntity):
    """Base commune : rafraîchissement sur signal + à minuit."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: JowManager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Jow",
            manufacturer="Jow (non officiel)",
            entry_type="service",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )
        # Les dates glissent : on recalcule chaque nuit.
        self.async_on_remove(
            async_track_time_change(
                self.hass, self._handle_midnight, hour=0, minute=0, second=10
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_midnight(self, _now) -> None:
        self.async_write_ha_state()

    # -- helpers ------------------------------------------------------
    @property
    def _meal(self) -> dict | None:
        raise NotImplementedError

    @property
    def native_value(self) -> str:
        meal = self._meal
        return meal["name"] if meal else "Rien de prévu"

    @property
    def entity_picture(self) -> str | None:
        """C'est ce qui donne la vignette dans les cartes Lovelace."""
        meal = self._meal
        return meal.get("image") if meal else None

    @property
    def extra_state_attributes(self) -> dict:
        meal = self._meal
        if not meal:
            return {"planned": False}
        return {
            "planned": True,
            "recipe_id": meal.get("id"),
            "url": meal.get("url"),
            "image": meal.get("image"),
            "description": meal.get("description"),
            "preparation_time": meal.get("preparation_time"),
            "cooking_time": meal.get("cooking_time"),
            "covers": meal.get("covers"),
            "calories": meal.get("calories"),
            "ingredients": meal.get("ingredients", []),
        }


class JowDaySensor(JowBaseSensor):
    """Repas planifié pour un jour donné de la semaine en cours."""

    _attr_icon = "mdi:silverware-fork-knife"

    def __init__(self, manager: JowManager, entry: ConfigEntry, index: int) -> None:
        super().__init__(manager, entry)
        self._index = index
        self._attr_name = WEEKDAYS[index].capitalize()
        self._attr_unique_id = f"{entry.entry_id}_{WEEKDAYS[index]}"

    @property
    def _date(self) -> date:
        return self._manager.week_dates()[self._index]

    @property
    def _meal(self) -> dict | None:
        return self._manager.get_meal(self._date)

    @property
    def extra_state_attributes(self) -> dict:
        return {**super().extra_state_attributes, "date": self._date.isoformat()}


class JowTodaySensor(JowBaseSensor):
    """Le repas du jour, pratique pour un dashboard cuisine."""

    _attr_icon = "mdi:chef-hat"

    def __init__(self, manager: JowManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_name = "Repas du jour"
        self._attr_unique_id = f"{entry.entry_id}_today"

    @property
    def _meal(self) -> dict | None:
        return self._manager.get_meal(date.today())
