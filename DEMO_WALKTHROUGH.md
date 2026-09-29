# Demo Walkthrough: Step-by-Step Guide

**Purpose:** Show this project to judges, evaluators, or anyone in 5 minutes. Follow these exact steps.

---

## Before You Start

✅ Backend running: `cd backend && python -m uvicorn app.main:app --port 8000`  
✅ Frontend running: `cd frontend && npm run dev`  
✅ Open: http://127.0.0.1:5173  
✅ Login: `admin@acme.test` / `password123`

---

## The 5-Minute Demo Script

### **STEP 1: Show "How This Works" Page (30 seconds)**

**Where:** Click **"How this works"** in sidebar (first item)

**What to say:**
> "This is OpsMemory AI — an AI agent that learns from its own failures. Most AI agents forget everything between incidents. This one remembers what it tried, what failed, and what actually fixed things. Let me show you."

**What they see:**
- Explanation of the problem (AI agents that forget)
- The solution (memory-first architecture)
- Comparison table: with/without memory

**Point out:** The table shows an AI recommending the same failed restart twice without memory, vs avoiding it with memory.

---

### **STEP 2: Show Services Running (20 seconds)**

**Where:** Click **"Overview"** in sidebar (under "Respond" section)

**What to say:**
> "This is our simulated production environment — 8 microservices with real-time metrics. The AI monitors these services for anomalies."

**What they see:**
- 8 service cards (payment-service, checkout-service, etc.)
- Each card shows: Health Score, Error Rate, Connections, CPU %, Memory %
- Numbers update every few seconds (live simulation)

**Optional:** Stay here 20 seconds to show numbers changing live. Point to one service: "See the metrics updating in real-time."

---

### **STEP 3: Check Memory is Empty (20 seconds)**

**Where:** Click **"Organizational Memory"** in sidebar (under "Learn" section)

**What to say:**
> "Before we start, let me show you the memory. Right now it's empty — zero facts stored."

**What they see:**
- Big numbers at top: **0 facts, 0 observations, 0 documents**
- Empty graph (or very few items if run before)

**Why this matters:** Starting from zero proves anything we see later came from learning, not pre-loaded data.

---

### **STEP 4: Run the Learning Loop (2 minutes)**

**Where:** Click **"Learning Loop"** in sidebar (under "Learn" section)

**What to say:**
> "Now I'll run the learning loop. This will simulate two similar incidents and show how the AI learns from the first failure. This takes about 2 minutes — real AI is running, real memory is being written."

**Actions:**
1. Click the big blue button: **"Run the learning loop"**
2. Wait for progress bar to complete (~1-2 minutes)

**What they see while waiting:**
- "Running learning loop demonstration..."
- Progress indicator

**Don't skip the wait** — this proves it's not pre-recorded, it's actually running.

---

### **STEP 5: Show the Results (2 minutes)**

**What to say:**
> "Done! Now let me walk through what happened."

**Scroll through the 5 steps and explain each:**

---

#### **Step 1: Problem Detected ✅**
**What to say:**
> "First, the AI detected a problem — connection pool leak on payment-service. It opened an incident automatically."

**What they see:**
- Incident ID, timestamp
- Root cause: "Connection pool exhaustion"
- Anomalies detected

---

#### **Step 2: AI Investigates (No Memory) 🔍**
**What to say:**
> "The AI investigated but found zero similar incidents in memory. So it recommended the obvious first try: restart the service."

**What they see:**
- "No similar incidents found"
- Recommended action: `restart_service`
- Tool calls: 47 (count this, important for later)

**Point out:** "47 tool calls — that's how much work it took."

---

#### **Step 3: Verification FAILS ❌ (CRITICAL)**
**What to say:**
> "Here's the key moment. The AI restarted the service, connections cleared temporarily, but the leak resumed. Verification FAILED. And critically — the AI admits it failed. It doesn't pretend it worked."

**What they see:**
- Verification verdict: `FAILED` ❌
- Error rate didn't normalize (still 8.2%, threshold is 5%)
- "Storing failure in memory with proof attached"

**Why this matters:** Honest failure detection is what makes learning possible.

---

#### **Step 4: AI Re-Investigates and Fixes It ✅**
**What to say:**
> "The AI tried again with a different approach — updated the configuration to fix pool settings. This time verification passed."

**What they see:**
- New action: `update_known_safe_configuration`
- Verification: `PASSED` ✅
- Error rate normalized, leak stopped

---

#### **Step 5: Second Incident — Learning Proof 🎯**
**What to say:**
> "Now the punchline. Another similar incident happened 4 days later. Watch what happens."

