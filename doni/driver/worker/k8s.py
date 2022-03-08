from typing import TYPE_CHECKING

from kubernetes import config, client
from oslo_config.cfg import StrOpt, DictOpt

from doni.conf import CONF
from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerResult

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware

_KUBERNETES_CLIENT = None


def kubernetes_client():
    global _KUBERNETES_CLIENT
    if not _KUBERNETES_CLIENT:
        config.load_kube_config(config_file=CONF.k8s.kubeconfig_file)
        _KUBERNETES_CLIENT = client.CoreV1Api()
    return _KUBERNETES_CLIENT


class K8sWorker(BaseWorker):
    opts = [
        StrOpt("kubeconfig_file", help="Kubeconfig file to use for calls to k8s"),
        StrOpt(
            "expected_labels_index_property",
            default="machine_name",
            help=(
                "The property name to use to index into the ``expected_labels`` "
                "configuration."
            ),
        ),
        DictOpt(
            "expected_labels",
            help=(
                "A mapping of the hardware property index key to a set of labels that "
                "should exist for k8s nodes associated w/ the hardware."
            ),
        ),
    ]
    opt_group = "k8s"

    def process(
        self,
        context: "RequestContext",
        hardware: "Hardware",
        availability_windows: "list[AvailabilityWindow]" = None,
        state_details: "dict" = None,
    ) -> "WorkerResult.Base":
        core_v1 = kubernetes_client()

        idx_property = CONF.k8s.expected_labels_index_property
        idx = hardware.properties.get(idx_property)
        if not idx:
            raise ValueError(f"Missing {idx_property} on hardware {hardware.uuid}")

        expected_labels = CONF.k8s.expected_labels.get(idx)
        # Expand config structure from "key1=value1,key2=value2" to dict
        expected_labels = {
            key: value
            for label_spec in (expected_labels.split(",") if expected_labels else [])
            for key, value in label_spec.split("=")
        }

        payload = {}
        if expected_labels:
            try:
                core_v1.patch_node(
                    hardware.name,
                    {
                        "metadata": {
                            "labels": expected_labels,
                        }
                    },
                )
            except client.ApiException as exc:
                if exc.status == 404:
                    return WorkerResult.Defer(reason="No matching k8s node found")
                else:
                    raise
            payload["num_labels"] = len(expected_labels)
        else:
            payload["num_labels"] = 0

        return WorkerResult.Success()
