package com.anwesha.listingforge.catalog.service;

import com.anwesha.listingforge.catalog.domain.Listing;
import com.anwesha.listingforge.catalog.domain.ListingStatus;
import com.anwesha.listingforge.catalog.repo.ListingRepository;
import io.micrometer.core.instrument.MeterRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.Duration;
import java.time.Instant;
import java.util.List;

/**
 * Finds listings that entered the pipeline and never came out.
 *
 * <p>This exists because of a real failure observed during benchmarking: the
 * enrichment worker exhausted its retries against a rate-limited provider and
 * correctly routed two listings to the dead-letter queue — but nothing moved
 * those listings out of {@code ENRICHING}, so they sat there indefinitely,
 * invisible to both the seller and the review queue.
 *
 * <p>An event-driven pipeline needs a reconciliation path precisely because
 * events can be dropped, dead-lettered, or consumed by a process that dies
 * before producing its result. Waiting for a message that will never arrive is
 * not a state a system should be able to stay in.
 */
@Component
public class ReconciliationJob {

    private static final Logger log = LoggerFactory.getLogger(ReconciliationJob.class);

    private final ListingRepository listings;
    private final ListingService service;
    private final MeterRegistry meters;
    private final Duration stuckAfter;

    public ReconciliationJob(ListingRepository listings, ListingService service, MeterRegistry meters,
                             @Value("${listingforge.reconciliation.stuck-after:PT5M}") Duration stuckAfter) {
        this.listings = listings;
        this.service = service;
        this.meters = meters;
        this.stuckAfter = stuckAfter;
    }

    /**
     * Sweeps for stalled listings.
     *
     * <p>Runs on a fixed delay rather than a fixed rate so that a slow sweep
     * never overlaps with itself. The sweep is deliberately conservative: it
     * marks listings FAILED rather than re-enqueuing them, because the common
     * cause is a provider refusing the request, and a sweep that re-enqueues
     * automatically turns a quota problem into an infinite retry loop.
     */
    @Scheduled(fixedDelayString = "${listingforge.reconciliation.interval:PT60S}")
    @Transactional
    public void sweep() {
        Instant cutoff = Instant.now().minus(stuckAfter);
        List<Listing> stuck = listings.findStuckInEnriching(cutoff);
        if (stuck.isEmpty()) {
            return;
        }

        log.warn("reconciliation found {} listing(s) stuck in ENRICHING for over {}", stuck.size(), stuckAfter);
        for (Listing listing : stuck) {
            service.markFailed(listing, "no enrichment result received within " + stuckAfter);
        }
        meters.counter("listings.reconciliation.failed").increment(stuck.size());
    }
}
