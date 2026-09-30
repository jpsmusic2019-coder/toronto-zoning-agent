# Copyright © 2026 Joshua Seaton. All rights reserved. Evaluation only; see LICENSE.
"""
Zoning Agent — AI narrative layer.

Load-bearing contract: **rules decide the
facts; Claude only writes the interpretation.** Every count, ratio, type, signal and
flag in `facts` is computed deterministically *before* this module runs. Claude
receives the finished fact-set and writes a short read on top — it never
retrieves records, counts approvals, or invents or changes a number.

Default behaviour is the deterministic template (no API call). Claude runs only
when `use_claude=True` AND `ANTHROPIC_API_KEY` is set.

A source that couldn't be reached is never described as having no records: the
template and the prompt both say it couldn't be checked.
"""
from __future__ import annotations

import os
import re

from dotenv import load_dotenv

load_dotenv()

_MODEL_HAIKU = "claude-haiku-4-5"  # interpretation only — never decides a fact
TEMPLATE_VERSION = "zoning-narrative-template-2.0"
_CLIENT = None


def _get_client():
    global _CLIENT
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    if _CLIENT is None:
        from anthropic import Anthropic
        _CLIENT = Anthropic()
    return _CLIENT


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def precedent_sentence(facts: dict) -> str:
    """The neighbour-precedent sentence, from the computed counts only."""
    radius = facts.get("radius_m", 250)
    status = facts.get("neighbour_status")
    n = facts.get("neighbour_coa_count", 0)
    decided = facts.get("decided_nearby", 0)
    approved = facts.get("approved_nearby", 0)
    if status == "unchecked":
        return ("Neighbour precedent couldn't be checked because the City's Committee of "
                "Adjustment data wasn't reachable; treat it as missing data, not a result.")
    if status == "skipped":
        return ""
    if n == 0:
        return (f"No Committee of Adjustment applications are on record within {radius} m, "
                f"so there is no nearby precedent either way.")
    if decided == 0:
        return (f"Within {radius} m, {_plural(n, 'application is', 'applications are')} on "
                f"file but none has been approved or refused yet.")
    s = (f"Within {radius} m, {approved} of {decided} decided applications were approved "
         f"({facts.get('approval_pct')}%)")
    three = facts.get("three_storey_new_houses", 0)
    nh = facts.get("approved_new_houses", 0)
    if three:
        s += f", including {_plural(three, 'three-storey new house', 'three-storey new houses')}"
    elif nh:
        s += f", including {_plural(nh, 'new house', 'new houses')}"
    return s + "."


def timing_sentence(facts: dict) -> str:
    w = facts.get("typical_hearing_weeks")
    if w is None:
        return ""
    if facts.get("typical_hearing_recent"):
        return f"Recent hearings came a median of {w} weeks after filing."
    return f"Hearings came a median of {w} weeks after filing (all years; too few recent files)."


def _timing_fact(facts: dict) -> str:
    """The hearing-time line given to Claude: always named as a median."""
    w = facts.get("typical_hearing_weeks")
    if w is None:
        return "too few decided files to say"
    if facts.get("typical_hearing_recent"):
        n = facts.get("recent_hearing_n")
        return (f"median {w} weeks from filing to hearing" +
                (f", across {n} decided files filed in the last three years" if n else ""))
    return f"median {w} weeks from filing to hearing, all years (too few recent files)"


def deterministic_narrative(facts: dict) -> str:
    """Plain-English memo assembled purely from the computed facts (no API call)."""
    parts: list[str] = []
    status = facts.get("subject_status")
    chain = facts.get("subject_chain", "")
    if chain:
        parts.append(chain)
    elif status == "unchecked":
        parts.append("This lot's application and permit history couldn't be checked "
                     "because the City's data service wasn't reachable.")
    else:
        parts.append("No Committee of Adjustment or building permit history is on record "
                     "for this lot.")
    for s in (precedent_sentence(facts), timing_sentence(facts)):
        if s:
            parts.append(s)
    devapps = facts.get("devapp_count", 0)
    if devapps and facts.get("devapp_status") != "unchecked":
        oz = facts.get("oz_count", 0)
        what = _plural(devapps, "development application", "development applications")
        if oz:
            what += f" ({_plural(oz, 'rezoning or Official Plan amendment', 'rezonings or Official Plan amendments')})"
        parts.append(f"{what} within {facts.get('radius_m', 250)} m.")
    unchecked = facts.get("unchecked_sources") or []
    if unchecked:
        parts.append("Not checked this run because the City's data wasn't reachable: "
                     + ", ".join(unchecked) + ".")
    return " ".join(parts)


