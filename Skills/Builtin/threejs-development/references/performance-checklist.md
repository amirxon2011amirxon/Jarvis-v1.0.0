# Three.js Performance Checklist

## Frame budget

- [ ] Measure frame time on a mid-range device, not only on the development
      machine.
- [ ] Keep the render loop to one `requestAnimationFrame` chain.
- [ ] Stop rendering when the scene is hidden, idle or off-screen.
- [ ] Clamp delta time after tab switches and long pauses.
- [ ] Separate expensive updates (physics, raycasts, DOM writes) from the render
      loop where possible.

## Renderer

- [ ] One `WebGLRenderer`, one canvas.
- [ ] Pixel ratio capped (often 2 or lower for heavy scenes).
- [ ] Correct output colour space and tone mapping.
- [ ] Shadow maps sized to the visible scene, not the entire world.
- [ ] `renderer.info` inspected for draw calls, triangles, geometries, textures
      and programs.
- [ ] Context loss and restoration handled.
- [ ] Antialiasing chosen deliberately; post-processing may make MSAA redundant.

## Geometry and draw calls

- [ ] Static meshes merged where they share a material.
- [ ] Repeated objects instanced.
- [ ] Materials and geometries reused.
- [ ] Transparent objects kept to a minimum.
- [ ] Off-screen and distant objects culled or simplified.
- [ ] Shadow-casting flags set per object, not on every object by default.
- [ ] Level of detail used for large worlds where appropriate.

## Textures

- [ ] Dimensions sized to the rendered screen area.
- [ ] Colour space correct per map type.
- [ ] Compressed formats used for large scenes where supported.
- [ ] Mipmaps enabled where appropriate.
- [ ] Texture memory estimated against the target device budget.
- [ ] Unused textures and render targets disposed.

## Shaders and materials

- [ ] Material count kept low; shared programs where possible.
- [ ] Shader compilation warmed before first interaction.
- [ ] Custom shaders have no per-frame uniform uploads unless needed.
- [ ] `onBeforeCompile` changes are tested across the Three.js version in use.
- [ ] Post-processing passes are justified by the visual result and measured.

## Animation

- [ ] Animation uses elapsed time, not frame count.
- [ ] Damping is delta-aware.
- [ ] Mixers stop or pause when off-screen.
- [ ] Long clips do not allocate objects per frame.
- [ ] Reduced-motion path exists for large camera moves.

## Interaction

- [ ] Raycasting is event-driven, not per-frame.
- [ ] Hit proxies are large enough for touch.
- [ ] Orbit and object drag do not fire together.
- [ ] Camera controls clamp distance and polar angle.
- [ ] Hover feedback has a non-hover equivalent.
- [ ] Keyboard and DOM alternatives exist for essential actions.

## Assets and lifecycle

- [ ] GLB inspected for scale, pivots, material count and animation clips.
- [ ] Loading progress is real, not a fake spinner.
- [ ] Failed model loads render a useful fallback.
- [ ] Geometries, materials, textures, render targets and composers disposed on
      unmount.
- [ ] Animation frame, resize observer and event listeners removed on unmount.
- [ ] A WebGL context loss does not leave the page stuck on a blank canvas.

## Motion and accessibility

- [ ] Large camera motion respects `prefers-reduced-motion`.
- [ ] Flashing or rapid luminance changes avoided.
- [ ] Essential information is available outside the canvas.
- [ ] Canvas has an accessible name and instructions where interaction matters.
- [ ] The experience remains usable when WebGL is unavailable.
