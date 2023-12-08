DOCKER_REGISTRY ?= docker.chameleoncloud.org
DOCKER_IMAGE = $(DOCKER_REGISTRY)/doni:latest

.PHONY: setup
setup:
	tox -e dev

.PHONY: clean
clean:
	rm -rf .venv && \
	rm -rf .pytest_cache && \
	rm -rf .tox && \
	rm -rf build && \
	rm -rf dist && \
	rm -rf doni.egg-info && \
	rm -rf instance

.PHONY: test
test:
	tox
