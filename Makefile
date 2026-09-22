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

NAMESPACE ?= multistack

# Image/chart registry root — the service name is appended per-command.
REGISTRY ?= ghcr.io/multicorewareinc/m-stack
CHART_REGISTRY ?= oci://ghcr.io/multicorewareinc/m-stack/charts
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
	stripe-service \
	tokenizer

SERVICE_CONTEXT_mock-upstream := api/tests/model-gateway/e2e/mock-upstream

define service_context
$(if $(SERVICE_CONTEXT_$(1)),$(SERVICE_CONTEXT_$(1)),$(API_DIR)/$(1))
endef

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
	OWNER=$$(echo "$${GITHUB_REPOSITORY_OWNER:-multicorewareinc}" | tr '[:upper:]' '[:lower:]'); \
	REPO_NAME=$${GITHUB_REPOSITORY##*/}; \
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
	@echo "  make deploy               Deploy all services with Helm"
	@echo "  make deploy SERVICE=<name>"
	@echo "  make status               Show pod status"
	@echo "  make services             Show Kubernetes services"
	@echo "  make logs SERVICE=<name>  Show service logs"
	@echo "  make restart SERVICE=<name>"
	@echo "  make rollout-status SERVICE=<name>"
	@echo "  make delete SERVICE=<name>"
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
# Docker
# ------------------------------------------------------------
# Image path: $(REGISTRY)/<service>:$(TAG)
#   e.g. ghcr.io/multicorewareinc/m-stack/billing:latest
# ============================================================

.PHONY: docker-build
docker-build:
	@if [ -n "$(SERVICE)" ]; then \
		echo "Building Docker image for $(SERVICE)..."; \
		$(DOCKER) build \
			-t $(REGISTRY)/$(SERVICE):$(TAG) \
			$(call service_context,$(SERVICE)); \
	else \
		for service in $(SERVICES); do \
			context="$(API_DIR)/$$service"; \
			[ -d "$$context" ] || { echo "skip $$service (no dir)"; continue; }; \
			echo ""; \
			echo "========================================="; \
			echo "Building $$service"; \
			echo "========================================="; \
			$(DOCKER) build \
				-t $(REGISTRY)/$$service:$(TAG) \
				$$context; \
		done; \
	fi

.PHONY: docker-push
docker-push:
	@if [ -n "$(SERVICE)" ]; then \
		$(DOCKER) push $(REGISTRY)/$(SERVICE):$(TAG); \
	else \
		for service in $(SERVICES); do \
			echo "Pushing $$service..."; \
			$(DOCKER) push $(REGISTRY)/$$service:$(TAG); \
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
#   e.g. oci://ghcr.io/multicorewareinc/m-stack/charts/billing
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
deploy:
	@if [ -n "$(SERVICE)" ]; then \
		chart="$(API_DIR)/$(SERVICE)/chart"; \
		[ -f "$$chart/Chart.yaml" ] || { echo "ERROR: no chart for $(SERVICE)"; exit 1; }; \
		echo "Deploying $(SERVICE)..."; \
		$(HELM) upgrade --install \
			$(SERVICE) "$$chart" \
			-n $(NAMESPACE) \
			--create-namespace \
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
				$(HELM_ARGS); \
		done; \
	fi


.PHONY: cluster-info
cluster-info:
	$(KUBECTL) get nodes
	$(KUBECTL) cluster-info


.PHONY: docker-smoke
docker-smoke:
	$(DOCKER) run --rm \
		-e STRIPE_SECRET_KEY=sk_test_placeholder \
		-e STRIPE_WEBHOOK_SECRET=whsec_placeholder \
		-e ENABLE_STRIPE_CLI=false \
		-e ENVIRONMENT=test \
		-e DEBUG=false \
		$(REGISTRY)/$(or $(SERVICE),stripe-service):$(TAG) \
		python -c "import app.main; print('image import ok')"


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