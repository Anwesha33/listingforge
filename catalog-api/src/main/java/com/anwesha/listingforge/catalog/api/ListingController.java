package com.anwesha.listingforge.catalog.api;

import com.anwesha.listingforge.catalog.api.dto.Dtos;
import com.anwesha.listingforge.catalog.domain.ListingEvent;
import com.anwesha.listingforge.catalog.domain.ListingStatus;
import com.anwesha.listingforge.catalog.service.ListingService;
import jakarta.validation.Valid;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageRequest;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/v1/listings")
@CrossOrigin
public class ListingController {

    private final ListingService service;

    public ListingController(ListingService service) {
        this.service = service;
    }

    /**
     * Ingests a listing.
     *
     * <p>Returns 202 Accepted rather than 201 Created: the listing exists, but
     * the work the caller actually cares about — enrichment and moderation —
     * happens asynchronously, and claiming 201 would imply a finished resource.
     */
    @PostMapping
    public ResponseEntity<Dtos.ListingResponse> create(@Valid @RequestBody Dtos.CreateListingRequest req) {
        var listing = service.ingest(req);
        return ResponseEntity.accepted().body(Dtos.ListingResponse.from(listing));
    }

    @GetMapping("/{id}")
    public ResponseEntity<Dtos.ListingResponse> get(@PathVariable Long id) {
        return service.find(id)
                .map(Dtos.ListingResponse::from)
                .map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.notFound().build());
    }

    @GetMapping
    public Page<Dtos.ListingResponse> list(
            @RequestParam(defaultValue = "NEEDS_REVIEW") ListingStatus status,
            @RequestParam(defaultValue = "0") int page,
            @RequestParam(defaultValue = "20") int size) {
        return service.byStatus(status, PageRequest.of(page, size)).map(Dtos.ListingResponse::from);
    }

    @GetMapping("/{id}/history")
    public List<Map<String, Object>> history(@PathVariable Long id) {
        return service.history(id).stream().map(ListingController::renderEvent).toList();
    }

    @PostMapping("/{id}/review")
    public ResponseEntity<Dtos.ListingResponse> review(@PathVariable Long id,
                                                       @Valid @RequestBody Dtos.ReviewDecisionRequest req) {
        return ResponseEntity.ok(Dtos.ListingResponse.from(service.review(id, req)));
    }

    @GetMapping("/stats")
    public Dtos.PipelineStats stats() {
        return service.stats();
    }

    private static Map<String, Object> renderEvent(ListingEvent e) {
        return Map.of(
                "from", e.getFromStatus() == null ? "" : e.getFromStatus().name(),
                "to", e.getToStatus().name(),
                "actor", e.getActor(),
                "detail", e.getDetail(),
                "at", e.getCreatedAt().toString());
    }

    @ExceptionHandler(IllegalStateException.class)
    public ResponseEntity<Map<String, String>> onIllegalState(IllegalStateException ex) {
        return ResponseEntity.status(HttpStatus.CONFLICT).body(Map.of("error", ex.getMessage()));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<Map<String, String>> onIllegalArgument(IllegalArgumentException ex) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND).body(Map.of("error", ex.getMessage()));
    }
}
