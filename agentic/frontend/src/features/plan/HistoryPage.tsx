import { Badge, Button, EmptyState, PageHeader } from '@multistack/ui';
import { FileCode2, Trash2 } from 'lucide-react';
import * as React from 'react';
import { deleteSession, listSessions, type StoredSession } from '../../lib/storage';

function relativeTime(ts: number): string {
  const minutes = Math.round((Date.now() - ts) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function HistoryPage({ onOpen }: { onOpen: (sessionId: string) => void }) {
  // Read fresh every time this page mounts (App only mounts it while
  // active -- see App.tsx), so a plan saved a moment ago always shows up.
  const [sessions, setSessions] = React.useState<StoredSession[]>(() => listSessions());

  function remove(sessionId: string, event: React.MouseEvent) {
    event.stopPropagation();
    deleteSession(sessionId);
    setSessions(listSessions());
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-6 py-6">
      <PageHeader
        title="History"
        description="Saved in this browser. The agentic service itself keeps no session state across a restart, so this is what makes a plan revisitable."
      />

      {sessions.length === 0 ? (
        <div className="mt-8 flex flex-1 items-center justify-center">
          <EmptyState
            title="No saved plans yet"
            description="A conversation is saved here as soon as the agent replies."
          />
        </div>
      ) : (
        <div className="mt-4 flex flex-col gap-2">
          {sessions.map((s) => (
            <button
              key={s.sessionId}
              type="button"
              onClick={() => onOpen(s.sessionId)}
              className="flex items-center justify-between gap-3 rounded-lg border border-subtle bg-neutral-0 p-3 text-left transition-colors hover:border-primary-300 hover:bg-primary-50"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-body font-semibold text-neutral-900">{s.title}</p>
                <p className="mt-0.5 flex items-center gap-2 text-caption text-neutral-500">
                  <span>
                    {s.messages.length} message{s.messages.length === 1 ? '' : 's'}
                  </span>
                  <span aria-hidden="true">·</span>
                  <span>{relativeTime(s.updatedAt)}</span>
                  {s.script && (
                    <>
                      <span aria-hidden="true">·</span>
                      <span className="inline-flex items-center gap-1 text-primary-600">
                        <FileCode2 className="h-3.5 w-3.5" aria-hidden="true" />
                        script
                      </span>
                    </>
                  )}
                </p>
              </div>
              {s.status === 'resolved' ? (
                <Badge variant="success">resolved</Badge>
              ) : (
                <Badge variant="warning">needs input</Badge>
              )}
              <Button
                size="sm"
                variant="ghost"
                onClick={(e) => remove(s.sessionId, e)}
                aria-label={`Delete "${s.title}"`}
              >
                <Trash2 className="h-4 w-4" aria-hidden="true" />
              </Button>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
