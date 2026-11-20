# State Viewer

A read-only web page over the SDK's state database (`~/.multistack/state.db`), for the person
creating or managing a platform: see what is deployed, what shape each layer is in, why something
failed, and what happened — after the Python script that did it has finished.

## What it shows today

Only what `multistack/state` records:

| Page | Source table | Shows |
|---|---|---|
| **Deployments** | `deployments` (+ latest `health_checks`) | Every layer/module/capability the SDK deployed: status (`pending` … `provisioned`/`degraded`/`failed`/`deleting`), the error text when it failed, last-updated time, kubeconfig. Click a row for its status history. |
| **Activity** | `health_checks` | Newest-first outcomes across everything (healthy / degraded / unhealthy). |

It refreshes itself (default every 5 s) so a script running in another terminal shows up live.

**Not shown, because the SDK does not record it yet:** the step-by-step sequence ("starting RKE2",
"joining node", "pod ready"), pods and nodes, and anything about a component after it was deleted
(a successful delete removes its row). Those need an append-only `events` table in
`multistack/state` (the viewer is built so adding it is a new page + endpoint, not a rewrite).

## Run it

The built page ships **inside the SDK package**, so on any machine with the SDK installed there is
nothing to build and no Node needed:

```bash
pip install multistack-sdk
multistack-state-viewer                              # or: python -m multistack.state.viewer
multistack-state-viewer --db ./state.db --port 8765
#    -> http://127.0.0.1:8765
```

The database path resolves the same way the backends do: `--db`, else `$MULTISTACK_STATE_DB`, else
`~/.multistack/state.db`. If the SDK runs on another machine, run the viewer there (and tunnel the
port), or copy `state.db` (plus `state.db-wal` / `state.db-shm` if present) and pass `--db`.

It only reads the file. If the folder is not writable (read-only mount, another user's directory) it
reads a private copy instead, and a file that is not an SDK state database is reported as such rather
than failing obscurely.

### Safety defaults

* Binds to `127.0.0.1` only. It has **no authentication**; a non-loopback `--host` prints a warning.
* Requests whose `Host` header is not a loopback name are refused (blocks DNS-rebinding from a web
  page on another site).
* Every non-GET request is answered `405`; the database is opened with SQLite `mode=ro`.
* A port that is already in use is reported (exit code 2) instead of being shared.

## Develop it

```bash
python -m multistack.state.viewer --port 8765        # the API
cd ui/state-viewer && npm run dev                    # http://localhost:5176, proxies /api to :8765
npm test                                             # vitest (API faked with fetch)
```

Set `STATE_VIEWER_API` to point the dev proxy elsewhere.

### Updating the UI that ships with the SDK

`multistack/state/viewer/web/` is the built page, committed so `pip install` works without Node. After
changing anything under `ui/state-viewer/src`, rebuild it and commit the result:

```bash
cd ui && npm run build:sdk -w state-viewer
```

`tests/test_state_viewer_portability.py` fails if the shipped build is missing or refers to files that
are not there.

## Notes

* The database is in WAL mode, so reading while an SDK script writes is safe and always sees the
  latest commit.
* Standard library only on the Python side — the SDK's one hard dependency stays `pydantic`.
* Supports Python >= 3.10 (the SDK's minimum).
