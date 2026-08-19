import type { ReactNode } from "react";

interface Props {
  role: "user" | "system";
  children: ReactNode;
}

export default function MessageBubble({ role, children }: Props) {
  return (
    <div className={`message-row ${role}`}>
      <div className={`message-bubble ${role}`}>{children}</div>
    </div>
  );
}
