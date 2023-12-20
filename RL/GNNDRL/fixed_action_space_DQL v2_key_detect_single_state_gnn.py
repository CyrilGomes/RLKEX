# -------------------------
# IMPORTS AND SETUP
# -------------------------

import os
import random
import numpy as np
import networkx as nx
import torch
import torch.nn.functional as F
import matplotlib.pyplot as plt
import keyboard
from torch_geometric.data import Data
from torch_geometric.nn import SAGEConv, global_mean_pool

from rl_base.rl_environment_key_detect_single_state import GraphTraversalEnv
from collections import deque
import numpy as np
import random
import torch
import torch.nn.functional as F
from torch import optim
from collections import namedtuple, deque
#import range tqdm
from tqdm import tqdm
from tqdm import trange
from torch_geometric.nn import GCNConv
from torch_geometric.data import Batch
from torch_geometric.data import DataLoader, Batch
import torch.nn.functional as F


from torch_geometric.nn import GCNConv, global_mean_pool, SAGPooling
from torch_geometric.data import Data
import torch_geometric.transforms as T
from torch_geometric.nn import GATConv


device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
import matplotlib.pyplot as plt



# -------------------------
# GRAPH PROCESSING
# -------------------------


def connect_components(graph):
    undi_graph = graph.to_undirected()

    # Connect components
    components = list(nx.connected_components(undi_graph))
    for i in range(1, len(components)):

        graph.add_edge(random.choice(list(components[0])), random.choice(list(components[i])), offset=0)
        graph.add_edge(random.choice(list(components[i])), random.choice(list(components[0])), offset=0)

    return graph

def add_global_root_node(graph):
    root_node = "root"
    graph.add_node(root_node, label=root_node, cat=0, struct_size=0, pointer_count=0, valid_pointer_count=0, invalid_pointer_count=0, first_pointer_offset=0, last_pointer_offset=0, first_valid_pointer_offset=0, last_valid_pointer_offset=0, visited=1)
    [graph.add_edge(root_node, node, offset=0) for node in graph.nodes() if len(list(graph.predecessors(node))) == 0 and node != root_node]
    return graph

def convert_types(G):
    # Convert the string attributes to their corresponding types
    for node, data in G.nodes(data=True):
        # The label remains a string, so no conversion is needed for 'label'
        # Convert struct_size, valid_pointer_count, invalid_pointer_count,
        # first_pointer_offset, last_pointer_offset, first_valid_pointer_offset,
        # last_valid_pointer_offset, and address to int
        data['struct_size'] = int(data['struct_size'])
        data['valid_pointer_count'] = int(data['valid_pointer_count'])
        data['invalid_pointer_count'] = int(data['invalid_pointer_count'])
        data['first_pointer_offset'] = int(data['first_pointer_offset'])
        data['last_pointer_offset'] = int(data['last_pointer_offset'])
        data['first_valid_pointer_offset'] = int(data['first_valid_pointer_offset'])
        data['last_valid_pointer_offset'] = int(data['last_valid_pointer_offset'])
        data['address'] = int(data['address'])

        # Convert cat to an integer and ensure it's within the range of a byte (0-255)
        data['cat'] = int(data['cat'])
        if not (0 <= data['cat'] <= 255):
            raise ValueError(f"Value of 'cat' out of range for u8: {data['cat']}")

    #Same for edges attributes (offset)
    for u, v, data in G.edges(data=True):
        data['offset'] = int(data['offset'])


    return G

def remove_all_isolated_nodes(graph):
    graph.remove_nodes_from(list(nx.isolates(graph)))
    return graph
def preprocess_graph(graph):
    graph = nx.convert_node_labels_to_integers(graph)
    
    # Removing string attributes from nodes and edges
    graph = remove_all_isolated_nodes(graph)
    graph = convert_types(graph)
    for _, attributes in graph.nodes(data=True):
        for key in list(attributes):
            if isinstance(attributes[key], str):
                del attributes[key]

    for u, v, attributes in graph.edges(data=True):
        for key in list(attributes):
            if isinstance(attributes[key], str):
                del attributes[key]
    nx.set_node_attributes(graph, 0, 'visited')
    #graph = connect_components(graph)
    #graph = nx.subgraph(graph, nx.bfs_tree(graph, 0))

    #graph = add_global_root_node(graph)
    #Check if all edges possess an offset attribute

    return graph

