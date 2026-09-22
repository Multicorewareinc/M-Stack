import { Alert, Button, FormField, Input } from '@multistack/ui';
import * as React from 'react';
import type { LoginInput } from './api';

export interface LoginPageProps {
  onSubmit: (input: LoginInput) => void;
  submitting: boolean;
  error: string | null;
  /** Non-error banner, e.g. "Password updated — sign in again" after a change-password logout. */
  notice?: string | null;
}

export function LoginPage({ onSubmit, submitting, error, notice }: LoginPageProps) {
  const [email, setEmail] = React.useState('');
  const [password, setPassword] = React.useState('');

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    onSubmit({ email, password });
  }

  return (
    <div className="flex h-screen items-center justify-center bg-neutral-50">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-lg border border-subtle bg-neutral-0 p-6 shadow-md"
      >
        <div className="flex flex-col gap-1">
          <h1 className="text-pageTitle font-semibold text-neutral-900">Sign in</h1>
          <p className="text-secondary text-neutral-500">Multistack</p>
        </div>
        {notice && !error && <Alert variant="success">{notice}</Alert>}
        {error && (
          <Alert variant="error" title="Sign-in failed">
            {error}
          </Alert>
        )}
        <FormField label="Email" htmlFor="login-email" required>
          <Input
            id="login-email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </FormField>
        <FormField label="Password" htmlFor="login-password" required>
          <Input
            id="login-password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </FormField>
        <Button type="submit" loading={submitting} disabled={submitting}>
          Sign in
        </Button>
      </form>
    </div>
  );
}
