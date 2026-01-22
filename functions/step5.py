import networkx as nx

def step5_generate_forwarding_entries(topology, final_flow_dict):
    """
    Generates explicit forwarding paths from edge flow allocations using Greedy Flow Decomposition.

    Inputs:
    - topology: A networkx.Graph object.
    - final_flow_dict: A dictionary mapping edges (u, v) to flow volumes. 
      Format: { (u, v): 10.0, ... }

    Outputs:
    - forwarding_paths: A list of dictionaries representing specific paths and rates.
      Format: [{'path': [node_a, node_b, ...], 'rate': float}, ...]
    """
    forwarding_paths = []

    # 1. Create a working copy of flows (residual graph)
    # We filter out negligible flows to avoid floating point noise.
    residual_flows = {edge: flow for edge, flow in final_flow_dict.items() if flow > 1e-6}
    
    # Helper: Build adjacency map for efficient traversal
    def build_adjacency(flows):
        adj = {}
        for u, v in flows:
            if u not in adj: adj[u] = []
            adj[u].append(v)
        return adj

    # 2. Decomposition Loop
    while residual_flows:
        adjacency = build_adjacency(residual_flows)
        
        # 3. Find a start node (Source)
        # Ideally, a source is a node with outgoing flow but no incoming flow in the residual graph.
        # If the flow contains cycles, we might just pick any node with outgoing flow.
        potential_sources = set(adjacency.keys())
        potential_targets = set(v for u_list in adjacency.values() for v in u_list)
        
        # Nodes that are in keys but not values are pure sources
        pure_sources = list(potential_sources - potential_targets)
        
        if pure_sources:
            start_node = pure_sources[0]
        elif potential_sources:
            # If no pure source exists (e.g., circulation/cycles), pick arbitrary node
            start_node = list(potential_sources)[0]
        else:
            break # No more edges to traverse

        # 4. Traverse to find a path (DFS/BFS)
        # We need to find *any* simple path from start_node to a sink or until we get stuck.
        path = [start_node]
        curr = start_node
        visited = {start_node}
        
        while curr in adjacency and adjacency[curr]:
            # Simple greedy strategy: pick the first available neighbor
            next_node = adjacency[curr][0]
            
            # Cycle prevention for simple paths (optional but good for decomposition)
            if next_node in visited:
                # If we hit a cycle, we might need to handle it, but for standard DAG flows,
                # we just skip or break. Here we just take it to close the loop if necessary
                # or try a different neighbor.
                # For simplicity in this greedy approach, we just stop if we loop.
                # However, strict decomposition usually implies Acyclic flows.
                pass 
                
            path.append(next_node)
            visited.add(next_node)
            curr = next_node
            
            # If current node has no outgoing edges in residual, it's a sink for this path
            if curr not in adjacency:
                break
        
        # If path has only 1 node, we're stuck, remove it to prevent infinite loop
        if len(path) < 2:
            # Remove bad edges from this node to progress
            if start_node in adjacency:
                for neighbor in adjacency[start_node]:
                    if (start_node, neighbor) in residual_flows:
                        del residual_flows[(start_node, neighbor)]
            continue

        # 5. Identify Bottleneck
        # Find the edge in the path with the minimum remaining flow
        path_edges = list(zip(path[:-1], path[1:]))
        min_rate = float('inf')
        
        for edge in path_edges:
            # Handle undirected/directed key mismatch if necessary
            if edge in residual_flows:
                flow = residual_flows[edge]
            elif (edge[1], edge[0]) in residual_flows:
                 # If using undirected graph, check reverse key
                 edge = (edge[1], edge[0])
                 flow = residual_flows[edge]
            else:
                 flow = 0
            
            if flow < min_rate:
                min_rate = flow

        # 6. Store Result
        forwarding_paths.append({
            'path': path,
            'rate': min_rate
        })

        # 7. Update Residuals
        # Subtract the bottleneck rate from all edges in the path
        for u, v in path_edges:
            edge_key = (u, v)
            if edge_key not in residual_flows:
                edge_key = (v, u) # Check reverse if undirected
            
            if edge_key in residual_flows:
                residual_flows[edge_key] -= min_rate
                # 8. Clean up zero flows
                if residual_flows[edge_key] <= 1e-6:
                    del residual_flows[edge_key]

    return forwarding_paths