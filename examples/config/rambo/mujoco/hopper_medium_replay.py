from ..base import base_params
from copy import deepcopy

params = deepcopy(base_params)
params.update({
    'domain': 'hopper',
    'task': 'medium-replay-v2',
    'exp_name': 'hopper_medium_replay'
})
params['kwargs'].update({
    'pool_load_path': 'd4rl/hopper-medium-replay-v2',
    'rollout_length': 2,
    'adversary_loss_weighting': 0.0768,
})
