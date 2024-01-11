import gym
from gym import spaces
import networkx as nx
import numpy as np
import torch
import random 
from torch_geometric.data import Data
import torch_geometric.transforms as T


from torch_geometric.data import Data
from torch_geometric.nn import SAGEConv, global_mean_pool
import concurrent.futures
from numba import jit
from torch_geometric.utils import to_undirected
from root_heuristic_rf import GraphPredictor
from torch_geometric.transforms import Compose, ToUndirected, AddSelfLoops, NormalizeFeatures
from torch_geometric.utils import add_self_loops
@jit(nopython=True)
def compute_reward(has_found_target, 
                   TARGET_FOUND_REWARD, STEP_PENALTY, INCORRECT_LEAF_PENALTY, is_incorect_leaf):
    

    if has_found_target:
        target_reward =  TARGET_FOUND_REWARD
        return target_reward
    
    if is_incorect_leaf == False:
        return INCORRECT_LEAF_PENALTY
    
    total_reward = STEP_PENALTY 
    #print(f"Distance reward: {distance_reward}, Step penalty: {STEP_PENALTY}, Revisit penalty: {revisit_penalty}, New node bonus: {new_node_bonus}")
    return total_reward
class GraphTraversalEnv(gym.Env):
    def __init__(self, graph, target_nodes,num_actions ,root_detection_model_path="models/root_heuristic_model.joblib"):
        """
        Initializes the Graph Traversal Environment.

        Args:
            graph (nx.Graph): A NetworkX graph.
            target_nodes (list): A list of target nodes within the graph.
            subgraph_detection_model_path (str): Path to the trained subgraph detection model.
            max_episode_steps (int): Maximum steps allowed per episode.
        """
        super(GraphTraversalEnv, self).__init__()
        self.path_cache = {}
        self.action_space = spaces.Discrete(num_actions)
        self._validate_graph(graph)
        self.main_graph = graph


        self.target_nodes = target_nodes
        self.shortest_path_cache = {}
        self.visited_stack = []
        self.current_node_iterator = 0

        self._init_rewards_and_penalties()
        #load model
        self.root_detection_model_path = root_detection_model_path
        self.root_detector = GraphPredictor(self.root_detection_model_path)
        print("Model loaded!")
        #get proba for all root nodes, returns a map of root node -> proba
        self.root_proba = self.root_detector.predict_probabilities(self.main_graph)


        #sort every roots based on proba, ommit the ones with less than 0.5 proba
         
        self.sorted_roots = sorted(self.root_proba, key=self.root_proba.get, reverse=True)

        #remvove the roots with proba < 0.5
        self.sorted_roots = [root for root in self.sorted_roots if self.root_proba[root] > 0.5]
        #from the current_node_iterator, get the corresponding root
        self.best_root, ref_graph = self._get_best_root()
        #update the graph to be the subgraph of the root (using BFS)
        self.reference_graph = ref_graph
        centralities = self.compute_centralities(graph=ref_graph)
        for node in ref_graph.nodes():
            ref_graph.nodes[node].update(centralities[node])

        #create a copy of the reference graph
        self.graph = self.reference_graph.copy()

        self.state_size = 13

        self.observation_space = self._define_observation_space()
        self.nb_targets = len(self.target_nodes)
        self.nb_actions_taken = 0
        self.target_node = None
        self.reset()
        self.assess_target_complexity()


    def assess_target_complexity(self):
        print("-------------------- ASSESSING TARGET COMPLEXITY ----------------------")
        cycles = list(nx.simple_cycles(self.graph))

        has_cycle = len(cycles) > 0
        print(f"Has cycles: {has_cycle}")
        if has_cycle:
            print("Cycles found:", cycles)
        print(f"Number of targets: {self.nb_targets}")
        print(f"Number of nodes in the graph: {len(self.graph.nodes)}")
        print(f"Number of edges in the graph: {len(self.graph.edges)}")
        #get the number of nodes between the current node and the target nodes
        sum_path = 0
        for target in self.target_nodes:
            sum_path += self._get_path_length(self.current_node, target)
        print(f"Path length from current node to target nodes: {sum_path}")
        mean_number_of_neighbors = np.mean([self.graph.out_degree(node) for node in self.graph.nodes])
        mean_number_of_neighbors_for_non_leaves = np.mean([self.graph.out_degree(node) for node in self.graph.nodes if self.graph.out_degree(node) > 0])
        print(f"Mean number of neighbors: {mean_number_of_neighbors} (for non leaves: {mean_number_of_neighbors_for_non_leaves})")
        depth = nx.dag_longest_path_length(self.graph)
        print(f"Depth of the graph: {depth}")
        nb_neighbours_root = self.graph.out_degree(self.best_root)
        print(f"Number of neighbors of the root: {nb_neighbours_root}")
        #number of leaves
        nb_leaves = len([node for node in self.graph.nodes if self.graph.out_degree(node) == 0])
        print(f"Number of leaves: {nb_leaves} which is {nb_leaves/len(self.graph.nodes)}% of the graph")

        print("------------------------------------------------------------------------")



    def _get_best_root(self):

        #get root with highest proba
        best_root = self.sorted_roots[self.current_node_iterator]

        best_subgraph = nx.bfs_tree(self.main_graph, best_root)


        #check if there is a path from root to all target nodes
        has_path = {}
        for target in self.target_nodes:
            try:
                path = nx.shortest_path(best_subgraph, best_root, target)
                has_path[target] = True
            except nx.NetworkXNoPath:
                has_path[target] = False
                raise ValueError(f"There is no path from root {best_root} to target {target}")

        #if there is no path from root to all target nodes, throw an error
        if not all(has_path.values()):
            raise ValueError("There is no path from root to all target nodes")

        for node in best_subgraph.nodes():
            # Copy node attributes
            best_subgraph.nodes[node].update(self.main_graph.nodes[node])
        for u, v in best_subgraph.edges():
            # In a multigraph, there might be multiple edges between u and v.
            # Here, we take the attributes of the first edge.
            if self.main_graph.has_edge(u, v):
                key = next(iter(self.main_graph[u][v]))
                best_subgraph.edges[u, v].update(self.main_graph.edges[u, v, key])

        return best_root, best_subgraph



    def _validate_graph(self, graph):
        if not isinstance(graph, nx.Graph):
            raise ValueError("Graph should be a NetworkX graph.")

    def _init_rewards_and_penalties(self):
        self.TARGET_FOUND_REWARD = 20
        self.STEP_PENALTY = -0.2
        self.PROXIMITY_MULTIPLIER = 1.5
        self.INCORRECT_LEAF_PENALTY = -1




    def _get_path_length(self, source, target):
        """
        Gets the length of the shortest path between two nodes.

        Args:
            source: The source node.
            target: The target node.

        Returns:
            int or None: The length of the shortest path or None if no path exists.
        """
        if (source, target) not in self.shortest_path_cache:
            try:
                path = nx.shortest_path(self.graph, source, target)
                self.shortest_path_cache[(source, target)] = len(path) - 1
            except nx.NetworkXNoPath:
                return None
        return self.shortest_path_cache[(source, target)]
    


    def _get_dist_to_target(self):
        """
        Calculates the distance from the current node to the target node.

        Returns:
            int or None: Distance to the target node or None if no path exists.
        """
        # If no path exists, return None.
        try:
            # Check if the path is in the cache
            if (self.current_node, self.target_node) in self.path_cache:
                return self.path_cache[(self.current_node, self.target_node)]
            
            path = nx.shortest_path(self.graph, self.current_node, self.target_node)
            dist = len(path) - 1

            # Store the path length in the cache
            self.path_cache[(self.current_node, self.target_node)] = dist

            return dist
        except nx.NetworkXNoPath:
            return None


    def _sample_start_node(self):
        """
        Samples a start node for the agent.

        Returns:
            Node: A node from sorted promising roots to start the episode.
        """

        #only keep the roots with proba > 0.5
        return self.best_root


        
    def _define_observation_space(self):
        """
        Defines the observation space for the environment.

        Returns:
            gym.spaces: Observation space object.
        """
        return spaces.Dict({
            'x': spaces.Box(low=-float('inf'), high=float('inf'), shape=(self.state_size,)),
            'edge_index': spaces.Tuple((spaces.Discrete(len(self.graph.nodes())), spaces.Discrete(len(self.graph.nodes()))))
        })
    

    def _restart_agent_from_root(self):
        """
        Resets the agent's position to the start node.
        """
        self.current_node = self._sample_start_node()
        self.visited_stack.append(self.current_node)
        self._increment_visited(self.current_node)

    def _get_valid_actions(self):
        """
        Determines the valid actions that can be taken from the current node.

        Returns:
            list: A list of valid action indices in PyTorch Geometric format.
        """
        neighbors = list(self.graph.successors(self.current_node))[:self.action_space.n]
        #For sanity check that the node mapping is correct
        
        #valid actions are the index of the neighbors
        #for example, if we have a space of 50 actions, but if the current node has only 3 neighbors, then the valid actions are [0,1,2]
        valid_actions = [i for i, _ in enumerate(neighbors)]

        return valid_actions







    def _get_action_mask(self):
        """
        Creates a mask for valid actions in the action space.

        Returns:
            np.array: An array representing the action mask.
        """
        valid_actions = self._get_valid_actions()

        action_mask = np.full(self.action_space.n, 0)
        action_mask[valid_actions] = 1

        return action_mask
    

    def _get_probability_distribution(self, action_mask):
        """
        Creates a weight vector for the action space.
        Which makes 50% chance of choosing a good action and 50% chance of choosing a bad action

        Returns:
            np.array: An array representing the action mask.
        """
        #for each of the neighbour check if they have at least one path to a target
        neighbours = list(self.graph.successors(self.current_node))
        has_path_to_target = {}
        for neighbour in neighbours:
            has_path_to_target[neighbour] = False
            for target in self.target_nodes:
                if self._get_path_length(neighbour, target) is not None:
                    has_path_to_target[neighbour] = True
                    break
        
        #now for each neighbour, choosing a neighbour with a path to a target has 50% chance and choosing a neighbour without a path to a target has 50% chance
        
        nb_neighbours_with_path = sum(has_path_to_target.values())
        valid_action_count = np.sum(action_mask)
        nb_neighbours_without_path = valid_action_count - nb_neighbours_with_path
        weight_array = np.zeros(len(action_mask))
        #for each index of the action mask, if the neighbour has a path to a target, give it a weight of 1/nb_neighbours_with_path
        #if the neighbour doesn't have a path to a target, give it a weight of 1/nb_neighbours_without_path
        for i, neighbour in enumerate(neighbours):
            if has_path_to_target[neighbour]:
                weight_array[i] = 1/(2*nb_neighbours_with_path)
            else:
                weight_array[i] = 1/(2*nb_neighbours_without_path)


        return weight_array
    



    def _get_distance_to_goal(self, goal):
        """
        Gets the distance from the current node to the goal node.

        Args:
            goal: The goal node.

        Returns:
            int or None: Distance to the goal node or None if no path exists.
        """
        #get the goalth target node
        target_node = self.target_nodes[goal]
        # If no path exists, return None.
        try:
            # Check if the path is in the cache
            if (self.current_node, target_node) in self.path_cache:
                return self.path_cache[(self.current_node, target_node)]
            
            path = nx.shortest_path(self.graph, self.current_node, target_node)
            dist = len(path) - 1

            # Store the path length in the cache
            self.path_cache[(self.current_node, target_node)] = dist
        

            return dist
        except nx.NetworkXNoPath:
            return None


    def step(self, action, goal):
        
        #goal is a number defining the target node (0, 1, 2, 3, etc...)
        #check if the goal is a valid target node
        if goal >= len(self.target_nodes):
            raise ValueError(f"Goal {goal} is out of range for target nodes {self.target_nodes}")


        #check if has neighbors
        if self.graph.out_degree(self.current_node) == 0:
            raise ValueError(f"Current node {self.current_node} has no neighbors")


        prev_distance_to_goal = self._get_distance_to_goal(goal)

        self._perform_action(action)
        

        new_distance_to_goal = self._get_distance_to_goal(goal)

        #check if goal is reachable
        has_path = new_distance_to_goal is not None
        is_incorect_leaf = self.graph.out_degree(self.current_node) == 0 and not has_path


        has_found_target = self.current_node == self.target_nodes[goal]

        reward = compute_reward(has_found_target, self.TARGET_FOUND_REWARD, self.STEP_PENALTY, self.INCORRECT_LEAF_PENALTY, is_incorect_leaf)
        
        if has_path and not has_found_target:

            if new_distance_to_goal < prev_distance_to_goal:
                #give a reward for getting closer to the target
                distance_reward = self.PROXIMITY_MULTIPLIER
            elif new_distance_to_goal > prev_distance_to_goal:
                print("Not possible")
            reward += distance_reward


        self.nb_actions_taken += 1

        if has_found_target:

            #print(f"Found target {self.target_node}! reward : {reward}")
            obs = self._get_obs()

            reward = self.TARGET_FOUND_REWARD
            return obs, reward, True, self._episode_info(found_target=True)



        obs = self._get_obs()
        done = is_incorect_leaf

        return obs, reward, done, self._episode_info(incorrect_leaf=is_incorect_leaf, no_path=not has_path)



    def _perform_action(self, action):
        """
        Performs the given action (moving to a neighboring node) and updates the environment state.

        Args:
            action (int): The action to be performed.
        """
        neighbors = list(self.graph.neighbors(self.current_node))

        if action >= len(neighbors):
            raise ValueError(f"Action {action} is out of range for neighbors {neighbors}")
        
        self.current_node = neighbors[action]
        # Update the visited stack
        self.visited_stack.append(self.current_node)




    def _episode_info(self, found_target=False, incorrect_leaf=False, no_path=False):
        """
        Constructs additional info about the current episode.

        Args:
            found_target (bool): Flag indicating whether the target was found.

        Returns:
            dict: Additional info about the episode.
        """
        return {
            'found_target': found_target,
            'nb_actions_taken': self.nb_actions_taken,
            'nb_nodes_visited': self.calculate_number_visited_nodes(),
            'stopped_for_incorrect_leaf': incorrect_leaf,
            'stopped_for_no_path': no_path,
        }




    def calculate_number_visited_nodes(self):
        #itearte over all nodes in the graph and count the number of visited nodes
        return len(self.visited_stack)
    


    def reset(self):
        """
        Resets the environment for a new episode.

        Returns:
            Data: Initial observation data after resetting.
        """
        self.graph = self.reference_graph.copy()

        self.current_node_iterator = 0
        self.current_node = self._sample_start_node()
        self.current_subtree_root = self.current_node
        self.visited_stack = []
        self.nb_actions_taken = 0
        self.observation_space = self._define_observation_space()

        return self._get_obs()
    


    def compute_centralities(self, graph):
        """
        Computes the centrality measures for all nodes in the graph.

        Returns:
            dict: A mapping from node to centrality measure.
        """
        return {
            node: {
                'degree_centrality': nx.degree_centrality(graph)[node]
            } for node in graph.nodes()
        }




    def _get_obs(self):


        node_mapping = {node: i for i, node in enumerate(self.graph.nodes())}

        data_space_current_node_idx = node_mapping[self.current_node]
        # Use the node mapping to convert node indices

        edge_index = torch.tensor([(node_mapping[u], node_mapping[v]) for u, v in self.graph.edges()], dtype=torch.long).t().contiguous()

        #set the node "is_current" to 1 if it is the current node, 0 otherwise
        #set the number of keys found

        #give each node the index from the visited stack

        num_nodes_in_graph = len(self.graph.nodes)

        map_neighbour_to_index = {neighbour: i for i, neighbour in enumerate(self.graph.successors(self.current_node))}
 


        nb_nodes_visited = len(self.visited_stack)

        #count for each node in visited stack, how many times it has been visited
        count_visited = {}
        for node in self.visited_stack:
            if node not in count_visited:
                count_visited[node] = 0
            count_visited[node] += 1

        nb_neighbours = self.graph.out_degree(self.current_node)


        x = torch.tensor([[
            attributes['struct_size'],
            attributes['valid_pointer_count'],
            attributes['invalid_pointer_count'],
            attributes['first_pointer_offset'],
            attributes['last_pointer_offset'],
            attributes['first_valid_pointer_offset'],
            attributes['last_valid_pointer_offset'],
            count_visited[node]/nb_nodes_visited if node in count_visited else 0,
            self.nb_targets,
            self.graph.out_degree(node),
            num_nodes_in_graph,
            node == self.current_node,
            map_neighbour_to_index[node]/nb_neighbours if node in map_neighbour_to_index else -1, 
        ] for node, attributes in self.graph.nodes(data=True)], dtype=torch.float)

        
        edge_attr = torch.tensor([data['offset'] for u, v, data in self.graph.edges(data=True)], dtype=torch.float).unsqueeze(1)        # y is 1 if there's at least one node with cat=1 in the graph, 0 otherwise
        
        """

        # Normalize edge attributes
        edge_attr_np = edge_attr.numpy()
        edge_attr_np = (edge_attr_np - np.mean(edge_attr_np, axis=0)) / np.std(edge_attr_np, axis=0)
        edge_attr = torch.tensor(edge_attr_np, dtype=torch.float)
        """
        """
        # Standardize features (subtract mean, divide by standard deviation), ignore the last two features
        x_np = x.numpy()
        eps = 1e-8
        x_np[:, :-2] = (x_np[:, :-2] - np.mean(x_np[:, :-2] , axis=0)) / (np.std(x_np[:, :-2] , axis=0) + eps)
        """

        # Convert back to tensor
        #x = torch.tensor(x_np, dtype=torch.float)
        # Check if the shape of x matches self.state_size



        transform = T.Compose([
            NormalizeFeatures(),    # Normalize node features
            #ToUndirected(),         # Convert to undirected graph
        ])


        #reverse the direction of the edges
        #edge_index = edge_index[[1,0],:]
        #edge_index, edge_attr = add_self_loops(edge_index, edge_attr=edge_attr, fill_value=0, num_nodes=x.shape[0])


        

        data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr, current_node_id = data_space_current_node_idx)
        data = transform(data)

        if data.x.shape[1] != self.state_size:
            raise ValueError(f"The shape of x ({x.shape[1]}) does not match self.state_size ({self.state_size})")
        

        return data

    def close(self):
        pass

    