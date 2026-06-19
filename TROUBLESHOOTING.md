# Troubleshooting

This guide covers the most likely production and local failure cases for the WNBA app.

## Quick Triage

Check these first:

1. `GET /api/health`
2. `GET /api/matchups`
3. browser devtools Network tab
4. backend logs
5. frontend domain/proxy bindings

If `health` works but the app is blank, the problem is usually routing, payload shape, or frontend runtime behavior rather than backend startup.

For stale-frontend suspicion, also check:

1. `curl -I https://<domain>/index.html`
2. `curl -I https://<domain>/runtime-config.js`
3. confirm both return `Cache-Control: no-store`
4. confirm `/assets/*` responses come from the current frontend image/build

## Public Routing Problems

### Symptom

- homepage loads but API calls fail
- `/api/*` returns the wrong payload
- app is blank even though containers are healthy

### What To Check

- the public domain is attached to the frontend service only
- frontend nginx is serving `/`
- `/api/*` is being proxied by nginx to the backend
- no stale Coolify/Traefik override is hijacking `/api`

### Strong Signal

If `https://<domain>/api/matchups` returns a response directly from `uvicorn` instead of `nginx`, public routing is wrong.

## Admin Login Works But Data POSTs Fail

### Symptom

- `POST /api/auth/login` succeeds
- `Refresh Covers`, `Recalculate`, `Refresh ESPN`, or similar actions return `403`

### Likely Cause

- proxy-origin mismatch
- CSRF/session validation mismatch through Coolify/Traefik

### What To Check

- browser is using the intended public hostname
- backend sees forwarded host/proto headers
- `GET /api/auth/me` shows an authenticated admin session
- the failing POST includes `X-CSRF-Token`

### Notes

This app now accepts a valid admin session when either:

- forwarded origin matches the public host/proto
- CSRF token matches the session token

If login still works but POSTs fail, inspect the backend log line and response body before changing unrelated app logic.

## Unauthorized On Mutation Routes

### Symptom

- mutation routes return `401 Unauthorized`

### Likely Cause

- no valid admin session
- bad or missing `API_KEY` on manual requests

### What To Check

- sign in again from the `Data` tab
- confirm the request is a browser session-authenticated call
- for curl/manual calls, send:
  - `X-API-Key: <API_KEY>`
  - or `Authorization: Bearer <API_KEY>`

## Covers Refresh Fails

### Symptom

- `Refresh Covers` fails
- cached Covers data loads but live refresh does not

### Likely Cause

- Covers parser drift
- Covers page markup changed
- source host/IP is blocked or challenged
- scrape fallback/runtime dependencies differ by environment

### What To Check

- backend error detail for the Covers route
- whether the response is an auth failure, parser failure, or network failure
- whether the live container can reach Covers successfully

### Important

This is an HTML scrape path, not a stable API integration. Production failures can happen even when local works.

## Odds Import Fails

### Symptom

- `Refresh Odds` fails or reports missing API key

### Likely Cause

- `ODDS_API_KEY` is not set in production

### What To Check

- backend env var exists
- provider key is attached to the active backend resource

## SQLite And Persistence Problems

### Symptom

- data disappears after redeploy
- restored DB does not appear in the app
- app starts but important records are missing

### Likely Cause

- wrong volume mounted
- old DB restored into the wrong container/resource
- ephemeral storage used by mistake

### What To Check

- `/data/wnba.sqlite` exists in the active backend container
- Coolify volume is attached to `/data`
- cache and snapshot directories also point to `/data`
- restored file is the actual runtime DB, not a stale or partial backup

## SQLite Locking Or Concurrency Issues

### Symptom

- admin actions are slow
- imports/recalculate collide
- `database is locked`

### What To Know

- the app uses SQLite
- it now uses `WAL`
- `WAL` helps reads during writes but does not support multi-writer scale

### What To Check

- only one backend instance is running
- multiple admins are not launching heavy write flows simultaneously

### Recommended Direction

- keep one backend instance
- serialize heavy admin jobs if contention becomes common

## Blank Or Incomplete Matchup Panels

### Symptom

- matchup page loads but some sub-panels look empty or odd

### Common Causes

- sparse real source data
- expansion-team matchups with no prior history
- Covers records missing for a specific game

### H2H Specific Note

The H2H display is now intentionally sparse-aware:

- `0` meetings: note only
- `1` meeting: compact summary
- `2+` meetings: full table

So an absent or very small H2H view is not always a bug.

## Roster Tab Behavior

### Current Intended Behavior

- roster data is readable normally
- `Refresh Roster` is admin-only

If the roster tab is missing entirely or the `Data` tab breaks after roster changes, compare the current frontend against the last known-good tab layout before changing backend behavior.

## Deployment Recovery Workflow

When production behavior becomes unclear:

1. save a DB backup
2. save current logs
3. note the live commit hash
4. verify `/api/health`
5. verify `/api/matchups`
6. verify admin login
7. test one protected POST route
8. change one layer at a time

Avoid changing auth, proxy, DB mode, and frontend behavior all at once. That makes root cause hard to isolate.