def load_graphs_from_directory(directory_path):
    graph_files = [f for f in os.listdir(directory_path) if f.endswith('.graphml')]
    graphs = [nx.read_graphml(os.path.join(directory_path, f)) for f in graph_files]
    return [preprocess_graph(g) for g in graphs]

def graph_to_data(graph):
    x = torch.tensor([[
        attributes['struct_size'],
        attributes['valid_pointer_count'],
        attributes['invalid_pointer_count'],
        attributes['first_pointer_offset'],
        attributes['last_pointer_offset'],
        attributes['first_valid_pointer_offset'],
        attributes['last_valid_pointer_offset'],
        attributes['visited']
    ] for _, attributes in graph.nodes(data=True)], dtype=torch.float)
    
    edge_index = torch.tensor(list(graph.edges), dtype=torch.long).t().contiguous()
    edge_attr = torch.tensor([graph[u][v]['offset'] for u, v in graph.edges], dtype=torch.float).unsqueeze(1)
    return Data(x=x, edge_index=edge_index, edge_attr=edge_attr)


# -------------------------
# HYPERPARAMETERS
# -------------------------
BUFFER_SIZE = int(1e5)  # replay buffer size
BATCH_SIZE = 256         # batch size
GAMMA = 0.99            # discount factor
TAU = 1e-3              # soft update of target parameters
LR = 5e-4               # learning rate
UPDATE_EVERY = 4        # how often to update the network



