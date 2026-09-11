/**
 * The honest scope statement.
 *
 * Milestone 5 runs several competing attempts, so the milestone-4 claim that
 * "competing parallel attempts" are not implemented is no longer true. Keep this
 * text matching what the code actually does — stale honesty text is worse than
 * none.
 *
 * Two distinctions this text has to carry: tests really do run, but passing
 * tests are not a correctness proof; and a recommendation is a conservative
 * preference among demonstrated fixes, not a judgement that one patch is right.
 * Do not compress either into "verified" or "best".
 */
export function NotImplementedNote({ compact = false }: { compact?: boolean }) {
  if (compact) {
    return (
      <p className="note note--compact">
        Inspection is read-only. Proposed patches can be applied and tested in containers — that
        shows how the existing tests behave, not that a patch is correct.
      </p>
    );
  }

  return (
    <div className="note">
      <p>
        <strong>Milestone 5: inspect, then run competing attempts.</strong> Creating a run stores it
        as <code>pending</code>. One worker command inspects the repository through the GitHub API.
        Then <code>orchestrate</code> runs the requested number of attempts (1-3) as separate
        processes, a bounded number at a time: each proposes a patch with its own agent and applies
        it to a throwaway copy of the exact inspected commit, running the repository&apos;s own tests
        before and after in a container with no network. The single-attempt{" "}
        <code>propose</code>/<code>verify</code> commands still work.
      </p>
      <p>
        <strong>A verification result is not a correctness proof.</strong> It reports how the tests
        that actually ran behaved, on one commit, in one fixed runner profile. Repository
        dependencies are never installed and no setup script is ever run. An attempt is recommended
        only when its verification demonstrated originally failing tests now passing with nothing
        regressed or missing; otherwise the result is &ldquo;No demonstrated fix&rdquo;. Ties are
        broken by a stated preference for smaller diffs, which is not evidence of better code.
      </p>
      <p>
        Not implemented: retries, recovery if an orchestrator process is killed outright, a global
        capacity limit across orchestrators (the concurrency limit is per process), launching work
        from this page, and scheduling. Nothing is scheduled — you run each worker command yourself,
        then refresh.
      </p>
    </div>
  );
}
