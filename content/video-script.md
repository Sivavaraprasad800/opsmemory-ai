# Video script

**Length:** ~3 minutes · **Format:** screen recording + voiceover · **Resolution:** 1080p minimum

Replace `[YOUR NAME]` before recording. Read the narration naturally — do not read it verbatim.

---

## Setup before you hit record

- Terminal font size up, editor font size up
- Notifications off, unrelated tabs closed
- Backend and frontend already running, browser already logged in as `admin@acme.test`
- **Run the learning loop once beforehand** so the Memory page is populated — an empty bank on camera wastes the best moment
- Browser zoom ~110% so text is legible at 1080p

---

## 0:00 – 0:30 · Intro

**ON SCREEN:** `Overview` page. Cursor moving slowly over the service gauges.

> Hi, I'm `[YOUR NAME]`. I built an AI agent that responds to production incidents — and the interesting part isn't that it reasons about them. It's that it remembers which of its own fixes *failed*, and refuses to try them again.

> Everything on this screen is live telemetry from a simulated payments estate: eight services, three environments. Eight services with genuinely different behaviour — that cache cluster and that notification worker aren't clones.

---

## 0:30 – 1:00 · The problem

**ON SCREEN:** Click **How this works** in the sidebar. Scroll to the without-memory / with-memory comparison. Let it sit on screen.

> Here's the failure mode that made this worth building. A payment service starts leaking database connections. The obvious fix is to restart it — that clears the connection pool, and for about ninety seconds the dashboards go green. Then the leak resumes, because the leak was never the pool.

> A stateless agent recommends that restart on Tuesday, watches it fail, and recommends exactly the same thing on Thursday. It has no opinion about what doesn't work here — because it has no *here*.

**ON SCREEN:** Point at the two columns.

> Same incident. Same agent. Two outcomes. The only difference is memory.

---

## 1:00 – 2:00 · The demo — this is the core

**ON SCREEN:** Click **Learning Loop**. Press the run button. Let the progress indicator be visible.

> One click runs the whole loop — observe, understand, investigate, recall, reason, recommend, approve, act, verify, learn, remember, improve. It takes about two minutes, because it's doing real work: real AI investigations and real network writes to the memory layer.

**ON SCREEN:** Stay on the page. Let the steps appear one at a time.

> Watch step three. The system is about to report that a fix *failed*.

**ON SCREEN:** When the `failed_attempt` step appears, pause and read it.

> There it is. A restart cleared the pool, so latency genuinely improved and it *looks* fixed. But the underlying fault was still active, so connections climbed straight back up inside the settle window.

> And notice what it did *not* do — it didn't round that up to a success. Three of the six verification checks are blocking, and "is the underlying fault actually gone" is one of them. No language model decided that. A counter did.

---

## 2:00 – 2:45 · The payoff

**ON SCREEN:** The `incident_2_resolved` step appears. Slow down here. Read the recommendation out loud.

> A similar incident, different environment, days later. This is the whole project:

> It chose `update_known_safe_configuration` — and it explicitly **avoided** `restart_service`, because it remembers that the restart failed.

> Same model. Same prompt. Same tools. Different decision, because of what it remembered. Without memory this is a generic assistant that recommends the same wrong fix forever. With it, it's a colleague who has an opinion.

**ON SCREEN:** Click **Organizational Memory**. Click the **Observations** tab.

> And these aren't claims, they're the bank. Facts, and consolidated observations — each one carrying a proof count and the IDs of the facts that support it. An observation *strengthens* as evidence accumulates instead of being overwritten.

---

## 2:45 – 3:15 · Takeaway

**ON SCREEN:** Back to the `Overview` page, or your face on camera.

> The thing that surprised me: the hard part of agent memory isn't the retrieval. It's the discipline — retaining failures with the same weight as successes, retaining only *after* verification returns, and never letting the model grade its own work.

> Get those three right and the agent genuinely changes its behaviour. Get them wrong and you've built a very expensive way to repeat yourself.

---

## Five YouTube titles

1. I Built an SRE Agent That Remembers Its Failed Fixes
2. My AI Agent Refused to Repeat a Fix That Failed — Here's How
3. Why Agent Memory Is Useless If You Only Store Successes
4. An SRE Agent With Hindsight Memory: Failed Fix, Learned, Fixed
5. I Made an AI Agent Learn From Its Own Mistakes in Production

---

## Thumbnail prompt (for an image generator)

> Generate a viral YouTube thumbnail, 16:9 aspect ratio, for a video about an AI agent that learns from failed production fixes. Dark technical dashboard aesthetic, a red "VERIFICATION FAILED" status card on the left and a green "verified success" card on the right, with an arrow curving between them to suggest learning over time. Bold short text overlay: "IT REMEMBERS." High contrast, readable at small sizes on a phone. Attach a photo of the presenter for the corner.
