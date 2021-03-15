# Ensure builder and runtime use same python binary
ARG python_version=3.8
FROM python:${python_version}-slim-buster as python-base
ENV VIRTUAL_ENV=/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
ENV POETRY_VERSION=1.1.5
RUN pip install "poetry==$POETRY_VERSION"

# First stage to compile deps
FROM python-base as venv-builder

RUN apt update && apt install -y --no-install-recommends \
    gcc \
    linux-libc-dev \
    libc6-dev

RUN python -m venv $VIRTUAL_ENV
COPY poetry.lock pyproject.toml ./
RUN poetry install --no-root --no-dev -vvv

# Second stage copies venv from first
FROM python-base as runtime
#create directory to mount as volume
RUN mkdir -p /work
WORKDIR /work

#use venv from builder
COPY --from=venv-builder $VIRTUAL_ENV $VIRTUAL_ENV
COPY docker/start_server.sh .
COPY doni ./doni
COPY poetry.lock pyproject.toml ./
RUN poetry install --no-dev -vvv

EXPOSE 8001
CMD [ "./start_server.sh" ]
