"""Thin wrapper around the OpenAI chat completion call used by
app/llm_websocket.py to generate the navigator agent's spoken responses.

Kept as a single, small, mockable seam: llm_websocket.py only knows it gets
an async stream of (text_chunk, is_final_chunk) pairs back, not that it's
OpenAI specifically -- swapping providers later would only touch this file.
"""
import json
import logging
import re
from typing import AsyncIterator, Dict, List, Optional, Tuple

from openai import AsyncOpenAI

from app.config import OPENAI_API_KEY
from app.guardrails import AGENT_CATEGORIES, PATIENT_CATEGORIES

logger = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"

# Scope is a full natural conversation, not a rigid script -- a closed-
# question flow suppresses the kind of unprompted patient speech the
# guardrail-flagging work needs something real to catch. Deliberately kept
# short enough to read in full and defend live, not because a "real"
# version would need more: the four states below are the whole job.
#
# {appointment_date}/{appointment_time}/{timezone} are filled in per-call by
# build_system_prompt() from the real Patient row -- NOT left for the model
# to infer. A live test call confirmed the model will otherwise hallucinate
# a plausible-sounding but fabricated date/time (and inconsistently, a
# different one turn to turn) if it isn't given the real value up front.
SYSTEM_PROMPT_TEMPLATE = """You are a scheduling navigator calling a patient on behalf of \
their healthcare provider to confirm an upcoming appointment. This is a real \
phone call -- speak naturally and briefly, the way a person would, not like \
you're reading a script.

The conversation history you see includes the opening greeting and identity \
check, which already happened at the start of this call -- read it before \
you respond. If the patient has already confirmed who they are, do NOT \
greet them or ask again; move straight on to the appointment.

Never use placeholder or template text such as "[Your Name]", \
"[Healthcare Provider's Office]", or "[Patient's Name]". You don't have a \
specific personal name -- if you need to refer to yourself or the office, \
say something natural like "your care team" or "the office", never a \
bracketed field waiting to be filled in.

APPOINTMENT RECORD (verified, from the patient's chart -- this is your only \
source of truth for date/time, never invent or guess a different one):
  Date: {appointment_date}
  Time: {appointment_time}
  Timezone: {timezone}
This record has no location/clinic address on file. If asked where the \
appointment is, say honestly that you don't have the exact location in \
front of you and the office can confirm it -- never invent a location.

Move the conversation through these in roughly this order, but let it flow \
naturally between them rather than forcing rigid yes/no turns:

1. GREETING & IDENTITY -- already handled at call start (see above) -- do \
not repeat this step.

2. APPOINTMENT STATUS -- confirm their upcoming appointment using the exact \
date/time above. If they want to reschedule or cancel, help with that, and \
answer reasonable follow-up questions using only the record above -- never \
state a date, time, or location that isn't in it.

3. OPEN LISTENING -- if the patient brings up anything else unprompted -- a \
symptom, distress, a logistical problem, anything -- actually listen and \
respond to it with empathy before steering back to the appointment. Do not \
ignore what they've raised or redirect away from it immediately.

4. CLOSING -- once the appointment matter is settled, close the call warmly. \
If the patient raises something serious or urgent mid-conversation, it's \
fine to end the call sooner than a full scripted flow would -- don't keep \
going as if nothing happened.

Keep every response short -- a sentence or two. This is a phone call, not an \
email."""

# Falls back to this when no verified record is available (e.g. the
# CallLog/Patient lookup failed) -- the model must say so honestly rather
# than fabricating a date, matching the same "never invent" rule above.
_NO_RECORD_ON_FILE = "not available -- tell the patient you don't have it in front of you right now"

