base_params = {
    'type': 'RAMBO',
    'universe': 'gym',

    'kwargs': {
        'log_dir': './logs/rambo',
        'log_wandb': False,

        'epoch_length': 1000,
        'n_epochs': 2000,
        'train_every_n_steps': 1,
        'n_train_repeat': 1,
        'eval_render_mode': None,
        'evaluate_interval': 1,
        'eval_n_episodes': 10,
        'eval_deterministic': True,
        'separate_mean_var': True,

        'discount': 0.99,
        'tau': 5e-3,
        'reward_scale': 1.0,

        'critic_lr': 3e-4,
        'actor_lr': 1e-4,
        'adv_lr': 3e-4,
        'real_ratio': 0.5,
        'train_adversarial': True,
        'model_train_freq': 1000,
        'model_retain_epochs': 5,
        'rollout_batch_size': 50e3,
        'deterministic': False,
        'num_networks': 7,
        'num_elites': 5,
        'max_model_t': None,
        'pretrain_bc': False,
        'dynamics_pretrain_epochs': 500,
        # enter None or model path without extension: /Users/hfml/Documents/Ran/rambo/logs/hopper-medium-expert-v2/models
        'model_load_dir': None,
        # enter None or checkpoint path up to task name: /Users/hfml/Documents/Ran/rambo/logs/hopper-medium-expert-v2
        'checkpoint_load_dir': None,
    }
}
