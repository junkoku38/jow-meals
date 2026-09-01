"""Capteurs d'état Jow : synchro, compte, panier, défis.

Capteurs d'état pour les automatisations et alertes :
- sensor.jow_synchro : santé de la connexion (ok / token_expiré /
  sans_compte) + dates et compteurs des dernières synchros menu +
  divergence HA↔Jow (plats HA absents de la liste ouverte Jow)
- sensor.jow_compte : connexion, allergies/préférences, agent IA
- sensor.jow_plats_dans_jow : plats réels de la liste ouverte jow.fr

Enregistrés par sensor.py (import), réveillés par le signal du manager
et à minuit.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_change

from .const import DOMAIN
from .manager import JowManager

_LOGGER = logging.getLogger(__name__)



class _JowStateSensorBase(SensorEntity):
    """Base commune : signal + minuit + device info."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # état recalculé à la demande : pas de state_class (valeurs textuelles)

    def __init__(self, manager: JowManager, entry: ConfigEntry) -> None:
        self._manager = manager
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
        self.async_on_remove(
            async_track_time_change(self.hass, self._handle_update, hour=0, minute=0, second=20)
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class JowSyncSensor(_JowStateSensorBase):
    """Santé de la synchro HA ↔ jow.fr — la base des alertes."""

    _attr_icon = "mdi:sync"

    def __init__(self, manager: JowManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_name = "Synchro"
        self._attr_unique_id = f"{entry.entry_id}_synchro"

    @property
    def native_value(self) -> str:
        if not self._manager.jow_token and not self._manager.jow_refresh_token:
            return "sans_compte"
        if not self._manager.jow_token:
            return "token_expiré"
        return "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._manager
        # divergence réelle : plats du plan HA absents de la liste ouverte Jow
        # (le cache est rempli par les synchros ; s'il est vide, la divergence
        # n'est pas calculable et vaut None)
        plan_ids = {
            meal.get("id") for meal in m.plan.values()
            if isinstance(meal, dict) and meal.get("id")
        }
        if m.jow_open_meals:
            jow_ids = {
                (mm.get("recipe") or {}).get("id") or (mm.get("recipe") or {}).get("_id")
                for mm in m.jow_open_meals
            }
            divergence = len(plan_ids - jow_ids)
            plats_jow = len(jow_ids)
        else:
            divergence = None
            plats_jow = None
        return {
            "plats_planifies": len(m.plan),
            "plats_dans_jow": plats_jow,
            "divergence_ha_vers_jow": divergence,
            "dernier_import": m.last_import,
            "dernier_envoi": m.last_send,
            "rejets_memorises": len(m.rejected),
            "ingredients_interdits": len(m.banned_ingredients),
            "ingredients_a_eviter": len(m.avoid_ingredients),
            "items_courses": len(m.shopping),
            "items_approuves": len(m.approved),
            "anti_repetition_jours": 60,
        }


class JowAccountSensor(_JowStateSensorBase):
    """Compte Jow connecté (profil) — matières à notifications."""

    _attr_icon = "mdi:account"

    def __init__(self, manager: JowManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_name = "Compte"
        self._attr_unique_id = f"{entry.entry_id}_compte"

    @property
    def native_value(self) -> str:
        if self._manager.jow_token:
            return "connecté"
        if self._manager.jow_refresh_token:
            return "à rafraîchir"
        return "non configuré"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._manager
        return {
            "allergies": m.allergies or None,
            "preferences": m.preferences or None,
            "agent_ia": m.ai_entity or None,
            "entite_meteo": m.weather_entity or None,
            "couverts_defaut": m.default_covers,
        }


class JowCartSensor(_JowStateSensorBase):
    """Plats de la liste ouverte jow.fr (le menu du compte).

    Cache rempli par les services de synchro (import_menu / send_menu /
    meal_done) — le capteur reflète l'état réel côté jow.fr, et retombe
    sur le plan HA tant qu'aucune synchro n'a eu lieu.
    """

    _attr_icon = "mdi:cart-outline"

    def __init__(self, manager: JowManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry)
        self._attr_name = "Plats dans Jow"
        self._attr_unique_id = f"{entry.entry_id}_panier"

    @property
    def native_value(self) -> int | str:
        if not self._manager.is_authenticated:
            return "indisponible"
        if self._manager.jow_open_meals:
            return len(self._manager.jow_open_meals)
        # aucune synchro encore : le plan HA est le meilleur reflet
        return len(self._manager.plan)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        meals = self._manager.jow_open_meals or []
        return {
            "source": "jow.fr (synchro)" if meals else "plan HA (aucune synchro récente)",
            "plats": [
                (m.get("recipe") or {}).get("title") or (m.get("recipe") or {}).get("name")
                for m in meals[:20]
            ],
        }
