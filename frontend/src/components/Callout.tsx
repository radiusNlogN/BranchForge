import type { ReactNode } from "react";

type Tone = "info" | "error";

interface CalloutProps {
  tone?: Tone;
  title: string;
  children?: ReactNode;
  onRetry?: () => void;
}

export function Callout({ tone = "info", title, children, onRetry }: CalloutProps) {
  return (
    <div className={`callout callout--${tone}`} role={tone === "error" ? "alert" : undefined}>
      <div className="callout__body">
        <p className="callout__title">{title}</p>
        {children ? <div className="callout__text">{children}</div> : null}
      </div>
      {onRetry ? (
        <button type="button" className="button button--ghost" onClick={onRetry}>
          Retry
        </button>
      ) : null}
    </div>
  );
}
