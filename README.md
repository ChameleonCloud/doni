[![Unit Tests](https://github.com/ChameleonCloud/doni/actions/workflows/test.yml/badge.svg?branch=chameleoncloud%2Fxena&event=push)](https://github.com/ChameleonCloud/doni/actions/workflows/test.yml)


# doni

Chameleon hardware registration and enrollment service

## Development

### Dependencies

- [Poetry](https://getpoetry.org): `pip install poetry`
  > Note: this is not strictly necessary as most of the development can
  > be accommodated solely via Docker. But, if you want to be running
  > auto-formatting and using the local `black` lint rules, Poetry is
  > used to install those.
- [tox](https://tox.readthedocs.io/en/latest/): `pip install tox`
  > For running unit tests locally.
- Docker, Docker Compose

### Installing dependencies for IDE (e.g. VSCode)

The `setup` target will just install all the project runtime and development
dependencies into a local virtualenv using Poetry. You can then configure the
IDE to point to the `.venv` directory created by Poetry as your Python
interpreter.

```shell
make setup
```

### Running a local development server

Run the `start` Make target to bring up a Docker Compose development
environment. The Flask application server should reload whenever any file is
changed.

```shell
make start
```

### Running unit tests

```shell
make test
```

### Adding new DB migrations

Adding a new DB migration is necessary whenever there are changes to the
`models.py` file that, e.g., change, add, or remove a column or constraint.
Similarly, a migration is necessary whenever new tables are added.

This can be a bit tricky due to the fact that the local DB uses SQLite, which
doesn't have support for some iterative migrations we use. The best procedure
is to use the `create_schema` command to re-create a local SQLite DB from a
complete schema at the last recorded state, then have sqlalchemy figure out how
to map your changes to a migration via the autogenerate capability.

Here's a step-by-step:

```shell
# Stash any local changes to the models.py file
git stash
# Reset the local alembic DB, if any.
rm -f doni/doni.sqlite
# Snapshot the current schema
poetry run doni-dbsync create_schema

# Bring back local changes
git stash pop
# Auto-generate the migration file
poetry run doni-dbsync revision --message "some_description_with_underscores" --autogenerate
```
