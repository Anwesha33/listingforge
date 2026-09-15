package com.anwesha.listingforge.catalog.service;

import com.anwesha.listingforge.catalog.api.dto.Dtos;
import com.anwesha.listingforge.catalog.domain.Listing;
import com.anwesha.listingforge.catalog.domain.ListingEvent;
import com.anwesha.listingforge.catalog.domain.ListingStatus;
import com.anwesha.listingforge.catalog.events.EventPublisher;
import com.anwesha.listingforge.catalog.events.Events;
import com.anwesha.listingforge.catalog.repo.ListingEventRepository;
import com.anwesha.listingforge.catalog.repo.ListingRepository;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.stream.Collectors;

/** Application logic for the listing lifecycle. */
@Service
public class ListingService {

    private static final Logger log = LoggerFactory.getLogger(ListingService.class);

    private final ListingRepository listings;
    private final ListingEventRepository events;
    private final EventPublisher publisher;
    private final MeterRegistry meters;

    public ListingService(ListingRepository listings, ListingEventRepository events,
                          EventPublisher publisher, MeterRegistry meters) {
        this.listings = listings;
        this.events = events;
        this.publisher = publisher;
        this.meters = meters;
    }

    /**
     * Accepts a listing from a seller.
     *
     * <p>Idempotent on {@code (sellerId, sellerSku)}: re-submitting the same SKU
     * updates the existing listing and re-runs the pipeline rather than creating
     * a second one. Sellers retry uploads constantly — on a flaky mobile
     * connection, from a bulk spreadsheet run twice — and a catalog that grows a
     * duplicate every time is worse than useless.
     */
    @Transactional
    public Listing ingest(Dtos.CreateListingRequest req) {
        Optional<Listing> existing = listings.findBySellerIdAndSellerSku(req.sellerId(), req.sellerSku());

        Listing listing = existing.map(l -> {
            l.setRawTitle(req.title());
            l.setRawDescription(req.description());
            return l;
        }).orElseGet(() -> new Listing(req.sellerId(), req.sellerSku(), req.title(), req.description()));

        boolean isNew = listing.getId() == null;
        // The return value matters: save() may return a different managed
        // instance than the one passed in, and mutating the detached original
        // afterwards silently does nothing.
        listing = listings.save(listing);

        // A resubmission of a listing a human already rejected is not silently
        // re-enqueued; that would let a seller bypass review by re-uploading.
        if (!isNew && listing.getStatus().isTerminal()) {
            log.info("listing {} resubmitted but is in terminal state {}; not re-enqueued",
                    listing.getId(), listing.getStatus());
            counter("listings.ingest.terminal_skip").increment();
            return listing;
        }

        transition(listing, ListingStatus.ENRICHING, "catalog-api",
                Map.of("trigger", isNew ? "created" : "resubmitted"));

        publisher.publish(Events.TOPIC_CREATED, listing.getId().toString(),
                new Events.ListingCreated(listing.getId(), listing.getSellerId(),
                        listing.getSellerSku(), listing.getRawTitle(), listing.getRawDescription()));

        counter(isNew ? "listings.ingest.created" : "listings.ingest.updated").increment();
        return listing;
    }

    /**
     * Applies the enrichment worker's output.
     *
     * @return true when the enrichment was applied, so the caller knows whether
     *         to republish the listing for moderation. A dropped late event
     *         must not trigger a downstream decision.
     */
    @Transactional
    public boolean applyEnrichment(Events.ListingEnriched e) {
        Listing listing = listings.findById(e.listingId()).orElse(null);
        if (listing == null) {
            log.warn("enrichment for unknown listing {}", e.listingId());
            return false;
        }
        // Late events are dropped rather than applied. Kafka gives us
        // at-least-once delivery, so a redelivered enrichment can arrive after
        // a human has already decided; applying it would reopen a closed case.
        if (!listing.getStatus().canTransitionTo(ListingStatus.ENRICHED)) {
            log.info("ignoring enrichment for listing {} in state {}", e.listingId(), listing.getStatus());
            counter("listings.enrichment.ignored").increment();
            return false;
        }

        listing.setNormalizedTitle(e.normalizedTitle());
        listing.setDescription(e.description());
        listing.setCategory(e.category());
        listing.setAttributes(e.attributes());
        transition(listing, ListingStatus.ENRICHED, "enrichment-worker",
                Map.of("model", String.valueOf(e.extractionModel()),
                       "latency_ms", String.valueOf(e.latencyMs())));

        if (e.embedding() != null && !e.embedding().isEmpty()) {
            listings.upsertEmbedding(listing.getId(), e.embeddingModel(), toVectorLiteral(e.embedding()));
        }
        counter("listings.enrichment.applied").increment();
        return true;
    }

