from google.genai import types
import pytest

from atlas.agent.drive_guard import required_drive_args
from atlas.agent.loop import run_turn
from atlas.db.models import User
from atlas.llm import provider


def _turn(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part(text=text)])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("check my drive for a file named Signature", {"query": "Signature", "kind": "any", "max": 10, "include_content": False, "exact_name": True}),
        ("list files in my drive", {"query": "", "kind": "any", "max": 25, "include_content": False, "exact_name": False}),
        ("what folders are in my drive?", {"query": "", "kind": "folder", "max": 25, "include_content": False, "exact_name": False}),
        ("find an image of my signture in my drive", {"query": "signture", "kind": "image", "max": 10, "include_content": False, "exact_name": False}),
        ("is there a folder called mec", {"query": "mec", "kind": "folder", "max": 10, "include_content": False, "exact_name": True}),
        ("find files mentioning Nvidia in my drive", {"query": "Nvidia", "kind": "any", "max": 10, "include_content": True, "exact_name": False}),
    ],
)
def test_required_drive_args(text, expected):
    assert required_drive_args(_turn(text)) == expected


def test_financial_use_of_drive_is_not_mistaken_for_google_drive():
    assert required_drive_args(_turn("What drives Nvidia's datacenter revenue?")) is None


@pytest.mark.asyncio
async def test_drive_request_forces_fetch_and_uses_api_rows(monkeypatch):
    stream_results = iter([
        provider.Result(text="[Signature](https://docs.google.com/document/d/fake/edit)"),
        provider.Result(text="I found it."),
    ])
    dispatched = []

    async def system_instruction(*args, **kwargs):
        return "system"

    async def stream(*args, **kwargs):
        return next(stream_results)

    async def dispatch(name, args, ctx):
        dispatched.append((name, args))
        return ({"data": [{
            "name": "SIGNATURE.jpeg",
            "mimeType": "image/jpeg",
            "web_url": "https://drive.google.com/file/d/real/view",
        }], "source": "Google Drive"}, 12, True)

    monkeypatch.setattr("atlas.agent.loop.context_builder.system_instruction", system_instruction)
    monkeypatch.setattr("atlas.agent.loop.provider.stream", stream)
    monkeypatch.setattr("atlas.agent.loop.registry.dispatch", dispatch)

    result = await run_turn(User(id="u", timezone="UTC"), _turn("find Signature in my drive"), history=[])

    assert dispatched == [("search_drive", {
        "query": "Signature", "kind": "any", "max": 10,
        "include_content": False, "exact_name": False,
    })]
    assert "https://drive.google.com/file/d/real/view" in result.text
    assert "/fake/" not in result.text
    assert result.tool_calls[0]["result_count"] == 1


@pytest.mark.asyncio
async def test_empty_drive_result_replaces_invented_link(monkeypatch):
    stream_results = iter([
        provider.Result(text="[Made up](https://docs.google.com/document/d/fake/edit)"),
        provider.Result(text="[Still made up](https://docs.google.com/document/d/fake/edit)"),
    ])

    async def system_instruction(*args, **kwargs):
        return "system"

    async def stream(*args, **kwargs):
        return next(stream_results)

    async def dispatch(name, args, ctx):
        return ({"data": [], "source": "Google Drive"}, 8, True)

    monkeypatch.setattr("atlas.agent.loop.context_builder.system_instruction", system_instruction)
    monkeypatch.setattr("atlas.agent.loop.provider.stream", stream)
    monkeypatch.setattr("atlas.agent.loop.registry.dispatch", dispatch)

    result = await run_turn(User(id="u", timezone="UTC"), _turn("list files in my drive"), history=[])

    assert result.text == "📁 **0 files found**"
    assert result.tool_calls[0]["result_count"] == 0


@pytest.mark.asyncio
async def test_disconnected_drive_result_uses_only_real_connection_link(monkeypatch):
    stream_results = iter([
        provider.Result(text="[Fake](https://docs.google.com/document/d/fake/edit)"),
        provider.Result(text="Try this fake link."),
    ])

    async def system_instruction(*args, **kwargs):
        return "system"

    async def stream(*args, **kwargs):
        return next(stream_results)

    async def dispatch(name, args, ctx):
        return ({"error": "not_connected", "link": "https://atlas.example/oauth/google/start?state=real"}, 8, False)

    monkeypatch.setattr("atlas.agent.loop.context_builder.system_instruction", system_instruction)
    monkeypatch.setattr("atlas.agent.loop.provider.stream", stream)
    monkeypatch.setattr("atlas.agent.loop.registry.dispatch", dispatch)

    result = await run_turn(User(id="u", timezone="UTC"), _turn("list files in my drive"), history=[])

    assert result.text.startswith("Google Drive needs to be connected")
    assert "state=real" in result.text
    assert "/fake/" not in result.text


@pytest.mark.asyncio
async def test_drive_search_retries_misspelling_against_filenames(monkeypatch):
    from atlas.services import google

    calls = []

    async def get_json(url, token, what, params=None):
        calls.append(params)
        if len(calls) == 1:
            return {"files": []}
        return {"files": [
            {"name": "SIGNATURE.jpeg", "mimeType": "image/jpeg", "webViewLink": "https://drive.google.com/real"},
            {"name": "Quarterly report.pdf", "mimeType": "application/pdf"},
        ]}

    monkeypatch.setattr(google, "_get_json", get_json)

    result = await google.drive_search("token", "signture", 10, "image")

    assert [item["name"] for item in result] == ["SIGNATURE.jpeg"]
    assert calls[0]["q"] == "trashed = false and name contains 'signture' and mimeType contains 'image/'"
    assert calls[1]["q"] == "trashed = false and mimeType contains 'image/'"


@pytest.mark.asyncio
async def test_drive_search_only_uses_full_text_when_explicitly_requested(monkeypatch):
    from atlas.services import google

    calls = []

    async def get_json(url, token, what, params=None):
        calls.append(params)
        return {"files": [{"name": "Board notes"}]}

    monkeypatch.setattr(google, "_get_json", get_json)

    await google.drive_search("token", "signature")
    await google.drive_search("token", "signature", include_content=True)

    assert calls[0]["q"] == "trashed = false and name contains 'signature'"
    assert "fullText contains 'signature'" in calls[1]["q"]


@pytest.mark.asyncio
async def test_exact_folder_name_filters_contains_matches(monkeypatch):
    from atlas.services import google

    async def get_json(url, token, what, params=None):
        return {"files": [
            {"name": "MEC", "mimeType": "application/vnd.google-apps.folder"},
            {"name": "MEC DOC", "mimeType": "application/vnd.google-apps.folder"},
        ]}

    monkeypatch.setattr(google, "_get_json", get_json)

    result = await google.drive_search("token", "mec", kind="folder", exact_name=True)

    assert [item["name"] for item in result] == ["MEC"]
