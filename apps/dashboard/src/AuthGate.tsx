import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { api } from "./api";
import App from "./App";
import type { BrowserSession } from "./types";

export function AuthGate() {
  const [session, setSession] = useState<BrowserSession | null>(null);
  const [loading, setLoading] = useState(true);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try { const value = await api.session(); if (active) setSession(value); }
      catch { if (active) setError("Unable to check your session. Reload to try again."); }
      finally { if (active) setLoading(false); }
    };
    const expired = () => {
      setSession(null);
      setError("Your session has expired. Sign in again to continue.");
      void load();
    };
    void load();
    window.addEventListener("prahari:session-expired", expired);
    return () => { active = false; window.removeEventListener("prahari:session-expired", expired); };
  }, []);

  async function signIn(event: FormEvent) {
    event.preventDefault();
    if (loading) return;
    setLoading(true); setError(null);
    try { setSession(await api.login(username.trim().toLowerCase(), password)); setPassword(""); }
    catch (error) { setError(error instanceof Error ? error.message : "Sign-in failed."); }
    finally { setLoading(false); }
  }

  async function signOut() {
    setLoading(true); setError(null);
    try { await api.logout(); setSession(await api.session()); }
    catch { setError("Sign-out could not be confirmed. Try again."); }
    finally { setLoading(false); }
  }

  if (session?.authenticated && session.actor) {
    return <>
      {error ? <p className="session-error" role="alert">{error}</p> : null}
      <App actor={session.actor} onSignOut={session.mode === "local" ? () => void signOut() : undefined} />
    </>;
  }
  return <main className="auth-shell">
    <section className="auth-card" aria-labelledby="sign-in-title">
      <p className="section-label">PRAHARI</p>
      <h1 id="sign-in-title">Sign in to your NOC workspace</h1>
      {loading ? <p role="status">Checking your session...</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      {session?.mode === "api_key" ? <p>Browser sign-in has not been configured. Contact your deployment administrator.</p> : null}
      {session?.mode === "local" ? <form onSubmit={(event) => void signIn(event)}>
        <label>Username<input autoComplete="username" required maxLength={100} value={username} onChange={(event) => setUsername(event.target.value)} /></label>
        <label>Password<input type="password" autoComplete="current-password" required maxLength={256} value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <button type="submit" disabled={loading || !username.trim() || !password}>{loading ? "Signing in..." : "Sign in"}</button>
      </form> : null}
    </section>
  </main>;
}
