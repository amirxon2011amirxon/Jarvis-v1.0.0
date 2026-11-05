import { memo, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";

import type { AzeState, SubscribeAudioLevel } from "../types";
import { cn } from "../lib/utils";

interface ArcReactorProps {
  state: AzeState;
  subscribeAudio: SubscribeAudioLevel;
  activeTools: number;
  muted: boolean;
  error: boolean;
  accent: string;
  /** true = glow constantly; false = glow while speaking. */
  glowAlways: boolean;
  /** Stop the animation loop while the video player covers the stage. */
  suspended?: boolean;
}

function glowTexture(): THREE.Texture {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 256;
  const context = canvas.getContext("2d");
  if (context) {
    const gradient = context.createRadialGradient(
      128, 128, 0, 128, 128, 128);
    gradient.addColorStop(0, "rgba(176,232,255,0.95)");
    gradient.addColorStop(0.22, "rgba(84,186,255,0.42)");
    gradient.addColorStop(0.55, "rgba(54,134,220,0.10)");
    gradient.addColorStop(1, "rgba(0,0,0,0)");
    context.fillStyle = gradient;
    context.fillRect(0, 0, 256, 256);
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function ArcReactorBase({
  state,
  subscribeAudio,
  activeTools,
  muted,
  error,
  glowAlways,
  suspended = false,
}: ArcReactorProps) {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [failed, setFailed] = useState(false);
  const levelRef = useRef(0);
  const tiltTarget = useRef({ x: 0, y: 0 });
  const stateRef = useRef({ state, activeTools, muted, error, glowAlways });
  const suspendedRef = useRef(suspended);
  stateRef.current = { state, activeTools, muted, error, glowAlways };
  suspendedRef.current = suspended;

  const stars = useMemo(() => Array.from({ length: 34 }, (_, index) => {
    const angle = (index * 137.508 + 11) * (Math.PI / 180);
    const radius = 45 + ((index * 7) % 18);
    return {
      left: 50 + Math.cos(angle) * radius,
      top: 50 + Math.sin(angle) * radius,
      size: index % 7 === 0 ? 3 : index % 3 === 0 ? 2 : 1.4,
      delay: (index % 11) * 0.27,
      blue: index % 5 === 0,
    };
  }), []);

  useEffect(() => {
    return subscribeAudio((level, source) => {
      if (source === "output") {
        levelRef.current = Math.max(levelRef.current, level);
      } else if (stateRef.current.state === "LISTENING") {
        levelRef.current = Math.max(levelRef.current, level * 0.72);
      }
    });
  }, [subscribeAudio]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: true,
        powerPreference: "high-performance",
      });
    } catch {
      setFailed(true);
      return;
    }

    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 0.46;
    renderer.setClearColor(0x000000, 0);
    renderer.domElement.className = "aze-reactor-canvas";
    host.appendChild(renderer.domElement);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 100);
    camera.position.set(0, 0, 8.6);
    camera.lookAt(0, 0, 0);

    const pmrem = new THREE.PMREMGenerator(renderer);
    pmrem.compileEquirectangularShader();
    const room = new RoomEnvironment();
    const environment = pmrem.fromScene(room, 0.035).texture;
    scene.environment = environment;
    room.dispose?.();

    const disposables: Array<{ dispose: () => void }> = [
      environment, pmrem,
    ];
    const track = <T extends { dispose: () => void }>(item: T): T => {
      disposables.push(item);
      return item;
    };
    const material = (
      options: THREE.MeshStandardMaterialParameters,
    ): THREE.MeshStandardMaterial => track(new THREE.MeshStandardMaterial(options));
    const geometry = <T extends THREE.BufferGeometry>(item: T): T => track(item);

    const physicalGold = (
      options: THREE.MeshPhysicalMaterialParameters,
    ): THREE.MeshPhysicalMaterial => track(new THREE.MeshPhysicalMaterial(options));
    const gold = physicalGold({
      color: 0xb9852c,
      metalness: 1,
      roughness: 0.2,
      clearcoat: 0.65,
      clearcoatRoughness: 0.14,
      envMapIntensity: 0.62,
    });
    const brightGold = physicalGold({
      color: 0xf3c45b,
      metalness: 1,
      roughness: 0.12,
      clearcoat: 0.8,
      clearcoatRoughness: 0.08,
      envMapIntensity: 0.72,
    });
    const darkGold = physicalGold({
      color: 0x4d3310,
      metalness: 0.96,
      roughness: 0.34,
      clearcoat: 0.35,
      clearcoatRoughness: 0.25,
      envMapIntensity: 0.45,
    });
    const spokeGold = material({
      color: 0x7a4f12,
      metalness: 0.72,
      roughness: 0.58,
      envMapIntensity: 0.2,
    });
    const darkMetal = material({
      color: 0x0e0b07,
      metalness: 0.9,
      roughness: 0.36,
      envMapIntensity: 0.35,
    });
    const blackGlass = material({
      color: 0x05070a,
      metalness: 0.18,
      roughness: 0.76,
      envMapIntensity: 0.08,
    });
    const blueCore = material({
      color: 0x041827,
      emissive: 0x2a9fe0,
      emissiveIntensity: 0.95,
      metalness: 0.6,
      roughness: 0.22,
    });
    const blueLine = new THREE.MeshBasicMaterial({
      color: 0x8ce6ff,
      toneMapped: false,
    });
    disposables.push(blueLine);

    const reactor = new THREE.Group();
    reactor.rotation.x = -0.08;
    scene.add(reactor);

    const backing = new THREE.Mesh(
      geometry(new THREE.CircleGeometry(3.55, 128)),
      material({
        color: 0x090704,
        metalness: 0.7,
        roughness: 0.4,
        envMapIntensity: 0.8,
      }),
    );
    backing.position.z = -0.62;
    reactor.add(backing);

    const glowMap = glowTexture();
    disposables.push(glowMap);
    const coreGlow = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowMap,
      color: 0x72d4ff,
      transparent: true,
      opacity: 0.20,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      toneMapped: false,
    }));
    coreGlow.scale.set(2.15, 2.15, 1);
    coreGlow.position.z = 0.9;
    reactor.add(coreGlow);
    disposables.push(coreGlow.material);

    const warmGlow = new THREE.Sprite(new THREE.SpriteMaterial({
      map: glowMap,
      color: 0xffbd55,
      transparent: true,
      opacity: 0.035,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      toneMapped: false,
    }));
    warmGlow.scale.set(4.0, 4.0, 1);
    warmGlow.position.z = -0.9;
    reactor.add(warmGlow);
    disposables.push(warmGlow.material);

    const torus = (
      radius: number,
      tube: number,
      mat: THREE.Material,
      arc = Math.PI * 2,
      radial = 24,
      tubular = 160,
    ) => {
      const mesh = new THREE.Mesh(
        geometry(new THREE.TorusGeometry(radius, tube, radial, tubular, arc)),
        mat,
      );
      return mesh;
    };

    const outerRotor = new THREE.Group();
    const innerRotor = new THREE.Group();
    reactor.add(outerRotor, innerRotor);

    const outerBase = torus(3.38, 0.09, brightGold, Math.PI * 2, 20, 220);
    outerBase.position.z = -0.3;
    reactor.add(outerBase);
    outerRotor.add(torus(3.12, 0.28, darkMetal, Math.PI * 2, 20, 220));
    outerRotor.add(torus(2.5, 0.1, brightGold, Math.PI * 2, 16, 200));

    // Gear-like outer teeth and deep machined grooves. These make the rim read
    // as a heavy cast/machined assembly rather than a smooth ring.
    const toothGeometry = geometry(new THREE.BoxGeometry(0.2, 0.32, 0.3));
    const teeth = new THREE.InstancedMesh(toothGeometry, gold, 36);
    const toothMatrix = new THREE.Matrix4();
    for (let index = 0; index < 36; index += 1) {
      const angle = (index / 36) * Math.PI * 2;
      toothMatrix.makeRotationZ(angle);
      toothMatrix.setPosition(
        Math.cos(angle) * 3.46,
        Math.sin(angle) * 3.46,
        -0.04,
      );
      teeth.setMatrixAt(index, toothMatrix);
    }
    outerRotor.add(teeth);

    outerRotor.add(torus(3.34, 0.045, darkMetal, Math.PI * 2, 10, 220));
    outerRotor.add(torus(3.2, 0.04, darkGold, Math.PI * 2, 10, 220));
    outerRotor.add(torus(2.98, 0.03, brightGold, Math.PI * 2, 10, 220));

    // Dark turbine vanes sit behind the plates and are visible through the
    // gaps. One instanced draw call keeps the extra depth cheap.
    const vaneGeometry = geometry(new THREE.BoxGeometry(0.16, 0.9, 0.12));
    const vanes = new THREE.InstancedMesh(vaneGeometry, darkGold, 24);
    const vaneMatrix = new THREE.Matrix4();
    for (let index = 0; index < 24; index += 1) {
      const angle = (index / 24) * Math.PI * 2;
      vaneMatrix.makeRotationZ(angle);
      vaneMatrix.setPosition(
        Math.cos(angle) * 2.56,
        Math.sin(angle) * 2.56,
        -0.26,
      );
      vanes.setMatrixAt(index, vaneMatrix);
    }
    outerRotor.add(vanes);

    // Layered outer armour plates with visible gaps, rather than one smooth
    // torus. Each plate gets a dark backing lip and a bright bevel.
    for (let index = 0; index < 12; index += 1) {
      const step = (Math.PI * 2) / 12;
      const start = index * step + 0.055;
      const length = step - 0.19;
      const backingPlate = torus(2.9, 0.39, darkGold, length, 18, 22);
      backingPlate.rotation.z = start;
      backingPlate.position.z = -0.08;
      outerRotor.add(backingPlate);
      const plate = torus(
        2.9, 0.3, index % 4 === 0 ? brightGold : gold, length, 18, 22);
      plate.rotation.z = start + 0.012;
      plate.position.z = 0.11;
      outerRotor.add(plate);
      const bevel = torus(3.18, 0.055, brightGold, length + 0.04, 12, 18);
      bevel.rotation.z = start;
      bevel.position.z = 0.26;
      outerRotor.add(bevel);
    }

    for (let index = 0; index < 14; index += 1) {
      const start = index * (Math.PI * 2 / 14) + 0.03;
      const length = (Math.PI * 2 / 14) - 0.1;
      const plate = torus(
        3.18,
        0.34,
        index % 3 === 0 ? darkGold : gold,
        length,
        18,
        36,
      );
      plate.rotation.z = start;
      plate.position.z = 0.08 + (index % 2) * 0.03;
      outerRotor.add(plate);
    }

    // Machined vent slots around the armour ring. Instancing keeps this detail
    // cheap: one draw call for all 48 slots.
    const ventGeometry = geometry(new THREE.BoxGeometry(0.12, 0.2, 0.06));
    const vents = new THREE.InstancedMesh(ventGeometry, darkMetal, 48);
    const ventMatrix = new THREE.Matrix4();
    for (let index = 0; index < 48; index += 1) {
      const angle = (index / 48) * Math.PI * 2;
      ventMatrix.makeRotationZ(angle + Math.PI / 2);
      ventMatrix.setPosition(
        Math.cos(angle) * 3.27,
        Math.sin(angle) * 3.27,
        0.4,
      );
      vents.setMatrixAt(index, ventMatrix);
    }
    outerRotor.add(vents);

    // Radial fins visible between the outer plates, mostly in the dark gaps.
    const finGeometry = geometry(new THREE.BoxGeometry(0.07, 0.3, 0.14));
    const fins = new THREE.InstancedMesh(finGeometry, darkGold, 24);
    const finMatrix = new THREE.Matrix4();
    for (let index = 0; index < 24; index += 1) {
      const angle = (index / 24) * Math.PI * 2;
      finMatrix.makeRotationZ(angle);
      finMatrix.setPosition(
        Math.cos(angle) * 3.03,
        Math.sin(angle) * 3.03,
        -0.2,
      );
      fins.setMatrixAt(index, finMatrix);
    }
    outerRotor.add(fins);

    const innerRotorGroup = new THREE.Group();
    innerRotor.add(innerRotorGroup);
    innerRotorGroup.add(torus(2.16, 0.08, brightGold, Math.PI * 2, 14, 180));
    innerRotorGroup.add(torus(1.92, 0.25, darkMetal, Math.PI * 2, 18, 160));
    innerRotorGroup.add(torus(1.68, 0.11, gold, Math.PI * 2, 16, 160));
    innerRotorGroup.add(torus(1.44, 0.2, darkGold, Math.PI * 2, 18, 160));
    innerRotorGroup.add(torus(1.23, 0.06, brightGold, Math.PI * 2, 14, 160));

    const innerToothGeometry = geometry(
      new THREE.BoxGeometry(0.16, 0.22, 0.16));
    const innerTeeth = new THREE.InstancedMesh(
      innerToothGeometry, brightGold, 22);
    const innerToothMatrix = new THREE.Matrix4();
    for (let index = 0; index < 22; index += 1) {
      const angle = (index / 22) * Math.PI * 2;
      innerToothMatrix.makeRotationZ(angle);
      innerToothMatrix.setPosition(
        Math.cos(angle) * 1.82,
        Math.sin(angle) * 1.82,
        0.29,
      );
      innerTeeth.setMatrixAt(index, innerToothMatrix);
    }
    innerRotorGroup.add(innerTeeth);

    // Hairline grooves catch the key light and add scale to the metal.
    for (const radius of [2.26, 2.08, 1.78, 1.56, 1.33]) {
      const groove = torus(radius, 0.012, darkMetal, Math.PI * 2, 8, 180);
      groove.position.z = 0.34;
      innerRotorGroup.add(groove);
    }

    const blueRing = new THREE.Mesh(
      geometry(new THREE.TorusGeometry(2.03, 0.018, 8, 180)),
      blueLine,
    );
    blueRing.position.z = 0.25;
    innerRotorGroup.add(blueRing);
    const blueRingInner = new THREE.Mesh(
      geometry(new THREE.TorusGeometry(1.34, 0.016, 8, 160)),
      blueLine,
    );
    blueRingInner.position.z = 0.26;
    innerRotorGroup.add(blueRingInner);

    // Short machined arcs between the continuous rings. The gaps read as
    // separate metal shoes instead of a painted circle.
    for (let index = 0; index < 14; index += 1) {
      const step = (Math.PI * 2) / 14;
      const arc = torus(
        1.15,
        0.045,
        index % 3 === 0 ? brightGold : gold,
        step - 0.16,
        10,
        14,
      );
      arc.rotation.z = index * step + 0.08;
      arc.position.z = 0.3;
      innerRotorGroup.add(arc);
    }

    const spokeAngles = [0, Math.PI / 2, Math.PI, Math.PI * 1.5];
    for (const angle of spokeAngles) {
      const spoke = new THREE.Group();
      spoke.rotation.z = angle;
      const arm = new THREE.Mesh(
        geometry(new THREE.BoxGeometry(0.54, 2.2, 0.42)),
        spokeGold,
      );
      arm.position.y = 1.78;
      const sideLeft = new THREE.Mesh(
        geometry(new THREE.BoxGeometry(0.08, 2.0, 0.5)),
        gold,
      );
      sideLeft.position.set(-0.31, 1.78, 0.02);
      const sideRight = sideLeft.clone();
      sideRight.position.x = 0.31;
      const inset = new THREE.Mesh(
        geometry(new THREE.BoxGeometry(0.18, 1.92, 0.44)),
        blackGlass,
      );
      inset.position.set(0, 1.78, 0.2);
      const cap = new THREE.Mesh(
        geometry(new THREE.BoxGeometry(0.82, 0.38, 0.62)),
        gold,
      );
      cap.position.y = 2.9;
      const foot = new THREE.Mesh(
        geometry(new THREE.BoxGeometry(0.5, 0.32, 0.44)),
        darkGold,
      );
      foot.position.y = 0.72;
      const blueRail = new THREE.Mesh(
        geometry(new THREE.BoxGeometry(0.035, 1.75, 0.02)),
        blueLine,
      );
      blueRail.position.set(0.22, 1.78, 0.34);
      spoke.add(arm, sideLeft, sideRight, inset, cap, foot, blueRail);
      reactor.add(spoke);
    }

    const boltGeometry = geometry(new THREE.SphereGeometry(0.04, 8, 8));
    const bolts = new THREE.InstancedMesh(boltGeometry, brightGold, 96);
    const matrix = new THREE.Matrix4();
    let boltIndex = 0;
    for (const radius of [3.36, 2.6, 2.22, 1.42]) {
      const count = radius > 3 ? 32 : 20;
      for (let index = 0; index < count; index += 1) {
        const angle = (index / count) * Math.PI * 2;
        matrix.makeTranslation(
          Math.cos(angle) * radius,
          Math.sin(angle) * radius,
          0.38,
        );
        bolts.setMatrixAt(boltIndex, matrix);
        boltIndex += 1;
      }
    }
    bolts.count = boltIndex;
    outerRotor.add(bolts);

    const coreGroup = new THREE.Group();
    coreGroup.position.z = 0.36;
    reactor.add(coreGroup);

    coreGroup.add(torus(1.05, 0.13, brightGold, Math.PI * 2, 16, 160));
    coreGroup.add(torus(0.94, 0.06, blueLine, Math.PI * 2, 12, 140));
    const innerGold = torus(0.86, 0.12, gold, Math.PI * 2, 16, 140);
    innerGold.position.z = 0.05;
    coreGroup.add(innerGold);

    const orbBezel = torus(0.58, 0.08, brightGold, Math.PI * 2, 18, 140);
    orbBezel.position.z = 0.18;
    coreGroup.add(orbBezel);
    const orbBlueRing = torus(0.49, 0.035, blueLine, Math.PI * 2, 12, 140);
    orbBlueRing.position.z = 0.3;
    coreGroup.add(orbBlueRing);

    // An iris of machined blades around the blue orb, plus fine coil windings.
    for (let index = 0; index < 10; index += 1) {
      const angle = (index / 10) * Math.PI * 2;
      const blade = new THREE.Mesh(
        geometry(new THREE.BoxGeometry(0.14, 0.24, 0.035)),
        index % 2 === 0 ? darkGold : spokeGold,
      );
      blade.position.set(
        Math.cos(angle) * 0.67,
        Math.sin(angle) * 0.67,
        0.45,
      );
      blade.rotation.z = angle + Math.PI / 2;
      coreGroup.add(blade);
    }
    for (const radius of [0.69, 0.75, 0.81]) {
      const coil = torus(radius, 0.009, blueLine, Math.PI * 2, 8, 120);
      coil.position.z = 0.49 + (radius - 0.69) * 0.9;
      coreGroup.add(coil);
    }

    const orb = new THREE.Mesh(
      geometry(new THREE.SphereGeometry(0.42, 48, 32)),
      material({
        color: 0x04131f,
        emissive: 0x3fbfff,
        emissiveIntensity: 1.35,
        metalness: 0.25,
        roughness: 0.16,
      }),
    );
    orb.position.z = 0.3;
    coreGroup.add(orb);

    const orbInner = new THREE.Mesh(
      geometry(new THREE.SphereGeometry(0.2, 32, 24)),
      material({
        color: 0x0a3550,
        emissive: 0x9eeaff,
        emissiveIntensity: 1.6,
        metalness: 0.1,
        roughness: 0.08,
      }),
    );
    orbInner.position.z = 0.56;
    coreGroup.add(orbInner);

    const orbHalo = new THREE.Mesh(
      geometry(new THREE.RingGeometry(0.5, 0.56, 72)),
      blueLine,
    );
    orbHalo.position.z = 0.33;
    coreGroup.add(orbHalo);

    // Front retaining bezel and a faint glass shell over the emitter.
    const frontBezel = torus(0.63, 0.035, gold, Math.PI * 2, 14, 160);
    frontBezel.position.z = 0.62;
    coreGroup.add(frontBezel);
    const frontBevel = torus(0.68, 0.016, brightGold, Math.PI * 2, 10, 160);
    frontBevel.position.z = 0.66;
    coreGroup.add(frontBevel);

    const glassShell = new THREE.Mesh(
      geometry(new THREE.SphereGeometry(0.47, 32, 24)),
      new THREE.MeshPhysicalMaterial({
        color: 0xa6e9ff,
        metalness: 0,
        roughness: 0.05,
        clearcoat: 1,
        clearcoatRoughness: 0.03,
        transparent: true,
        opacity: 0.1,
        depthWrite: false,
      }),
    );
    glassShell.position.z = 0.3;
    coreGroup.add(glassShell);
    disposables.push(glassShell.material);

    const clipGeometry = geometry(new THREE.BoxGeometry(0.1, 0.16, 0.05));
    const clips = new THREE.InstancedMesh(clipGeometry, darkGold, 10);
    const clipMatrix = new THREE.Matrix4();
    for (let index = 0; index < 10; index += 1) {
      const angle = (index / 10) * Math.PI * 2;
      clipMatrix.makeRotationZ(angle);
      clipMatrix.setPosition(
        Math.cos(angle) * 0.63,
        Math.sin(angle) * 0.63,
        0.64,
      );
      clips.setMatrixAt(index, clipMatrix);
    }
    coreGroup.add(clips);

    const bezelBoltGeometry = geometry(new THREE.SphereGeometry(0.018, 6, 6));
    const bezelBolts = new THREE.InstancedMesh(
      bezelBoltGeometry, brightGold, 24);
    const bezelMatrix = new THREE.Matrix4();
    for (let index = 0; index < 24; index += 1) {
      const angle = (index / 24) * Math.PI * 2;
      bezelMatrix.makeTranslation(
        Math.cos(angle) * 0.54,
        Math.sin(angle) * 0.54,
        0.57,
      );
      bezelBolts.setMatrixAt(index, bezelMatrix);
    }
    coreGroup.add(bezelBolts);

    const coreLight = new THREE.PointLight(0x59c8ff, 1.4, 4.5, 2);
    coreLight.position.set(0, 0, 1.3);
    reactor.add(coreLight);

    scene.add(new THREE.AmbientLight(0x4d3718, 0.1));
    const keyLight = new THREE.DirectionalLight(0xffd08a, 1.35);
    keyLight.position.set(4.5, 5.5, 6);
    scene.add(keyLight);
    const rimLight = new THREE.DirectionalLight(0x8bdcff, 0.45);
    rimLight.position.set(-5, -2, 4);
    scene.add(rimLight);
    const fillLight = new THREE.DirectionalLight(0xffa93c, 0.3);
    fillLight.position.set(-4, 4, 2);
    scene.add(fillLight);

    // One draw call per dust layer: cheap, stable ambient stars around the
    // reactor. Positions are fixed; only the group rotates and the material
    // twinkles, so the frame cost stays constant.
    let seed = 0x51a7;
    const random = () => {
      seed = (seed * 1664525 + 1013904223) >>> 0;
      return seed / 0xffffffff;
    };
    const particleGroup = new THREE.Group();
    reactor.add(particleGroup);
    const makeDust = (
      count: number,
      color: number,
      size: number,
      minRadius: number,
      maxRadius: number,
    ) => {
      const positions = new Float32Array(count * 3);
      for (let index = 0; index < count; index += 1) {
        const angle = random() * Math.PI * 2;
        const radius = minRadius + random() * (maxRadius - minRadius);
        positions[index * 3] = Math.cos(angle) * radius;
        positions[index * 3 + 1] = Math.sin(angle) * radius;
        positions[index * 3 + 2] = -1.7 + random() * 3.6;
      }
      const buffer = geometry(new THREE.BufferGeometry());
      buffer.setAttribute(
        "position", new THREE.BufferAttribute(positions, 3));
      const points = new THREE.Points(buffer, new THREE.PointsMaterial({
        map: glowMap,
        color,
        size,
        transparent: true,
        opacity: 0.55,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
        sizeAttenuation: true,
        toneMapped: false,
      }));
      disposables.push(points.material);
      particleGroup.add(points);
      return points;
    };
    const goldDust = makeDust(220, 0xffc55f, 0.095, 3.2, 5.4);
    const blueDust = makeDust(100, 0x79dbff, 0.075, 2.8, 4.8);

    const resize = () => {
      const rect = host.getBoundingClientRect();
      const width = Math.max(1, Math.round(rect.width));
      const height = Math.max(1, Math.round(rect.height));
      renderer.setSize(width, height, false);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    resize();
    const observer = typeof ResizeObserver === "function"
      ? new ResizeObserver(resize)
      : null;
    observer?.observe(host);
    window.addEventListener("resize", resize, { passive: true });

    const clock = new THREE.Clock();
    let raf = 0;
    let smoothed = 0;
    let lastRender = 0;

    const animate = () => {
      raf = window.requestAnimationFrame(animate);
      const delta = Math.min(0.05, clock.getDelta());
      const now = performance.now();
      const current = stateRef.current;
      const speaking = current.state === "SPEAKING" || current.activeTools > 0;
      const thinking = current.state === "THINKING"
        || current.state === "PROCESSING";
      const listening = current.state === "LISTENING";
      const target = levelRef.current;
      levelRef.current *= Math.exp(-delta * 6);
      smoothed += (target - smoothed)
        * (target > smoothed ? 1 - Math.exp(-delta * 22)
          : 1 - Math.exp(-delta * 5));

      outerRotor.rotation.z += delta * (speaking ? 0.36 : thinking ? 0.16 : 0.06);
      innerRotorGroup.rotation.z -= delta * (speaking ? 0.52 : 0.2);
      coreGroup.rotation.z = Math.sin(now / 2600) * 0.012;

      const pulse = 1 + smoothed * 0.06
        + (listening ? Math.sin(now / 900) * 0.008 : 0);
      coreGroup.scale.setScalar(pulse);
      blueprintGlow(blueCore, smoothed, speaking);
      const glowIntensity = (current.glowAlways ? 0.62 : 0.34)
        + smoothed * (speaking ? 1.5 : 0.55)
        + (thinking ? 0.22 : 0);
      coreGlow.material.opacity = Math.min(0.62, glowIntensity);
      warmGlow.material.opacity = 0.025 + smoothed * 0.08;
      warmGlow.scale.setScalar(4.0 + smoothed * 0.5);
      coreLight.intensity = 1.2 + smoothed * 4.5;
      reactor.rotation.x = -0.08 + tiltTarget.current.x * 0.012;
      reactor.rotation.y = tiltTarget.current.y * 0.012;
      reactor.position.y = Math.sin(now / 1900) * 0.055;
      reactor.position.z = Math.sin(now / 2600) * 0.025;
      particleGroup.rotation.z += delta * 0.035;
      particleGroup.rotation.x = Math.sin(now / 4200) * 0.04;
      particleGroup.visible = !suspendedRef.current;
      goldDust.material.opacity = 0.55 + Math.sin(now / 1300) * 0.14;
      blueDust.material.opacity = 0.48 + Math.cos(now / 1700) * 0.12;

      const currentSuspended = suspendedRef.current;
      if (!currentSuspended && now - lastRender > 10) {
        renderer.render(scene, camera);
        lastRender = now;
      }
      if (currentSuspended) {
        clock.getDelta();
      }
    };
    animate();

    return () => {
      window.cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      observer?.disconnect();
      renderer.dispose();
      renderer.domElement.remove();
      for (const item of disposables) {
        try {
          item.dispose();
        } catch {
          // A disposed resource must not break unmount.
        }
      }
      for (const item of [outerBase, ...outerRotor.children,
                          ...innerRotor.children, coreGlow, warmGlow]) {
        if (item instanceof THREE.Mesh) {
          item.geometry?.dispose?.();
        }
      }
    };
  }, []);

  const speaking = state === "SPEAKING" || activeTools > 0;
  const thinking = state === "THINKING" || state === "PROCESSING";
  const listening = state === "LISTENING";

  return (
    <div
      ref={rootRef}
      className={cn(
        "aze-reactor",
        speaking && "aze-reactor--speaking",
        thinking && "aze-reactor--thinking",
        listening && "aze-reactor--listening",
        muted && "aze-reactor--muted",
        error && "aze-reactor--error",
        suspended && "aze-reactor--suspended",
      )}
      role="img"
      aria-label="J.A.R.V.I.S. core"
      onPointerMove={(event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        const px = (event.clientX - rect.left) / rect.width;
        const py = (event.clientY - rect.top) / rect.height;
        tiltTarget.current = {
          x: (0.5 - py) * 7,
          y: (px - 0.5) * 9,
        };
        event.currentTarget.style.setProperty(
          "--reactor-mx", `${(px * 100).toFixed(1)}%`);
        event.currentTarget.style.setProperty(
          "--reactor-my", `${(py * 100).toFixed(1)}%`);
      }}
      onPointerLeave={() => {
        tiltTarget.current = { x: 0, y: 0 };
      }}
    >
      <div className="aze-reactor-ambient" aria-hidden="true" />
      <div className="aze-reactor-stars" aria-hidden="true">
        {stars.map((star, index) => (
          <span
            key={index}
            className={cn(
              "aze-reactor-star",
              star.blue && "aze-reactor-star--blue",
            )}
            style={{
              left: `${star.left}%`,
              top: `${star.top}%`,
              width: `${star.size}px`,
              height: `${star.size}px`,
              animationDelay: `${star.delay}s`,
            }}
          />
        ))}
      </div>
      {failed ? (
        <div className="aze-reactor-art">
          <div className="aze-reactor-art-fallback" aria-hidden="true" />
        </div>
      ) : (
        <div ref={hostRef} className="aze-reactor-canvas-host" />
      )}
      <div className="aze-reactor-glare" aria-hidden="true" />
    </div>
  );
}

function blueprintGlow(
  material: THREE.MeshStandardMaterial,
  level: number,
  speaking: boolean,
): void {
  material.emissiveIntensity = 0.75 + level * (speaking ? 1.4 : 0.45);
}

export const ArcReactor = memo(ArcReactorBase);
