/**
 * The honest scope statement.
 *
 * Milestone 4 applies patches and runs tests in containers, so the milestone-3
 * claim that BranchForge "does not apply it, compile it, or run any tests" is no
 * longer true. Keep this text matching what the code actually does — stale
 * honesty text is worse than none.
 *
 * The distinction this text has to carry now is subtler than before: tests really
 * do run, but passing tests are not a correctness proof. Do not compress that
 * into "verified".
 */
export function NotImplementedNote({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <p className="note note--compact">
        Inspection is read-only. A proposed patch can be applied and tested in a container — that
        shows how the existing tests behave, not that the patch is correct.
      </p>
    );
  }

  return (
    <div className="note">
      <p>
        <strong>Milestone 4: inspect, propose, then verify.</strong> Creating a run stores it as{" "}
        <code>pending</code>. One worker command inspects the repository through the GitHub API; a
        second runs a single bounded agent that proposes a patch; a third applies that patch to a
        throwaway copy of the exact inspected commit and runs the repository&apos;s own tests before
        and after, inside a container with no network access.
      </p>
      <p>
        <strong>A verification result is not a correctness proof.</strong> It reports how the tests
        that actually ran behaved, on one commit, in one fixed runner profile. Repository
        dependencies are never installed and no repository setup script is ever run, so a project
        needing more than the profile provides is reported as an environment limitation rather than
        as a failing patch. Tests the patch adds are reported separately from the original suite.
      </p>
      <p>
        Not implemented: competing parallel attempts, retries, patch application to your own
        checkout, and scheduling. Nothing is scheduled — you run each worker command yourself, then
        refresh.
      </p>
    </div>
  );
}
