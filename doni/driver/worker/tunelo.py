import json
import logging
import typing

from doni.common import keystone
from doni.conf import auth as auth_conf
from doni.driver.util import ks_service_requestor
from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerResult

if typing.TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware

LOG = logging.getLogger(__name__)

_TUNELO_ADAPTER = None


def _get_tunelo_adapter():
    global _TUNELO_ADAPTER
    if not _TUNELO_ADAPTER:
        _TUNELO_ADAPTER = keystone.get_adapter(
            "tunelo",
            session=keystone.get_session("tunelo"),
            auth=keystone.get_auth("tunelo"),
        )
    return _TUNELO_ADAPTER


class TuneloWorker(BaseWorker):
    opts = []
    opt_group = "tunelo"

    def register_opts(self, conf):
        conf.register_opts(self.opts, group=self.opt_group)
        auth_conf.register_auth_opts(conf, self.opt_group, service_type="tunnel")

    def list_opts(self):
        return auth_conf.add_auth_opts(self.opts, service_type="tunnel")

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        payload = {}

        # Mapping of channel names to channel UUIDs
        channel_state = state_details.get("channels", {})

        # Mapping of channel UUIDs to their existing representations
        existing_channels = {
            c["uuid"]: c
            for c in self._call_tunelo(context, "/channels", method="get")["channels"]
        }

        # Channels which exist but we have no record of
        dangling_channels = set(existing_channels.keys()) - set(channel_state.keys())

        for channel_name, channel_props in hardware.properties["channels"].items():
            channel_uuid = channel_state.get(channel_name)
            # Recreate if representation differs
            if channel_uuid:
                existing_props = existing_channels[channel_uuid]["properties"]
                if not self._differs(channel_props, existing_props):
                    # Nothing to do, move on
                    continue
                else:
                    self._call_tunelo(
                        context, f"/channels/{channel_uuid}", method="delete"
                    )
                    LOG.info(
                        f"Channel {channel_name} changed, will re-create "
                        f"{channel_uuid} with new properties"
                    )

            channel_req = {
                # TODO: tunelo should just read this from headers, no need to send.
                "project_id": context.project_id,
                "channel_type": channel_props.get("channel_type"),
                "properties": {
                    "public_key": channel_props.get("public_key"),
                },
            }
            channel = self._call_tunelo(
                context, "/channels", method="post", json=json.dumps(channel_req)
            )
            LOG.info(f"Created new {channel_name} channel at {channel['uuid']}")
            payload[channel_name] = channel["uuid"]

        for channel_uuid in dangling_channels:
            self._call_tunelo(f"/channels/{channel_uuid}", method="delete")
            LOG.info(f"Deleted dangling channel {channel_uuid}")

        return WorkerResult.Success(payload)

    def _differs(self, chan_a, chan_b):
        for key in ["channel_type", "public_key"]:
            if chan_a.get(key) != chan_b.get(key):
                return True
        return False

    def _call_tunelo(self, *args, **kwargs):
        return ks_service_requestor("Tunelo", _get_tunelo_adapter)(*args, **kwargs)
