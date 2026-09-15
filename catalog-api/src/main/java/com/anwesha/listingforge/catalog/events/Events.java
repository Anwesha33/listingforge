package com.anwesha.listingforge.catalog.events;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

import java.util.List;
import java.util.Map;

/**
 * Event payloads exchanged over Kafka.
 *
 * <p>These records are the contract between three services written in three
 * languages, so every field name here is mirrored by hand in the Python worker
 * and the Go moderation service. That duplication is the cost of not running a
 * schema registry; for a system this size the tradeoff is worth it, and the
 * field names are kept deliberately boring to make the mirroring obvious.
 */
public final class Events {

    private Events() {
    }

    public static final String TOPIC_CREATED = "listing.created";

    /**
     * Where the enrichment worker publishes its result. Only the catalogue
     * consumes this.
     */
    public static final String TOPIC_ENRICHMENT_RESULT = "listing.enrichment-result";

    /**
     * Published by the catalogue once an enrichment result has been persisted.
     * Moderation consumes this rather than the worker's output directly.
     *
     * <p>The indirection removes a race that stranded a third of the pipeline:
     * moderation reaches a decision in milliseconds, so when it consumed the
     * worker's output it routinely produced a verdict before the catalogue had
     * written the enrichment. The verdict then arrived for a listing still in
     * ENRICHING, the state machine correctly refused the transition, and the
     * listing was stuck forever. Letting the single writer sequence the
     * pipeline makes the ordering a property of the design rather than a race
     * that usually goes the right way.
     */
    public static final String TOPIC_ENRICHED = "listing.enriched";
    public static final String TOPIC_MODERATED = "listing.moderated";
    public static final String TOPIC_DLQ       = "listing.dlq";

    /** Emitted when a listing is accepted from a seller. */
    public record ListingCreated(
            Long listingId,
            String sellerId,
            String sellerSku,
            String rawTitle,
            String rawDescription
    ) {
    }

    /**
     * Emitted by the enrichment worker once attributes have been extracted.
     *
     * <p>Unknown fields are ignored so the Python worker can add a field
     * without this service failing to deserialize every message until it is
     * redeployed. Additive changes staying backward compatible is the whole
     * point of not sharing a schema registry.
     */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ListingEnriched(
            Long listingId,
            String sellerId,
            String normalizedTitle,
            String description,
            String category,
            Map<String, Object> attributes,
            List<Double> embedding,
            String embeddingModel,
            String extractionModel,
            Integer promptTokens,
            Integer completionTokens,
            Long latencyMs
    ) {
    }

    /** Emitted by the moderation service with the final automated decision. */
    @JsonIgnoreProperties(ignoreUnknown = true)
    public record ListingModerated(
            Long listingId,
            String decision,            // PUBLISH, REVIEW or REJECT
            List<String> reasons,
            Long duplicateOf,
            Float duplicateScore,
            Double confidence
    ) {
    }
}
