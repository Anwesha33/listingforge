# ListingForge

An event-driven pipeline that turns messy seller listings into clean catalogue
data, screens them against content policy, detects duplicates, and decides
whether a human needs to look.

Paste in what a seller actually writes:

> `BEST QUALITY saree combo 2pcs cotton matrial free shipping!! 100% color guarantee`

and the pipeline produces:

```json
{
  "category": "sarees",
  "normalized_title": "Cotton Saree Combo Pack of 2",
  "attributes": { "fabric": "cotton", "pack_size": 2, "color": "red" },
  "status": "NEEDS_REVIEW",
  "review_reasons": [
    "UNSUPPORTED_GUARANTEE: Absolute guarantees such as \"100% guaranteed\" need evidence before they can be published.",
    "POSSIBLE_DUPLICATE: 88.8% similar to listing #2"
  ]
}
```

## Architecture

Three services, three languages, one Kafka backbone.

```
  seller
    │  POST /api/v1/listings          (idempotent on seller_id + seller_sku)
    ▼
┌─────────────────┐  listing.created   ┌──────────────────────┐
│  catalog-api    │ ─────────────────► │  enrichment-worker   │
│  Java / Spring  │                    │  Python + Gemini     │
│                 │                    │                      │
│  system of      │ ◄───────────────── │  attribute extraction│
│  record,        │  listing.          │  + 768-d embedding   │
│  single writer, │  enrichment-result └──────────────────────┘
│  state machine  │
│                 │  listing.enriched  ┌──────────────────────┐
│                 │ ─────────────────► │  moderation-service  │
│                 │                    │  Go + pgvector       │
│                 │ ◄───────────────── │                      │
└─────────────────┘  listing.moderated │  policy + duplicate  │
    │                                  │  detection           │
    ▼                                  └──────────────────────┘
  Postgres + pgvector
```

**Why the catalogue sits in the middle of the second hop.** Moderation consumes
`listing.enriched`, which the *catalogue* publishes after persisting the
worker's result — not the worker's output directly. That indirection exists
because of a race this project actually hit: moderation decides in
milliseconds, so it routinely produced a verdict before the catalogue had
written the enrichment. The verdict then arrived for a listing still in
`ENRICHING`, the state machine correctly refused the transition, and a third of
the pipeline was stranded. Letting the single writer sequence the pipeline makes
ordering a property of the design instead of a race that usually goes the right
way.

## Results

31 labelled listings through the real pipeline, no mocks.

| Metric | Result |
|---|---|
| Category accuracy | **1.000** |
| Attribute accuracy (labelled attributes) | **0.919** (57/62) |
| Decision accuracy | 0.742 |
| Auto-publish rate | 0.613 |
| Duplicate groups detected | **4/4** |

Decision confusion (expected → actual):

| | PUBLISH | REVIEW | REJECT |
|---|---|---|---|
| **PUBLISH** | 13 | 0 | 1 |
| **REVIEW** | 6 | 5 | 0 |
| **REJECT** | 0 | 1 | 5 |

These figures come from one run. The provider is not perfectly deterministic
even at temperature 0, so a re-run moves attribute accuracy by roughly a point
either way (0.919 and 0.935 on two runs of the same dataset); category accuracy,
decision accuracy, the auto-publish rate and duplicate detection have been stable
across runs.

Most of the disagreement is `REVIEW → PUBLISH`: the labels predicted a human
would be needed because an attribute looked unstated, and the model extracted it
anyway. That is the pipeline outperforming its own label set, not failing — but
the six cases are counted as errors rather than quietly relabelled, because
relabelling to match the implementation is how a benchmark stops meaning
anything.

### Duplicate thresholds were measured, not guessed

The reject and review thresholds started as intuition (0.97 / 0.92) and were
wrong: a genuine self-duplicate scored 0.9475 and was only sent to review, while
a true cross-seller duplicate scored 0.8793 and was missed entirely.

`benchmark/sweep_thresholds.py` computes every pairwise similarity the pipeline
produced, labels each pair, and sweeps the cutoff:

| | value |
|---|---|
| lowest true-duplicate similarity | 0.8883 |
| highest non-duplicate similarity | 0.7583 |
| **separation margin** | **+0.130** |
| best F1 | 1.000 at threshold 0.76 |

The first sweep produced a *negative* margin of −0.066. Investigating the worst
false positive showed it was a labelling error: two listings described "cotton
kurti, blue, size M" in different words and genuinely were the same product. The
label was corrected and the finding kept, because it produced the design change
below.

### Similarity alone is not enough

Even after relabelling, embeddings separate "same kind of product" far more
strongly than "same product" — a red kurti and a blue kurti sit close together
because the vector is dominated by product type. Moderation therefore requires
**structural agreement** as well: same category, and at least 60% of shared
attribute keys holding the same value, before similarity is allowed to mean
duplicate.

## Quickstart

```bash
export GEMINI_API_KEY=...
make up                              # Redpanda + Postgres/pgvector, and the topics
make build                           # catalog-api (Java 17), moderation (Go), worker (Python)
make run-catalog                     # terminal 1
make run-enrichment                  # terminal 2
make run-moderation                  # terminal 3
make benchmark                       # submit the labelled dataset and score
```

