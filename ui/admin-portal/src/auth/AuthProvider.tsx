import { useQueryClient } from '@tanstack/react-query';
import * as React from 'react';
import { Spinner } from '@multistack/ui';
import { ApiError, refreshAccessToken, type AuthSession } from '../api/client';
import { changePassword as changePasswordRequest, login as loginRequest, logoutRequest, type ChangePasswordInput, type LoginInput } from './api';
import { AuthContext, type AuthUser } from './AuthContext';
import { ChangePasswordPage } from './ChangePasswordPage';
import { LoginPage } from './LoginPage';
import { getAccessToken as getStoredAccessToken } from './tokenStore';

function toAuthUser(session: AuthSession): AuthUser {
  return { id: session.user.id, displayName: session.user.email, role: session.user.role };
}

/**
 * Real auth provider — login/refresh flow against modules/auth (POST /api/auth/login,
 * /api/auth/refresh). On mount, attempts a silent refresh against the httpOnly refresh-token
 * cookie (so a page reload doesn't force a re-login while it's still valid); renders LoginPage
 * until authenticated. Mounted instead of MockAuthProvider when shouldUseRealAuth() is true (see
 * auth/env.ts and App.tsx).
 *
 * permissions is always [] here — super-admins are org-less and carry no permission list (see
 * modules/auth/schemas.py's own header comment); nothing in admin-portal currently gates on it
 * for a real identity (usePermission.ts is only exercised against MockAuthProvider's mock set).
 */
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [session, setSession] = React.useState<AuthSession | null>(null);
  const [bootstrapping, setBootstrapping] = React.useState(true);
  const [loginError, setLoginError] = React.useState<string | null>(null);
  const [loggingIn, setLoggingIn] = React.useState(false);
  const [loginNotice, setLoginNotice] = React.useState<string | null>(null);
  const [changePasswordError, setChangePasswordError] = React.useState<string | null>(null);
  const [changingPassword, setChangingPassword] = React.useState(false);

  React.useEffect(() => {
    let cancelled = false;
    refreshAccessToken().then((restored) => {
      if (!cancelled) {
        setSession(restored);
        setBootstrapping(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleLogin(input: LoginInput) {
    setLoggingIn(true);
    setLoginError(null);
    try {
      const newSession = await loginRequest(input);
      setSession(newSession);
      setLoginNotice(null);
    } catch (err) {
      setLoginError(err instanceof Error ? err.message : 'Unable to sign in.');
    } finally {
      setLoggingIn(false);
    }
  }

  async function handleLogout() {
    await logoutRequest();
    setSession(null);
    queryClient.clear();
  }

  // Success revokes every refresh token for this account server-side (D5) — the current session
  // can never keep working, so the only sound move is to log out and send the user back through
  // a real login with the password they just set.
  async function handleChangePassword(input: ChangePasswordInput) {
    setChangingPassword(true);
    setChangePasswordError(null);
    try {
      await changePasswordRequest(input);
      await handleLogout();
      setLoginNotice('Password updated. Sign in with your new password.');
    } catch (err) {
      setChangePasswordError(err instanceof ApiError ? err.message : 'Unable to change password.');
    } finally {
      setChangingPassword(false);
    }
  }

  if (bootstrapping) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Spinner label="Loading session" />
      </div>
    );
  }

  if (session?.must_change_password) {
    return (
      <ChangePasswordPage
        onSubmit={handleChangePassword}
        submitting={changingPassword}
        error={changePasswordError}
      />
    );
  }

  if (!session) {
    return <LoginPage onSubmit={handleLogin} submitting={loggingIn} error={loginError} notice={loginNotice} />;
  }

  const value = {
    user: toAuthUser(session),
    permissions: [],
    getCurrentUser: () => toAuthUser(session),
    // Always the tokenStore's current value, not the possibly-stale `session` closure — a
    // background refresh (triggered by client.ts on a 401) updates the store directly without
    // going through this component's state.
    getAccessToken: () => getStoredAccessToken(),
    logout: handleLogout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
