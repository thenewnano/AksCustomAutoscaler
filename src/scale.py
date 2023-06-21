import dataclasses
import json
import logging
import os
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import List, Literal

import kubernetes.client
from azure.identity import DefaultAzureCredential
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.containerservice.v2019_02_01.models import ManagedCluster, AgentPool
from azure.cli.core import get_default_cli

from colorlog import ColoredFormatter
from kubernetes.client import CoreV1Api
from kubernetes.config import load_kube_config

from external.azure_identity_credential_adapter import AzureIdentityCredentialAdapter

logger = logging.getLogger(__name__)
formatter = ColoredFormatter(
    '%(log_color)s%(levelname)s:%(reset)s %(message)s',
    log_colors={
        'DEBUG': 'grey',
        'INFO': 'green',
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'red,bg_white',
    })

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)
logger.setLevel(logging.INFO)


# logging.getLogger("azure").setLevel(logging.INFO)
# logging.getLogger("msrest").setLevel(logging.WARNING)
# logging.getLogger("urllib3").setLevel(logging.WARNING)
# logging.getLogger("kubernetes").setLevel(logging.WARNING)


@dataclasses.dataclass(frozen=True)
class Config:
    """
    The configuration parameters for scaling. This is stored in a JSON file and loaded at runtime called config.json.
    """
    AGENT_POOL_NAME: str
    AZURE_SUBSCRIPTION_ID: str
    AZURE_RESOURCE_GROUP_NAME: str
    AKS_CLUSTER_NAME: str
    DEFAULT_NAMESPACE: str
    MAX_POD_QUEUE: int = 10
    DELAY_BEFORE_SCALE_UP: int = 100
    DELAY_BEFORE_SCALE_DOWN: int = 300
    PERIODIC_CHECK_RATE: int = 1
    TIMEOUT: int = 60
    DEFAULT_DOWN_SCALING_STRATEGY: Literal["latest", "oldest"] = "latest"
    DEFAULT_POD_PHASE: Literal["Queued", "Pending", "Running", "Succeeded", "Failed", "Unknown"] = "Queued"


def load_config_file() -> Config:
    """
    Load the configuration file from the default location. If the file does not exist, create it and raise an error
    to allow the user to fill in the configuration parameters.

    :return Config: The configuration parameters.
    """

    config_file = Path(os.environ.get("AKS_SCALER_CONFIG_FILE", "~/.aks_scaler/config.json")).expanduser()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(config_file, "r") as f:
            run_config = json.load(f)
    except FileNotFoundError:
        with open(config_file, "w") as f:
            json.dump({prop.name: getattr(Config, prop.name, None) for prop in dataclasses.fields(Config)}, f, indent=4)
        raise FileNotFoundError(
            f"Please fill in the config file generated in {config_file} "
            f"and run the script again")
    os.environ["AZURE_SUBSCRIPTION_ID"] = run_config["AZURE_SUBSCRIPTION_ID"]
    return Config(**run_config)


def get_number_of_pods_in_phase(k8s_client: CoreV1Api, namespace: str,
                                pod_phase: str, ) -> int:
    """
    Get the number of pods in the specified phase.

    :param k8s_client:
    :param namespace:
    :param pod_phase:
    :return int: The number of pods in the specified phase.
    """
    pods = k8s_client.list_namespaced_pod(namespace=namespace, watch=False).items
    return len([pod for pod in pods if pod.status.phase == pod_phase])


def get_nodes_in_pool(k8s_client: CoreV1Api, agent_pool: AgentPool) -> List[str]:
    """
    Get the list of nodes in the agent pool.

    :param k8s_client: CoreV1Api: The Kubernetes client.
    :param agent_pool: AgentPool: The AKS agent pool to scale.
    :return List[str]: The list of nodes in the agent pool.
    """
    nodes = k8s_client.list_node(watch=False).items
    nodes_in_pool = sorted(
        [node.metadata.name for node in nodes if node.metadata.labels.get("agentpool") == agent_pool.name])
    return list(set(nodes_in_pool))


