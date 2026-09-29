# Demo Checklist — Quick Reference

**Print this or keep it on another screen during your demo.**

---

## Pre-Demo Checklist

- [ ] Backend running: `cd backend && python -m uvicorn app.main:app --port 8000`
- [ ] Frontend running: `cd frontend && npm run dev`
- [ ] Opened: http://127.0.0.1:5173
- [ ] Logged in: `admin@acme.test` / `password123`
- [ ] Practice run done (optional but recommended)

---

## Demo Flow (5 minutes)

| Step | Page | Time | What to Show | Key Point |
|------|------|------|--------------|-----------|
| 1 | **How this works** | 30s | Problem explanation, comparison table | "AI agents that forget are useless during incidents" |
| 2 | **Overview** | 20s | 8 service cards, live metrics | "Real-time monitoring, numbers update live" |
| 3 | **Organizational Memory** | 20s | **0 facts, 0 observations** | "Starting from zero — no pre-loaded data" |
| 4 | **Learning Loop** | 2min | Click "Run", wait for completion | "Real AI running, real memory writing" |
| 5 | **Results** | 2min | Walk through 5 steps | See breakdown below |
| 6 | **Organizational Memory** | 30s | **~41 facts now** | "Memory grew — learning happened" |

---

## Step 5 Breakdown: What to Say for Each Result

### Step 1: Problem Detected ✅
> "AI detected connection pool leak, opened incident automatically."

### Step 2: AI Investigates 🔍
> "Zero similar incidents found. AI recommends restart_service. **47 tool calls.**"

### Step 3: Verification FAILS ❌ **← MOST IMPORTANT**
> "Restart didn't work — verification FAILED. AI admits failure honestly. Stores it in memory."

### Step 4: AI Fixes It ✅
> "AI tries different approach — update configuration. Verification PASSES this time."

### Step 5: Second Incident 🎯 **← THE PUNCHLINE**
> "Similar incident happens again. AI recalls previous failure, **explicitly avoids restart**, jumps to the fix. **31 tool calls** (was 47) = **34% faster.**"

**Point at the JSON:**
```
"failed_action_avoided_in_incident_2": true
"incident_2_recall_hits": 8
```

---

## Key Numbers to Emphasize

| Metric | Before | After | What It Proves |
|--------|--------|-------|----------------|
| **Facts in memory** | 0 | ~41 | Learning happened |
| **Tool calls (Incident 1)** | 47 | — | Baseline effort |
| **Tool calls (Incident 2)** | — | 31 | 34% improvement |
| **Recall hits** | 0 | 8 | Memory was used |
| **Failed action repeated** | ❌ | ✅ Avoided | AI learned the lesson |

---

## What Makes This Demo Convincing

✅ **Visible transformation:** Empty memory → 41 facts (can't fake)  
✅ **Honest failure:** System says "FAILED" not "SUCCESS"  
✅ **Quantifiable improvement:** 47 calls → 31 calls  
✅ **Explicit reasoning:** AI cites previous failure by ID  
✅ **Real-time execution:** 2-minute wait proves it's live  

---

## Questions Judges Might Ask

**Q: "Is the failure scripted?"**  
**A:** No. The simulator has a causal model — restart clears connections but doesn't fix the leak. Verification checks real metrics and sees error rate still high. The failure is real.

**Q: "How do I know memory is really stored?"**  
**A:** Three ways: (1) Memory page shows growth (2) Backend logs show Hindsight API calls (3) You can login to Hindsight dashboard and see the facts.

**Q: "Are logs real or simulated?"**  
**A:** Simulated for reproducibility. But the AI pipeline (detection, parsing, reasoning) is production-ready. To connect real logs, see `PRODUCTION_SETUP.md`.

**Q: "Why should I believe the second incident is actually different?"**  
**A:** Backend logs show two separate incident IDs, different timestamps. The Learning Loop response shows `incident_2_recall_hits: 8` — memory was queried and returned results.

---

## If Something Goes Wrong

| Problem | Quick Fix |
|---------|-----------|
| "Learning loop button doesn't work" | Check backend is running at http://127.0.0.1:8000/docs |
| "Memory still shows 0 facts" | Refresh page, or check backend logs for errors |
| "Second incident recommends same action" | Memory didn't store properly — check `.env` has Hindsight config or leave empty for local fallback |
| "Services don't show metrics" | Frontend not connected to backend — check proxy settings in `vite.config.ts` |

---

## Backup Demo (If Learning Loop Fails)

If the Learning Loop breaks during your demo:

1. Go to **"Incidents"** page — show existing incidents
2. Go to **"Investigations"** page — show AI reasoning for past incidents
3. Go to **"Organizational Memory"** — show existing facts (if any from previous runs)
4. Explain: "The learning loop ran earlier, these are the results"

---

## Post-Demo: What to Show Next

If judges want to dig deeper:

- **"Investigations"** page → Detailed AI reasoning with tool calls
- **"Patterns"** page → System-learned patterns over time
- **"Reliability & Postmortems"** → Auto-generated postmortems
- **Backend logs** → Show real Hindsight API calls
- **Hindsight dashboard** → https://app.hindsight.vectorize.io (if using cloud)

---

## One-Sentence Summary

> "Watch memory grow from 0 to 41 facts, see verification fail honestly, see the second incident avoid the failure and finish 34% faster."

---

## Emergency Contact

If backend crashes or something breaks during demo:

```bash
# Restart backend
cd backend
.\.venv\Scripts\activate
python -m uvicorn app.main:app --port 8000 --reload

# Check health
curl http://127.0.0.1:8000/api/health
```

---

**Good luck with your demo! 🚀**
