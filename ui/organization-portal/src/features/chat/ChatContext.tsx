import * as React from 'react';
import { ApiError } from '../../api/client';
import { useAuth } from '../../auth/AuthContext';
import { useToast } from '../../app/ToastProvider';
import { ensureChatKey } from './chatKey';
import { listGatewayModels } from './gatewayModels';
import { streamChat, type RateLimitUsage } from './streamChat';

/**
 * Chat playground orchestration state. In-memory only for now — no server-side persistence
 * (conversation history/search/delete is intentionally not wired up; see modules/chat on
 * organization-control-plane, which stays unused until that's revisited). This page exists
 * to demonstrate the Model Gateway's RPM/TPM rate limiting end-to-end, not as a durable chat
 * history product: a page refresh or tab close loses the transcript.
 *
 * Inference streaming happens browser -> Model Gateway DIRECTLY via streamChat (never proxied
 * through organization-control-plane) — authenticated with a per-user API key.
 */

export interface ChatModel {
  id: string;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
}

interface ChatContextValue {
  models: ChatModel[];
  modelsLoading: boolean;
  selectedModelId: string | null;
  selectModel: (id: string) => void;
  catalogEmpty: boolean;

  messages: ChatMessage[];
  isStreaming: boolean;
  streamingText: string;

  /** Set when the gateway's RPM/TPM policy denies a request (429) — shown as a banner above the
   * composer, not just a transient toast, since the user needs to see it before they try again. */
  rateLimitMessage: string | null;
  /** Current RPM/TPM usage against the org's plan, read off the gateway's response headers on
   * every send (success or deny) — powers the usage/billing showcase panel. */
  rateLimitUsage: RateLimitUsage;

  newChat: () => void;
  send: (text: string) => Promise<void>;
  canSend: boolean;
}

const ChatContext = React.createContext<ChatContextValue | undefined>(undefined);

function toGatewayMessages(
  messages: ChatMessage[],
  next: { role: 'user'; content: string },
): Array<{ role: string; content: string }> {
  return [...messages.map((m) => ({ role: m.role, content: m.content })), next];
}

export function ChatProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const toast = useToast();

  const [models, setModels] = React.useState<ChatModel[]>([]);
  const [modelsLoading, setModelsLoading] = React.useState(true);
  const [selectedModelId, setSelectedModelId] = React.useState<string | null>(null);

  const [messages, setMessages] = React.useState<ChatMessage[]>([]);
  const [isStreaming, setIsStreaming] = React.useState(false);
  const [streamingText, setStreamingText] = React.useState('');
  const [rateLimitMessage, setRateLimitMessage] = React.useState<string | null>(null);
  const [rateLimitUsage, setRateLimitUsage] = React.useState<RateLimitUsage>({});

  const chatKeyRef = React.useRef<string | null>(null);
  const abortRef = React.useRef<AbortController | null>(null);
  const rateLimitTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const nextIdRef = React.useRef(0);

  React.useEffect(
    () => () => {
      if (rateLimitTimerRef.current) clearTimeout(rateLimitTimerRef.current);
    },
    [],
  );

  const ensureKey = React.useCallback(async (): Promise<string> => {
    if (chatKeyRef.current) return chatKeyRef.current;
    const key = await ensureChatKey(user.id);
    chatKeyRef.current = key;
    return key;
  }, [user.id]);

  // Load the model catalog once (needs the chat key too — GET /v1/models is on the same
  // per-request-authenticated gateway surface as chat/completions).
  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const key = await ensureKey();
        const list = await listGatewayModels(key);
        if (cancelled) return;
        setModels(list);
        setSelectedModelId((current) => current ?? list[0]?.id ?? null);
      } catch (err) {
        if (!cancelled) toast.error('Unable to load models', err instanceof ApiError ? err.message : undefined);
      } finally {
        if (!cancelled) setModelsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- run once on mount
  }, []);

  const catalogEmpty = !modelsLoading && models.length === 0;

  const newChat = React.useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setMessages([]);
    setStreamingText('');
    setIsStreaming(false);
  }, []);

  const send = React.useCallback(
    async (text: string) => {
      const content = text.trim();
      if (!content || isStreaming || !selectedModelId) return;

      // A fresh attempt gets a fresh chance — clear any earlier rate-limit banner rather than
      // leaving a stale one up once its retry-after window has plausibly passed.
      if (rateLimitTimerRef.current) {
        clearTimeout(rateLimitTimerRef.current);
        rateLimitTimerRef.current = null;
      }
      setRateLimitMessage(null);

      const priorMessages = messages;
      const userMsg: ChatMessage = { id: `local-${nextIdRef.current++}`, role: 'user', content };
      setMessages((m) => [...m, userMsg]);

      const gatewayMessages = toGatewayMessages(priorMessages, { role: 'user', content });

      const abort = new AbortController();
      abortRef.current = abort;
      setIsStreaming(true);
      setStreamingText('');

      const runStream = async (key: string): Promise<string> => {
        let assembled = '';
        for await (const delta of streamChat({
          apiKey: key,
          model: selectedModelId,
          messages: gatewayMessages,
          signal: abort.signal,
          onRateLimitUsage: (usage) => setRateLimitUsage((prev) => ({ ...prev, ...usage })),
        })) {
          assembled += delta;
          setStreamingText(assembled);
        }
        return assembled;
      };

      try {
        let key = await ensureKey();
        let assembled: string;
        try {
          assembled = await runStream(key);
        } catch (err) {
          if (err instanceof ApiError && err.status === 401) {
            chatKeyRef.current = null;
            key = await ensureKey();
            assembled = await runStream(key);
          } else {
            throw err;
          }
        }

        if (!assembled.trim()) {
          toast.error('The model returned an empty response.');
        } else {
          setMessages((m) => [...m, { id: `local-${nextIdRef.current++}`, role: 'assistant', content: assembled }]);
        }
      } catch (err) {
        if (err instanceof ApiError && err.code === 'RATE_LIMITED') {
          setRateLimitMessage(err.message);
          if (err.retryAfter) {
            rateLimitTimerRef.current = setTimeout(() => setRateLimitMessage(null), err.retryAfter * 1000);
          }
        } else {
          toast.error('Unable to get a response', err instanceof ApiError ? err.message : undefined);
        }
      } finally {
        setStreamingText('');
        setIsStreaming(false);
        abortRef.current = null;
      }
    },
    [isStreaming, selectedModelId, messages, ensureKey, toast],
  );

  const value: ChatContextValue = {
    models,
    modelsLoading,
    selectedModelId,
    selectModel: setSelectedModelId,
    catalogEmpty,
    messages,
    isStreaming,
    streamingText,
    rateLimitMessage,
    rateLimitUsage,
    newChat,
    send,
    canSend: !catalogEmpty && Boolean(selectedModelId) && !isStreaming,
  };

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}

export function useChat(): ChatContextValue {
  const ctx = React.useContext(ChatContext);
  if (!ctx) throw new Error('useChat must be used within a ChatProvider');
  return ctx;
}
