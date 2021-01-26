DOCKER_REGISTRY ?= docker.chameleoncloud.org
DOCKER_IMAGE = $(DOCKER_REGISTRY)/doni:latest

.PHONY: setup
setup:
	poetry install

.PHONY: build
build:
	docker build -t $(DOCKER_IMAGE) -f docker/Dockerfile .

.PHONY: publish
publish:
	docker push $(DOCKER_IMAGE)

.PHONY: start
start:
	docker-compose up --build
