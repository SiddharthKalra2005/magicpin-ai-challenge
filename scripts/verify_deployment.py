#!/usr/bin/env python3
"""Post-deploy verification script.

Hits the 5 required endpoints of the live deployed service over real HTTPS
and reports PASS/FAIL for each. Exits with code 0 only if every check passes.

Usage:
    python scripts/verify_deployment.py https://your-app.onrender.com

This is intentionally NOT using TestClient — the whole point is to catch
deploy-environment issues (missing files, cold-start timeouts, missing env
vars) that in-process tests cannot see.
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any

try:
    import httpx
except ImportError:
    print("ERROR: httpx is required. Run: pip install httpx")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append((name, passed, detail))
    status = "PASS" if passed else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


def assert_status(resp: httpx.Response, expected: int) -> bool:
    if resp.status_code != expected:
        return False
    return True


# ---------------------------------------------------------------------------
# Sample payloads — small, representative, no dependency on expanded dataset
# ---------------------------------------------------------------------------

SAMPLE_CATEGORY = {
    "slug": "dentists",
    "display_name": "Dentists",
    "voice": {"tone": "peer_clinical", "register": "respectful_collegial"},
    "peer_stats": {
        "median_views_per_week": 420,
        "median_calls_per_week": 12,
        "median_ctr": 0.032,
    },
    "vocab_taboo": [],
}

SAMPLE_MERCHANT = {
    "merchant_id": "m_verify_001",
    "category_slug": "dentists",
    "identity": {"name": "Verify Dental Clinic", "city": "Delhi"},
    "subscription": {"status": "active", "plan": "Pro", "days_remaining": 30},
    "performance": {"views_7d": 350, "calls_7d": 8, "ctr": 0.023},
    "signals": ["stale_posts:14d"],
}

SAMPLE_TRIGGER = {
    "id": "trg_verify_001",
    "scope": "merchant",
    "kind": "stale_posts",
    "source": "internal",
    "merchant_id": "m_verify_001",
    "customer_id": None,
    "payload": {"days_since_last_post": 14, "last_post_date": "2026-09-14"},
    "urgency": 3,
    "suppression_key": "verify:stale_posts:m_verify_001",
    "expires_at": "2027-12-31T00:00:00Z",
}


def push_context(client: httpx.Client, base: str, scope: str, cid: str, payload: dict, version: int = 1) -> httpx.Response:
    return client.post(
        f"{base}/v1/context",
        json={
            "scope": scope,
            "context_id": cid,
            "version": version,
            "payload": payload,
            "delivered_at": "2026-09-28T00:00:00Z",
        },
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Main verification sequence
# ---------------------------------------------------------------------------

def run(base_url: str) -> int:
    base = base_url.rstrip("/")
    print(f"\nVerifying deployment at: {base}\n")

    with httpx.Client(base_url=base, timeout=30) as client:

        # ------------------------------------------------------------------ #
        # (a) GET /v1/healthz
        # ------------------------------------------------------------------ #
        print("── /v1/healthz ────────────────────────────────────────────────")
        try:
            t0 = time.time()
            resp = client.get("/v1/healthz")
            latency = time.time() - t0
            ok_status = assert_status(resp, 200)
            check("healthz: HTTP 200", ok_status, f"{int(latency*1000)}ms")
            if ok_status:
                body: Any = resp.json()
                check(
                    "healthz: status == 'ok'",
                    body.get("status") == "ok",
                    repr(body.get("status")),
                )
                check(
                    "healthz: uptime_seconds present",
                    isinstance(body.get("uptime_seconds"), int),
                )
                check(
                    "healthz: contexts_loaded present",
                    isinstance(body.get("contexts_loaded"), dict),
                )
        except Exception as exc:
            check("healthz: reachable", False, str(exc))

        # ------------------------------------------------------------------ #
        # (b) GET /v1/metadata
        # ------------------------------------------------------------------ #
        print("\n── /v1/metadata ───────────────────────────────────────────────")
        try:
            resp = client.get("/v1/metadata")
            ok_status = assert_status(resp, 200)
            check("metadata: HTTP 200", ok_status)
            if ok_status:
                body = resp.json()
                for field in ("team_name", "team_members", "model", "approach", "version"):
                    check(f"metadata: '{field}' present", bool(body.get(field)), repr(body.get(field)))
        except Exception as exc:
            check("metadata: reachable", False, str(exc))

        # ------------------------------------------------------------------ #
        # Teardown before state-dependent tests (idempotent even if first run)
        # ------------------------------------------------------------------ #
        try:
            client.post("/v1/teardown", timeout=10)
        except Exception:
            pass

        # ------------------------------------------------------------------ #
        # (c) POST /v1/context
        # ------------------------------------------------------------------ #
        print("\n── /v1/context ────────────────────────────────────────────────")
        try:
            resp = push_context(client, base, "category", "dentists", SAMPLE_CATEGORY)
            check("context: push category → 200", assert_status(resp, 200), resp.text[:120])

            resp = push_context(client, base, "merchant", "m_verify_001", SAMPLE_MERCHANT)
            check("context: push merchant → 200", assert_status(resp, 200), resp.text[:120])

            resp = push_context(client, base, "trigger", "trg_verify_001", SAMPLE_TRIGGER)
            check("context: push trigger → 200", assert_status(resp, 200), resp.text[:120])

            # Verify accepted:true in response
            body = resp.json()
            check("context: accepted == true", body.get("accepted") is True)

            # Verify 409 for stale version
            resp_stale = push_context(client, base, "merchant", "m_verify_001", SAMPLE_MERCHANT, version=0)
            check(
                "context: stale version → 409",
                assert_status(resp_stale, 409),
                resp_stale.text[:120],
            )
        except Exception as exc:
            check("context: request succeeded", False, str(exc))

        # ------------------------------------------------------------------ #
        # (d) POST /v1/tick
        # ------------------------------------------------------------------ #
        print("\n── /v1/tick ────────────────────────────────────────────────────")
        try:
            t0 = time.time()
            resp = client.post(
                "/v1/tick",
                json={"now": "2026-09-28T09:00:00Z", "available_triggers": ["trg_verify_001"]},
                timeout=30,
            )
            latency = time.time() - t0
            ok_status = assert_status(resp, 200)
            check("tick: HTTP 200", ok_status, f"{int(latency*1000)}ms")
            if ok_status:
                body = resp.json()
                actions = body.get("actions", [])
                check("tick: 'actions' key present", "actions" in body)
                check("tick: at least 1 action returned", len(actions) >= 1, f"got {len(actions)}")
                if actions:
                    action = actions[0]
                    required_keys = {
                        "conversation_id", "merchant_id", "send_as", "trigger_id",
                        "template_name", "template_params", "body", "cta",
                        "suppression_key", "rationale",
                    }
                    missing = required_keys - set(action.keys())
                    check("tick: action has all required keys", not missing, f"missing: {missing}")
                    body_text = action.get("body", "")
                    check("tick: body is non-empty", bool(body_text.strip()), repr(body_text[:60]))
                    check("tick: body contains no URL", "http://" not in body_text and "https://" not in body_text)
                    check("tick: body contains no literal None", "None" not in body_text)
                    # Fact-anchored: must contain at least one digit from merchant facts
                    import re
                    has_number = bool(re.search(r"\d", body_text))
                    check(
                        "tick: body fact-anchored (contains a real number or signal)",
                        has_number or any(sig.replace("_", " ").split(":")[0] in body_text.lower()
                                          for sig in SAMPLE_MERCHANT.get("signals", [])),
                        body_text[:80],
                    )
        except Exception as exc:
            check("tick: request succeeded", False, str(exc))

        # ------------------------------------------------------------------ #
        # (e) POST /v1/reply
        # ------------------------------------------------------------------ #
        print("\n── /v1/reply ───────────────────────────────────────────────────")
        try:
            # Use a conversation_id that won't exist — /v1/reply should still
            # return a sensible response (not a 500 or malformed JSON).
            resp = client.post(
                "/v1/reply",
                json={
                    "conversation_id": "conv_verify_001",
                    "merchant_id": "m_verify_001",
                    "from_role": "merchant",
                    "message": "Sounds good, let's do it!",
                    "received_at": "2026-09-28T09:05:00Z",
                    "turn_number": 2,
                },
                timeout=30,
            )
            ok_status = assert_status(resp, 200)
            check("reply: HTTP 200", ok_status, resp.text[:120])
            if ok_status:
                body = resp.json()
                check("reply: 'action' key present", "action" in body, repr(list(body.keys())))
                check(
                    "reply: action is valid enum",
                    body.get("action") in ("send", "wait", "end"),
                    repr(body.get("action")),
                )

            # Verify that a bad from_role gets a 400, not a 422 or 500
            resp_bad = client.post(
                "/v1/reply",
                json={
                    "conversation_id": "conv_verify_002",
                    "merchant_id": "m_verify_001",
                    "from_role": "admin",  # invalid
                    "message": "test",
                    "received_at": "2026-09-28T09:05:00Z",
                    "turn_number": 1,
                },
                timeout=10,
            )
            check(
                "reply: invalid from_role → 400 (not 422/500)",
                assert_status(resp_bad, 400),
                resp_bad.text[:80],
            )
        except Exception as exc:
            check("reply: request succeeded", False, str(exc))

    # Teardown after test run
    try:
        with httpx.Client(base_url=base, timeout=10) as cleanup:
            cleanup.post("/v1/teardown")
    except Exception:
        pass

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    for name, ok, detail in RESULTS:
        status = "PASS" if ok else "FAIL"
        suffix = f"  ({detail})" if detail and not ok else ""
        print(f"  [{status}] {name}{suffix}")
    print("-" * 60)
    print(f"  {passed} passed, {failed} failed")
    if failed == 0:
        print("\n✅  ALL CHECKS PASSED — deployment is ready for submission.")
        return 0
    else:
        print(f"\n❌  {failed} CHECK(S) FAILED — fix before submitting.")
        return 1


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/verify_deployment.py <base-url>")
        print("  e.g. python scripts/verify_deployment.py https://vera-magicpin.onrender.com")
        return 2
    return run(sys.argv[1])


if __name__ == "__main__":
    raise SystemExit(main())
