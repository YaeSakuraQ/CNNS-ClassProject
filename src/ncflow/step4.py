import networkx as nx

def step4_flow_optimization(topology, current_edge_flows, demands, assigned_paths, demand_allocations):
    """
    Step 4: Flow Optimization Extension.
    
    Refines the feasible allocation from Step 3 to increase the total flow allocated, 
    aiming for global max-flow optimality. It attempts to route additional flow 
    for demands that were not fully satisfied, using residual capacity on their 
    assigned paths.

    Inputs:
    - topology: networkx.Graph object with 'capacity' attribute on edges.
    - current_edge_flows: Dict mapping edges (u, v) to current flow volume.
    - demands: List of dicts {'id', 'volume', ...}.
    - assigned_paths: Dict mapping demand ID -> List of nodes [n1, n2, ...].
    - demand_allocations: Dict mapping demand ID -> Currently allocated volume.

    Outputs:
    - optimized_edge_flows: Dict mapping edges (u, v) to new optimized flow volume.
    - optimized_demand_allocations: Dict mapping demand ID -> New allocated volume.
    """
    print("Running Step 4: Flow Optimization Extension...")
    
    optimized_edge_flows = current_edge_flows.copy()
    optimized_demand_allocations = demand_allocations.copy()
    
    # 1. Build Residual Graph
    # Calculate available capacity on each edge
    residual_graph = nx.DiGraph() # Use DiGraph to track directional residuals
    
    for u, v, data in topology.edges(data=True):
        cap = data.get('capacity', 0.0)
        
        # Calculate used capacity (sum of flow in both directions for undirected link capacity)
        # Assuming undirected links share capacity for both directions:
        flow_uv = optimized_edge_flows.get((u, v), 0.0)
        flow_vu = optimized_edge_flows.get((v, u), 0.0)
        
        # In this model, we treat capacity as shared or directed?
        # Step 1 sends 'capacity' to generic graph. 
        # Usually in WAN TE, links are directed or undirected with shared cap.
        # Let's assume directed capacity constraints for simple max flow, 
        # or undirected with shared cap.
        # Given Step 5 uses "residual_flows" and "adjacency", 
        # let's stick to the convention: Capacity is per-direction or shared? 
        # To be safe: Assume Directed edges in implementation, or handle undirected.
        # Let's calculate residual for (u,v) based on cap - flow(u,v).
        
        residual = cap - flow_uv
        if residual > 1e-6:
            residual_graph.add_edge(u, v, capacity=residual)
            
        # If topology is undirected in NX, we check the other way too
        if not topology.is_directed():
            residual_vu = cap - flow_vu
            if residual_vu > 1e-6:
                residual_graph.add_edge(v, u, capacity=residual_vu)

    # 2. Iteratively try to increase flow for unsatisfied demands
    for demand in demands:
        did = demand['id']
        requested_vol = demand['volume']
        current_vol = optimized_demand_allocations.get(did, 0.0)
        
        if current_vol >= requested_vol - 1e-6:
            continue # Fully satisfied
            
        needed = requested_vol - current_vol
        path = assigned_paths.get(did)
        
        if not path or len(path) < 2:
            continue
            
        # 3. Check Bottleneck on the Assigned Path
        path_edges = list(zip(path[:-1], path[1:]))
        can_push = needed
        
        for u, v in path_edges:
            if not residual_graph.has_edge(u, v):
                can_push = 0
                break
            res_cap = residual_graph[u][v]['capacity']
            can_push = min(can_push, res_cap)
            
        if can_push > 1e-6:
            # 4. Push Flow
            # Update allocations
            optimized_demand_allocations[did] += can_push
            
            # Update Flows and Residuals
            for u, v in path_edges:
                # Update Edge Flow
                optimized_edge_flows[(u, v)] = optimized_edge_flows.get((u, v), 0.0) + can_push
                
                # Update Residual Graph
                prior_res = residual_graph[u][v]['capacity']
                new_res = prior_res - can_push
                if new_res < 1e-6:
                    residual_graph.remove_edge(u, v)
                else:
                    residual_graph[u][v]['capacity'] = new_res
                    
    return optimized_edge_flows, optimized_demand_allocations

# --- Test Case ---
if __name__ == "__main__":
    # Simple star topology
    G = nx.Graph()
    G.add_edge(0, 1, capacity=10.0)
    G.add_edge(1, 2, capacity=5.0) # Bottleneck
    
    # Existing Flow: 2.0 on 0->1->2
    curr_flows = {(0, 1): 2.0, (1, 2): 2.0}
    
    demands = [{'id': 'd1', 'volume': 10.0}] # Wants 10, got 2
    allocs = {'d1': 2.0}
    paths = {'d1': [0, 1, 2]}
    
    print("Initial:", allocs)
    
    new_flows, new_allocs = step4_flow_optimization(G, curr_flows, demands, paths, allocs)
    
    print("Optimized Allocations:", new_allocs)
    print("Optimized Flows:", new_flows)
    
    # Expected: 
    # Link (1,2) has cap 5, used 2 -> Residual 3.
    # We need 8 more. Can push 3.
    # Total d1 = 2 + 3 = 5.
    # Flows (0,1)=5, (1,2)=5.