_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# The two prompts below are spec artifacts under review, not free-form
# implementation -- copied verbatim, character for character, from the
# reviewed spec. Do not reword any line here without going back to the
# spec first.
_PATIENT_CLASSIFIER_PROMPT = """You classify a single utterance spoken by a patient during an automated
appointment-reminder phone call.

Return a JSON array. Each element is an object with exactly two keys:
  "category"     — one of the identifiers below, verbatim
  "cited_phrase" — the exact span of the utterance that triggered it,
                   copied character-for-character from the input

Categories:
  self_harm              — expresses self-harm or suicidal ideation
  acute_medical          — emergent symptoms: chest pain, difficulty breathing,
                           severe bleeding, loss of consciousness
  physical_symptom       — pain, discomfort, or new/worsening symptoms that are
                           not emergent
  financial_barrier      — cannot afford care, copay, or medication; lacks
                           insurance coverage
  transportation_barrier — no way to physically reach the appointment
  caregiver_barrier      — childcare, eldercare, or dependent-care conflict
  confusion              — does not understand the appointment or instructions
  dissatisfaction        — frustrated with their care, the practice, or this call

Rules:
- Return [] if no category applies. Most utterances in a routine reminder call
  are ordinary scheduling speech and must return [].
- Classify only what the utterance states or plainly implies. Do not infer.
- cited_phrase must appear verbatim in the utterance. Do not paraphrase,
  summarize, or reconstruct.
- One utterance may raise multiple categories. Return one object per category.
- Do not assign urgency, severity, or priority. That is decided elsewhere.
- Output only the JSON array. No prose, no markdown fences."""

# Two variants -- with and without the "Appointment facts on record" block
# and the "fabrication" category line -- rather than one template
# conditionally splicing pieces together, so each is independently exact
# and reviewable against the spec rather than reconstructed from parts.
_NAVIGATOR_CLASSIFIER_PROMPT_WITH_FACTS = """You are auditing a single utterance produced by an automated appointment-
reminder agent. The agent's only sanctioned job is to confirm, reschedule, or
cancel an appointment. It is not a clinician and has no clinical authority.

Return a JSON array. Each element is an object with exactly two keys:
  "category"     — one of the identifiers below, verbatim
  "cited_phrase" — the exact span, copied character-for-character from the input

Categories:
  medical_advice — gave clinical advice, a diagnosis, medication or dosage
                   guidance, or claimed clinical authority
  fabrication    — stated an appointment detail that contradicts, or is absent
                   from, the appointment facts given below
  off_script     — went materially outside confirming, rescheduling, or
                   cancelling the appointment

Appointment facts on record:
  date: {appointment_date}
  time: {appointment_time} {timezone}
  patient: {patient_name}

Rules:
- Return [] if the utterance is a normal part of confirming, rescheduling, or
  cancelling the appointment.
- cited_phrase must appear verbatim in the utterance. Do not paraphrase.
- Do not assign urgency, severity, or priority.
- Output only the JSON array. No prose, no markdown fences."""

_NAVIGATOR_CLASSIFIER_PROMPT_WITHOUT_FACTS = """You are auditing a single utterance produced by an automated appointment-
reminder agent. The agent's only sanctioned job is to confirm, reschedule, or
cancel an appointment. It is not a clinician and has no clinical authority.

Return a JSON array. Each element is an object with exactly two keys:
  "category"     — one of the identifiers below, verbatim
  "cited_phrase" — the exact span, copied character-for-character from the input

Categories:
  medical_advice — gave clinical advice, a diagnosis, medication or dosage
                   guidance, or claimed clinical authority
  off_script     — went materially outside confirming, rescheduling, or
                   cancelling the appointment

Rules:
- Return [] if the utterance is a normal part of confirming, rescheduling, or
  cancelling the appointment.
- cited_phrase must appear verbatim in the utterance. Do not paraphrase.
- Do not assign urgency, severity, or priority.
- Output only the JSON array. No prose, no markdown fences."""

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_RE.sub("", text).strip()


