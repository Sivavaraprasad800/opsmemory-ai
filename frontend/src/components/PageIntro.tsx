import type { ReactNode } from "react";

/**
 * A short, plain-English band at the top of every page.
 *
 * The console is dense on purpose - an investigation has a lot of evidence - but density is only
 * useful if the reader knows what they are looking at. Three questions get answered before any
 * data: what is this page, why does it exist, and what should I look at first. `steps` turns the
 * band into a short "read it in this order" hint for pages that follow a sequence.
 */
export default function PageIntro({
  icon,
  title,
  what,
  why,
  lookFor,
  steps,
  children,
}: {
  icon?: string;
  title: string;
  /** One sentence: what this page shows. */
  what: string;
  /** One sentence: why it matters. */
  why?: string;
  /** Plain-language pointer to the single most important thing on the page. */
  lookFor?: ReactNode;
  /** Optional ordered reading path through the page. */
  steps?: string[];
  children?: ReactNode;
}) {
  return (
    <section className="intro">
      <div className="intro-head">
        {icon && <span className="intro-icon">{icon}</span>}
        <div>
          <h1 className="intro-title">{title}</h1>
          <p className="intro-what">{what}</p>
        </div>
      </div>

      {why && <p className="intro-why">{why}</p>}

      {(lookFor || children) && (
        <div className="intro-look">
          <span className="intro-look-label">What to look at</span>
          <div className="intro-look-body">{lookFor ?? children}</div>
        </div>
      )}

      {steps && steps.length > 0 && (
        <ol className="intro-steps">
          {steps.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
      )}
    </section>
  );
}
