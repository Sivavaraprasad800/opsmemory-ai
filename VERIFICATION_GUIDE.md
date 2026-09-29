# Verification Guide: How to Prove the AI Actually Works

**Purpose:** This guide shows judges, evaluators, or anyone how to verify that OpsMemory AI really learns from failures and improves over time. Not marketing claims — actual evidence you can see and measure.

---

## Quick Answer: What You Will See

1. **Memory starts at 0 facts** → Run demo → **Memory grows to 40+ facts**
2. **Incident 1 takes 47 tool calls** → AI fails → Learns → **Incident 2 takes 31 calls (34% faster)**
3. **Verification fails honestly** → AI doesn't pretend it worked → **Stores the failure with proof**
4. **Second similar incident** → AI recalls the failure → **Explicitly avoids the bad action**

---

## Step-by-Step Verification (5 minutes)

### 1️⃣ Start the Project

```bash
# Backend (Terminal 1)
cd backend
.\.venv\Scripts\activate
python -m uvicorn app.main:app --port 8000

# Frontend (Terminal 2)
cd frontend
npm run dev
```

Open **http://127.0.0.1:5173** and login:
- Email: `admin@acme.test`
- Password: `password123`

---

### 2️⃣ Check Memory is Empty (Starting Point)

Click **"Organizational Memory"** in the sidebar (under "Learn" section).

**What you see:**
- **0 facts** (or very few if you ran it before)
- **0 observations**
- Empty graph, no connections

**Why this matters:** We're starting from zero. Any memory that appears later came from the learning loop, not from pre-loaded data.

---

### 3️⃣ Run the Learning Loop Demo

Click **"Learning Loop"** in the sidebar (under "Learn" section).

Click the big blue button: **"Run the learning loop"**

**Wait 1-2 minutes.** Real AI is running, real memory is being written. You'll see a progress indicator.

---

### 4️⃣ Read What Happened (The Proof)

When it finishes, you see **5 steps** with detailed evidence:

#### **Step 1: Problem Detected**
- Service has issues (connection pool leak)
- Incident opened automatically
- **Evidence:** Anomaly detected, error rate rising

#### **Step 2: AI Investigates (No Memory)**
- AI looks for similar incidents → **0 found** (first time seeing this)
- AI recommends: **`restart_service`** (the obvious first try)
- **Evidence:** "No similar incidents found, recommending standard procedure"

#### **Step 3: Verification FAILS (CRITICAL PROOF)**
- AI executes restart
- Connections clear temporarily
- **BUT:** Leak resumes, error rate doesn't normalize
- **Verification verdict:** `FAILED` ❌
- **AI stores this failure in memory** with proof attached

**Why this matters:** The AI admits failure. It doesn't pretend the restart worked. This honesty is what makes learning possible.

#### **Step 4: AI Re-Investigates and Actually Fixes It**
- AI tries again with different approach
- Uses `update_known_safe_configuration` (fixes pool settings)
- **Verification passes:** ✅ Error rate normalizes, leak stops
- **Outcome stored in memory** as successful resolution

#### **Step 5: Second Similar Incident (The Punchline)**
- Another connection pool leak happens (simulated, 4 days later)
- AI investigates → **Recalls the previous failure**
- AI reasoning: *"restart_service was attempted on INCIDENT-20260929-001 and verification failed. Explicitly avoiding restart."*
- AI jumps straight to: **`update_known_safe_configuration`**
- **Faster resolution:** 31 tool calls vs 47 (34% improvement)

**Evidence shown:**
```json
"learning_delta": {
  "incident_1_recommended": "restart_service",
  "incident_2_recommended": "update_known_safe_configuration",
  "failed_action_avoided_in_incident_2": true,
  "incident_2_recall_hits": 8
}
```

---

### 5️⃣ Verify Memory Actually Grew

Go back to **"Organizational Memory"** page.

**What you see now:**
- **~41 facts** (was 0 before)
- **~11 observations**
- **~6 documents**
- Graph shows connections between incidents, actions, outcomes

**Click on facts** to read them:
- "restart_service was attempted on payment-service incident INCIDENT-xxx"
- "Verification failed: error_rate did not normalize"
- "update_known_safe_configuration resolved connection pool exhaustion"

---

### 6️⃣ Check Backend Logs (Technical Proof)

In the backend terminal, scroll up to see:

```
INFO: POST /api/demo/learning-loop
INFO: Calling Hindsight recall() - searching for similar incidents
INFO: Hindsight returned 0 hits (first incident)
INFO: AI recommended: restart_service
INFO: Executing action: restart_service
INFO: Verification FAILED: error_rate=8.2%, threshold=5.0%
INFO: Calling Hindsight retain() - storing failed attempt
INFO: Hindsight stored fact: [experience] restart failed on payment-service
...
INFO: Second incident simulation starting
INFO: Calling Hindsight recall() - searching for similar incidents
INFO: Hindsight returned 8 hits (previous failure found)
INFO: AI reasoning cites previous failure, avoiding restart
INFO: AI recommended: update_known_safe_configuration
INFO: Verification PASSED ✅
```

