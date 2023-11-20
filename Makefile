DOCKER_REGISTRY ?= docker.chameleoncloud.org
DOCKER_IMAGE = $(DOCKER_REGISTRY)/doni:latest

.PHONY: setup
setup:
	python3 -m venv .venv
	.venv/bin/pip install \
		-c https://releases.openstack.org/constraints/upper/xena \
		.[balena,dev]

.PHONY: build
build:
	docker build -t $(DOCKER_IMAGE) -f docker/Dockerfile .

.PHONY: publish
publish:
	docker push $(DOCKER_IMAGE)

.PHONY: start
start:
	docker-compose up --build

.PHONY: test
test:
	tox
