"""Guardrail category taxonomy + regex ruleset for live transcript flagging.

Design split: the LLM tier (openai_client.classify_utterance) identifies
WHAT was said -- a category. CODE decides HOW URGENT that is, via
CATEGORY_SEVERITY below. Severity is clinical/business policy: it must be
reviewable and changeable in one place without touching a prompt, and it
must never be something a model can silently redefine on a bad day.

Two rulesets, each a plain list of (compiled_pattern, category) tuples --
evaluated against TranscriptTurn.content by app/llm_websocket.py on
response_required (see that module for the trigger/dispatch logic). Not
DB-backed: editable-without-redeploy is scope creep for what this is -- a
short, defensible set of phrase matches, not a rules engine.

Regex is high-precision/low-recall by design: every pattern below should be
justifiable line-by-line out loud, matching only deliberate, specific
phrasings. Categories that need judgment regex can't do -- comparing against
ground truth, or reading conversational context -- have NO regex rule at
all and rely entirely on the LLM tier, which owns recall. See the
AGENT_RULES comment for the concrete case (fabrication/off_script) that
motivated this split.
"""
import re
from typing import Dict, List, Pattern, Tuple

Rule = Tuple[Pattern[str], str]

# Valid categories the LLM tier may return for a role -- and the source of
# truth for what get_call_classification/classify_utterance validates its
# response against ("Discard any category not in the valid list for that
# role" -- see app/openai_client.py). Values are the description text used
# to build that role's classifier prompt.
PATIENT_CATEGORIES: Dict[str, str] = {
    "self_harm": "self-harm or suicidal ideation",
    "acute_medical": "chest pain, difficulty breathing, severe/emergent",
    "physical_symptom": "pain, discomfort, new or worsening symptoms",
    "financial_barrier": "cannot afford care, copay, medication, insurance",
    "transportation_barrier": "no ride, cannot physically get to the appointment",
    "caregiver_barrier": "childcare, eldercare, dependent-care conflict",
    "confusion": "does not understand the appointment or instructions",
    "dissatisfaction": "frustrated with care, the practice, or this call",
}

AGENT_CATEGORIES: Dict[str, str] = {
    "medical_advice": "advice, dosage, diagnosis, or clinical guidance",
    "fabrication": "stated a time/place/detail not in the Patient record",
    "off_script": "materially outside confirming/rescheduling an appointment",
}

# The ONLY place severity is decided -- not the LLM (it must not return a
# severity; see classify_utterance), not the regex rules below, not the
# escalation logic in app/llm_websocket.py. Every category in
# PATIENT_CATEGORIES/AGENT_CATEGORIES must have an entry here (enforced by
# test_guardrails.py) so a missing mapping fails a test, not a live call.
CATEGORY_SEVERITY: Dict[str, str] = {
    "self_harm": "high",
    "acute_medical": "high",
    "medical_advice": "high",
    "physical_symptom": "low",
    "financial_barrier": "low",
    "transportation_barrier": "low",
    "caregiver_barrier": "low",
    "confusion": "low",
    "dissatisfaction": "low",
    "fabrication": "low",
    "off_script": "low",
}

# Ordering for the Escalation ratchet (app/llm_websocket.py): severity
# only ever moves up this scale, never down. Lives here, not in the
# escalation logic itself, for the same reason CATEGORY_SEVERITY does --
# one reviewable place for "how urgent," not scattered comparisons.
SEVERITY_RANK: Dict[str, int] = {"low": 0, "high": 1}

# Human-readable labels for the dashboard -- never show a raw category slug
# to the operator.
CATEGORY_LABELS: Dict[str, str] = {
    "self_harm": "Self-harm risk",
    "acute_medical": "Acute medical emergency",
    "physical_symptom": "New or worsening symptom",
    "financial_barrier": "Cannot afford care",
    "transportation_barrier": "No transportation to appointment",
    "caregiver_barrier": "Caregiving conflict",
    "confusion": "Confused about appointment",
    "dissatisfaction": "Dissatisfied with care",
    "medical_advice": "Agent gave medical advice",
    "fabrication": "Agent stated unverified details",
    "off_script": "Agent went off script",
}

