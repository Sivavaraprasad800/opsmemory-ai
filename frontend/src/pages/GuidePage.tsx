import { Link } from "react-router-dom";
import { Badge, Card } from "../components/ui";

const DEMO_STEPS: { title: string; do: string; say: string }[] = [
  {
    title: "Sign in",
    do: "Use admin@acme.test with password123.",
    say: "Everything here runs against a simulated payments platform, so nothing on this screen can touch a real system. Admin can inject faults; the other three logins cannot.",
  },
  {
    title: "Look at Overview",
    do: "Click Overview in the sidebar. This is where the page starts.",
    say: "Eight services, live gauges, green means healthy. Right now nothing is broken. Notice the four numbers at the top move the moment the assistant learns something.",
  },
  {
    title: "Start the story",
    do: 'Go to Learning Loop, then click "Run the complete learning loop". Leave the tab open — it takes about two minutes because it is really investigating, not replaying a recording.',
    say: "It is injecting a connection leak into payment-service, detecting it with its own anomaly detection, then investigating with an AI model that has no memory of this ever happening before.",
  },
  {
    title: "Read the five steps that appear",
    do: "When it finishes, scroll the numbered steps.",
    say: "Step 3 is the whole point. A human applies the obvious fix — restart the service — and the system verifies it and says it FAILED. Restarting clears the pool but the leak comes straight back. That failure is now a permanent memory.",
  },
  {
    title: "Show what it remembered",
    do: "Open Organizational Memory, then Incidents, then the AI investigations tab.",
    say: "Those are the lessons it kept: the facts it extracted, and the consolidated beliefs with a proof count. When the second incident arrives, it recalls the failed restart and skips straight to the fix that worked. Same problem, better answer, because it remembers.",
  },
];

const PAGES: { to: string; name: string; what: string }[] = [
  {
    to: "/",
    name: "Overview",
    what: "The dashboard. Is anything broken, is the estate healthy, and how much does the assistant remember.",
  },
  {
    to: "/incidents",
    name: "Incidents",
    what: "What has gone wrong. Open one to see its evidence, the AI's reasoning, the fix it proposed, and whether that fix was verified.",
  },
  {
    to: "/demo",
    name: "Learning Loop",
    what: "The demo. One button runs the entire story from alert to remembered lesson.",
  },
  {
    to: "/memory",
    name: "Organizational Memory",
    what: "The long-term memory itself. What is stored, and a search box you can query live in front of an audience.",
  },
  {
    to: "/learning",
    name: "Reliability & Postmortems",
    what: "What the assistant learned across many incidents: repeat failure patterns, and written postmortems.",
  },
  {
    to: "/environment",
    name: "Environment & Actions",
    what: "The world it operates in: the services watched, the fixes it is allowed to propose, and an audit trail of every action.",
  },
];

const GLOSSARY: { term: string; plain: string }[] = [
  { term: "Incident", plain: "Something broke. The system opens one automatically when metrics go wrong." },
  { term: "Hindsight", plain: "The memory service. It stores what happened, in plain language, permanently — so it survives restarts and is shared by the whole team." },
  { term: "Retain", plain: "Saving a new lesson into that memory." },
  { term: "Recall", plain: "Searching the memory for something relevant to what is happening now." },
  { term: "Fact", plain: "One small true statement pulled out of an incident, e.g. \"a restart did not fix the leak\"." },
  { term: "Observation", plain: "A belief the system formed by combining many facts, showing how many facts back it up. It is refined as new evidence arrives, never silently overwritten." },
  { term: "Hypothesis", plain: "A possible explanation the AI is testing. It is not allowed to state a cause until the evidence supports it." },
  { term: "Verification", plain: "Checking whether a fix actually worked, by measuring the metrics before and after. A fix is never called successful because it was applied." },
  { term: "Learning loop", plain: "The full cycle: detect → investigate → propose → get approval → act → verify → learn → remember." },
  { term: "Autonomy level", plain: "How much the AI may do without a human approving first. Level 4 asks for approval; level 5 is narrowly allowed to act on its own for low-risk fixes." },
  { term: "Degraded", plain: "The assistant is running without one of its usual parts — often the memory service — so it falls back to a local store. It still works, and it says so instead of hiding it." },
];

