from __future__ import annotations

from typing import Any


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
