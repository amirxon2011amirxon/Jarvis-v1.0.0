# Three.js Development

## Objective

Build real-time 3D experiences that are structurally sound, visually coherent
and fast enough to feel immediate. This Skill covers Three.js architecture,
scene composition, asset integration, interaction and rendering budgets. Use
`frontend-design` for the surrounding interface and `web-development` for
application architecture and production tooling.

## When to use this Skill

Use it when:

- implementing a Three.js scene, product configurator, 3D portfolio, data
  visualisation or immersive experience;
- integrating glTF/GLB models, HDR environments, textures or animation clips;
- diagnosing frame drops, shader compilation stalls, memory leaks or broken
  camera behaviour;
- designing interaction for orbit, fly, drag, hover or object selection.

Do not use it for a 2D CSS animation that merely looks three-dimensional, or for
a backend task with no rendering surface.

## Define the experience before the scene

Write a short specification:

1. **Primary subject.** One object, place or spatial idea should dominate.
2. **Camera relationship.** Is the user observing, orbiting, moving through or
   manipulating the subject?
3. **Interaction model.** Orbit, first-person, guided path, click-to-focus,
   drag, scroll-linked or none.
4. **Lighting story.** Daylight, studio, interior, night, abstract emissive or
   stylised unlit.
5. **Performance target.** Target devices, pixel ratio, texture memory and
   acceptable frame rate.
6. **Fallback.** What appears before WebGL loads, on slow devices, or when the
   context is lost?

A scene with no camera or lighting idea becomes a collection of models. Decide
the relationship first.

## Scene architecture

- Keep a small explicit scene graph. Name important nodes and group by function,
  not by file order.
- Separate **world**, **camera rig**, **lighting**, **post-processing** and
  **UI overlay** responsibilities.
- Avoid putting application state into `Object3D.userData` for everything; use a
  dedicated controller/model for behaviour.
- One animation loop for the whole experience. Do not create a renderer loop per
  component.
- Treat renderer, scene, camera and controls as long-lived resources with an
  explicit lifecycle. React components mount and unmount; WebGL resources need a
  matching disposal plan.
- Prefer a single `WebGLRenderer` and canvas. Multiple canvases multiply GPU
  context cost.

## Renderer

- Create the renderer once and resize it from a `ResizeObserver`.
- Cap pixel ratio for performance: often `Math.min(devicePixelRatio, 2)`, lower
  on heavy scenes.
- Set `outputColorSpace = SRGBColorSpace` for colour-managed assets.
- Use `powerPreference: "high-performance"` only when the experience warrants
  it; it can increase battery use.
- Enable shadows deliberately. Shadow maps are expensive; choose cast/receive
  flags per object rather than globally.
- Handle `webglcontextlost` and `webglcontextrestored`. Pause the loop on loss
  and rebuild GPU resources on restore.
- Dispose geometries, materials, textures, render targets and composers when a
  scene or model is removed.

## Camera

- Choose one primary camera and make its behaviour legible. A perspective camera
  around 35–55° FOV often feels more natural than the default 75° for product
  and architectural views.
- Separate camera state from camera object mutation. A controller should own
  target, distance, yaw, pitch and damping.
- Clamp orbit polar angles and distance to prevent the camera entering geometry
  or flipping.
- For product views, frame the bounding box with margin rather than hard-coding
  positions. Recompute on resize and model change.
- Damping should feel weighted, not laggy. Stop rendering when nothing moves if
  the scene is otherwise static.
- For scroll-driven experiences, use a normalised progress value as the source
  of truth and derive camera position from it; do not accumulate deltas that
  drift.

## Lighting

- Start with one key light, one fill or ambient contribution and one accent or
  rim light. Add lights only to solve a visible problem.
- Match light direction to the subject's story. A pagoda at dusk and a chrome
  product need different setups.
- Use physically based units consistently when using PBR materials.
- Bake or fake where possible: emissive materials, gradient environment maps and
  light probes are cheaper than many real lights.
- Shadow quality is a budget: map size, camera frustum and update frequency
  matter more than the number of shadow-casting objects.
- HDR environment maps do most of the work for reflective materials. Use a small
  compressed environment when full HDR is unnecessary.

## Materials and textures

