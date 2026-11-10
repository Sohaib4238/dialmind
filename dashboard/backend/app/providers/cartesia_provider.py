"""Cartesia behind the provider interface: a thin wrapper over app/cartesia.py and app/cartesia_setup.py.
Nothing here changes how Cartesia calls work; calls.py used to call these functions directly."""

import os

from app import cartesia
from app.providers.base import ProviderError, VoiceProvider


class CartesiaProvider(VoiceProvider):
    name = "cartesia"
    label = "Cartesia"
    features = {
        "voicemail_detection": True,  # Cartesia's voicemail tool + our transcript check
        "voice_speed": True,
        "background_sound": True,  # our own recordings (Voice page)
        "transfer": "SIP REFER (cold transfer) to the supervisor who accepted",
        "summary": True,
        "webhooks": True,
    }

    def owns(self, call_ref: str) -> bool:
        return (call_ref or "").startswith("ac_")

    async def prepare(self) -> dict:
        from app import cartesia_setup

        if not os.getenv("CARTESIA_AGENT_ID", "") or not os.getenv("CARTESIA_FROM_NUMBER_ID", ""):
            raise ProviderError("Cartesia isn't set up yet: run python dashboard\\backend\\setup_cartesia.py")
        try:
            return await cartesia_setup.sync_agent("before a call")  # pushes agent_settings.yaml if it changed
        except cartesia.CartesiaError as e:
            raise ProviderError(f"Couldn't update the Cartesia agent: {e}", e.status, e.body) from None

    async def start_call(self, to_number: str, variables: dict, ringing_timeout_s: int, max_minutes: int) -> str:
        return await cartesia.create_call(os.getenv("CARTESIA_AGENT_ID", ""), os.getenv("CARTESIA_FROM_NUMBER_ID", ""),
                                          to_number, variables, ringing_timeout_s, max_minutes)

    async def get_call(self, call_ref: str) -> dict:
        return await cartesia.get_call(call_ref)

    async def end_call(self, call_ref: str) -> dict:
        return await cartesia.cancel_call(call_ref)

    async def recording(self, call_ref: str) -> tuple[bytes, str]:
        return await cartesia.call_audio(call_ref)
