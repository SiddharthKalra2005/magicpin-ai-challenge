"""Entry point matching the challenge's `uvicorn bot:app` convention.

Run locally with:
    uvicorn bot:app --host 0.0.0.0 --port 8080

All actual logic lives in app/ — see README.md for the module map.
"""

from typing import Optional
from datetime import datetime, timezone
from app.main import app
from app.facts import extract
from app.language import resolve_language
from app import composer, validator

__all__ = ["app", "compose"]

def compose(category: dict, merchant: dict, trigger: dict, customer: Optional[dict] = None) -> dict:
    """
    Implements the challenge-brief.md §7 contract for one-shot composition.
    """
    now_dt = datetime.now(timezone.utc)
    kind = trigger.get("kind", "")
    facts = extract(kind, category, merchant, trigger, customer, now_dt)
    lang = resolve_language(merchant, customer)
    
    compose_kind = kind if facts.is_specific else "__generic__"
    body, cta = composer.compose(compose_kind, category, merchant, trigger, customer, facts, lang)
    ok, reason = validator.validate(body, category, merchant, trigger, customer, facts)
    
    rationale_suffix = ""
    if not ok:
        body, cta = validator.safe_fallback(category, merchant, trigger, customer, lang)
        rationale_suffix = f" (fell back to safe template: {reason})"
        
    send_as = "merchant_on_behalf" if customer is not None else "vera"
    suppression_key = trigger.get("suppression_key") or f"{kind}:{merchant.get('merchant_id')}:{trigger.get('id')}"

    return {
        "body": body,
        "cta": cta,
        "send_as": send_as,
        "suppression_key": suppression_key,
        "rationale": f"Rule-based deterministic composition for {kind}{rationale_suffix}"
    }
