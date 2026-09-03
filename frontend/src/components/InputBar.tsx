import { useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import type { CreateTurnInput } from "../api/client";

interface Props {
  disabled?: boolean;
  onSubmit: (input: CreateTurnInput) => Promise<void> | void;
}

// One free-text box for everything: a single lookup, a pasted table, or
// text accompanying an attached file. There is no mode to pick — the
// backend decides what the text is from its own shape (see
// app/api.py's `_looks_like_pasted_table`). A "+" attaches an optional
// spreadsheet, mirroring a familiar chat-input pattern instead of a row of
// toggle buttons for three input "modes" that were never mutually exclusive
// to begin with.
export default function InputBar({ disabled, onSubmit }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const canSubmit = !submitting && !disabled && (file != null || text.trim().length > 0);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const input: CreateTurnInput = { file, text: text.trim() ? text.trim() : null };
      await onSubmit(input);
      setFile(null);
      setText("");
      if (fileInputRef.current) fileInputRef.current.value = "";
      if (textareaRef.current) textareaRef.current.style.height = "auto";
    } finally {
      setSubmitting(false);
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // `e.key` is the right check for a real keyboard, but some input paths
    // (IME composition, certain automation/virtual-keyboard input) report
    // it as "Unidentified" while still setting `keyCode`/`which` to 13 --
    // checking both keeps Enter-to-send working in those cases too.
    const isEnter = e.key === "Enter" || e.keyCode === 13 || e.which === 13;
    if (isEnter && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit(e);
    }
  }

  function handleTextChange(value: string) {
    setText(value);
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
    }
  }

  return (
    <form className="input-bar" onSubmit={handleSubmit}>
      {file && (
        <div className="attached-file-chip">
          <span className="attached-file-name">📎 {file.name}</span>
          <button
            type="button"
            className="attached-file-remove"
            aria-label="Remove attached file"
            onClick={() => {
              setFile(null);
              if (fileInputRef.current) fileInputRef.current.value = "";
            }}
          >
            ×
          </button>
        </div>
      )}

      <div className="input-bar-row">
        <input
          ref={fileInputRef}
          type="file"
          accept=".xlsx"
          className="attach-file-input"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        <button
          type="button"
          className="attach-button"
          aria-label="Attach a spreadsheet"
          title="Attach a spreadsheet"
          disabled={disabled}
          onClick={() => fileInputRef.current?.click()}
        >
          +
        </button>

        <textarea
          ref={textareaRef}
          className="input-bar-textarea"
          placeholder='Ask anything — e.g. "Find Jane Doe at Acme Corp", or paste a table…'
          value={text}
          onChange={(e) => handleTextChange(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
          disabled={disabled}
        />

        <button
          type="submit"
          className="send-button"
          aria-label="Send"
          title="Send"
          disabled={!canSubmit}
        >
          {submitting ? "…" : "↑"}
        </button>
      </div>
    </form>
  );
}
