import { Alert, Badge, Button, EmptyState, IconButton, PageHeader, Spinner, Textarea, Tooltip } from '@multistack/ui';
import {
  BookOpen,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Download,
  FileText,
  Maximize2,
  MessageSquarePlus,
  Paperclip,
  SendHorizontal,
  Square,
  TriangleAlert,
  User,
  Wrench,
  X,
} from 'lucide-react';
import * as React from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  AgenticError,
  streamMessage,
  type Attachment,
  type SessionResponse,
  type TraceEvent,
} from '../../api/agentic';
import { getStoredSession, saveSession, titleFrom, type StoredMessage } from '../../lib/storage';

/** Extensions the attach button accepts -- the same text-like formats the
 * backend actually knows what to do with once spliced into the model's
 * context (a spec, an inventory, a snippet of output), not arbitrary
 * binary files a 20KB text-only channel couldn't carry anyway (see
 * router.py's MAX_ATTACHMENT_CHARS). */
const ATTACHMENT_ACCEPT = '.txt,.md,.yaml,.yml,.json,.py';
const MAX_ATTACHMENT_BYTES = 20_000;

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

const FENCE = /```/;

/** File extension for a downloaded code block, by the language the fence
 * declared -- the model only ever emits Python for the script itself
 * (hence the fallback), but a reply can fence other languages too (e.g.
 * a YAML snippet quoted while asking a clarifying question). */
const LANG_EXTENSIONS: Record<string, string> = {
  python: 'py',
  py: 'py',
  yaml: 'yaml',
  yml: 'yml',
  json: 'json',
  bash: 'sh',
  sh: 'sh',
};

function downloadCode(code: string, lang: string) {
  const ext = LANG_EXTENSIONS[lang.toLowerCase()] ?? 'txt';
  const blob = new Blob([code], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `multistack-plan.${ext}`;
  a.click();
  URL.revokeObjectURL(url);
}

/** A fenced code block with its own copy/download/expand buttons, right
 * where it appears in the reply -- not a separate collapsible element,
 * just part of the message like any code block in a chat client. */
function InlineCodeBlock({ code, lang }: { code: string; lang: string }) {
  const [expanded, setExpanded] = React.useState(false);
  return (
    <div className="w-full min-w-0 overflow-hidden rounded-lg border border-subtle bg-neutral-50">
      <div className="flex items-center justify-between gap-2 border-b border-subtle px-3 py-1.5">
        <span className="font-mono text-caption text-neutral-500">{lang}</span>
        <div className="flex items-center gap-3">
          <CopyButton script={code} />
          <Tooltip content="Download">
            <IconButton aria-label="Download code" variant="ghost" onClick={() => downloadCode(code, lang)}>
              <Download className="h-4 w-4" aria-hidden="true" />
            </IconButton>
          </Tooltip>
          <Tooltip content="Expand">
            <IconButton aria-label="Expand code" variant="ghost" onClick={() => setExpanded(true)}>
              <Maximize2 className="h-4 w-4" aria-hidden="true" />
            </IconButton>
          </Tooltip>
        </div>
      </div>
      <CodeBlock script={code} className="max-h-96" />
      {expanded && <CodeExpandedView code={code} lang={lang} onClose={() => setExpanded(false)} />}
    </div>
  );
}

/** Full-screen reading view for a code block -- a plain overlay portaled
 * to <body>, not the shared Dialog component: that one's header is fixed
 * title-then-footer, and this needs Copy/Download/Close laid out beside
 * the language label like the inline header above it. Escape and a click
 * on the backdrop both close it. */
function CodeExpandedView({ code, lang, onClose }: { code: string; lang: string; onClose: () => void }) {
  React.useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return createPortal(
    <div className="fixed inset-0 z-modal flex items-center justify-center bg-black/60 p-6" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Expanded ${lang} code`}
        onClick={(e) => e.stopPropagation()}
        className="flex h-full w-full max-w-4xl flex-col overflow-hidden rounded-lg border border-subtle bg-neutral-50 shadow-lg"
      >
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-subtle px-4 py-3">
          <span className="font-mono text-caption text-neutral-500">{lang}</span>
          <div className="flex items-center gap-3">
            <CopyButton script={code} />
            <Tooltip content="Download">
              <IconButton aria-label="Download code" variant="ghost" onClick={() => downloadCode(code, lang)}>
                <Download className="h-4 w-4" aria-hidden="true" />
              </IconButton>
            </Tooltip>
            <Tooltip content="Close">
              <IconButton aria-label="Close" variant="ghost" onClick={onClose}>
                <X className="h-4 w-4" aria-hidden="true" />
              </IconButton>
            </Tooltip>
          </div>
        </div>
        <CodeBlock script={code} className="flex-1" />
      </div>
    </div>,
    document.body,
  );
}

