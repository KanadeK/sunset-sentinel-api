# Public repository sample scan

Search date: 2026-07-23.

GitHub CLI searches covered the exact project name, exact slug, and combinations of `sunset`,
`deprecation`, `OpenAPI`, and `API lifecycle`. Repository metadata and README files were then
reviewed for the most relevant results. Stars and update dates are a point-in-time snapshot from
GitHub's `updatedAt` field. The overlap estimates compare user-facing MVP capabilities; they do
not claim code similarity.

No public repository was returned for the exact name `Sunset Sentinel API` or exact slug
`sunset-sentinel-api`.

| Repository | Stars | Updated | Status | Main capability | Overlap with Sunset Sentinel API | Estimated overlap |
|---|---:|---|---|---|---|---:|
| [wework/faraday-sunset](https://github.com/wework/faraday-sunset) | 12 | 2023-01-28 | Archived; last code push 2021-04-29 | Ruby Faraday response middleware that reads `Sunset` and emits logger, ActiveSupport, or Rollbar warnings | Reads a third-party `Sunset` signal, but has no `Deprecation`, OpenAPI, persistence, consumer graph, impact score, or migration artifacts | 10–20% |
| [Atlancia-Labs/spring-api-sunset](https://github.com/Atlancia-Labs/spring-api-sunset) | 5 | 2026-07-16 | Active | Spring Boot provider starter that emits `Sunset`, `Deprecation`, and `Link`, can return 410, and exposes Micrometer and Actuator data | Shares headers, deadlines, endpoints, consumers, and countdowns; it publishes and enforces provider policy rather than monitoring third parties | 35–45% |
| [funnyhcat-dotcom/api-sunset-header-doctor](https://github.com/funnyhcat-dotcom/api-sunset-header-doctor) | 0 | 2026-06-29 | Active | Node.js CLI that audits API documentation for headers, timelines, migration guides, SDK impact, governance, and CI readiness | Shares lifecycle vocabulary, timelines, and migration checks; it does not fetch APIs, preserve observations, or calculate urgency and blast radius | 15–25% |
| [pb33f/openapi-changes](https://github.com/pb33f/openapi-changes) | 357 | 2026-07-18 | Active | Semantic OpenAPI diff across files, Git revisions, and history with TUI, JSON, Markdown, and offline HTML reports | Shares OpenAPI and local change evidence; it does not combine lifecycle headers, manual feeds, consumer impact, countdowns, or calendars | 20–30% |
| [swade1987/deprek8ion](https://github.com/swade1987/deprek8ion) | 143 | 2025-06-11 | Archived; last code push 2021-07-28 | Rego and Conftest policies for deprecated Kubernetes APIs | Shares deprecation detection and replacement advice, but is limited to Kubernetes manifests | 10–20% |
| [naquada/deprek8](https://github.com/naquada/deprek8) | 112 | 2026-03-01 | Not archived; last code push 2020-11-24 | Kubernetes deprecation policies that identify unavailable API versions and replacements | Shares the deprecation concept, but has no generic HTTP/OpenAPI aggregation, state database, consumer map, or migration output | 10–20% |
| [gkarthiks/argo-apid-helper](https://github.com/gkarthiks/argo-apid-helper) | 12 | 2023-09-03 | Not archived; last code push 2023-10-11 | Read-only ArgoCD cluster queries that connect deprecated Kubernetes APIs to affected workloads | Its deprecated API to workload mapping resembles blast radius, but only within ArgoCD and Kubernetes | 20–30% |
| [api-evangelist/deprecation](https://github.com/api-evangelist/deprecation) | 0 | 2026-07-20 | Active content repository | Lifecycle vocabulary, RFC 8594 and Deprecation header material, OpenAPI flags, migration patterns, and governance rules | Covers the domain model but is not an executable collector, state store, calculator, or exporter | 5–15% |
| [specshield26/specshield-cli](https://github.com/specshield26/specshield-cli) | 5 | 2026-07-17 | Active | OpenAPI breaking-change diff, bidirectional contract tests, HAR consumer contracts, conformance checks, and deployment gates | Shares OpenAPI changes, consumer relationships, and history; focuses on pre-release contracts rather than deadlines and lifecycle headers | 25–35% |
| [eamoruso/ZombieAPI](https://github.com/eamoruso/ZombieAPI) | 0 | 2026-06-14 | Active | Runtime discovery of hidden or still-reachable deprecated endpoints using artifacts, method mutation, version enumeration, and response comparison | Shares endpoint discovery and deprecation status, but is an aggressive attack-surface scanner rather than an allowlisted low-frequency monitor | 15–25% |

Additional direct neighbors include
[sophiabits/graphql-sunset](https://github.com/sophiabits/graphql-sunset),
[wework/rails-sunset](https://github.com/wework/rails-sunset), and
[hskrasek/guzzle-sunset](https://github.com/hskrasek/guzzle-sunset). They publish or consume one
header in one framework and overlap by approximately 5–20%.

## Naming and high-overlap decision

No active repository in this sample overlaps more than 70% of the MVP. The closest result,
`spring-api-sunset`, has the opposite product direction: it helps an API provider publish and
enforce lifecycle policy, while this project helps an integration team observe third-party
signals and maintain its own risk view.

`specshield-cli` and `openapi-changes` focus on pre-release contract differences.
`argo-apid-helper` focuses on a Kubernetes-specific impact inventory. None joins standard
lifecycle signals, first-discovery persistence, local consumers, countdowns, impact radius,
calendar entries, issue drafts, and migration checklists.

The project therefore keeps:

- Project name: `Sunset Sentinel API`
- Repository slug: `sunset-sentinel-api`

## Differentiation kept in scope

1. Consumer-side and third-party-first, rather than provider middleware.
2. One evidence model for HTTP headers, OpenAPI flags, and manual feeds.
3. Persistent first discovery, source history, endpoints, and local consumers.
4. Deterministic urgency and blast-radius scoring.
5. ICS calendar, issue drafts, and migration checklists from the same records.
6. Explicit allowlists, cache bounds, request spacing, and offline fixtures; no endpoint
   enumeration or high-intensity probing.

The README uses the deliberately narrow statement:

> A sample search of public repositories found no active project with both the same name and a
> highly isomorphic feature set. Sunset Sentinel API is differentiated by its integration-side
> combination of third-party lifecycle signals, local consumer impact, and executable migration
> artifacts.
