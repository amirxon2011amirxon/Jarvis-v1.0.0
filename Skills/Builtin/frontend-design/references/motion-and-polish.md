# Motion and Polish

## Principles

Motion is a communication layer. It should answer one of these questions:

- What changed?
- Where did it come from or go?
- Which element now has focus?
- Is the system working?

If motion answers none of them, it is decoration. Decoration is allowed in
small doses, but it must not delay reading or interaction.

## Timing

| Interaction | Duration | Easing |
| --- | --- | --- |
| Hover / press feedback | 100–160ms | ease-out |
| Small state change | 140–220ms | cubic-bezier(.2,.8,.2,1) |
| Panel / drawer | 220–320ms | cubic-bezier(.2,.8,.2,1) |
| Page-level reveal | 300–500ms | gentle ease-out |
| Ambient loop | 6–20s | linear or sine |

Duration should grow with distance, but not without bound. A menu that takes
600ms to open feels broken.

## Choreography

- Animate the container before its contents.
- Stagger no more than 3–5 items, with 40–80ms between them.
- Keep transform origins consistent with the direction of travel.
- Never animate every card independently as it scrolls into view; it turns a
  page into a slideshow.
- Use one motion language across the product: the same easing, distance and
  layering rules.

## Performance

- Prefer `transform` and `opacity`. They can run on the compositor.
- Avoid animating `width`, `height`, `top`, `left`, `margin` or `box-shadow`
  continuously.
- Use `will-change` sparingly and remove it after the interaction.
- Respect `prefers-reduced-motion` with a real alternative, not a broken
  half-state.

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

For meaningful spatial motion, consider a cross-fade or an instant state change
instead of merely shortening the animation.

## Optical polish

- Align icon centres optically, not just by bounding box.
- Match icon stroke weight to text weight.
- Nudge punctuation and quotation marks where the typeface hangs them poorly.
- Avoid clipped descenders in tight line-height display text.
- Check borders against both light and dark surfaces; a 1px border can appear
  heavier on dark backgrounds.
- Use consistent corner radii. A pill button beside a 4px card looks accidental
  unless the contrast is clearly intentional.
- Check the design at 100% zoom on a real display. A zoomed-out canvas hides
  spacing and contrast problems.

## Final pass

1. Remove one effect. If the design improves, remove another.
2. Check focus, hover, active, disabled and loading states.
3. Check reduced motion.
4. Check the longest realistic content.
5. Check the smallest supported viewport.
6. Check that the primary action is still obvious after all polish is applied.