# -------------------------
# MODEL DEFINITION
# -------------------------
class GraphQNetwork(torch.nn.Module):
    def __init__(self, node_feature_size, action_size, seed, heads=4):
        super(GraphQNetwork, self).__init__()
        self.seed = torch.manual_seed(seed)
        
        # Using Graph Attention Networks (GAT)
        self.conv1 = GATConv(node_feature_size, 32 // heads, heads=heads, concat=True)
        self.conv2 = GATConv(32, 32 // heads, heads=heads, concat=True)

        # Dueling DQN
        self.value_stream = torch.nn.Linear(32, 32)
        self.value = torch.nn.Linear(32, 1)
        
        self.advantage_stream = torch.nn.Linear(32, 32)
        self.advantage = torch.nn.Linear(32, action_size)

    def forward(self, data, action_mask=None):
        x, edge_index = data.x, data.edge_index
        
        x = F.elu(self.conv1(x, edge_index))
        x = F.elu(self.conv2(x, edge_index))
        
        # Global pooling
        x = global_mean_pool(x, data.batch)
        
        value = F.relu(self.value_stream(x))
        value = self.value(value)
        
        advantage = F.relu(self.advantage_stream(x))
        advantage = self.advantage(advantage)
        # Combine value and advantage streams

        qvals = value + (advantage - advantage.mean(dim=1, keepdim=True))

        if action_mask is not None:
                # Ensure the mask is the same shape and on the same device as qvals
                action_mask = action_mask.to(qvals.device)
                qvals += action_mask

        return qvals



# -------------------------
# AGENT DEFINITION
# -------------------------
class Agent:
    def __init__(self, state_size, action_size, seed):
        self.state_size = state_size
        self.action_size = action_size
        self.seed = random.seed(seed)

        # Q-Network
        self.qnetwork_local = GraphQNetwork(state_size, action_size, seed).to(device)
        self.qnetwork_target = GraphQNetwork(state_size, action_size, seed).to(device)
        self.optimizer = optim.Adam(self.qnetwork_local.parameters(), lr=LR)

        # Replay memory
        self.memory = deque(maxlen=BUFFER_SIZE)
        self.experience = namedtuple("Experience", field_names=["state", "action", "reward", "next_state", "done", "action_mask", "next_action_mask"])

        self.t_step = 0

        self.losses = []

    def step(self, state, action, reward, next_state, done, action_mask=None, next_action_mask=None):
        #ensure everything is on device
        state = state.to(device)
        next_state = next_state.to(device)
        


        # Save experience in replay memory
        e = self.experience(state, action, reward, next_state, done, action_mask, next_action_mask)
        self.memory.append(e)

        # Learn every UPDATE_EVERY time steps.
        self.t_step = (self.t_step + 1) % UPDATE_EVERY
        if self.t_step == 0:
            if len(self.memory) > BATCH_SIZE:
                experiences = self.sample()
                self.learn(experiences, GAMMA)

    def act(self, state, eps=0, action_mask=None):

        state = state.to(device)
        self.qnetwork_local.eval()
        action_mask = torch.tensor(action_mask).to(device)

        with torch.no_grad():
            action_values = self.qnetwork_local(state, action_mask)
        self.qnetwork_local.train()

        # Epsilon-greedy action selection
        if random.random() > eps:
            return np.argmax(action_values.cpu().data.numpy())
        else:
            return random.choice(np.where(action_mask.cpu() == 0)[0])  # Select from valid actions only

    def learn(self, experiences, gamma):
        states, actions, rewards, next_states, dones, action_masks, next_action_masks = experiences

        # DDQN
        indices = self.qnetwork_local(next_states, next_action_masks).detach().argmax(1).unsqueeze(1)
        Q_targets_next = self.qnetwork_target(next_states, next_action_masks).detach().gather(1, indices)

        # Reshape rewards and dones to be column vectors
        rewards = rewards.unsqueeze(1)
        dones = dones.unsqueeze(1)

        # Compute Q targets for current states
        Q_targets = rewards + (gamma * Q_targets_next * (1 - dones))

        # Get expected Q values from local model
        Q_expected = self.qnetwork_local(states, action_masks).gather(1, actions)

        # Compute loss
        loss = F.mse_loss(Q_expected, Q_targets)
        self.losses.append(loss.item())
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        # Soft update target network
        self.soft_update(self.qnetwork_local, self.qnetwork_target, TAU)



    def soft_update(self, local_model, target_model, tau):
        for target_param, local_param in zip(target_model.parameters(), local_model.parameters()):
            target_param.data.copy_(tau * local_param.data + (1.0 - tau) * target_param.data)

    def sample(self):
        experiences = random.sample(self.memory, k=BATCH_SIZE)

        states = Batch.from_data_list([e.state for e in experiences if e is not None]).to(device)
        
        actions = torch.tensor([e.action for e in experiences if e is not None], dtype=torch.long).unsqueeze(-1).to(device)
        rewards = torch.tensor([e.reward for e in experiences if e is not None]).to(device)
        
        # You should handle next_states in the same manner as states, given that it also contains graph data
        next_states = Batch.from_data_list([e.next_state for e in experiences if e is not None]).to(device)
        
        dones = torch.tensor([torch.tensor(e.done, dtype=torch.uint8) for e in experiences if e is not None]).to(device).float()
        # Convert the list of numpy arrays to a single numpy array before converting to a tensor
        action_masks = torch.tensor(np.array([e.action_mask for e in experiences if e is not None])).to(device)
        next_action_masks = torch.tensor(np.array([e.next_action_mask for e in experiences if e is not None])).to(device)

        return (states, actions, rewards, next_states, dones, action_masks, next_action_masks)




# -------------------------
# MAIN EXECUTION
# -------------------------

# Load and preprocess graph

FOLDER = "Generated_Graphs/output/"
ACTION_SPACE = 50
STATE_SPACE = 9

agent = Agent(STATE_SPACE, ACTION_SPACE, seed=0)


INIT_EPS = 0.98
EPS_DECAY = 0.99991


def check_parameters(env):
    #check if state space and action space are correct
    if env.action_space.n != ACTION_SPACE:
        raise ValueError("Action space is not correct")
    if env.observation_space.spaces['x'].shape[0] != STATE_SPACE:
        raise ValueError("State space is not correct")
    
    

def execute_for_graph(file, training = True):
    graph = nx.read_graphml(file)
    graph = preprocess_graph(graph)

    #get all target_nodes, check if nodes has 'cat' = 1
    target_nodes = [node for node, attributes in graph.nodes(data=True) if attributes['cat'] == 1]
    print("Number of target nodes : ", len(target_nodes))
    episode_rewards = []
    #data = graph_to_data(graph)
    env = GraphTraversalEnv(graph, target_nodes, obs_is_full_graph=True)

    check_parameters(env)
    windowed_success = 0
    num_episodes = 5000 if training else 2
    stats = {"nb_success": 0}
    range_episode = trange(num_episodes, desc="Episode", leave=True)
    max_reward = -500
    max_key_found = 0
    max_posssible_key = 0


    #find the factor to which I have to multiply curr_eps such that at the end of the training it is 0.05
    
    curr_eps = 0.99
    for episode in range_episode:
        observation = env.reset()
        episode_reward = 0
        episode_stats = {"nb_of_moves": 0,
                         "nb_key_found": 0,
                         'nb_possible_keys' : 0}
        global EPS 
        if training:
            EPS = EPS * EPS_DECAY
        
        #a function of episode over num_epsiode, such that at the end it is 0.05, linear
        curr_eps =    (0.99) * (1 - episode / num_episodes)
        curr_episode_rewards = []
        while True:
            action_mask = env._get_action_mask()

            action = agent.act(observation,curr_eps, action_mask)
            new_observation, reward, done, info = env.step(action)
            next_action_mask = env._get_action_mask()
            curr_episode_rewards.append(reward)
            if training:
                agent.step(observation, action, reward, new_observation, done, action_mask, next_action_mask)

            episode_stats["nb_of_moves"] += 1
            
            if done:
                episode_stats["nb_key_found"] = info["nb_keys_found"]
                episode_stats["nb_possible_keys"] = info["nb_possible_keys"]
                if info["found_target"]:
                    stats["nb_success"] += 1
                    #print("Success !")
                    windowed_success += 1
                break
            
            observation = new_observation
        
        episode_reward = np.sum(curr_episode_rewards)
        """
        if episode == num_episodes - 1:
            plt.plot(curr_episode_rewards)
            plt.show()
        """
        """
        if episode_stats["nb_key_found"] > max_key_found:
            max_key_found = episode_stats["nb_key_found"]
            max_posssible_key = episode_stats["nb_possible_keys"]
        """
        if episode_reward > max_reward:
            max_reward = episode_reward
            max_key_found = episode_stats["nb_key_found"]
            max_posssible_key = episode_stats["nb_possible_keys"]


        if episode % 500 == 499:
            #plot the losses of the agent
            moving_average = np.convolve(agent.losses, np.ones((100,))/100, mode='valid')
            plt.plot(moving_average)
            plt.show()

        # Update the plot after each episode

        """
        ax.clear()
        ax.plot(agent.gradient_norms)
        ax.set_title("Gradient Norms During Training")
        ax.set_xlabel("Training Steps")
        ax.set_ylabel("Average Gradient Norm")
        plt.pause(0.001)  # Pause briefly to update the plot
        """


        avg_reward = np.mean(episode_rewards[-10:]) if len(episode_rewards) > 0 else 0.0
        keys_found = episode_stats["nb_key_found"]
        range_episode.set_description(f"MKF : {max_key_found}/{len(target_nodes)}/{max_posssible_key}, MER : {max_reward:.2f}, KeysFound : {keys_found} Avg Reward : {avg_reward:.2f} SR : {(stats['nb_success'] / (episode + 1)):.2f} eps : {curr_eps:.2f}")
        range_episode.refresh() # to show immediately the update
        episode_rewards.append(episode_reward)
        
    return episode_rewards, stats["nb_success"] / num_episodes

        #if episode % 500 == 0:
        #    print(f"Episode {episode + 1}: Reward = {episode_reward} \t Nb_Moves = {episode_stats['nb_of_moves']} \t Nb_Success = {stats['nb_success']} / {episode + 1}")




def visualize(rewards):
    # Visualization
    window_size = 30
    success_array = np.array(rewards)
    success = np.convolve(success_array, np.ones((window_size,))/window_size, mode='valid')
    plt.plot(success)
    plt.show()



#take random files from folder and execute
nb_random_files = 15

nb_try = 6

EPS = INIT_EPS

#get all files in the folder recursively
all_files = []
for root, dirs, files in os.walk(FOLDER):
    for file in files:
        if file.endswith(".graphml"):
            all_files.append(os.path.join(root, file))


for curr_try in range( nb_try):
    random_files = random.sample(all_files, nb_random_files)
    i = 0
    print(f"Try {curr_try + 1} / {nb_try}")
    for file in random_files:
        if file.endswith(".graphml"):
            i+=1
            print(f"[{i} / {nb_random_files}] : Executing Training for {file}")
            execute_for_graph(file, True)
    random_test_file = random.choice(all_files)
    print(f"Training done ")
    print(f"Executing Testing for {random_test_file}")
    rewards, succes_rate = execute_for_graph(random_test_file, False)
    print(f"Success rate : {succes_rate}")

    

#take random file from folder and execute
random_file = random.choice(all_files)
print(f"Executing Testing for {random_file}")
rewards, succes_rate = execute_for_graph(random_file, False)
print(f"Success rate : {succes_rate}")
#visualize(rewards)


# Save Model
save_model = input("Do you want to save the model? (y/n)")
if save_model == "y":
    model_name = input("Enter the model name: ")
    #check if folder exists
    if not os.path.exists("models"):
        os.makedirs("models")
    torch.save(agent.qnetwork_local.state_dict(), f"models/{model_name}.pt")