**`make up` creates the Kafka topics.** It did not always: they needed a separate
`make topics`, and forgetting it produced the worst failure this project had —
every service started cleanly, reported healthy, and nothing ever moved. Every
`run-*` target now checks the topics exist first and refuses to start with a
named error instead of stalling.

**Java 17 is resolved, not hardcoded.** `make build` looks at `JAVA17_HOME`,
then `/usr/libexec/java_home -v 17`, then `JAVA_HOME`, then `/usr/lib/jvm/*17*`,
and fails with an explicit message if none of them is a JDK 17. Override with
`make build JAVA17=/path/to/jdk-17`.

```bash
curl -X POST localhost:8081/api/v1/listings -H 'Content-Type: application/json' -d '{
  "sellerId":"S1","sellerSku":"SAR-1",
  "title":"BEST QUALITY saree combo 2pcs cotton matrial free shipping!!",
  "description":"100% color guarantee"}'

curl localhost:8081/api/v1/listings/1
curl localhost:8081/api/v1/listings/1/history     # full audit trail
curl 'localhost:8081/api/v1/listings?status=NEEDS_REVIEW'
curl localhost:8081/api/v1/listings/stats
```

## Design decisions

**Single writer.** Only the catalogue writes to the database. The worker and the
moderation service consume events, compute, and emit events. This is what makes
out-of-order results tractable: there is exactly one place where a status can
change, and one transition table governing it.

**Explicit state machine.** `ListingStatus` declares which transitions are legal
instead of scattering `if` statements. Because the pipeline is asynchronous,
events genuinely arrive late — Kafka is at-least-once, so a redelivered
enrichment can land after a human already decided. A rejected transition is a
signal, not an inconvenience.

**Idempotent ingestion** on `(seller_id, seller_sku)`. Sellers retry uploads
constantly, from flaky connections and from bulk spreadsheets run twice. A
catalogue that grows a duplicate row on every retry is worse than useless.

**Optimistic locking.** The enrichment and moderation results can commit in
either order; the version check stops a slow enrichment from overwriting a newer
moderation decision.

**No model in the moderation path.** Policy is deterministic regex plus
structural checks. A refusal has to be explainable to the seller it affects, and
"the classifier said so" is not an explanation. It also keeps moderation at
microseconds so the pipeline's cost stays dominated by enrichment.

**Moderation sees the seller's raw text.** The enrichment prompt strips
promotional and non-compliant language, so a listing claiming to cure diabetes
arrives at moderation already sanitised. Checking only the normalised text let
the model quietly launder policy violations; both are checked now.

**Taxonomy as data.** Categories and attribute schemas live in
`taxonomy.py` as structures, not prompt text, so extracted values are
*validated* rather than trusted — a value outside the declared enum is a
detectable error instead of a plausible string entering the catalogue.

**Reconciliation sweep.** A scheduled job fails listings stuck in `ENRICHING`
past a timeout. This exists because rate limiting sent two listings to the
dead-letter queue and nothing moved them out of `ENRICHING` — they sat there
invisible to both the seller and the review queue. An event-driven pipeline needs
a path for events that never arrive.

## Things this got wrong first, and what they taught

These are documented rather than quietly fixed, because each produced a design
change.

| Bug | Lesson |
|---|---|
| Moderation decided before the catalogue persisted enrichment, stranding a third of the pipeline | Ordering between services must be designed, not assumed from timing |
| pgvector IVFFlat returned **zero** rows while exact search found a 0.93 match | An approximate index built on an empty table probes empty lists; HNSW needs no training pass and is correct from the first row |
| `@Version` initialised to `0` made Spring Data treat new entities as existing, so `save()` merged instead of persisting and inserted twice | Framework new-entity detection is subtle; always use `save()`'s return value |
| Republishing a JSON string through a JSON serializer double-encoded it | Forward a parsed tree, which also preserves fields this service does not model |
| Rate limit (12 rpm) was slower than the stall timeout (5 min), so reconciliation failed healthy listings | Rate limits and liveness timeouts must be chosen together |
| Enum validation lowercased values, rejecting `"L"` for a size enum of `["S","M","L"]` | Normalise for comparison, store the canonical form |

## Layout

```
catalog-api/          Java 17 + Spring Boot 3 — system of record, state machine, REST
enrichment-worker/    Python — Gemini extraction, embeddings, rate limiting, DLQ
moderation-service/   Go — deterministic policy, pgvector duplicate detection
benchmark/            labelled dataset, end-to-end scorer, threshold sweep
deploy/               schema, Prometheus config, Maven settings
```

~1,050 lines Java · ~1,220 Python · ~990 Go · 11 Go test functions
Gemini `gemini-flash-lite-latest` for extraction, `gemini-embedding-001` (768-d)
for embeddings. Every service exposes Prometheus metrics.

## Limitations

- **The dataset is 31 listings.** Enough to catch design errors, not enough to
  pin a threshold precisely. Production calibration needs real catalogue data.
- **Extraction is single-pass.** One call against a union schema, filtered per
  category afterwards. Classifying first and extracting against a
  category-specific schema would be more precise and would double cost and
  latency.
- **Text only.** Listing images are ignored entirely, which is a large blind spot
  for a marketplace.
- **No authentication** on any service; they assume a trusted network.
- **The review UI is the API.** There is no front end; the review queue is a
  JSON endpoint.
