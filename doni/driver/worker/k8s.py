import base64

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from kubernetes import config, client
from oslo_config.cfg import StrOpt, DictOpt, BoolOpt
from oslo_log import log


from doni.conf import CONF
from doni.driver.worker.base import BaseWorker
from doni.worker import WorkerResult
from doni.driver.util import generate_k8s_bootstrap_token

if TYPE_CHECKING:
    from doni.common.context import RequestContext
    from doni.objects.availability_window import AvailabilityWindow
    from doni.objects.hardware import Hardware

# Kubernetes 10.x/12.x support
try:
    K8sApiException = client.ApiException  # >=12.x
except:
    K8sApiException = client.api_client.ApiException

_KUBERNETES_CLIENT = None

LOG = log.getLogger(__name__)

def kubernetes_client():
    global _KUBERNETES_CLIENT
    if not _KUBERNETES_CLIENT:
        config.load_kube_config(config_file=CONF.k8s.kubeconfig_file)
        _KUBERNETES_CLIENT = client.CoreV1Api()
    return _KUBERNETES_CLIENT


class K8sWorker(BaseWorker):
    opts = [
        StrOpt("kubeconfig_file", help="Kubeconfig file to use for calls to k8s"),
        BoolOpt("enable_worker_taint", default=False, help="Enables the tainting of K8S worker nodes"),
        StrOpt('worker_taint_key', help="The key for the taint"),
        StrOpt('worker_taint_value', help="The value for the taint"),
        StrOpt('worker_taint_effect', help="The effect for the taint"),
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

        payload = {}

        # Bootstrap token creation/deletion
        payload["deleted_token_secrets"] = 0
        payload["created_token_secrets"] = 0
        payload["issued new token"] = "null"

        if 'k8s_bootstrap_token' not in hardware.properties or hardware.properties.get("k8s_bootstrap_token") == "":
            LOG.info(f"Missing Token for device '{hardware.name}'. Issuing token.")
            new_token = generate_k8s_bootstrap_token()

            hardware.properties["k8s_bootstrap_token"] = new_token
            hardware.save()

            payload["issued_new_token"] = new_token

        bootstrap_token = hardware.properties.get("k8s_bootstrap_token")
        token_id, token_secret = bootstrap_token.split('.')
        secret_name = f"bootstrap-token-{token_id}"

        if hardware.deleted:
            payload["deleted_token_secrets"] += self._delete_bootstrap_token_secret(secret_name)
            return WorkerResult.Success(payload)

        if not self._check_secret_exists(secret_name):
            payload["created_token_secrets"] += self._create_bootstrap_token_secret(token_id, token_secret)


        if CONF.k8s.enable_worker_taint:
            # Tainting the worker node if not tainted
            self._validate_taint(CONF.k8s.worker_taint_key,
                                CONF.k8s.worker_taint_value,
                                CONF.k8s.worker_taint_effect)

            taint = client.V1Taint(
                key=CONF.k8s.worker_taint_key,
                value=CONF.k8s.worker_taint_value,
                effect=CONF.k8s.worker_taint_effect
            )

            if self._add_taint_to_node(hardware.name, taint):
                LOG.info(f"Taint {taint.key}={taint.value}:{taint.effect} added to node {hardware.name}")
                payload["Added worker_node taint"] = True
            else:
                LOG.info(f"Taint {taint.key}={taint.value}:{taint.effect} already exists on node {hardware.name}")
                payload["Added worker_node taint"] = False

        # Label Patching
        idx_property = CONF.k8s.expected_labels_index_property
        idx = hardware.properties.get(idx_property)
        if not idx:
            raise ValueError(f"Missing {idx_property} on hardware {hardware.uuid}")

        expected_labels = CONF.k8s.expected_labels.get(idx)
        labels = {}
        # Expand config structure from "key1=value1,key2=value2" to dict
        for label_spec in expected_labels.split("|") if expected_labels else []:
            label, value = label_spec.split("=")
            labels[label] = value

        # handle egress toggle
        local_egress = hardware.properties.get("local_egress")
        if local_egress == "deny":
            labels["chi.edge/local_egress"] = "deny"

        if labels:
            try:
                core_v1.patch_node(hardware.name, {"metadata": {"labels": labels}})
            except K8sApiException as exc:
                if exc.status == 404:
                    return WorkerResult.Defer(reason="No matching k8s node found")
                else:
                    raise
            payload["num_labels"] = len(labels)
        else:
            payload["num_labels"] = 0

        return WorkerResult.Success(payload)


    def _validate_taint(self, key, value, effect):
        if not key or not isinstance(key, str):
            raise ValueError("K8S Taint key must be a non-empty string")

        if not value or not isinstance(value, str):
            raise ValueError("K8S Taint value must be a non-empty string")

        valid_effects = {"NoSchedule", "PreferNoSchedule", "NoExecute"}
        if effect not in valid_effects:
            raise ValueError(f"K8S Taint effect must be one of {valid_effects}")

    def _taint_exists(self, node, taint):
        if node.spec.taints:
            for t in node.spec.taints:
                if t.key == taint.key and t.value == taint.value and t.effect == taint.effect:
                    return True
        return False

    def _add_taint_to_node(self, node_name, taint):
        try:
            core_v1 = client.CoreV1Api()
            node = core_v1.read_node(node_name)

            if not self._taint_exists(node, taint):
                if node.spec.taints is None:
                    node.spec.taints = []
                node.spec.taints.append(taint)
                core_v1.patch_node(node_name, node)
                return True
            else:
                return False
        except K8sApiException as e:
            LOG.error(f"Error adding taint to node: {e.body}")
            return False


    def _check_secret_exists(self, secret_name):
        try:
            core_v1 = kubernetes_client()
            core_v1.read_namespaced_secret(name=secret_name, namespace="kube-system")
            return True
        except client.rest.ApiException as e:
            if e.status == 404:
                return False

    def _create_bootstrap_token_secret(self, token_id, token_secret):
        try:
            core_v1 = kubernetes_client()

            # Token expiry date is 7 days from enrollment of device
            expiry_date = datetime.utcnow() + timedelta(days=7)
            expiry_string = expiry_date.strftime("%Y-%m-%dT%H:%M:%SZ")

            secret_data = {
                "description": f"Bootstrap token generated by doni k8s worker",
                "token-id": token_id,
                "token-secret": token_secret,
                "expiration": expiry_string,
                "usage-bootstrap-signing": "true",
                "usage-bootstrap-authentication": "true",
            }

            # Encode the secret data in b64
            encoded_secret_data = {k: base64.b64encode(v.encode()).decode() for k, v in secret_data.items()}

            secret = client.V1Secret(
                api_version="v1",
                kind="Secret",
                metadata=client.V1ObjectMeta(name=f"bootstrap-token-{token_id}", namespace="kube-system"),
                type="bootstrap.kubernetes.io/token",
                data=encoded_secret_data
            )

            core_v1.create_namespaced_secret(namespace="kube-system", body=secret)

            LOG.info(f"Created secret for token id {token_id}")
            return 1
        except K8sApiException as e:
            LOG.error(f"Error creating bootstrap token secret: {e.body}")
            return 0

    def _delete_bootstrap_token_secret(self, secret_name):
        try:
            core_v1 = kubernetes_client()
            core_v1.delete_namespaced_secret(name=secret_name, namespace="kube-system")

            LOG.info(f"Deleted secret {secret_name}")
            return 1
        except K8sApiException as e:
            LOG.error(f"Error deleting bootstrap token secret: {e.body}")
            return 0
