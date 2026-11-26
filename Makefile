# ============================================================
# Python Microservices Makefile
# Owner: Multicorewareinc
# Repo:  M-Stack
# ============================================================

SHELL := /bin/bash

# ------------------------------------------------------------
# Tools
# ------------------------------------------------------------

PYTHON  ?= python3
PIP     ?= $(PYTHON) -m pip
PYTEST  ?= $(PYTHON) -m pytest

RUFF    ?= ruff
BLACK   ?= black
MYPY    ?= mypy

DOCKER  ?= docker
BUILDX ?= 0
CACHE_ENABLED ?= 0
PUSH_TO_GHCR ?= false
KUBECTL ?= kubectl
HELM    ?= helm
HELM_ARGS ?=
SSH     ?= ssh
SCP     ?= scp
IMAGE ?=
HEALTHCHECK_URL ?=
HEALTHCHECK_NAME ?= make-healthcheck
ROLLOUT_TIMEOUT ?= 5m
REGISTRY_USER ?=
REGISTRY_PASSWORD ?=
REGISTRY_SECRET_NAME ?= ghcr-secret


# ------------------------------------------------------------
# Repository
# ------------------------------------------------------------

API_DIR := api/microservices

# NOTE: do NOT default SERVICE_DIR to a specific service here.
# Targets that operate on a single service require SERVICE=<name>
# on the command line. Targets that iterate skip missing dirs.

TEST_DIR ?= tests
TEST_ARGS ?=
TEST_WORKING_DIR ?= .
TEST_ALL_ARGS ?=

# Legacy vars — kept so any external reference still works.
# Prefer SERVICE_DIR derived from SERVICE below.
SERVICE_DIR ?= $(if $(SERVICE),$(API_DIR)/$(SERVICE),$(API_DIR))
CHECK_DIR   ?= $(SERVICE_DIR)/app $(SERVICE_DIR)/tests
TYPECHECK_DIR ?= $(SERVICE_DIR)/app
E2E_PROJECT ?= multistack-e2e
E2E_COMPOSE_FILE ?= api/tests/manual-test/docker-compose.full.yml
E2E_RESULTS_DIR ?= test-results

NAMESPACE ?= multistack

# Image/chart registry root — the service name is appended per-command.
# Owner is derived from the repo, never hardcoded: GITHUB_REPOSITORY_OWNER
# in CI, otherwise the origin remote. This is what makes the same Makefile
# resolve to the right owner for whatever repo it runs in -- for example
# ghcr.io/multicorewareinc/m-stack in M-Stack, with nothing to edit per repo.
GH_REPO ?= $(shell \
	if [ -n "$$GITHUB_REPOSITORY" ]; then echo "$${GITHUB_REPOSITORY##*/}"; \
	else basename "$$(git config --get remote.origin.url 2>/dev/null)" .git; fi \
	| tr '[:upper:]' '[:lower:]')
ifeq ($(strip $(GH_REPO)),)
GH_REPO := m-stack
endif

GH_OWNER ?= $(shell \
	if [ -n "$$GITHUB_REPOSITORY_OWNER" ]; then echo "$$GITHUB_REPOSITORY_OWNER"; \
	else git config --get remote.origin.url 2>/dev/null; fi \
	| sed -E 's,^(git@|https://|ssh://git@)github\.com[:/]+([^/]+)/.*,\2,' \
	| tr '[:upper:]' '[:lower:]')
ifeq ($(strip $(GH_OWNER)),)
GH_OWNER := multicorewareinc
endif

REGISTRY ?= ghcr.io/$(GH_OWNER)/$(GH_REPO)
CHART_REGISTRY ?= oci://ghcr.io/$(GH_OWNER)/$(GH_REPO)/charts
TAG      ?= latest


# ------------------------------------------------------------
# RKE2 Provisioning Configuration
# ------------------------------------------------------------

RKE2_ENV         ?= onprem

SERVER           ?= $(MULTISTACK_SERVER)
AGENTS           ?= $(MULTISTACK_AGENTS)
SSH_USER         ?= $(MULTISTACK_SSH_USER)
SSH_KEY          ?= $(HOME)/.ssh/id_ed25519

MULTISTACK_DIR      := $(HOME)/.multistack
MULTISTACK_STATE    := $(MULTISTACK_DIR)/state/ai-cluster.json
MULTISTACK_KUBECONF := $(MULTISTACK_DIR)/kubeconfig

REMOTE_MULTISTACK_DIR := /home/$(SSH_USER)/.multistack

INSTALL_STORAGE ?= 0
FORCE_RECREATE  ?= 1

RKE2_EXAMPLES   := examples/rke2
STORAGE_EXAMPLES:= examples/storage
K8S_DIR          := k8s


# ------------------------------------------------------------
# Microservices
# ------------------------------------------------------------
# Source of truth: every subdirectory under api/microservices/
# that has a chart/ or src/ (or app/) directory.
# ------------------------------------------------------------

