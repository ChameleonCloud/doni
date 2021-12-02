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
    from doni.worker import WorkerField


class BaseHardwareType(abc.ABC):
    """A base hardware type.

    A hardware type is a collection of workers considered valid for that type,
    and an optional list of default fields, which should be applied during any
    Hardware update or create operation.

    Attributes:
        enabled_workers (list[str]): A list of which workers can be enabled for
            this hardware type.
        default_fields (list[WorkerField]): A list of worker fields that apply
            to this hardware type generically.
        worker_overrides (dict): A dict of worker field names to the values that should
            be overridden on the worker. This allows a hardware type to require that
            a given worker field always has some set value, and prohibits the end-user
            from choosing a different value.
    """

    enabled_workers: "list[str]" = ()
    default_fields: "list[WorkerField]" = []
    worker_overrides: "dict" = {}
