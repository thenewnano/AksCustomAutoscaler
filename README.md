# AKS Custom Autoscaler
Not readu for production use yet, use at your own risk.
Aks_custom_autoscaler includes `scale.py` which is a Python service that provides functionality for scaling an
Azure Kubernetes Service (AKS) cluster by adding or removing nodes from an agent pool.
It differs from the standard cluster autoscaler in the notion that it, is decoupled from the cluster scheduler. 
as a standalone service, outside the cluster context you can achieve a more fine-grained control over the 
scaling process, and you can also use it spin up a node that is tainted to it can't take any pods, run some 
pods/deamonsets on it to prepare the node, like copy a big amount of data to the node and later share it to the pods using a HOSTPATH volumes etc. 
When the Node is ready you can untaint it and let the scheduler take over.

## Prerequisites

Before using `scale.py`, you must have the following:

- An Azure subscription
- An AKS cluster
- The Azure CLI installed on your local machine
- Python 3.10 or later installed on your local machine
- for Python package dependencies, see Pipfile 

## Usage

To use `scale.py`, follow these steps:

1. Clone the repository to your local machine.
2. Open a terminal and navigate to the `scale` directory.
3. Run the following command to install the required Python packages:

   ```
   pipenv install
   ```

4. Run the following command to scale the AKS cluster:

   ```
   pipenv run scale.py
   ```

   This will scale the AKS cluster by adding or removing nodes from the agent pool, depending on the current load.

## Configuration

`scale.py` reads its configuration from a JSON file named `config.json`. The following parameters can be configured:

- `AZURE_SUBSCRIPTION_ID`: The ID of the Azure subscription.
- `AZURE_RESOURCE_GROUP_NAME`: The name of the resource group that contains the AKS cluster.
- `AKS_CLUSTER_NAME`: The name of the AKS cluster.
- `AGENT_POOL_NAME`: The name of the agent pool to scale.
- `MIN_NODE_COUNT`: The minimum number of nodes in the agent pool.
- `MAX_NODE_COUNT`: The maximum number of nodes in the agent pool.
- `SCALE_UP_THRESHOLD`: The CPU usage threshold at which to scale up the agent pool.
- `SCALE_DOWN_THRESHOLD`: The CPU usage threshold at which to scale down the agent pool.
- `TIMEOUT`: The maximum amount of time to wait for pods to be evicted from a node before deleting the node.

if you run the script without a config.json file, it will create one for you with default values, which you have
to manually complete with the mandatory null values set to appropriate values.
## License

`scale.py` is licensed under the Apache version 2 License. See the `LICENSE` file for more information.

