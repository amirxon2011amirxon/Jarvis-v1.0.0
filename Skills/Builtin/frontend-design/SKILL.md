# Frontend Design

## Objective

Produce interfaces that look intentionally designed for their subject, not
assembled from a generic component template. The result should have a clear
visual idea, a readable hierarchy, confident typography, deliberate spacing and
a level of polish that survives real content, narrow screens and keyboard use.

This Skill is about visual and interaction quality. Use `web-development` for
application architecture, data flow, build tooling and production engineering;
use `threejs-development` when the visual centrepiece is a real-time 3D scene.

## When to use this Skill

Use it when:

- the task asks for a polished marketing page, portfolio, dashboard, product UI
  or other interface where appearance is part of the deliverable;
- the user asks for a "premium", "editorial", "immersive", "distinctive" or
  "non-templated" visual result;
- an existing interface works but looks generic, inconsistent or visually flat;
- you need to establish a coherent design system before implementing screens.

Do not use it for:

- a one-line text change, a bug fix with no visual consequence or a backend-only
  task;
- quick internal utilities where the user explicitly values speed over design;
- reproducing a third-party design pixel-for-pixel without permission.

## Start with a design direction

Before writing markup, write a short direction in your working notes:

1. **Subject and audience.** What is this interface for, and what should the
   user feel or understand in the first five seconds?
2. **One visual idea.** A material, a contrast, a spatial rhythm, a typographic
   voice or a colour relationship. One idea carried through beats five
   unrelated effects.
3. **Reference points.** Name real editorial, architectural, product or game
   references. Translate qualities, not layouts. Never copy a living site's
   distinctive composition wholesale.
4. **Constraints.** Content volume, screen sizes, performance budget,
   accessibility requirements and the existing brand or codebase.

Avoid defaulting to the current AI aesthetic: a dark purple gradient, a centred
hero with three identical feature cards, glassmorphism everywhere, huge rounded
corners, floating blobs and a generic geometric sans. If the subject suggests
ink, paper, metal, stone, cloth, light or machinery, let that material drive the
design.

## Layout and hierarchy

- Establish one dominant element per view. Everything else supports it.
- Use an intentional grid, then break it once or twice for emphasis. A broken
  grid should feel decided, not accidental.
- Vary scale and density across sections. A page where every section has the
  same padding, heading size and card count reads as a template.
- Prefer asymmetry with structural balance over centred everything.
- Let whitespace do work. Space is a design material, not unused pixels.
- Design the first viewport to be readable on a laptop around 1280×720. Do not
  hide the actual product or message below a decorative full-screen hero.
- Use alignment deliberately. When elements share an edge, make that edge
  unmistakable; when they do not, offset them clearly.
- Avoid cards inside cards inside cards. Use dividers, typographic grouping and
  background shifts before adding another container.

## Typography

- Choose a display face and a text face with a clear relationship. Two families
  are usually enough; one family used with real weight and size contrast can be
  stronger.
- Load only the weights you use. Declare fallbacks to avoid layout shift.
- Body text should normally sit between 16–19px with 1.5–1.7 line height and a
  measure of roughly 55–75 characters. Long-form text can go slightly larger.
- Use weight, size and case for hierarchy before colour. Do not make everything
  bold.
- Tighten tracking for large display text; leave body tracking alone.
- Avoid centred paragraphs longer than two lines.
- Test real strings, including long names, translated text and numbers. A layout
  that only works with short placeholder copy is not finished.

See `references/typography-and-spacing.md` for a practical scale and spacing
system.

## Colour and surface

- Build from a restrained palette: one dominant surface family, one text family,
  one accent and a small set of semantic colours.
- Contrast is structural: use it to separate zones, not to decorate every
  element.
- A single saturated accent is usually stronger than five competing gradients.
- Define tokens for background, surface, elevated surface, border, muted text,
  primary text, accent and focus. Do not scatter raw hex values through markup.
- Check contrast at the actual rendered sizes. Decorative low-contrast text is
  still unreadable text.
- Use shadows, borders and blur to communicate depth or state. If a surface does
  not need to float, do not make it float.

`resources/design-tokens.css` contains a neutral starting token file. Rename and
reshape it for the subject; do not ship it unchanged as the design.

## Components and consistency

- Define the small set of primitives the interface actually needs: button,
  input, surface, badge, navigation item, dialog, empty state.
- Give each primitive clear states: default, hover, active, focus-visible,
  disabled, loading, error. Missing states are where interfaces feel unfinished.
- Radius, border width, shadow, icon stroke and spacing should follow a small
  scale. Inconsistent micro-values make a design feel noisy even when each part
  looks fine alone.
- Prefer one strong component used well over many variants. Variation should
  come from content and composition.
- Empty, loading and error states are part of the design, not an afterthought.

## Responsive composition

- Design mobile and desktop deliberately; do not simply stack the desktop
  layout.
- Decide what disappears, what reflows and what becomes a drawer or horizontal
  scroller. Hiding essential actions is not responsive design.
- Keep touch targets at least 44×44px where practical.
- Avoid hover-only affordances for essential information.
- Test at 360px, 768px, 1280px and one wide viewport. Check long content and
  zoom at 200%.
- Use fluid type and spacing with sensible clamps, but keep line lengths and
  reading comfort bounded.

## Accessibility as design quality

- Use semantic landmarks, headings in order, real buttons and links, and labels
  tied to inputs.
- Provide visible `:focus-visible` treatment that fits the visual language.
- Never remove focus outlines without replacing them.
- Ensure text and interactive elements meet WCAG AA contrast.
- Respect `prefers-reduced-motion`; motion must not be required to understand
  content.
- Do not use colour alone to communicate state. Add text, iconography, shape or
  position.
- Keyboard-test the complete flow, including dialogs and menus.

## Motion and polish

- Motion should clarify hierarchy, state change or spatial relationships. If it
  only decorates, remove it.
- Use short, eased transitions for state changes (120–240ms for most UI).
  Longer sequences belong to deliberate reveals, not every hover.
- Animate transform and opacity where possible; avoid layout-thrashing
  properties in continuous motion.
- Stagger a small number of elements at most. Excessive stagger makes an
  interface feel slow.
- Add a reduced-motion path for every non-trivial animation.
- Final polish includes optical alignment, consistent icon sizing, correct
  apostrophes, balanced line breaks in headings and no clipped descenders.

## Implementation workflow

1. Inventory the real content and required states.
2. Write the design direction and token set.
3. Build the structural layout with real copy at desktop and mobile widths.
4. Refine typography, spacing and colour against the direction.
5. Add interaction states, empty/loading/error states and responsive behaviour.
6. Add motion only where it communicates.
7. Audit accessibility, performance and visual consistency.
8. Compare the result against the checklist in
   `references/visual-quality-checklist.md` before declaring it finished.

## Tool guidance

- Use `file_control` to read and write project files; inspect the existing
  design system before introducing a second one.
- Use `open_web` only when the user asks you to show the result in a browser or
  to consult an explicitly requested reference. Do not browse for inspiration
  silently.
- Use the terminal only through approved tooling in the host environment. This
  Skill does not grant permissions.

## Quality bar

A finished interface should answer "yes" to all of these:

- Is there a clear visual idea that fits the subject?
- Is the most important content obvious within the first viewport?
- Does the typography feel chosen rather than defaulted?
- Is spacing consistent and generous enough for the density?
- Does it hold together at 360px and at 1280px?
- Can it be used fully with a keyboard and a screen reader?
- Do loading, empty and error states look designed?
- Would a designer recognise this as intentional rather than templated?

If any answer is no, keep refining before presenting the result.
