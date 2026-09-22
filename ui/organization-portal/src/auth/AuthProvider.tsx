import { useQuery, useQueryClient } from '@tanstack/react-query';
import * as React from 'react';
import { EmptyState, Spinner } from '@multistack/ui';
import { ApiError, refreshAccessToken, type AuthSession } from '../api/client';
import { listPermissions } from '../api/permissions';
import { changePassword as changePasswordRequest, login as loginRequest, logoutRequest, type ChangePasswordInput, type LoginInput } from './api';
import { AuthContext, type AuthUser, type OrganizationContext } from './AuthContext';
import { ChangePasswordPage } from './ChangePasswordPage';
import { setCurrentOrgId } from './currentOrgId';
import { LoginPage } from './LoginPage';
import { getAccessToken as getStoredAccessToken } from './tokenStore';

function toAuthUser(session: AuthSession): AuthUser {
  return { id: session.user.id, displayName: session.user.display_name ?? session.user.username, role: 'org_user' };
}

/**
 * Real auth provider — login/refresh flow against modules/auth (POST /api/auth/login,
 * /api/auth/refresh). On mount, attempts a silent refresh against the httpOnly refresh-token
 * cookie (so a page reload doesn't force a re-login while it's still valid); renders LoginPage
 * until authenticated. Mounted instead of MockAuthProvider when shouldUseRealAuth() is true (see
 * auth/env.ts and App.tsx).
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

  // client.ts reads getCurrentOrgId() on every request; keep it in sync with whichever
  // organization the active session is currently scoped to.
  React.useEffect(() => {
    if (session?.org) setCurrentOrgId(session.org.id);
  }, [session]);

  // The login/refresh response carries permission ids from the master catalog, not slugs (see
  // AuthSession's own comment in api/client.ts) — resolve them the same way UserDetailPage
  // already resolves a user's effective permissions, so <Can permission="slug"> keeps working.
  const { data: catalog } = useQuery({
    queryKey: ['auth-permission-catalog'],
    queryFn: () => listPermissions(),
    enabled: Boolean(session?.org),
    staleTime: 5 * 60 * 1000,
  });
  const permissionSlugs = React.useMemo(() => {
    if (!session || !catalog) return [];
    const bySlug = new Map(catalog.map((p) => [p.id, p.slug]));
    return session.permissions.map((id) => bySlug.get(id)).filter((s): s is string => Boolean(s));
  }, [session, catalog]);

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

  if (!session.org) {
    return (
      <div className="flex h-screen items-center justify-center">
        <EmptyState
          title="No organization"
          description="This account isn't a member of any organization yet."
          action={{ label: 'Sign out', onClick: handleLogout }}
        />
      </div>
    );
  }

  const organizationContext: OrganizationContext = { id: session.org.id, name: session.org.name };

  const value = {
    user: toAuthUser(session),
    organizationContext,
    permissions: permissionSlugs,
    getCurrentUser: () => toAuthUser(session),
    getOrganizationContext: () => organizationContext,
    // Always the tokenStore's current value, not the possibly-stale `session` closure — a
    // background refresh (triggered by client.ts on a 401) updates the store directly without
    // going through this component's state.
    getAccessToken: () => getStoredAccessToken(),
    logout: handleLogout,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
