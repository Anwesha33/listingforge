package com.anwesha.listingforge.catalog.repo;

import com.anwesha.listingforge.catalog.domain.ListingEvent;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface ListingEventRepository extends JpaRepository<ListingEvent, Long> {
    List<ListingEvent> findByListingIdOrderByCreatedAtAsc(Long listingId);
}
