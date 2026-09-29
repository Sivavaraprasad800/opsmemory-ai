# Navigation Map: Where to Find Everything

**Quick visual guide to all pages in OpsMemory AI.**

---

## Sidebar Structure

```
┌─────────────────────────────────────────┐
│         OPSMEMORY AI                    │
├─────────────────────────────────────────┤
│                                         │
│  START HERE                             │
│  └─ 📖 How this works ← START HERE     │
│                                         │
│  RESPOND                                │
│  ├─ 🏠 Overview                        │
│  └─ 🚨 Incidents                       │
│                                         │
│  LEARN                                  │
│  ├─ 🔄 Learning Loop ← DEMO PAGE       │
│  ├─ 🧠 Organizational Memory           │
│  └─ 📊 Reliability & Postmortems       │
│                                         │
│  OPERATE                                │
│  └─ ⚙️ Environment & Actions           │
│      └─ Tab: "Simulator & audit"        │
│                                         │
│  ADMIN (if logged as admin)             │
│  └─ 👥 Users                           │
│                                         │
└─────────────────────────────────────────┘
```

---

## Page-by-Page Guide

### 📖 **How this works** (Start here)
**Path:** First item in sidebar

**What's here:**
- Project explanation
- Problem statement (AI agents that forget)
- Comparison table: with/without memory
- Quick start instructions

**Use for:**
- Opening slide of your demo
- Explaining the concept before showing it working

---

### 🏠 **Overview**
**Path:** Sidebar → RESPOND → Overview

**What's here:**
- 8 service cards (payment-service, checkout-service, etc.)
- Live metrics: Health Score, Error Rate, CPU %, Memory %, Connections
- Numbers update every few seconds

**Use for:**
- Showing the "production environment" being monitored
- Demonstrating live data updates
- Context for where incidents come from

---

### 🚨 **Incidents**
**Path:** Sidebar → RESPOND → Incidents

**What's here:**
- List of all incidents (past and current)
- Status: open, investigating, resolved, closed
- Severity, timeline, affected services
- Click an incident to see full details

**Use for:**
- Showing history of detected problems
- Drilling into specific incident details
- Proving detection works

---

### 🔄 **Learning Loop** ⭐ MAIN DEMO PAGE
**Path:** Sidebar → LEARN → Learning Loop

**What's here:**
- Big button: "Run the learning loop"
- Progress indicator during run
- 5-step results after completion:
  1. Problem detected
  2. AI investigates (no memory)
  3. Verification fails
  4. AI fixes it
  5. Second incident faster (learning proof)

**Use for:**
- **This is your main demo!**
- Proving learning happens
- Showing measurable improvement (47 → 31 calls)

---

### 🧠 **Organizational Memory** ⭐ PROOF OF LEARNING
**Path:** Sidebar → LEARN → Organizational Memory

**What's here:**
- Count of facts, observations, documents
- Knowledge graph visualization
- List of stored facts (click to read)
- Entities and relationships

**Use for:**
- Showing memory growth (0 → 41 facts)
- Proving learning isn't faked
- Reading actual stored memories
- **Check BEFORE and AFTER running learning loop**

---

### 📊 **Reliability & Postmortems**
**Path:** Sidebar → LEARN → Reliability & Postmortems

**What's here:**
- Auto-generated postmortems for resolved incidents
- Timeline of events
- Root cause analysis
- Contributing factors
- Lessons learned
- Action items

**Use for:**
- Showing structured incident documentation
- Proving the system understands causality
- Optional: show after main demo if time permits

---

### ⚙️ **Environment & Actions**
**Path:** Sidebar → OPERATE → Environment & Actions

**What's here:**
- Two tabs:
  - **"Simulator & audit"** — Inject faults manually, view audit log
  - **"Actions"** — View all available remediation actions

**Use for:**
- (Optional) Manually trigger incidents
- Show safety guardrails on actions
- **Usually skip this in demo** (Learning Loop is simpler)

---

### 👥 **Users** (Admin only)
**Path:** Sidebar → ADMIN → Users

**What's here:**
- List of all users
- Roles: admin, sre, analyst, viewer
- Add/edit users

**Use for:**
- (Optional) Showing role-based access control
- Usually not needed in demo

---

## Demo Path (Recommended Order)

```
1. How this works (30s)
   ↓
2. Overview (20s)
   ↓
3. Organizational Memory (20s) ← Check it's empty
   ↓
4. Learning Loop (2min) ← Run the demo
   ↓
5. Read 5 steps (2min) ← Explain each
   ↓
6. Organizational Memory (30s) ← Show growth

Total: ~5 minutes
```

---

## Optional Extensions (If You Have Time)

### Path A: Show Detailed Investigation
```
Learning Loop → Incidents → Click incident → Investigation details
```
**What they see:** Full AI reasoning, tool calls, evidence, decision points

---

### Path B: Show Learned Patterns
```
Learning Loop → (sidebar) Patterns
```
**What they see:** System-discovered patterns like "Services prone to connection issues"

---

### Path C: Show Postmortem
```
Learning Loop → Reliability & Postmortems → Click a postmortem
```
**What they see:** Full incident report with timeline, root cause, lessons

---

### Path D: Manual Fault Injection (Advanced)
```
Environment & Actions → Simulator & audit tab → Select service → Inject fault
→ Watch Overview page metrics change
→ Wait for incident detection
→ Go to Incidents page
```
**What they see:** Real-time detection and response (but takes longer than Learning Loop)

---

## Key Pages for Judges to Verify Learning

| What to Verify | Where to Look |
|----------------|---------------|
| Memory starts empty | Organizational Memory (before loop) |
| Memory grows | Organizational Memory (after loop) |
| Verification fails honestly | Learning Loop → Step 3 result |
| Second incident faster | Learning Loop → Step 5 result |
| Failed action avoided | Learning Loop → Step 5 reasoning text |
| Backend API calls | Terminal output (backend logs) |

---

## Quick Navigation Tips

1. **Sidebar is always visible** on the left
2. **Sections collapse/expand** — click section headers
3. **Current page highlighted** in sidebar
4. **Breadcrumbs** at top show where you are
5. **Back button works** — use browser back if needed

---

## Troubleshooting Navigation

**Q: "I don't see Organizational Memory page"**  
**A:** It's under the "LEARN" section. Click "LEARN" to expand, then click "Organizational Memory".

**Q: "Where's the Platform page?"**  
**A:** It's called "Environment & Actions" under "OPERATE" section.

**Q: "I don't see Learning Loop button"**  
**A:** Make sure you're on the "Learning Loop" page (LEARN section). The button is at the top of the page.

**Q: "Where do I see investigation details?"**  
**A:** Go to Incidents page → Click any incident → Scroll down to see investigation section.

---

## Mobile/Small Screen Note

On narrow screens:
- Sidebar becomes a hamburger menu (☰)
- Click hamburger to show/hide sidebar
- Navigation otherwise works the same

---

**Print this page and keep it next to you during the demo!**
