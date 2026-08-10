"""The fallback has no tools. It must carry real tool results across, and refuse to
invent when there are none — observed fabricating "Bloomberg" citations before this.
"""

from google.genai import types

from atlas.llm.provider import FALLBACK_NO_DATA, FALLBACK_WITH_DATA, _to_messages


def _msg(role: str, *parts: types.Part) -> types.Content:
    return types.Content(role=role, parts=list(parts))


def test_no_tool_results_forbids_figures():
    msgs = _to_messages("sys", [_msg("user", types.Part(text="how's Nvidia trading"))])
    assert msgs[0]["content"].endswith(FALLBACK_NO_DATA)
    assert msgs[-1]["content"] == "how's Nvidia trading"


def test_tool_results_are_carried_across():
    contents = [
        _msg("user", types.Part(text="how's Nvidia trading")),
        _msg("model", types.Part(function_call=types.FunctionCall(name="get_quote", args={}))),
        _msg(
            "user",
            types.Part(
                function_response=types.FunctionResponse(
                    name="get_quote", response={"data": {"price": 180.1}}
                )
            ),
        ),
    ]
    msgs = _to_messages("sys", contents)
    assert msgs[0]["content"].endswith(FALLBACK_WITH_DATA)
    assert "180.1" in msgs[-1]["content"]
    assert msgs[-1]["role"] == "user"


def test_trailing_model_turn_gets_a_user_nudge():
    msgs = _to_messages("", [_msg("model", types.Part(text="hi"))])
    assert msgs[-1] == {"role": "user", "content": "(continue)"}


def test_model_turn_replays_the_thought_signature():
    """Gemini 2.5 stamps function calls with a signature and wants it back untouched."""
    from atlas.agent.loop import _model_turn
    from atlas.llm.provider import Result

    call = types.FunctionCall(name="get_quote", args={"symbol": "NVDA"})
    part = types.Part(function_call=call, thought_signature=b"sig-abc")
    turn = _model_turn(Result(text="", function_calls=[call], call_parts=[part]))

    assert turn.parts[0].thought_signature == b"sig-abc"


def test_model_turn_still_works_without_parts():
    from atlas.agent.loop import _model_turn
    from atlas.llm.provider import Result

    call = types.FunctionCall(name="get_quote", args={})
    turn = _model_turn(Result(text="checking", function_calls=[call]))

    assert turn.parts[0].text == "checking"
    assert turn.parts[1].function_call.name == "get_quote"
