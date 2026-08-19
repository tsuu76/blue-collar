# CardSwap widget

Isolated React island that renders the official [React Bits](https://reactbits.dev)
`CardSwap` component into the existing Flask/Jinja dashboard's READY_TO_APPLY
column. This is **not** a frontend for the rest of the app — the dashboard
stays server-rendered Flask/Jinja everywhere else.

- `src/CardSwap.jsx` / `src/CardSwap.css` — the official component, unmodified.
- `src/main.jsx` — glue code (not part of the official component) that reads
  real job data from a `<script type="application/json">` tag the dashboard
  template already renders, and mounts CardSwap into a shadow root (to keep
  its CSS from colliding with the dashboard's own `.card` class).
- `src/card-content.css` — this project's dark-maroon styling for what's
  *inside* each card. Kept separate from the official `CardSwap.css`.

## Rebuilding after an edit

```bash
npm install
npm run build
```

This writes `cardswap-react.js` straight into `../../src/dashboard/static/`
(non-hashed filename, so `dashboard.html` doesn't need to change). Flask
needs no Node process at runtime — only when this widget itself is edited
does anyone need to run this build. Commit the rebuilt
`src/dashboard/static/cardswap-react.js` alongside your source change.

An unused `cardswap-react.css` also gets emitted as a build byproduct (see
the note in `vite.config.js`) — safe to delete, nothing references it.
