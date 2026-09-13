/**
 * The honest scope statement.
 *
 * Milestone 6 starts the workflow from this page and polls while a job runs, so
 * the milestone-5 claims that work cannot be launched here, that nothing is
 * scheduled, and that nothing polls are all no longer true. Keep this text
 * matching what the code actually does — stale honesty text is worse than none.
 *
 * Three distinctions this text has to carry: tests really do run, but passing
 * tests are not a correctness proof; a recommendation is a conservative
 * preference among demonstrated fixes, not a judgement that one patch is right;
 * and "queued" means a separate dispatcher process still has to pick the job up,
 * not that anything is happening yet. Do not compress any of them into
 * "verified", "best", or a progress bar.
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
        <strong>Milestone 6: start the whole workflow from this page.</strong> Creating a run stores
        it as <code>pending</code>. <strong>Start</strong> adds it to a queue and returns
        immediately — a separate <code>dispatch</code> process does the work, so a queued job sits
        still until one is running. That process inspects the repository through the GitHub API,
        then runs the requested number of attempts (1-3) as separate processes, a bounded number at
        a time: each proposes a patch with its own agent and applies it to a throwaway copy of the
        exact inspected commit, running the repository&apos;s own tests before and after in a
        container with no network. The single-attempt <code>propose</code>/<code>verify</code>{" "}
        commands still work.
      </p>
      <p>
        <strong>A verification result is not a correctness proof.</strong> It reports how the tests
        that actually ran behaved, on one commit, in one fixed runner profile. Repository
        dependencies are never installed and no setup script is ever run. An attempt is recommended
        only when its verification demonstrated originally failing tests now passing with nothing
        regressed or missing; otherwise the result is &ldquo;No demonstrated fix&rdquo;.
      </p>
      <p>
        Not implemented: retries, automatic recovery if a dispatcher or orchestrator process is
        killed outright, and a global capacity limit (one dispatcher runs one job at a time on one
        machine, and manual worker commands still bypass the queue entirely). A run can be started
        once; cancelling it keeps whatever was already produced, but it cannot be resumed or
        restarted. Nothing is scheduled for you — a dispatcher must be running for a queued job to
        move.
      </p>
    </div>
  );
}
