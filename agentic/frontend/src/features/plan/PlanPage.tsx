import { Alert, Badge, Button, EmptyState, PageHeader, Spinner, Textarea } from '@multistack/ui';
import {
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  MessageSquarePlus,
  SendHorizontal,
  TriangleAlert,
  User,
  Wrench,
} from 'lucide-react';
import * as React from 'react';
import { AgenticError, streamMessage, type SessionResponse, type TraceEvent } from '../../api/agentic';
import { getStoredSession, saveSession, titleFrom, type StoredMessage } from '../../lib/storage';

type Message = StoredMessage;

/** Reconstructs a saved conversation, or a blank one when there isn't one
 * -- e.g. a brand-new plan, or a sessionId typed into the URL by hand that
 * this browser never saved. */
function loadInitial(sessionId: string | null): {
  messages: Message[];
  script: string | null;
  status: SessionResponse['status'] | null;
} {
  const stored = sessionId ? getStoredSession(sessionId) : undefined;
  if (!stored) return { messages: [], script: null, status: null };
  return { messages: stored.messages, script: stored.script, status: stored.status };
}

function Avatar({ role }: { role: 'user' | 'assistant' }) {
  const isUser = role === 'user';
  return (
    <div
      className={
        'flex h-7 w-7 shrink-0 items-center justify-center rounded-full ' +
        (isUser ? 'bg-primary-600 text-white' : 'bg-neutral-200 text-neutral-600')
      }
    >
      {isUser ? <User className="h-4 w-4" aria-hidden="true" /> : <Bot className="h-4 w-4" aria-hidden="true" />}
    </div>
  );
}

function MessageBubble({
  role,
  content,
  trace,
}: {
  role: 'user' | 'assistant';
  content: string;
  trace?: TraceEvent[];
}) {
  const isUser = role === 'user';
  return (
    <div className={'flex gap-3 rounded-md p-2 ' + (isUser ? 'flex-row-reverse' : 'flex-row')}>
      <Avatar role={role} />
      <div className="flex max-w-[75%] flex-col items-start gap-1.5">
        {trace && trace.length > 0 && <TraceDisclosure events={trace} />}
        <div
          className={
            'rounded-lg px-3 py-2 text-body ' +
            (isUser ? 'bg-primary-600 text-white' : 'bg-neutral-0 text-neutral-900 shadow-sm')
          }
        >
          {/* Plain text, whitespace-preserving. Never raw HTML: both the typed
              request and the model's reply are untrusted content. */}
          <div className="whitespace-pre-wrap">{content}</div>
        </div>
      </div>
    </div>
  );
}

/** Which capabilities a tool call named, so the trace says "valkey, cnpg"
 * rather than an opaque tool name on its own. Falls back to the argument
 * keys, which is still more informative than nothing. */
function argSummary(args: Record<string, unknown>): string {
  const values = Object.values(args)
    .flatMap((v) => (Array.isArray(v) ? v : [v]))
    .filter((v): v is string => typeof v === 'string');
  const shown = values.length > 0 ? values : Object.keys(args);
  return shown.slice(0, 6).join(', ');
}

/**
 * A turn's work, line by line: which tool the model reached for, whether
 * the SDK accepted it, and any text it wrote along the way. Shown live
 * while the turn runs, and again behind TraceDisclosure once it lands.
 */
function ActivityTrace({ events }: { events: TraceEvent[] }) {
  return (
    <div className="flex flex-col gap-1.5 border-l-2 border-subtle py-1 pl-3">
      {events.map((event, i) => {
        if (event.type === 'reasoning') {
          return (
            <p key={i} className="max-w-[75%] whitespace-pre-wrap text-caption italic text-neutral-500">
              {event.text}
            </p>
          );
        }
        if (event.type === 'tool_call') {
          return (
            <span key={i} className="flex items-center gap-2 text-caption text-neutral-600">
              <Wrench className="h-3.5 w-3.5 shrink-0 text-primary-600" aria-hidden="true" />
              <span className="font-mono">{event.tool}</span>
              <span className="truncate text-neutral-500">{argSummary(event.args)}</span>
            </span>
          );
        }
        return (
          <span key={i} className="flex items-center gap-2 text-caption text-neutral-600">
            {event.valid === false ? (
              <TriangleAlert className="h-3.5 w-3.5 shrink-0 text-warning-500" aria-hidden="true" />
            ) : (
              <Check className="h-3.5 w-3.5 shrink-0 text-success-500" aria-hidden="true" />
            )}
            <span className="font-mono">{event.tool}</span>
            <span className="truncate text-neutral-500">
              {event.valid === false ? (event.error ?? 'rejected') : 'validated'}
            </span>
          </span>
        );
      })}
    </div>
  );
}

/**
 * A finished turn's trace, collapsed to one line. Collapsed by default so
 * a revisited conversation reads as a conversation, not as a wall of tool
 * output -- but the work stays there to open, which is the whole reason
 * the trace is saved with the message rather than thrown away when the
 * spinner stops.
 */
function TraceDisclosure({ events }: { events: TraceEvent[] }) {
  const [open, setOpen] = React.useState(false);
  const calls = events.filter((e) => e.type === 'tool_call').length;
  const label = calls === 0 ? 'Reasoning' : `${calls} tool call${calls === 1 ? '' : 's'}`;
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div className="flex w-full flex-col gap-1.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1 self-start rounded text-caption text-neutral-500 hover:text-neutral-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-600"
      >
        <Chevron className="h-3.5 w-3.5" aria-hidden="true" />
        {label}
      </button>
      {open && <ActivityTrace events={events} />}
    </div>
  );
}

