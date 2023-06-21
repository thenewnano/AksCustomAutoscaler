import pytest
from unittest.mock import MagicMock
from kubernetes.client import CoreV1Api
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.containerservice.models import ManagedCluster, AgentPool
from scale import get_number_of_pods_in_phase, get_nodes_in_pool, aks_scaler

@pytest.fixture
def k8s_client():
    return MagicMock(spec=CoreV1Api)

@pytest.fixture
def container_service_client():
    return MagicMock(spec=ContainerServiceClient)

@pytest.fixture
def agent_pool():
    return AgentPool(name="pool1")

@pytest.fixture
def cluster():
    return ManagedCluster(id="/subscriptions/123/resourceGroups/rg1/providers/Microsoft.ContainerService/managedClusters/cluster1")

def test_get_number_of_pods_in_phase(k8s_client):
    k8s_client.list_namespaced_pod.return_value.items = [
        MagicMock(status=MagicMock(phase="Running")),
        MagicMock(status=MagicMock(phase="Pending")),
        MagicMock(status=MagicMock(phase="Running")),
        MagicMock(status=MagicMock(phase="Failed")),
    ]
    assert get_number_of_pods_in_phase(k8s_client, "default", "Running") == 2

def test_get_nodes_in_pool(k8s_client, agent_pool):
    k8s_client.list_node.return_value.items = [
        MagicMock(metadata=MagicMock(name="node1", labels={"agentpool": "pool1"})),
        MagicMock(metadata=MagicMock(name="node2", labels={"agentpool": "pool2"})),
        MagicMock(metadata=MagicMock(name="node3", labels={"agentpool": "pool1"})),
    ]
    assert get_nodes_in_pool(k8s_client, agent_pool) == ["node1", "node3"]

def test_aks_scaler(container_service_client, k8s_client, agent_pool, cluster):
    last_scaling_event_time = None
    config_params = MagicMock()
    aks_scaler(container_service_client, k8s_client, agent_pool, cluster, last_scaling_event_time, config_params)
    container_service_client.container_services.get.assert_called_once_with(
        resource_group_name="rg1", container_service_name="cluster1")