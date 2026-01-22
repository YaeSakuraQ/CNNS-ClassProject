import networkx as nx
import collections

# Try importing Gurobi
try:
    import gurobipy as gp
    from gurobipy import GRB
    GUROBI_AVAILABLE = True
except ImportError:
    GUROBI_AVAILABLE = False
    print("Warning: Gurobi not found. Falling back to mock solver logic.")

def solve_max_agg_flow_gurobi(aggregated_graph, bundled_demands, agg_paths):
    """
    Solves MaxAggFlow using Gurobi.
    Maximize total flow for bundles on their fixed aggregated paths.
    """
    m = gp.Model("MaxAggFlow")
    m.setParam('OutputFlag', 0)

    # Variables: Flow for each bundle
    flow_vars = {}
    for bundle, volume in bundled_demands.items():
        flow_vars[bundle] = m.addVar(lb=0, ub=volume, name=f"f_{bundle}")

    # Constraints: Edge Capacities
    for u, v, data in aggregated_graph.edges(data=True):
        cap = data.get('capacity', 0.0)
        
        flow_sum = 0
        used_flag = False
        
        for bundle, path in agg_paths.items():
            # Check if bundle uses edge (u,v)
            # Path is list of nodes [c1, c2...]
            for i in range(len(path) - 1):
                if (path[i] == u and path[i+1] == v) or (path[i] == v and path[i+1] == u):
                    flow_sum += flow_vars[bundle]
                    used_flag = True
                    break
        
        if used_flag:
            m.addConstr(flow_sum <= cap, name=f"cap_{u}_{v}")

    # Objective: Maximize total flow
    m.setObjective(gp.quicksum(flow_vars.values()), GRB.MAXIMIZE)
    m.optimize()

    results = {}
    if m.status == GRB.OPTIMAL:
        for bundle, var in flow_vars.items():
            results[bundle] = var.X
    else:
        for bundle in bundled_demands:
            results[bundle] = 0.0
    return results

def solve_cluster_flow_gurobi(cluster_id, topology, cluster_nodes, local_demands, transit_demands):
    """
    Solves MaxClusterFlow for a single cluster using Gurobi.
    """
    m = gp.Model(f"ClusterFlow_{cluster_id}")
    m.setParam('OutputFlag', 0)

    subgraph = topology.subgraph(cluster_nodes)
    
    # Combined list of all flows to manage
    all_flows = []
    
    for d in local_demands:
        all_flows.append({'id': d['id'], 'src': d['source'], 'dst': d['target'], 'vol': d['volume']})
    for d in transit_demands:
        all_flows.append({'id': d['id'], 'src': d['ingress'], 'dst': d['egress'], 'vol': d['volume']})
        
    # Variables: Flow per demand per directed edge
    f_vars = {}
    for d in all_flows:
        did = d['id']
        for u, v in subgraph.edges():
            f_vars[did, u, v] = m.addVar(lb=0, name=f"f_{did}_{u}_{v}")
            f_vars[did, v, u] = m.addVar(lb=0, name=f"f_{did}_{v}_{u}") # Reverse direction for undirected edge capacity sharing

    # Constraints: Edge Capacities
    for u, v in subgraph.edges():
        cap = subgraph[u][v].get('capacity', 0.0)
        # Sum of flow in BOTH directions <= Capacity
        edge_sum = gp.quicksum(f_vars[d['id'], u, v] + f_vars[d['id'], v, u] for d in all_flows)
        m.addConstr(edge_sum <= cap, name=f"edge_cap_{u}_{v}")

    # Flow Conservation & Objective Variables
    delivered_vars = {}
    
    for d in all_flows:
        did = d['id']
        src = d['src']
        dst = d['dst']
        vol = d['vol']
        
        delivered = m.addVar(lb=0, ub=vol, name=f"delivered_{did}")
        delivered_vars[did] = delivered
        
        for n in cluster_nodes:
            # Inflow from neighbors k -> n
            inflow = gp.quicksum(f_vars[did, k, n] for k in subgraph.neighbors(n))
            # Outflow from n -> k
            outflow = gp.quicksum(f_vars[did, n, k] for k in subgraph.neighbors(n))
            
            if n == src:
                if src == dst: # Corner case: src==dst in transit? unlikely but possible
                     m.addConstr(delivered == vol) 
                else:
                     m.addConstr(outflow - inflow == delivered, name=f"src_{did}_{n}")
            elif n == dst:
                m.addConstr(inflow - outflow == delivered, name=f"dst_{did}_{n}")
            else:
                m.addConstr(inflow - outflow == 0, name=f"cons_{did}_{n}")

    # Objective: Maximize total throughput
    m.setObjective(gp.quicksum(delivered_vars.values()), GRB.MAXIMIZE)
    m.optimize()
    
    allocations = {}
    if m.status == GRB.OPTIMAL:
        for did, var in delivered_vars.items():
            allocations[did] = var.X
    return allocations

