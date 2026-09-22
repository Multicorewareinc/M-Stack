/**
 * Browser-local persistence for agentic conversations.
 *
 * The agentic service itself keeps state in memory only (see
 * agentic/README.md's Known Limitations -- a restart loses every thread),
 * and its HTTP API only ever returns the LATEST message and script, never
 * the full transcript (agentic/backend/src/router.py's SessionResponse). So the
 * only place a full, revisitable chat history can live is here, in the
 * browser that already holds it in React state -- this just persists that
 * state to localStorage instead of throwing it away.
 *
 * This means history is per-browser, not shared, and does not survive
 * "clear site data." Both are the right tradeoffs for a mock console; a
 * real deployment would persist server-side instead.
 */

import type { TraceEvent } from '../api/agentic';

export interface StoredMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  /** For an assistant message, the tool calls and reasoning that produced
   * it. Persisted with the reply so reopening a conversation can still
   * show how the agent got there -- the service keeps none of this, and
   * re-running the turn would not reproduce it. Absent on user messages,
   * and on assistant messages saved before traces were recorded. */
  trace?: TraceEvent[];
}

export interface StoredSession {
  sessionId: string;
  title: string;
  updatedAt: number;
  messages: StoredMessage[];
  script: string | null;
  status: 'needs_input' | 'resolved';
}

const KEY = 'agentic-console:sessions:v1';

function readAll(): StoredSession[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as StoredSession[]) : [];
  } catch {
    // Corrupt JSON, or storage unavailable (private browsing) -- an empty
    // history is the right degradation, not a crashed console.
    return [];
  }
}

function writeAll(sessions: StoredSession[]): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(sessions));
  } catch {
    // Quota exceeded or storage blocked -- losing the newest save is
    // better than throwing out of a message send.
  }
}

/** Every saved conversation, most recently updated first. */
export function listSessions(): StoredSession[] {
  return readAll().sort((a, b) => b.updatedAt - a.updatedAt);
}

export function getStoredSession(sessionId: string): StoredSession | undefined {
  return readAll().find((s) => s.sessionId === sessionId);
}

/** Upserts by sessionId -- called after every turn once a session exists. */
export function saveSession(session: StoredSession): void {
  const all = readAll();
  const idx = all.findIndex((s) => s.sessionId === session.sessionId);
  if (idx >= 0) all[idx] = session;
  else all.push(session);
  writeAll(all);
}

export function deleteSession(sessionId: string): void {
  writeAll(readAll().filter((s) => s.sessionId !== sessionId));
}

/** A short, human title from the conversation's first message. */
export function titleFrom(message: string): string {
  const trimmed = message.trim().replace(/\s+/g, ' ');
  if (!trimmed) return 'Untitled plan';
  return trimmed.length > 64 ? `${trimmed.slice(0, 61)}…` : trimmed;
}
