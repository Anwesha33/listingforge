package com.anwesha.listingforge.catalog.events;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.kafka.core.KafkaTemplate;
import org.springframework.stereotype.Component;

/** Publishes domain events to Kafka. */
@Component
public class EventPublisher {

    private static final Logger log = LoggerFactory.getLogger(EventPublisher.class);

    private final KafkaTemplate<String, Object> kafka;

    public EventPublisher(KafkaTemplate<String, Object> kafka) {
        this.kafka = kafka;
    }

    /**
     * Publishes {@code payload} keyed by {@code key}.
     *
     * <p>The key is always the listing id, which puts every event for one
     * listing on the same partition and therefore in order. Without that, a
     * listing's enrichment and moderation events could be processed by
     * different consumer threads in the wrong order, and the status machine
     * would be left guessing.
     */
    public void publish(String topic, String key, Object payload) {
        kafka.send(topic, key, payload).whenComplete((result, ex) -> {
            if (ex != null) {
                // The send is asynchronous and the caller's transaction has
                // already committed, so this is logged rather than thrown. The
                // reconciliation path for a lost event is the scheduled sweep
                // over listings stuck in ENRICHING, described in the README.
                log.error("failed to publish to {} key {}", topic, key, ex);
            } else {
                log.debug("published to {} partition {} offset {}", topic,
                        result.getRecordMetadata().partition(), result.getRecordMetadata().offset());
            }
        });
    }
}