def mock_online_allocation(topology, clusters, aggregated_graph, demands, constraints):
    """
    Original mock logic fallback.
    """
    bundled_demands = collections.defaultdict(float)
    for d in demands:
        c_src = clusters[d['source']]
        c_dst = clusters[d['target']]
        bundled_demands[(c_src, c_dst)] += d['volume']

    # 1. Agg Flow
    f1_allocation = {}
    for (c_u, c_v), requested_vol in bundled_demands.items():
        if c_u == c_v:
            f1_allocation[(c_u, c_v)] = float('inf')
        elif aggregated_graph.has_edge(c_u, c_v):
            capacity = aggregated_graph[c_u][c_v]['capacity']
            f1_allocation[(c_u, c_v)] = min(requested_vol, capacity)
        else:
            f1_allocation[(c_u, c_v)] = 0.0

    # 2. Cluster Flow
    f2_allocation_per_cluster = collections.defaultdict(dict)
    
    for d in demands:
        did = d['id']
        c_src = clusters[d['source']]
        c_dst = clusters[d['target']]
        vol = d['volume']
        
        # Apply f1 limits proportionally
        bundle_vol = bundled_demands.get((c_src, c_dst), 1.0)
        f1_limit = f1_allocation.get((c_src, c_dst), 0.0)
        
        factor = 1.0
        if c_src != c_dst and bundle_vol > 0:
            factor = min(1.0, f1_limit / bundle_vol)
            
        allocated = vol * factor
        f2_allocation_per_cluster[c_src][did] = allocated
        f2_allocation_per_cluster[c_dst][did] = allocated
        
        # also set for transit clusters roughly
        cons = constraints.get((c_src, c_dst))
        if cons:
            for c in cons.get('allowed_path', []):
                f2_allocation_per_cluster[c][did] = allocated

    # 3. Finalize
    final_flow = {}
    for d in demands:
        did = d['id']
        # Min of all involved clusters (simplified)
        vals = []
        for c in f2_allocation_per_cluster:
            if did in f2_allocation_per_cluster[c]:
                vals.append(f2_allocation_per_cluster[c][did])
        
        final_flow[did] = min(vals) if vals else 0.0
        
    return final_flow

