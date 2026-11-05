# UI/UX Design

## Objective

Design how a product works before deciding how it looks. The goal is a coherent
task flow with clear information architecture, predictable interaction and
feedback that tells the user what happened. Use `frontend-design` for visual
craft; this Skill owns the structure and behaviour underneath it.

## When to use this Skill

Use it when:

- designing a new product, feature, dashboard or multi-step flow;
- the interface has working screens but users cannot find, understand or
  complete a task;
- defining navigation, information architecture, empty states or error recovery;
- simplifying an overgrown interface or reducing cognitive load;
- reviewing usability, accessibility and interaction consistency.

Do not use it for pure visual styling, a backend API design with no user surface,
or a task where the user has specified the complete interaction and only wants
implementation.

## Start with the user's job

Write a short UX brief:

1. **Primary user and context.** Who is doing this, on what device, under what
   time pressure or distraction?
2. **Job to be done.** One sentence describing the outcome, not the feature.
3. **Trigger and entry point.** How does the user arrive here?
4. **Success.** What must be true for the task to be complete?
5. **Failure and recovery.** What can go wrong, and how does the user get back
   on track?
6. **Constraints.** Existing navigation, permissions, data availability, brand,
   accessibility requirements.

A feature is not a user goal. "Export" is a feature; "get the report to my
accountant by Friday" is a job.

## Information architecture

- Group by the user's mental model, not by the engineering database schema.
- Keep the primary navigation small enough to scan. Secondary options belong
  inside the relevant area, not in the top bar by default.
- Use labels users already understand. Internal acronyms need an explanation or
  a different label.
- Make location visible: where am I, how did I get here, and how do I go back?
- Prefer progressive disclosure for advanced options. Do not hide the primary
  action behind a "more" menu.
- Give every screen one clear primary purpose. If two purposes compete, split the
  screen or establish an explicit mode.
- Search and navigation should not duplicate each other's jobs. Search is for
  finding a known thing; navigation is for understanding the space.

## Task flows

- Map the happy path in numbered steps, then map the branches: empty, loading,
  error, permission denied, expired session, partial success, cancellation.
- Remove unnecessary decisions. Every field, modal and confirmation adds cost.
- Defaults should be safe and useful. The user should not have to configure
  everything before seeing value.
- Do not force account creation, onboarding or a tour before the core value is
  visible, unless the domain genuinely requires it.
- For long flows, show progress and preserve entered data on back/refresh where
  possible.
- Destructive actions need a clear consequence statement and a recovery path.
  Confirmation alone is not a substitute for undo when undo is feasible.
- Allow the user to stop, pause or cancel long-running work. "Cancel" must
  actually cancel, not merely close a dialog.

## Screen structure

- Lead with the user's current context and the next action.
- Put the primary action in a stable, predictable place.
- Separate input from output: do not interleave controls with results in a way
  that makes it unclear what changed.
- Use progressive disclosure for detail: summary first, then inspect.
- Keep related actions together and unrelated actions apart.
- Avoid modes that look identical but behave differently. If a mode is
  necessary, make it unmistakable and easy to exit.
- On mobile, preserve the primary task. Do not simply hide half the desktop
  interface.

## Interaction and feedback

- Every action should produce visible feedback within the interface's latency
  budget. Immediate press feedback, then progress, then result.
- Use the right feedback for the duration:
  - under ~100ms: no spinner needed;
  - 100ms–1s: subtle busy state;
  - over 1s: progress or a clear working message;
  - long tasks: progress, cancellation and a way to continue elsewhere.
- Report what happened, not just "Done". "Saved 3 changes", "Opened in Brave",
  "Could not reach the server — retrying".
- Never claim success before the operation is confirmed.
- Disable or guard repeated submission, but explain why an action is unavailable.
- Preserve user input on failure. Losing a form is a usability defect.
- Make empty states useful: explain what will appear and offer the next action.
- Error messages should say what happened, what the user can do, and whether data
  was affected.

## Forms and input

- Ask only for what is needed now.
- Use the correct input type, autocomplete and keyboard hints.
- Put labels above inputs, not only placeholders.
- Validate on blur or submission, not on every keystroke unless the feedback is
  genuinely helpful.
- Show errors next to the field and summarise them when several exist.
- Do not clear a field because validation failed.
- Default to preserving the user's work. Confirm before discarding unsaved
  changes.
- For destructive or irreversible actions, require an explicit choice; do not
  preselect the dangerous option.

## Accessibility and inclusion

- Design keyboard-first flows for anything a keyboard user must complete.
- Ensure focus order matches visual order and focus is never lost after a modal
  closes or content updates.
- Use semantic controls; a clickable `div` is not a button.
- Provide names for icon-only controls.
- Keep touch targets comfortable and avoid gestures with no alternative.
- Do not rely on colour alone for status.
- Support zoom, long text, translations and reduced motion.
- Write in plain language. Explain jargon where the audience may not know it.
- Consider users on slow connections, older devices and small screens.

## Consistency

- Reuse interaction patterns for the same job. A save action should behave like
  other save actions.
- Keep terminology stable. Do not call the same object a "project", "workspace"
  and "board" in different places.
- Align empty, loading, error and success states across features.
- Follow platform conventions unless there is a strong reason not to; novelty
  adds learning cost.
- Define a small set of layout and feedback patterns instead of solving each
  screen independently.

## Usability evaluation

You do not always need a formal study. Use these lightweight methods:

- **Task walkthrough:** can a new user complete the primary job without help?
- **Five-second test:** what does the user think this screen is for?
- **First-click test:** where would they click to start the main task?
- **Error injection:** force every failure path and inspect the recovery.
- **Keyboard pass:** complete the flow without a pointer.
- **Content stress test:** longest label, longest value, empty value, translated
  string.
- **Cognitive load check:** count decisions, fields and competing actions on the
  screen.

A usability problem is only solved when observed behaviour improves, not when
the mockup looks cleaner.

## Handoff to visual design

Provide the visual designer or implementer with:

- the task flow and screen states;
- hierarchy: what is primary, secondary and tertiary;
- required content and realistic examples;
- interaction rules and state transitions;
- accessibility requirements;
- responsive behaviour;
- error and empty-state copy.

Do not hand off a single happy-path mockup and leave failure behaviour implicit.

## Quality bar

- A new user can identify the primary task and start it without instruction.
- Every state has a designed next step: loading, empty, error, success,
  permission denied.
- The user never loses work because of a recoverable failure.
- Navigation communicates location and history.
- Keyboard and screen-reader users can complete the core flow.
- Feedback is honest, specific and appears when the user needs it.
- The interface uses one vocabulary and one pattern for each repeated job.
