"""Speech-to-text fallback for voice notes that Gemini could not process."""

import logging
import mimetypes

import httpx
from google.genai import types

from atlas.config import settings

log = logging.getLogger(__name__)


def _audio_filename(mime_type: str) -> str:
    extension = mimetypes.guess_extension(mime_type) or ".audio"
    return f"voice{extension}"


async def transcribe_audio(contents: list[types.Content]) -> list[types.Content]:
    """Replace inline audio parts with transcripts; leave every other part untouched."""
    output: list[types.Content] = []
    transcribed = 0
    api_key = settings.transcription_api_key or settings.fallback_llm_api_key

    async with httpx.AsyncClient(timeout=30) as http:
        for content in contents:
            parts: list[types.Part] = []
            for part in content.parts or []:
                blob = part.inline_data
                mime_type = blob.mime_type if blob else ""
                if not blob or not mime_type.startswith("audio/"):
                    parts.append(part)
                    continue

                response = await http.post(
                    settings.transcription_url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    data={"model": settings.transcription_model, "response_format": "json"},
                    files={
                        "file": (_audio_filename(mime_type), blob.data, mime_type),
                    },
                )
                response.raise_for_status()
                transcript = response.json().get("text", "").strip()
                if not transcript:
                    raise ValueError("speech-to-text returned an empty transcript")
                parts.append(types.Part(text=f"[Voice note transcript]\n{transcript}"))
                transcribed += 1
            output.append(types.Content(role=content.role, parts=parts))
    if transcribed:
        log.warning("transcribed %d voice attachment(s) through the fallback", transcribed)
    return output