def _decision_counts(facts: dict) -> str:
    if facts.get("neighbour_status") in ("unchecked", "skipped"):
        return "not checked"
    n = facts.get("neighbour_coa_count", 0)
    decided = facts.get("decided_nearby", 0)
    approved = facts.get("approved_nearby", 0)
    return (f"{n} applications on file: {approved} approved, {decided - approved} refused, "
            f"{n - decided} not decided (awaiting a hearing, deferred, withdrawn or closed "
            f"without a decision)")


def _claude_prompt(facts: dict) -> str:
    unchecked = facts.get("unchecked_sources") or []
    return f"""You are a Toronto land-use planner writing for a real estate developer.
Below is a FACT-SET already computed by a rule-based model. Do NOT invent, round
differently or change any number, date, file number, outcome or count. Write 3-5
sentences interpreting what this lot's history and the neighbour precedent imply about
what is likely to be approved here, and flag anything worth the developer's attention.
Use only the facts given.

Sources that could NOT be checked this run: {', '.join(unchecked) if unchecked else 'none'}.
For any source listed there, say it couldn't be checked. Never describe it as having
no records, no precedent or as untested.
The hearing time below is a MEDIAN. Call it the median or typical time; never call it
an average or a mean. Every number you write must appear in the facts below.
Use every permit and file status word for word as the facts give it (for example "with
a revision issued" or "under inspection"). Never restate a status as a different state:
a revision that was issued is not "under revision".
Don't describe the neighbours with anything the facts don't give (for example, don't say
they share this lot's zone). Plain sentences only: no markdown, bold, headings or lists.
Describe each part of the zone string only by its decoded meaning below (for example, f is
the minimum lot frontage in metres and d is the maximum floor space index).
If the zone string has a site-specific exception (an "x" number), never say only the base
zone rules apply; say the exception can change them.

Address: {facts.get('address', '')}
Zone: {facts.get('zone_code', 'n/a')} ({facts.get('zone_name', '')})
Zone string decoded (use these meanings exactly): {facts.get('zone_decoded') or 'not decoded'}
By-right envelope: {facts.get('envelope_str', 'n/a')}
Governing plan: {facts.get('governing_plan', 'n/a')}
This lot's history: {facts.get('subject_chain') or ('could not be checked' if facts.get('subject_status') == 'unchecked' else 'no Committee of Adjustment or permit history on record')}
Neighbour precedent (computed sentence): {precedent_sentence(facts) or 'not checked'}
Neighbour decisions: {_decision_counts(facts)}
Approved new houses within the radius: {facts.get('approved_new_houses', 0)} (three-storey: {facts.get('three_storey_new_houses', 0)})
Time from filing to hearing: {_timing_fact(facts)}
Neighbour building-permit projects within the radius: {facts.get('neighbour_permit_count', 0)} (new buildings: {facts.get('neighbour_new_building_permits', 0)})
Nearby development applications: {facts.get('devapp_count', 0) if facts.get('devapp_status') != 'unchecked' else 'could not be checked'} (rezonings / Official Plan amendments among them: {facts.get('oz_count', 0)}; the rest are site plans, subdivisions, condominiums or part lot control, not rezonings)
Precedent signal (approval-rate rule): {facts.get('precedent_signal', 'Sparse')}

Output only the interpretation text, no preamble."""


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_AVERAGE_RE = re.compile(r"\baverag\w*|\bmean (?:time|wait|of|hearing)", re.I)
# Status paraphrases that change what the City's record says (65 Bristol's permit is
# "with a revision issued"; a draft once called it "now under revision").
_MARKDOWN_RE = re.compile(r"\*\*|__|^\s*(?:#+|[-*•]|\d+\.)\s", re.M)
# Neighbour facts carry no zone, so "within the R (d0.6) zone" about them is invented.
_SAME_ZONE_RE = re.compile(r"\b(?:same|this lot's|the lot's|within the|in the)\s+(?:[A-Z]{1,4}\b.{0,30}?\s)?zone\b", re.I)
# "floor space index parameters (f12.0; d0.65)": f is frontage, not FSI.
_FRONTAGE_AS_FSI_RE = re.compile(
    r"(?:floor space index|\bFSI\b)[^.]{0,40}\(f\d|\bf\d+(?:\.\d+)?\b[^.;]{0,25}(?:floor space index|\bFSI\b)", re.I)