    /** Applies the moderation service's decision. */
    @Transactional
    public void applyModeration(Events.ListingModerated m) {
        Listing listing = listings.findById(m.listingId()).orElse(null);
        if (listing == null) {
            log.warn("moderation for unknown listing {}", m.listingId());
            return;
        }

        ListingStatus target = switch (m.decision()) {
            case "PUBLISH" -> ListingStatus.PUBLISHED;
            case "REJECT"  -> ListingStatus.REJECTED;
            default        -> ListingStatus.NEEDS_REVIEW;
        };

        if (!listing.getStatus().canTransitionTo(target)) {
            log.info("ignoring moderation {} for listing {} in state {}",
                    m.decision(), m.listingId(), listing.getStatus());
            counter("listings.moderation.ignored").increment();
            return;
        }

        listing.setReviewReasons(m.reasons());
        listing.setDuplicateOf(m.duplicateOf());
        listing.setDuplicateScore(m.duplicateScore());
        transition(listing, target, "moderation-service",
                Map.of("decision", m.decision(),
                       "confidence", String.valueOf(m.confidence()),
                       "reasons", String.join("; ", m.reasons())));

        counter("listings.moderation." + target.name().toLowerCase()).increment();
    }

    /**
     * Marks a listing failed after its enrichment result never arrived.
     *
     * <p>FAILED is not terminal: the transition table allows FAILED to move
     * back to ENRICHING, so a listing dropped because of a transient provider
     * outage can be replayed once the cause is fixed.
     */
    @Transactional
    public void markFailed(Listing listing, String reason) {
        if (!listing.getStatus().canTransitionTo(ListingStatus.FAILED)) {
            return;
        }
        listing.setReviewReasons(List.of("PIPELINE_STALLED: " + reason));
        transition(listing, ListingStatus.FAILED, "reconciliation", Map.of("reason", reason));
    }

    /** Records a human reviewer's verdict. */
    @Transactional
    public Listing review(Long id, Dtos.ReviewDecisionRequest req) {
        Listing listing = listings.findById(id)
                .orElseThrow(() -> new IllegalArgumentException("no listing " + id));

        ListingStatus target = "APPROVE".equalsIgnoreCase(req.decision())
                ? ListingStatus.PUBLISHED : ListingStatus.REJECTED;

        if (!listing.getStatus().canTransitionTo(target)) {
            throw new IllegalStateException(
                    "cannot move listing " + id + " from " + listing.getStatus() + " to " + target);
        }

        transition(listing, target, "reviewer:" + req.reviewerId(),
                Map.of("note", req.note() == null ? "" : req.note()));
        counter("listings.review." + target.name().toLowerCase()).increment();
        return listing;
    }

    @Transactional(readOnly = true)
    public Optional<Listing> find(Long id) {
        return listings.findById(id);
    }

    @Transactional(readOnly = true)
    public Page<Listing> byStatus(ListingStatus status, Pageable pageable) {
        return listings.findByStatus(status, pageable);
    }

    @Transactional(readOnly = true)
    public List<ListingEvent> history(Long id) {
        return events.findByListingIdOrderByCreatedAtAsc(id);
    }

    /**
     * Pipeline counters.
     *
     * <p>The auto-publish rate is the number this whole system exists to move:
     * the share of listings that cleared without a human ever looking at them.
     */
    @Transactional(readOnly = true)
    public Dtos.PipelineStats stats() {
        long published = listings.countByStatus(ListingStatus.PUBLISHED);
        long review = listings.countByStatus(ListingStatus.NEEDS_REVIEW);
        long rejected = listings.countByStatus(ListingStatus.REJECTED);
        long decided = published + review + rejected;

        return new Dtos.PipelineStats(
                listings.countByStatus(ListingStatus.DRAFT),
                listings.countByStatus(ListingStatus.ENRICHING),
                listings.countByStatus(ListingStatus.ENRICHED),
                review, published, rejected,
                listings.countByStatus(ListingStatus.FAILED),
                decided == 0 ? 0.0 : (double) published / decided);
    }

    /** Moves a listing to a new status and records the transition. */
    private void transition(Listing listing, ListingStatus to, String actor, Map<String, Object> detail) {
        ListingStatus from = listing.getStatus();
        listing.setStatus(to);
        listing = listings.save(listing);
        events.save(new ListingEvent(listing.getId(), from, to, actor, detail));
        log.info("listing {} {} -> {} by {}", listing.getId(), from, to, actor);
    }

    /**
     * Renders an embedding as a pgvector literal: {@code [0.1,0.2,...]}.
     * pgvector accepts this text form and casts it server-side, which avoids
     * needing a custom Hibernate type for a single column.
     */
    private static String toVectorLiteral(List<Double> embedding) {
        return embedding.stream()
                .map(d -> String.format("%.6f", d))
                .collect(Collectors.joining(",", "[", "]"));
    }

    private Counter counter(String name) {
        return meters.counter(name);
    }
}