def aks_scaler(client: ContainerServiceClient, k8s_client: CoreV1Api, agent_pool: AgentPool,
               cluster: ManagedCluster,
               last_scaling_event_time: datetime,
               config_params: Config) -> datetime:
    """
    Scales the AKS agent pool based on the number of queued pods and the configured scaling parameters.

    :param client: ContainerServiceClient The Azure Container Service client.
    :param k8s_client: CoreV1Api The Kubernetes client.
    :param agent_pool: AgentPool The AKS agent pool to scale.
    :param cluster: ManagedCluster The AKS cluster containing the agent pool.
    :param last_scaling_event_time: datetime The time of the last scaling event.
    :param config_params: Config The configuration parameters for scaling.

    :return datetime: The time of the scaling event, or the time of the last scaling event if no scaling occurred.
    """
    scaling_start_time = datetime.now(tz=timezone.utc)
    scaling_event_time: datetime | None = None
    post_scale_pool_size = agent_pool.count

    # check if the autoscaling is enabled on the agent pool
    if agent_pool.enable_auto_scaling:
        logger.warning("Autoscaling is enabled on the agent pool leaving the scaling to AKS")
        return last_scaling_event_time

    # check if the agent pool is in a state that can be scaled
    if agent_pool.provisioning_state != "Succeeded":
        logger.warning("Agent pool is not in a state that can be scaled")
        return last_scaling_event_time

    nodes_pre_scale = get_nodes_in_pool(k8s_client=k8s_client, agent_pool=agent_pool)
    pre_scale_pool_size = agent_pool.count
    queued_pods = get_number_of_pods_in_phase(k8s_client=k8s_client, namespace=config_params.DEFAULT_NAMESPACE,
                                              pod_phase=config_params.DEFAULT_POD_PHASE)

    if queued_pods > config_params.MAX_POD_QUEUE:
        if (last_scaling_event_time - datetime.now(tz=timezone.utc)).seconds > config_params.DELAY_BEFORE_SCALE_UP:
            agent_pool.count += 1
            logger.info(f"Scaling up the agent pool from {pre_scale_pool_size} to {agent_pool.count}")
            post_scale_pool_size = agent_pool.count
            scaling_event_time = datetime.now(tz=timezone.utc)
    elif queued_pods < config_params.MAX_POD_QUEUE:
        if config_params.DEFAULT_DOWN_SCALING_STRATEGY == "latest":
            with suppress(IndexError):
                node_to_remove = nodes_pre_scale[-1]
        elif config_params.DEFAULT_DOWN_SCALING_STRATEGY == "oldest":
            with suppress(IndexError):
                node_to_remove = nodes_pre_scale[0]
        else:
            raise ValueError("Unknown strategy")

        # cordon the node to be removed
        if node_to_remove is not None and (last_scaling_event_time - datetime.now(tz=timezone.utc)).seconds > \
                config_params.DELAY_BEFORE_SCALE_DOWN:
            logger.info(f"Cordoned node {node_to_remove}, wait for all running pods to finish")
            k8s_client.patch_node(node_to_remove, {"spec": {"unschedulable": True}})
            while len(get_pods_running_on_node(k8s_client, node_to_remove)) > 0 and (
                    datetime.now(tz=timezone.utc) - scaling_start_time).seconds < config_params.TIMEOUT:
                logger.info(f"Waiting for pods to be evicted from {node_to_remove}")
                logger.info(get_pods_running_on_node(k8s_client, node_to_remove))
                sleep(1)
            if (datetime.now(tz=timezone.utc) - scaling_start_time).seconds < config_params.TIMEOUT:
                logger.warning(f"Timeout is reached, Node {node_to_remove} is being deleted")
            k8s_client.delete_node(node_to_remove)

            if agent_pool.count > 1:
                agent_pool.count -= 1
            else:
                aks_scale_pool_to_0(agent_pool=agent_pool, cluster=cluster,
                                    resource_group=config_params.AZURE_RESOURCE_GROUP_NAME)

            logger.info(
                f"Scaling down the agent pool from {pre_scale_pool_size} to {agent_pool.count} the node "
                f"is already deleted, just syncing the pool size")
            post_scale_pool_size = agent_pool.count
            scaling_event_time = datetime.now(tz=timezone.utc)

    if post_scale_pool_size != pre_scale_pool_size:
        client.agent_pools.create_or_update(resource_group_name=config_params.AZURE_RESOURCE_GROUP_NAME,
                                            managed_cluster_name=cluster.name, agent_pool_name=agent_pool.name,
                                            parameters=agent_pool)

    if scaling_event_time is not None:
        return scaling_event_time
    else:
        return last_scaling_event_time


def aks_scale_pool_to_0(agent_pool: AgentPool, cluster: ManagedCluster, resource_group: str) -> None:
    """
    Scale the agent pool to 0 nodes, this works around the issue with API not doing scaling down to 0

    :param agent_pool: AgentPool The AKS agent pool to scale.
    :param cluster: ManagedCluster The AKS cluster containing the agent pool.
    :param resource_group: str The name of the resource group containing the agent pool.
    :return: None
    """
    cli = get_default_cli()
    cli.invoke(["aks", "nodepool", "scale", "--resource-group", resource_group, "--cluster-name", cluster.name,
                "--name", agent_pool.name, "--node-count", "0", "--no-wait"])


def get_pods_running_on_node(k8s_client: CoreV1Api, node_to_remove: str) -> list[str]:
    """
    Get all the pods running on the node to be removed to exclude daemonsets.

    :param k8s_client: CoreV1Api The Kubernetes client.
    :param node_to_remove: str The name of the node to be removed.
    :return list[str]: The list of pods running on the node to be removed.
    """
    pods_running_on_node = k8s_client.list_namespaced_pod(namespace="monitoring", watch=False,
                                                          label_selector="kubernetes.io/created-by!=DaemonSet",
                                                          field_selector=f"spec.nodeName={node_to_remove}"
                                                                         f",status.phase=Running")
    return [pod.metadata.name for pod in pods_running_on_node.items]


def main():
    config_params = load_config_file()
    scaler_start_time = datetime.now(tz=timezone.utc)
    last_scaling_event_time = None
    client = ContainerServiceClient(
        credentials=AzureIdentityCredentialAdapter(DefaultAzureCredential()),
        subscription_id=config_params.AZURE_SUBSCRIPTION_ID,
    )
    cluster = client.managed_clusters.get(resource_group_name=config_params.AZURE_RESOURCE_GROUP_NAME,
                                          resource_name=config_params.AKS_CLUSTER_NAME)
    load_kube_config()
    k8s = kubernetes.client.CoreV1Api()
    while True:
        try:
            agent_pool = client.agent_pools.get(resource_group_name=config_params.AZURE_RESOURCE_GROUP_NAME,
                                                managed_cluster_name=cluster.name,
                                                agent_pool_name=config_params.AGENT_POOL_NAME)
            last_scaling_event_time = aks_scaler(client=client, k8s_client=k8s, agent_pool=agent_pool,
                                                 cluster=cluster,
                                                 last_scaling_event_time=last_scaling_event_time or scaler_start_time,
                                                 config_params=config_params)
        except Exception as e:
            logger.exception(e)
        sleep(config_params.PERIODIC_CHECK_RATE)


if __name__ == "__main__":
    main()
