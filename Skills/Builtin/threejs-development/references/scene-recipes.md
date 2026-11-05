# Scene Recipes

These are starting compositions, not templates to copy without adapting. Each
one assumes a single primary subject and a deliberate camera relationship.

## Product studio

**Subject:** one object with material detail (metal, glass, fabric, ceramic).

- Camera: perspective 35–45° FOV, elevated slightly above the object, aimed at
  the bounding-box centre with margin.
- Lighting: large soft key from front-left, dim fill from the right, a narrow rim
  behind to separate the silhouette.
- Environment: small neutral HDR or a generated gradient environment for
  reflections.
- Ground: subtle shadow-catching plane or contact shadow, not a mirror floor.
- Motion: slow idle turntable or drag-to-rotate, never both automatically.
- UI: controls outside the canvas; the object remains the hero.

Watch for: blown-out metal without an environment, harsh shadow edges, camera
clipping into the model, transparent glass sorting artifacts.

## Architectural / cultural scene

**Subject:** a building or structure with a strong silhouette and context.

- Camera: eye-level or slightly low, 40–55° FOV; the structure should feel
  grounded, not floating.
- Lighting: directional key with long soft shadows for dusk; warm interior
  emissive windows; cool sky fill.
- Environment: gradient sky or compressed HDRI, atmospheric fog at distance.
- Ground: extended terrain or plaza with material variation to anchor scale.
- Motion: gentle camera drift or scroll-linked approach; avoid a constant
  orbit that hides the entrance.
- Detail: use instancing for repeated lanterns, tiles or trees.

Watch for: every surface sharing one material, no scale reference, shadows
pointing in conflicting directions, aliasing on thin geometry.

## Atmospheric landscape

**Subject:** terrain, horizon or weather mood rather than a single object.

- Camera: wide 50–60° FOV, low horizon, slow movement.
- Lighting: low sun angle, strong colour temperature separation between light
  and shadow.
- Environment: layered fog, distant silhouettes, sky dome or gradient.
- Ground: vertex-coloured or texture-blended terrain; avoid a tiled texture that
  reveals its repetition.
- Motion: wind-driven vegetation, drifting particles, very slow camera parallax.
- Performance: LOD for terrain and vegetation; instancing for repeated elements.

Watch for: fog hiding the subject rather than depth, uniform particle density,
motion sickness from excessive camera sway.

## Abstract data space

**Subject:** relationships, clusters or flow represented spatially.

- Camera: orbit around a stable centre; allow zoom-to-cluster.
- Lighting: emissive nodes, dim environment, restrained bloom.
- Geometry: instanced nodes and line segments; labels rendered outside WebGL
  where possible.
- Interaction: click to focus, hover to reveal metadata, escape to reset.
- Performance: cap visible nodes, aggregate distant clusters, pause simulation
  when idle.
- Accessibility: provide a table or list view with the same information.

Watch for: meaning encoded only by colour, unreadable labels, physics that never
settle, bloom that obscures the data.

## Scroll-linked narrative

- Use document scroll progress as the single source of truth.
- Map progress ranges to camera positions, object states and copy sections.
- Precompute keyframes and interpolate; do not run separate scroll listeners per
  object.
- Provide a reduced-motion version with static sections.
- Ensure the experience is navigable by keyboard and that content exists in the
  DOM.

Watch for: scroll hijacking, long dead zones, cameras that overshoot, content
that cannot be reached without a wheel.
