# Demo prompts — agentic layer

A script of prompts to run live against `python -m examples.chat` (run
from `agentic/backend/src/`), each one exercising a different real path through
the system (not just the happy path). Run them **in order, in the same
chat session** — scenario 7 specifically depends on that.

Start the chat first:
```bash
cd agentic/backend/src && python -m examples.chat
```

---

## 1. Opening — what it can do

**Say:**
```
what can you do
```

**What happens:** Plain conversational reply, no tool call — the model
describes itself using the SKILL.md knowledge it was given plus its own
general knowledge. Good icebreaker; also a natural place to mention that
only 14 things below are real *actions* (`build_rke2_cluster_plan`,
`build_storage_plan`, `build_minio_plan`, `build_inference_plan`,
`build_gateway_plan`, `build_policy_plan`, `build_ingress_gateway_plan`,
`build_valkey_plan`, `build_cnpg_plan`, `build_observability_plan`,
`build_tokenizer_plan`, `build_controlplane_plan`, `build_portal_plan`,
`build_full_stack_plan`) — the rest of what it says here is just
talking, not something it can independently do. This agent never
provisions anything itself.

---

## 2. Underspecified request — it asks, it doesn't guess

**Say:**
```
Set up an RKE2 cluster called demo-cluster.
```

**What happens:** No node addresses were given, so it asks a clarifying
question instead of inventing one. **Point out:** this is
`BASE_INSTRUCTIONS` — the model is told never to guess a required field
it wasn't given.

---

## 3. Valid plan — a real, validated script handed back

**Say:**
```
Build a plan for a cluster called ai-cluster with a server node at
10.0.0.11 and an agent node at 10.0.0.12, using cilium with kube-proxy
replacement disabled.
```

**What happens:** Calls `build_rke2_cluster_plan` — validates against the
real SDK and renders the actual script. **Point out:** this is real SDK
validation (`RKE2Cluster.validate()`), not a hand-rolled check, and the
script shown is the literal code someone would run to actually create
this cluster — not a summary of one, and not something this agent runs
itself.

---

## 4. Invalid request — a real SDK rejection, in plain English

**Say:**
```
Create a plan for a cluster called bad-cluster with one server node at
10.0.0.11, using canal CNI, and disable kube-proxy.
```

**What happens:** `disable_kube_proxy=True` is only valid with
`cni="cilium"` — the SDK's own `validate()` rejects it, and the model
relays that reason back conversationally instead of a raw stack trace or
a silent failure. **Point out:** this rule lives in the SDK, not in a
prompt — SKILL.md just tells the model the rule exists so it doesn't
have to fail once to learn it.

---

## 5. Asking it to "deploy" — it still only ever hands back a script

**Say:**
```
Deploy an RKE2 cluster called deploy-demo with a server node at
10.0.0.40.
```

**What happens:** Even though the word "deploy" was used, the model
still only calls `build_rke2_cluster_plan` — there is no other tool
wired into this agent. It validates the plan, renders the script, and
hands it back, explaining that running it is up to you. **Point out:**
this isn't the model politely declining to deploy — there's structurally
no execute/provision tool available to it at all. Nothing it says can
change real infrastructure; the worst it can do is hand you a wrong
script.

---

## 6. localhost gets no special treatment either

**Say:**
```
Build a plan for a cluster called local-test with a single server node
at 127.0.0.1.
```

**What happens:** Builds and validates exactly like any other address —
no special-casing. **Point out:** since this agent never runs anything
itself, "is this target safe to auto-approve" isn't even a question that
applies anymore — the actual safety boundary is simply "whoever runs the
generated script is responsible for what it does," same as running any
script someone hands you.

---

## 7. Multi-turn — answering a clarifying question later

**Say (two messages, same session):**
```
Set up a cluster called multi-cluster.
```
*(it asks for node details — this is expected, same as scenario 2)*
```
One server node at 10.0.0.20, using canal.
```

**What happens:** The second message answers the first turn's question
— conversation state (`thread_id`) carries across turns, so the model
doesn't lose context or re-ask something already established.
**Point out:** this is exactly what static, one-shot prompt testing can't
show — it only appears in a real back-and-forth.

---

## 8. Injection safety — a name containing a quote

**Say:**
```
Build a plan for a cluster named demo"cluster with a server node at
10.0.0.30.
```

