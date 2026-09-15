package com.anwesha.listingforge.catalog.events;

import com.anwesha.listingforge.catalog.service.ListingService;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Component;

/**
 * Consumes the results produced by the two downstream services.
 *
 * <p>Payloads are received as raw strings and deserialized explicitly here,
 * rather than letting Spring infer the type. The producers are a Python worker
 * and a Go service, neither of which writes Spring's Java type headers, and
 * inferring the type from the listener signature does not work when one
 * consumer group handles two topics carrying two different shapes. Doing the
 * mapping by hand also means a malformed message fails inside the listener,
 * where it can be logged with its content, instead of inside the deserializer
 * where all that survives is a stack trace.
 */
@Component
public class PipelineConsumers {

    private static final Logger log = LoggerFactory.getLogger(PipelineConsumers.class);

    private final ListingService service;
    private final ObjectMapper mapper;
    private final EventPublisher publisher;

    public PipelineConsumers(ListingService service, ObjectMapper mapper, EventPublisher publisher) {
        this.service = service;
        this.mapper = mapper;
        this.publisher = publisher;
    }

    /**
     * Persists an enrichment result, then republishes it for moderation.
     *
     * <p>The republish happens only after the transaction has committed, which
     * is the whole point: moderation is guaranteed to see a listing the
     * database already knows is ENRICHED.
     */
    @KafkaListener(topics = Events.TOPIC_ENRICHMENT_RESULT, groupId = "catalog-api")
    public void onEnrichmentResult(String payload) {
        try {
            Events.ListingEnriched event = mapper.readValue(payload, Events.ListingEnriched.class);
            if (service.applyEnrichment(event)) {
                // Forwarded as a parsed tree rather than as the record or the
                // raw string. Publishing the record would drop fields this
                // service does not model but moderation needs -- the seller's
                // original text and the extraction diagnostics. Publishing the
                // string would let the JSON serializer encode it as a quoted,
                // escaped JSON *string*, which the Go consumer cannot read.
                JsonNode forwarded = mapper.readTree(payload);
                publisher.publish(Events.TOPIC_ENRICHED, String.valueOf(event.listingId()), forwarded);
            }
        } catch (Exception ex) {
            // Swallowed deliberately: rethrowing would redeliver the same
            // message forever and stall the partition behind it. The payload is
            // logged so the bad message can be found and replayed by hand.
            log.error("could not apply enrichment event: {}", truncate(payload), ex);
        }
    }

    @KafkaListener(topics = Events.TOPIC_MODERATED, groupId = "catalog-api")
    public void onModerated(String payload) {
        try {
            Events.ListingModerated event = mapper.readValue(payload, Events.ListingModerated.class);
            service.applyModeration(event);
        } catch (Exception ex) {
            log.error("could not apply moderation event: {}", truncate(payload), ex);
        }
    }

    private static String truncate(String s) {
        return s == null ? "null" : s.length() <= 500 ? s : s.substring(0, 500) + "...";
    }
}