- Use `MeshStandardMaterial` or `MeshPhysicalMaterial` when physical response
  matters; use `MeshBasicMaterial`/`MeshLambertMaterial` for stylised or
  performance-constrained scenes.
- Texture colour space matters: albedo/emissive maps are sRGB, normal/roughness/
  metalness/data maps are linear. Setting the wrong colour space is a common
  cause of washed-out or overly dark assets.
- Reuse materials and textures. Every unique material can become a shader
  program and a draw call.
- Keep texture dimensions appropriate to screen size. A 4K texture on a small
  prop is wasted memory.
- Prefer compressed textures (KTX2/Basis) for large scenes when the pipeline
  supports them.
- For transparency, expect sorting artifacts. Prefer alpha test or opaque
  cutouts where possible, and set `depthWrite` deliberately.

## glTF / GLB integration

- Prefer GLB for a single self-contained asset; use Draco or Meshopt compression
  when load size matters.
- Inspect the asset before coding: node names, scale, orientation, pivots,
  animations, material count and texture dimensions.
- Normalise units and forward axis at import. Do not scatter scale corrections
  through the scene.
- Traverse once after load to set shadows, material overrides and interaction
  metadata. Avoid traversing the whole scene every frame.
- Clone with `SkeletonUtils.clone` when a skinned model must be instanced.
  `Object3D.clone` does not correctly clone skinned meshes.
- Keep a loading manager and show real progress when assets are large. A blank
  canvas with no status feels broken.

## Animation

- Use `AnimationMixer` for imported clips and a small explicit update function
  for procedural motion.
- Drive animation from elapsed time, not frame count. Clamp delta to avoid a
  huge jump after a tab regains focus.
- Keep transitions interruptible. Cross-fade short clips; avoid stacking
  conflicting actions.
- For camera and object motion, use damping or spring functions with stable
  parameters. Do not put a per-frame lerp with a frame-dependent factor in a
  scene that may run at 30 or 120 fps; use delta-aware damping.
- Stop or reduce rendering when the scene is idle, hidden or off-screen.
- Respect `prefers-reduced-motion` for large camera moves and ambient motion.

## Interaction

- Raycasting is a user-input operation, not a per-frame operation. Raycast on
  pointer movement only when hover feedback exists, and throttle if needed.
- Use a larger invisible hit proxy for small objects.
- Distinguish click from drag. Orbit controls and object selection must not fire
  together accidentally.
- Provide keyboard and non-3D alternatives for essential information or actions.
- On touch, avoid requiring hover and make targets forgiving.
- When focusing an object, animate the camera to a framed composition rather
  than snapping to its origin.

## Performance

- Measure with the browser performance panel and `renderer.info`: draw calls,
  triangles, geometries, textures, programs.
- Target a stable frame budget: roughly 16.7ms at 60fps and 33ms at 30fps,
  leaving room for the browser and UI.
- Reduce draw calls through merged static geometry, instancing and shared
  materials.
- Use frustum culling and level of detail for large worlds.
- Prefer fewer, larger textures over many tiny ones when memory and upload cost
  dominate.
- Compile shaders before the first interaction when possible; a first-drag
  stutter is often shader compilation.
- Cap the pixel ratio and render resolution before removing visual features.
- Profile on a mid-range device, not only the development machine.

See `references/performance-checklist.md` for a concrete audit.

## React / framework integration

- Keep the Three.js world in a controller class or module, mounted by one
  component. Do not recreate the renderer when a nearby UI state changes.
- Pass UI intents into the controller through explicit methods; do not read React
  state from the animation loop without a synchronisation plan.
- Dispose the controller on unmount, including controls, observers, animation
  frames and GPU resources.
- If using react-three-fiber, still apply the same scene, disposal and render
  budget rules. It is a reconciler, not a performance guarantee.
- Lazy-load the 3D bundle so the surrounding page can render before WebGL is
  ready.

## Quality bar

- The first frame has a deliberate composition, not a default camera.
- Lighting reveals form and material; it does not merely make the scene visible.
- Interaction is discoverable, forgiving and does not fight the camera.
- The scene holds its target frame rate on a representative device.
- Assets are disposed when removed, and a context loss does not leave a dead
  canvas.
- There is a graceful loading and fallback state.

See `references/scene-recipes.md` for starting compositions for product,
architectural and atmospheric scenes.
