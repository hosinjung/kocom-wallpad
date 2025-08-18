"""Controller for Kocom Wallpad."""

from __future__ import annotations

from typing import List, Callable, Any, Tuple
from dataclasses import dataclass, replace

from homeassistant.const import Platform
from homeassistant.components.switch import SwitchDeviceClass
from homeassistant.components.climate.const import (
    PRESET_NONE,
    PRESET_AWAY,
    HVACMode,
)

from .const import (
    LOGGER,
    PACKET_PREFIX,
    PACKET_SUFFIX,
    PACKET_LEN,
    CMD_CONFIRM_TIMEOUT,
    DeviceType,
    SubType,
)
from .models import (
    DEVICE_TYPE_MAP,
    VENTILATION_PRESET_MAP,
    ELEVATOR_DIRECTION_MAP,
    DeviceKey,
    DeviceState
)

Predicate = Callable[[DeviceState], bool]

REV_DT_MAP = {v: k for k, v in DEVICE_TYPE_MAP.items()}
REV_VENT_PRESET_MAP = {v: k for k, v in VENTILATION_PRESET_MAP.items()}


@dataclass(slots=True, frozen=True)
class PacketFrame:
    """Packet frame."""
    raw: bytes

    @property
    def packet_type(self) -> int:
        return (self.raw[3] >> 4) & 0x0F

    @property
    def dest(self) -> bytes:
        return self.raw[5:7]

    @property
    def src(self) -> bytes:
        return self.raw[7:9]

    @property
    def command(self) -> int:
        return self.raw[9]

    @property
    def payload(self) -> bytes:
        return self.raw[10:18]

    @property
    def checksum(self) -> int:
        return self.raw[18]

    @property
    def peer(self) -> tuple[int | None, int | None]:
        if self.dest[0] == 0x01:
            return (self.src[0], self.src[1])
        elif self.src[0] == 0x01:
            return (self.dest[0], self.dest[1])
        else:
            LOGGER.warning("Peer resolution failed: dest=%s, src=%s", self.dest.hex(), self.src.hex())
            return (None, None)

    @property
    def dev_type(self) -> DeviceType:
        dev_type = DEVICE_TYPE_MAP.get(self.peer[0], None)
        if dev_type is None:
            LOGGER.debug("Unknown device type code=%s, raw=%s", hex(self.peer[0]), self.raw.hex())
            dev_type = DeviceType.UNKNOWN
        return dev_type

    @property
    def dev_room(self) -> int | None:
        return self.peer[1]


