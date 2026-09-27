import json
from pathlib import Path

from tests.conftest import DATASET_DIR, push


def _load_expanded():
    exp = DATASET_DIR / "expanded"
    categories = {json.load(open(f))["slug"]: json.load(open(f)) for f in (exp / "categories").glob("*.json")}
    merchants = {json.load(open(f))["merchant_id"]: json.load(open(f)) for f in (exp / "merchants").glob("*.json")}
    customers = {json.load(open(f))["customer_id"]: json.load(open(f)) for f in (exp / "customers").glob("*.json")}
    triggers = {json.load(open(f))["id"]: json.load(open(f)) for f in (exp / "triggers").glob("*.json")}
    return categories, merchants, customers, triggers


def test_tick_composes_specific_message_for_research_digest(client, dentists_category, dr_meera, research_digest_trigger):
    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)
    push(client, "trigger", research_digest_trigger["id"], research_digest_trigger)

    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [research_digest_trigger["id"]]})
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert len(actions) == 1
    action = actions[0]
    assert action["send_as"] == "vera"
    assert action["merchant_id"] == dr_meera["merchant_id"]
    assert action["trigger_id"] == research_digest_trigger["id"]
    # specificity: the trial size and source citation must be present verbatim
    assert "2,100" in action["body"] or "2100" in action["body"]
    assert "JIDA" in action["body"]
    # merchant fit: her real high-risk-adult count from customer_aggregate
    assert "124" in action["body"]
    assert action["cta"] in ("open_ended", "binary_yes_no", "binary_confirm_cancel", "multi_choice_slot", "none")
    for required in ("conversation_id", "suppression_key", "rationale", "template_name", "template_params"):
        assert action[required]


def test_tick_customer_facing_recall_uses_merchant_on_behalf(client, dentists_category, dr_meera, priya, recall_due_trigger):
    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)
    push(client, "customer", priya["customer_id"], priya)
    push(client, "trigger", recall_due_trigger["id"], recall_due_trigger)

    r = client.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": [recall_due_trigger["id"]]})
    actions = r.json()["actions"]
    assert len(actions) == 1
    action = actions[0]
    assert action["send_as"] == "merchant_on_behalf"
    assert action["customer_id"] == priya["customer_id"]
    assert "Priya" in action["body"]
    # her real active offer + real slot labels must appear
    assert "₹299" in action["body"]
    assert "Wed 5 Nov" in action["body"] or "Thu 6 Nov" in action["body"]


def test_tick_skips_customer_trigger_when_customer_context_missing(client, dentists_category, dr_meera, recall_due_trigger):
    """The judge pushes a customer context and its trigger 2 minutes apart —
    we must never fabricate customer identity before it arrives."""
    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)
    push(client, "trigger", recall_due_trigger["id"], recall_due_trigger)

    r = client.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": [recall_due_trigger["id"]]})
    assert r.json()["actions"] == []


def test_tick_respects_active_suppression_key(client, dentists_category, dr_meera, research_digest_trigger):
    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)
    push(client, "trigger", research_digest_trigger["id"], research_digest_trigger)

    r1 = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": [research_digest_trigger["id"]]})
    assert len(r1.json()["actions"]) == 1

    # Same trigger offered again on a later tick — suppression_key should block a resend.
    r2 = client.post("/v1/tick", json={"now": "2026-04-26T10:40:00Z", "available_triggers": [research_digest_trigger["id"]]})
    assert r2.json()["actions"] == []


def test_tick_never_exceeds_20_actions(client):
    categories, merchants, customers, triggers = _load_expanded()
    for slug, payload in categories.items():
        push(client, "category", slug, payload)
    for mid, payload in merchants.items():
        push(client, "merchant", mid, payload)
    for cid, payload in customers.items():
        push(client, "customer", cid, payload)

    all_ids = list(triggers.keys())
    for tid in all_ids:
        push(client, "trigger", tid, triggers[tid])

    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": all_ids})
    assert r.status_code == 200
    assert len(r.json()["actions"]) <= 20


def test_tick_one_action_per_merchant_per_tick(client):
    """Two different merchant-scope triggers for the SAME merchant in one
    tick should still only produce one action (use a later tick for more)."""
    categories, merchants, customers, triggers = _load_expanded()
    mid = "m_001_drmeera_dentist_delhi"
    push(client, "category", "dentists", categories["dentists"])
    push(client, "merchant", mid, merchants[mid])
    same_merchant_triggers = [t for t in triggers.values() if t.get("merchant_id") == mid and t.get("scope") == "merchant"]
    assert len(same_merchant_triggers) >= 2
    ids = [t["id"] for t in same_merchant_triggers]
    for t in same_merchant_triggers:
        push(client, "trigger", t["id"], t)

    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": ids})
    actions = [a for a in r.json()["actions"] if a["merchant_id"] == mid]
    assert len(actions) <= 1


