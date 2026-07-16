"""guardrails.py -- pure regex/dict logic, no I/O. Each case below exists so
every rule in PATIENT_RULES/AGENT_RULES is provably correct on its own,
independent of the later trigger/dispatch and escalation plumbing (which
gets its own integration coverage once built).
"""
import pytest

from app.guardrails import (
    AGENT_CATEGORIES,
    AGENT_RULES,
    CATEGORY_LABELS,
    CATEGORY_SEVERITY,
    PATIENT_CATEGORIES,
    PATIENT_RULES,
)


def _matches(rules, text):
    return [category for pattern, category in rules if pattern.search(text)]


@pytest.mark.parametrize(
    "text,expected_category,expected_severity",
    [
        ("I've been thinking about ending my life.", "self_harm", "high"),
        ("Honestly I just want to hurt myself.", "self_harm", "high"),
        ("I'm having chest pain right now.", "acute_medical", "high"),
        ("I can't breathe very well.", "acute_medical", "high"),
        ("Please help me, this is an emergency.", "acute_medical", "high"),
        ("Sorry, I don't understand what you're asking.", "confusion", "low"),
        ("Can you repeat that? I'm confused.", "confusion", "low"),
        ("This is ridiculous, I'm frustrated with this whole thing.", "dissatisfaction", "low"),
    ],
)
def test_patient_rules_match_expected_category(text, expected_category, expected_severity):
    assert expected_category in _matches(PATIENT_RULES, text)
    assert CATEGORY_SEVERITY[expected_category] == expected_severity


@pytest.mark.parametrize(
    "text",
    [
        "Yes, that works for me, thank you.",
        "I'll see you at my appointment on Thursday.",
        "Sounds good, talk to you soon.",
    ],
)
def test_patient_rules_do_not_match_ordinary_speech(text):
    assert _matches(PATIENT_RULES, text) == []


@pytest.mark.parametrize(
    "text,expected_category,expected_severity",
    [
        ("You should take 200 mg of ibuprofen for that.", "medical_advice", "high"),
        ("Go ahead and increase your dosage tonight.", "medical_advice", "high"),
        ("It sounds like you have an ear infection.", "medical_advice", "high"),
        ("You have a sinus condition based on what you're describing.", "medical_advice", "high"),
        ("Speaking as your doctor, I'd recommend rest.", "medical_advice", "high"),
        ("You will definitely be okay, don't worry.", "medical_advice", "high"),
        ("You will be cured within a week.", "medical_advice", "high"),
    ],
)
def test_agent_rules_match_expected_category(text, expected_category, expected_severity):
    assert expected_category in _matches(AGENT_RULES, text)
    assert CATEGORY_SEVERITY[expected_category] == expected_severity


@pytest.mark.parametrize(
    "text",
    [
        "I'm calling to confirm your appointment on Thursday at 2 PM.",
        "Would you like to reschedule or keep the current time?",
        "Great, we'll see you then. Take care!",
    ],
)
def test_agent_rules_do_not_match_ordinary_scheduling_speech(text):
    assert _matches(AGENT_RULES, text) == []


def test_every_category_has_a_severity():
    # A category with no severity must fail loudly here, at test time --
    # not as a KeyError in CATEGORY_SEVERITY[category] on a live call.
    all_categories = set(PATIENT_CATEGORIES) | set(AGENT_CATEGORIES)
    missing = all_categories - set(CATEGORY_SEVERITY)
    assert missing == set()


def test_every_severity_value_is_high_or_low():
    assert set(CATEGORY_SEVERITY.values()) <= {"high", "low"}


def test_every_category_has_a_dashboard_label():
    all_categories = set(PATIENT_CATEGORIES) | set(AGENT_CATEGORIES)
    missing = all_categories - set(CATEGORY_LABELS)
    assert missing == set()


def test_every_rule_category_is_in_its_taxonomy():
    for _, category in PATIENT_RULES:
        assert category in PATIENT_CATEGORIES
    for _, category in AGENT_RULES:
        assert category in AGENT_CATEGORIES
