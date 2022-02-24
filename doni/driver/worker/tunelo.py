import json
import logging
import typing

from oslo_log import log

from doni.common import keystone
from doni.conf import auth as auth_conf
from doni.driver.util import ks_service_requestor
from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerResult

if typing.TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware
    from keystoneauth1.adapter import Adapter

LOG = log.getLogger(__name__)

_TUNELO_ADAPTER: "Adapter" = None


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
        auth_conf.register_auth_opts(conf, self.opt_group, service_type="channel")

    def list_opts(self):
        return auth_conf.add_auth_opts(self.opts, service_type="channel")

    def _to_state_details(self, channel):
        return {
            "uuid": channel["uuid"],
            "peers": [peer["properties"] for peer in channel["peers"]],
            "endpoint": channel["properties"].get("endpoint"),
            "ip": channel["properties"].get("ip"),
        }

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        # Mapping of channel names to channel UUIDs
        channel_state: dict = state_details.setdefault("channels", {})
        # Backwards compatibility for before more properties were stored here.
        for channel_name in channel_state.keys():
            if isinstance(channel_state[channel_name], str):
                channel_state[channel_name] = {"uuid": channel_state[channel_name]}

        # Mapping of channel UUIDs to their existing representations
        existing_channels = {
            c["uuid"]: c
            for c in self._call_tunelo(context, "/channels", method="get")["channels"]
        }

        # Channels which exist but we have no record of
        dangling_channels = set(existing_channels.keys()) - set(
            [channel["uuid"] for channel in channel_state.values()]
        )
        hw_channels = hardware.properties.get("channels", {})
        for channel_name, channel_props in hw_channels.items():
            stored_channel = channel_state.get(channel_name)
            if stored_channel:
                channel_uuid = stored_channel["uuid"]
                existing = existing_channels[channel_uuid]
                if not self._differs(channel_props, existing):
                    # Nothing to do, move on, but save current channel details
                    channel_state[channel_name] = self._to_state_details(existing)
                    continue
                else:
                    # Recreate if representation differs
                    self._call_tunelo(
                        context, f"/channels/{channel_uuid}", method="delete"
                    )
                    LOG.info(
                        f"Channel {channel_name} changed, will re-create "
                        f"{channel_uuid} with new properties"
                    )

            channel_req = {
                # TODO: tunelo should infer this from auth headers
                "project_id": _get_tunelo_adapter().get_project_id(),
                "channel_type": channel_props.get("channel_type"),
                "properties": {
                    "public_key": channel_props.get("public_key"),
                },
            }
            channel = self._call_tunelo(
                context, "/channels", method="post", json=channel_req
            )
            LOG.info(f"Created new {channel_name} channel at {channel['uuid']}")
            channel_state[channel_name] = self._to_state_details(channel)

        for channel_uuid in dangling_channels:
            self._call_tunelo(context, f"/channels/{channel_uuid}", method="delete")
            LOG.info(f"Deleted dangling channel {channel_uuid}")

        return WorkerResult.Success(state_details)

    def _differs(self, chan_a, tunelo_channel):
        if chan_a["channel_type"] != tunelo_channel["channel_type"]:
            return True
        elif chan_a["public_key"] != tunelo_channel["properties"]["public_key"]:
            return True
        return False

    def _call_tunelo(self, *args, **kwargs):
        return ks_service_requestor(
            "Tunelo", _get_tunelo_adapter, parse_error=_parse_tunelo_error
        )(*args, **kwargs)


def _parse_tunelo_error(response_body):
    try:
        return json.loads(response_body).get("error")
    except json.JSONDecodeError:
        return response_body
