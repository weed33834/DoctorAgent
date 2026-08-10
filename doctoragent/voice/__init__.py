"""Voice conversation chain (ASR + TTS) support.

Implements the **08-voice** capability from the Universal Agent Builder spec:
end-to-end voice chat where the user speaks, the audio is transcribed (ASR),
routed into the agent, and the reply is spoken back (TTS).

See :class:`~doctoragent.voice.service.VoiceService`.
"""

from __future__ import annotations

from doctoragent.voice.service import (
    VoiceError,
    VoiceService,
    VoiceUnavailable,
)

__all__ = ["VoiceError", "VoiceService", "VoiceUnavailable"]