SERVICES := \
	admin-control-plane \
	billing \
	enricher \
	model-gateway \
	organization-control-plane \
	rate-limiter-rpm \
	rate-limiter-tpm \
	rag \
	tokenizer

# The two SPAs. Not in SERVICES: they are not under $(API_DIR), and their
# build context is the ui/ workspace root rather than their own directory,
# because each depends on the @multistack/ui workspace package. Nothing
# built these until now -- the images in GHCR were pushed by hand, and by
# 2026-10-09 were three weeks older than the UI they served.
PORTALS := \
	admin-portal \
	organization-portal

UI_DIR ?= ui

DOCKER_SERVICES := $(SERVICES) $(PORTALS)
ROOT_CONTEXT_SERVICES := billing enricher model-gateway rate-limiter-rpm rate-limiter-tpm

# Which services have a Helm chart (skip the rest in chart-* targets).
SERVICES_WITH_CHART := $(shell for s in $(SERVICES); do \
	[ -f "$(API_DIR)/$$s/chart/Chart.yaml" ] && echo $$s; \
done)

# Which services have python source to lint/type-check.
SERVICES_WITH_APP := $(shell for s in $(SERVICES); do \
	if [ -d "$(API_DIR)/$$s/app" ] || [ -d "$(API_DIR)/$$s/src" ]; then \
		echo $$s; \
	fi; \
done)


# ============================================================
# Main Targets
# ============================================================

.PHONY: all
all: check test build

.PHONY: build
build: docker-build


