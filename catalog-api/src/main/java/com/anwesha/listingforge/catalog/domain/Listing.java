package com.anwesha.listingforge.catalog.domain;

import jakarta.persistence.*;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

import java.time.Instant;
import java.util.List;
import java.util.Map;

/** A seller's product listing at whatever stage of the pipeline it has reached. */
@Entity
@Table(name = "listings",
       uniqueConstraints = @UniqueConstraint(columnNames = {"seller_id", "seller_sku"}))
public class Listing {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "seller_id", nullable = false)
    private String sellerId;

    /**
     * The seller's own identifier for this product. Together with sellerId it
     * forms the natural key that makes ingestion idempotent: a seller retrying
     * an upload updates their existing listing instead of creating a duplicate.
     */
    @Column(name = "seller_sku", nullable = false)
    private String sellerSku;

    @Column(name = "raw_title", nullable = false, length = 2000)
    private String rawTitle;

    @Column(name = "raw_description", length = 8000)
    private String rawDescription;

    @Column(name = "normalized_title", length = 2000)
    private String normalizedTitle;

    @Column(name = "description", length = 8000)
    private String description;

    @Column(name = "category")
    private String category;

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(name = "attributes", nullable = false)
    private Map<String, Object> attributes = Map.of();

    @Enumerated(EnumType.STRING)
    @Column(name = "status", nullable = false)
    private ListingStatus status = ListingStatus.DRAFT;

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(name = "review_reasons", nullable = false)
    private List<String> reviewReasons = List.of();

    @Column(name = "duplicate_of")
    private Long duplicateOf;

    @Column(name = "duplicate_score")
    private Float duplicateScore;

    /**
     * Optimistic lock. The enrichment worker and the moderation service can
     * finish in either order; without this a slow enrichment result could
     * overwrite a newer moderation decision and silently un-publish a listing.
     */
    /**
     * Deliberately left null rather than initialised to zero. Spring Data uses
     * the version attribute to decide whether an entity is new: a non-null
     * version means "already persisted", so {@code save()} would call
     * {@code merge()} on a brand-new object. Merge returns a *different*
     * managed instance, leaving the caller holding a detached one with a null
     * id — and the next save inserts the row a second time.
     */
    @Version
    @Column(name = "version", nullable = false)
    private Integer version;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt = Instant.now();

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt = Instant.now();

    @PreUpdate
    void touch() {
        this.updatedAt = Instant.now();
    }

    protected Listing() {
        // for JPA
    }

    public Listing(String sellerId, String sellerSku, String rawTitle, String rawDescription) {
        this.sellerId = sellerId;
        this.sellerSku = sellerSku;
        this.rawTitle = rawTitle;
        this.rawDescription = rawDescription;
    }

    public Long getId() { return id; }
    public String getSellerId() { return sellerId; }
    public String getSellerSku() { return sellerSku; }
    public String getRawTitle() { return rawTitle; }
    public void setRawTitle(String v) { this.rawTitle = v; }
    public String getRawDescription() { return rawDescription; }
    public void setRawDescription(String v) { this.rawDescription = v; }
    public String getNormalizedTitle() { return normalizedTitle; }
    public void setNormalizedTitle(String v) { this.normalizedTitle = v; }
    public String getDescription() { return description; }
    public void setDescription(String v) { this.description = v; }
    public String getCategory() { return category; }
    public void setCategory(String v) { this.category = v; }
    public Map<String, Object> getAttributes() { return attributes; }
    public void setAttributes(Map<String, Object> v) { this.attributes = v == null ? Map.of() : v; }
    public ListingStatus getStatus() { return status; }
    public void setStatus(ListingStatus v) { this.status = v; }
    public List<String> getReviewReasons() { return reviewReasons; }
    public void setReviewReasons(List<String> v) { this.reviewReasons = v == null ? List.of() : v; }
    public Long getDuplicateOf() { return duplicateOf; }
    public void setDuplicateOf(Long v) { this.duplicateOf = v; }
    public Float getDuplicateScore() { return duplicateScore; }
    public void setDuplicateScore(Float v) { this.duplicateScore = v; }
    public Integer getVersion() { return version; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
}
