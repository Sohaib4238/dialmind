"""The interface every voice provider (Cartesia, Phonic) implements for calls.py.

calls.py only talks to a provider through these methods. get_call() always returns the same call shape
(the one calls.ingest_call reads), whatever the provider:

    {"id", "status": None | "started" | "completed" | "failed", "end_reason", "error_message",
     "start_time", "end_time", "summary",
     "transcript": [{"role": "assistant" | "user", "text", "start_timestamp", "end_timestamp",
                     "tool_calls": [{"id", "name", "arguments", "result"}]}]}

Tool names in the transcript use our names: "end_call" (the agent hung up), "record_outcome", "collect_email",
"request_supervisor", anything with "transfer" in it, "voicemail" / "voicemail_detection".
"""


class ProviderError(Exception):
    """A provider refused a request or couldn't be reached. status = HTTP status (0 = network / local refusal)."""

    def __init__(self, message: str, status: int = 0, body: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class CallNotConnected(ProviderError):
    """The provider says the call never connected (busy, no answer, rejected by the carrier).
    result = our call result: "busy" / "no_answer" / "failed"."""

    def __init__(self, message: str, result: str, status: int = 0, body: object = None) -> None:
        super().__init__(message, status, body)
        self.result = result


class VoiceProvider:
    name = ""
    label = ""
    # What the provider can do, shown on the settings page. A feature that's missing stays False: never faked.
    features: dict[str, bool | str] = {}

    def owns(self, call_ref: str) -> bool:
        """Is this Call.room an id of a real call at this provider (not "pending-..." / "sim-...")?"""
        raise NotImplementedError

    async def prepare(self) -> dict:
        """Before a call: push the agent settings / tools if they changed. Returns a short status for the call log."""
        raise NotImplementedError

    async def start_call(self, to_number: str, variables: dict, ringing_timeout_s: int, max_minutes: int) -> str:
        """Dial one number. Returns the provider's call id (stored in Call.room)."""
        raise NotImplementedError

    async def get_call(self, call_ref: str) -> dict:
        raise NotImplementedError

    async def end_call(self, call_ref: str) -> dict:
        """Hang up from our side."""
        raise NotImplementedError

    async def recording(self, call_ref: str) -> tuple[bytes, str]:
        raise NotImplementedError
