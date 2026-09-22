import * as React from 'react';

export interface AuthUser {
  id: string;
  displayName: string;
  role: string;
}

export interface AuthContextValue {
  user: AuthUser;
  permissions: string[];
  getCurrentUser: () => AuthUser;
  getAccessToken: () => string | null;
  logout: () => void;
}

export const AuthContext = React.createContext<AuthContextValue | undefined>(undefined);

export function useAuth(): AuthContextValue {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider');
  return ctx;
}
