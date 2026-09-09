/**
 * The honest scope statement.
 *
 * Milestone 3 added real model calls and patch proposals, so the milestone-2
 * claim that BranchForge "does not call any AI model" is no longer true. Keep
 * this text matching what the code actually does — stale honesty text is worse
 * than none.
 */
export function NotImplementedNote({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <p className="note note--compact">
        Inspection is read-only. Any proposed patch is unverified — nothing is applied and no tests
        are run.
      </p>
    );
  }

  return (
    <div className="note">
      <p>
        <strong>Milestone 3: inspect a repository, then propose a patch.</strong> Creating a run
        stores it as <code>pending</code>. One worker command inspects the repository through the
        GitHub API; a second runs a single bounded agent that reads the issue and the inspection and
        proposes a patch.
      </p>
      <p>
        <strong>Any patch shown here is unverified.</strong> It is checked only for valid
        unified-diff syntax. BranchForge does not apply it, compile it, or run any tests, and it
        never clones the repository or executes repository code. Competing parallel attempts,
        sandboxed verification, and applying patches are not implemented.
      </p>
      <p>
        Nothing is scheduled — you run each worker command yourself, then refresh.
      </p>
    </div>
  );
}
