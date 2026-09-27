# Vera 2.0 — Deterministic Merchant Message Engine

> **⚠️ Submission Contract Notice**
>
> **This project supports both submission contracts** found in the challenge briefs:
> 1. **HTTP-service contract** (`challenge-testing-brief.md`): Exposes `/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata` via a FastAPI app (`bot.app`).
> 2. **File-based contract** (`challenge-brief.md §7`): Exposes a direct `compose(category, merchant, trigger, customer) -> dict` function in `bot.py`, and includes a pre-generated `submission.jsonl` (generated via `build_submission.py`).
>
> You can submit either the public HTTPS URL for the live harness, or the `bot.py` + `submission.jsonl` files depending on what the final submission portal requires. Both are driven by the exact same core deterministic engine.

---

A from-scratch rebuild of Vera's message-composing engine for the magicpin AI Challenge, exposed as the 5-endpoint HTTP service the judge harness expects (`/v1/context`, `/v1/tick`, `/v1/reply`, `/v1/healthz`, `/v1/metadata`, plus optional `/v1/teardown`).

**No LLM is called anywhere in the composition path.** Every message is produced by a deterministic decision layer + a template-based phrasing layer + an anti-hallucination validator, all pure Python, all sub-millisecond. This was a deliberate choice — see [Model / approach choice](#model--approach-choice) below.

---

## Quickstart

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

Sanity-check it against the full expanded dataset (no LLM key needed — this is a structural/quality smoke test, not the scored judge run):

```bash
python scripts/smoke_test.py
```

Run the unit test suite:

```bash
pip install pytest httpx
pytest tests/ -v
```

Run the official (LLM-scored) local judge simulator once you've set an API key in its config block:

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

`dataset/` ships both the original seed files (as given in the challenge zip) and a pre-generated `dataset/expanded/` (50 merchants / 200 customers / 100 triggers / 30 test pairs), produced deterministically by `dataset/generate_dataset.py --seed-dir . --out ./expanded`.

---

## Architecture

```
app/
├── main.py             FastAPI wiring for the 5 endpoints — thin, no business logic
├── store.py             In-memory ContextStore (versioned, idempotent) + SuppressionStore + ConversationStore
├── models.py             Pydantic request models (context payloads stay plain dict — see note below)
├── facts.py              DECISION layer, part 2: per-trigger-kind fact extraction (~30 kinds + generic fallback)
├── decision_engine.py     DECISION layer, part 1: scores & ranks triggers for /v1/tick, orchestrates the pipeline
├── composer.py            PHRASING layer: template rendering per (category, trigger-kind), English + Hinglish
├── validator.py           Anti-hallucination validator + guaranteed-safe fallback template
├── conversation.py        /v1/reply state machine: auto-reply detection, intent transitions, hostility, off-topic
└── language.py            Language-preference resolution (hi_en vs en) + salutation logic
```

### Decide with rules, phrase with templates

This is the non-negotiable design principle from the spec, and the module boundaries above exist specifically to enforce it:

1. **`decision_engine.py`** looks at every trigger the judge says is "available right now", resolves its merchant + category (+ customer), and **scores** it: base score from `urgency`, a bonus if the trigger's `kind` lines up with one of the merchant's own `signals` (confirms the trigger isn't noise), a bonus if it's about to expire, a penalty if this merchant was just messaged very recently and hasn't replied. It sorts, deduplicates to one action per distinct engagement target (`merchant_id`, or `merchant_id::customer_id` for customer-scope triggers) per tick, and caps at 20.
2. **`facts.py`** then extracts, for the *one* trigger selected for each target, the specific facts worth saying — the one digest item, the one number, the one signal — never inventing anything not present in a context payload. Every derived value (a ratio turned into a percentage, a day-count computed from two dates) is explicitly recorded so the validator can pre-approve it.
3. **`composer.py`** takes those facts and arranges them into a sentence, dispatched by `(customer present?, trigger.kind)`. It never touches raw context — only `facts.values`.
4. **`validator.py`** re-extracts every number from the finished body and confirms each one traces back to the source context objects (or an explicitly-derived value); rejects any URL, any category `voice.vocab_taboo` phrase, and any literal `"None"` leak (a robust tripwire for "an extractor field was missing and got interpolated anyway" bugs). On failure, it substitutes a guaranteed-safe fallback message built only from the merchant's own name and the trigger's own `kind` — both always-sourced, so the fallback can never itself fail validation.

If a trigger's payload doesn't have enough real detail for its kind's bespoke template (this happens for filler/placeholder-payload triggers, and will happen for **any trigger kind this build has never seen before** — a real risk during Phase 3's adaptive injection), `facts.py` transparently falls back to a generic extractor that only ever surfaces whatever concrete fields *are* present in the payload plus one real merchant signal. The bot never says something generic-and-empty; it either says something specific or says less.

### Why context payloads are plain `dict`, not typed Pydantic models

The testing brief's schemas are illustrative, not exhaustive — the real dataset already varies field-by-field (not every merchant has `review_themes`; `subscription` has different keys depending on `status`), and Phase 3 explicitly pushes *evolving* payloads. Pydantic models strict enough to validate the documented shape would reject legal partial payloads; models loose enough to accept everything would add no safety. Instead, every field read anywhere in the app goes through the defensive accessor `facts.g(d, *path, default=...)`, which never raises on a missing key — this is what lets the bot "genuinely read live context state at request time" instead of assuming the sample dataset's shape.

### Suppression, conversation state, and the hard constraints

- `ContextStore` is keyed by `(scope, context_id)`, versioned; a strictly-lower version is rejected with `409 stale_version`, an equal version is a no-op `200`, a higher version replaces atomically. Payloads over 500KB are rejected with `400` before they're even parsed.
- `SuppressionStore` tracks two things: per-`suppression_key` expiry (set to the trigger's own `expires_at` when we send), and a separate per-merchant opt-out flag (30 days) set the moment a merchant says "stop messaging me" — this suppresses *every* future trigger for that merchant, not just the current conversation.
- `ConversationStore` tracks turn history, every body already sent (for anti-repetition), and each merchant's last-touch timestamp, used to soft-deprioritize (not hard-block, except for very high urgency) sending to a merchant who was just messaged minutes ago and hasn't replied yet.
- `/v1/tick` enforces the 20-action cap, one action per `(merchant_id[, customer_id])` per tick, and a soft internal time budget (8s) — if a batch of triggers is unusually large, it returns whatever it finished composing rather than risk the judge's 30s timeout.
- `/v1/reply` never lets an unhandled exception surface as invalid JSON — both endpoints are wrapped so a bug degrades to `{"actions": []}` or a safe `wait`, never a malformed response or a 500.
- **Consistent 4xx envelope.** Both `/v1/context` and `/v1/reply` parse the raw request body themselves (`Request`, not a typed Pydantic parameter) and validate required fields by hand, so a malformed request — a missing field, a wrong type — always comes back as `400 {"accepted": false, "reason": "<code>", "details": "<what was wrong>"}`. Neither endpoint ever lets FastAPI's own validation layer fire and return its raw `{"detail": [...]}` 422 shape. (`/v1/tick` keeps a typed `body: TickRequest` parameter deliberately — a malformed tick request has no well-formed "no-op" reply the way context/reply do, and the judge harness's own schemas guarantee tick requests are well-formed, so the extra hand-validation isn't buying anything there.)
- **`from_role` is a closed set, not free text: `{"merchant", "customer"}`.** Nothing in the reply state machine currently branches on who sent the message — the same opt-out/hostile/deferral/intent patterns apply whether the merchant or the customer said them — so treating it as decoration would have been an easy accident. Instead it's validated explicitly: any other value (including a garbage string, a number after JSON parsing, or a missing key) is rejected with `400 {"accepted": false, "reason": "invalid_from_role", ...}`, the same way an invalid `scope` is rejected on `/v1/context`. This was a deliberate choice over "accept anything and treat it as a generic participant" — a `from_role` outside the two real participants in this system is far more likely to be a caller bug than a legitimate new kind of sender, and a loud, cheap-to-fix 400 beats silently accepting it into a field that (today) does nothing.

### `/v1/reply` state machine (`conversation.py`)

Priority order, each one a hard `return` (never falls through to a later rule):

1. **Hostile / explicit opt-out** — always ends immediately and suppresses the merchant for 30 days.
2. **Auto-reply / canned-text detection** — first sighting of a known canned phrase *or* the first exact repeat of the previous incoming message gets one gentle nudge (`send`); a second consecutive identical message backs off with `wait` (24h); a third `end`s the conversation. This exact three-step shape matches the testing brief's own "auto-reply hell" replay example.
3. **Deferral** ("give me time", "call you back") → `wait`.
4. **Explicit action-intent** ("let's do it", "go ahead", a bare "yes" with no question) → switches straight into an action-move template naming a concrete next step for that trigger's kind, **never** re-asking a qualifying question — this is the specific failure mode ("Intent-handoff failure") called out as production Vera's biggest miss.
5. **Off-topic curveball** — a deliberately narrow, explicit list of unrelated-domain markers (GST filing, insurance, legal advice, "can you also help me with…") triggers a one-line polite decline + redirect back to the original trigger. This is intentionally conservative: a genuine follow-up *question about the trigger itself* is not flagged as off-topic (that would be worse than not detecting real curveballs at all) — it falls through to the default acknowledgment instead.
6. **Turn cap** (5 turns) reached without resolution → `end` rather than looping forever.
7. **Default** — acknowledges and proposes the same concrete next step, rotating through 2–3 phrasing variants so turn 3's message is never identical to turn 2's.

Every `send` from this state machine is checked against `sent_bodies` for that conversation and never repeats verbatim (falls back to an explicit "circling back" variant as a last resort if every variant has been used).

---

## Model / approach choice

**No LLM in the composition path — deterministic templates only**, dispatched by `(category, trigger.kind)` and filled exclusively with facts the decision layer verified against source context. This is a stronger fit for this specific challenge than an LLM-based composer, for reasons specific to *this* rubric:

- The spec's own non-negotiable design principle is "decide with rules, phrase with templates" and an explicit anti-hallucination validator with **template fallback** — i.e., the brief itself specifies the deterministic architecture as the target, not merely an acceptable one.
- Zero hallucination risk by construction: the composer physically cannot reference a fact it wasn't handed, and the validator is a second, independent check.
- Sub-millisecond composition — no risk of ever approaching the 30s budget, no dependency on an external API's uptime/rate limits/cost during a scored 60-minute window.
- Fully reproducible for the determinism requirement — the same input always produces the byte-identical output (tested in `tests/test_determinism.py`), which an LLM at temperature 0 usually but not always guarantees across providers/versions.

## Tradeoffs

- **Ceiling on nuance.** A frontier LLM composer can react to genuinely novel phrasing or a subtle merchant question with real comprehension; this engine's `/v1/reply` state machine is keyword/pattern-based and will occasionally misclassify an ambiguous message (e.g. a message that mixes deferral and affirmative intent). The off-topic and action-intent detectors are deliberately tuned conservative (prefer a safe default acknowledgment over a wrong classification) — see `conversation.py`'s docstring for the exact priority order and reasoning.
- **New trigger `kind`s** the engine has never seen get the generic fallback template, not a bespoke one — but the generic fallback now always anchors on at least one real fact from `merchant.performance` or `category.peer_stats` (or a merchant signal), so it never produces a content-free response even for a completely unknown trigger kind. Extending specificity is pure data work: add one function to `facts.py`'s extractor registry and one to `composer.py`'s renderer registry.
- **Regional-language code-mixes we don't fabricate.** `language.py` produces English or Hindi-English (Hinglish) only. For a customer whose `language_pref` is `te-en mix` / `kn-en mix` / `ta-en mix` / a plain regional code, we deliberately stay in English rather than generate Telugu/Kannada/Tamil/Marathi text we have no verified vocabulary for — inventing words in a language we can't actually speak is a worse failure than an honest English fallback. We still honor the *relationship* (first name, warmth, real facts); we just don't fabricate the *language*.
- **The double-text cooldown is a heuristic, not a hard guarantee.** A merchant messaged 10 minutes ago via a lower-scoring trigger can still get a second message this tick if a *much* higher-urgency trigger (urgency 5) fires for them — deliberate (a supply-chain recall shouldn't wait behind cooldown logic), but it is a design choice that trades a small spam risk for never missing something genuinely urgent.

## What additional context would have helped most

1. **A real WhatsApp Business template library** (the actual Kaleyra-approved templates) — right now `template_name`/`template_params` are synthesized plausibly (`vera_{kind}_v1`, `[merchant_name, why_now, body]`) but a real template catalog would let the first-touch-per-24h-window distinction be enforced structurally rather than assumed.
2. **A ground-truth mapping from trigger `kind` → expected specificity** (which fields of the payload the judge expects cited) would remove the guesswork in `facts.py`'s ~30 per-kind extractors, several of which (e.g. `local_news_event`, `weather_heatwave`) had to guess at payload field names since no seed example exercises them.
3. **A larger set of scored example replies** (beyond the 3 replay scenarios) for calibrating the `/v1/reply` state machine's edge cases — e.g. what the "right" answer looks like when a merchant asks a *genuine* clarifying question mid-pitch. The `/v1/reply` state machine now handles this with a dedicated "question detected" branch that re-anchors on the trigger's own payload fact, but calibrating what "good" looks like for specific domains (dentists vs gyms vs restaurants) would require scored examples.

---

## Local test suite

`tests/` covers exactly what the build spec's step 10 asked for:

| File | Covers |
|---|---|
| `tests/test_context.py` | Idempotent posting, the `409 stale_version` case, version-bump replace, `400` for invalid scope / oversized payload, teardown |
| `tests/test_tick.py` | Specific, sourced composition for a merchant- and a customer-facing trigger; customer-context-not-yet-arrived is skipped (never fabricated); active suppression blocks a resend; 20-action cap; one action per merchant per tick; no URLs / no `None` leaks ever; **generic fallback anchors on a real merchant signal or perf number** |
| `tests/test_reply.py` | Accept / decline / hostile+opt-out-suppression / off-topic-stays-on-mission / the full 4-turn auto-reply-hell sequence / intent-transition-skips-qualifying / never-repeats-a-body / turn-cap; **clarifying question re-anchors on trigger fact** |
| `tests/test_determinism.py` | Same `/v1/tick` and `/v1/reply` input twice (fresh conversation/suppression state, same context) → byte-identical output |

`scripts/smoke_test.py` is a non-LLM structural pass over the **entire** 100-trigger expanded dataset — useful for eyeballing every composed message's quality in one run before spending judge-simulator LLM calls.

---

## Submission target

> **Live public base URL (HTTP submission contract):**
> ```
> https://<service-name>.onrender.com
> ```
> *(Replace `<service-name>` with the actual Render slug after deploy — paste this URL into the challenge portal's "Submission URL" field.)*
>
> The file-based contract (`bot.compose()` / `submission.jsonl`) remains in the repo as a **local-testing fallback only**. The authoritative submission is the live HTTPS service above.

---

## Deployment (Render — chosen platform)

**Why Render?** The repo already has a `Procfile` (`web: uvicorn bot:app --host 0.0.0.0 --port $PORT`) and `render.yaml` pinning `numInstances: 1`. Render auto-detects Python, needs zero dashboard configuration beyond connecting the repo, and the free tier gives a persistent public URL for the submission portal.

**Steps:**

1. Push this repo to a public GitHub/GitLab repo (`bot.py`, `app/`, `dataset/expanded/`, `requirements.txt`, `render.yaml` at minimum).
2. On [render.com](https://render.com) → **New +** → **Web Service** → connect the repo.
3. Render reads `render.yaml` automatically — no manual field changes needed.
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `uvicorn bot:app --host 0.0.0.0 --port $PORT`
   - **Instances**: 1 (pinned in `render.yaml` — **do not change**)
4. After deploy completes, run the post-deploy verification script:
   ```bash
   python scripts/verify_deployment.py https://<service-name>.onrender.com
   ```
   All 5 endpoints should report PASS before submitting.

### ⚠️ Cold-start notice (free tier)

Render's free tier suspends the service after ~15 minutes of idle. On the next request, it cold-starts in a few seconds. **If the judge run starts without a warm-up call**, the very first real request may time out.

**Recommendation:** Immediately before the scored window begins, send a single `GET /v1/healthz` ping:
```bash
curl https://<service-name>.onrender.com/v1/healthz
```
Wait for a `{"status": "ok", ...}` response before relying on the service. The judge harness typically calls `/v1/healthz` first — if it does, the cold-start is absorbed harmlessly. If you want zero cold-start risk, upgrade to Render's $7/mo Starter tier or use Railway's free tier (which does not sleep).

### Single-instance constraint

This service stores all conversation/context/suppression state in process-local memory (`app/store.py`). **Running more than 1 worker or replica will silently split state** — e.g. a `/v1/context` push to replica A will not be visible to a `/v1/tick` request that lands on replica B. The `render.yaml` pins `numInstances: 1` and the start command uses uvicorn's default single-worker mode (no `--workers` flag). Do not add `--workers N` or enable auto-scaling.

### Environment variables

This service has **no required environment variables** — the entire composition path is deterministic pure Python with no LLM calls and no external API dependencies. `PORT` is provided automatically by Render and read from the environment by uvicorn.

### Alternative platforms

Both Railway and Fly.io work identically — point the start command at `uvicorn bot:app --host 0.0.0.0 --port $PORT` and lock to 1 replica:

```bash
# Railway
railway init && railway up
# Set start command in dashboard or railway.json to: uvicorn bot:app --host 0.0.0.0 --port $PORT

# Fly.io
fly launch    # accept auto-detected Python config, adjust to bot:app
fly scale count 1   # ensure exactly 1 instance
fly deploy
```

---

## Endpoint reference

See `challenge-testing-brief.md` in the original challenge zip for the full contract this implements. Summary:

| Endpoint | Method | Notes |
|---|---|---|
| `/v1/context` | POST | `{scope, context_id, version, payload, delivered_at}` → `200`/`409`/`400 {"accepted": false, "reason", "details"}` |
| `/v1/tick` | POST | `{now, available_triggers}` → `{actions: [...]}`, max 20, may be empty |
| `/v1/reply` | POST | `{conversation_id, merchant_id, customer_id, from_role, message, received_at, turn_number}` → `send`/`wait`/`end`, or `400 {"accepted": false, "reason", "details"}` if `message`/`from_role`/etc. are missing or invalid — see "Suppression, conversation state, and the hard constraints" above for the exact rules |
| `/v1/healthz` | GET | `{status, uptime_seconds, contexts_loaded}` |
| `/v1/metadata` | GET | team/model/approach metadata |
| `/v1/teardown` | POST | wipes all in-memory state (optional, called at test end) |
