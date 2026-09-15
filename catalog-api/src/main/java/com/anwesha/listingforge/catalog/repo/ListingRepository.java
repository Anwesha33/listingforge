package com.anwesha.listingforge.catalog.repo;

import com.anwesha.listingforge.catalog.domain.Listing;
import com.anwesha.listingforge.catalog.domain.ListingStatus;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.time.Instant;
import java.util.List;
import java.util.Optional;

public interface ListingRepository extends JpaRepository<Listing, Long> {

    Optional<Listing> findBySellerIdAndSellerSku(String sellerId, String sellerSku);

    Page<Listing> findByStatus(ListingStatus status, Pageable pageable);

    Page<Listing> findBySellerId(String sellerId, Pageable pageable);

    long countByStatus(ListingStatus status);

    /**
     * Listings that entered enrichment before {@code cutoff} and are still
     * there. These are the ones whose result event never arrived.
     */
    @Query("SELECT l FROM Listing l WHERE l.status = 'ENRICHING' AND l.updatedAt < :cutoff")
    List<Listing> findStuckInEnriching(@Param("cutoff") Instant cutoff);

    /**
     * Stores the embedding for a listing.
     *
     * <p>Written with a native query because pgvector's {@code vector} type has
     * no JPA mapping; the value is passed as its text representation and cast
     * on the server. The upsert is deliberate — enrichment is at-least-once, so
     * the same listing can legitimately be enriched twice and the second result
     * should replace the first rather than fail on the primary key.
     */
    @Modifying
    @Query(value = """
            INSERT INTO listing_embeddings (listing_id, model, embedding)
            VALUES (:listingId, :model, CAST(:embedding AS vector))
            ON CONFLICT (listing_id) DO UPDATE
              SET embedding = EXCLUDED.embedding,
                  model     = EXCLUDED.model,
                  created_at = now()
            """, nativeQuery = true)
    void upsertEmbedding(@Param("listingId") Long listingId,
                         @Param("model") String model,
                         @Param("embedding") String embedding);
}
