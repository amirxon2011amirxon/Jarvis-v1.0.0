# Web Development

## Objective

Build web applications whose structure makes the next change easy, whose types
tell the truth at every boundary, and whose production behaviour is verified
rather than assumed. This Skill covers application engineering. Use
`frontend-design` for visual direction and `threejs-development` for real-time
3D work.

## When to use this Skill

Use it when:

- creating or extending a React/TypeScript application;
- connecting a frontend to an HTTP, WebSocket or server-rendered backend;
- defining project structure, shared types, API clients or data-fetching
  patterns;
- investigating performance, bundling, hydration or production build issues;
- preparing an application for testing, deployment or maintenance.

Do not use it for a visual-only polish pass, a pure research task or a small
Python change that has no web component.

## Start from the system, not the page

Before implementation, establish:

1. **Runtime shape.** Static site, SPA, server-rendered, hybrid, local desktop
   web UI or embedded surface.
2. **Truth boundaries.** Where does authoritative state live: browser, server,
   database, filesystem, external API?
3. **Data flow.** Which operations are reads, writes, subscriptions or
   commands? Which need optimistic UI, retries or cancellation?
4. **Failure modes.** Offline, slow network, partial response, auth expiry,
   malformed data, duplicate submission and server restart.
5. **Constraints.** Browser support, bundle budget, latency budget, hosting and
   security boundary.

Write these down before adding abstractions. Most over-engineering comes from
solving a problem the system does not actually have.

## Project architecture

- Prefer feature-oriented folders once the app has more than a few screens:
  `features/<name>/{components,hooks,api,types,__tests__}`.
- Keep shared primitives genuinely shared. A component used once belongs near
  its feature, not in a global `components/` folder.
- Separate **transport**, **domain** and **presentation**:
  - transport maps raw payloads and status codes;
  - domain maps validated data into product concepts;
  - presentation renders and handles interaction.
- A component should not know the wire format of an API response.
- A server endpoint should not import browser UI code.
- Keep configuration in one place. Do not read environment variables from
  arbitrary modules.
- Avoid barrel files that create import cycles or make tree-shaking worse. Use
  them only where they simplify a real public boundary.

## TypeScript

- Enable `strict`, `noUncheckedIndexedAccess` where practical, and
  `exactOptionalPropertyTypes` if the codebase can support it.
- Model discriminated unions for state that has mutually exclusive shapes:

```ts
type LoadState<T> =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; error: string };
```

- Parse untrusted data at the boundary. Type assertions are not validation.
  Use a runtime schema when the response is external, persisted or versioned.
- Avoid `any`. Use `unknown` and narrow it, or define the real type.
- Do not duplicate API types by hand across client and server. Generate them or
  keep one shared source when the stack allows it.
- Keep function signatures honest: no optional parameter that is always passed,
  no boolean flag that changes the entire behaviour of a component.
- Use `satisfies` for configuration objects where it preserves literal types.

## React structure

- Components should have one reason to change. Split when a component mixes
  data orchestration, complex derived state and multiple rendering modes.
- Keep derived values out of state. Compute them during render, or memoise only
  when profiling shows a real cost.
- Reducers are appropriate for multi-field state transitions; `useState` is
  appropriate for one independent value. Do not put a whole application into a
  reducer by default.
- Effects are for synchronising with external systems, not for deriving data
  from props. If an effect only sets state from props, it is probably a render
  calculation.
- Every effect that subscribes or schedules work must clean up.
- Keep context narrow. A single application context that changes on every
  metrics tick re-renders everything below it.
- Prefer composition over boolean prop proliferation. A component with five
  layout flags is usually three components.
- Keep accessibility in the component contract: labels, roles, keyboard
  behaviour and focus management are not optional decoration.

See `references/react-architecture.md` for a more detailed component and state
playbook.

## Frontend/backend integration

- Define one transport module per backend surface. Components call typed
  functions, not `fetch` ad hoc.
- Centralise base URL, headers, credentials, timeout, error mapping and abort
  handling.
- Distinguish network failure, HTTP failure and domain failure. A generic
  "Something went wrong" hides actionable states.
- Validate every response before it reaches UI state.
- For mutations, decide explicitly:
  - wait for server confirmation; or
  - optimistic update with rollback;
  - and prevent duplicate submission while in flight.
- Treat server events as authoritative only when the protocol says so. A
  websocket reconnect should resynchronise from a snapshot, not assume it missed
  nothing.
- When using WebSockets, correlate request/response messages with ids and apply
  bounded timeouts. A sent message is not a completed operation.
- Never ship secrets to the browser. A local-only app still has a trust boundary.

## Performance

- Measure before optimizing. Record the user-visible metric that matters:
  first contentful paint, interaction latency, long-task duration, memory or
  network payload.
- Keep the initial bundle small. Lazy-load heavy, below-the-fold or optional
  surfaces.
- Audit the bundle for duplicate dependencies, moment-sized date libraries and
  full icon packs imported into the main chunk.
- Avoid rendering huge lists without windowing or pagination.
- Keep server state and client state distinct. Refetching everything on every
  interaction is not a data strategy.
- Use images at the rendered size, modern formats and explicit dimensions.
- Profile React renders before adding memoisation. Then memoise the expensive
  boundary, not every component.
- Beware of layout shift from fonts, images, late widgets and dynamic banners.

## Testing

- Test behaviour at the boundary the user experiences, not private
  implementation details.
- Have at least one happy path and one failure path for each API operation.
- Test the contract: request shape, response parsing, error mapping and state
  transition.
- For components, test keyboard flow and accessible names, not just snapshots.
- Use a real browser for flows that depend on layout, focus, media, canvas or
  WebSocket timing.
- Keep tests deterministic. Mock the network, clock and randomness at their
  boundary, not deep inside domain logic.
- A flaky test is a defect in the test or the system. Do not normalise retries
  as a fix.

See `references/performance-and-testing.md` for measurement and test strategy.

## Production readiness

Before declaring a web application finished:

- production build succeeds with warnings treated deliberately;
- no secret is present in client assets or source maps that will be served;
- all external requests have timeouts and user-visible failure states;
- cache headers and asset hashing are correct;
- error reporting exists for the server and the browser where appropriate;
- accessibility pass completed with keyboard and screen reader;
- responsive layouts verified at real breakpoints;
- empty, loading, error, offline and permission-denied states exist;
- logs do not contain credentials or private user content;
- the application starts from a clean install using documented steps.

## Tool guidance

- Use `file_control` to inspect the existing structure before adding folders or
  dependencies.
- Use `web_fetch` for current library documentation when an API detail matters;
  do not guess a version-specific behaviour.
- Use the terminal only through the host's approved tooling. This Skill does not
  grant shell or deployment permissions.

## Quality bar

A good web implementation is unsurprising to maintain: types match reality,
state has one owner, failures are visible, tests describe user outcomes, and a
new engineer can find the code responsible for a behaviour without searching
the entire repository.
