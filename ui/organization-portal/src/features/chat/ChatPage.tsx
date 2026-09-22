import { Alert, Button, EmptyState, PageHeader, Select, Spinner, Textarea } from '@multistack/ui';
import { Bot, Gauge, MessageSquarePlus, SendHorizontal, User } from 'lucide-react';
import * as React from 'react';
import { shouldUseRealAuth } from '../../auth/env';
import type { RateLimitScopeUsage } from './streamChat';
import { ChatProvider, useChat } from './ChatContext';

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

function MessageBubble({ role, content, streaming }: { role: 'user' | 'assistant'; content: string; streaming?: boolean }) {
  const isUser = role === 'user';
  return (
    <div className={'flex gap-3 rounded-md p-2 ' + (isUser ? 'flex-row-reverse' : 'flex-row')}>
      <Avatar role={role} />
      <div
        className={
          'max-w-[75%] rounded-lg px-3 py-2 text-body ' +
          (isUser ? 'bg-primary-600 text-white' : 'bg-neutral-0 text-neutral-900 shadow-sm')
        }
      >
        {/* Plain text, whitespace-preserving — no markdown rendering (minimal scope). Never raw
            HTML: both user input and model output are untrusted content. */}
        <div className="whitespace-pre-wrap">{content}</div>
        {streaming && <span className={'ml-0.5 inline-block animate-pulse ' + (isUser ? 'text-white/70' : 'text-neutral-400')}>▍</span>}
      </div>
    </div>
  );
}

function MessageThread() {
  const { messages, isStreaming, streamingText } = useChat();
  const bottomRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages, streamingText]);

  if (messages.length === 0 && !isStreaming) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <EmptyState title="Start a conversation" description="Ask the assistant anything." />
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-4 py-4 sm:px-8">
      <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-2">
        {messages.map((m) => (
          <MessageBubble key={m.id} role={m.role} content={m.content} />
        ))}
        <div aria-live="polite" className="flex flex-col gap-2">
          {isStreaming && (
            <>
              {streamingText && <MessageBubble role="assistant" content={streamingText} streaming />}
              <span className="ml-10 inline-flex items-center gap-2 text-secondary text-neutral-500">
                <Spinner label="Assistant is responding" />
                Assistant is responding…
              </span>
            </>
          )}
        </div>
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

function Composer() {
  const { models, selectedModelId, selectModel, catalogEmpty, canSend, isStreaming, send, rateLimitMessage } = useChat();
  const [draft, setDraft] = React.useState('');

  if (catalogEmpty) {
    return <div className="border-t border-subtle p-4 text-center text-body text-neutral-500">No models are available.</div>;
  }

  function submit() {
    const text = draft.trim();
    if (!text || !canSend) return;
    setDraft('');
    void send(text);
  }

  return (
    <div className="border-t border-subtle bg-neutral-0 px-4 py-3 sm:px-8">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-2">
        {/* Above the input field, per the rate-limiting requirement — a persistent banner, not
            just a toast, so it's still visible while the user decides what to do next. */}
        {rateLimitMessage && (
          <Alert variant="warning" title="Rate limit reached">
            {rateLimitMessage}
          </Alert>
        )}
        <div className="flex items-center gap-2">
          <label htmlFor="chat-model" className="text-caption uppercase tracking-wide text-neutral-500">
            Model
          </label>
          <Select
            id="chat-model"
            className="w-auto"
            value={selectedModelId ?? ''}
            onValueChange={selectModel}
            options={models.map((m) => ({ value: m.id, label: m.id }))}
          />
        </div>
        {/* One input frame — the frame owns the border/focus ring; the textarea inside is
            borderless so it reads as a single box, not two stacked controls. */}
        <div className="flex items-end gap-2 rounded-lg border border-default bg-neutral-0 px-3 py-2 focus-within:ring-2 focus-within:ring-primary-500">
          <Textarea
            aria-label="Message"
            placeholder="Message the model…"
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            className="max-h-40 flex-1 resize-none border-0 bg-transparent px-0 py-1 shadow-none focus-visible:ring-0"
          />
          <Button
            size="icon"
            aria-label="Send"
            onClick={submit}
            disabled={!canSend || draft.trim().length === 0}
            loading={isStreaming}
          >
            <SendHorizontal className="h-4 w-4" aria-hidden="true" />
          </Button>
        </div>
      </div>
    </div>
  );
}

/** Compact number formatting for the usage meter — 100000 -> "100k", 12345 -> "12.3k". Values
 * under 1000 are shown as-is (already-round integers off the gateway's headers). */
