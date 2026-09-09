/**
 * The honest scope statement.
 *
 * Milestone 2 added real GitHub inspection, so the earlier claim that BranchForge
 * "does not contact GitHub" is no longer true. Keep this text matching what the
 * code actually does — stale honesty text is worse than none.
 */
export function NotImplementedNote({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <p className="note note--compact">
        Inspection is read-only. No fix has been attempted — AI fixes and test execution are not
        implemented yet.
      </p>
    );
  }

  return (
    <div className="note">
      <p>
        <strong>Milestone 2: repository inspection.</strong> Creating a run stores it as{" "}
        <code>pending</code>. A separate worker command then reads the repository through the GitHub
        API and saves an inspection report, moving the run to <code>ready</code> or{" "}
        <code>failed</code>.
      </p>
      <p>
        <strong>&ldquo;Ready&rdquo; means the inspection finished, not that a fix exists.</strong>{" "}
        BranchForge does not yet call any AI model, generate patches, or run tests, and nothing is
        simulated. Inspection is strictly read-only: the repository is never cloned, its
        dependencies are never installed, and its code is never executed.
      </p>
      <p>
        Runs are not scheduled automatically — you run the worker yourself, then refresh.
      </p>
    </div>
  );
}
