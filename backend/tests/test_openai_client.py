"""openai_client.build_system_prompt -- pure string formatting, no I/O,
so no OpenAI client to mock here (stream_agent_response itself is
exercised indirectly via test_llm_websocket.py's monkeypatched tests).

classify_utterance tests below DO mock the OpenAI client (monkeypatching
_client.chat.completions.create directly, same seam-boundary approach as
test_llm_websocket.py's stream_agent_response mocking) so they never hit
the real API.
"""
import json

from app import openai_client
from app.openai_client import build_system_prompt, classify_utterance


def test_build_system_prompt_includes_real_appointment_record():
    prompt = build_system_prompt(
        {
            "appointment_date": "Thursday, July 16, 2026",
            "appointment_time": "2:00 PM",
            "timezone": "America/Los_Angeles",
        }
    )

    assert "Thursday, July 16, 2026" in prompt
    assert "2:00 PM" in prompt
    assert "America/Los_Angeles" in prompt


def test_build_system_prompt_falls_back_to_honest_not_available_when_context_missing():
    prompt = build_system_prompt(None)

    assert "not available" in prompt
    # Must not silently omit the field or leave a raw {appointment_date}
    # template placeholder unformatted.
    assert "{appointment_date}" not in prompt


# --- classify_utterance -----------------------------------------------------


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _mock_openai_response(monkeypatch, content):
    async def _fake_create(*args, **kwargs):
        return _FakeResponse(content)

    monkeypatch.setattr(openai_client._client.chat.completions, "create", _fake_create)


async def test_classify_utterance_returns_valid_categories(monkeypatch):
    _mock_openai_response(
        monkeypatch,
        json.dumps([{"category": "confusion", "cited_phrase": "I don't understand"}]),
    )

    result = await classify_utterance("I don't understand what you mean.", "patient")

    assert result == [{"category": "confusion", "cited_phrase": "I don't understand"}]


async def test_classify_utterance_strips_markdown_code_fences(monkeypatch):
    _mock_openai_response(
        monkeypatch,
        '```json\n[{"category": "confusion", "cited_phrase": "I don\'t understand"}]\n```',
    )

    result = await classify_utterance("I don't understand.", "patient")

    assert result == [{"category": "confusion", "cited_phrase": "I don't understand"}]


async def test_classify_utterance_discards_hallucinated_category(monkeypatch):
    # "medical_advice" is a valid AGENT category, not a valid PATIENT one --
    # must be discarded, not written through, when role="patient".
    _mock_openai_response(
        monkeypatch,
        json.dumps([{"category": "medical_advice", "cited_phrase": "something"}]),
    )

    result = await classify_utterance("some utterance", "patient")

    assert result == []


async def test_classify_utterance_discards_entry_missing_cited_phrase(monkeypatch):
    _mock_openai_response(
        monkeypatch,
        json.dumps([{"category": "confusion"}, {"category": "dissatisfaction", "cited_phrase": ""}]),
    )

    result = await classify_utterance("some utterance", "patient")

    assert result == []


async def test_classify_utterance_ignores_severity_if_model_returns_one(monkeypatch):
    _mock_openai_response(
        monkeypatch,
        json.dumps(
            [{"category": "confusion", "cited_phrase": "I don't understand", "severity": "high"}]
        ),
    )

    result = await classify_utterance("I don't understand.", "patient")

    assert result == [{"category": "confusion", "cited_phrase": "I don't understand"}]
    assert "severity" not in result[0]


async def test_classify_utterance_malformed_json_returns_empty_list(monkeypatch):
    _mock_openai_response(monkeypatch, "this is not json at all")

    result = await classify_utterance("some utterance", "patient")

    assert result == []


async def test_classify_utterance_non_array_json_returns_empty_list(monkeypatch):
    _mock_openai_response(monkeypatch, json.dumps({"category": "confusion"}))

    result = await classify_utterance("some utterance", "patient")

    assert result == []


