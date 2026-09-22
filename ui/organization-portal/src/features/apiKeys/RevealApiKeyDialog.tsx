import { Alert, Button, DetailDialog } from '@multistack/ui';
import * as React from 'react';

export interface RevealApiKeyDialogProps {
  /** Null closes the dialog — the raw key is discarded the moment this becomes null; nothing
   * keeps it around after that (matches a real key-management page: shown once, never again). */
  rawKey: string | null;
  onClose: () => void;
}

export function RevealApiKeyDialog({ rawKey, onClose }: RevealApiKeyDialogProps) {
  const [copied, setCopied] = React.useState(false);

  React.useEffect(() => {
    if (rawKey) setCopied(false);
  }, [rawKey]);

  return (
    <DetailDialog open={Boolean(rawKey)} onOpenChange={(open) => !open && onClose()} label="API key created">
      <div className="flex flex-col gap-4">
        <div>
          <h2 className="text-pageTitle font-semibold text-neutral-900">API key created</h2>
          <p className="text-secondary text-neutral-500">
            Copy this key now — you won&apos;t be able to see it again.
          </p>
        </div>
        <Alert variant="warning">This is the only time the full key is shown.</Alert>
        <div className="flex items-center gap-2 rounded-md border border-default bg-neutral-50 p-3">
          <code className="flex-1 break-all font-mono text-body text-neutral-900">{rawKey}</code>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              if (!rawKey) return;
              navigator.clipboard.writeText(rawKey);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
          >
            {copied ? 'Copied' : 'Copy'}
          </Button>
        </div>
        <Button onClick={onClose} className="self-end">
          Done
        </Button>
      </div>
    </DetailDialog>
  );
}
