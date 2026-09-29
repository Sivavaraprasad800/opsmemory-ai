import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, getToken, onUnauthorized, setToken } from "./api";
import type { Session } from "./types";

interface AuthValue {
  session: Session | null;
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  can: (permission: string) => boolean;
}

const AuthContext = createContext<AuthValue | null>(null);

/** Mirrors the backend RBAC so the UI does not offer actions the API will refuse. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);

  const load = useCallback(async () => {
    if (!getToken()) {
      setSession(null);
      setReady(true);
      return;
    }
    try {
      setSession(await api.me());
    } catch {
      setToken(null);
      setSession(null);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void load();
    return onUnauthorized(() => {
      setToken(null);
      setSession(null);
    });
  }, [load]);

  const login = useCallback(async (email: string, password: string) => {
    const result = await api.login(email, password);
    setToken(result.access_token);
    setSession(await api.me());
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setSession(null);
  }, []);

  const value = useMemo<AuthValue>(
    () => ({
      session,
      ready,
      login,
      logout,
      can: (permission: string) => Boolean(session?.permissions?.includes(permission)),
    }),
    [session, ready, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (value === null) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}