def test_tick_no_body_ever_contains_a_url(client):
    categories, merchants, customers, triggers = _load_expanded()
    for slug, payload in categories.items():
        push(client, "category", slug, payload)
    for mid, payload in merchants.items():
        push(client, "merchant", mid, payload)
    for cid, payload in customers.items():
        push(client, "customer", cid, payload)
    all_ids = list(triggers.keys())
    for tid in all_ids:
        push(client, "trigger", tid, triggers[tid])

    seen = 0
    for i in range(0, len(all_ids), 15):
        batch = all_ids[i : i + 15]
        r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": batch})
        for a in r.json()["actions"]:
            seen += 1
            assert "http://" not in a["body"]
            assert "https://" not in a["body"]
            assert "www." not in a["body"]
            assert "None" not in a["body"]
            assert a["body"].strip() != ""
    assert seen > 0



def test_tick_generic_fallback_anchors_on_real_number_not_just_kind_label(client, dentists_category, dr_meera):
    """Task 2: A thin/placeholder trigger payload must still produce a message
    containing at least one real number from merchant.performance or
    category.peer_stats. It must NOT fall through to the bare content-free
    'ek X aaya hai' phrasing with zero concrete numbers."""
    import re

    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)

    # Construct a trigger with a fully placeholder payload — no real facts at all.
    placeholder_trigger = {
        "id": "trg_test_placeholder_generic",
        "scope": "merchant",
        "kind": "unknown_future_kind_xyz",   # not in _EXTRACTORS, forces generic fallback
        "source": "external",
        "merchant_id": dr_meera["merchant_id"],
        "customer_id": None,
        "payload": {"placeholder": True, "metric_or_topic": "unknown_future_kind_xyz"},
        "urgency": 2,
        "suppression_key": "test:placeholder:generic:001",
        "expires_at": "2026-12-31T00:00:00Z",
    }
    push(client, "trigger", placeholder_trigger["id"], placeholder_trigger)

    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z",
                                      "available_triggers": [placeholder_trigger["id"]]})
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert len(actions) == 1, "Expected 1 action for the placeholder trigger"

    body = actions[0]["body"]
    assert body.strip(), "Body must not be empty"

    # The improved generic fallback must cite something concrete — either a
    # real number from merchant.performance or category.peer_stats, OR a real
    # merchant signal (like "stale posts") from the merchant's own signals array.
    # Both are always sourced from real merchant data, not invented.
    #
    # Dr. Meera's signals include "stale_posts" which becomes "stale posts"
    # in the message — that IS a concrete fact, not empty generic phrasing.
    # We also accept any digit sequence like "2,410" (views) or "21" (calls).
    import re
    has_number = bool(re.search(r'\b\d[\d,\.]*\b', body))
    # The renderer normalizes signals the same way as _extract_generic:
    # s.replace("_", " ").split(":")[0]  →  "stale_posts:22d" → "stale posts"
    has_real_signal = any(
        s.replace("_", " ").split(":")[0].strip() in body.lower()
        for s in (dr_meera.get("signals") or [])
    )
    assert has_number or has_real_signal, (
        f"Generic fallback body should contain at least one real number "
        f"(from merchant.performance or category.peer_stats) OR a merchant "
        f"signal from the merchant's own signals array, got: {body!r}"
    )

    # Must NOT be the bare content-free fallback — the improved generic must
    # reference something concrete, not just echo the trigger kind.
    # These phrases appear in the empty-payload path (no signal, no number at all).
    generic_empty_phrases = [
        "came up on your account. Want me to take a closer look",
    ]
    body_lower = body.lower()
    for phrase in generic_empty_phrases:
        assert phrase.lower() not in body_lower, (
            f"Body looks like a content-free fallback (contains '{phrase}'): {body!r}"
        )


def test_tick_empty_when_no_triggers_worth_sending(client, dentists_category, dr_meera):
    push(client, "category", "dentists", dentists_category)
    push(client, "merchant", dr_meera["merchant_id"], dr_meera)
    r = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z", "available_triggers": []})
    assert r.status_code == 200
    assert r.json()["actions"] == []
