from google.genai import types

from atlas.llm import transcription


class _Response:
    def raise_for_status(self):
        pass

    def json(self):
        return {"text": "compare Nvidia and AMD"}


class _Client:
    def __init__(self, captured):
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, **kwargs):
        self.captured.update(url=url, **kwargs)
        return _Response()


async def test_audio_is_sent_to_speech_to_text_and_replaced(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        transcription.httpx, "AsyncClient", lambda timeout: _Client(captured)
    )
    monkeypatch.setattr(transcription.settings, "transcription_api_key", "speech-key")
    voice = types.Part(
        inline_data=types.Blob(mime_type="audio/ogg", data=b"voice-bytes")
    )
    contents = [types.Content(role="user", parts=[voice, types.Part(text="context")])]

    result = await transcription.transcribe_audio(contents)

    assert result[0].parts[0].text.endswith("compare Nvidia and AMD")
    assert result[0].parts[1].text == "context"
    assert captured["headers"]["Authorization"] == "Bearer speech-key"
    assert captured["data"]["model"] == "whisper-large-v3-turbo"
    assert captured["files"]["file"][1:] == (b"voice-bytes", "audio/ogg")
