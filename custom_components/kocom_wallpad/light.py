from __future__ import annotations

from typing import Any, List

from homeassistant.components.light import LightEntity, ColorMode, ATTR_BRIGHTNESS

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .gateway import KocomGateway, DeviceState
from .entity_base import KocomBaseEntity
from .const import DOMAIN, LOGGER


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry, 
    async_add_entities: AddEntitiesCallback
) -> bool:
    gateway: KocomGateway = hass.data[DOMAIN][entry.entry_id]

    @callback
    def async_add_light(devices=None):
        if devices is None:
            devices = gateway.get_devices_from_platform(Platform.LIGHT)
            
        entities: List[KocomLight] = []
        for dev in devices:
            ent = KocomLight(gateway, dev)
            entities.append(ent)
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, gateway.async_signal_new_device(Platform.LIGHT), async_add_light
        )
    )
    async_add_light()


class KocomLight(KocomBaseEntity, LightEntity):
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(
        self, gateway: KocomGateway, device: DeviceState
    ) -> None:
        super().__init__(gateway, device)

    @property
    def is_on(self) -> bool:
        return self._state_cache.get("state", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.gateway.async_send_action(self._device.key, "turn_on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.gateway.async_send_action(self._device.key, "turn_off")