/** A tool call's resource name for the summary's "Resources" row, read
 * from orchestration/tools.py's own naming (agentic/backend) by way of
 * the tool name the trace already carries -- kept small and falling back
 * to a cleaned-up tool name for anything not listed, rather than an
 * exhaustive hardcoded set that drifts the moment a tool is renamed
 * (the same risk CapabilitiesPage.tsx's own static list already has). */
const TOOL_RESOURCE_NAMES: Record<string, string> = {
  build_rke2_cluster_plan: 'RKE2 cluster',
  build_storage_plan: 'Longhorn storage',
  build_minio_plan: 'MinIO tenant',
  build_inference_plan: 'Model serving (vLLM)',
  build_gateway_plan: 'Model gateway',
  build_policy_plan: 'Rate limiting policy',
  build_ingress_gateway_plan: 'Ingress gateway',
  build_route_plan: 'Route',
  build_valkey_plan: 'Valkey cache',
  build_cnpg_plan: 'CloudNativePG database',
  build_observability_plan: 'Observability stack',
  build_tokenizer_plan: 'Tokenizer',
  build_enricher_plan: 'Enricher',
  build_billing_plan: 'Billing',
  build_controlplane_plan: 'Control plane',
  build_portal_plan: 'Portal',
  build_accelerator_plan: 'GPU accelerator',
  build_full_stack_plan: 'Whole platform',
};

function resourceName(tool: string): string {
  return TOOL_RESOURCE_NAMES[tool] ?? tool.replace(/^build_/, '').replace(/_plan$/, '').replace(/_/g, ' ');
}

function capitalize(s: string): string {
  return s.length > 0 ? s[0].toUpperCase() + s.slice(1) : s;
}

/** What the trace's tool_call events say was actually built, for the
 * summary card below -- read from the validated args the SDK accepted,
 * not re-derived from the rendered script text, so it can never disagree
 * with what the script actually does. */
function summarizePlan(trace: TraceEvent[]): { resources: string; storage: string | null } {
  const calls = trace.filter((e): e is Extract<TraceEvent, { type: 'tool_call' }> => e.type === 'tool_call');
  const names = Array.from(new Set(calls.map((c) => resourceName(c.tool))));
  const resources = names.length > 0 ? names.join(', ') : '—';

  const storageCall = calls.find((c) => c.tool === 'build_storage_plan');
  let storage: string | null = null;
  if (storageCall) {
    const storageArgs = storageCall.args.storage as Record<string, unknown> | undefined;
    const type = typeof storageArgs?.type === 'string' ? capitalize(storageArgs.type) : 'Storage';
    const replicas = typeof storageArgs?.replica_count === 'number' ? storageArgs.replica_count : undefined;
    storage = replicas !== undefined ? `${type} (${replicas} replica${replicas === 1 ? '' : 's'})` : type;
  }
  return { resources, storage };
}

/** A quick-scan recap of what a turn built, in the shape of a resource's
 * own summary card rather than another thing to read in the reply text
 * or expand in the code block -- the trace already has the real,
 * SDK-validated facts (tool names and args), so this costs nothing new
 * to compute. "Estimated cost" is always N/A: the console never prices
 * anything (read-only by construction -- see the sidebar's own note). */
