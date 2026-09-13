import { useRef } from "react";

import type { PatchAttempt } from "../types";
import { OUTCOME_LABELS } from "./VerificationPanel";

/**
 * A tab strip for selecting one attempt.
 *
 * This replaces a vertical stack of `<details>` blocks, each holding a whole
 * attempt's diff, events, and container logs. Stacking them meant the page grew
 * with the number of attempts even though only one is ever being read.
 *
 * A real tablist, not a row of buttons: `role="tablist"` with `role="tab"` and
 * `role="tabpanel"`, `aria-selected`, `aria-controls`, roving `tabIndex` (only
 * the active tab is in the tab order), and Left/Right/Home/End key handling.
 *
 * Deliberately NOT modelled on `NewRunForm`'s `role="radiogroup"`: that control
 * has no `onKeyDown` and no roving `tabIndex`, so it announces a keyboard
 * contract it does not honour. Copying it would spread the bug.
 */

/** One line summarising an attempt, for the tab's second row. */
function summarise(attempt: PatchAttempt): string {
  const verification = attempt.verification;
  if (verification?.outcome) {
    return OUTCOME_LABELS[verification.outcome] ?? verification.outcome;
  }
  if (verification) return `verification ${verification.status}`;
  if (attempt.pipeline_error_kind) return "verification never started";
  if (attempt.status === "queued") return "waiting for a slot";
  if (attempt.status === "running") return "in progress";
  return "no verification";
}

interface Props {
  attempts: PatchAttempt[];
  selectedIndex: number;
  onSelect: (attemptIndex: number) => void;
  /** Highlighted in the strip, so the recommended attempt is findable at a glance. */
  recommendedIndex: number | null;
  /**
   * Shared id prefix, owned by the caller.
   *
   * The tabs and their panel must agree on `aria-controls` / `aria-labelledby`,
   * and they render in different components. Generating the prefix inside this
   * one would give the panel a different value and silently break the pairing —
   * the markup would still look correct while announcing nothing.
   */
  baseId: string;
}

export function AttemptTabs({
  attempts,
  selectedIndex,
  onSelect,
  recommendedIndex,
  baseId,
}: Props) {
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  function focusTab(position: number) {
    const target = tabRefs.current[position];
    if (target) {
      target.focus();
      const attempt = attempts[position];
      if (attempt) onSelect(attempt.attempt_index);
    }
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLButtonElement>, position: number) {
    const last = attempts.length - 1;
    let next: number | null = null;
    if (event.key === "ArrowRight") next = position === last ? 0 : position + 1;
    else if (event.key === "ArrowLeft") next = position === 0 ? last : position - 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = last;
    if (next !== null) {
      event.preventDefault();
      focusTab(next);
    }
  }

  return (
    <div
      className="attemptTabs"
      role="tablist"
      aria-label={`Attempts (${attempts.length})`}
    >
      {attempts.map((attempt, position) => {
        const selected = attempt.attempt_index === selectedIndex;
        const recommended = recommendedIndex === attempt.attempt_index;
        return (
          <button
            key={attempt.id}
            ref={(node) => {
              tabRefs.current[position] = node;
            }}
            type="button"
            role="tab"
            id={`${baseId}-tab-${attempt.attempt_index}`}
            aria-selected={selected}
            aria-controls={`${baseId}-panel-${attempt.attempt_index}`}
            // Roving tabIndex: one stop for the whole strip, then arrow keys.
            tabIndex={selected ? 0 : -1}
            className={`attemptTab${selected ? " attemptTab--active" : ""}`}
            onClick={() => onSelect(attempt.attempt_index)}
            onKeyDown={(event) => handleKeyDown(event, position)}
          >
            <span className="attemptTab__title">
              Attempt {attempt.attempt_index}
              {recommended ? <span className="attemptTab__star"> · recommended</span> : null}
            </span>
            <span className="attemptTab__summary">{summarise(attempt)}</span>
          </button>
        );
      })}
    </div>
  );
}

/** The panel the strip controls. Kept here so the id scheme stays in one file. */
export function AttemptTabPanel({
  baseId,
  attemptIndex,
  children,
}: {
  baseId: string;
  attemptIndex: number;
  children: React.ReactNode;
}) {
  return (
    <div
      role="tabpanel"
      id={`${baseId}-panel-${attemptIndex}`}
      aria-labelledby={`${baseId}-tab-${attemptIndex}`}
      tabIndex={0}
    >
      {children}
    </div>
  );
}
