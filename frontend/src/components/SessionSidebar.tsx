import type { SessionSummary } from "../api/types";
import { formatRelativeTime } from "../lib/formatRelativeTime";

interface Props {
  sessions: SessionSummary[];
  activeSessionId: string | null;
  loading: boolean;
  error: string | null;
  onSelect: (sessionId: string) => void;
  onNewChat: () => void;
}

export default function SessionSidebar({
  sessions,
  activeSessionId,
  loading,
  error,
  onSelect,
  onNewChat,
}: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <button type="button" className="primary new-chat-button" onClick={onNewChat}>
          + New chat
        </button>
      </div>

      <nav className="session-list" aria-label="Past sessions">
        {loading && <p className="hint sidebar-note">Loading sessions…</p>}
        {error && <p className="error-text sidebar-note">{error}</p>}
        {!loading && !error && sessions.length === 0 && (
          <p className="hint sidebar-note">No past sessions yet.</p>
        )}
        {sessions.map((session) => (
          <button
            key={session.session_id}
            type="button"
            className={`session-row${session.session_id === activeSessionId ? " active" : ""}`}
            onClick={() => onSelect(session.session_id)}
            title={session.first_turn_preview || session.session_id}
          >
            <span className="session-row-title">
              {session.first_turn_preview || `Session ${session.session_id.slice(0, 8)}`}
            </span>
            <span className="session-row-meta">
              {formatRelativeTime(session.last_active_at)} · {session.turn_count} turn
              {session.turn_count === 1 ? "" : "s"}
            </span>
          </button>
        ))}
      </nav>
    </aside>
  );
}
