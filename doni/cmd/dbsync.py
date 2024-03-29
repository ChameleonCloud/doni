"""
Run storage database migration.
"""

import sys

from oslo_config import cfg

from doni.common import context
from doni.common import service
from doni.conf import CONF
from doni.db import api as db_api
from doni.db import migration


dbapi = db_api.get_instance()

# NOTE(rloo): This is a list of functions to perform online data migrations
# (from previous releases) for this release, in batches. It may be empty.
# The migration functions should be ordered by execution order; from earlier
# to later releases.
#
# Each migration function takes two arguments -- the context and maximum
# number of objects to migrate, and returns a 2-tuple -- the total number of
# objects that need to be migrated at the beginning of the function, and the
# number migrated. If the function determines that no migrations are needed,
# it returns (0, 0).
#
# The last migration step should always remain the last one -- it migrates
# all objects to their latest known versions.
#
# NOTE(vdrok): Do not access objects' attributes, instead only provide object
# and attribute name tuples, so that not to trigger the load of the whole
# object, in case it is lazy loaded. The attribute will be accessed when needed
# by doing getattr on the object
ONLINE_MIGRATIONS = ()

# These are the models added in supported releases. We skip the version check
# for them since the tables do not exist when it happens.
NEW_MODELS = []


class DBCommand(object):
    def upgrade(self):
        migration.upgrade(CONF.command.revision)

    def revision(self):
        migration.revision(CONF.command.message, CONF.command.autogenerate)

    def stamp(self):
        migration.stamp(CONF.command.revision)

    def version(self):
        print(migration.version())

    def create_schema(self):
        migration.create_schema()

    def online_data_migrations(self):
        self._run_online_data_migrations(
            max_count=CONF.command.max_count, options=CONF.command.options
        )

    def _run_migration_functions(self, context, max_count, options):
        """Runs the migration functions.

        Runs the data migration functions in the ONLINE_MIGRATIONS list.
        It makes sure the total number of object migrations doesn't exceed the
        specified max_count. A migration of an object will typically migrate
        one row of data inside the database.

        Args:
            context (RequestContext): an admin context.
            max_count (int): The maximum number of objects (rows) to migrate;
                a value >= 1.
            options (dict): migration options - dict mapping migration name
                to a dictionary of options for this migration.

        Returns:
            Boolean value indicating whether migrations are done.

            Returns False if max_count objects have been migrated (since at that
            point, it is unknown whether all migrations are done). Returns
            True if migrations are all done (i.e. fewer than max_count objects
            were migrated when the migrations are done).

        Raises:
            Exception: any exception from the migration function.
        """
        total_migrated = 0

        for migration_func_obj, migration_func_name in ONLINE_MIGRATIONS:
            migration_func = getattr(migration_func_obj, migration_func_name)
            migration_opts = options.get(migration_func_name, {})
            num_to_migrate = max_count - total_migrated
            try:
                total_to_do, num_migrated = migration_func(
                    context, num_to_migrate, **migration_opts
                )
            except Exception as e:
                print(
                    ("Error while running %(migration)s: %(err)s.")
                    % {"migration": migration_func.__name__, "err": e},
                    file=sys.stderr,
                )
                raise

            print(
                ("%(migration)s() migrated %(done)i of %(total)i objects.")
                % {
                    "migration": migration_func.__name__,
                    "total": total_to_do,
                    "done": num_migrated,
                }
            )
            total_migrated += num_migrated
            if total_migrated >= max_count:
                # NOTE(rloo). max_count objects have been migrated so we have
                # to stop. We return False because there is no look-ahead so
                # we don't know if the migrations have been all done. All we
                # know is that we've migrated max_count. It is possible that
                # the migrations are done and that there aren't any more to
                # migrate after this, but that would involve checking:
                #   1. num_migrated == total_to_do (easy enough), AND
                #   2. whether there are other migration functions and whether
                #      they need to do any object migrations (not so easy to
                #      check)
                return False

        return True

    def _run_online_data_migrations(self, max_count=None, options=None):
        """Perform online data migrations for the release.

        Online data migrations are done by running all the data migration
        functions in the ONLINE_MIGRATIONS list. If max_count is None, all
        the functions will be run in batches of 50 objects, until the
        migrations are done. Otherwise, this will run (some of) the functions
        until max_count objects have been migrated.

        Args:
            max_count (int): the maximum number of individual object migrations
                or modified rows, a value >= 1. If None, migrations are run in a
                loop in batches of 50, until completion.
            options (dict): options to pass to migrations. List of values in the
                form of <migration name>.<option>=<value>

        Raises:
            SystemExit: With exit code of:
                0: when all migrations are complete.
                1: when objects were migrated and the command needs to be
                re-run (because there might be more objects to be migrated)
                127: if max_count is < 1 or any option is invalid
            Exception: from any exception from a migration function.
        """
        parsed_options = {}
        if options:
            for option in options:
                try:
                    migration, key_value = option.split(".", 1)
                    key, value = key_value.split("=", 1)
                except ValueError:
                    print(("Malformed option %s") % option)
                    sys.exit(127)
                else:
                    parsed_options.setdefault(migration, {})[key] = value

        admin_context = context.get_admin_context()
        finished_migrating = False
        if max_count is None:
            max_count = 50
            print(
                ("Running batches of %i until migrations have been " "completed.")
                % max_count
            )
            while not finished_migrating:
                finished_migrating = self._run_migration_functions(
                    admin_context, max_count, parsed_options
                )
            print(("Data migrations have completed."))
            sys.exit(0)

        if max_count < 1:
            print(('"max-count" must be a positive value.'), file=sys.stderr)
            sys.exit(127)

        finished_migrating = self._run_migration_functions(
            admin_context, max_count, parsed_options
        )
        if finished_migrating:
            print(("Data migrations have completed."))
            sys.exit(0)
        else:
            print(("Data migrations have not completed. Please re-run."))
            sys.exit(1)


