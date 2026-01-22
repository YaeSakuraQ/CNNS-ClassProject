import networkx as nx
import random


def get_physical_crossing_edges(topology, clusters):
    """
    Identifies all physical edges connecting different clusters.
    
    Returns:
    dict: {(cluster_u, cluster_v): [(u, v), ...]}
    """
    crossing_edges = {}
    for u, v in topology.edges():
        c_u = clusters.get(u)
        c_v = clusters.get(v)
        
        if c_u is not None and c_v is not None and c_u != c_v:
            # Canonical key (min, max) to handle undirectedness if needed?
            # Or directional keys. Step 3 uses (ClusterU, ClusterV) as directional sequence.
            # So we store both directions or match topology type.
            # Assuming topology is undirected or we want bidirectional options:
            
            if (c_u, c_v) not in crossing_edges: crossing_edges[(c_u, c_v)] = []
            crossing_edges[(c_u, c_v)].append((u, v))
            
            # If undirected, also store reverse mapping for lookup convenience
            if (c_v, c_u) not in crossing_edges: crossing_edges[(c_v, c_u)] = []
            crossing_edges[(c_v, c_u)].append((v, u))
            
    return crossing_edges

def shortest_path_in_cluster(topology, clusters, cluster_id, u, v):
    """
    Finds a shortest path between u and v restricted to nodes within cluster_id.
    """
    # Create subgraph view for the cluster
    cluster_nodes = [n for n, c in clusters.items() if c == cluster_id]
    subgraph = topology.subgraph(cluster_nodes)
    
    try:
        return nx.shortest_path(subgraph, source=u, target=v, weight='weight') # Use weight if exists, else hops
    except nx.NetworkXNoPath:
        return []

def resolve_demand_path(topology, clusters, demand, constraints):
    """
    Constructs the full node-level path for a demand based on Step 3 constraints.
    
    Args:
        topology: NetworkX graph.
        clusters: Node -> Cluster mapping.
        demand: {'source': s, 'target': t}.
        constraints: {'allowed_path': [c1, c2..], 'allowed_edges': [(u1,v1), (u2,v2)...]}.
        
    Returns:
        List of nodes [s, ..., t].
    """
    start_node = demand['source']
    end_node = demand['target']
    
    c_start = clusters[start_node]
    c_end = clusters[end_node]
    
    # Check if intra-cluster (no constraints or empty path list)
    if c_start == c_end:
        return shortest_path_in_cluster(topology, clusters, c_start, start_node, end_node)
        
    if not constraints or 'allowed_path' not in constraints or not constraints['allowed_path']:
        # Fallback: Just shortest path on whole graph if something went wrong
        try:
            return nx.shortest_path(topology, start_node, end_node)
        except:
            return []

    # Inter-cluster routing
    # Path: Start -> (Intra) -> Cross1_U -> Cross1_V -> (Intra) -> Cross2_U ... -> End
    
    full_path = []
    curr_node = start_node
    curr_cluster = c_start
    
    cluster_path = constraints['allowed_path']
    crossing_edges = constraints.get('allowed_edges', [])
    
    # Sanity check: length of crossing edges should be len(cluster_path) - 1
    if len(crossing_edges) != len(cluster_path) - 1:
        # Mismatch (maybe mock data issue), return fallback
        print(f"Warning: constraints mismatch for demand {demand.get('id')}. Path len {len(cluster_path)}, edges {len(crossing_edges)}")
        return []
        
    for i, edge in enumerate(crossing_edges):
        # Edge is (u, v) crossing from cluster_path[i] to cluster_path[i+1]
        u_cross, v_cross = edge
        
        # Ensure directionality: u_cross should be in current cluster
        if clusters.get(u_cross) != curr_cluster:
            # Maybe it's recorded as (v, u) in the constraints? Swap.
            if clusters.get(v_cross) == curr_cluster:
                 u_cross, v_cross = v_cross, u_cross
            else:
                 # Logic error or discontiguity
                 print(f"Error: Crossing edge {edge} does not connect to current cluster {curr_cluster}")
                 return []
        
        # 1. Route to the crossing point (Intra-cluster)
        intra_path = shortest_path_in_cluster(topology, clusters, curr_cluster, curr_node, u_cross)
        if not intra_path:
             print(f"Error: No internal path in cluster {curr_cluster} from {curr_node} to {u_cross}")
             return []
             
        # Append intra-path (avoid duplicating current node if not first segment)
        if full_path:
            full_path.extend(intra_path[1:]) # Skip duplicate join point
        else:
            full_path.extend(intra_path)
            
        # 2. Cross the edge
        # Append v_cross
        full_path.append(v_cross)
        
        # Update state
        curr_node = v_cross
        curr_cluster = clusters[curr_node] # Should be cluster_path[i+1]
        
    # Final Segment: From last entry point to destination
    last_intra = shortest_path_in_cluster(topology, clusters, curr_cluster, curr_node, end_node)
    if not last_intra:
        print(f"Error: No final path in cluster {curr_cluster} from {curr_node} to {end_node}")
        return []
        
    if full_path:
        full_path.extend(last_intra[1:])
    else:
        full_path.extend(last_intra)
        
    
    return full_path

def load_topology_zoo(file_path):
    """
    Loads a topology from a GraphML file (Internet Topology Zoo format).
    Normalizes capacity to Mbps.
    """
    try:
        G = nx.read_graphml(file_path)
    except Exception as e:
        print(f"Error reading GraphML: {e}")
        return nx.Graph()

    # Convert node labels to integers if they are not
    G = nx.convert_node_labels_to_integers(G, label_attribute='original_label')

    # key mapping for capacity might vary, but usually 'LinkSpeedRaw' is bps
    # If not found, default to 10 Gbps (10000 Mbps)
    DEFAULT_CAPACITY_MBPS = 1000.0 

    for u, v, data in G.edges(data=True):
        cap_bps = data.get('LinkSpeedRaw')
        
        if cap_bps is not None:
            try:
                # Convert bps to Mbps
                capacity_mbps = float(cap_bps) / 1e6
                # Ensure non-zero
                if capacity_mbps <= 0: capacity_mbps = DEFAULT_CAPACITY_MBPS
            except:
                capacity_mbps = DEFAULT_CAPACITY_MBPS
        else:
            # Check for LinkSpeed string (e.g. "10", "2.5") + Units
            # This is complex to parse robustly, so we might fallback
            capacity_mbps = DEFAULT_CAPACITY_MBPS
            
        data['capacity'] = capacity_mbps
        
    return G

def generate_synthetic_demands(topology, num_demands=50, max_volume=100.0):
    """
    Generates random traffic demands for the given topology.
    """
    demands = []
    nodes = list(topology.nodes())
    
    if len(nodes) < 2:
        return []
        
    for i in range(num_demands):
        src, dst = random.sample(nodes, 2)
        vol = random.uniform(1.0, max_volume)
        demands.append({
            'id': f'd{i}',
            'source': src,
            'target': dst,
            'volume': vol
        })
    return demands