function formatCompactCount(n: number): string {
  if (n < 1000) return String(Math.round(n));
  const k = n / 1000;
  const rounded = Math.round(k * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(1)}k`;
}

/** One RPM/TPM meter: a thin progress bar plus the raw numbers and a reset countdown. Reads
 * directly off the gateway's per-scope X-RateLimit-{scope}-* response headers (see
 * streamChat.ts's readRateLimitUsage) — this is the org's actual, currently-enforced plan
 * limit, not a mock. Absent (undefined) means that limiter hasn't reported yet, or is
 * unconfigured (unlimited) for this org. */
function UsageMeter({ label, usage }: { label: string; usage: RateLimitScopeUsage | undefined }) {
  if (!usage) {
    return (
      <div className="flex min-w-[9rem] flex-1 items-center gap-2 text-caption text-neutral-400">
        <span className="w-8 shrink-0 font-semibold uppercase tracking-wide">{label}</span>
        <span>—</span>
      </div>
    );
  }
  const used = Math.max(0, usage.limit - usage.remaining);
  const pct = usage.limit > 0 ? Math.min(100, Math.round((used / usage.limit) * 100)) : 0;
  const resetIn = Math.max(0, usage.reset - Math.floor(Date.now() / 1000));
  const tone = pct >= 100 ? 'bg-danger-500' : pct >= 75 ? 'bg-warning-500' : 'bg-primary-600';
  return (
    <div className="flex min-w-[9rem] flex-1 items-center gap-2">
      <span className="w-8 shrink-0 text-caption font-semibold uppercase tracking-wide text-neutral-500">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-neutral-200" role="progressbar" aria-label={`${label} usage`} aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div className={'h-full rounded-full transition-all ' + tone} style={{ width: `${pct}%` }} />
      </div>
      <span className="w-24 shrink-0 text-right font-mono text-caption text-neutral-500">
        {formatCompactCount(used)}/{formatCompactCount(usage.limit)} · {resetIn}s
      </span>
    </div>
  );
}

/** Live RPM/TPM showcase — the org's plan limits as actually enforced by rate-limiter-rpm/tpm
 * via model-gateway's policy chain, not a mock billing dashboard. Updates after every send. */
function UsagePanel() {
  const { rateLimitUsage } = useChat();
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-subtle bg-neutral-0 px-4 py-2">
      <span className="flex shrink-0 items-center gap-1.5 text-caption font-semibold uppercase tracking-wide text-neutral-500">
        <Gauge className="h-3.5 w-3.5" aria-hidden="true" />
        Plan usage
      </span>
      <UsageMeter label="RPM" usage={rateLimitUsage.rpm} />
      <UsageMeter label="TPM" usage={rateLimitUsage.tpm} />
    </div>
  );
}

function ChatPlayground() {
  const { newChat } = useChat();
  return (
    <div className="flex min-h-0 flex-1 flex-col rounded-md border border-subtle bg-neutral-50">
      <div className="flex items-center justify-between border-b border-subtle px-4 py-2">
        <h2 className="text-body font-medium text-neutral-900">New chat</h2>
        <Button variant="secondary" size="sm" onClick={newChat} className="gap-2">
          <MessageSquarePlus className="h-4 w-4" aria-hidden="true" />
          New chat
        </Button>
      </div>
      <UsagePanel />
      <MessageThread />
      <Composer />
    </div>
  );
}

/**
 * Backed by the Model Gateway (streaming, authenticated via a per-user API key minted through
 * the existing api_keys module) — a real signed-in session, not the shared dev key. Gated the
 * same way ApiKeysPage is.
 *
 * No server-side history: this is an in-memory playground demonstrating the gateway's RPM/TPM
 * rate limiting, not a persisted chat product. A refresh loses the transcript.
 *
 * Fills the Shell's <main> exactly (flex column, PageHeader sized to content, the playground
 * taking the rest via flex-1/min-h-0) rather than a fixed calc() height guessed against the
 * header/padding — the message thread scrolls internally, the outer page never does.
 */
export function ChatPage() {
  if (!shouldUseRealAuth()) {
    return (
      <div>
        <PageHeader title="Chat" />
        <EmptyState
          title="Requires a real signed-in session"
          description="The chat playground authenticates through your own API key, not the shared development key this environment is currently using. Sign in with VITE_REAL_AUTH enabled to use it."
        />
      </div>
    );
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      <PageHeader title="Chat" description="A playground for the org's model gateway." />
      <ChatProvider>
        <ChatPlayground />
      </ChatProvider>
    </div>
  );
}