export function PlanPage({
  initialSessionId,
  onNewPlan,
}: {
  initialSessionId: string | null;
  /** Resets and remounts this page from the App level -- see App.tsx for
   * why that's a remount (planKey) rather than local state clearing. */
  onNewPlan: () => void;
}) {
  const [initial] = React.useState(() => loadInitial(initialSessionId));
  const [messages, setMessages] = React.useState<Message[]>(initial.messages);
  const [sessionId, setSessionId] = React.useState<string | null>(initialSessionId);
  const [script, setScript] = React.useState<string | null>(initial.script);
  const [status, setStatus] = React.useState<SessionResponse['status'] | null>(initial.status);
  const [draft, setDraft] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [trace, setTrace] = React.useState<TraceEvent[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const bottomRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages, busy, trace]);

  // Autosaves every turn once a session exists (see lib/storage.ts) --
  // this is what makes "go to a previous chat" possible at all, since the
  // agentic service's own API only ever returns the latest message, never
  // the full transcript, and keeps nothing across a restart.
  React.useEffect(() => {
    if (!sessionId) return;
    saveSession({
      sessionId,
      title: messages[0] ? titleFrom(messages[0].content) : 'Untitled plan',
      updatedAt: Date.now(),
      messages,
      script,
      status: status ?? 'needs_input',
    });
  }, [sessionId, messages, script, status]);

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;

    setMessages((prev) => [...prev, { id: crypto.randomUUID(), role: 'user', content: text }]);
    setDraft('');
    setBusy(true);
    setTrace([]);
    setError(null);

    try {
      // One turn arrives as a stream of events rather than a single
      // response: everything before "final" is progress to show live, and
      // "final" carries exactly what the blocking endpoints would have
      // returned. The events are collected locally as well as in state --
      // the closure's `trace` is a render-time snapshot and would be stale
      // by the time "final" arrives, but `steps` is always complete.
      const steps: TraceEvent[] = [];
      for await (const event of streamMessage(sessionId, text)) {
        if (event.type !== 'final') {
          steps.push(event);
          setTrace([...steps]);
          continue;
        }
        setSessionId(event.session_id);
        setStatus(event.status);
        if (event.script) setScript(event.script);
        if (event.message) {
          setMessages((prev) => [
            ...prev,
            { id: crypto.randomUUID(), role: 'assistant', content: event.message!, trace: steps },
          ]);
        }
      }
    } catch (e) {
      const message =
        e instanceof AgenticError
          ? e.message
          : 'Could not reach the agentic service. Is it running on :8000?';
      setError(message);
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void send();
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 px-6 pt-6">
        <PageHeader
          title="Build a plan"
          description="Describe the infrastructure you want. The agent validates it against the real SDK and hands back a script."
        />
      </div>

      <div className="mx-6 mt-4 flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-subtle bg-neutral-50">
        {/* Session strip -- the same shape as the gateway playground's usage bar. */}
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-subtle px-4">
          <div className="flex items-center gap-6">
            <span className="text-caption font-semibold uppercase tracking-wide text-neutral-500">Session</span>
            <span className="font-mono text-caption text-neutral-600">
              {sessionId ? sessionId.slice(0, 12) : '—'}
            </span>
            <span className="text-caption font-semibold uppercase tracking-wide text-neutral-500">Status</span>
            {status === 'resolved' ? (
              <Badge variant="success">script ready</Badge>
            ) : status === 'needs_input' ? (
              <Badge variant="warning">needs input</Badge>
            ) : (
              <span className="text-caption text-neutral-600">—</span>
            )}
          </div>
          <Button size="sm" variant="secondary" onClick={onNewPlan} disabled={busy || messages.length === 0}>
            <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
            New plan
          </Button>
        </div>

        <div className="flex min-h-0 flex-1">
          <div className="flex min-h-0 flex-1 flex-col">
            {messages.length === 0 ? (
              <div className="flex flex-1 items-center justify-center p-6">
                <EmptyState
                  title="Describe what you want built"
                  description='Try: "a cluster called demo with a server node at 10.0.0.60, Longhorn storage with 2 replicas, and a MinIO tenant called models".'
                />
              </div>
            ) : (
              <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-4 py-4">
                <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-2">
                  {messages.map((m) => (
                    <MessageBubble key={m.id} role={m.role} content={m.content} trace={m.trace} />
                  ))}
                  <div aria-live="polite" className="flex flex-col gap-1.5">
                    {/* Expanded while the turn runs -- it is the only sign of
                        progress. It collapses into the reply once that lands. */}
                    {busy && trace.length > 0 && (
                      <div className="ml-10">
                        <ActivityTrace events={trace} />
                      </div>
                    )}
                    {busy && (
                      <span className="ml-10 inline-flex items-center gap-2 text-secondary text-neutral-500">
                        <Spinner label="Building a plan" />
                        Validating against the SDK…
                      </span>
                    )}
                  </div>
                  <div ref={bottomRef} />
                </div>
              </div>
            )}

            {error && (
              <div className="shrink-0 px-4 pb-2">
                <Alert variant="error" title="Request failed">
                  {error}
                </Alert>
              </div>
            )}

            <div className="shrink-0 border-t border-subtle p-4">
              <div className="mx-auto flex w-full max-w-3xl items-end gap-2">
                <Textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={onKeyDown}
                  rows={3}
                  aria-label="Describe the infrastructure you want"
                  placeholder="Describe the infrastructure…"
                  disabled={busy}
                />
                <Button onClick={() => void send()} disabled={busy || draft.trim().length === 0}>
                  <SendHorizontal className="h-4 w-4" aria-hidden="true" />
                  Send
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