.PHONY: github-variables
github-variables:
	@if [[ "$${GITHUB_REF}" == refs/tags/* ]]; then \
		VERSION="$${GITHUB_REF_NAME}"; \
	else \
		VERSION="$${GITHUB_SHA:0:7}"; \
	fi; \
	OWNER="$(GH_OWNER)"; \
	REPO_NAME="$(GH_REPO)"; \
	REPO_NAME=$${REPO_NAME:-m-stack}; \
	echo "version=$$VERSION"; \
	echo "python_version=$${PYTHON_VERSION:-3.11}"; \
	echo "image_tag=$$VERSION"; \
	echo "image_registry=ghcr.io/$$OWNER/$$REPO_NAME"; \
	echo "chart_registry=ghcr.io/$$OWNER/$$REPO_NAME/charts"; \
	echo "chart_version=0.0.0-$$VERSION"; \
	echo "namespace=$${NAMESPACE:-multistack}"


.PHONY: release-changelog
release-changelog:
	@previous_tag=$$(git describe --tags --abbrev=0 HEAD^ 2>/dev/null || true); \
		if [ -n "$$previous_tag" ]; then \
			git log "$$previous_tag..HEAD" --oneline --pretty=format:"- %s"; \
		else \
			git log --oneline --pretty=format:"- %s" | head -20; \
		fi


# ============================================================
# Help
# ============================================================

.PHONY: help
help:
	@echo ""
	@echo "Python Microservices Makefile (M-Stack)"
	@echo ""
	@echo "Available services:"
	@for svc in $(SERVICES); do echo "  - $$svc"; done
	@echo ""
	@echo "Services with Helm charts:"
	@for svc in $(SERVICES_WITH_CHART); do echo "  - $$svc"; done
	@echo ""
	@echo "Usage:"
	@echo "  make install              Install dependencies"
	@echo "  make check                Run all code checks (all services)"
	@echo "  make check SERVICE=<name> Run checks for one service"
	@echo "  make lint                 Run Ruff (all services)"
	@echo "  make format               Format Python code (all services)"
	@echo "  make format-check         Check formatting (all services)"
	@echo "  make type-check           Run mypy (all services)"
	@echo "  make test                 Run tests"
	@echo "  make test-all             Run the complete repository test suite"
	@echo "  make test-helm            Run Helm chart tests"
	@echo "  make install-tests        Install repository test dependencies"
	@echo "  make build                Build all services"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build         Build all Docker images"
	@echo "  make docker-build SERVICE=<name>"
	@echo "  make docker-push          Push all Docker images"
	@echo "  make docker-login         Login to registry"
	@echo ""
	@echo "Helm charts:"
	@echo "  make chart-package        Package all charts -> ./dist"
	@echo "  make chart-package SERVICE=<name>"
	@echo "  make chart-push           Package + push all charts to GHCR"
	@echo "  make chart-push SERVICE=<name>"
	@echo ""
	@echo "Kubernetes:"
	@echo "  make deploy-infrastructure Deploy PostgreSQL, then NATS"
	@echo "  make deploy               Deploy all services with Helm"
	@echo "  make deploy SERVICE=<name>"
	@echo "  make status               Show pod status"
	@echo "  make services             Show Kubernetes services"
	@echo "  make logs SERVICE=<name>  Show service logs"
	@echo "  make restart SERVICE=<name>"
	@echo "  make rollout-status SERVICE=<name>"
	@echo "  make delete SERVICE=<name>"
	@echo "  make delete-e2e-apps     Remove E2E application releases only"
	@echo ""
	@echo "RKE2 Provisioning:"
	@echo "  make provision            Full end-to-end: setup + create + kubeconfig + verify"
	@echo "  make provision-rke2       Create RKE2 cluster (uses FORCE_RECREATE=1)"
	@echo "  make verify-rke2          Verify RKE2 cluster + kubeconfig"
	@echo "  make upgrade-rke2         Upgrade RKE2 cluster"
	@echo "  make delete-rke2          Delete RKE2 cluster"
	@echo "  make install-storage      Install Longhorn storage"
	@echo "  make setup-rke2           Setup SSH key + env vars only"
	@echo ""
	@echo "Examples:"
	@echo "  make docker-build"
	@echo "  make docker-build SERVICE=billing"
	@echo "  make deploy"
	@echo "  make deploy SERVICE=model-gateway"
	@echo "  make chart-push"
	@echo "  make logs SERVICE=tokenizer"
	@echo "  make provision-rke2 INSTALL_STORAGE=1"
	@echo ""


# ============================================================
# Python Dependencies
# ============================================================

.PHONY: install
install:
	$(PIP) install --upgrade pip
	@if [ -f requirements.txt ]; then $(PIP) install -r requirements.txt; fi
	@if [ -f requirements-dev.txt ]; then $(PIP) install -r requirements-dev.txt; fi


.PHONY: install-ci
install-ci:
	$(PIP) install --upgrade pip
	$(PIP) install ruff black mypy pytest pytest-cov
	@set -e; \
	if [ -n "$(SERVICE)" ]; then \
		services="$(SERVICE)"; \
	else \
		services="$(SERVICES)"; \
	fi; \
	for svc in $$services; do \
		for f in "$(API_DIR)/$$svc/requirements.txt" \
		         "$(API_DIR)/$$svc/requirements-dev.txt"; do \
			if [ ! -f "$$f" ]; then \
				echo "--- Skipping $$f (not found) ---"; \
				continue; \
			fi; \
			echo "--- Installing $$f ---"; \
			$(PIP) install -r "$$f"; \
		done; \
	done


.PHONY: install-tests
install-tests:
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev,helm]" pytest-cov || $(PIP) install -e "multistack[dev,helm]" pytest-cov


.PHONY: install-rke2-deps
install-rke2-deps:
	$(PIP) install --upgrade pip
	$(PIP) install "pydantic>=2.7" pyhelm3 "PyYAML>=6.0"
	@echo "export PYTHONPATH=$$PWD:$$PWD/multistack:\$$PYTHONPATH" > .rke2-env
	@echo "Run: source .rke2-env  (before running RKE2 targets)"


# ============================================================
# Code Quality
# ------------------------------------------------------------
# SERVICE=<name>  -> that service only
# (unset)         -> iterate over every service with src/app
# ============================================================

.PHONY: lint
lint:
	@if [ -n "$(SERVICE)" ]; then \
		dir="$(API_DIR)/$(SERVICE)"; \
		[ -d "$$dir" ] || { echo "ERROR: $$dir not found"; exit 1; }; \
		targets=""; \
		[ -d "$$dir/app" ] && targets="$$targets $$dir/app"; \
		[ -d "$$dir/src" ] && targets="$$targets $$dir/src"; \
		[ -d "$$dir/tests" ] && targets="$$targets $$dir/tests"; \
		echo "--- Ruff: $(SERVICE) ---"; \
		$(RUFF) check $$targets; \
	else \
		for svc in $(SERVICES_WITH_APP); do \
			dir="$(API_DIR)/$$svc"; \
			targets=""; \
			[ -d "$$dir/app" ] && targets="$$targets $$dir/app"; \
			[ -d "$$dir/src" ] && targets="$$targets $$dir/src"; \
			[ -d "$$dir/tests" ] && targets="$$targets $$dir/tests"; \
			echo "--- Ruff: $$svc ---"; \
			$(RUFF) check $$targets; \
		done; \
	fi


.PHONY: format
format:
	@if [ -n "$(SERVICE)" ]; then \
		dir="$(API_DIR)/$(SERVICE)"; \
		targets=""; \
		[ -d "$$dir/app" ] && targets="$$targets $$dir/app"; \
		[ -d "$$dir/src" ] && targets="$$targets $$dir/src"; \
		$(RUFF) format $$targets; \
		$(BLACK)    $$targets; \
	else \
		for svc in $(SERVICES_WITH_APP); do \
			dir="$(API_DIR)/$$svc"; \
			targets=""; \
			[ -d "$$dir/app" ] && targets="$$targets $$dir/app"; \
			[ -d "$$dir/src" ] && targets="$$targets $$dir/src"; \
			echo "--- Format: $$svc ---"; \
			$(RUFF) format $$targets; \
			$(BLACK)    $$targets; \
		done; \
	fi


.PHONY: format-check
format-check:
	@if [ -n "$(SERVICE)" ]; then \
		dir="$(API_DIR)/$(SERVICE)"; \
		targets=""; \
		[ -d "$$dir/app" ] && targets="$$targets $$dir/app"; \
		[ -d "$$dir/src" ] && targets="$$targets $$dir/src"; \
		$(RUFF) format --check $$targets; \
		$(BLACK) --check      $$targets; \
	else \
		for svc in $(SERVICES_WITH_APP); do \
			dir="$(API_DIR)/$$svc"; \
			targets=""; \
			[ -d "$$dir/app" ] && targets="$$targets $$dir/app"; \
			[ -d "$$dir/src" ] && targets="$$targets $$dir/src"; \
			echo "--- Format-check: $$svc ---"; \
			$(RUFF) format --check $$targets; \
			$(BLACK) --check      $$targets; \
		done; \
	fi


.PHONY: type-check
type-check:
	@if [ -n "$(SERVICE)" ]; then \
		dir="$(API_DIR)/$(SERVICE)"; \
		target=""; \
		[ -d "$$dir/app" ] && target="$$dir/app"; \
		[ -z "$$target" ] && [ -d "$$dir/src" ] && target="$$dir/src"; \
		[ -n "$$target" ] || { echo "no python source in $(SERVICE)"; exit 0; }; \
		$(MYPY) $$target --ignore-missing-imports; \
	else \
		for svc in $(SERVICES_WITH_APP); do \
			dir="$(API_DIR)/$$svc"; \
			target=""; \
			[ -d "$$dir/app" ] && target="$$dir/app"; \
			[ -z "$$target" ] && [ -d "$$dir/src" ] && target="$$dir/src"; \
			[ -n "$$target" ] || continue; \
			echo "--- mypy: $$svc ---"; \
			$(MYPY) $$target --ignore-missing-imports; \
		done; \
	fi


.PHONY: check
check: lint format-check type-check


.PHONY: ci-gate
ci-gate:
	@if [ "$(LINT_RESULT)" != "success" ] || [ "$(TEST_RESULT)" != "success" ] || [ "$(IMAGE_RESULT)" != "success" ]; then \
		echo "CI failed: lint=$(LINT_RESULT), test=$(TEST_RESULT), image=$(IMAGE_RESULT)"; \
		exit 1; \
	fi
	@echo "All microservice CI checks passed."


# Lint the complete SDK and all Python microservices
.PHONY: lint-sdk
lint-sdk:
	$(RUFF) check multistack/ examples/ tests/ api/microservices/

# If you want formatting checks for the same scope, add:
.PHONY: format-check-sdk
format-check-sdk:
	$(RUFF) format --check multistack/ examples/ tests/ api/microservices/
# 	$(BLACK) --check multistack/ examples/ tests/ api/microservices/

# ============================================================
# Tests
# ============================================================

.PHONY: test
test:
	@mkdir -p "$(TEST_WORKING_DIR)/junit"
	cd "$(TEST_WORKING_DIR)" && $(PYTEST) $(TEST_DIR) -v $(TEST_ARGS)

.PHONY: test-all
test-all:
	@mkdir -p test-results
	$(PYTEST) tests -v $(TEST_ALL_ARGS)

.PHONY: test-all-ci
test-all-ci: test-all

.PHONY: test-helm
test-helm:
	@mkdir -p test-results
	$(PYTEST) tests/test_charts.py -v $(TEST_ALL_ARGS)

# ============================================================
# End-to-end checks
# ============================================================

# .PHONY: api-full

# api-full:
# 	@set -e; \
# 		mkdir -p "$(E2E_RESULTS_DIR)/api"; \
# 		trap '$(DOCKER) compose -p "$(E2E_PROJECT)" -f "$(E2E_COMPOSE_FILE)" logs --no-color > "$(E2E_RESULTS_DIR)/api/compose.log" || true; $(DOCKER) compose -p "$(E2E_PROJECT)" -f "$(E2E_COMPOSE_FILE)" down -v --remove-orphans || true' EXIT; \
# 		$(DOCKER) compose -p "$(E2E_PROJECT)" -f "$(E2E_COMPOSE_FILE)" up -d --build \
# 			mock-upstream redis nats admin-postgres org-postgres tokenizer \
# 			rate-limiter-rpm rate-limiter-tpm admin-control-plane \
# 			organization-control-plane seed-admin seed-api-key gateway; \
# 		$(DOCKER) run --rm --network host \
# 			-v "$(CURDIR)/api/tests/manual-test/postman_collection.json:/etc/newman/collection.json:ro" \
# 			-v "$(CURDIR)/$(E2E_RESULTS_DIR)/api:/etc/newman/results" \
# 			postman/newman:alpine run /etc/newman/collection.json \
# 			--reporters cli,junit \
# 			--reporter-junit-export /etc/newman/results/newman-results.xml

.PHONY: api-e2e-ci
api-e2e-ci:
	cd api/tests/ci && npm ci && bash run-ci.sh

.PHONY: agentic-back

agentic-back:
	@mkdir -p "$(E2E_RESULTS_DIR)"
	cd agentic/backend && $(PYTHON) -m pytest -q --junitxml=../../$(E2E_RESULTS_DIR)/agentic-junit.xml


.PHONY: agentic-front

agentic-front:
	cd agentic/frontend && npm ci && npm run build


.PHONY: ui-admin

ui-admin:
	cd ui && npm ci && npx playwright install --with-deps chromium && npm --workspace admin-portal run test:e2e


.PHONY: ui-organization

ui-organization:
	cd ui && npm ci && npx playwright install --with-deps chromium && npm --workspace organization-portal run test:e2e


.PHONY: e2e

e2e: api-e2e-ci agentic-back agentic-front ui-admin ui-organization

# ============================================================
# Docker
# ------------------------------------------------------------
# Image path: $(REGISTRY)/<service>:$(TAG)
#   Covers $(SERVICES) plus $(PORTALS). Most services build from their own
#   directory; SDK-dependent services use the repository root, and portals
#   build from the ui/ workspace root with -f.
#   e.g. ghcr.io/<owner>/<repo>/billing:latest  (derived, see GH_OWNER/GH_REPO)
# ============================================================

.PHONY: docker-build
docker-build:
	@set -eu; \
	if [ -n "$(SERVICE)" ]; then \
		services="$(SERVICE)"; \
	else \
		services="$(DOCKER_SERVICES)"; \
	fi; \
	for service in $$services; do \
		if [ -f "$(API_DIR)/$$service/Dockerfile" ]; then \
			dockerfile="$(API_DIR)/$$service/Dockerfile"; \
			case " $(ROOT_CONTEXT_SERVICES) " in \
				*" $$service "*) context="." ;; \
				*) context="$(API_DIR)/$$service" ;; \
			esac; \
		elif [ -f "$(UI_DIR)/$$service/Dockerfile" ]; then \
			context="$(UI_DIR)"; \
			dockerfile="$(UI_DIR)/$$service/Dockerfile"; \
		else \
			if [ -n "$(SERVICE)" ]; then \
				echo "ERROR: no Dockerfile found for $$service under $(API_DIR) or $(UI_DIR)" >&2; \
				exit 1; \
			fi; \
			echo "Skipping $$service (no Dockerfile)"; \
			continue; \
		fi; \
		image="$(REGISTRY)/$$service:$(TAG)"; \
		echo "Building $$image (context=$$context, dockerfile=$$dockerfile)"; \
		if [ "$(BUILDX)" = "1" ]; then \
			output_args=(--load); \
			if [ "$(PUSH_TO_GHCR)" = "true" ]; then output_args=(--push); fi; \
			cache_args=(); \
			if [ "$(CACHE_ENABLED)" = "1" ]; then \
				cache_args=( \
					--cache-from "type=gha,scope=$$service" \
					--cache-to "type=gha,scope=$$service,mode=max" \
				); \
			fi; \
			$(DOCKER) buildx build \
				"$${cache_args[@]}" \
				"$${output_args[@]}" \
				-f "$$dockerfile" \
				-t "$$image" \
				"$$context"; \
		else \
			$(DOCKER) build \
				-f "$$dockerfile" \
				-t "$$image" \
				"$$context"; \
		fi; \
	done

.PHONY: docker-push
docker-push:
	@set -e; \
	if [ -n "$(SERVICE)" ]; then \
		$(DOCKER) push $(REGISTRY)/$(SERVICE):$(TAG); \
	else \
		for service in $(SERVICES); do \
			echo "Pushing $$service..."; \
			$(DOCKER) push $(REGISTRY)/$$service:$(TAG); \
		done; \
		for portal in $(PORTALS); do \
			echo "Pushing $$portal..."; \
			$(DOCKER) push $(REGISTRY)/$$portal:$(TAG); \
		done; \
	fi

.PHONY: docker-login
docker-login:
	@echo "Logging into $(REGISTRY)..."
	@echo "$$REGISTRY_PASSWORD" | $(DOCKER) login $$(echo $(REGISTRY) | cut -d/ -f1) -u "$$REGISTRY_USER" --password-stdin


# ============================================================
# Helm Chart Packaging / Publishing
# ------------------------------------------------------------
# Only services with chart/Chart.yaml are packaged.
# Pushes to: oci://$(CHART_REGISTRY)
#   e.g. oci://ghcr.io/<owner>/<repo>/charts/billing  (derived)
# ============================================================

.PHONY: chart-package
chart-package:
	@mkdir -p ./dist
	@if [ -n "$(SERVICE)" ]; then \
		chart="$(API_DIR)/$(SERVICE)/chart"; \
		[ -f "$$chart/Chart.yaml" ] || { echo "ERROR: $$chart/Chart.yaml missing"; exit 1; }; \
		$(HELM) package "$$chart" --destination ./dist; \
	else \
		for svc in $(SERVICES_WITH_CHART); do \
			chart="$(API_DIR)/$$svc/chart"; \
			echo "Packaging $$svc..."; \
			$(HELM) package "$$chart" --destination ./dist; \
		done; \
	fi
	@echo ""
	@echo "dist contents:"
	@ls -la ./dist

.PHONY: chart-push
chart-push: chart-package
	@if [ -n "$(SERVICE)" ]; then \
		$(HELM) push ./dist/$(SERVICE)-*.tgz $(CHART_REGISTRY); \
	else \
		for f in ./dist/*.tgz; do \
			[ -f "$$f" ] || continue; \
			echo "Pushing $$f ..."; \
			$(HELM) push "$$f" $(CHART_REGISTRY); \
		done; \
	fi


# ============================================================
# Kubernetes / Helm Deploy
# ============================================================

.PHONY: deploy
deploy: deploy-infrastructure
	@if [ -n "$(SERVICE)" ]; then \
		chart="$(API_DIR)/$(SERVICE)/chart"; \
		[ -f "$$chart/Chart.yaml" ] || { echo "ERROR: no chart for $(SERVICE)"; exit 1; }; \
		echo "Deploying $(SERVICE)..."; \
		$(HELM) upgrade --install \
			$(SERVICE) "$$chart" \
			-n $(NAMESPACE) \
			--create-namespace \
			--set-string image.repository="$(REGISTRY)/$(SERVICE)" \
			$(HELM_ARGS); \
	else \
		for service in $(SERVICES_WITH_CHART); do \
			echo ""; \
			echo "========================================="; \
			echo "Deploying $$service"; \
			echo "========================================="; \
			$(HELM) upgrade --install \
				$$service "$(API_DIR)/$$service/chart" \
				-n $(NAMESPACE) \
				--create-namespace \
				--set-string image.repository="$(REGISTRY)/$$service" \
				$(HELM_ARGS); \
		done; \
	fi

.PHONY: deploy-infrastructure
deploy-infrastructure:
	@if [ -z "$$CNPG_PASSWORD" ]; then \
		echo "ERROR: CNPG_PASSWORD is required to create the PostgreSQL cluster"; exit 1; \
	fi
	@if [ ! -f "$(MULTISTACK_KUBECONF)" ]; then \
		echo "ERROR: kubeconfig missing at $(MULTISTACK_KUBECONF)"; exit 1; \
	fi
	KUBECONFIG="$(MULTISTACK_KUBECONF)" $(KUBECTL) get storageclass longhorn >/dev/null
	KUBECONFIG="$(MULTISTACK_KUBECONF)" MULTISTACK_KUBECONFIG="$(MULTISTACK_KUBECONF)" PYTHONPATH="$(CURDIR):$(CURDIR)/multistack:$$PYTHONPATH" $(PYTHON) examples/database/create_operator.py
	KUBECONFIG="$(MULTISTACK_KUBECONF)" MULTISTACK_KUBECONFIG="$(MULTISTACK_KUBECONF)" CNPG_PASSWORD="$$CNPG_PASSWORD" PYTHONPATH="$(CURDIR):$(CURDIR)/multistack:$$PYTHONPATH" $(PYTHON) examples/database/create_pgcluster.py
	KUBECONFIG="$(MULTISTACK_KUBECONF)" $(KUBECTL) wait --for=jsonpath='{.status.phase}'='Cluster in healthy state' cluster/$${MULTISTACK_CNPG_NAME:-cnpg-test} -n $${MULTISTACK_CNPG_NS:-postgres} --timeout=20m
	KUBECONFIG="$(MULTISTACK_KUBECONF)" MULTISTACK_KUBECONFIG="$(MULTISTACK_KUBECONF)" PYTHONPATH="$(CURDIR):$(CURDIR)/multistack:$$PYTHONPATH" $(PYTHON) examples/nats/deploy_nats.py


.PHONY: cluster-info
cluster-info:
	$(KUBECTL) get nodes
	$(KUBECTL) cluster-info


# .PHONY: docker-smoke
# docker-smoke:
# 	$(DOCKER) run --rm \
# 		-e STRIPE_SECRET_KEY=sk_test_placeholder \
# 		-e STRIPE_WEBHOOK_SECRET=whsec_placeholder \
# 		-e ENABLE_STRIPE_CLI=false \
# 		-e ENVIRONMENT=test \
# 		-e DEBUG=false \
# 		$(REGISTRY)/$(or $(SERVICE),stripe-service):$(TAG) \
# 		python -c "import app.main; print('image import ok')"


.PHONY: health-check
health-check:
	@if [ -z "$(HEALTHCHECK_URL)" ]; then echo "ERROR: HEALTHCHECK_URL is required"; exit 1; fi
	$(KUBECTL) run $(HEALTHCHECK_NAME) \
		--image=curlimages/curl:latest \
		--rm -i --restart=Never \
		--namespace=$(NAMESPACE) -- \
		curl --fail --show-error --silent $(HEALTHCHECK_URL)


.PHONY: dependency-audit
dependency-audit:
	$(PIP) install pip-audit
	@if [ -n "$(SERVICE)" ]; then \
		req="$(API_DIR)/$(SERVICE)/requirements.txt"; \
		[ -f "$$req" ] || { echo "no requirements for $(SERVICE)"; exit 0; }; \
		$(PIP) install -r "$$req"; \
		pip-audit -r "$$req" --strict --progress-spinner=off; \
	else \
		for svc in $(SERVICES); do \
			req="$(API_DIR)/$$svc/requirements.txt"; \
			[ -f "$$req" ] || continue; \
			echo "--- audit: $$svc ---"; \
			$(PIP) install -r "$$req" >/dev/null; \
			pip-audit -r "$$req" --strict --progress-spinner=off || true; \
		done; \
	fi


.PHONY: security-scan
security-scan:
	$(PIP) install bandit
	@if [ -n "$(SERVICE)" ]; then \
		dir="$(API_DIR)/$(SERVICE)"; \
		[ -d "$$dir/src" ] && bandit -r "$$dir/src" -f json -o bandit-results.json; \
		[ -d "$$dir/app" ] && bandit -r "$$dir/app" -f json -o bandit-results.json; \
	else \
		bandit -r $(API_DIR)/*/src $(API_DIR)/*/app -f json -o bandit-results.json || true; \
	fi


# ============================================================
# Kubernetes Status
# ============================================================

.PHONY: status
status:
	$(KUBECTL) get pods -n $(NAMESPACE) -o wide

.PHONY: services
services:
	$(KUBECTL) get svc -n $(NAMESPACE)

.PHONY: deployments
deployments:
	$(KUBECTL) get deployments -n $(NAMESPACE)

.PHONY: images
images:
	$(KUBECTL) get deployments \
		-n $(NAMESPACE) \
		-o custom-columns='NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image'

.PHONY: logs
logs:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	$(KUBECTL) logs -n $(NAMESPACE) \
		-l app.kubernetes.io/instance=$(SERVICE) \
		--tail=200 --all-containers=true


# ============================================================
# Kubernetes Restart / Rollout / Delete
# ============================================================

.PHONY: restart
restart:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	$(KUBECTL) rollout restart deployment/$(SERVICE) -n $(NAMESPACE)

.PHONY: rollout-status
rollout-status:
	@if [ -z "$(SERVICE)" ]; then echo "ERROR: SERVICE is required"; exit 1; fi
	$(KUBECTL) rollout status \
		deployment/$(SERVICE) \
		-n $(NAMESPACE) \
		--timeout=$(ROLLOUT_TIMEOUT)

.PHONY: delete
delete:
	@if [ -n "$(SERVICE)" ]; then \
		$(HELM) uninstall $(SERVICE) -n $(NAMESPACE); \
	else \
		for service in $(SERVICES_WITH_CHART); do \
			$(HELM) uninstall $$service -n $(NAMESPACE) || true; \
		done; \
	fi

.PHONY: delete-e2e-apps
delete-e2e-apps:
	@set -e; \
	for service in admin-control-plane organization-control-plane model-gateway rate-limiter-rpm rate-limiter-tpm tokenizer billing enricher; do \
		echo "Removing application release $$service from $(NAMESPACE)..."; \
		KUBECONFIG="$(MULTISTACK_KUBECONF)" $(HELM) uninstall "$$service" \
			--namespace "$(NAMESPACE)" --ignore-not-found; \
	done


# ============================================================
# RKE2 — Setup / Delete / Provision / Verify / Storage / Upgrade
# ============================================================

.PHONY: setup-rke2
setup-rke2:
	@echo "Setting up RKE2 prerequisites"
	@if [ -z "$(SERVER)" ]; then echo "ERROR: SERVER is required"; exit 1; fi
	@if [ -z "$(SSH_USER)" ]; then echo "ERROR: SSH_USER is required"; exit 1; fi
	@if [ ! -f "$(SSH_KEY)" ]; then echo "ERROR: SSH key not found at $(SSH_KEY)"; exit 1; fi
	mkdir -p $(HOME)/.ssh $(MULTISTACK_DIR)/state
	chmod 700 $(HOME)/.ssh
	@for host in $(SERVER) $$(echo $(AGENTS) | tr ',' ' '); do \
		[ -z "$$host" ] && continue; \
		echo "  -> $$host"; \
		ssh-keyscan -H $$host >> $(HOME)/.ssh/known_hosts 2>/dev/null || true; \
	done
	@for host in $(SERVER) $$(echo $(AGENTS) | tr ',' ' '); do \
		[ -z "$$host" ] && continue; \
		echo "  -> $(SSH_USER)@$$host"; \
		$(SSH) -i $(SSH_KEY) -o StrictHostKeyChecking=no \
			$(SSH_USER)@$$host "echo 'SSH OK' && sudo -n whoami"; \
	done
	@echo "setup-rke2 complete."

.PHONY: delete-rke2
delete-rke2:
	@if [ -f "$(RKE2_EXAMPLES)/delete.py" ]; then \
		$(PYTHON) $(RKE2_EXAMPLES)/delete.py; \
	else \
		echo "WARNING: $(RKE2_EXAMPLES)/delete.py not found"; \
	fi
	rm -f $(MULTISTACK_STATE) $(MULTISTACK_KUBECONF)

.PHONY: provision-rke2
provision-rke2:
	@if [ "$(FORCE_RECREATE)" = "1" ] && [ -f "$(MULTISTACK_STATE)" ]; then \
		$(MAKE) delete-rke2 || echo "Delete returned non-zero"; \
	fi
	@if [ ! -f "$(RKE2_EXAMPLES)/cluster.py" ]; then \
		echo "ERROR: $(RKE2_EXAMPLES)/cluster.py not found"; exit 1; \
	fi
	$(PYTHON) $(RKE2_EXAMPLES)/cluster.py

.PHONY: copy-kubeconfig
copy-kubeconfig:
	@if [ ! -f "$(MULTISTACK_KUBECONF)" ]; then echo "ERROR: kubeconfig missing"; exit 1; fi
	@if [ -z "$(SERVER)" ]; then echo "ERROR: SERVER is required"; exit 1; fi
	$(SSH) -i $(SSH_KEY) -o StrictHostKeyChecking=no \
		$(SSH_USER)@$(SERVER) "mkdir -p $(REMOTE_MULTISTACK_DIR)"
	$(SCP) -i $(SSH_KEY) -o StrictHostKeyChecking=no \
		$(MULTISTACK_KUBECONF) \
		$(SSH_USER)@$(SERVER):$(REMOTE_MULTISTACK_DIR)/kubeconfig
	$(SSH) -i $(SSH_KEY) -o StrictHostKeyChecking=no \
		$(SSH_USER)@$(SERVER) \
		"chmod 600 $(REMOTE_MULTISTACK_DIR)/kubeconfig && ls -la $(REMOTE_MULTISTACK_DIR)/kubeconfig"

.PHONY: verify-rke2
verify-rke2:
	@if [ -z "$(SERVER)" ]; then echo "ERROR: SERVER is required"; exit 1; fi
	$(SSH) -i $(SSH_KEY) -o StrictHostKeyChecking=no \
		$(SSH_USER)@$(SERVER) \
		"export KUBECONFIG=$(REMOTE_MULTISTACK_DIR)/kubeconfig && \
		 $(KUBECTL) config current-context && \
		 $(KUBECTL) get nodes -o wide"
	@if [ -f "$(MULTISTACK_KUBECONF)" ]; then \
		KUBECONFIG=$(MULTISTACK_KUBECONF) $(KUBECTL) get nodes -o wide; \
	fi

.PHONY: install-storage
install-storage:
	@if [ ! -f "$(STORAGE_EXAMPLES)/install.py" ]; then \
		echo "ERROR: $(STORAGE_EXAMPLES)/install.py not found"; exit 1; \
	fi
	KUBECONFIG=$(MULTISTACK_KUBECONF) $(PYTHON) $(STORAGE_EXAMPLES)/install.py
	@for i in $$(seq 1 30); do \
		if KUBECONFIG=$(MULTISTACK_KUBECONF) $(KUBECTL) get storageclass longhorn &>/dev/null; then \
			echo "Longhorn StorageClass ready!"; break; \
		fi; \
		echo "Waiting for Longhorn... ($$i/30)"; sleep 5; \
	done
	KUBECONFIG=$(MULTISTACK_KUBECONF) $(KUBECTL) get storageclass
	KUBECONFIG=$(MULTISTACK_KUBECONF) $(KUBECTL) get pods -n longhorn-system

.PHONY: upgrade-rke2
upgrade-rke2:
	@if [ ! -f "$(RKE2_EXAMPLES)/upgrade.py" ]; then \
		echo "ERROR: $(RKE2_EXAMPLES)/upgrade.py not found"; exit 1; \
	fi
	$(PYTHON) $(RKE2_EXAMPLES)/upgrade.py

.PHONY: provision
provision: setup-rke2 provision-rke2 copy-kubeconfig verify-rke2
	@if [ "$(INSTALL_STORAGE)" = "1" ]; then $(MAKE) install-storage; fi
	@echo "RKE2 provisioning complete"


# ============================================================
# Cleanup
# ============================================================

.PHONY: clean
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf ./dist
	@echo "Cleanup completed."

.PHONY: clean-rke2
clean-rke2:
	rm -f $(MULTISTACK_STATE) $(MULTISTACK_KUBECONF)
	@echo "RKE2 local state cleaned."