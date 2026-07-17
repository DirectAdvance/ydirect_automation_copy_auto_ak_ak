# Direct automation: audit of the current split (2026-07-12)

## Verdict

The service is split at both the **process/operational** and the core
**code/dependency** boundaries. Keep it as a modular monolith; more microservices are
not needed yet.

Verified active on LXC 101:

- `direct-create.service` — web, `direct.main`;
- `direct-create-worker.service` — durable create queue worker, `direct.worker_main`;
- `direct-content.service` — content editor web, `direct.content_main`;
- `direct-content-worker.service` — content queue worker, `direct.content_worker`;
- `direct-copy.service` — copy web plus its own in-memory worker, `direct.copy_main`.

The create queue has the strongest boundary: web posts jobs to PostgreSQL and the worker
claims them. Restarting the web process does not own an in-flight create job. Content has
its own worker. Copy is isolated from create, but its queue is still in the copy web
process and therefore is not restart-durable.

## Implemented module boundaries

The former 8,054-line `blueprint.py` is now a 360-line Flask composition root. It owns
access decorators, the Blueprint object and route registration only. Runtime wiring is
in `automation_runtime.py`; compatibility exports keep old maintenance imports working.

| Module | Ownership |
|---|---|
| `queue_server.py` | in-memory queue state, worker pool, claim/recovery, watchdog, resume and delayed repair |
| `job_repository.py` | PostgreSQL persistence for jobs, deferred work, delayed repairs and ready logins |
| `yandex_gateway.py` | OAuth token selection, v5/v501 transport and cookie/Grid transport |
| `direct_repository.py` | Victory read/read-write connections and DB secret loading |
| `account_service.py` | account prefill/assets/audiences/campaign operations and safe draft deletion |
| `pack_resolver.py` | slepok key resolution, M3/provider health, pack previews and segment counts |
| `automation_runtime.py` | create-set domain wiring shared by web, worker and copy processes; no Blueprint or route registration |
| `blueprint.py` | web composition only |

`content_main`, `content_worker`, `price_check_cron`, `copy_main` and `worker_main` no
longer import `direct.blueprint` or `direct.main`. Copy and worker import the runtime and
their concrete services directly; import tests verify neither web module enters
`sys.modules`.

## Remaining large cohesive modules

Current measured hotspots:

| Module | Lines | Functions/classes | Assessment |
|---|---:|---:|---|
| `automation_runtime.py` | 3,703 | 235 / 0 | compatibility/runtime wiring; next candidate for gradual extraction |
| `blueprint.py` | 360 | 3 / 1 | thin Flask composition root |
| `routes_content_editor.py` | 3,118 | 97 / 0 | large, but mostly one bounded context |
| `campaign_spec_audit.py` | 2,889 | 54 / 0 | cohesive audit/fix domain; split internally only with tests |
| `ai_agents.py` | 2,741 | 80 / 0 | cohesive but oversized AI domain |
| `grid_finalize.py` | 2,515 | 55 / 2 | transport-specific finalization domain |
| `copy_engine.py` | 2,470 | 62 / 0 | cohesive copy domain |

The remaining 3.7k-line runtime is a wiring/facade hotspot, but it no longer owns queue,
job persistence, API/DB gateways, account operations or pack resolution. Its extraction
should continue by bounded create-set capability, not by creating more processes.

## Recommended next split

1. **Create-set facades** — split `automation_runtime.py` by orchestration, content/promo,
   settings/minus rules and create-set module configuration. Remove compatibility exports
   only after maintenance scripts migrate.
2. **Copy durability** — if copy jobs must survive a service restart, move the copy queue to
   PostgreSQL or a dedicated copy worker. If restart loss is acceptable, document that
   explicitly and keep the current simpler process.
3. Split the other 2k–3k line modules only after characterization
   tests exist. Their size alone is less dangerous than cross-process imports from `blueprint`.

## Migration gates

Do one boundary per change. For every extraction: `py_compile`, import all five entrypoints,
route-map tests, queue claim/recovery tests, rendered `/direct/automation` JavaScript check,
then a controlled live dry-run. Do not combine the queue extraction with Yandex gateway work.