**What they see:**
- Second incident: connection pool leak again
- AI recalls previous failure: **8 memory hits**
- AI reasoning: *"restart_service was attempted on INCIDENT-xxx and verification failed. Explicitly avoiding restart."*
- AI jumps straight to: `update_known_safe_configuration`
- **31 tool calls** (vs 47 before) = **34% faster**

**Point at the learning delta:**
```json
"learning_delta": {
  "incident_1_recommended": "restart_service",
  "incident_2_recommended": "update_known_safe_configuration",
  "failed_action_avoided_in_incident_2": true,
  "incident_2_recall_hits": 8
}
```

**What to say:**
> "See that? The AI explicitly avoided the action that failed before. It learned. And it resolved the second incident 34% faster — measurable improvement."

---

### **STEP 6: Show Memory Growth (30 seconds)**

**Where:** Click back to **"Organizational Memory"** in sidebar

**What to say:**
> "Remember we started with zero facts? Look now."

**What they see:**
- **~41 facts** (was 0 before)
- **~11 observations**
- **~6 documents**
- Graph shows connections between incidents, actions, outcomes

**Actions:**
1. Click on a few facts to read them
2. Point out facts like: "restart_service failed on payment-service incident INCIDENT-xxx"

**What to say:**
> "This is real memory — stored externally in Hindsight. It persists across sessions, so the AI gets smarter over time."

---

## Summary (What You Just Proved)

✅ **AI detected problems** (connection pool leak)  
✅ **AI investigated and recommended action** (restart_service)  
✅ **Verification failed honestly** (didn't fake success)  
✅ **AI stored the failure** (memory grew from 0 → 41 facts)  
✅ **AI learned** (second incident avoided failed action)  
✅ **Measurable improvement** (47 calls → 31 calls, 34% faster)  

---

## Optional: Show Other Pages (If You Have Time)

### **Incidents Page**
**Where:** Sidebar → "Incidents" (under "Respond")

**What to show:**
- List of all incidents
- Status, severity, timeline
- Click one to see details

---

### **Investigations Page**
**Where:** Sidebar → "Investigations" (custom link or via incident details)

**What to show:**
- Detailed AI reasoning for each investigation
- Tool calls, evidence, recommendations
- Decision points and why the AI chose each action

---

### **Patterns Page**
**Where:** Sidebar → "Patterns" (under "Learn")

**What to show:**
- System learned patterns over time
- "Services that frequently experience connection pool issues"
- "Actions that historically resolved connection issues"

---

### **Reliability & Postmortems**
**Where:** Sidebar → "Reliability & Postmortems" (under "Learn")

**What to show:**
- Auto-generated postmortems for incidents
- Timeline, root cause, contributing factors, lessons learned
- Action items

---

## Troubleshooting

**Q: "Learning loop button doesn't work"**  
**A:** Check backend is running. Visit http://127.0.0.1:8000/docs to see if API is up.

**Q: "Memory still shows 0 facts after running loop"**  
**A:** Refresh the page. If still 0, check backend logs for errors.

**Q: "Second incident still recommends restart_service"**  
**A:** Means recall didn't work. Check:
- Backend logs show "Hindsight recall()" was called
- Memory page shows facts were stored (should be ~41 facts)
- If using Hindsight Cloud, check API key in `.env`

**Q: "Numbers on Overview page don't change"**  
**A:** That's fine — the demo focuses on the Learning Loop. Overview is just to show services exist.

---

## Tips for a Great Demo

✅ **Practice once** before showing judges — know where each page is

✅ **Don't skip the wait** during learning loop — it proves it's real, not pre-recorded

✅ **Point at specific numbers** — "0 facts became 41 facts", "47 calls became 31 calls"

✅ **Show verification failure** — this is the most important moment, proves honesty

✅ **Let them read Step 5** — the learning delta JSON is powerful visual evidence

✅ **Be honest** — logs are simulated, but the AI pipeline is production-ready

---

## What Makes This Demo Convincing

1. **Visible transformation:** 0 facts → 41 facts (can't fake)
2. **Quantifiable improvement:** 47 calls → 31 calls (measurable)
3. **Honest failure:** Verification says "FAILED" not "SUCCESS" (proves integrity)
4. **Explicit avoidance:** AI reasoning cites previous failure (proves recall)
5. **Real-time execution:** 2-minute wait proves it's not pre-recorded

---

**Bottom line:** Start with empty memory → Run learning loop → Show memory grew and second incident was faster → Done. That's the whole thesis in 5 minutes.
