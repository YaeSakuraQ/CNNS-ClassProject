import networkx as nx

def step1_offline_clustering(topology, traffic_history=None):
    """
    Perform offline clustering on the network topology using greedy modularity maximization
    and construct a contracted network graph.

    Inputs:
    - topology: A networkx.Graph object where edges have 'capacity' attributes.
    - traffic_history: (Optional) Historical traffic matrices (unused in this specific step).

    Outputs:
    - clusters: A dictionary mapping node IDs to cluster IDs.
    - aggregated_graph: A networkx.Graph representing the clusters and aggregated capacities.
    """
    
    # 1. Modularity-based Clustering
    # We use greedy_modularity_communities which starts with singleton communities 
    # and merges the pair that maximizes modularity increase.
    # We prioritize 'capacity' as the edge weight.
    try:
        communities_generator = nx.algorithms.community.greedy_modularity_communities(
            topology, 
            weight='capacity'
        )
    except AttributeError:
        # Fallback for different NetworkX versions
        communities_generator = nx.community.greedy_modularity_communities(
            topology, 
            weight='capacity'
        )

    # 2. Format Output: Node -> Cluster ID
    clusters = {}
    for cluster_id, community_nodes in enumerate(communities_generator):
        for node in community_nodes:
            clusters[node] = cluster_id

    # 3. Construct Aggregated Graph (Contracted Network)
    aggregated_graph = nx.Graph()
    unique_clusters = set(clusters.values())
    aggregated_graph.add_nodes_from(unique_clusters)

    # Iterate over original edges to aggregate capacity between clusters
    for u, v, data in topology.edges(data=True):
        c_u = clusters.get(u)
        c_v = clusters.get(v)

        # Only process edges connecting different clusters
        if c_u is not None and c_v is not None and c_u != c_v:
            edge_capacity = data.get('capacity', 1.0)
            
            if aggregated_graph.has_edge(c_u, c_v):
                aggregated_graph[c_u][c_v]['capacity'] += edge_capacity
            else:
                aggregated_graph.add_edge(c_u, c_v, capacity=edge_capacity)

    return clusters, aggregated_graph

# --- Test Case ---
if __name__ == "__main__":
    # Create a 6-node graph with two distinct triangles (clusters) connected by a bridge
    # Cluster 1: Nodes 0, 1, 2 (Strong internal links)
    # Cluster 2: Nodes 3, 4, 5 (Strong internal links)
    # Bridge: Edge (2, 3) (Weaker or single link)
    
    G = nx.Graph()
    
    # High capacity within cluster 1
    G.add_edge(0, 1, capacity=10)
    G.add_edge(1, 2, capacity=10)
    G.add_edge(0, 2, capacity=10)
    
    # High capacity within cluster 2
    G.add_edge(3, 4, capacity=10)
    G.add_edge(4, 5, capacity=10)
    G.add_edge(3, 5, capacity=10)
    
    # Lower capacity bridge between clusters
    G.add_edge(2, 3, capacity=2)
    
    print("Running offline clustering...")
    node_clusters, agg_graph = step1_offline_clustering(G)
    
    print("\nNode to Cluster Mapping:")
    print(node_clusters)
    
    print("\nAggregated Graph Edges (Cluster -> Cluster : Capacity):")
    for u, v, data in agg_graph.edges(data=True):
        print(f"Cluster {u} <-> Cluster {v} : Capacity {data['capacity']}")
        
    # Expected Behavior: 
    # Nodes 0,1,2 should share a cluster ID.
    # Nodes 3,4,5 should share a different cluster ID.
    # The aggregated graph should have one edge with capacity 2 connecting the two clusters.