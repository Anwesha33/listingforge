package com.anwesha.listingforge.catalog.domain;

import java.util.EnumSet;
import java.util.Map;
import java.util.Set;

/**
 * Lifecycle of a listing.
 *
 * <p>The legal transitions are declared here rather than being implied by
 * scattered {@code if} statements. Two things make that worth the ceremony:
 * the pipeline is asynchronous, so events genuinely do arrive late and out of
 * order, and a rejected transition is the signal that something upstream is
 * misbehaving — silently accepting it would hide the bug.
 */
public enum ListingStatus {

    /** Accepted from the seller, not yet picked up for enrichment. */
    DRAFT,

    /** Handed to the enrichment worker. */
    ENRICHING,

    /** Attributes extracted; awaiting a moderation decision. */
    ENRICHED,

    /** Moderation was not confident enough to decide alone. */
    NEEDS_REVIEW,

    /** Live in the catalog. */
    PUBLISHED,

    /** Refused, by policy or by a human reviewer. */
    REJECTED,

    /** Enrichment failed permanently after retries. */
    FAILED;

    private static final Map<ListingStatus, Set<ListingStatus>> ALLOWED = Map.of(
            DRAFT,        EnumSet.of(ENRICHING, REJECTED),
            ENRICHING,    EnumSet.of(ENRICHED, FAILED),
            ENRICHED,     EnumSet.of(PUBLISHED, NEEDS_REVIEW, REJECTED),
            NEEDS_REVIEW, EnumSet.of(PUBLISHED, REJECTED),
            PUBLISHED,    EnumSet.of(NEEDS_REVIEW, REJECTED),
            REJECTED,     EnumSet.noneOf(ListingStatus.class),
            FAILED,       EnumSet.of(ENRICHING)
    );

    /** Whether this listing may move to {@code next}. */
    public boolean canTransitionTo(ListingStatus next) {
        return ALLOWED.getOrDefault(this, EnumSet.noneOf(ListingStatus.class)).contains(next);
    }

    /**
     * Whether this is an end state. Used to drop late-arriving events for
     * listings a human has already decided on: a moderation verdict that
     * arrives after a reviewer rejected the listing must not resurrect it.
     */
    public boolean isTerminal() {
        return this == REJECTED;
    }
}
