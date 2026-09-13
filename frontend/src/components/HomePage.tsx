import { API_BASE_URL } from "../api";
import type { FieldErrors } from "../api";
import type { Run, RunCreateInput } from "../types";
import { Callout } from "./Callout";
import { NewRunForm } from "./NewRunForm";
import { NotImplementedNote } from "./NotImplementedNote";
import { RunList } from "./RunList";

/**
 * The list route: create a run, and pick one to open.
 *
 * Presentational. Every fetch still lives in `App.tsx`, so this component keeps
 * no state of its own beyond what `NewRunForm` owns internally.
 *
 * The full scope note lives here and only here. It used to render twice at once
 * — full at the top of the page and compact inside the detail pane — which said
 * the same thing to the reader twice on one screen.
 */

interface Props {
  runs: Run[] | null;
  listError: string | null;
  onReloadRuns: () => void;
  onOpenRun: (runId: string) => void;

  submitting: boolean;
  submitError: string | null;
  fieldErrors: FieldErrors;
  onSubmit: (input: RunCreateInput) => Promise<boolean>;
  onDismissFieldError: (field: keyof RunCreateInput) => void;
}

export function HomePage({
  runs,
  listError,
  onReloadRuns,
  onOpenRun,
  submitting,
  submitError,
  fieldErrors,
  onSubmit,
  onDismissFieldError,
}: Props) {
  return (
    <>
      <NotImplementedNote />

      {listError ? (
        <Callout tone="error" title="Could not load runs" onRetry={onReloadRuns}>
          <p>{listError}</p>
          <p className="muted">
            API base URL: <code>{API_BASE_URL}</code>
          </p>
        </Callout>
      ) : null}

      <div className="layout">
        <div className="layout__column">
          <NewRunForm
            onSubmit={onSubmit}
            submitting={submitting}
            serverFieldErrors={fieldErrors}
            onDismissFieldError={onDismissFieldError}
          />
          {submitError ? (
            <Callout tone="error" title="Run was not created">
              <p>{submitError}</p>
            </Callout>
          ) : null}
        </div>

        <div className="layout__column">
          {/* Nothing is "selected" on this route any more — opening a run is a
              navigation, so no row is rendered in a selected state. */}
          <RunList runs={runs} selectedId={null} onSelect={onOpenRun} />
        </div>
      </div>
    </>
  );
}
