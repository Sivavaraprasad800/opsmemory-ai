# Video Recording Steps — Simple Guide

**How to record your demo video for hackathon submission.**

---

## Before Recording

### ✅ Prepare Your Computer

1. **Close unnecessary programs** (Slack, Discord, email, etc.)
2. **Clean your desktop** (hide personal files if sharing full screen)
3. **Turn off notifications** (Windows: Settings → System → Notifications)
4. **Check audio** (speak into microphone, listen back)
5. **Have script ready** (open `content/video-script.md` on another screen or phone)

---

### ✅ Start the Application

**Terminal 1 (Backend):**
```bash
cd backend
.\.venv\Scripts\activate
python -m uvicorn app.main:app --port 8000
```

**Terminal 2 (Frontend):**
```bash
cd frontend
npm run dev
```

**Browser:**
- Open: http://127.0.0.1:5173
- Login: `admin@acme.test` / `password123`
- Go to **"Organizational Memory"** page
- Check it shows **0 facts** (if not, that's okay — just note the starting number)

---

## Recording Method (Windows)

### Option 1: Windows Game Bar (Built-in, Easiest)

1. Press **Win + G** (opens Game Bar)
2. Click **"Capture"** button (camera icon)
3. Click **"Start Recording"** (round record button)
4. Record your demo (follow script below)
5. Press **Win + Alt + R** to stop recording
6. Video saved to: `C:\Users\[YourName]\Videos\Captures\`

### Option 2: OBS Studio (Free, Professional)

1. Download: https://obsproject.com/
2. Install and open OBS
3. Add source: "Display Capture" (full screen) or "Window Capture" (browser only)
4. Click **"Start Recording"**
5. Record your demo (follow script below)
6. Click **"Stop Recording"**
7. Video saved to: Check OBS settings for save location

---

## What to Record (5 Minutes)

### 🎬 Part 1: Introduction (30 seconds)

**Show:** Any slide or just speak to camera (optional)

**Say:**
> "Hi, I'm [your name]. This is OpsMemory AI — an AI incident response agent that learns from its own failures. Most AI agents forget everything between incidents. This one remembers what it tried, what failed, and what actually fixed things. Let me show you how it works."

**Tip:** You don't need to show your face — just voice with screen recording is fine!

---

### 🎬 Part 2: Show the Problem (30 seconds)

**Show:** "How this works" page (first in sidebar)

**Say:**
> "Here's the problem: An AI agent that forgets is useless during incidents. It will recommend the same failed restart over and over. We solve this with a memory-first architecture. Watch this comparison table."

**Point at the table** (move mouse cursor):
> "Without memory: Same failed restart twice. With memory: AI recalls the failure and avoids it. Now let me prove this actually works."

---

### 🎬 Part 3: Starting Point — Empty Memory (20 seconds)

**Show:** Click **"Organizational Memory"** in sidebar

**Say:**
> "First, let's check the memory. Right now we have zero facts — completely empty. No pre-loaded data. Anything we see later came from real learning."

**Point at the numbers** (move cursor):
> "Zero facts, zero observations. Starting from scratch."

---

### 🎬 Part 4: Run the Learning Loop (2 minutes)

**Show:** Click **"Learning Loop"** in sidebar

**Say:**
> "Now I'll run the learning loop. This simulates two similar incidents and shows how the AI learns from the first failure."

**Click:** "Run the learning loop" button

**Say while waiting:**
> "This takes about 2 minutes because real AI is running — real reasoning, real memory being written. I'll wait for it to complete... [pause 10 seconds] ... While we wait, I'll explain what's about to happen. The AI will detect a problem, recommend a fix, the fix will fail, and then the AI will store that failure in memory. Then a similar incident happens, and the AI will recall the failure and avoid it. Let's see the results..."

**Keep talking or stay silent** — either is fine. Just wait for the progress bar to finish.

---

### 🎬 Part 5: Explain Results (2 minutes)

**Show:** Scroll through the 5 steps slowly

**Say:**

**Step 1:**
> "Okay, results are in. Step 1: Problem detected — connection pool leak on payment service. Incident opened automatically."

**Step 2:**
> "Step 2: AI investigates. Notice it says 'zero similar incidents found' — no memory yet. So it recommends the obvious first try: restart the service. That took 47 tool calls."

**Step 3:** (IMPORTANT — speak slowly here)
> "Step 3 is the key moment. The AI restarted the service, connections cleared temporarily, but the leak resumed. Verification FAILED. And critically — the AI admits it failed. It doesn't pretend it worked. This honest failure detection is what makes learning possible."

**Step 4:**
> "Step 4: AI tries again with a different approach — updates the configuration to fix the pool settings. This time verification passed."

**Step 5:** (THE PUNCHLINE — speak clearly)
> "Step 5: Another similar incident happens 4 days later. Watch what happens. The AI recalls the previous failure — 8 memory hits. It cites the failure by incident ID. And it explicitly avoids the restart. It jumps straight to the configuration fix. Result: 31 tool calls instead of 47 — that's 34% faster."

**Point at the JSON** (move cursor to highlight):
> "See this: 'failed action avoided in incident 2: true.' That's proof the AI learned the lesson."

---

### 🎬 Part 6: Show Memory Growth (30 seconds)

**Show:** Click back to **"Organizational Memory"**

**Say:**
> "Remember we started with zero facts? Look now. [pause] 41 facts, 11 observations, 6 documents. This is real memory — stored in Hindsight. It persists across sessions."

**Click on a fact** (expand one to read):
> "Here's an example: 'restart service failed on payment service incident' — with the timestamp and proof attached. The AI can query this later and avoid the same mistake."

---

### 🎬 Part 7: Conclusion (30 seconds)

**Show:** Any page or just the memory page

**Say:**
> "So that's OpsMemory AI. We proved: memory grew from zero to 41 facts, verification failed honestly, and the second incident was 34% faster. This is an AI agent that learns from its own failures. Thank you for watching!"

---

## Tips for Good Recording

✅ **Speak clearly and slowly** — Judges may not be native English speakers

✅ **Move your mouse cursor** — Point at things as you explain them

✅ **Pause briefly** — Give viewers time to read numbers on screen

✅ **Don't apologize** — If you stumble, just keep going or restart

✅ **Show excitement** — You built something cool! Let your voice show it

✅ **Keep it under 5 minutes** — Judges watch many videos, shorter is better

✅ **Test your mic** — Record 10 seconds first, play it back, check volume

---

## After Recording

### 1. Watch the Video

- Play it back fully
- Check: Can you hear your voice? Can you see the screen clearly?
- If not: Record again (it's okay! Second take is usually better)

---

### 2. Edit (Optional)

**If video is too long:**
- Use Windows Video Editor (built-in) or any video editor
- Cut out long waiting periods
- Keep it under 5 minutes

**If you made mistakes:**
- Small stumbles are fine! Judges understand English may not be first language
- Only re-record if something major is wrong (app crashed, can't hear audio, etc.)

---

### 3. Upload to YouTube

1. Go to: https://youtube.com/upload
2. Sign in with Google account
3. Click **"Upload video"**
4. Select your video file
5. **Title:** "OpsMemory AI — AI Incident Response with Learning Memory"
6. **Description:** Copy from `content/video-script.md` (intro section)
7. **Visibility:** 
   - If hackathon requires public: **Public**
   - If you prefer: **Unlisted** (only people with link can watch)
8. Click **"Publish"**
9. Copy the video URL (example: `https://youtu.be/abc123xyz`)

---

### 4. Add to Your Submission

Put the YouTube link in your hackathon submission form.

**Also include in your GitHub README:**
```markdown
## Demo Video

Watch the 5-minute demo: [YouTube Link](https://youtu.be/your-video-id)
```

---

## Troubleshooting

**Problem: Can't hear my voice in recording**
- **Fix:** Check Windows microphone settings
- **Fix:** Speak louder, closer to mic
- **Fix:** Use headset mic instead of laptop mic

**Problem: Screen looks blurry**
- **Fix:** Record in 1080p if your screen supports it
- **Fix:** Close browser zoom (should be 100%, not zoomed in/out)

**Problem: Recording is too big to upload**
- **Fix:** Use Windows Video Editor to compress
- **Fix:** OBS settings: Output → Recording Quality → "High Quality, Medium File Size"

**Problem: Learning loop failed during recording**
- **Fix:** Stop recording, restart backend, run learning loop once to test, then record again

**Problem: I don't like my voice**
- **Fix:** Everyone feels this way! Judges care about the project, not your voice
- **Fix:** If really uncomfortable, record without voice and add text captions (but voice is better)

---

## Checklist Before Submitting Video

- [ ] Video is 3-5 minutes long
- [ ] Can clearly hear voice
- [ ] Can clearly see screen and text
- [ ] Shows memory growing (0 → 41 facts)
- [ ] Shows Step 3 (verification failed)
- [ ] Shows Step 5 (second incident faster)
- [ ] Explains why this matters (learning from failures)
- [ ] Uploaded to YouTube
- [ ] Link added to submission form
- [ ] Link added to GitHub README

---

## Example Timeline (for reference)

```
0:00 - 0:30   Introduction
0:30 - 1:00   Show "How this works" page
1:00 - 1:20   Show empty memory (0 facts)
1:20 - 1:40   Start learning loop (click button)
1:40 - 3:40   Wait + explain what's happening
3:40 - 4:10   Explain Step 3 (failure)
4:10 - 4:40   Explain Step 5 (learning)
4:40 - 5:10   Show memory growth
5:10 - 5:30   Conclusion
```

Total: ~5 minutes

---

## You Got This! 🚀

Recording feels scary at first, but:
- Judges are technical people who care about your project, not your presentation skills
- They watch dozens of videos — clear and short is better than polished and long
- Small mistakes are fine — they show it's real, not scripted
- You built something impressive — just show it working!

**Take a deep breath, press record, and follow the script. Good luck!** 🎥

---

## Need Help?

If something goes wrong during recording:
1. Check that both backend and frontend are still running
2. Refresh the browser page
3. Check backend terminal for error messages
4. If learning loop fails: Restart backend and try again before recording

**Remember:** You can record as many takes as you need. Delete the bad ones, keep the good one!
