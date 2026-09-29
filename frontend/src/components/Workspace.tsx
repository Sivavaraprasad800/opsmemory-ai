import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";

export interface WorkspaceTab {
  key: string;
  label: string;
  /** One line explaining why this tab exists, shown as a tooltip and under the tab bar. */
  hint: string;
  element: ReactNode;
}

/**
 * Several closely related views live on one route behind a tab bar instead of becoming a
 * separate top-level page each. The navigation stays short enough to hold in your head, and
 * related evidence (an incident and the AI's investigation of it, a pattern and the postmortem
 * that explains it) sits where an operator would look for it.
 *
 * The tab is part of the URL, so any view can still be linked to, bookmarked and reloaded.
 */
export default function Workspace({ tabs }: { tabs: WorkspaceTab[] }) {
  const [params, setParams] = useSearchParams();
  const requested = params.get("tab");
  const current = tabs.find((tab) => tab.key === requested) ?? tabs[0];

  const select = (key: string) => {
    if (key === tabs[0].key) setParams({}, { replace: true });
    else setParams({ tab: key }, { replace: true });
  };

  return (
    <>
      <div className="workspace-bar">
        <div className="tabs" role="tablist">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              role="tab"
              aria-selected={tab.key === current.key}
              title={tab.hint}
              className={`tab ${tab.key === current.key ? "active" : ""}`}
              onClick={() => select(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <div className="workspace-hint">{current.hint}</div>
      </div>
      {current.element}
    </>
  );
}