def add_command_parsers(subparsers):
    command_object = DBCommand()

    parser = subparsers.add_parser(
        "upgrade",
        help=(
            "Upgrade the database schema to the latest version. "
            "Optionally, use --revision to specify an alembic revision "
            "string to upgrade to. It returns 2 (error) if the database is "
            "not compatible with this version. If this happens, the "
            "'doni-dbsync online_data_migrations' command should be run "
            "using the previous version of doni, before upgrading and "
            "running this command."
        ),
    )

    parser.set_defaults(func=command_object.upgrade)
    parser.add_argument("--revision", nargs="?")

    parser = subparsers.add_parser("stamp")
    parser.add_argument("--revision", nargs="?")
    parser.set_defaults(func=command_object.stamp)

    parser = subparsers.add_parser(
        "revision",
        help=(
            "Create a new alembic revision. " "Use --message to set the message string."
        ),
    )
    parser.add_argument("-m", "--message")
    parser.add_argument("--autogenerate", action="store_true")
    parser.set_defaults(func=command_object.revision)

    parser = subparsers.add_parser(
        "version", help=("Print the current version information and exit.")
    )
    parser.set_defaults(func=command_object.version)

    parser = subparsers.add_parser(
        "create_schema", help=("Create the database schema.")
    )
    parser.set_defaults(func=command_object.create_schema)

    parser = subparsers.add_parser(
        "online_data_migrations",
        help=(
            "Perform online data migrations for the release. If "
            "--max-count is specified, at most max-count objects will be "
            "migrated. If not specified, all objects will be migrated "
            "(in batches to avoid locking the database for long periods of "
            "time). "
            "The command returns code 0 (success) after migrations are "
            "finished or there are no data to migrate. It returns code "
            "1 (error) if there are still pending objects to be migrated. "
            "Before upgrading to a newer release, this command must be run "
            "until code 0 is returned. "
            "It returns 127 (error) if max-count is < 1. "
            "It returns 2 (error) if the database is not compatible with "
            "this release. If this happens, this command should be run "
            "using the previous release of doni, before upgrading and "
            "running this command."
        ),
    )
    parser.add_argument(
        "--max-count",
        metavar="<number>",
        dest="max_count",
        type=int,
        help=(
            "Maximum number of objects to migrate. If unspecified, all "
            "objects are migrated."
        ),
    )
    parser.add_argument(
        "--option",
        metavar="<migration.opt=val>",
        action="append",
        dest="options",
        default=[],
        help=(
            "Options to pass to the migrations in the form of "
            "<migration name>.<option>=<value>"
        ),
    )
    parser.set_defaults(func=command_object.online_data_migrations)


def main():
    command_opt = cfg.SubCommandOpt(
        "command",
        title="Command",
        help=("Available commands"),
        handler=add_command_parsers,
    )

    CONF.register_cli_opt(command_opt)

    service.prepare_service(sys.argv)
    CONF.command.func()
