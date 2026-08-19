import { useRef, useState, type FormEvent } from "react";
import type { CreateTurnInput } from "../api/client";

type Mode = "file" | "paste" | "instructions";

interface Props {
  disabled?: boolean;
  onSubmit: (input: CreateTurnInput) => Promise<void> | void;
}

export default function InputBar({ disabled, onSubmit }: Props) {
  const [mode, setMode] = useState<Mode>("file");
  const [file, setFile] = useState<File | null>(null);
  const [pastedText, setPastedText] = useState("");
  const [instructionsText, setInstructionsText] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const canSubmit =
    !submitting &&
    !disabled &&
    ((mode === "file" && file != null) ||
      (mode === "paste" && pastedText.trim().length > 0) ||
      (mode === "instructions" && instructionsText.trim().length > 0));

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const input: CreateTurnInput = {
        file: mode === "file" ? file : null,
        pastedText: mode === "paste" ? pastedText : null,
        instructionsText: instructionsText.trim() ? instructionsText.trim() : null,
      };
      await onSubmit(input);
      setFile(null);
      setPastedText("");
      setInstructionsText("");
      if (fileInputRef.current) fileInputRef.current.value = "";
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="input-bar" onSubmit={handleSubmit}>
      <div className="input-mode-tabs">
        <button
          type="button"
          className={mode === "file" ? "active" : ""}
          onClick={() => setMode("file")}
        >
          Upload file
        </button>
        <button
          type="button"
          className={mode === "paste" ? "active" : ""}
          onClick={() => setMode("paste")}
        >
          Paste table
        </button>
        <button
          type="button"
          className={mode === "instructions" ? "active" : ""}
          onClick={() => setMode("instructions")}
        >
          Single lookup
        </button>
      </div>

      {mode === "file" && (
        <input
          ref={fileInputRef}
          type="file"
          accept=".xlsx"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      )}

      {mode === "paste" && (
        <textarea
          placeholder="Paste tab- or comma-delimited rows, including a header row…"
          value={pastedText}
          onChange={(e) => setPastedText(e.target.value)}
          rows={4}
        />
      )}

      <textarea
        className="instructions-textarea"
        placeholder={
          mode === "instructions"
            ? "e.g. Find Jane Doe at Acme Corp"
            : "Optional instructions to accompany this upload…"
        }
        value={instructionsText}
        onChange={(e) => setInstructionsText(e.target.value)}
        rows={2}
      />

      <button type="submit" className="primary send-button" disabled={!canSubmit}>
        {submitting ? "Sending…" : "Send"}
      </button>
    </form>
  );
}
