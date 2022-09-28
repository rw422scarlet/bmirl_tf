from ..base import base_params
from copy import deepcopy

params = deepcopy(base_params)
params.update({
    'domain': 'antmaze',
    'task': 'medium-diverse-v0',
    'exp_name': 'antmaze_medium_diverse'
})
params['kwargs'].update({
    'pool_load_path': 'd4rl/antmaze-medium-diverse-v0',
    'rollout_length': 5,
    'adversary_loss_weighting': 0,
    'rollout_random': True,
    'pretrain_bc': False
})
