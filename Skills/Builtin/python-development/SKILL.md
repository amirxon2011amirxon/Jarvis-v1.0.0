# Python Development

## Objective

Write Python that is easy to operate, test and change. Prefer explicit data,
explicit failure and small boundaries over clever abstractions. Match the
existing project's conventions before introducing a new pattern.

## When to use this Skill

Use it when:

- building or refactoring a Python module, service, CLI or library;
- adding async I/O, concurrency or background work;
- designing error handling, retries, cancellation or resource lifetimes;
- improving type safety, testability or maintainability;
- diagnosing a subtle bug in state, concurrency or data flow.

Do not use it for a one-line configuration change or a task whose real work is
in another language or the browser.

## Architecture

- Group code by responsibility, not by technical noun alone. A feature package
  with its own models, service and tests is often clearer than three global
  `utils` modules.
- Keep a small public surface. Prefix internals with `_`; export only what a
  caller should depend on.
- Separate:
  - **domain logic** (pure decisions and transformations);
  - **I/O adapters** (filesystem, HTTP, database, process);
  - **orchestration** (the sequence that coordinates both).
- Pure functions should stay pure. Do not hide a network call in a helper named
  `normalise`.
- Avoid circular imports by moving shared data structures to a neutral module,
  not by adding import-time workarounds.
- Configuration belongs at the process boundary. Read environment variables
  once, validate them, and pass typed settings inward.
- Use dependency injection when it improves testability or enables multiple
  implementations. Do not wrap every function in an interface for its own sake.

## Data and typing

- Add type hints to public functions and to non-obvious locals. Use `from
  __future__ import annotations` in modules with forward references.
- Prefer concrete types (`list[str]`, `dict[str, int]`) over `Any`.
- Model states with dataclasses, enums or `Literal` unions when they are finite.
- Use `TypedDict` for external JSON shapes when a full model is unnecessary.
- Validate untrusted input at the boundary. A type hint is not a runtime check.
- Keep optional values genuinely optional. Do not return `None` for "error",
  `0` for "unknown" and `""` for "not found" in the same API.
- Avoid mutable default arguments. Use `None` and construct inside the function.
- Do not use `assert` for user input validation; asserts disappear under `-O`.

## Error handling

- Raise exceptions for exceptional conditions; return values for expected
  outcomes such as "not found" when that is part of the contract.
- Define a small exception hierarchy at the module boundary when callers need to
  distinguish failure classes. Do not create one exception class per line.
- Include enough context to act: what was attempted, which resource, and the
  underlying cause.
- Preserve the cause with `raise ... from exc`.
- Catch the narrowest exception that you can actually handle. A blanket
  `except Exception` belongs at a process/worker boundary where it is logged and
  converted into a safe response.
- Never swallow an exception silently. Log with context or re-raise.
- Clean up resources with `with`/`async with`. Avoid manual `close()` in a
  `finally` unless no context manager exists.
- Make retries explicit: bounded attempts, backoff, jitter and a clear
  non-retryable set. Never retry a mutating operation without idempotency.

## Async programming

- Async is for I/O concurrency, not for making CPU work faster. Use a process or
  thread boundary for blocking CPU-heavy work.
- Never call blocking I/O directly inside an async function. Use the appropriate
  async library or `asyncio.to_thread` for a genuinely blocking call.
- Create tasks with a clear owner. Keep a reference to long-lived tasks and
  cancel them on shutdown.
- Use `asyncio.TaskGroup` or `gather` according to failure semantics. Know
  whether one failure should cancel siblings.
- Use timeouts on every external await. An unbounded wait is a hang waiting to
  happen.
- Protect shared mutable state with a lock when multiple tasks can mutate it.
  Prefer message passing or a single owner where possible.
- Do not mix event loops. If code may run in a thread, use
  `asyncio.run_coroutine_threadsafe` to hand work back to the owning loop.
- Cancellation is normal. Clean up in `finally`, re-raise `CancelledError`, and
  do not convert cancellation into a generic failure.

## Concurrency and threads

- Define ownership: which thread owns a resource and which threads may touch it.
- Use a lock around a coherent state transition, not around every field access.
- Avoid holding locks across external calls; it turns a slow dependency into a
  global stall.
- Use queues for handoff between producers and consumers; bound them when a slow
  consumer could grow memory without limit.
- Make shutdown explicit: set a stop event, wake waiters, join with a timeout,
  and report threads that failed to stop.

## Structure and readability

- Name things for what they mean in the domain, not for their type.
- Keep functions focused on one level of abstraction. A function that validates,
  parses, writes and logs is doing too much.
- Prefer early returns over deep nesting.
- Comments should explain *why* a non-obvious choice exists, not restate the
  code. Keep them accurate when behaviour changes.
- Avoid global mutable state. If a module must own a singleton, provide a
  reset/injection point for tests.
- Do not build a framework for three call sites. Extract after the pattern is
  proven.
- Keep imports at module scope unless deferred import is required to break a
  real cycle or avoid an optional dependency.

## Testing

- Test behaviour through the public boundary. Avoid asserting private helpers
  unless they carry complex logic worth isolating.
- Use `unittest` or `pytest` consistent with the project. Do not mix frameworks
  in one suite.
- Isolate external systems: temporary directories, fake clocks, mocked HTTP
  clients and in-memory databases.
- Cover success, expected failure and edge cases: empty input, duplicate calls,
  timeouts, cancellation and partial failure.
- Make tests deterministic. No real sleeps for coordination; use events or fake
  time.
- A regression test should fail for the bug before the fix and pass after it.
- Test resource cleanup where lifetime bugs are plausible (files, threads,
  subprocesses, database connections).

## Performance and operations

- Measure before optimizing. Use `time.perf_counter`, profiling or a benchmark,
  not intuition.
- Optimize algorithmic complexity before micro-optimizing Python syntax.
- Avoid repeated I/O inside loops; batch or cache with an explicit invalidation
  policy.
- Bound memory: cap caches, queues and result sets.
- Log at boundaries with structured context. Never log secrets, tokens or full
  private payloads.
- Make startup failures loud and actionable. A service that silently starts in a
  degraded state is harder to operate than one that refuses to start.

## Tool guidance

- Use `file_control` to inspect existing modules and conventions before editing.
- Run the relevant test suite after changes, and report the actual result.
- Use the host's approved terminal tooling for tests, linters and type checkers.
  This Skill does not grant execution permissions.

## Quality bar

- A reader can tell where data comes from and who owns it.
- Every external call has a timeout and a failure path.
- Async code cannot block the loop or leak tasks.
- Errors carry context and preserve their cause.
- Tests cover the behaviour that matters and run without network access.
- The change fits the project's existing patterns instead of introducing a
  parallel architecture.
