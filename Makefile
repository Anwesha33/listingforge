# Java 17 is required: the catalog API uses language features Java 11 does not
# have, and Spring Boot 3 refuses to start on anything older. This is resolved
# rather than hardcoded, because a hardcoded path is the first thing that breaks
# on someone else's machine. Override explicitly if detection picks wrong:
#
#   make build JAVA17=/path/to/jdk-17
#
JAVA17 ?= $(shell \
	if [ -n "$$JAVA17_HOME" ]; then echo "$$JAVA17_HOME"; \
	elif /usr/libexec/java_home -v 17 >/dev/null 2>&1; then /usr/libexec/java_home -v 17; \
	elif [ -n "$$JAVA_HOME" ] && "$$JAVA_HOME/bin/java" -version 2>&1 | grep -q '"17'; then echo "$$JAVA_HOME"; \
	else ls -d /usr/lib/jvm/*17* 2>/dev/null | head -1; fi)

MVN    := mvn -s deploy/maven-settings.xml

TOPICS := listing.created listing.enrichment-result listing.enriched listing.moderated listing.dlq

.PHONY: up down topics check-java check-topics build run-catalog run-enrichment run-moderation benchmark sweep test clean reset

## Infrastructure -------------------------------------------------------------
# `up` now creates the topics too. They used to need a separate `make topics`,
# and forgetting it produced the worst possible failure: every service started
# cleanly, reported healthy, and nothing ever moved. A required step that fails
# silently is a bug in the Makefile, not in the user.
up:
	docker compose up -d
	@echo "waiting for redpanda..."
	@for i in $$(seq 1 60); do \
		if docker exec listingforge-redpanda-1 rpk cluster health >/dev/null 2>&1; then break; fi; \
		sleep 2; \
	done
	@$(MAKE) --no-print-directory topics
	@echo "listingforge infrastructure is up and the topics exist."

down:
	docker compose down

topics:
	@docker exec listingforge-redpanda-1 rpk topic create $(TOPICS) -p 3 -r 1 2>&1 \
	  | grep -v TOPIC_ALREADY_EXISTS || true
	@$(MAKE) --no-print-directory check-topics

# Fails loudly if a topic is missing, so the silent-stall failure mode cannot
# happen: every `run-*` target depends on this.
check-topics:
	@missing=""; \
	for t in $(TOPICS); do \
		if ! docker exec listingforge-redpanda-1 rpk topic describe $$t >/dev/null 2>&1; then \
			missing="$$missing $$t"; \
		fi; \
	done; \
	if [ -n "$$missing" ]; then \
		echo ""; \
		echo "ERROR: missing Kafka topics:$$missing"; \
		echo "       Nothing will move through the pipeline until these exist."; \
		echo "       Run: make up    (which now creates them)"; \
		echo ""; \
		exit 1; \
	fi; \
	echo "all $(words $(TOPICS)) topics present"

# Fails loudly when Java 17 cannot be found, instead of failing later inside
# Maven with an error about a language level.
check-java:
	@if [ -z "$(JAVA17)" ] || [ ! -x "$(JAVA17)/bin/java" ]; then \
		echo ""; \
		echo "ERROR: could not find a Java 17 JDK."; \
		echo "       Looked at: JAVA17_HOME, /usr/libexec/java_home -v 17, JAVA_HOME, /usr/lib/jvm/*17*"; \
		echo "       Install one (macOS: brew install --cask zulu@17) or point at yours:"; \
		echo "           make build JAVA17=/path/to/jdk-17"; \
		echo ""; \
		exit 1; \
	fi
	@echo "using Java 17 at $(JAVA17)"

## Build ----------------------------------------------------------------------
build: check-java
	cd catalog-api && JAVA_HOME=$(JAVA17) $(MVN) -q -DskipTests package
	cd moderation-service && go build -o bin/moderation ./cmd/moderation
	cd enrichment-worker && python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt

## Run ------------------------------------------------------------------------
run-catalog: check-java check-topics
	$(JAVA17)/bin/java -jar catalog-api/target/catalog-api-1.0.0.jar

run-enrichment: check-topics
	cd enrichment-worker && PYTHONPATH=src ./.venv/bin/python -m enrichment

run-moderation: check-topics
	cd moderation-service && ./bin/moderation

## Measure --------------------------------------------------------------------
benchmark:
	python3 benchmark/run.py --json benchmark/results.json

sweep:
	python3 benchmark/sweep_thresholds.py --json benchmark/threshold-sweep.json

test: check-java
	cd moderation-service && go test ./... -count=1
	cd catalog-api && JAVA_HOME=$(JAVA17) $(MVN) -q test

## Reset ----------------------------------------------------------------------
# Wipes all pipeline state so a benchmark run starts from a known baseline.
reset:
	docker exec listingforge-postgres-1 psql -U listingforge -d listingforge \
	  -c "TRUNCATE listing_events, listing_embeddings, listings RESTART IDENTITY CASCADE;"
	for t in listing.created listing.enrichment-result listing.enriched listing.moderated listing.dlq; do \
	  docker exec listingforge-redpanda-1 rpk topic delete $$t >/dev/null 2>&1 || true; done
	$(MAKE) topics

clean:
	rm -rf catalog-api/target moderation-service/bin enrichment-worker/.venv
