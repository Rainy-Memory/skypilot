# Generates a kind-cluster.yaml file with all ports mapped from host to container

preamble = """
apiVersion: kind.x-k8s.io/v1alpha4
kind: Cluster
nodes:
- role: control-plane
  extraPortMappings:"""
suffix = ""  # """- role: worker"""  # Uncomment this line to add a worker node
with open('kind-cluster.yaml', 'w') as f:
    f.write(preamble)
    for port in range(30000, 32768):
        f.write(f"""
  - containerPort: {port}
    hostPort: {port}
    listenAddress: "0.0.0.0"
    protocol: tcp""")
    f.write("\n")
    f.write(suffix)
