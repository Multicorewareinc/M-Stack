import { Alert, Button, Input } from '@multistack/ui';
import * as React from 'react';

export interface InviteLinkCardProps {
  link: string;
  email: string;
  role: string;
  /** Extra "Label: value" facts shown on the same line as the email and role (e.g. the organization). */
  extra?: { label: string; value: string }[];
  /** Whether the server also emailed the link, and why not when it didn't. */
  emailStatus?: 'not_configured' | 'failed' | 'rejected';
  emailDetail?: string | null;
  onDismiss: () => void;
}

/** Fallback shown on the Invitations page when an invitation could NOT be emailed (email not set up, or the
 * send failed): the one-time link, ready to copy and send by hand. It is not retrievable afterwards, so it stays
 * until dismissed. When the email went out the page shows a short confirmation instead and never this card. */
export function InviteLinkCard({
  link,
  email,
  role,
  extra = [],
  emailStatus,
  emailDetail,
  onDismiss,
}: InviteLinkCardProps) {
  const [copied, setCopied] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  // A fresh invitation replaces the previous one in this card — don't keep saying "Copied".
  React.useEffect(() => setCopied(false), [link]);

  async function copy() {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      // Clipboard API unavailable (insecure origin / permission) — select the text so Ctrl+C works.
      inputRef.current?.select();
    }
  }

  return (
    <section
      aria-label="Invite link created"
      className="mb-6 flex flex-col gap-3 rounded-lg border border-subtle bg-neutral-0 p-5"
    >
      <div className="flex flex-col gap-1">
        <h2 className="text-cardTitle font-semibold text-neutral-900">Invite link created</h2>
        <p className="text-secondary text-neutral-500">Copy this link now — it won&apos;t be shown again.</p>
      </div>
      <p className="text-secondary text-neutral-600">
        Email: <span className="font-medium text-neutral-900">{email}</span>
        {extra.map((f) => (
          <React.Fragment key={f.label}>
            {' · '}
            {f.label}: <span className="font-medium text-neutral-900">{f.value}</span>
          </React.Fragment>
        ))}
        {' · '}Role: <span className="font-medium text-neutral-900">{role}</span>
      </p>
      {emailStatus === 'rejected' && (
        <Alert variant="error" title="That email address was rejected">
          The mail server says {email} can&apos;t receive mail{emailDetail ? ` (${emailDetail})` : ''}. Check the
          address for typos, or send the link below some other way.
        </Alert>
      )}
      {emailStatus === 'failed' && (
        <Alert variant="warning" title="The email could not be sent">
          {emailDetail ? `${emailDetail} ` : ''}Copy the link below and send it to {email} yourself.
        </Alert>
      )}
      {emailStatus === 'not_configured' && (
        <Alert variant="info">
          Email isn&apos;t set up, so nothing was sent. Copy the link below and send it to {email} yourself.
        </Alert>
      )}
      <Input
        ref={inputRef}
        readOnly
        aria-label="Invitation link"
        value={link}
        className="font-mono"
        onFocus={(e) => e.currentTarget.select()}
      />
      <div className="flex gap-2">
        <Button type="button" variant="secondary" onClick={copy}>
          {copied ? 'Copied' : 'Copy link'}
        </Button>
        <Button type="button" variant="ghost" onClick={onDismiss}>
          Dismiss
        </Button>
      </div>
    </section>
  );
}
