# LinkedIn post

Publish the post, then add the article URL as the **first comment** (LinkedIn suppresses posts with external links in the body).

---

An SRE agent that forgets its own failed fixes is worse than no agent.

It recommends the same restart on Thursday that failed on Tuesday.

I built one that remembers.

Two rules mattered:

• Recall before forming a hypothesis. Retain only after verification returns. Ordering matters more than the model.

• Retain failures with the same weight as successes. Knowing what doesn't work is what saves you.

Result: incident 1 restarts a service, verification fails, the failure is retained. Incident 2, same signature, days later, avoids that restart and applies the config fix instead.

Same model. Same prompt. Different decision — because of what it remembered.

That's why I chose Hindsight for agent memory over a vector store bolted onto a prompt.

#AIAgents #AI #Hindsight #AgentMemory #LLM

---

## First comment (post immediately after)

Here's a link to Hindsight if you want to check it out: https://github.com/vectorize-io/hindsight

## Notes

- Post the project repo link in the **main post body** as well.
- Do not mention any event, competition, or programme — the post is about the engineering, not the occasion.
- Hashtags appear on the last line only. No links or hashtags in the first two lines.
- Keep the body under 800 characters.
