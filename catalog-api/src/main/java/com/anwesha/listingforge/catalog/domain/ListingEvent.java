package com.anwesha.listingforge.catalog.domain;

import jakarta.persistence.*;
import org.hibernate.annotations.JdbcTypeCode;
import org.hibernate.type.SqlTypes;

import java.time.Instant;
import java.util.Map;

/**
 * One recorded status transition.
 *
 * <p>Written on every state change so that a listing's history can be
 * reconstructed afterwards. A moderation decision a human cannot explain after
 * the fact is not much use to the person who has to defend it to the seller.
 */
@Entity
@Table(name = "listing_events")
public class ListingEvent {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "listing_id", nullable = false)
    private Long listingId;

    @Enumerated(EnumType.STRING)
    @Column(name = "from_status")
    private ListingStatus fromStatus;

    @Enumerated(EnumType.STRING)
    @Column(name = "to_status", nullable = false)
    private ListingStatus toStatus;

    /** Which component caused the transition: a service name or a reviewer id. */
    @Column(name = "actor", nullable = false)
    private String actor;

    @JdbcTypeCode(SqlTypes.JSON)
    @Column(name = "detail", nullable = false)
    private Map<String, Object> detail = Map.of();

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt = Instant.now();

    protected ListingEvent() {
    }

    public ListingEvent(Long listingId, ListingStatus from, ListingStatus to, String actor, Map<String, Object> detail) {
        this.listingId = listingId;
        this.fromStatus = from;
        this.toStatus = to;
        this.actor = actor;
        this.detail = detail == null ? Map.of() : detail;
    }

    public Long getId() { return id; }
    public Long getListingId() { return listingId; }
    public ListingStatus getFromStatus() { return fromStatus; }
    public ListingStatus getToStatus() { return toStatus; }
    public String getActor() { return actor; }
    public Map<String, Object> getDetail() { return detail; }
    public Instant getCreatedAt() { return createdAt; }
}