_STATUS_DRIFT_RE = re.compile(r"\b(?:under|in|being|pending) revision\b|\bbeing revised\b", re.I)


def _numbers(text: str) -> set[float]:
    out = set()
    for m in _NUM_RE.findall(text or ""):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def check_narrative(text: str, prompt: str) -> list[str]:
    """Problems that stop Claude's text being used: a number that isn't in the facts,
    or a median called an average. [] when the text passes."""
    problems = []
    extra = sorted(_numbers(text) - _numbers(prompt))
    if extra:
        problems.append("numbers not in the facts: " + ", ".join(f"{x:g}" for x in extra))
    if _AVERAGE_RE.search(text or ""):
        problems.append("called the median hearing time an average")
    if _MARKDOWN_RE.search(text or ""):
        problems.append("used markdown formatting; write plain sentences")
    if _SAME_ZONE_RE.search(text or ""):
        problems.append("said the neighbours share the lot's zone, which the facts don't give")
    if re.search(r"\(x\d+\)", prompt or "") and re.search(r"\bbase zon(?:e|ing)\b(?! rules? (?:may|can))", text or "", re.I):
        problems.append("said base zone rules apply, but the lot has a site-specific exception")
    if _FRONTAGE_AS_FSI_RE.search(text or ""):
        problems.append("called the frontage token (f…) a floor space index; f is minimum lot frontage")
    for m in _STATUS_DRIFT_RE.finditer(text or ""):
        if m.group(0).lower() not in (prompt or "").lower():
            problems.append(f'restated a status as "{m.group(0)}"; use the status word for word')
            break
    return problems


def _ask(client, content: str) -> str:
    resp = client.messages.create(
        model=_MODEL_HAIKU,
        max_tokens=350,
        # anthropic 1.x dropped `temperature` from messages.create(); Haiku 4.5 still
        # accepts it, and a low value suits a numbers-exact rewording.
        extra_body={"temperature": 0.3},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("model declined (stop_reason=refusal)")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise RuntimeError(f"empty response (stop_reason={resp.stop_reason})")
    return text


def _claude_narrative(facts: dict, client) -> str:
    """Claude's interpretation, checked against the facts. One corrective retry, then
    the caller falls back to the template."""
    prompt = _claude_prompt(facts)
    text = _ask(client, prompt)
    problems = check_narrative(text, prompt)
    if problems:
        text = _ask(client, prompt + "\n\nYour previous draft was rejected ("
                    + "; ".join(problems) + "). Rewrite it using only the facts above.")
        problems = check_narrative(text, prompt)
        if problems:
            raise RuntimeError("draft failed the fact check (" + "; ".join(problems) + ")")
    return text


def describe_failure(e: Exception) -> str:
    """Short reason for a failed Claude call, e.g. '401 invalid x-api-key'."""
    code = getattr(e, "status_code", None)
    msg = ""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message", "") if isinstance(body.get("error"), dict) else ""
    msg = msg or str(e).splitlines()[0][:120] or type(e).__name__
    return f"{code} {msg}" if code else f"{type(e).__name__}: {msg}"


# Why the last requested Claude call didn't produce the narrative ("" when it did,
# or when Claude wasn't requested). Read by the agent to report it to the user.
last_failure = ""


def generate_zoning_narrative(facts: dict, use_claude: bool = False) -> tuple[str, str]:
    """Return (narrative_text, model_version).

    Deterministic template unless use_claude AND a key is present. Any Claude error
    degrades to the template — the narrative call must never break a completed memo —
    and the reason is kept in `last_failure` so the run can say why.
    """
    global last_failure
    last_failure = ""
    client = _get_client() if use_claude else None
    if not client:
        if use_claude:
            last_failure = "no ANTHROPIC_API_KEY set"
        return deterministic_narrative(facts), TEMPLATE_VERSION
    try:
        return _claude_narrative(facts, client), f"zoning-narrative-{_MODEL_HAIKU}"
    except Exception as e:  # noqa: BLE001
        last_failure = describe_failure(e)
        return deterministic_narrative(facts), TEMPLATE_VERSION
