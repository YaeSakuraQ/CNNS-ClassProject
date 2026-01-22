import random

"""
SeCoT (Semantic Code Analysis)

1.  **Control Flow:**
    *   **Start** -\> Call `step3_feasibility_reconciliation`.
    *   **Initialize:** Create an empty dictionary `constraints`.
    *   **Loop (Outer):** Iterate through each `bundle` (source/target cluster pair) in the input `cluster_bundles`.
    *   **Path Lookup:** Call `get_assigned_path` to retrieve the single offline-computed path for the current bundle.
    *   **Branch:** Check if a valid path exists. If not, `continue` to the next bundle.
    *   **Initialize List:** Create `chosen_edges` to store the specific physical links for this path.
    *   **Loop (Inner):** Iterate through adjacent pairs of clusters (`cluster_u`, `cluster_v`) along the `selected_path`.
        *   **Edge Lookup:** Retrieve the list of all physical edges connecting `cluster_u` and `cluster_v` from `physical_crossing_edges`.
        *   **Branch:** Check if edges exist.
            *   **True:** Call `select_one_edge` to pick exactly one physical edge from the available options. Append it to `chosen_edges`.
    *   **Store:** Save the `selected_path` and `chosen_edges` into the `constraints` dictionary under the current `bundle` key.
    *   **Return:** Return the `constraints` dictionary -\> **End**.
2.  **Data Flow:**
    *   The `aggregated_graph` (containing pre-computed path data) flows into `get_assigned_path`, producing `selected_path`.
    *   The `physical_crossing_edges` map flows into the inner loop, providing the raw set of `available_edges` for each hop.
    *   `available_edges` flows into `select_one_edge`, which filters the data down to a single `single_edge`.
    *   The `single_edge` results are accumulated into the `chosen_edges` list.
    *   The `selected_path` and `chosen_edges` are combined into a dictionary structure and stored in `constraints` keyed by the `bundle`.
    *   The final `constraints` object is returned to the caller (likely the solver in the next step).
3.  **Finally:** This analysis confirms that the function strictly enforces the "single path, single crossing edge" heuristic described in the paper. By restricting the data flow to a single choice per hop, it ensures that the parallel sub-problems solved in subsequent steps will not have conflicting flow allocations, satisfying the feasibility requirement.
"""

def get_assigned_path(aggregated_graph, bundle_key):
    """
    Retrieves the pre-computed path on the aggregated graph for a specific cluster bundle.
    
    Requirement:
    "First, when solving MaxAgg Flow, only one path on the aggregated graph can be used for all 
    [cite_start]of the demands between a given pair of clusters." [cite: 325]
    "NCFlow also pre-computes offline... which path on the aggregated graph to use for each 
    [cite_start]cluster bundled demand in each iteration." [cite: 392]
    
    Args:
        aggregated_graph: Dictionary representing the cluster-level topology and paths.
        bundle_key: Tuple (source_cluster_id, target_cluster_id).
    
    Returns:
        List of cluster IDs representing the path.
    """
    # In NCFlow, paths are determined offline based on historical traffic.
    # We simulate this lookup here.
    if 'paths' in aggregated_graph and bundle_key in aggregated_graph['paths']:
        return aggregated_graph['paths'][bundle_key]
    return []

def select_one_edge(available_edges):
    """
    Selects exactly one physical edge to carry flow between two connected clusters.
    
    Requirement:
    "Next, between a pair of connected clusters, only one edge can carry the flow for a 
    [cite_start]cluster bundle." [cite: 327]
    
    Args:
        available_edges: List of edge IDs connecting two clusters.
        
    Returns:
        A single edge ID.
    """
    # Heuristic: Pick one edge (e.g., based on capacity or pseudo-random).
    # [cite_start]"NCFlow also pre-computes offline (1) a pseudo-random choice of which edges to use..." [cite: 392]
    return random.choice(available_edges)

