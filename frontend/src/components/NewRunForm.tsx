import { useState } from "react";
import type { FormEvent } from "react";

import type { FieldErrors } from "../api";
import { ATTEMPT_CHOICES, MAX_ISSUE_DESCRIPTION_LENGTH } from "../types";
import type { RunCreateInput } from "../types";

interface NewRunFormProps {
  onSubmit: (input: RunCreateInput) => Promise<boolean>;
  submitting: boolean;
  /** Field messages returned by the server (authoritative). */
  serverFieldErrors: FieldErrors;
  /** Called when a field is edited, so its stale server message can be cleared. */
  onDismissFieldError: (field: keyof RunCreateInput) => void;
}

/** Fast local feedback. The server remains the authority on validity. */
function localErrors(input: RunCreateInput): FieldErrors {
  const errors: FieldErrors = {};

  const url = input.repository_url.trim();
  if (!url) {
    errors.repository_url = "Enter a repository URL.";
  } else if (!url.startsWith("https://github.com/")) {
    errors.repository_url = "Must be an HTTPS github.com URL, e.g. https://github.com/owner/repo.";
  }

  if (!input.issue_description.trim()) {
    errors.issue_description = "Describe the issue to investigate.";
  } else if (input.issue_description.trim().length > MAX_ISSUE_DESCRIPTION_LENGTH) {
    errors.issue_description = `Must be at most ${MAX_ISSUE_DESCRIPTION_LENGTH.toLocaleString()} characters.`;
  }

  return errors;
}

export function NewRunForm({
  onSubmit,
  submitting,
  serverFieldErrors,
  onDismissFieldError,
}: NewRunFormProps) {
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [issueDescription, setIssueDescription] = useState("");
  const [maxParallelAttempts, setMaxParallelAttempts] = useState(1);
  const [clientErrors, setClientErrors] = useState<FieldErrors>({});
  const [touched, setTouched] = useState(false);

  // Server messages win: they reflect what was actually rejected.
  const errors: FieldErrors = { ...clientErrors, ...serverFieldErrors };
  const descriptionLength = issueDescription.trim().length;
  const overLimit = descriptionLength > MAX_ISSUE_DESCRIPTION_LENGTH;

  /** Editing a field invalidates both error sources for that field. */
  function handleEdit(field: keyof RunCreateInput) {
    setClientErrors((previous) => {
      if (!(field in previous)) return previous;
      const next = { ...previous };
      delete next[field];
      return next;
    });
    if (serverFieldErrors[field]) onDismissFieldError(field);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    const input: RunCreateInput = {
      repository_url: repositoryUrl.trim(),
      issue_description: issueDescription.trim(),
      max_parallel_attempts: maxParallelAttempts,
    };

    setTouched(true);
    const found = localErrors(input);
    setClientErrors(found);
    if (Object.keys(found).length > 0) return;

    const created = await onSubmit(input);
    if (created) {
      setRepositoryUrl("");
      setIssueDescription("");
      setMaxParallelAttempts(1);
      setClientErrors({});
      setTouched(false);
    }
  }

  const showError = (field: keyof RunCreateInput) =>
    (touched || serverFieldErrors[field]) && errors[field] ? errors[field] : undefined;

  return (
    <form className="card" onSubmit={handleSubmit} noValidate>
      <div className="card__header">
        <h2>New run</h2>
        <p className="card__subtitle">Queue an issue for investigation.</p>
      </div>

      <div className="field">
        <label htmlFor="repository-url">Repository URL</label>
        <input
          id="repository-url"
          type="text"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          placeholder="https://github.com/owner/repo"
          value={repositoryUrl}
          disabled={submitting}
          aria-invalid={showError("repository_url") ? true : undefined}
          aria-describedby={showError("repository_url") ? "repository-url-error" : undefined}
          onChange={(event) => {
            setRepositoryUrl(event.target.value);
            handleEdit("repository_url");
          }}
        />
        {showError("repository_url") ? (
          <p className="field__error" id="repository-url-error">
            {showError("repository_url")}
          </p>
        ) : (
          <p className="field__hint">Public GitHub repository over HTTPS.</p>
        )}
      </div>

      <div className="field">
        <div className="field__labelRow">
          <label htmlFor="issue-description">Issue description</label>
          <span className={`counter${overLimit ? " counter--over" : ""}`}>
            {descriptionLength.toLocaleString()} / {MAX_ISSUE_DESCRIPTION_LENGTH.toLocaleString()}
          </span>
        </div>
        <textarea
          id="issue-description"
          rows={6}
          placeholder="What is going wrong, how to reproduce it, and what the expected behaviour is."
          value={issueDescription}
          disabled={submitting}
          aria-invalid={showError("issue_description") ? true : undefined}
          aria-describedby={showError("issue_description") ? "issue-description-error" : undefined}
          onChange={(event) => {
            setIssueDescription(event.target.value);
            handleEdit("issue_description");
          }}
        />
        {showError("issue_description") ? (
          <p className="field__error" id="issue-description-error">
            {showError("issue_description")}
          </p>
        ) : null}
      </div>

      <div className="field">
        <label id="attempts-label">Max parallel attempts</label>
        <div className="segmented" role="radiogroup" aria-labelledby="attempts-label">
          {ATTEMPT_CHOICES.map((value) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={maxParallelAttempts === value}
              className={`segmented__option${
                maxParallelAttempts === value ? " segmented__option--active" : ""
              }`}
              disabled={submitting}
              onClick={() => {
                setMaxParallelAttempts(value);
                handleEdit("max_parallel_attempts");
              }}
            >
              {value}
            </button>
          ))}
        </div>
        <p className="field__hint">How many competing fix attempts this run may use, 1&ndash;3.</p>
      </div>

      <button type="submit" className="button button--primary" disabled={submitting}>
        {submitting ? "Saving run…" : "Create run"}
      </button>
    </form>
  );
}
