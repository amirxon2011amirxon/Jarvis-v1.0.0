# Performance and Testing Playbook

## Measure first

Pick the metric that reflects the user complaint:

- **Initial load:** LCP, total transferred bytes, JavaScript parse/execute time.
- **Interaction:** INP, long tasks, event-handler duration.
- **Rendering:** React commit time, number of rendered components, layout
  duration.
- **Data:** time to first byte, request waterfall, payload size, cache hit rate.
- **Memory:** heap growth across repeated flows, retained DOM nodes, listener
  counts.

Record a baseline before changing code. An optimization without a measurement is
a guess.

## Bundle hygiene

- Inspect the production build, not the development server.
- Look for duplicated packages at different versions.
- Lazy-load large optional surfaces and route-level screens.
- Import icons individually; avoid a single icon barrel that pulls in hundreds
  of components.
- Replace heavyweight utilities with small focused functions when the bundle
  cost is real.
- Check source maps and environment substitution for secrets.
- Set an explicit budget and fail CI when it is exceeded if the project supports
  it.

## Rendering performance

- Find the actual slow component with the profiler before memoising.
- Stabilise object and array identities passed through context or expensive
  children.
- Split high-frequency subscriptions from the main application tree.
- Virtualise lists only when the item count and row cost justify it; virtualised
  content adds accessibility and measurement complexity.
- Avoid measuring layout inside render. Batch DOM reads and writes.
- For canvas or WebGL, keep the render loop outside React's render cycle and
  clean up listeners, observers and GPU resources.

## Network performance

- Cancel obsolete requests when inputs change.
- Debounce search only when it reduces real work; do not debounce a button
  press.
- Cache immutable assets with hashed filenames.
- Do not cache API responses without an invalidation strategy.
- Prefer one well-shaped endpoint over a waterfall of dependent calls.
- Stream or paginate large result sets.
- Use compression and modern image formats where the host supports them.

## Test pyramid for web apps

1. **Unit tests** for pure domain logic, parsers, reducers and formatting.
2. **Contract tests** for API clients: successful response, HTTP error, network
   error, malformed payload, cancellation.
3. **Component tests** for user-visible behaviour, keyboard access and state
   transitions.
4. **Integration tests** for the real boundary: browser + server + storage, with
   deterministic fixtures.
5. **A small number of end-to-end smoke tests** for critical journeys.

Do not mock the thing under test. Mock the network, clock, random source or
operating-system boundary.

## Failure-path tests worth writing

- request times out;
- response arrives after the component unmounts;
- websocket disconnects mid-command;
- duplicate submit is prevented;
- malformed payload is rejected without crashing the tree;
- expired credentials produce a recoverable state;
- empty result set renders an intentional empty state;
- server restart invalidates cached session state.

## Production verification

Run the built application, not only the dev server. Verify:

- deep links resolve;
- refresh preserves or intentionally resets state;
- assets load from the configured base path;
- Content-Security-Policy does not block required features;
- WebSocket reconnection resynchronises correctly;
- errors are logged without leaking credentials;
- the documented clean-install steps actually work.
