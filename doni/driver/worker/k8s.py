import base64

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from kubernetes import config, client
from oslo_config.cfg import StrOpt, DictOpt
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

        secret_exists = self._check_secret_exists(secret_name)

        if secret_exists:
            LOG.info(f"Valid Secret '{secret_name}' for device '{hardware.name}' already exists. Skipping creation.")
        else:
            payload["created_token_secrets"] += self._create_bootstrap_token_secret(token_id, token_secret)

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