async def test_classify_utterance_api_failure_returns_empty_list(monkeypatch):
    async def _raise(*args, **kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr(openai_client._client.chat.completions, "create", _raise)

    result = await classify_utterance("some utterance", "patient")

    assert result == []


async def test_classify_utterance_unrecognized_role_returns_empty_list_without_calling_api():
    # No mocking here on purpose -- if this reached the API call it would
    # fail against the real (fake test) API key, proving the early return
    # happens before any request is made.
    result = await classify_utterance("some utterance", "unknown")

    assert result == []


async def test_classify_utterance_navigator_role_uses_agent_categories(monkeypatch):
    _mock_openai_response(
        monkeypatch,
        json.dumps([{"category": "medical_advice", "cited_phrase": "take 200mg"}]),
    )

    result = await classify_utterance("You should take 200mg.", "navigator")

    assert result == [{"category": "medical_advice", "cited_phrase": "take 200mg"}]


# --- appointment_facts / fabrication ----------------------------------------

_FACTS = {
    "appointment_date": "Wednesday, July 15, 2026",
    "appointment_time": "9:00 AM",
    "timezone": "America/Los_Angeles",
    "patient_name": "Liam Pinson",
}


async def test_classify_utterance_navigator_prompt_includes_facts_and_fabrication(monkeypatch):
    captured = {}

    async def _fake_create(*args, **kwargs):
        captured["messages"] = kwargs["messages"]
        return _FakeResponse(json.dumps([]))

    monkeypatch.setattr(openai_client._client.chat.completions, "create", _fake_create)

    await classify_utterance("Your appointment is next Tuesday at 3pm.", "navigator", _FACTS)

    system_prompt = captured["messages"][0]["content"]
    assert "fabrication" in system_prompt
    assert "Wednesday, July 15, 2026" in system_prompt
    assert "9:00 AM" in system_prompt
    assert "America/Los_Angeles" in system_prompt
    assert "Liam Pinson" in system_prompt


async def test_classify_utterance_navigator_prompt_omits_fabrication_when_facts_missing(
    monkeypatch, caplog
):
    captured = {}

    async def _fake_create(*args, **kwargs):
        captured["messages"] = kwargs["messages"]
        return _FakeResponse(json.dumps([]))

    monkeypatch.setattr(openai_client._client.chat.completions, "create", _fake_create)

    with caplog.at_level("WARNING"):
        await classify_utterance("Your appointment is next Tuesday.", "navigator", None)

    system_prompt = captured["messages"][0]["content"]
    assert "fabrication" not in system_prompt
    assert "Appointment facts on record" not in system_prompt
    assert any("appointment_facts" in r.message for r in caplog.records)


async def test_classify_utterance_fabrication_fires_when_contradicted(monkeypatch):
    _mock_openai_response(
        monkeypatch,
        json.dumps([{"category": "fabrication", "cited_phrase": "next Tuesday at 3pm"}]),
    )

    result = await classify_utterance(
        "Your appointment is actually next Tuesday at 3pm.", "navigator", _FACTS
    )

    assert result == [{"category": "fabrication", "cited_phrase": "next Tuesday at 3pm"}]


async def test_classify_utterance_fabrication_discarded_when_facts_missing_even_if_returned(
    monkeypatch,
):
    # Defensive: even if the model somehow returns "fabrication" despite it
    # not being offered, it's discarded -- the model can't verify a claim
    # against facts it was never given.
    _mock_openai_response(
        monkeypatch,
        json.dumps([{"category": "fabrication", "cited_phrase": "next Tuesday"}]),
    )

    result = await classify_utterance("Your appointment is next Tuesday.", "navigator", None)

    assert result == []


async def test_classify_utterance_medical_advice_and_off_script_still_valid_without_facts(
    monkeypatch,
):
    # Only "fabrication" is gated on appointment_facts -- the other two
    # navigator categories are unaffected.
    _mock_openai_response(
        monkeypatch,
        json.dumps([{"category": "medical_advice", "cited_phrase": "take some aspirin"}]),
    )

    result = await classify_utterance("You should take some aspirin.", "navigator", None)

    assert result == [{"category": "medical_advice", "cited_phrase": "take some aspirin"}]


async def test_classify_utterance_appointment_facts_ignored_for_patient_role(monkeypatch):
    captured = {}

    async def _fake_create(*args, **kwargs):
        captured["messages"] = kwargs["messages"]
        return _FakeResponse(json.dumps([]))

    monkeypatch.setattr(openai_client._client.chat.completions, "create", _fake_create)

    await classify_utterance("I don't understand.", "patient", _FACTS)

    system_prompt = captured["messages"][0]["content"]
    # The patient prompt has no appointment-facts placeholders at all --
    # confirms appointment_facts was never even consulted for this role.
    assert "Liam Pinson" not in system_prompt
    assert "Appointment facts" not in system_prompt