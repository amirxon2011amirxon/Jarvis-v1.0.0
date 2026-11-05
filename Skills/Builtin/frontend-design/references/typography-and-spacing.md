# Typography and Spacing

## A practical type scale

Start from a body size and build a ratio. A 1.2–1.333 ratio works for most
interfaces. Example for a content-led site:

| Role | Size | Line height | Weight |
| --- | --- | --- | --- |
| Display | clamp(48px, 7vw, 104px) | 0.95–1.05 | 500–700 |
| H1 | clamp(36px, 4.5vw, 64px) | 1.05–1.15 | 600 |
| H2 | clamp(28px, 3vw, 40px) | 1.15–1.25 | 600 |
| H3 | 22–26px | 1.3 | 600 |
| Body large | 18–20px | 1.55 | 400 |
| Body | 16–17px | 1.6 | 400 |
| UI label | 13–14px | 1.3 | 500–600 |
| Caption | 11–12px | 1.4 | 500 |

Scale values are starting points. Optical size matters more than arithmetic:
large type usually needs tighter tracking and a shorter line height; small type
needs more line height and often a slightly heavier weight.

## Choosing faces

- Pair a display face with a highly readable text face. Contrast can come from
  width, era, texture or weight rather than from two similar sans-serifs.
- A single family with a wide weight range is often stronger than a weak pair.
- Check that the face supports every language and glyph the product needs.
- Avoid a trendy face solely because it is trendy. Ask what it contributes to
  the subject.
- Use `font-display: swap` and a metric-compatible fallback where possible.

## Measure and rhythm

- Keep body text between 55–75 characters per line. Use `max-width: 65ch` as a
  default, then adjust visually.
- Headings can be wider or narrower than body text, but the choice should be
  consistent within a level.
- Use a baseline spacing scale: 4, 8, 12, 16, 24, 32, 48, 64, 96, 128. Most
  values should land on the scale.
- Space before a heading should be greater than the space after it. This binds
  the heading to its content.
- Use em-based margins for prose so spacing scales with text size.

## Spacing system

Define semantic spacing tokens rather than raw numbers in components:

```css
--space-1: 0.25rem;  /*  4px */
--space-2: 0.5rem;   /*  8px */
--space-3: 0.75rem;  /* 12px */
--space-4: 1rem;     /* 16px */
--space-5: 1.5rem;   /* 24px */
--space-6: 2rem;     /* 32px */
--space-7: 3rem;     /* 48px */
--space-8: 4rem;     /* 64px */
--space-9: 6rem;     /* 96px */
```

Use larger jumps between sections than within them. A section that contains
three items should not have the same gap between items and between sections.

## Responsive type

- Use `clamp()` for display sizes, but keep body text within a comfortable
  range.
- Do not scale all text proportionally with viewport width. Labels and body
  text become unreadable on small screens.
- Re-evaluate line breaks at each breakpoint. A heading that looks balanced at
  1280px may leave a single word on the last line at 360px.
- Test at 200% browser zoom; effective viewport width halves.

## Common mistakes

- Body text under 15px on desktop.
- Line height below 1.4 for paragraphs.
- Letter-spacing applied to lowercase body text.
- Too many weights (400, 500, 600, 700, 800) in one view.
- All-caps labels with normal tracking; add 0.06–0.12em if using uppercase.
- Centred long paragraphs.
- Headings that are bold only because the design lacks another hierarchy tool.
- Placeholder Latin copy that hides real wrapping problems.