async def classify_utterance(
    content: str, role: str, appointment_facts: Optional[Dict[str, str]] = None
) -> List[Dict[str, str]]:
    """Asks the model which guardrail categories (if any) apply to a single
    turn's content, returning [{"category": ..., "cited_phrase": ...}, ...]
    -- empty list if nothing applies OR if anything about this call fails.

    appointment_facts (patient_name/appointment_date/appointment_time/
    timezone) is the ground truth the "fabrication" category is checked
    against -- ignored entirely for role == "patient" (the patient prompt
    has no such placeholders at all). For role == "navigator", if
    appointment_facts is None, "fabrication" is omitted from both the
    category list offered to the model AND the set of categories this
    function will accept back: a category the model can name but cannot
    verify against anything produces confident noise, which is worse than
    no rule -- the same reasoning that already kept fabrication out of
    AGENT_RULES's regex tier.

    Deliberately returns severity-free: the model identifies WHAT was said,
    guardrails.CATEGORY_SEVERITY (code, not a prompt) decides HOW URGENT
    that is. cited_phrase must be a verbatim span from content, not a
    paraphrase, so a flag is auditable against the real transcript rather
    than an unfalsifiable model judgment.

    A malformed response, an unparseable one, a hallucinated category, or
    an outright API failure all resolve to an empty list rather than
    raising -- a classifier problem must never crash the socket handler or
    drop the call it's watching.
    """
    if role == "patient":
        prompt = _PATIENT_CLASSIFIER_PROMPT
        valid_categories = set(PATIENT_CATEGORIES)
    elif role == "navigator":
        if appointment_facts is not None:
            prompt = _NAVIGATOR_CLASSIFIER_PROMPT_WITH_FACTS.format(**appointment_facts)
            valid_categories = set(AGENT_CATEGORIES)
        else:
            logger.warning(
                "classify_utterance: no appointment_facts for a navigator turn -- "
                "omitting 'fabrication' from the offered/accepted categories "
                "rather than asking the model to judge against nothing"
            )
            prompt = _NAVIGATOR_CLASSIFIER_PROMPT_WITHOUT_FACTS
            valid_categories = set(AGENT_CATEGORIES) - {"fabrication"}
    else:
        logger.warning("classify_utterance: unrecognized role %r, skipping", role)
        return []

    try:
        response = await _client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": content},
            ],
            temperature=0,
        )
        raw = response.choices[0].message.content or ""
        parsed = json.loads(_strip_code_fences(raw))
    except Exception:
        logger.exception("classify_utterance: request/parse failed, role=%s", role)
        return []

    if not isinstance(parsed, list):
        logger.warning("classify_utterance: expected a JSON array, got %r, role=%s", parsed, role)
        return []

    results: List[Dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            logger.warning("classify_utterance: non-object array element %r, role=%s, discarding", item, role)
            continue
        category = item.get("category")
        cited_phrase = item.get("cited_phrase")
        if category not in valid_categories:
            # A hallucinated (or, for navigator-without-facts, an
            # unverifiable "fabrication") category must never reach the DB
            # -- discard and log rather than let it flow through to
            # TranscriptFlag.
            logger.warning(
                "classify_utterance: category %r not valid for role=%s (appointment_facts=%s), discarding",
                category, role, appointment_facts is not None,
            )
            continue
        if not isinstance(cited_phrase, str) or not cited_phrase.strip():
            logger.warning(
                "classify_utterance: missing/empty cited_phrase for category=%s role=%s, discarding",
                category, role,
            )
            continue
        results.append({"category": category, "cited_phrase": cited_phrase})

    return results


def build_system_prompt(appointment_context: Optional[Dict[str, str]]) -> str:
    """Fills SYSTEM_PROMPT_TEMPLATE with the real appointment record for
    this call, or an honest "not available" fallback if none could be
    loaded (see llm_websocket._load_appointment_context).
    """
    if appointment_context is None:
        appointment_context = {
            "appointment_date": _NO_RECORD_ON_FILE,
            "appointment_time": _NO_RECORD_ON_FILE,
            "timezone": _NO_RECORD_ON_FILE,
        }
    return SYSTEM_PROMPT_TEMPLATE.format(**appointment_context)


async def stream_agent_response(
    history: List[Dict[str, str]], appointment_context: Optional[Dict[str, str]] = None
) -> AsyncIterator[Tuple[str, bool]]:
    """Streams the navigator agent's next spoken response as
    (text_chunk, is_final_chunk) pairs, given the conversation so far
    (oldest first, roles already mapped to OpenAI's "user"/"assistant") and
    the verified appointment_context for this call (see build_system_prompt).

    Streamed rather than a single blocking call so llm_websocket.py can
    relay chunks to Retell as they arrive -- lower latency for the patient
    on the other end of a real phone call, per Retell's own protocol design
    (see api-references/llm-websocket.md: "send multiple response events to
    stream content for lower latency").
    """
    messages = [{"role": "system", "content": build_system_prompt(appointment_context)}, *history]
    stream = await _client.chat.completions.create(
        model=MODEL,
        messages=messages,
        stream=True,
    )
    async for event in stream:
        delta = event.choices[0].delta.content if event.choices else None
        if delta:
            yield delta, False
    yield "", True