class KocomController:
    """Controller for Kocom Wallpad."""

    def __init__(self, gateway) -> None:
        """Initialize the controller."""
        self.gateway = gateway
        self._rx_buf = bytearray()
        self._device_storage: dict[str, Any] = {}

    @staticmethod
    def _checksum(buf: bytes) -> int:
        return sum(buf) % 256

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._rx_buf.extend(chunk)
        for pkt in self._split_buf():
            LOGGER.debug("Packet received: raw=%s", pkt.hex())
            self._dispatch_packet(pkt)

    def _split_buf(self) -> List[bytes]:
        packets: List[bytes] = []
        buf = self._rx_buf
        while True:
            start = buf.find(PACKET_PREFIX)
            if start < 0:
                # 프리픽스 이전의 쓰레기 데이터 제거
                buf.clear()
                break
            if start > 0:
                del buf[:start]
            if len(buf) < PACKET_LEN:
                # 더 받을 때까지 대기
                break
            # 고정 길이 확인 후 서픽스 검사
            candidate = bytes(buf[:PACKET_LEN])
            if not candidate.endswith(PACKET_SUFFIX):
                # 한 바이트 밀어서 재탐색 (프레이밍 어긋남 복구)
                del buf[0]
                continue
            packets.append(candidate)
            del buf[:PACKET_LEN]
        return packets

    def _dispatch_packet(self, packet: bytes) -> None:
        frame = PacketFrame(packet)
        if self._checksum(packet[2:18]) != frame.checksum:
            LOGGER.debug("Packet checksum is invalid. raw=%s", frame.raw.hex())
            return

        dev_state = None
        if frame.dev_type == DeviceType.LIGHT:
            dev_state = self._handle_switch(frame)
        elif frame.dev_type == DeviceType.OUTLET:
            dev_state = self._handle_switch(frame)
        elif frame.dev_type == DeviceType.THERMOSTAT:
            dev_state = self._handle_thermostat(frame)
        elif frame.dev_type == DeviceType.VENTILATION:
            dev_state = self._handle_ventilation(frame)
        elif frame.dev_type == DeviceType.GASVALVE:
            dev_state = self._handle_gasvalve(frame)
        elif frame.dev_type == DeviceType.ELEVATOR:
            dev_state = self._handle_elevator(frame)
        else:
            LOGGER.debug("Unhandled device type: %s (raw=%s)", frame.dev_type.name, frame.raw.hex())
            return

        if not dev_state:
            return

        if isinstance(dev_state, list):
            for state in dev_state:
                state._packet = packet
                self.gateway.on_device_state(state)
        else:
            dev_state._packet = packet
            self.gateway.on_device_state(dev_state)

    def _handle_switch(self, frame: PacketFrame) -> List[DeviceState]:
        states: List[DeviceState] = []
        if frame.command == 0x00:
            for idx in range(8):
                key = DeviceKey(
                    device_type=frame.dev_type,
                    room_index=frame.dev_room,
                    device_index=idx,
                    sub_type=SubType.NONE,
                )
                platform = Platform.LIGHT if frame.dev_type == DeviceType.LIGHT else Platform.SWITCH      
                attribute = {}
                if platform == Platform.SWITCH:
                    attribute = {"device_class": SwitchDeviceClass.OUTLET}
                state = frame.payload[idx] == 0xFF        
                dev = DeviceState(key=key, platform=platform, attribute=attribute, state=state)
                if state:
                    dev._is_register = True
                else:
                    dev._is_register = False
                states.append(dev)
            return states

    def _handle_thermostat(self, frame: PacketFrame) -> DeviceState:
        if frame.command == 0x00:
            key = DeviceKey(
                device_type=frame.dev_type,
                room_index=frame.dev_room,
                device_index=0,
                sub_type=SubType.NONE,
            )
            havc_mode = HVACMode.HEAT if frame.payload[0] >> 4 == 0x01 else HVACMode.OFF
            preset_mode = PRESET_AWAY if frame.payload[1] & 0x0F == 0x01 else PRESET_NONE
            target_temp = float(frame.payload[2])
            current_temp = float(frame.payload[4])

            attribute = {
                "hvac_modes": [HVACMode.HEAT, HVACMode.OFF],
                "feature_preset": True,
                "preset_modes": [PRESET_AWAY, PRESET_NONE],
                "temp_step": self._device_storage.get("temp_step", 1.0),
            }
            state = {
                "hvac_mode": havc_mode,
                "preset_mode": preset_mode,
                "target_temp": self._device_storage.get("target_temp", target_temp),
                "current_temp": self._device_storage.get("current_temp", current_temp),
            }
            if target_temp % 1 == 0.5 and self._device_storage.get("temp_step") != 0.5:
                LOGGER.debug("0.5°C step detected, heating supports 0.5 increments.")
                self._device_storage["temp_step"] = 0.5
            if target_temp != 0 and current_temp != 0:
                self._device_storage["target_temp"] = target_temp
                self._device_storage["current_temp"] = current_temp
            dev = DeviceState(key=key, platform=Platform.CLIMATE, attribute=attribute, state=state)
            return dev

    def _handle_ventilation(self, frame: PacketFrame) -> DeviceState:
        if frame.command == 0x00:
            key = DeviceKey(
                device_type=frame.dev_type,
                room_index=frame.dev_room,
                device_index=0,
                sub_type=SubType.NONE,
            )
            state = frame.payload[0] >> 4 == 0x01
            preset_mode = VENTILATION_PRESET_MAP.get(frame.payload[1], "unknown")
            speed = frame.payload[2]
            
            attribute = {
                "feature_preset": self._device_storage.get("feature_preset", False),
                "preset_modes": self._device_storage.get("preset_modes", []),
                "speed_list": [0x40, 0x80, 0xC0]
            }
            state = {
                "state": state,
                "preset_mode": preset_mode,
                "speed": speed,
            }
            if preset_mode != "unknown" and preset_mode != "ventilation":
                if self._device_storage.get("preset_modes") is None:
                    LOGGER.debug("New ventilation preset detected (excluding default).")
                    self._device_storage["feature_preset"] = True
                    self._device_storage["preset_modes"] = ["ventilation"]
                if preset_mode not in self._device_storage["preset_modes"]:
                    LOGGER.debug(f"Added presets: {preset_mode}")
                    self._device_storage["preset_modes"].append(preset_mode)
            dev = DeviceState(key=key, platform=Platform.FAN, attribute=attribute, state=state)
            return dev

    def _handle_gasvalve(self, frame: PacketFrame) -> DeviceState:
        if frame.command in (0x01, 0x02):
            key = DeviceKey(
                device_type=frame.dev_type,
                room_index=frame.dev_room,
                device_index=0,
                sub_type=SubType.NONE,
            )
            state = frame.command == 0x01
            dev = DeviceState(key=key, platform=Platform.SWITCH, attribute={}, state=state)
            return dev

    def _handle_elevator(self, frame: PacketFrame) -> List[DeviceState]:    
        states: List[DeviceState] = []
        key = DeviceKey(
            device_type=frame.dev_type,
            room_index=frame.dev_room,
            device_index=0,
            sub_type=SubType.NONE,
        )
        state = False
        if frame.payload[0] == 0x03:
            state = False
        elif frame.payload[0] in (0x01, 0x02) or frame.packet_type == 0x0D:
            state = True
        dev = DeviceState(key=key, platform=Platform.SWITCH, attribute={}, state=state)
        states.append(dev)

        key = DeviceKey(
            device_type=frame.dev_type,
            room_index=frame.dev_room,
            device_index=0,
            sub_type=SubType.DIRECTION,
        )
        state = ""
        if frame.payload[0] == 0x00 and frame.packet_type == 0x0D:
            state = "called"
        else:
            state = ELEVATOR_DIRECTION_MAP.get(frame.payload[0], "unknown")
        dev = DeviceState(key=key, platform=Platform.SENSOR, attribute={}, state=state)
        states.append(dev)
        
        key = DeviceKey(
            device_type=frame.dev_type,
            room_index=frame.dev_room,
            device_index=0,
            sub_type=SubType.FLOOR,
        )
        state = ""
        if frame.payload[1] == 0x00:
            state = "unknown"
        else:
            if frame.payload[2] != 0x00:
                state = f"{chr(frame.payload[1])}{chr(frame.payload[2])}"
            else:
                if frame.payload[1] >> 4 == 0x08:
                    state = f"B{str(frame.payload[1] & 0x0F)}"
                else:
                    state = str(frame.payload[1])
        dev = DeviceState(key=key, platform=Platform.SENSOR, attribute={}, state=state)
        # Except for wallpad that do not support floor
        if dev.state != "":
            states.append(dev)
        return states
    
    # TODO: 명령 상태 비교 로직 통합 (gateway.py)
    def _match_key_and(self, key: DeviceKey, cond: Predicate) -> Predicate:
        def _inner(dev: DeviceState) -> bool:
            if dev.key.key != key.key:
                return False
            return cond(dev)
        return _inner

    def _expect_for_switch_like(self, key: DeviceKey, action: str, **kwargs: Any) -> Tuple[Predicate, float]:
        def _on(dev: DeviceState) -> bool:  return bool(dev.state) is True
        def _off(dev: DeviceState) -> bool: return bool(dev.state) is False

        if action == "turn_on":
            return self._match_key_and(key, _on), CMD_CONFIRM_TIMEOUT
        if action == "turn_off":
            return self._match_key_and(key, _off), CMD_CONFIRM_TIMEOUT
        return self._match_key_and(key, lambda _d: False), CMD_CONFIRM_TIMEOUT

    def _expect_for_ventilation(self, key: DeviceKey, action: str, **kwargs: Any) -> Tuple[Predicate, float]:
        def is_on(d: DeviceState) -> bool:
            return isinstance(d.state, dict) and d.state.get("state") is True
        def is_off(d: DeviceState) -> bool:
            return isinstance(d.state, dict) and d.state.get("state") is False

        if action == "turn_on":
            return self._match_key_and(key, is_on), CMD_CONFIRM_TIMEOUT
        if action == "turn_off":
            return self._match_key_and(key, is_off), CMD_CONFIRM_TIMEOUT

        if action == "set_preset":
            pm = kwargs["preset_mode"]
            return self._match_key_and(key, lambda d: isinstance(d.state, dict) and d.state.get("preset_mode") == pm), CMD_CONFIRM_TIMEOUT
        if action == "set_percentage":
            speed = kwargs["speed"]
            return self._match_key_and(key, lambda d: isinstance(d.state, dict) and d.state.get("speed") == speed), CMD_CONFIRM_TIMEOUT

        return self._match_key_and(key, lambda _d: False), CMD_CONFIRM_TIMEOUT

    def _expect_for_gasvalve(self, key: DeviceKey, action: str, **kwargs: Any) -> Tuple[Predicate, float]:
        # 밸브는 동작이 느릴 수 있으니 기본 타임아웃 상향
        base_timeout = max(CMD_CONFIRM_TIMEOUT, 1.5)
        if action == "turn_on":
            return True, base_timeout
        if action == "turn_off":
            return self._match_key_and(key, lambda d: bool(d.state) is False), base_timeout
        return self._match_key_and(key, lambda _d: False), base_timeout

    def _expect_for_thermostat(self, key: DeviceKey, action: str, **kwargs: Any) -> Tuple[Predicate, float]:
        if action == "set_hvac":
            hm = kwargs["hvac_mode"]
            return self._match_key_and(key, lambda d: isinstance(d.state, dict) and d.state.get("hvac_mode") == hm), CMD_CONFIRM_TIMEOUT
        if action == "set_preset":
            pm = kwargs["preset_mode"]
            return self._match_key_and(key, lambda d: isinstance(d.state, dict) and d.state.get("preset_mode") == pm), CMD_CONFIRM_TIMEOUT
        if action == "set_temperature":
            tt = kwargs["target_temp"]
            return self._match_key_and(key, lambda d: isinstance(d.state, dict) and d.state.get("target_temp") == tt), CMD_CONFIRM_TIMEOUT
        if action == "turn_on":
            return self._match_key_and(key, lambda d: isinstance(d.state, dict) and d.state.get("state") is True), CMD_CONFIRM_TIMEOUT
        if action == "turn_off":
            return self._match_key_and(key, lambda d: isinstance(d.state, dict) and d.state.get("state") is False), CMD_CONFIRM_TIMEOUT
        return self._match_key_and(key, lambda _d: False), CMD_CONFIRM_TIMEOUT

    def build_expectation(self, key: DeviceKey, action: str, **kwargs: Any) -> Tuple[Predicate, float]:
        dt = key.device_type
        if dt in (DeviceType.LIGHT, DeviceType.OUTLET):
            return self._expect_for_switch_like(key, action, **kwargs)
        if dt == DeviceType.VENTILATION:
            return self._expect_for_ventilation(key, action, **kwargs)
        if dt == DeviceType.GASVALVE:
            return self._expect_for_gasvalve(key, action, **kwargs)
        if dt == DeviceType.THERMOSTAT:
            return self._expect_for_thermostat(key, action, **kwargs)
        return self._match_key_and(key, lambda _d: False), CMD_CONFIRM_TIMEOUT

    def generate_command(self, key: DeviceKey, action: str, **kwargs) -> Tuple[bytes, Predicate, float]:
        device_type = key.device_type
        room_index = key.room_index
        device_index = key.device_index
        sub_type = key.sub_type

        if device_type not in REV_DT_MAP:
            raise ValueError(f"Invalid device type: {device_type}")

        type_bytes = bytes([0x30, 0xBC])
        padding = bytes([0x00])
        dest_dev = bytes([REV_DT_MAP[device_type]])
        dest_room = bytes([room_index & 0xFF])
        src_dev = bytes([0x01])
        src_room = bytes([0x00])
        command = bytes([0x00])
        data = bytearray(8)

        if device_type in (DeviceType.LIGHT, DeviceType.OUTLET):
            data = self._generate_switch(key, action, data)
        elif device_type == DeviceType.VENTILATION:
            data = self._generate_ventilation(action, data, **kwargs)
        elif device_type == DeviceType.THERMOSTAT:
            data = self._generate_thermostat(action, data, **kwargs)
        elif device_type == DeviceType.GASVALVE:
            command = bytes([0x02])
        elif device_type == DeviceType.ELEVATOR:
            dest_dev = bytes([0x01])
            dest_room = bytes([0x00])
            src_dev = bytes([0x44])
            src_room = bytes([room_index & 0xFF])
            command = bytes([0x01])
        else:
            raise ValueError(f"Invalid device generator: {device_type}")

        body = b"".join([type_bytes, padding, dest_dev, dest_room, src_dev, src_room, command, bytes(data)])
        checksum = bytes([self._checksum(body)])
        packet = bytes([0xAA, 0x55]) + body + checksum + bytes([0x0D, 0x0D])

        expect, timeout = self.build_expectation(key, action, **kwargs)
        return packet, expect, timeout

    def _generate_switch(self, key: DeviceKey, action: str, data: bytes) -> bytes:
        for idx in range(8):
            new_key = replace(key, device_index=idx)
            st = self.gateway.registry.get(new_key)
            if idx != key.device_index:
                bit = 0xFF if (st and st.state is True) else 0x00
                data[idx] = bit
            else:
                data[idx] = 0xFF if action == "turn_on" else 0x00
        return data

    def _generate_ventilation(self, action: str, data: bytes, **kwargs: Any) -> bytes:
        if action == "set_preset":
            pm = kwargs["preset_mode"]
            data[0] = 0x11
            data[1] = REV_VENT_PRESET_MAP[pm]
        elif action == "set_percentage":
            speed = kwargs["speed"]
            data[0] = 0x00 if speed == 0 else 0x11
            data[2] = speed
        else:
            data[0] = 0x11 if action == "turn_on" else 0x00
        return data
    
    def _generate_thermostat(self, action: str, data: bytes, **kwargs: Any) -> bytes:
        if action == "set_hvac":
            hm = kwargs["hvac_mode"]
            data[0] = 0x11 if hm == HVACMode.HEAT else 0x01
            data[1] = 0x00
        elif action == "set_preset":
            pm = kwargs["preset_mode"]
            data[0] = 0x11
            data[1] = 0x01 if pm == PRESET_AWAY else 0x00
        elif action == "set_temperature":
            tt = kwargs["target_temp"]
            data[2] = int(tt)
        return data
