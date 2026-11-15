# Technical Baseline and Adaptation Findings

Updated: 2026-11-15. These read-only findings describe useful behavior and required
adaptations. They contain no internal reference repository names, source checkout
paths, deployment identities or credentials. Actual implementation results are recorded separately in [verification](verification.md).

## 1. Adaptation map

| Area | Useful baseline | Required adaptation |
|---|---|---|
| Extraction | Strict block/link selection, info generation, durable original-block backlog | Standalone server contracts/jobs without client databases/imports |
| Channels | Independent schema/store routing and existing policy/economy definitions | Enable both existing domain schemas at first startup; auto-created channels bind a predefined generic schema |
| Schema review | Original blocks, schema fingerprints, usage and rejection evidence | Strict fixed vocabulary plus periodic manual calibration and explicit revision activation |
| Graph | Per-block named graphs, durable results, superseding, one owner | Write-intent recovery, authorized query views, progress and provenance |
| Viewer | Entity search, graph/path exploration, source evidence and ECharts | Local shell, live server data and dynamic channel discovery |
| Material | DOM evidence/segments, metadata/link maps, tables and coarse groups | Adapt projection to the current browser without retaining another driver |
| Browser | Managed assets, persistent identities, leases, unknown-outcome errors | Backend ownership, separate OSBI state, human wait/resume and events |
| Reuse | Independent stage caches and database-backed asynchronous reuse patterns | Durable concurrency, exact semantic identities and additional observations |
| Authentication | Header middleware and key lifecycle concepts | Opaque expiring keys, revocation/grants and verified HTTPS |
| Settings | Grouped forms, save/reset, validation and feedback | Supported local config, masked keys, no cloud account flow |
| Notifications | History/live messages, filters, connection state and human prompts | Local durable cursor/event stream and validated run actions |
| Hunters/reports/curation | Longer-term behavior and design constraints | Deferred without dependencies from the three demo pages |

## 2. Findings affecting delivery

1. Existing boundaries still assume same-machine database/cache/module access.
   Split transport contracts before separating the server and client deployments.
2. Existing multi-channel configuration does not implement automatic discovery.
   Match/no-match routing and atomic creation are new. Both domain definitions
   exist, but the inspected startup registry enables only one; OSBI-Net must enable
   both from its first deployment. New channels bind generic vocabulary without
   automatically introducing entity types or predicates.
3. Current browser ARIA/action snapshots are not the complete material format;
   adapt structured DOM evidence and grouping to the managed runtime.
4. Retain durable graph queuing but replace a fixed nightly schedule with continuous
   default processing and a configurable common daily window.
5. Stage cache hits alone do not guarantee durable reuse of concurrent requests.
   Preserve observations and per-request identifiers under canonical work reuse.
6. Reversible user-ID encoding is not signed authentication. Replace that scheme
   rather than inheriting function names implying signed tokens.
7. The relevant settings surface is the application configuration form. Account
   membership, balance, password, phone binding and recharge flows are excluded.
8. Notification history/live events/human prompts are useful existing distinctions.
   Keep the behavior without old remote identity bootstrap/account dependencies.
9. The inspected frontend uses React/Next.js with shared account stores. Three
   backend-served pages can reuse its UX without transplanting that full stack.
10. Client optional AI and server model credentials remain separate. No Jev key is
    required to acquire material or extract/query a graph in the demo.
11. Name-only entity identities may conflate people. Full curation is deferred;
    show ambiguity honestly and avoid claiming inferred identity as verified fact.

## 3. Initial domain schemas

The first deployment must ship the existing policy schema v3.2 and economy schema
v1.5. These are domain-specific starting definitions, not aliases for the generic
schema. Preserve their semantic triples, type hierarchy, predicates, labels,
domain/range rules and required controlled-tag definitions. Review/calibration
later produces an explicit revision rather than silently changing these baselines.

| Channel | Existing revision | Inspected input SHA-256 |
|---|---|---|
| `policy` | v3.2 | `1011e38ac693c1634e1b115b8f8b6a3ca311d30be665cf0fdfb9e0ddc2c3d0e1` |
| `economy` | v1.5 | `b23c052867c3ac0a43ee5b7065e8fb06e7c5df4a9d282f90aa478391bae5b532` |

These hashes identify the inspected inputs without exposing internal checkout
paths. English comment cleanup during migration can change file bytes; verify RDF
semantic equivalence separately, without renaming classes/predicates or changing
labels/rules as an incidental part of migration. Confirm that both packaged
channels are enabled and usable before server readiness.

The simple generic schema is a separate predefined vocabulary for new channels.
Its later revisions require the same evidence-based review, validation and explicit
activation as domain schemas; no automatic vocabulary expansion is allowed.

## 4. Browser and DSH host baseline

The inspected managed runtime pins Camoufox SDK `0.5.9`, `fpgen-js` `1.3.0`,
`playwright-core` `1.62.1` and browser `158.0.1-beta.41`. Preserve paired assets,
integrity checks and isolated profiles instead of choosing versions independently.

The inspected public DSH commit is `39163dd30f703a79079e2d2446545cff3c453738`, root
package `0.2.2-rc.3`, requiring Node `>=24.9.0`. The pinned CLI and OSBI MCP overlay have now passed local startup/configuration
composition checks. Provider-backed conversational acceptance remains separate.

## 5. Jev research

TypeSafe's Jev answers typed questions over state. Choice selects listed options,
Score applies a rubric, and Noul gives a yes/no probability. See the
[official introduction](https://docs.typesafe.ai/introduction). Narrow local
workflow judgments are a potential use, not a graph extraction prerequisite.

Closed-set choice does not establish that any option actually fits. The
[companion Noul pattern](https://docs.typesafe.ai/patterns/companion-noul) separates
fit from choice. This distinction also informs the server router, without moving
Jev onto the server or making client judgments authoritative there.

The [quick start](https://docs.typesafe.ai/introduction/quickstart) and
[Choice API](https://docs.typesafe.ai/primitives/choice) describe a dedicated typed
state/questions/answers contract. Configurable base URL does not imply chat API
compatibility. Measure workload cost/latency/accuracy rather than inheriting
marketing claims. Optional integration remains off the demo dependency path.

## 6. Other public sources

- [DSH skills](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/docs/subsystems/skills.md): discovery roots and skill identity.
- [DSH MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/packages/mcp/mcp-client/README.md): transports and probe startup behavior.
- [DSH manifest](https://github.com/deepseek-ai/deepseek-harness/blob/39163dd30f703a79079e2d2446545cff3c453738/package.json): inspected version and Node requirements.
- [Oxigraph store](https://pyoxigraph.readthedocs.io/en/stable/store.html): transactions and backup, distinct from cross-store application consistency.

Recheck changing dependencies before implementation. Historical source behavior
and deployment records are not OSBI-Net customer acceptance results.
