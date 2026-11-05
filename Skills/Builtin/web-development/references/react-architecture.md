# React Architecture Playbook

## Component boundaries

Use these questions to decide whether a component should be split:

- Does it have more than one reason to change?
- Does it mix data acquisition with a complex visual region?
- Is a prop only used to select one of several unrelated layouts?
- Would a test need to mock an unrelated subsystem to exercise it?
- Is the same state transition duplicated in several siblings?

Split along behaviour, not line count. A 200-line presentational component with
one clear job is often healthier than five 30-line components passing twelve
props each.

## State ownership

Classify state before choosing a tool:

| Kind | Owner | Tool |
| --- | --- | --- |
| Server data | Query/cache layer or feature hook | `fetch` + cache, or query library |
| URL state | Router | search params / route segments |
| Form draft | Form component or form library | local state, reducer |
| Cross-tree UI state | Nearest common provider | narrow context |
| Persistent local preference | Storage boundary | typed adapter |
| Ephemeral animation | Component | refs / local state |

Do not copy server data into a global store by default. It creates two sources
of truth and an invalidation problem.

## Data hooks

A feature data hook should expose a complete state machine, not a pile of
booleans:

```ts
function useProjects() {
  const [state, setState] = useState<LoadState<Project[]>>({ status: "idle" });
  // load, retry, cancel, refresh
  return { state, refresh, cancel };
}
```

Keep transport concerns inside the hook or API module. Components should not
build URLs or inspect HTTP status codes.

## Context discipline

- One provider per concern: theme, auth, widget runtime, feature flags.
- Split state and actions if consumers need only one of them.
- Keep the context value referentially stable unless a real dependency changes.
- Avoid a provider that updates on high-frequency telemetry unless subscribers
  are isolated with an external store.
- If a context value is an object rebuilt every render, every consumer
  re-renders. Memoise it or redesign the boundary.

## Effects

An effect is correct when it synchronises with something outside React:

- subscribing to events;
- starting/stopping a timer;
- controlling a media element or canvas;
- writing to storage or the document title;
- imperative browser APIs.

An effect is suspicious when it:

- copies props into state;
- computes derived values;
- triggers a parent callback on mount;
- fetches data without cancellation or a query layer;
- exists only to work around a render-order problem.

## Composition patterns

- Use children and slots before adding layout boolean flags.
- Use compound components when the parts share implicit state and the consumer
  needs a readable structure (for example `Tabs`, `Tabs.List`, `Tabs.Panel`).
- Prefer explicit render props or hooks over cloning elements.
- Keep controlled and uncontrolled behaviour explicit. Do not mix both modes
  ambiguously.
- If a component needs a ref to its own DOM node, expose it intentionally; do not
  make every consumer pass refs through.

## Error boundaries

- Place boundaries around independent surfaces, not only at the application
  root.
- A widget failure should not blank the whole application.
- Error boundaries do not catch event handler, async or server errors. Handle
  those at the operation boundary.
- Provide a recovery action where one is possible: retry, reset, close or
  reload the failed surface.

## Testing components

Prefer tests that resemble use:

```ts
render(<SkillList skills={skills} onSelect={onSelect} />);
await user.click(screen.getByRole("button", { name: /code review/i }));
expect(onSelect).toHaveBeenCalledWith("code-review");
```

Query by role and accessible name. Avoid asserting class names or internal
state. Add a test for keyboard interaction when the component implements custom
navigation. Test loading and error states, not only the happy path.