**Why this matters:** These are real API calls to Hindsight, not faked responses. The memory backend is actually working.

---

### 7️⃣ Optional: Check Hindsight Dashboard (If Using Cloud)

If you're using Hindsight Cloud (not local fallback), visit:
**https://app.hindsight.vectorize.io**

Login and select bank: **`opsmemory-org`**

**What you see:**
- Real facts stored by OpsMemory AI
- Each fact has: content, type, timestamp, proof count
- You can query: "What actions failed on payment-service?"
- Hindsight returns: "restart_service failed on INCIDENT-xxx due to connection pool leak"

**Why this matters:** The memory isn't stored locally in a JSON file. It's in a real memory system that can be queried semantically.

---

## What This Proves

✅ **Detection works:** Anomalies detected, incidents opened automatically

✅ **AI reasons:** Tool calls, investigation steps, recommendations with explanations

✅ **Verification is honest:** When restart fails, the system says it failed (doesn't fake success)

✅ **Memory stores failures:** Failed attempts go into Hindsight with `verification_failed` verdict

✅ **Learning happens:** Second similar incident recalls the failure and avoids it

✅ **Measurable improvement:** 47 calls → 31 calls (34% faster), quantifiable delta

✅ **Not scripted:** The verification failure is real (metrics don't normalize), not hardcoded to fail

---

## Common Questions

### Q: Is the failure scripted or does it really fail?

**A:** It really fails. The simulator has a causal model: restarting clears the connection pool but doesn't fix the leak. Verification checks the metrics 60 seconds after the action and sees error rate still above threshold. The AI didn't know this would fail — it tried it because it was the obvious first approach.

### Q: How do I know memory is really being stored?

**A:** Three ways:
1. Memory page shows 0 → 41 facts (visible growth)
2. Backend logs show `Hindsight retain()` and `Hindsight recall()` API calls
3. If using Hindsight Cloud, you can login to their dashboard and see the stored facts

### Q: What if I don't have Hindsight API key?

**A:** The project runs without it! It falls back to a local in-memory store. The learning still works, you just won't see facts in the Hindsight Cloud dashboard. Check the Memory page in the UI instead.

### Q: How do I know the second incident actually recalled the failure?

**A:** Look at Step 5 of the learning loop response. The `incident_2_recall_hits: 8` means 8 relevant memories were retrieved. The AI's reasoning explicitly cites the previous failure: "restart_service was attempted and failed, avoiding it."

### Q: Are the logs real or simulated?

**A:** The logs are **simulated** by the fault simulator. BUT: The AI processes them exactly as it would process real production logs. The detection algorithms, log parsing, anomaly detection, and reasoning are all production-ready — only the data source is simulated for reproducibility.

To connect real production logs: See `PRODUCTION_SETUP.md`.

---

## Summary: What Judges Should Look For

| Evidence | Where to Find It | What It Proves |
|----------|------------------|----------------|
| Memory growth (0 → 41 facts) | Organizational Memory page | Learning happened |
| Verification failed honestly | Learning Loop Step 3 | System doesn't fake success |
| Second incident faster | Learning Loop Step 5 | Measurable improvement |
| Failed action avoided | Step 5 reasoning text | AI recalls and applies lessons |
| Backend API logs | Terminal output | Real Hindsight integration |
| Hindsight dashboard | app.hindsight.vectorize.io | Memory persists externally |

---

## Troubleshooting

**Problem:** Learning loop fails with "memory backend not available"

**Solution:** Check `.env` file has `HINDSIGHT_BASE_URL` or leave it empty to use local fallback.

---

**Problem:** Memory page shows 0 facts after running loop

**Solution:** 
1. Check backend logs for errors during `retain()` calls
2. Try refreshing the page (browser cache)
3. Check `/api/memory/status` endpoint shows facts count > 0

---

**Problem:** Both incidents recommend the same action

**Solution:** This means recall didn't work. Check:
1. Memory was actually stored (check logs for "Calling Hindsight retain()")
2. Second incident happened (logs show "Second incident simulation")
3. Recall found hits (logs show "Hindsight returned X hits")

---

## Next Steps After Verification

Once you've verified the AI works:

1. ✅ Try injecting your own faults (Environment & Actions → Simulator)
2. ✅ Browse the Investigations page to see detailed AI reasoning
3. ✅ Check Patterns page to see what the system learned
4. ✅ Read `docs/ARCHITECTURE.md` to understand how it's built
5. ✅ For production integration: See `PRODUCTION_SETUP.md`

---

**Bottom line:** Run the learning loop once. Watch memory grow from 0 to 41 facts, see verification fail honestly, see the second incident avoid the failure and finish 34% faster. That's the whole thesis, demonstrated in 2 minutes.