function PlanSummary({ trace, createdAt }: { trace: TraceEvent[]; createdAt?: number }) {
  const { resources, storage } = React.useMemo(() => summarizePlan(trace), [trace]);
  const created = createdAt
    ? new Date(createdAt).toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        year: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      })
    : '—';

  return (
    <div className="w-full min-w-0 overflow-hidden rounded-lg border border-subtle bg-neutral-50">
      <div className="flex items-center gap-2 border-b border-subtle px-3 py-2">
        <FileText className="h-4 w-4 shrink-0 text-primary-600" aria-hidden="true" />
        <span className="text-body font-semibold text-neutral-900">Plan summary</span>
      </div>
      <dl className="divide-y divide-subtle">
        {[
          ['Resources', resources],
          ...(storage ? [['Storage', storage]] : []),
          ['Estimated cost', 'N/A (simulation)'],
          ['Created', created],
        ].map(([label, value]) => (
          <div key={label} className="flex items-center justify-between gap-3 px-3 py-2 text-caption">
            <dt className="text-neutral-500">{label}</dt>
            <dd className="truncate text-right text-neutral-800">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** react-markdown renders a fenced code block as `<pre><code
 * className="language-x">...</code></pre>` and inline code as a bare
 * `<code>` with no wrapper -- overriding `pre` (not `code`) is what lets
 * this tell the two apart reliably, since newer react-markdown versions
 * dropped the old `inline` prop that used to do that job directly. */
function MarkdownPre({ children }: React.ComponentPropsWithoutRef<'pre'>) {
  const codeEl = React.isValidElement<{ className?: string; children?: React.ReactNode }>(children)
    ? children
    : null;
  if (!codeEl) return <pre>{children}</pre>;
  const lang = /language-(\w+)/.exec(codeEl.props.className ?? '')?.[1] || 'python';
  const code = String(codeEl.props.children ?? '').replace(/\n$/, '');
  return <InlineCodeBlock code={code} lang={lang} />;
}

/** Element styling for the model's reply text -- never raw HTML (both the
 * typed request and the model's reply are untrusted content; react-markdown
 * never renders raw HTML by default, which is exactly what's wanted here). */
const markdownComponents: Components = {
  p: ({ children }) => <p className="whitespace-pre-wrap">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noreferrer noopener" className="underline underline-offset-2">
      {children}
    </a>
  ),
  ul: ({ children }) => <ul className="list-disc space-y-1 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5">{children}</ol>,
  li: ({ children }) => <li>{children}</li>,
  h1: ({ children }) => <h1 className="text-cardTitle font-semibold">{children}</h1>,
  h2: ({ children }) => <h2 className="text-body font-semibold">{children}</h2>,
  h3: ({ children }) => <h3 className="text-body font-semibold">{children}</h3>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-subtle pl-3 italic text-neutral-500">{children}</blockquote>
  ),
  hr: () => <hr className="border-subtle" />,
  code: ({ children, className }) => (
    <code className={'rounded bg-neutral-200/60 px-1 py-0.5 font-mono text-[0.9em] ' + (className ?? '')}>
      {children}
    </code>
  ),
  pre: MarkdownPre,
  table: ({ children }) => (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border-b border-subtle px-2 py-1 font-semibold">{children}</th>,
  td: ({ children }) => <td className="border-b border-subtle px-2 py-1">{children}</td>,
};

function MessageBubble({
  role,
  content,
  trace,
  script,
  createdAt,
}: {
  role: 'user' | 'assistant';
  content: string;
  trace?: TraceEvent[];
  /** The script this turn produced, per the API's dedicated field --
   * appended as a fenced block only when the reply text didn't already
   * paste the script itself, so it's never shown twice. Only ever set
   * on an assistant message. */
  script?: string;
  /** When this reply arrived -- shown in the plan summary card's
   * "Created" row, only rendered alongside a script. */
  createdAt?: number;
}) {
  const isUser = role === 'user';
  const hasCode = FENCE.test(content) || Boolean(script);
  // If the model's reply text didn't already paste the script itself,
  // append it as a fenced block so it renders through the same code
  // panel as everything else -- a separate rendering path for "the
  // script" vs. "code the model happened to fence" would just be two
  // ways to show the same thing.
  const markdownSource =
    !FENCE.test(content) && script ? `${content}\n\n\`\`\`python\n${script}\n\`\`\`` : content;

  return (
    <div className={'flex min-w-0 gap-3 rounded-md p-2 ' + (isUser ? 'flex-row-reverse' : 'flex-row')}>
      <Avatar role={role} />
      <div
        className={'flex min-w-0 flex-col items-start gap-1.5 ' + (hasCode ? 'w-full max-w-full' : 'max-w-[75%]')}
      >
        {trace && trace.length > 0 && <TraceDisclosure events={trace} />}
        <div
          className={
            'flex min-w-0 flex-col gap-2 rounded-lg px-3 py-2 text-body ' +
            (hasCode ? 'w-full ' : '') +
            (isUser ? 'bg-primary-600 text-white' : 'bg-neutral-0 text-neutral-900 shadow-sm')
          }
        >
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
            {markdownSource}
          </ReactMarkdown>
        </div>
        {script && trace && trace.length > 0 && <PlanSummary trace={trace} createdAt={createdAt} />}
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
        if (event.type === 'sources') {
          return (
            <div key={i} className="flex flex-col gap-0.5 text-caption text-neutral-600">
              <span className="flex items-center gap-2">
                <BookOpen className="h-3.5 w-3.5 shrink-0 text-primary-600" aria-hidden="true" />
                Read {event.sources.length} reference passage{event.sources.length === 1 ? '' : 's'}
              </span>
              <ul className="ml-5 flex flex-col gap-0.5">
                {event.sources.map((source, n) => (
                  <li key={n} className="truncate font-mono text-neutral-500" title={source.context}>
                    {source.path}:{source.start}-{source.end}
                  </li>
                ))}
              </ul>
            </div>
          );
        }
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
  const read = events.some((e) => e.type === 'sources');
  const label =
    calls > 0 ? `${calls} tool call${calls === 1 ? '' : 's'}` : read ? 'Reference read' : 'Reasoning';
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

/** Monospace source with a line-number gutter -- a grid rather than a
 * <pre> so the numbers stay aligned to wrapped content and never scroll
 * independently of the code next to them. */
function CodeBlock({ script, className }: { script: string; className?: string }) {
  const lines = script.split('\n');
  return (
    <div className={'scrollbar-hide overflow-auto ' + (className ?? '')}>
      <div className="grid w-max min-w-full grid-cols-[auto_1fr] gap-x-4 px-3 py-2 font-mono text-caption">
        {lines.map((line, i) => (
          <React.Fragment key={i}>
            <span aria-hidden="true" className="select-none text-right text-neutral-500">
              {i + 1}
            </span>
            <span className="whitespace-pre text-neutral-800">{line.length > 0 ? line : ' '}</span>
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

/** Copy-to-clipboard button shown on every code block. */
function CopyButton({ script }: { script: string }) {
  const [copied, setCopied] = React.useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(script);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable (insecure context, permission denied) --
      // the script is still selectable text, so this is a degradation,
      // not a dead end.
    }
  }

  return (
    <Tooltip content={copied ? 'Copied' : 'Copy'}>
      <IconButton aria-label="Copy script" variant="ghost" onClick={() => void copy()}>
        {copied ? (
          <Check className="h-4 w-4 text-success-500" aria-hidden="true" />
        ) : (
          <Copy className="h-4 w-4" aria-hidden="true" />
        )}
      </IconButton>
    </Tooltip>
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
  const [attachment, setAttachment] = React.useState<Attachment | null>(null);
  const [attachmentError, setAttachmentError] = React.useState<string | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  // The in-flight turn's controller, so Stop has something to cancel --
  // null whenever nothing is running. One at a time: a new turn can't
  // start while `busy` is true anyway (see send()'s guard).
  const abortRef = React.useRef<AbortController | null>(null);
  const bottomRef = React.useRef<HTMLDivElement>(null);

  function stop() {
    abortRef.current?.abort();
  }

  // Follows the conversation to the bottom as it grows -- a new message,
  // and every trace event streamed in while one is still generating, so
  // the reply stays in view instead of growing off the bottom of the
  // scroll area while you watch it arrive.
  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages, busy, trace]);

  async function onFileSelected(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ''; // lets picking the same file again re-fire onChange
    if (!file) return;

    setAttachmentError(null);
    if (file.size > MAX_ATTACHMENT_BYTES) {
      setAttachmentError(`"${file.name}" is too large (max ${Math.round(MAX_ATTACHMENT_BYTES / 1000)}KB).`);
      return;
    }
    try {
      const content = await file.text();
      setAttachment({ name: file.name, content });
    } catch {
      setAttachmentError(`Could not read "${file.name}".`);
    }
  }

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
    const typed = draft.trim();
    // A file alone is a valid turn -- the backend only needs `message` to
    // be non-empty for the model to have something to read; the attached
    // file's content is what the user actually wants looked at. Requiring
    // typed text on top of an attachment was a no-op button, not a rule
    // worth keeping.
    if ((!typed && !attachment) || busy) return;
    const text = typed || `See the attached file (${attachment!.name}).`;

    // The attachment rides on this one turn only (see agentic/backend's
    // graph.py -- it's spliced into context for this call, not persisted),
    // so the chip clears once sent rather than silently reattaching itself
    // to every later message in the conversation.
    const sentAttachment = attachment ?? undefined;
    setMessages((prev) => [
      ...prev,
      {
        id: crypto.randomUUID(),
        role: 'user',
        content: sentAttachment ? `${text}\n\n📎 ${sentAttachment.name}` : text,
      },
    ]);
    setDraft('');
    setAttachment(null);
    setBusy(true);
    setTrace([]);
    setError(null);

    const controller = new AbortController();
    abortRef.current = controller;
    // Declared outside the try so a Stop mid-turn (caught below) can still
    // show whatever progress had already streamed in, instead of losing it.
    const steps: TraceEvent[] = [];

    try {
      // One turn arrives as a stream of events rather than a single
      // response: everything before "final" is progress to show live, and
      // "final" carries exactly what the blocking endpoints would have
      // returned. The events are collected locally as well as in state --
      // the closure's `trace` is a render-time snapshot and would be stale
      // by the time "final" arrives, but `steps` is always complete.
      for await (const event of streamMessage(sessionId, text, sentAttachment, controller.signal)) {
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
            {
              id: crypto.randomUUID(),
              role: 'assistant',
              content: event.message!,
              trace: steps,
              script: event.script ?? undefined,
              createdAt: Date.now(),
            },
          ]);
        }
      }
    } catch (e) {
      if (controller.signal.aborted) {
        // User-initiated Stop, not a failure -- keep whatever the turn
        // had done up to that point (steps collected so far) visible as
        // a normal reply instead of an error banner, the same way a
        // stopped generation in a chat client leaves a partial answer.
        setMessages((prev) => [
          ...prev,
          { id: crypto.randomUUID(), role: 'assistant', content: 'Stopped.', trace: steps },
        ]);
      } else {
        const message =
          e instanceof AgenticError ? e.message : 'Could not reach the agentic service. Check that it is running.';
        setError(message);
      }
    } finally {
      abortRef.current = null;
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
          <Button size="sm" variant="primary" onClick={onNewPlan} disabled={busy || messages.length === 0}>
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
                  description='Try: "a cluster called demo with a server node at 10.0.0.60, Longhorn storage with 2 replicas, and SeaweedFS object storage called models".'
                />
              </div>
            ) : (
              <div className="scrollbar-hide flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-4 py-4">
                <div className="flex w-full flex-1 flex-col gap-2">
                  {messages.map((m, i) => {
                    // Fall back to the session-level script for the last
                    // assistant message when it has none of its own -- a
                    // conversation reopened from a session saved before
                    // per-message scripts existed still has one at the
                    // session level (lib/storage.ts), just not attached to
                    // any particular reply. Without this, that script is
                    // known to exist ("script ready" in the status strip)
                    // but has nowhere to render.
                    const isLastAssistant = m.role === 'assistant' && i === messages.length - 1;
                    const messageScript = m.script ?? (isLastAssistant ? (script ?? undefined) : undefined);
                    return (
                      <MessageBubble
                        key={m.id}
                        role={m.role}
                        content={m.content}
                        trace={m.trace}
                        script={messageScript}
                        createdAt={m.createdAt}
                      />
                    );
                  })}
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
              <div className="flex w-full flex-col gap-2">
                {attachmentError && (
                  <Alert variant="error" title="Couldn't attach that file">
                    {attachmentError}
                  </Alert>
                )}
                {attachment && (
                  <div className="flex w-fit items-center gap-2 rounded-full border border-subtle bg-neutral-50 py-1 pl-3 pr-1.5 text-caption text-neutral-600">
                    <Paperclip className="h-3.5 w-3.5 shrink-0 text-neutral-500" aria-hidden="true" />
                    <span className="max-w-[16rem] truncate font-mono">{attachment.name}</span>
                    <IconButton
                      aria-label={`Remove "${attachment.name}"`}
                      variant="ghost"
                      onClick={() => setAttachment(null)}
                    >
                      <X className="h-3.5 w-3.5" aria-hidden="true" />
                    </IconButton>
                  </div>
                )}
                <div className="flex items-end gap-2">
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept={ATTACHMENT_ACCEPT}
                    onChange={(e) => void onFileSelected(e)}
                    className="hidden"
                  />
                  <Tooltip content="Attach a file">
                    <IconButton
                      aria-label="Attach a file"
                      variant="secondary"
                      disabled={busy}
                      onClick={() => fileInputRef.current?.click()}
                    >
                      <Paperclip className="h-4 w-4" aria-hidden="true" />
                    </IconButton>
                  </Tooltip>
                  <Textarea
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={onKeyDown}
                    rows={3}
                    aria-label="Describe the infrastructure you want"
                    placeholder="Describe the infrastructure…"
                    disabled={busy}
                  />
                  {busy ? (
                    <Button variant="secondary" onClick={stop}>
                      <Square className="h-4 w-4" aria-hidden="true" />
                      Stop
                    </Button>
                  ) : (
                    <Button onClick={() => void send()} disabled={draft.trim().length === 0 && !attachment}>
                      <SendHorizontal className="h-4 w-4" aria-hidden="true" />
                      Send
                    </Button>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
