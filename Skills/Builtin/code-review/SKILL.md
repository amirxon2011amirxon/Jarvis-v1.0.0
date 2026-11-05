# Code Review

## Objective

Find the defects and design problems that matter, prove them with evidence, and
make each finding actionable. A good review improves the code without
overwhelming the author. It is not a style preference dump.

## When to use this Skill

Use it when:

- reviewing a diff, pull request, module or architecture proposal;
- auditing security-sensitive or data-handling code;
- investigating a suspected bug, race condition or performance regression;
- assessing whether a change is safe to ship.

Do not use it for a request to implement a feature, unless the user explicitly
asks for review after implementation. Do not turn a simple code explanation into
an unsolicited review.

## Review order

Review in priority order. Stop expanding scope when the important risks are
covered.

1. **Does it do the right thing?** Requirements, edge cases, data semantics.
2. **Can it corrupt or expose data?** Trust boundaries, authorization,
   injection, secrets, unsafe filesystem/process use.
3. **Can it fail badly?** Error paths, partial failure, retries, cancellation,
   cleanup, concurrency.
4. **Will it hold at scale?** Complexity, N+1 I/O, unbounded memory, blocking
   calls, hot loops.
5. **Can it be changed safely?** Coupling, hidden state, naming, tests,
   observability.
6. **Is it consistent with the codebase?** Existing patterns, conventions,
   error model and tooling.

Style-only comments belong last, and only when they affect readability or
consistency. Automated formatters should handle formatting; do not spend review
attention on spaces.

## Correctness

- Compare the implementation with the actual contract, not the author's
  description of it.
- Check boundary values: empty, zero, one, maximum, duplicate, missing and
  malformed input.
- Check temporal behaviour: ordering, repeated calls, concurrent calls,
  cancellation and restart.
- Check ownership: who mutates this state, who reads it, and what happens when
  two callers act at once.
- Check return values that are ignored, errors converted to success, and partial
  writes left behind.
- Verify that tests exercise the changed behaviour and would fail if the bug
  returned.
- Distinguish a real defect from a hypothetical preference. If you cannot
  describe a concrete failure scenario, say it is a suggestion, not a bug.

## Security

- Identify the trust boundary for every input: HTTP body, query string, header,
  file, environment variable, model output, tool result and browser message.
- Check authorization at the operation, not only at the route or UI.
- Look for path traversal, command injection, unsafe deserialization, SSRF,
  template injection and SQL/NoSQL injection.
- Check secret handling: logs, error messages, client bundles, source maps and
  crash reports.
- Check that a declared capability is not treated as a granted permission.
- Check that a local-only interface is still protected against cross-site
  requests, DNS rebinding and malicious pages when relevant.
- Check resource limits: body size, upload size, recursion depth, decompression
  and queue growth.
- For LLM systems, treat model output and retrieved content as untrusted. Verify
  that they cannot override system policy, trigger unapproved tools or leak
  secrets.

## Bugs and failure paths

- Follow the error from origin to user-visible result. Is it preserved, mapped or
  swallowed?
- Check cleanup on every exit path: files, sockets, locks, processes, temporary
  directories, listeners and GPU resources.
- Check retries for idempotency and bounded attempts.
- Check cancellation: does it stop work, release resources and avoid logging a
  spurious failure?
- Check timeouts on external calls. A missing timeout is a finding when the
  dependency can hang.
- Check that "not found" is not silently treated as success.
- Check that a failure in one item cannot take down an unrelated batch or
  process.

## Performance

- Look for the dominant cost first: an extra network round trip usually matters
  more than a micro-optimization in a formatter.
- Check algorithmic complexity and data structure choice.
- Check repeated I/O inside loops, missing pagination, unbounded caches and
  accidental quadratic behaviour.
- Check that an optimization is measured. A change that adds complexity without
  a benchmark is not automatically an improvement.
- Check that memoization has a correct invalidation policy.
- Check that frontend work avoids unnecessary re-renders only where it is
  actually a bottleneck; do not recommend memoization everywhere.

## Architecture and maintainability

- Does the change put logic at the right boundary? A UI component should not own
  database semantics; a transport adapter should not own product rules.
- Does it create a second source of truth?
- Does it duplicate an existing mechanism that should be reused?
- Does it introduce a cycle, a hidden global, or an implicit startup dependency?
- Is the new abstraction justified by more than one caller or a clear domain
  concept?
- Can the behaviour be observed and tested? Missing logging or missing seams can
  be a legitimate finding.
- Is the change reversible? Destructive migrations, data deletion and schema
  changes need explicit consideration.

## Testing review

- Do the tests describe user-visible or contract behaviour?
- Are important failure paths covered, not only the happy path?
- Are tests deterministic and isolated from real network, clock and user data?
- Does a test assert the wrong thing (for example, that a request was sent, not
  that the operation completed)?
- Are mocks at the boundary, or do they replace the logic under test?
- Is there a regression test for the specific bug being fixed?

## Reporting findings

Use a consistent severity:

- **Critical:** data loss, security breach, crash of the whole application,
  silent corruption, or a broken core workflow with no workaround.
- **High:** a real bug affecting many users, a serious permission/security gap,
  or a failure that requires manual recovery.
- **Medium:** incorrect behaviour in a plausible edge case, a meaningful
  performance or reliability risk, or a design issue that will cause repeated
  defects.
- **Low:** maintainability, local robustness or clarity issue with limited
  impact.
- **Nit:** optional polish. Keep these few and clearly optional.

Each finding should include:

1. **Location** — file and line or symbol.
2. **Severity** — one of the above.
3. **What happens** — the concrete failure or cost.
4. **Why it happens** — the causal mechanism, not just the symptom.
5. **Evidence** — code path, test result, reproduction or precise reasoning.
6. **Fix** — the smallest correct change, or a direction when the design needs
   discussion.

If a finding is speculative, label it as such and explain what would confirm it.
Do not invent a line number or claim a test failed unless you ran it.

## What not to do

- Do not list every function that could theoretically be smaller.
- Do not demand a rewrite when a focused fix solves the problem.
- Do not confuse personal style with correctness.
- Do not bury critical findings under a long list of nits.
- Do not claim the change is safe because tests pass if the tests do not cover
  the risky path.
- Do not repeat the same finding for every call site; group it and list the
  affected locations.
- Do not review generated or vendored code unless it is part of the change's
  risk surface.

## Review summary

End with a short verdict:

- **Block:** critical/high issues must be fixed.
- **Approve with changes:** medium issues should be addressed, or explicitly
  accepted with a follow-up.
- **Approve:** no material issues found; list remaining low-risk observations.

State what you actually verified (tests run, paths inspected, assumptions). A
review that cannot say what was checked is not a review.
