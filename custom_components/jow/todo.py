"""Liste de courses Jow, exposée comme une vraie liste de tâches Home Assistant."""

from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATE
from .manager import JowManager


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    manager: JowManager = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([JowShoppingList(manager, entry), JowApprovedList(manager, entry)])


class JowShoppingList(TodoListEntity):
    """Liste de courses agrégée depuis les recettes de la semaine."""

    _attr_has_entity_name = True
    _attr_name = "Courses"
    _attr_icon = "mdi:cart"
    _attr_should_poll = False
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, manager: JowManager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_shopping"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        self._update_todo_items()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self._update_todo_items()
        self.async_write_ha_state()

    def _update_todo_items(self) -> None:
        """Met à jour _attr_todo_items depuis le manager."""
        self._attr_todo_items = [
            TodoItem(
                uid=item["uid"],
                summary=item["summary"],
                status=TodoItemStatus.COMPLETED
                if item.get("done")
                else TodoItemStatus.NEEDS_ACTION,
            )
            for item in self._manager.shopping
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await self._manager.async_add_item(item.summary or "")

    async def async_update_todo_item(self, item: TodoItem) -> None:
        await self._manager.async_update_item(
            item.uid,
            item.summary,
            item.status == TodoItemStatus.COMPLETED if item.status else None,
        )

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        await self._manager.async_remove_items(uids)


class JowApprovedList(TodoListEntity):
    """Articles à toujours acheter (hors planning), fusionnés avec la liste auto."""

    _attr_has_entity_name = True
    _attr_name = "Liste approuvée"
    _attr_icon = "mdi:clipboard-list-outline"
    _attr_should_poll = False
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
    )

    def __init__(self, manager: JowManager, entry: ConfigEntry) -> None:
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_approved"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_added_to_hass(self) -> None:
        self._update_todo_items()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self._handle_update)
        )

    @callback
    def _handle_update(self) -> None:
        self._update_todo_items()
        self.async_write_ha_state()

    def _update_todo_items(self) -> None:
        """Met à jour _attr_todo_items depuis le manager."""
        self._attr_todo_items = [
            TodoItem(
                uid=item["uid"],
                summary=item["summary"],
                status=TodoItemStatus.COMPLETED
                if item.get("done")
                else TodoItemStatus.NEEDS_ACTION,
            )
            for item in self._manager.approved
        ]

    async def async_create_todo_item(self, item: TodoItem) -> None:
        await self._manager.async_add_approved(item.summary or "")

    async def async_update_todo_item(self, item: TodoItem) -> None:
        await self._manager.async_update_approved(
            item.uid,
            item.summary,
            item.status == TodoItemStatus.COMPLETED if item.status else None,
        )

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        await self._manager.async_remove_approved(uids)
