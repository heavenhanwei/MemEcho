import { Canvas, useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import type { EchoSphereVisualProps } from "./EchoSphere";

const vertexShader = `
  uniform float uTime;
  uniform float uEnergy;
  varying vec3 vNormal;
  varying vec3 vPosition;
  float hash(vec3 p) { return fract(sin(dot(p, vec3(127.1,311.7,74.7))) * 43758.5453); }
  float noise(vec3 p) {
    vec3 i=floor(p), f=fract(p); f=f*f*(3.0-2.0*f);
    return mix(mix(mix(hash(i),hash(i+vec3(1,0,0)),f.x),
                   mix(hash(i+vec3(0,1,0)),hash(i+vec3(1,1,0)),f.x),f.y),
               mix(mix(hash(i+vec3(0,0,1)),hash(i+vec3(1,0,1)),f.x),
                   mix(hash(i+vec3(0,1,1)),hash(i+vec3(1,1,1)),f.x),f.y),f.z);
  }
  void main() {
    vNormal = normal;
    float n = noise(position * 1.45 + vec3(uTime * .11, -uTime * .08, uTime * .06));
    float wave = sin(position.y * 4.0 + uTime * 1.15) * .035;
    vec3 moved = position + normal * ((n - .5) * .22 + wave + uEnergy * .16);
    vPosition = moved;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(moved, 1.0);
  }
`;

const fragmentShader = `
  uniform float uTime;
  uniform vec3 uColorA;
  uniform vec3 uColorB;
  varying vec3 vNormal;
  varying vec3 vPosition;
  void main() {
    float fresnel = pow(1.0 - abs(dot(normalize(vNormal), vec3(0.0,0.0,1.0))), 2.1);
    float flow = sin(vPosition.y * 3.2 + vPosition.x * 2.1 + uTime * .5) * .5 + .5;
    vec3 color = mix(uColorA, uColorB, flow);
    float alpha = .56 + fresnel * .3;
    gl_FragColor = vec4(color + fresnel * .22, alpha);
  }
`;

function SphereMesh({ state, energy }: EchoSphereVisualProps) {
  const group = useRef<THREE.Group>(null);
  const mesh = useRef<THREE.Mesh>(null);
  const material = useRef<THREE.ShaderMaterial>(null);
  const uniforms = useMemo(
    () => ({
      uTime: { value: 0 },
      uEnergy: { value: 0 },
      uColorA: { value: new THREE.Color("#725ed7") },
      uColorB: { value: new THREE.Color("#b5d9d0") },
    }),
    [],
  );

  useFrame((clock, delta) => {
    if (!mesh.current || !material.current || !group.current) return;
    const elapsed = clock.clock.elapsedTime;
    uniforms.uTime.value = elapsed;
    uniforms.uEnergy.value = THREE.MathUtils.lerp(
      uniforms.uEnergy.value,
      state === "recording"
        ? Math.max(energy, 0.12)
        : state === "processing"
          ? 0.3
          : 0.04,
      0.08,
    );
    mesh.current.rotation.y += delta * (state === "processing" ? 0.22 : 0.06);
    const target = state === "paused" ? 0.91 : state === "responding" ? 1.06 : 1;
    mesh.current.scale.setScalar(
      THREE.MathUtils.lerp(mesh.current.scale.x, target, 0.04),
    );
    const floatSpeed = state === "paused" ? 0.3 : 1.1;
    group.current.position.y = Math.sin(elapsed * floatSpeed) * 0.055;
    group.current.rotation.z = Math.sin(elapsed * floatSpeed * 0.7) * 0.025;
  });

  return (
    <group ref={group}>
      <mesh ref={mesh}>
        <icosahedronGeometry args={[1.42, 32]} />
        <shaderMaterial
          ref={material}
          args={[{ uniforms, vertexShader, fragmentShader, transparent: true }]}
          depthWrite={false}
        />
      </mesh>
    </group>
  );
}

function MemoryPoints() {
  const points = useMemo(
    () => [
      [-1.05, 0.62, 0.8],
      [0.88, 0.76, 0.9],
      [0.56, -1.05, 0.78],
      [-0.74, -0.86, 0.9],
      [0.1, 1.3, 0.5],
    ],
    [],
  );

  return (
    <>
      {points.map((position, index) => (
        <mesh key={index} position={position as [number, number, number]}>
          <sphereGeometry args={[0.055, 16, 16]} />
          <meshBasicMaterial color={index % 2 ? "#f1b49f" : "#fff7dc"} />
        </mesh>
      ))}
    </>
  );
}

export function EchoSphereWebGL({ state, energy }: EchoSphereVisualProps) {
  return (
    <div
      className="sphere-webgl"
      data-testid="sphere-webgl"
      aria-label="memEcho WebGL 活动球体"
    >
      <Canvas
        camera={{ position: [0, 0, 4.6], fov: 42 }}
        dpr={[1, 1.7]}
        gl={{ alpha: true, antialias: true }}
        fallback={<div className="sphere-fallback" aria-label="memEcho 活动球体" />}
      >
        <ambientLight intensity={1.4} />
        <pointLight position={[2, 2, 3]} intensity={4} color="#ffffff" />
        <pointLight position={[-3, -1, 2]} intensity={2.5} color="#9d8df1" />
        <SphereMesh state={state} energy={energy} />
        {state === "memory" && <MemoryPoints />}
      </Canvas>
    </div>
  );
}
