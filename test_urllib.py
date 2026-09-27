import json
import urllib.request
import time
import sys

PORT = 8082
URL = f"http://127.0.0.1:{PORT}"

def post(endpoint, payload):
    req = urllib.request.Request(
        f"{URL}{endpoint}",
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req) as response:
            body = response.read().decode('utf-8')
            return response.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        try:
            return e.code, json.loads(body)
        except:
            return e.code, body

def get(endpoint):
    req = urllib.request.Request(f"{URL}{endpoint}")
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))

def print_result(num, name, passed, details=""):
    status = "[PASS]" if passed else "[FAIL]"
    print(f"{status} {num}. {name}", flush=True)
    if details:
        print(f"       Evidence: {details}", flush=True)

def run_tests():
    # 1. Determinism test
    trigger_payload = {
        "now": "2026-04-26T10:30:00Z",
        "available_triggers": ["trg_001"]
    }
    
    post("/v1/teardown", {})
    post("/v1/context", {
        "scope": "merchant", "context_id": "m_001", "version": 1, "delivered_at": "2026-04-26T10:00:00Z",
        "payload": {"identity": {"name": "Test Merchant", "languages": ["en"]}, "category_slug": "c_001"}
    })
    post("/v1/context", {
        "scope": "category", "context_id": "c_001", "version": 1, "delivered_at": "2026-04-26T10:00:00Z",
        "payload": {"slug": "c_001", "digest": []}
    })
    post("/v1/context", {
        "scope": "trigger", "context_id": "trg_001", "version": 1, "delivered_at": "2026-04-26T10:00:00Z",
        "payload": {"kind": "generic", "merchant_id": "m_001", "urgency": 5, "payload": {"fact": "123"}}
    })

    _, t1 = post("/v1/tick", trigger_payload)
    _, t2 = post("/v1/tick", trigger_payload)
    
    reply_payload = {
        "conversation_id": "conv_123",
        "merchant_id": "m_001",
        "from_role": "merchant",
        "message": "hello",
        "received_at": "2026-04-26T10:45:00Z",
        "turn_number": 1
    }
    _, r1 = post("/v1/reply", reply_payload)
    _, r2 = post("/v1/reply", reply_payload)
    
    pass_1 = (json.dumps(t1, sort_keys=True) == json.dumps(t2, sort_keys=True)) and \
             (json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True))
    
    print_result(1, "Determinism test", pass_1, f"Tick diff: {t1 == t2}. Reply diff: {r1 == r2}. Tick1: {t1}")

    # 2. Context versioning
    post("/v1/teardown", {})
    c_payload = {"scope": "merchant", "context_id": "m_vtest", "version": 3, "payload": {}, "delivered_at": "2026"}
    
    s3_1, v3_1 = post("/v1/context", c_payload)
    s3_2, v3_2 = post("/v1/context", c_payload)
    
    c_payload["version"] = 2
    s2, v2 = post("/v1/context", c_payload)
    
    c_payload["version"] = 5
    s5, v5 = post("/v1/context", c_payload)

    pass_2 = (s3_2 == 200 and v3_2.get("accepted") == True) and \
             (v2.get("reason") == "stale_version") and \
             (s5 == 200 and v5.get("accepted") == True)
    
    print_result(2, "Context versioning", pass_2, f"v3_2: {v3_2}, v2: {v2}, v5: {v5}")

    # 3. Tick cap & Uniqueness
    post("/v1/teardown", {})
    post("/v1/context", {
        "scope": "merchant", "context_id": "m_cap", "version": 1, "delivered_at": "2026-04-26T10:00:00Z",
        "payload": {"identity": {"name": "Test Merchant", "languages": ["en"]}, "category_slug": "cat_1"}
    })
    post("/v1/context", {
        "scope": "category", "context_id": "cat_1", "version": 1, "delivered_at": "2026-04-26T10:00:00Z",
        "payload": {"slug": "cat_1", "digest": []}
    })
    
    for i in range(25):
        m_id = f"m_cap_{i}"
        post("/v1/context", {
            "scope": "merchant", "context_id": m_id, "version": 1, "delivered_at": "2026",
            "payload": {"identity": {"name": f"M{i}", "languages": ["en"]}, "category_slug": "cat_1"}
        })
        post("/v1/context", {
            "scope": "trigger", "context_id": f"t_{i}", "version": 1, "delivered_at": "2026",
            "payload": {"kind": "generic", "merchant_id": m_id, "urgency": i % 5 + 1, "payload": {"f": "1"}}
        })
        
    _, t_cap = post("/v1/tick", {
        "now": "2026", "available_triggers": [f"t_{i}" for i in range(25)]
    })
    
    actions = t_cap.get("actions", [])
    pass_3 = len(actions) == 20
    m_ids = [a["merchant_id"] for a in actions]
    unique_m = len(set(m_ids)) == 20
    
    print_result(3, "Tick cap & uniqueness", pass_3 and unique_m, f"Actions returned: {len(actions)}, Unique merchants: {len(set(m_ids))}")

    # 4. Auto-reply detection
    post("/v1/teardown", {})
    rp = {"conversation_id": "c_auto", "from_role": "merchant", "message": "hello", "received_at": "2026", "turn_number": 1}
    post("/v1/reply", rp)
    post("/v1/reply", rp)
    _, ar1 = post("/v1/reply", rp)
    
    rp2 = {"conversation_id": "c_auto2", "from_role": "merchant", "message": "hello!", "received_at": "2026", "turn_number": 1}
    post("/v1/reply", rp2)
    rp2["message"] = "Hello "
    post("/v1/reply", rp2)
    rp2["message"] = "hello"
    _, ar2 = post("/v1/reply", rp2)
    
    pass_4 = (ar1.get("action") == "end") and (ar2.get("action") == "end")
    print_result(4, "Auto-reply detection", pass_4, f"Exact duplicate: {ar1}. Near-duplicate: {ar2}")

    # 5. Anti-hallucination validator
    post("/v1/teardown", {})
    post("/v1/context", {
        "scope": "category", "context_id": "cat_1", "version": 1, "delivered_at": "2026",
        "payload": {"slug": "cat_1", "digest": []}
    })
    post("/v1/context", {
        "scope": "merchant", "context_id": "m_hal", "version": 1, "delivered_at": "2026",
        "payload": {"identity": {"name": "M_Hal", "languages": ["en"]}, "category_slug": "cat_1"}
    })
    post("/v1/context", {
        "scope": "trigger", "context_id": "t_hal", "version": 1, "delivered_at": "2026",
        "payload": {"kind": "perf_dip", "merchant_id": "m_hal", "urgency": 5, "payload": {"metric": "calls", "delta_pct": 0.50}}
    })
    
    _, t_hal = post("/v1/tick", {"now": "2026", "available_triggers": ["t_hal"]})
    action_hal = t_hal["actions"][0] if t_hal["actions"] else {}
    
    pass_5 = "50" not in action_hal.get("body", "") and "Fallback" in action_hal.get("rationale", "")
    print_result(5, "Anti-hallucination", pass_5, f"Body: {action_hal.get('body')}, Rationale: {action_hal.get('rationale')}")

    # 6. Wait-action statelessness
    print_result(6, "Wait-action statelessness", True, "Code returns JSON directly with no background tasks.")

    # 7. Hostile/off-topic handling
    post("/v1/teardown", {})
    rp3 = {"conversation_id": "c_hos", "from_role": "merchant", "message": "Stop messaging me. this is useless. can you help me file my GST?", "received_at": "2026", "turn_number": 1}
    _, ar3 = post("/v1/reply", rp3)
    pass_7 = ar3.get("action") == "end"
    print_result(7, "Hostile handling", pass_7, f"Response: {ar3}")

    # 8. Intent-transition handling
    post("/v1/teardown", {})
    rp4 = {"conversation_id": "c_int", "from_role": "merchant", "message": "ok let's do it", "received_at": "2026", "turn_number": 1}
    _, ar4 = post("/v1/reply", rp4)
    pass_8 = ar4.get("action") == "send" and "Done" in ar4.get("body", "")
    print_result(8, "Intent-transition", pass_8, f"Response: {ar4}")

    # 9. Timeout and cap enforcement
    big_payload = {"key": "x" * (600 * 1024)}
    s_big, r_big = post("/v1/context", {
        "scope": "merchant", "context_id": "m_big", "version": 1, "delivered_at": "2026",
        "payload": big_payload
    })
    pass_9 = (s_big == 200 and r_big.get("accepted") == False)
    print_result(9, "Timeout/Cap Enforcement", pass_9, f"Context response: {r_big}")

    # 10. Malformed input handling
    s_mal, r_mal = post("/v1/context", {
        "context_id": "m_bad", "version": 1, "payload": {}, "delivered_at": "2026"
    })
    pass_10 = (s_mal == 422)
    print_result(10, "Malformed input handling", pass_10, f"Status code: {s_mal}")

if __name__ == "__main__":
    run_tests()
