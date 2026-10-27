from __future__ import annotations

from typing import Any


def input_devices(sounddevice: Any) -> list[tuple[str, str]]:
    """Return PortAudio input devices as stable-for-session IDs and labels."""
    devices = sounddevice.query_devices()
    return [
        (str(index), str(device["name"]))
        for index, device in enumerate(devices)
        if int(device.get("max_input_channels", 0)) > 0
    ]


def resolve_input_device(sounddevice: Any, selected_id: str) -> int | None:
    """Resolve a saved PortAudio input ID, falling back to the system default."""
    if not selected_id or selected_id == "default":
        return None
    try:
        selected_index = int(selected_id)
        devices = sounddevice.query_devices()
    except (TypeError, ValueError, OSError):
        return None
    if 0 <= selected_index < len(devices):
        if int(devices[selected_index].get("max_input_channels", 0)) > 0:
            return selected_index
    return None


def output_identifier(device: Any) -> str:
    return bytes(device.id()).hex()


def resolve_output_device(media_devices: Any, selected_id: str) -> Any:
    """Resolve a saved device without ever changing the OS-wide default."""
    default_device = media_devices.defaultAudioOutput()
    if not selected_id or selected_id == "default":
        return default_device
    for device in media_devices.audioOutputs():
        if output_identifier(device) == selected_id:
            return device
    return default_device


def apply_output_device(audio_output: Any, media_devices: Any, selected_id: str) -> Any:
    device = resolve_output_device(media_devices, selected_id)
    if device is not None:
        audio_output.setDevice(device)
    return device
