# Copyright 2021 University of Chicago
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import typing

if typing.TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware
    from doni.worker import WorkerField, WorkerResult
    from oslo_config.cfg import ConfigOpts, Opt


class BaseWorker(abc.ABC):
    """A base interface implementing common functions for Driver Interfaces.

    Attributes:
        fields (list[WorkerField]): A list of fields supported and/or required
            by the worker.
    """

    fields: "list[WorkerField]" = []
    opts: "list[Opt]" = []
    opt_group: str = ""

    @abc.abstractmethod
    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        pass

    def register_opts(self, conf: "ConfigOpts"):
        conf.register_opts(self.opts)

    def list_opts(self):
        return self.opts

    def json_schema(self):
        """Get the JSON schema for validating hardware properties for this worker.

        Returns:
            The JSON schema that validates that all worker fields are present
                and valid.
        """
        return {
            "type": "object",
            "properties": {field.name: field.schema or {} for field in self.fields},
            "required": [field.name for field in self.fields if field.required],
        }

    def import_existing(self, context: "RequestContext"):
        """Get all known external state managed by this worker.

        This is an optional capability of a worker and supports an 'import' flow
        where existing resources/state outside of the doni can be brought under
        doni's management.

        The expected return type is a list of objects with a "uuid" and a
        "properties" key, representing the UUID of the hardware the state
        corresponds to (or None if one could not be reasonably determined and
        should be auto-assigned) and a set of properties that should be imported
        for that hardware item.
        """
        pass
