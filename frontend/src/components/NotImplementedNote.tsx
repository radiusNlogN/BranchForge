/**
 * The honest disclaimer. This milestone stores runs and nothing else, so it is
 * stated wherever a run's status is shown rather than buried in a footer.
 */
export function NotImplementedNote({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <p className="note note--compact">
        Saved to the database. Execution is not implemented yet, so this run will stay{" "}
        <code>pending</code>.
      </p>
    );
  }

  return (
    <div className="note">
      <p>
        <strong>Milestone 1: intake and persistence only.</strong> Submitting a run validates it and
        writes it to the database, where it stays in the <code>pending</code> state.
      </p>
      <p>
        Nothing is executed. BranchForge does not yet clone the repository, contact GitHub, call a
        model, or run any fix attempts, and no progress or results are simulated. Repository URLs are
        checked for shape only — that a URL is accepted does not mean the repository exists or is
        public.
      </p>
    </div>
  );
}
