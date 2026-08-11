# Desktop frontend bundle split

Measured on 2026-08-02 with:

```powershell
cd apps/desktop
pnpm exec vite build
```

## Production output

| Asset | Minified | Gzip | Loading behavior |
|---|---:|---:|---|
| `index-*.js` | 232.48 kB | 75.13 kB | Initial application entry |
| `EchoSphereWebGL-*.js` | 862.94 kB | 232.31 kB | Loaded only when WebGL is available and reduced motion is off |
| `index-*.css` | 20.92 kB | 5.34 kB | Initial styles |
| `index.html` | 0.46 kB | 0.30 kB | Initial document |

Before the split, the JavaScript entry was 1,105.44 kB minified / 310.56 kB gzip.
The initial JavaScript entry is now about 79% smaller minified and 76% smaller gzip.
Three.js and React Three Fiber renderer symbols are present only in the deferred
`EchoSphereWebGL-*.js` chunk; the entry chunk contains only the dynamic chunk
reference and the immediate CSS fallback.

The deferred WebGL chunk remains larger than Vite's 500 kB advisory threshold.
The threshold was not raised or disabled: this is visible in production builds
and can be optimized independently without putting Three.js back into the entry.