def step3_feasibility_reconciliation(cluster_bundles, aggregated_graph, physical_crossing_edges):
    """
    Apply heuristics to restrict path and edge selection for cluster bundles to guarantee 
    that independent sub-problem solutions are consistent and feasible.
    
    Requirement:
    "To avoid end-to-end disagreements, we make two simple changes... First... only one path 
    [cite_start]on the aggregated graph can be used... Next... only one edge can carry the flow..." [cite: 324-327]
    "Intuitively, these changes suffice because the independent decisions made by different 
    [cite_start]problem instances cannot disagree..." [cite: 333]

    Inputs:
        cluster_bundles (list): A list of tuples, where each tuple represents a demand bundle 
                                (e.g., ('ClusterA', 'ClusterB')).
        aggregated_graph (dict): A dictionary containing graph structure and pre-computed 'paths'.
        physical_crossing_edges (dict): A dictionary mapping a tuple of connected clusters 
                                        (e.g., ('ClusterA', 'ClusterC')) to a list of physical edge IDs.
                                        
    Outputs:
        constraints (dict): A dictionary mapping each bundle to a dictionary containing 
                            'allowed_path' (list) and 'allowed_edges' (list).
    """
    constraints = {}
    
    # 1: Iterate through each bundle of demands between clusters
    # [cite_start]"we call such groups of demands to be cluster bundles." [cite: 326]
    for bundle in cluster_bundles:
        
        # 2: Select exactly one path on the aggregated graph for this bundle.
        # [cite_start]"only one path on the aggregated graph can be used for all of the demands between a given pair of clusters" [cite: 325]
        selected_path = get_assigned_path(aggregated_graph, bundle)
        
        # If no path is found (e.g., disconnected clusters), skip.
        if not selected_path:
            continue
            
        chosen_edges = []
        
        # 3: Iterate through each link (cluster pair) along the selected path
        # We need to determine the crossing edge for every hop in the aggregate path.
        for i in range(len(selected_path) - 1):
            cluster_u = selected_path[i]
            cluster_v = selected_path[i+1]
            
            # Retrieve all physical edges connecting these two clusters
            available_edges = physical_crossing_edges.get((cluster_u, cluster_v), [])
            
            # 4: Select exactly one physical edge connecting these clusters.
            # [cite_start]"between a pair of connected clusters, only one edge can carry the flow for a cluster bundle." [cite: 327]
            if available_edges:
                single_edge = select_one_edge(available_edges)
                chosen_edges.append(single_edge)
            else:
                # Should not happen if the aggregated path implies connectivity
                pass
                
        # 5 & 6: Store the constraints.
        # By explicitly defining which path and edges are allowed, we enforce the heuristic 
        # [cite_start]that prevents disagreements between parallel sub-problems. [cite: 333]
        constraints[bundle] = {
            'allowed_path': selected_path,
            'allowed_edges': chosen_edges
        }

    # 7: Return the constraints to be applied to the solver
    return constraints

# --- Test Case ---
if __name__ == "__main__":
    # Mock Data based on NCFlow structure
    # Bundles represent aggregated demands from one cluster to another
    bundles = [('RedCluster', 'GreenCluster'), ('RedCluster', 'BlueCluster')]
    
    # Aggregated Graph with pre-computed paths (simulating offline step 3.4)
    # The path is a sequence of Clusters (Nodes in the aggregated graph)
    agg_graph = {
        'paths': {
            ('RedCluster', 'GreenCluster'): ['RedCluster', 'YellowCluster', 'GreenCluster'],
            ('RedCluster', 'BlueCluster'): ['RedCluster', 'BlueCluster']
        }
    }
    
    # Physical edges connecting clusters (simulating the result of Step 1)
    # Maps (ClusterU, ClusterV) -> [List of Physical Edge IDs]
    phys_edges = {
        ('RedCluster', 'YellowCluster'): ['edge_r_y_1', 'edge_r_y_2'], 
        ('YellowCluster', 'GreenCluster'): ['edge_y_g_1', 'edge_y_g_2', 'edge_y_g_3'],
        ('RedCluster', 'BlueCluster'): ['edge_r_b_1']
    }
    
    print("Running Feasibility Reconciliation...")
    result_constraints = step3_feasibility_reconciliation(bundles, agg_graph, phys_edges)
    
    # Validation Output
    print("\nGenerated Constraints:")
    for bundle, cons in result_constraints.items():
        print(f"Bundle {bundle}:")
        print(f"  Allowed Path: {cons['allowed_path']}")
        print(f"  Allowed Crossing Edges: {cons['allowed_edges']}")
        
    # Expected Behavior:
    # Bundle ('RedCluster', 'GreenCluster'):
    #   - Path should be ['RedCluster', 'YellowCluster', 'GreenCluster']
    #   - Edges should contain exactly ONE edge from ['edge_r_y_1', 'edge_r_y_2'] 
    #     and ONE edge from ['edge_y_g_1', 'edge_y_g_2', 'edge_y_g_3'].
    #
    # Bundle ('RedCluster', 'BlueCluster'):
    #   - Path should be ['RedCluster', 'BlueCluster']
    #   - Edges should contain exactly ['edge_r_b_1'].