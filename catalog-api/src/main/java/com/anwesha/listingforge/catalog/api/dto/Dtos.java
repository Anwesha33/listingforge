package com.anwesha.listingforge.catalog.api.dto;

import com.anwesha.listingforge.catalog.domain.Listing;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/** Request and response payloads for the catalog API. */
public final class Dtos {

    private Dtos() {
    }

    /** What a seller submits. */
    public record CreateListingRequest(
            @NotBlank @Size(max = 64) String sellerId,
            @NotBlank @Size(max = 128) String sellerSku,
            @NotBlank @Size(max = 2000) String title,
            @Size(max = 8000) String description
    ) {
    }

    /** A human reviewer's verdict on a listing in NEEDS_REVIEW. */
    public record ReviewDecisionRequest(
            @NotBlank String reviewerId,
            @NotBlank String decision,   // APPROVE or REJECT
            String note
    ) {
    }

    /** Full listing view. */
    public record ListingResponse(
            Long id,
            String sellerId,
            String sellerSku,
            String rawTitle,
            String normalizedTitle,
            String description,
            String category,
            Map<String, Object> attributes,
            String status,
            List<String> reviewReasons,
            Long duplicateOf,
            Float duplicateScore,
            Instant createdAt,
            Instant updatedAt
    ) {
        public static ListingResponse from(Listing l) {
            return new ListingResponse(
                    l.getId(), l.getSellerId(), l.getSellerSku(),
                    l.getRawTitle(), l.getNormalizedTitle(), l.getDescription(),
                    l.getCategory(), l.getAttributes(), l.getStatus().name(),
                    l.getReviewReasons(), l.getDuplicateOf(), l.getDuplicateScore(),
                    l.getCreatedAt(), l.getUpdatedAt());
        }
    }

    /** Aggregate pipeline counters, used by the review UI and the benchmark. */
    public record PipelineStats(
            long draft, long enriching, long enriched,
            long needsReview, long published, long rejected, long failed,
            double autoPublishRate
    ) {
    }
}