# Signals the PATIENT needs a human -- evaluated against role == "patient"
# turns only.
#
# physical_symptom, financial_barrier, transportation_barrier, and
# caregiver_barrier have NO regex rule below -- deliberately, same rationale
# as the agent-side gap documented on AGENT_RULES. "I can't afford this,"
# "my ride fell through," "I have no one to watch my kids" all take too many
# forms to pattern-match reliably; a regex would either miss most real
# phrasings or fire on unrelated small talk. These four are LLM-tier-only.
PATIENT_RULES: List[Rule] = [
    # self-harm / suicidal language -- always escalate, never a false
    # positive worth suppressing.
    (re.compile(r"\b(kill myself|end(?:ing)? (it all|my life)|suicid\w*|hurt myself|self[- ]harm)\b", re.IGNORECASE), "self_harm"),
    # acute medical symptoms spoken mid-call -- an appointment reminder call
    # is not the place to triage a real emergency.
    (re.compile(r"\b(chest pain|can'?t breathe|trouble breathing|severe (pain|bleeding)|passing out|overdos\w*)\b", re.IGNORECASE), "acute_medical"),
    # explicit distress / calls for help -- generic emergency framing with
    # no more specific symptom named, still routed to acute_medical rather
    # than left uncategorized.
    (re.compile(r"\b(help me|i'?m (scared|terrified|desperate)|this is an emergency)\b", re.IGNORECASE), "acute_medical"),
    # the patient isn't following the conversation.
    (re.compile(r"\b(i don'?t understand|what do you mean|i'?m confused|can you repeat that)\b", re.IGNORECASE), "confusion"),
    # dissatisfaction with the call/service.
    (re.compile(r"\b(this is (ridiculous|unacceptable)|i'?m (frustrated|annoyed|upset) with)\b", re.IGNORECASE), "dissatisfaction"),
]

# Signals our own LLM did something incorrect or dangerous -- evaluated
# against role == "navigator" turns only.
#
# All four rules below collapse onto "medical_advice" -- each is a specific,
# deliberate phrasing a scheduling agent should never produce, so regex can
# catch them with high precision. fabrication and off_script have NO regex
# rule at all: both require comparing what was said against ground truth
# (the real Patient record) or judging conversational scope, which a
# pattern match cannot do. Forcing a regex for those would fire on noise,
# which is worse than not flagging at all given TranscriptFlag's dedup/audit
# role. Both are entirely the LLM tier's responsibility (classify_utterance
# in openai_client.py) -- this is the concrete case that motivated splitting
# "the LLM identifies what, code decides how urgent" in the first place:
# regex is high-precision/low-recall by design, and the LLM tier owns recall.
AGENT_RULES: List[Rule] = [
    # dosage/medication instructions -- a scheduling agent must never tell a
    # patient what or how much medication to take.
    (re.compile(r"\b(take|increase|stop taking|reduce)\b.{0,30}\b(\d+\s*(mg|milligrams|pills?|tablets?)|your (medication|dose|dosage))\b", re.IGNORECASE), "medical_advice"),
    # diagnosis language -- naming a condition/disease is clinical
    # territory, not appointment confirmation.
    (re.compile(r"\b(you (have|are experiencing)|it sounds like you have|this (indicates|means you have))\b.{0,30}\b(disease|infection|condition|disorder|syndrome)\b", re.IGNORECASE), "medical_advice"),
    # claiming clinical authority the agent doesn't have.
    (re.compile(r"\b(as your (doctor|physician|provider)|i diagnose|my medical opinion|speaking as a medical)\b", re.IGNORECASE), "medical_advice"),
    # promising a clinical outcome -- overstepping into guarantees no
    # scheduling call should ever make.
    (re.compile(r"\byou will (be (fine|cured)|recover fully|definitely (get better|be okay))\b", re.IGNORECASE), "medical_advice"),
]
