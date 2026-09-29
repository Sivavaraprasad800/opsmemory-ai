import { Navigate, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import Layout from "./components/Layout";
import Workspace from "./components/Workspace";
import { Loading } from "./components/ui";
import { useAuth } from "./auth";
import CataloguePage from "./pages/CataloguePage";
import DemoPage from "./pages/DemoPage";
import GuidePage from "./pages/GuidePage";
import IncidentDetailPage from "./pages/IncidentDetailPage";
import IncidentsPage from "./pages/IncidentsPage";
import InvestigationsPage from "./pages/InvestigationsPage";
import LoginPage from "./pages/LoginPage";
import MemoryPage from "./pages/MemoryPage";
import OverviewPage from "./pages/OverviewPage";
import PatternsPage from "./pages/PatternsPage";
import PlatformPage from "./pages/PlatformPage";
import PostmortemsPage from "./pages/PostmortemsPage";

function RequireAuth({ children }: { children: ReactNode }) {
  const { session, ready } = useAuth();
  if (!ready) {
    return (
      <div className="login-page">
        <Loading label="Checking session…" />
      </div>
    );
  }
  if (!session) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** Incidents and the AI's investigation record belong together: the audit trail explains the incident. */
function IncidentsWorkspace() {
  return (
    <Workspace
      tabs={[
        {
          key: "open",
          label: "Incidents",
          hint: "What is open, what the detector opened it for, and the evidence behind it",
          element: <IncidentsPage />,
        },
        {
          key: "investigations",
          label: "AI investigations",
          hint: "Every investigation the AI has run: hypotheses, tools it called, and its conclusion",
          element: <InvestigationsPage />,
        },
      ]}
    />
  );
}

/** Reliability analysis and postmortems are both "what we learned after the fact". */
function LearningWorkspace() {
  return (
    <Workspace
      tabs={[
        {
          key: "patterns",
          label: "Patterns & reliability",
          hint: "Recurring failures the platform has noticed across incidents, with their evidence",
          element: <PatternsPage />,
        },
        {
          key: "postmortems",
          label: "Postmortems",
          hint: "Written-ups of closed incidents: causes, mitigations, and the lessons retained",
          element: <PostmortemsPage />,
        },
      ]}
    />
  );
}

/** The catalogue and the simulator are both "the world the agent operates in". */
function EnvironmentWorkspace() {
  return (
    <Workspace
      tabs={[
        {
          key: "services",
          label: "Services & actions",
          hint: "The estate the agent watches and the registered actions it is allowed to propose",
          element: <CataloguePage />,
        },
        {
          key: "simulator",
          label: "Simulator & audit",
          hint: "Drive the simulated estate by hand and read the audit trail of everything that changed",
          element: <PlatformPage />,
        },
      ]}
    />
  );
}

export default function App() {
  const { session } = useAuth();

  return (
    <Routes>
      <Route
        path="/login"
        element={session ? <Navigate to="/" replace /> : <LoginPage />}
      />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/start" element={<GuidePage />} />
        <Route path="/" element={<OverviewPage />} />
        <Route path="/incidents" element={<IncidentsWorkspace />} />
        <Route path="/incidents/:id" element={<IncidentDetailPage />} />
        <Route path="/memory" element={<MemoryPage />} />
        <Route path="/learning" element={<LearningWorkspace />} />
        <Route path="/postmortems/:id" element={<PostmortemsPage />} />
        <Route path="/environment" element={<EnvironmentWorkspace />} />
        <Route path="/demo" element={<DemoPage />} />

        {/* Older, one-page-per-view links still work: they land on the right tab. */}
        <Route path="/investigations" element={<Navigate to="/incidents?tab=investigations" replace />} />
        <Route path="/patterns" element={<Navigate to="/learning" replace />} />
        <Route path="/postmortems" element={<Navigate to="/learning?tab=postmortems" replace />} />
        <Route path="/catalogue" element={<Navigate to="/environment" replace />} />
        <Route path="/platform" element={<Navigate to="/environment?tab=simulator" replace />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
