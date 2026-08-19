import { useCallback, useEffect, useRef, useState } from "react";
import "./App.css";
import type { SessionSummary } from "./api/types";
import { isGreetingTurn } from "./api/types";
import {
  apiErrorMessage,
  createSession,
  createTurn,
  getSession,
  listSessions,
  type CreateTurnInput,
} from "./api/client";
import InputBar from "./components/InputBar";
import MessageBubble from "./components/MessageBubble";
import BatchCard from "./components/BatchCard";
import SessionSidebar from "./components/SessionSidebar";
import { newMessageId, replaySession, type ChatMessage } from "./lib/chatThread";

function describeInput(input: CreateTurnInput): string {
  const parts: string[] = [];
  if (input.file) parts.push(`Uploaded file: ${input.file.name}`);
  if (input.pastedText) {
    const preview = input.pastedText.split("\n").slice(0, 3).join(" / ");
    parts.push(`Pasted table: ${preview}${input.pastedText.split("\n").length > 3 ? "…" : ""}`);
  }
  if (input.instructionsText) parts.push(input.instructionsText);
  return parts.join("\n") || "(empty)";
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [replaying, setReplaying] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const refreshSessions = useCallback(() => {
    return listSessions()
      .then((res) => {
        // A session is created on every page load / "New chat" click, so the
        // list is filtered to ones that actually contain a conversation —
        // otherwise the sidebar fills with blank rows nobody can reopen.
        setSessions(res.sessions.filter((s) => s.turn_count > 0));
        setSessionsError(null);
      })
      .catch((err) => setSessionsError(apiErrorMessage(err)))
      .finally(() => setSessionsLoading(false));
  }, []);

  useEffect(() => {
    void refreshSessions();
    createSession()
      .then((res) => setSessionId(res.session_id))
      .catch((err) => setSessionError(apiErrorMessage(err)));
  }, [refreshSessions]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function handleSubmit(input: CreateTurnInput) {
    if (!sessionId) return;
    setMessages((prev) => [
      ...prev,
      { id: newMessageId(), role: "user", kind: "text", text: describeInput(input) },
    ]);
    try {
      const turn = await createTurn(sessionId, input);
      if (isGreetingTurn(turn)) {
        // Chitchat: nothing was ingested, no batch exists — just the reply.
        setMessages((prev) => [
          ...prev,
          { id: newMessageId(), role: "system", kind: "reply", text: turn.reply },
        ]);
      } else {
        setMessages((prev) => [
          ...prev,
          {
            id: newMessageId(),
            role: "system",
            kind: "batch",
            batch: {
              batchId: turn.batch_id,
              columns: turn.columns,
              sampleRows: turn.sample_rows,
              fieldMappings: turn.proposal.field_mappings,
              selectedFields: turn.proposal.selected_fields,
              outputFormat: turn.output_format,
            },
          },
        ]);
      }
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { id: newMessageId(), role: "system", kind: "error", text: apiErrorMessage(err) },
      ]);
    } finally {
      // Order/preview changed, and a brand-new session now has its first turn.
      refreshSessions();
    }
  }

  async function handleSelectSession(id: string) {
    if (id === sessionId && messages.length > 0) {
      setSidebarOpen(false);
      return;
    }
    setSidebarOpen(false);
    setReplaying(true);
    setSessionError(null);
    setMessages([]);
    try {
      const payload = await getSession(id);
      // Reopening makes it the active session: the next turn continues this
      // conversation rather than starting a new one.
      setSessionId(payload.session_id);
      setMessages(replaySession(payload));
    } catch (err) {
      setSessionError(apiErrorMessage(err));
    } finally {
      setReplaying(false);
    }
  }

  async function handleNewChat() {
    setSidebarOpen(false);
    setSessionError(null);
    setMessages([]);
    try {
      const res = await createSession();
      setSessionId(res.session_id);
    } catch (err) {
      setSessionError(apiErrorMessage(err));
    }
  }

  return (
    <div className={`app-layout${sidebarOpen ? " sidebar-open" : ""}`}>
      <SessionSidebar
        sessions={sessions}
        activeSessionId={sessionId}
        loading={sessionsLoading}
        error={sessionsError}
        onSelect={handleSelectSession}
        onNewChat={handleNewChat}
      />

      <div className="app-shell">
        <header className="app-header">
          <button
            type="button"
            className="sidebar-toggle"
            aria-label="Toggle session history"
            onClick={() => setSidebarOpen((v) => !v)}
          >
            ☰
          </button>
          <h1>LinkedIn Enrichment</h1>
          <span className="session-indicator">
            {sessionId
              ? `Session ${sessionId.slice(0, 8)}`
              : sessionError
                ? "Session error"
                : "Starting session…"}
          </span>
        </header>

        <main className="thread">
          {replaying && <p className="empty-hint">Reopening session…</p>}
          {!replaying && messages.length === 0 && (
            <p className="empty-hint">
              Upload a spreadsheet, paste a table, or type a single lookup (e.g. "Find Jane Doe at
              Acme Corp") to get started.
            </p>
          )}
          {messages.map((m) => {
            if (m.kind === "text" || m.kind === "reply") {
              return (
                <MessageBubble key={m.id} role={m.role}>
                  <pre className="plain-text">{m.text}</pre>
                </MessageBubble>
              );
            }
            if (m.kind === "batch") {
              return (
                <MessageBubble key={m.id} role="system">
                  <BatchCard
                    key={m.id}
                    sessionId={sessionId!}
                    batch={m.batch}
                    resume={m.resume}
                  />
                </MessageBubble>
              );
            }
            if (m.kind === "error") {
              return (
                <MessageBubble key={m.id} role="system">
                  <div className="error-text">{m.text}</div>
                </MessageBubble>
              );
            }
            return (
              <MessageBubble key={m.id} role="system">
                <span className="hint">{m.text}</span>
              </MessageBubble>
            );
          })}
          <div ref={bottomRef} />
        </main>

        <footer className="app-footer">
          {sessionError && (
            <div className="error-text">
              Session problem: {sessionError}. Is the backend running at{" "}
              {import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000"}?
            </div>
          )}
          <InputBar disabled={!sessionId || replaying} onSubmit={handleSubmit} />
        </footer>
      </div>

      {sidebarOpen && (
        <div className="sidebar-scrim" onClick={() => setSidebarOpen(false)} aria-hidden="true" />
      )}
    </div>
  );
}