def step2_online_flow_allocation(topology, clusters, aggregated_graph, demands, constraints):
    if not GUROBI_AVAILABLE:
        print(" [Step 2] Gurobi not installed. Using Mock Solver.")
        return mock_online_allocation(topology, clusters, aggregated_graph, demands, constraints)

    print(" [Step 2] Using Gurobi Solver.")
    
    # 1. Prepare Aggregated Problems
    bundled_demands = collections.defaultdict(float)
    agg_paths = {}
    
    for d in demands:
        c_src = clusters[d['source']]
        c_dst = clusters[d['target']]
        if c_src != c_dst:
            bundled_demands[(c_src, c_dst)] += d['volume']
            if (c_src, c_dst) in constraints:
                agg_paths[(c_src, c_dst)] = constraints[(c_src, c_dst)]['allowed_path']
    
    # Solve MaxAggFlow
    # Note: f1_results gives Total Bundle Volume allowed.
    f1_results = solve_max_agg_flow_gurobi(aggregated_graph, bundled_demands, agg_paths)
    
    # 2. Parallel Decomposition (Per Cluster)
    # We store results as: allocations[demand_id][cluster_id] = flow
    parallel_allocations = collections.defaultdict(dict)
    
    unique_clusters = set(clusters.values())
    
    for cluster_id in unique_clusters:
        cluster_nodes = [n for n, c in clusters.items() if c == cluster_id]
        
        local_d = [d for d in demands if clusters[d['source']] == cluster_id and clusters[d['target']] == cluster_id]
        transit_d = []
        
        for d in demands:
            c_s = clusters[d['source']]
            c_t = clusters.get(d['target'])
            if c_s == c_t: continue 
            
            # Retrieve path info
            cons = constraints.get((c_s, c_t))
            path = cons.get('allowed_path', []) if cons else []
            edges = cons.get('allowed_edges', []) if cons else []
            
            ingress = None
            egress = None
            
            # Case: Outbound (Source is here)
            if c_s == cluster_id:
                # Egress is first crossing node u
                if edges: egress = edges[0][0]
                
            # Case: Inbound (Dest is here)
            elif c_t == cluster_id:
                # Ingress is last crossing node v
                if edges: ingress = edges[-1][1]
                
            # Case: Transit
            elif cluster_id in path:
                idx = path.index(cluster_id)
                # Ensure valid transit index
                if 0 < idx < len(path)-1 and len(edges) >= len(path)-1:
                    ingress = edges[idx-1][1]
                    egress = edges[idx][0]
            
            if (c_s == cluster_id and egress is not None) or \
               (c_t == cluster_id and ingress is not None) or \
               (ingress is not None and egress is not None):
                   
                # Constrain volume by bundle limit f1?
                # The total flow for this bundle is capped by f1_results[(c_s, c_t)].
                # We can't easily tell the solver "sum of these demands <= f1" unless we add bundle constraints inside cluster solver.
                # For simplicity, we assume Proportional Distribution of f1 to demands here
                # OR we just explicitly cap each demand by the bundle ratio.
                
                bundle_vol = bundled_demands.get((c_s, c_t), 1.0)
                allowed_bundle = f1_results.get((c_s, c_t), 0.0)
                
                ratio = 1.0
                if bundle_vol > 0: ratio = allowed_bundle / bundle_vol
                
                eff_vol = d['volume'] * ratio
                
                transit_d.append({
                    'id': d['id'],
                    'ingress': ingress if ingress is not None else d['source'],
                    'egress': egress if egress is not None else d['target'],
                    'volume': eff_vol
                })

        # Solve
        c_res = solve_cluster_flow_gurobi(cluster_id, topology, cluster_nodes, local_d, transit_d)
        for did, flow in c_res.items():
            parallel_allocations[did][cluster_id] = flow
            
    # 3. Final Reconciliation
    final_flow_allocation = {}
    for d in demands:
        did = d['id']
        if did not in parallel_allocations:
            final_flow_allocation[did] = 0.0
            continue
            
        # Take min of all segments involved
        # Which clusters are involved? Source, Dest, and Transit in path
        c_s = clusters[d['source']]
        c_t = clusters[d['target']]
        
        involved_clusters = {c_s, c_t}
        if c_s != c_t:
            cons = constraints.get((c_s, c_t))
            if cons:
                involved_clusters.update(cons.get('allowed_path', []))
        
        # Valid flows
        segment_flows = []
        for c in involved_clusters:
            if c in parallel_allocations[did]:
                segment_flows.append(parallel_allocations[did][c])
            else:
                # If a cluster along the path failed to route any, the E2E flow is 0
                segment_flows.append(0.0)
                
        final_flow_allocation[did] = min(segment_flows) if segment_flows else 0.0
        
    return final_flow_allocation