export default function GuidePage() {
  return (
    <div className="guide">
      <section className="guide-hero">
        <Badge tone="accent">Start here</Badge>
        <h1>An AI assistant that remembers what it learned</h1>
        <p className="guide-lede">
          OpsMemory AI helps on-call engineers when something breaks. It investigates the problem,
          proposes a fix, checks whether that fix actually worked, and remembers the outcome. Next
          time a similar problem appears it starts from experience instead of from zero.
        </p>
        <p className="guide-lede muted">
          That is the entire product in two sentences. Everything else on this site is evidence for
          those two sentences.
        </p>
      </section>

      <section className="guide-compare">
        <div className="compare-card without">
          <h3>Without memory</h3>
          <p className="compare-sub">An ordinary AI assistant</p>
          <ul>
            <li>Every incident starts cold. Nothing from last month is available.</li>
            <li>It suggests the most common fix it has read about: restart the service.</li>
            <li>It has no idea that a restart was already tried here and made things worse.</li>
            <li>It cannot tell you whether its advice worked — only that it gave it.</li>
          </ul>
        </div>
        <div className="compare-card with">
          <h3>With memory</h3>
          <p className="compare-sub">OpsMemory AI</p>
          <ul>
            <li>It recalls what was tried before on this service, in this environment.</li>
            <li>It skips the fix it knows failed, and says which lesson it is relying on.</li>
            <li>It states a cause only when the evidence supports it, and says so plainly.</li>
            <li>It verifies its fix against real metrics, and stores the verdict either way.</li>
          </ul>
        </div>
      </section>

      <Card
        title="Give the demo"
        subtitle="About four minutes, five steps. Click in this order and say this out loud."
      >
        <ol className="demo-script">
          {DEMO_STEPS.map((step, index) => (
            <li key={step.title}>
              <div className="demo-script-num">{index + 1}</div>
              <div className="demo-script-body">
                <div className="demo-script-title">{step.title}</div>
                <div className="demo-script-do">{step.do}</div>
                <div className="demo-script-say">
                  <span className="demo-script-say-label">Say</span>
                  <span>{step.say}</span>
                </div>
              </div>
            </li>
          ))}
        </ol>
        <div className="guide-tip">
          <strong>If it is your first run:</strong> the memory is meant to be empty at the start —
          that is what makes the learning visible. Running the loop twice in a row is even more
          convincing, because the second run recalls the first run's failure.
        </div>
      </Card>

      <Card title="What each page is for" subtitle="Six pages, one question each">
        <ul className="page-index">
          {PAGES.map((page) => (
            <li key={page.to}>
              <Link to={page.to}>{page.name}</Link>
              <span>{page.what}</span>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="Words used on this site" subtitle="In plain language, no jargon required">
        <dl className="glossary">
          {GLOSSARY.map((entry) => (
            <div key={entry.term}>
              <dt>{entry.term}</dt>
              <dd>{entry.plain}</dd>
            </div>
          ))}
        </dl>
      </Card>

      <Card title="If something looks odd" subtitle="The three things people ask about first">
        <div className="faq">
          <div>
            <div className="faq-q">It says memory is “degraded”. Is it broken?</div>
            <p>
              No. It means the memory service could not be reached, so the assistant switched to a
              local store that behaves the same way. The loop still completes and the lesson is
              still kept. The badge exists so you never guess whether memory was involved.
            </p>
          </div>
          <div>
            <div className="faq-q">The memory page is empty. Is that wrong?</div>
            <p>
              On a clean start, yes — and it is the correct behaviour. Memory fills up as incidents
              are resolved. Run the learning loop once and it will populate. That emptiness is the
              “before” picture.
            </p>
          </div>
          <div>
            <div className="faq-q">Why does the learning loop take a couple of minutes?</div>
            <p>
              Because it is doing real work: generating telemetry, running multi-round AI
              investigations with real tool calls, waiting out a settle window after each fix so the
              verification means something, and writing to the memory service over the network. It
              is not playing a recording.
            </p>
          </div>
        </div>
      </Card>
    </div>
  );
}