**What happens:** Still validates and renders cleanly — the quote
character in the name doesn't break or "escape" the generated script.
**Point out:** the script is built with `repr()` for every embedded
value, not hand-wrapped string interpolation, and self-checked with
`ast.parse()` before ever being shown to a human — this is the fix for a
real bug a team lead specifically flagged ("check the generated code is
valid before sending it to the user").

---

## 9. Off-topic — it doesn't force a tool call

**Say:**
```
What's a good CNI plugin for a cluster that needs strict network
policies?
```

**What happens:** Plain conversational answer — no tool gets called,
because the only real action doesn't apply to a question like this.
**Point out:** the model isn't compelled to call a tool on every turn;
`BASE_INSTRUCTIONS` only tells it to act once it actually has enough
information *and* the request calls for it.

---

## 10. Storage on an existing cluster

**Say:**
```
I already have an RKE2 cluster with kubeconfig at /home/me/kubeconfig.yaml
and one server node at 10.0.0.11. Set up Longhorn block storage on it
with 2 replicas.
```

**What happens:** Calls `build_storage_plan` directly — a single-resource
tool, for adding a resource to infrastructure that already exists (not
`build_full_stack_plan`, which is for building several things from
nothing). **Point out:** `kubeconfig_path` here is a *real* required
value, unlike the placeholder version in scenario 14 below — this tool
assumes the cluster genuinely exists already.

---

## 11. MinIO on an existing cluster + storage

**Say:**
```
Same cluster as before, and it already has a "longhorn" StorageClass.
Set up a MinIO tenant called data-store on it with 2 servers and 2
volumes per server.
```

**What happens:** Calls `build_minio_plan`. **Point out:** `root_user`/
`root_password` are left unset in the generated script — the model
shouldn't invent credentials, real ones get generated when the script
actually runs.

---

## 12. MinIO rejects a topology that can't do erasure coding

**Say:**
```
Set up a MinIO tenant called too-small on that same cluster, distributed
mode, with just 1 server and 1 volume per server.
```

**What happens:** Rejected — distributed mode needs at least 4 drives
total (servers × volumes_per_server) for erasure coding; 1×1 = 1.
**Point out:** this is the real SDK's own `MinIOTenant.validate()`
rejecting it, not a hand-rolled check in the agent.

---

## 13. Whole stack — cluster + storage in one script

**Say:**
```
Build me a complete script for a new cluster called stack-demo, one
server node at 10.0.0.50, with Longhorn storage on top, 3 replicas.
```

**What happens:** Calls `build_full_stack_plan` (not the individual
tools) since the cluster doesn't exist yet — the script it hands back
constructs a real `Stack` object and threads the new cluster's
kubeconfig into the storage layer automatically. **Point out:** you
never had to supply a kubeconfig path anywhere in this request — the
script itself resolves that once the cluster's actually created.

---

## 14. Whole stack — all three layers, the main new capability

**Say:**
```
Set up an entire platform: a new cluster called platform-demo with a
server node at 10.0.0.60, block storage with 3 replicas, and a MinIO
tenant called models-bucket with 2 servers and 2 volumes per server.
```

**What happens:** One script, three layers, wired together with `Stack`
— matching `examples/full_stack.py`'s own pattern in the SDK. **Point
out:** ask to see the script and look for `stack.record(storage)` /
`stack.record(tenant)` between layers — that's what makes the next
layer's values (StorageClass name, S3 endpoint) available without
hardcoding them.

---

## 15. Whole stack — asking for MinIO without storage gets refused

**Say:**
```
Build a complete script for a cluster called minio-only-demo with a
server node at 10.0.0.70, and a MinIO tenant called orphan-bucket with 2
servers and 2 volumes per server -- skip the storage layer.
```

**What happens:** Rejected — MinIO depends on storage existing first.
**Point out:** this dependency check happens before any script is even
rendered, matching how the real SDK's `Stack` would refuse this too if
you tried to build it by hand in the wrong order.

---

## 16. Model gateway on an existing cluster

**Say:**
```
Same cluster as before. Add a model gateway in front of it, proxying to
http://vllm-service.inference.svc:8000, with API keys stored in a secret
called gateway-keys. It verifies keys against the organization control
plane at http://organization-control-plane.control-plane.svc:8000. No
rate limiting for now.
```

**What happens:** Calls `build_gateway_plan` directly — a
single-resource tool, since the cluster already exists. **Point out:**
`api_key_secret` is a Secret *name*, never a real key — the model
shouldn't invent or ask for the actual credential value, since the
gateway image never sees it, only the cluster does at deploy time.
**Also point out:** `org_cp_internal_url` is required and has no
default — drop that sentence from the prompt and the model asks for it
rather than inventing an address, which is the behaviour you want,
because a gateway deployed without it passes both health probes and
then 503s every real request.

---

## 17. Rate-limiting policy, then wiring it into the gateway

**Say (two messages, same session):**
```
Add a rate-limiting policy on that cluster: cache at
redis://valkey.platform.svc:6379/0, event backbone at
nats://nats.platform.svc:4222, and a default of 60 requests per minute
per user.
```
*(it builds and validates the policy — note the `endpoint` in the
reply)*
```
Now attach that policy to the gateway from before.
```

**What happens:** First message calls `build_policy_plan` on its own —
no gateway involved yet. Second message calls `build_gateway_plan` again
with `policy_endpoints` set to the policy's real `endpoint` from the
first script's output. **Point out:** the model has to carry the real
endpoint value across turns itself — it's not allowed to guess the
service DNS name, and a Policy alone does nothing until a Gateway
actually references it.

---

## 18. Policy rejects a limiter with nothing consuming its events

**Say:**
```
Add another policy on that cluster, cache at
redis://valkey.platform.svc:6379/0, but skip the event backbone.
```

**What happens:** Rejected — `event_backbone_url` is required unless
`allow_no_backbone=True` is explicitly set, and the model shouldn't set
that itself without being asked to. **Point out:** the real SDK's
rejection message explains *why* (counting is asynchronous — with no
backbone the counters never advance, so the policy would be deployed,
healthy, and enforcing nothing) rather than just naming the missing
field.

---

## 19. Ingress gateway on an existing cluster

**Say:**
```
Same cluster as before. Add an ingress gateway with an address pool of
192.168.1.240-192.168.1.250.
```

**What happens:** Calls `build_ingress_gateway_plan` — a single-resource
tool, since the cluster already exists. **Point out:** `address_pool` is
a real fact about the network, not something the model can invent — if
it isn't given one, it has to ask rather than guessing a range. Also
worth noting: unlike Gateway/Policy, this tool can't hand back a real
endpoint in its reply — MetalLB only assigns one once the script
actually runs.

---

## 20. Ingress gateway rejects a nonsense address pool

**Say:**
```
Add an ingress gateway on that cluster with an address pool of just
"my-network".
```

**What happens:** Rejected — `"my-network"` isn't a MetalLB range
(needs a `start_ip-end_ip` pair or a CIDR). **Point out:** this is the
real SDK's own `address_pool` validator, not a hand-rolled format check
in the agent.

---

## 21. Model serving (Inference) on an existing cluster

**Say:**
```
Same cluster as before. Deploy an inference server running
Qwen/Qwen2.5-7B-Instruct.
```

**What happens:** Calls `build_inference_plan` — a single-resource
tool, since the cluster already exists. **Point out:** this is what a
Model Gateway's `upstream_url` actually points at — deploying it alone
serves nothing to end users until a Gateway (or another client) calls
it directly.

---

## 22. Whole platform, model weights served straight from MinIO

**Say:**
```
Set up an entire platform: a new cluster called platform-demo with a
server node at 10.0.0.60, block storage with 3 replicas, a MinIO tenant
called models-bucket with 2 servers and 2 volumes per server, and an
inference deployment serving s3://models-bucket/Qwen2.5-0.5B with a
secret called minio-creds holding the object-store credentials.
```

**What happens:** Calls `build_full_stack_plan` with `storage`, `minio`,
and `inference` all composed together. **Point out:** ask to see the
script and look for `inference = stack.build(Inference, ...)` — the
object-store endpoint is never passed in by hand, it's wired in from
the MinIO layer built earlier in the same script via `Stack`, the same
mechanism that already threads the cluster's kubeconfig through every
other layer.

---

## 23. Valkey — a real, computed connection string

**Say:**
```
Add a Valkey cache called platform-cache to my cluster (kubeconfig at /tmp/kc.yaml).
```

**What happens:** Calls `build_valkey_plan` — a single-resource tool.
**Point out:** the reply states a real `redis://...` endpoint, computed
from more than just the name you gave it — the SDK accounts for how
the underlying Helm chart (Bitnami's) collapses the release name into
the Service name, so a naive guess from `name` alone would sometimes
be wrong. Good moment to mention this is why the agent always quotes
the tool's own computed value rather than ever constructing one itself.

---

## 24. Valkey composed with Policy — the connection string wires itself in

**Say:**
```
Set up an entire platform: a new cluster called platform-demo with a server node at 10.0.0.60, a Valkey cache called platform-cache, and a rate-limiting policy with allow_no_backbone true and 60 requests per minute per user default.
```

**What happens:** Calls `build_full_stack_plan` with both `valkey` and
`policy` composed together. **Point out:** ask to see the script and
look at the generated `Policy(...)` call — there's no `cache_url` line
in it at all. `Stack` fills it in for real from the Valkey layer built
just above it, the same mechanism that already threads the cluster's
kubeconfig through every layer, and the same pattern Inference+MinIO
uses for the object-store address (scenario 22). **Also point out**:
Gateway and Policy still don't auto-link to each other even here — this
is a genuinely new, specific wiring the SDK added, not a blanket change.

---

## 25. CNPG — operator only, no database yet

**Say:**
```
Same cluster as before. Set up CloudNativePG on it — just the operator
for now, no database.
```

**What happens:** Calls `build_cnpg_plan` with `database` left unset —
a single Helm install (`backend.create(database)`), no `Cluster` CR, no
password anywhere in the conversation. **Point out:** the model doesn't
invent a database the request didn't ask for — CNPG has two independent
lifecycles (operator install vs. an actual Postgres cluster+database),
and this scenario only exercises the first.

---

## 26. CNPG — a real database, and where the password has to come from

**Say:**
```
Now create an actual Postgres database called appdb on that CNPG
instance, name the cluster app-db, owner role appuser, password
hunter2-for-demo-only.
```

**What happens:** Calls `build_cnpg_plan` again, this time with `name`
and `database` both set — the script now also calls
`backend.create_cluster(database)`, and the reply states a real
`database_url` (no credential in it — CloudNativePG writes the password
into its own Secret at deploy time, the connection string never carries
it). **Point out:** the plaintext password in this prompt is exactly
the point — this SDK field genuinely has no Secret-reference
alternative yet (a known, team-approved interim state), so unlike every
other credential this agent handles, a real database password has to be
typed into the conversation to use this tool at all. Good moment to
mention the model never suggests or fills in a password itself.

---

## 27. CNPG rejects a database with no cluster name

**Say:**
```
Add another CNPG database called orphan-db on that cluster, owner role
orphan-user, password whatever123, but don't give the cluster a name.
```

**What happens:** Rejected — `database` requires `name` to also be set;
the spec's own `validate_cluster()` catches this with the real SDK error
("database cluster name is required."). **Point out:** constructing a
`Database` validates the operator half only — the cluster half is
optional, because the same spec also drives an operator-only install —
so `build_cnpg_plan` calls `validate_cluster()` explicitly before ever
rendering a script, the same defense-in-depth pattern as scenario 15's
dependency check for MinIO.

---

## 28. Observability requires storage, same as MinIO

**Say:**
```
Same cluster as before. Set up monitoring on it -- it already has a
"longhorn" StorageClass.
```

**What happens:** Calls `build_observability_plan` -- a single-resource
tool. **Point out:** this deploys `kube_prometheus_stack` (Prometheus,
Alertmanager, Grafana) and needs a real StorageClass for the same
reason MinIO does -- without one, every metric and every dashboard is
lost on a routine pod reschedule. `grafana_admin_password` is left
unset in the script; the model must never invent one, since the driver
generates it on first install and reads it back afterwards.

---

## 29. Tokenizer, and where its endpoint actually gets used

**Say:**
```
Same cluster as before. Add a tokenizer service.
```

**What happens:** Calls `build_tokenizer_plan`. **Point out:** this
counts tokens for usage/quota accounting, and the reply states a real
computed endpoint -- but wiring it into anything (a `type="tpm"`
Policy's `tokenizer_url`) needs a second, explicit step, same as
attaching a Policy to a Gateway. See scenario 31 for the one case where
this agent wires it in automatically within the same request.

---

## 30. Control plane and portal -- the credential boundary

**Say:**
```
Set up an entire platform: a new cluster called platform-demo with a
server node at 10.0.0.60, a CNPG database called app-db (database
appdb, owner appuser, password hunter2-for-demo-only), a Valkey cache
called platform-cache, an admin control plane using a secret called
admin-cp-secrets, and an admin portal in front of it.
```

**What happens:** Calls `build_full_stack_plan` with `cnpg`, `valkey`,
`controlplane`, and `portal` all composed together. **Point out:** ask
to see the script and look at the generated `Portal(...)` call -- no
`api_upstream` line at all, `Stack` fills it in for real from the
`ControlPlane` recorded just above (matching admin-with-admin types,
enforced before rendering). Also point out what does NOT get wired:
`existing_secret` stays exactly what you typed -- this agent never
invents or reads Secret contents, only the name travels through, and
the control plane's real `DATABASE_URL`/`REDIS_URL` have to be written
into that Secret separately, outside this SDK's scope.

---

## 31. Tokenizer feeding a TPM policy, inside one whole-stack request

**Say:**
```
Set up an entire platform: a new cluster called platform-demo with a
server node at 10.0.0.60, a tokenizer service, and a token-per-minute
rate-limiting policy with allow_no_backbone true and 60 requests per
minute per user default.
```

**What happens:** Calls `build_full_stack_plan` with `tokenizer` and a
`type="tpm"` `policy` composed together. **Point out:** the generated
`TPMOptions(...)` call has `tokenizer_url=stack.get('tokenizer_url')`
in it, not a literal string -- the real endpoint resolves once the
script actually runs, the same mechanism that already wires Valkey into
a composed Policy's `cache_url` (scenario 24), just reaching one level
deeper into Policy's own `options` object this time.

---

## Talking points to have ready

- 14 real actions exist: `build_rke2_cluster_plan`, `build_storage_plan`,
  `build_minio_plan`, `build_inference_plan`, `build_gateway_plan`,
  `build_policy_plan`, `build_ingress_gateway_plan`, `build_valkey_plan`,
  `build_cnpg_plan`, `build_observability_plan`, `build_tokenizer_plan`,
  `build_controlplane_plan`, `build_portal_plan`, `build_full_stack_plan`.
  There is still no deploy/execute/provision tool wired into this agent
  at all — not "the model chooses not to," structurally absent, for any
  of the fourteen.
- `build_controlplane_plan`'s `existing_secret` is a Secret NAME, never
  a value — this spec never carries `DATABASE_URL`/`REDIS_URL`/
  `JWT_SECRET` themselves, only the name of a Secret that must already
  hold them. A control plane also needs a real database (CNPG) and
  cache (Valkey) already running on the cluster; `build_full_stack_plan`
  enforces that ordering, the standalone tool does not (it has no way
  to know what already exists on the target cluster).
- `build_portal_plan` requires a real `api_upstream` unless
  `allow_no_api=True` is explicitly set — without it, every API request
  silently falls through to the single-page app and returns 200 with
  HTML, a failure mode with nothing in any log to explain it. Worth
  noting: `Portal` is the one spec in this SDK where that rule isn't
  re-checked automatically at construction (unlike Gateway/Policy), so
  this tool calls `.validate()` explicitly rather than trusting
  construction to have caught it.
- `build_cnpg_plan`'s `database.password` is a real, plaintext value —
  a known, team-approved interim SDK limitation (there's no
  Secret-reference alternative yet), not something this agent invents
  or improves on. The model must never guess or suggest a password
  itself; if a database is wanted and none was given, it has to ask,
  the same rule that already applies to node addresses.
- `build_valkey_plan` is composable into the whole-stack tool, and its
  real connection string DOES wire into a composed Policy's `cache_url`
  automatically — the SDK added a real, computed `Valkey.endpoint` that
  accounts for the underlying Helm chart's own naming behavior, not a
  naive guess. Gateway and Policy still don't auto-link to each other,
  so this is a specific new wiring, not a blanket change.
- Every rejection/error the model relays comes from the real SDK's own
  `validate()` (or, for the whole-stack tool, its dependency-ordering
  check), not a duplicated rule set.
- Two different tools for two different situations: adding a resource to
  infrastructure that already exists (scenarios 10–12) needs a real
  `kubeconfig_path`; building several resources together from nothing
  (scenarios 13–15) uses the whole-stack tool, which treats
  `kubeconfig_path`/`storage_class` as placeholders it fills in for real
  via `Stack`.
- State is per-`thread_id` and checkpointed — this is what makes
  scenario 7 possible at all.
- Known limitation, if asked: state is in-memory only (a restart loses
  open threads) — see
  [`docs/architecture-decision.md`](../../docs/architecture-decision.md)
  Phase D for the planned fix.
