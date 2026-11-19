import {
  Avatar,
  BrandLogo,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  ThemeToggle,
} from '@multistack/ui';
import * as React from 'react';
import { useAuth } from '../../auth/AuthContext';
import { ChangePasswordDrawer } from '../../auth/ChangePasswordDrawer';
import { shouldUseRealAuth } from '../../auth/env';
import { BRANDING } from '../../branding';

export function Header() {
  const { user, logout } = useAuth();
  const [changePasswordOpen, setChangePasswordOpen] = React.useState(false);
  // No real password to change under MockAuthProvider — nothing here maps to a real account.
  const canChangePassword = shouldUseRealAuth();

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-subtle bg-neutral-0 px-4">
      <BrandLogo name={BRANDING.appName} logoUrl={BRANDING.logoUrl} />
      <div className="flex items-center gap-2">
        <ThemeToggle />
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex items-center gap-2 rounded-md px-2 py-1 hover:bg-neutral-100">
              <Avatar name={user.displayName} />
              <span className="text-body text-neutral-700">{user.displayName}</span>
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {canChangePassword && (
              <DropdownMenuItem onSelect={() => setChangePasswordOpen(true)}>Change Password</DropdownMenuItem>
            )}
            <DropdownMenuItem onSelect={logout}>Sign out</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {canChangePassword && (
        <ChangePasswordDrawer open={changePasswordOpen} onOpenChange={setChangePasswordOpen} />
      )}
    </header>
  );
}
