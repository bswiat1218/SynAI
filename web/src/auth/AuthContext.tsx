import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { LoginRequest, LoginResponse, SessionResponse } from "../api/contracts";
import { apiRequest, SESSION_EXPIRED_EVENT } from "../api/client";

type AuthState = {
  authenticated: boolean;
  loading: boolean;
  csrfToken: string | null;
  error: string | null;
  login: (password: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [authenticated, setAuthenticated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [csrfToken, setCsrfToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const installSession = useCallback((token: string) => {
    setCsrfToken(token);
    setAuthenticated(true);
    setError(null);
  }, []);

  useEffect(() => {
    let active = true;
    const onSessionExpired = () => {
      setAuthenticated(false);
      setCsrfToken(null);
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    const initialize = async () => {
      try {
        const session = await apiRequest<SessionResponse>("/api/v1/auth/session");
        if (!session.authenticated) return;
        const csrf = await apiRequest<{ csrf_token: string }>(
          "/api/v1/auth/csrf",
          { method: "POST" },
        );
        if (active) installSession(csrf.csrf_token);
      } catch (cause) {
        if (active && !(cause instanceof Error && "status" in cause && cause.status === 401)) {
          setError("Authentication status is unavailable. Try again.");
        }
      } finally {
        if (active) setLoading(false);
      }
    };
    void initialize();
    return () => {
      active = false;
      window.removeEventListener(SESSION_EXPIRED_EVENT, onSessionExpired);
    };
  }, [installSession]);

  const login = useCallback(async (password: string) => {
    setError(null);
    const body: LoginRequest = { password };
    try {
      const result = await apiRequest<LoginResponse>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify(body),
      });
      installSession(result.csrf_token);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign in failed.");
      throw cause;
    }
  }, [installSession]);

  const logout = useCallback(async () => {
    if (!csrfToken) return;
    await apiRequest("/api/v1/auth/logout", { method: "POST" }, csrfToken);
    setCsrfToken(null);
    setAuthenticated(false);
  }, [csrfToken]);

  const value = useMemo(
    () => ({ authenticated, loading, csrfToken, error, login, logout }),
    [authenticated, loading, csrfToken, error, login, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used within AuthProvider");
  return value;
}
