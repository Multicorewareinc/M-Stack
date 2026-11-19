import { cn, IconButton, Tooltip } from '@multistack/ui';
import { ArrowRight, Search, X } from 'lucide-react';
import * as React from 'react';
import { listSessions, searchSessions, type StoredSession } from '../../lib/storage';

interface NavItem {
  label: string;
  id: string;
}

/** How many recents show before "View all" -- past this, the list would
 * push the "Read-only by construction" footer out of view or force the
 * sidebar itself to scroll awkwardly. Chosen to comfortably fit this
 * w-56 sidebar's default height alongside the nav above it. */
const RECENTS_LIMIT = 6;

const AGENTIC_NAV: NavItem[] = [
  { label: 'Build a plan', id: 'plan' },
  { label: 'Capabilities', id: 'capabilities' },
  { label: 'History', id: 'history' },
];

export function Sidebar({
  active,
  onSelect,
  onOpenSession,
  onNewPlan,
  activeSessionId,
}: {
  active: string;
  onSelect: (id: string) => void;
  /** Opens a saved conversation from the recents list or a search hit --
   * the same "jump to a plan" action App.tsx's openFromHistory provides
   * to the History page (see App.tsx), just reached from the sidebar's
   * own Recents/search here too. */
  onOpenSession: (sessionId: string) => void;
  /** "Build a plan" always starts a fresh conversation, the way ChatGPT's
   * "New chat" does -- it's not just a tab switch, since PlanPage stays
   * mounted with whatever conversation was already open (App.tsx). Without
   * this, clicking it while a plan was already in progress did nothing
   * visible if you were already on that tab. */
  onNewPlan: () => void;
  /** The session currently open in PlanPage, if any -- highlights that
   * one row the way ChatGPT's own sidebar highlights the open chat. */
  activeSessionId: string | null;
}) {
  const [searchOpen, setSearchOpen] = React.useState(false);
  const [query, setQuery] = React.useState('');
  const searchRef = React.useRef<HTMLInputElement>(null);

  // Re-read on every render rather than caching in state -- there's no
  // event for "a session was saved elsewhere," and this list is at most a
  // few dozen items, so re-reading localStorage each render costs nothing
  // worth a subscription mechanism for.
  const sessions = listSessions();
  const searching = query.trim().length > 0;
  const shown = searching ? searchSessions(query) : sessions;
  // Search results show everything; the default Recents view is capped,
  // with "View all" handing off to the full History page rather than
  // expanding in place -- one "see everything" surface, not two.
  const visible = searching ? shown : shown.slice(0, RECENTS_LIMIT);
  const hasMore = !searching && shown.length > RECENTS_LIMIT;

  function toggleSearch() {
    setSearchOpen((open) => {
      const next = !open;
      if (!next) setQuery('');
      return next;
    });
  }

  React.useEffect(() => {
    if (searchOpen) searchRef.current?.focus();
  }, [searchOpen]);

  function openSession(sessionId: string) {
    setQuery('');
    setSearchOpen(false);
    onOpenSession(sessionId);
  }

  return (
    <nav aria-label="Primary" className="flex w-56 shrink-0 flex-col gap-1 border-r border-subtle bg-neutral-0 p-3">
      <div className="flex items-center justify-between px-2 pb-2">
        <p className="text-body font-medium text-neutral-700">agentic</p>
        <Tooltip content={searchOpen ? 'Close search' : 'Search chats'}>
          <IconButton
            aria-label={searchOpen ? 'Close search' : 'Search chats'}
            aria-expanded={searchOpen}
            variant="ghost"
            onClick={toggleSearch}
          >
            {searchOpen ? <X className="h-4 w-4" aria-hidden="true" /> : <Search className="h-4 w-4" aria-hidden="true" />}
          </IconButton>
        </Tooltip>
      </div>
      <div className="mb-2 border-t border-subtle" />

      {searchOpen && (
        <input
          ref={searchRef}
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') toggleSearch();
          }}
          placeholder="Search chats…"
          aria-label="Search chats"
          className="mb-1 rounded-md border border-default bg-neutral-50 px-2.5 py-1.5 text-body text-neutral-800 placeholder:text-neutral-500 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-600"
        />
      )}

      {AGENTIC_NAV.map((item) => (
        <button
          key={item.id}
          type="button"
          aria-current={active === item.id && !activeSessionId ? 'page' : undefined}
          onClick={() => (item.id === 'plan' ? onNewPlan() : onSelect(item.id))}
          className={cn(
            'rounded-md px-2 py-1.5 text-left text-body font-medium text-neutral-600 hover:bg-neutral-100',
            active === item.id && !activeSessionId && 'border-l-2 border-primary-600 bg-primary-50 font-semibold text-primary-700',
          )}
        >
          {item.label}
        </button>
      ))}

      <p className="mt-3 px-2 pb-1 text-caption font-semibold text-neutral-500">
        {searching ? 'Search results' : 'Recents'}
      </p>
      <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto">
        {shown.length === 0 ? (
          <p className="px-2 py-1 text-caption text-neutral-500">
            {searching ? `No chats match "${query.trim()}".` : 'A conversation appears here once the agent replies.'}
          </p>
        ) : (
          <>
            {visible.map((s: StoredSession) => (
              <button
                key={s.sessionId}
                type="button"
                onClick={() => openSession(s.sessionId)}
                aria-current={s.sessionId === activeSessionId ? 'page' : undefined}
                className={cn(
                  'truncate rounded-md px-2 py-1.5 text-left text-body text-neutral-600 hover:bg-neutral-100',
                  s.sessionId === activeSessionId && 'bg-neutral-100 font-medium text-neutral-900',
                )}
              >
                {s.title}
              </button>
            ))}
            {hasMore && (
              <button
                type="button"
                onClick={() => onSelect('history')}
                className="flex items-center gap-1 rounded-md px-2 py-1.5 text-left text-body font-medium text-primary-600 hover:bg-neutral-100"
              >
                View all
                <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
              </button>
            )}
          </>
        )}
      </div>

      <div className="mt-auto border-t border-subtle px-2 pt-3">
        <p className="text-caption text-neutral-500">
          Read-only by construction. This console can generate a script; it cannot run one.
        </p>
      </div>
    </nav>
  );
}
