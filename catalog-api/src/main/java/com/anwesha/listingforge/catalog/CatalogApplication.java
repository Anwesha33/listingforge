package com.anwesha.listingforge.catalog;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.kafka.annotation.EnableKafka;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * Catalog API: the system of record for seller listings.
 *
 * <p>This service owns the {@code listings} table and is the only writer to it.
 * The enrichment worker and the moderation service are deliberately stateless:
 * they consume events, compute, and emit events, and never touch the database
 * directly. Keeping a single writer is what makes the status transitions
 * reasonable to reason about when two workers finish out of order.
 */
@SpringBootApplication
@EnableKafka
@EnableScheduling
public class CatalogApplication {
    public static void main(String[] args) {
        SpringApplication.run(CatalogApplication.class, args);
    }
}
