import os
import math
import json
import pickle
import datetime
from collections import OrderedDict
from numbers import Number
from itertools import count
import gtimer as gt
import pdb
import sys
import copy

import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp
from tensorflow.python.training import training_util

from softlearning.algorithms.rl_algorithm import RLAlgorithm
from softlearning.replay_pools.simple_replay_pool import SimpleReplayPool
from softlearning.models.feedforward import feedforward_model

from rambo.models.constructor import construct_model, format_samples_for_training
from rambo.models.fake_env import FakeEnv
from rambo.utils.writer import Writer
from rambo.utils.visualization import visualize_policy
from rambo.utils.logging import Progress
import rambo.utils.utils as utl
import rambo.utils.filesystem as filesystem
import rambo.off_policy.loader as loader
from rambo.off_policy.loader import restore_pool_from_d4rl_trajectories

def soft_clamp(x, _min, _max):
    x = _max - tf.math.softplus(_max - x)
    x = _min + tf.math.softplus(x - _min)
    return x

def compute_reward(observation, action, terminated, rwd_clip_max, rwd_model, use_done_flag=True, clip=True):
    if use_done_flag:
        rwd = rwd_model([
            observation * (1 - terminated), 
            action * (1 - terminated),
            terminated,
        ])
    else:
        rwd = rwd_model([observation, action,])

    if clip:
        rwd = soft_clamp(rwd, -rwd_clip_max, rwd_clip_max)
    return rwd

def td_target(reward, discount, next_value, terminated, use_done_flag=True):
    value_done = discount / (1 - discount) * reward
    return reward + (1 - terminated) * discount * next_value + use_done_flag * (terminated) * value_done

class BIRL(RLAlgorithm):
    """ Bayesian inverse reinforcement learning """
    def __init__(
            self,
            training_environment,
            evaluation_environment,
            policy,
            Qs,
            pool,
            static_fns,
            plotter=None,
            tf_summaries=False,

            log_dir=os.getcwd(),
            log_wandb=False,
            wandb_project="RAMBO-RL",
            wandb_group="",
            config=None,

            num_expert_traj=50,
            rwd_done_flag=True,
            rwd_clip_max=10.,
            rwd_rollout_batch_size=32,
            rwd_rollout_length=100,
            rwd_udpate_method='traj',
            rwd_lr=1e-4,
            rwd_update_steps=50,
            grad_penalty=1.,
            l2_penalty=0.001,

            train_adversarial=True,
            start_adv_train_epoch=0,
            end_adv_train_epoch=float('inf'),
            adversary_loss_weighting=0.01,
            epoch_per_adv_update=1,
            adv_lr=3e-4,
            include_entropy_in_adv=False,
            use_state_action_baseline=True,
            evaluate_interval=10,
            update_adv_ratio=1.0,
            adv_update_steps=50,
            adv_rollout_length=10,
            normalize_states=True,
            normalize_rewards=False,

            critic_lr=3e-4,
            actor_lr=3e-4,
            reward_scale=1.0,
            target_entropy='auto',
            discount=0.99,
            tau=5e-3,
            alpha=0.2,
            min_alpha=0.001,
            auto_alpha=False,
            target_update_interval=1,
            action_prior='uniform',
            reparameterize=True,
            store_extra_policy_info=False,
            pretrain_bc=False,
            bc_lr=1e-4,
            bc_epochs=50,
            dynamics_pretrain_epochs=500,

            deterministic=False,
            rollout_random=False,
            model_rollout_freq=250,
            num_networks=7,
            num_elites=5,
            model_retain_epochs=20,
            rollout_batch_size=100e3,
            real_ratio=0.1,
            rollout_length=1,
            hidden_dim=200,
            max_model_t=None,
            model_type='mlp',
            separate_mean_var=False,
            identity_terminal=0,

            pool_load_path='',
            expert_load_path='',
            model_name=None,
            model_load_dir=None,
            checkpoint_load_dir=None,
            **kwargs,
    ):
        super(BIRL, self).__init__(**kwargs)
        self._obs_dim = np.prod(training_environment.active_observation_shape)
        self._act_dim = np.prod(training_environment.action_space.shape)
        self._model_type = model_type
        self._identity_terminal = identity_terminal
        self._hidden_dim = hidden_dim
        self._num_networks = num_networks
        self._num_elites = num_elites
        self._separate_mean_var = separate_mean_var
        self._model_name = model_name
        self._model_load_dir = model_load_dir
        self._deterministic = deterministic
        self._model = construct_model(obs_dim=self._obs_dim, act_dim=self._act_dim, hidden_dim=hidden_dim,
                                      num_networks=num_networks, num_elites=num_elites,
                                      model_type=model_type, separate_mean_var=separate_mean_var,
                                      name=model_name, load_dir=model_load_dir,
                                      deterministic=deterministic, session=self._session)
        self._static_fns = static_fns
        print("config", config)

        self._rollout_schedule = [20, 100, rollout_length, rollout_length]
        self._max_model_t = max_model_t

        self._model_retain_epochs = model_retain_epochs
        self._model_rollout_freq = model_rollout_freq
        self._rollout_batch_size = int(rollout_batch_size)
        self._deterministic = deterministic
        self._rollout_random = rollout_random
        self._real_ratio = real_ratio
        self._Q_avgs = list()
        self._n_iters_qvar = [100]
        
        # reward
        algo_params = config["algorithm_params"].toDict()["kwargs"]
        
        self._rwd_update_method = algo_params["rwd_update_method"]
        self._rwd_done_flag = algo_params["rwd_done_flag"]
        self._rwd_clip_max = algo_params["rwd_clip_max"]
        self._rwd_rollout_batch_size = algo_params["rwd_rollout_batch_size"]
        self._rwd_rollout_length = algo_params["rwd_rollout_length"]
        self._rwd_update_steps = algo_params["rwd_update_steps"]
        self._rwd_rollout_batch_size = max(
            self._rwd_rollout_batch_size, 
            int(self.sampler._batch_size * self._rwd_update_steps // self._rwd_rollout_length)
        )
        self._rwd_lr = algo_params["rwd_lr"]
        self._grad_penalty = algo_params["grad_penalty"]
        self._l2_penalty = algo_params["l2_penalty"]

        if self._rwd_done_flag:
            self._reward = feedforward_model(
                input_shapes=((self._obs_dim,), (self._act_dim,), (1,)),
                output_size=1, 
                hidden_layer_sizes=config["Q_params"]['kwargs']["hidden_layer_sizes"],
            ) # reward model with terminal flag at end
        else:
            self._reward = feedforward_model(
                input_shapes=((self._obs_dim,), (self._act_dim,)),
                output_size=1, 
                hidden_layer_sizes=config["Q_params"]['kwargs']["hidden_layer_sizes"],
            )

        # model
        self._start_adv_train_epoch = start_adv_train_epoch
        self._end_adv_train_epoch = end_adv_train_epoch
        self._adversary_loss_weighting = adversary_loss_weighting
        self._epoch_per_adv_update = epoch_per_adv_update
        self._adv_lr = adv_lr
        self._include_entropy_in_adv = include_entropy_in_adv
        self._evaluate_interval = evaluate_interval
        self._use_state_action_baseline = use_state_action_baseline
        self._adv_epoch = 0.
        self._update_adv_ratio = update_adv_ratio
        self._adv_update_steps = adv_update_steps
        self._adv_rollout_length = adv_rollout_length

        if self._adversary_loss_weighting == 0:
            self._train_adversarial = False
        else:
            self._train_adversarial = train_adversarial
        
        # init logging
        date_time = datetime.datetime.now().strftime("%m-%d-%Y-%H-%M-%S")
        self._log_dir = os.path.join(log_dir, "{}-{}/{}".format(
            config["environment_params"]["training"]["domain"],
            config["environment_params"]["training"]["task"],
            date_time,
        ))
        if not os.path.exists(self._log_dir):
            os.makedirs(self._log_dir)
        
        # save config
        with open(os.path.join(self._log_dir, "config.json"), "w") as f:
            json.dump(config, f)
        self._writer = Writer(self._log_dir, log_wandb, wandb_project, wandb_group, config)
        print('[ RAMBO ] WANDB Group: {}'.format(wandb_group))

        self._training_environment = training_environment
        self._evaluation_environment = evaluation_environment
        self._policy = policy

        self._Qs = Qs
        self._Q_targets = tuple(tf.keras.models.clone_model(Q) for Q in Qs)

        self._pool = pool # trajectory pool
        self._plotter = plotter
        self._tf_summaries = tf_summaries

        self._policy_lr = actor_lr
        self._Q_lr = critic_lr

        self._bc_lr = bc_lr
        self._bc_epochs = bc_epochs
        self._do_bc = pretrain_bc
        self.dynamics_pretrain_epochs = dynamics_pretrain_epochs

        self._reward_scale = reward_scale
        self._alpha = alpha
        self._min_alpha = min_alpha
        self._auto_alpha = auto_alpha
        self._target_entropy = (
            -np.prod(self._training_environment.action_space.shape)
            if target_entropy == 'auto'
            else target_entropy)
        print('[ RAMBO ] Target entropy: {}'.format(self._target_entropy))

        self._discount = discount
        self._tau = tau
        self._target_update_interval = target_update_interval
        self._action_prior = action_prior

        self._reparameterize = reparameterize
        self._store_extra_policy_info = store_extra_policy_info

        observation_shape = self._training_environment.active_observation_shape
        action_shape = self._training_environment.action_space.shape

        assert len(observation_shape) == 1, observation_shape
        self._observation_shape = observation_shape
        assert len(action_shape) == 1, action_shape
        self._action_shape = action_shape

        #### load replay pool data
        self._pool_load_path = pool_load_path

        obs_mean, obs_std = loader.restore_pool(
            self._pool,
            self._pool_load_path,
            save_path=self._log_dir,
            normalize_states=normalize_states,
            normalize_rewards=normalize_rewards
        )
        if normalize_states:
            self._obs_mean = obs_mean
            self._obs_std = obs_std
        else:
            self._obs_mean, self._obs_std = None, None

        self._init_pool_size = self._pool.size
        print('[ RAMBO ] Starting with pool size: {}'.format(self._init_pool_size))
        ####

        self.fake_env = FakeEnv(self._model, self._static_fns, penalty_coeff=0,
                                obs_mean=self._obs_mean, obs_std=self._obs_std)
        
        # additional pools
        self._expert_load_path = expert_load_path
        print("loading expert pool from", self._expert_load_path)

        self._expert_pool = SimpleReplayPool(
            self._pool._observation_space,
            self._pool._action_space,
            2e6
        )
        restore_pool_from_d4rl_trajectories(
            self._expert_pool,
            self._expert_load_path,
            num_expert_traj,
            obs_mean=obs_mean,
            obs_std=obs_std
        )
        
        self._checkpoint_load_dir = checkpoint_load_dir
        if checkpoint_load_dir is not None:
            self._load_checkpoint()
        self._build()
        self._state_samples = None
        self._batch_for_testing = None

    def _build(self):
        self._training_ops = {}

        self._init_global_step()
        self._init_placeholders()
        self._init_actor_update()
        self._init_critic_update()
        self._init_bc_update()

    def _train(self):
        """Return a generator that performs RAMBO offline RL training.
        """
        training_environment = self._training_environment
        evaluation_environment = self._evaluation_environment
        policy = self._policy
        pool = self._pool
        model_metrics = {}

        if not self._training_started:
            self._init_training()

        self.sampler.initialize(training_environment, policy, pool)

        gt.reset_root()
        gt.rename_root('RLAlgorithm')
        gt.set_def_unique(False)

        self._training_before_hook()

        if self._do_bc:
            print('[ RAMBO ] Behaviour cloning policy for {} epochs'.format(
                self._bc_epochs)
            )
            self._pretrain_bc(n_epochs=self._bc_epochs)

        #### model training
        print('[ RAMBO ] log_dir: {} | ratio: {}'.format(self._log_dir, self._real_ratio))
        print('[ RAMBO ] Training model at epoch {} | freq {} | timestep {} (total: {})'.format(
            self._epoch, self._model_rollout_freq, self._timestep, self._total_timestep)
        )

        max_epochs = 1 if self._model.model_loaded else self.dynamics_pretrain_epochs
        model_train_metrics = self._train_model(
            batch_size=256,
            max_epochs=max_epochs,
            max_epochs_since_update=10,
            holdout_ratio=0.1,
            max_t=self._max_model_t
            )

        model_metrics.update(model_train_metrics)
        self._log_model()
        if self._rwd_update_method == "traj":
            self._init_reward_update_traj()
        else:
            self._init_reward_update_marginal()
        self._init_adversarial_model_update()
        gt.stamp('epoch_train_model')
        adv_stats = {}
        ####


        # number of times to alternate between agent and adversary
        for outer in range(self._n_epochs // self._epoch_per_adv_update):

            # train the agent
            for self._epoch in gt.timed_for(range(self._epoch + 1, self._epoch + self._epoch_per_adv_update + 1)):
                self._epoch_before_hook()
                gt.stamp('epoch_before_hook')

                self._set_rollout_length()
                start_samples = self.sampler._total_samples
                for timestep in count():
                    self._timestep = timestep

                    if (timestep >= self._epoch_length
                        and self.ready_to_train):
                        break

                    self._timestep_before_hook()
                    gt.stamp('timestep_before_hook')

                    ## model rollouts
                    if timestep % self._model_rollout_freq == 0 and self._real_ratio < 1.0:
                        self._reallocate_model_pool()
                        model_rollout_metrics, _ = self._rollout_model(
                            self._model_pool,
                            rollout_batch_size=self._rollout_batch_size, 
                            rollout_length=self._rollout_length,
                            deterministic=self._deterministic,
                        )
                        model_metrics.update(model_rollout_metrics)

                        gt.stamp('epoch_rollout_model')

                    ## train actor and critic
                    if self.ready_to_train:
                        self._do_agent_training_repeats(timestep=timestep)
                    gt.stamp('train')

                    self._timestep_after_hook()
                    gt.stamp('timestep_after_hook')

            # reward and dynamics training loop
            while self._adv_epoch < self._update_adv_ratio * self._epoch:
                rwd_stats = self._train_reward()
                adv_stats = self._train_adversary()
                self._adv_epoch += 1
            
            # logging ops
            training_paths = self.sampler.get_last_n_paths(
                math.ceil(self._epoch_length / self.sampler._max_path_length))

            if self._epoch % self._evaluate_interval == 0 \
                or self._epoch >= self._n_epochs - self._avg_returns_num_iter:
                evaluation_paths = self._evaluation_paths(
                    policy,
                    evaluation_environment,
                    self._obs_mean,
                    self._obs_std
                )
                gt.stamp('evaluation_paths')

                evaluation_metrics = self._evaluate_rollouts(
                    evaluation_paths, evaluation_environment)
                gt.stamp('evaluation_metrics')
            else:
                evaluation_metrics = {}

            gt.stamp('epoch_after_hook')

            sampler_diagnostics = self.sampler.get_diagnostics()

            diagnostics = self.get_diagnostics(
                iteration=self._total_timestep,
                batch=self._evaluation_batch(),
                training_paths=training_paths)

            time_diagnostics = gt.get_times().stamps.itrs

            diagnostics.update(OrderedDict((
                *(
                    (f'evaluation/{key}', evaluation_metrics[key])
                    for key in sorted(evaluation_metrics.keys())
                ),
                *(
                    (f'times/{key}', time_diagnostics[key][-1])
                    for key in sorted(time_diagnostics.keys())
                ),
                *(
                    (f'sampler/{key}', sampler_diagnostics[key])
                    for key in sorted(sampler_diagnostics.keys())
                ),
                ('epoch', self._epoch),
                ('timestep', self._timestep),
                ('timesteps_total', self._total_timestep),
                ('train-steps', self._num_train_steps),
                *(
                    (f'model/{key}', model_metrics[key])
                    for key in sorted(model_metrics.keys())
                ),
            )))

            for iter in self._n_iters_qvar:
                diagnostics.update({
                    f'qvar/Q-var-{str(iter)}-iter': np.std(np.array(self._Q_avgs[max(self._epoch - iter, 0):]))**2
                })

            current_losses = self._model.validate()
            for i in range(len(current_losses)):
                diagnostics.update({'model/current_val_loss_' + str(i): current_losses[i]})
            diagnostics.update({'model/current_val_loss_avg': current_losses.mean()})
            diagnostics.update(adv_stats)
            diagnostics.update(rwd_stats)

            self._writer.add_dict(diagnostics, self._epoch)
            self._save_checkpoint()

            for item in diagnostics.items():
                print(item)

            if self._eval_render_mode is not None and hasattr(
                    evaluation_environment, 'render_rollouts'):
                training_environment.render_rollouts(evaluation_paths)

            ## ensure we did not collect any more data
            assert self._pool.size == self._init_pool_size

            yield diagnostics

        self.sampler.terminate()

        self._training_after_hook()

        yield {'done': True, **diagnostics}
    
    def _train_reward(self):
        if self._rwd_update_method == 'traj':
            rwd_stats = self._train_reward_traj()
        else:
            rwd_stats = self._train_reward_marginal()
        return rwd_stats
    
    def _train_reward_traj(self):
        rwd_loss_epoch = []
        decay_loss_epoch = []
        for i in range(self._rwd_update_steps):
            batch = self._expert_pool.random_batch(self._rwd_rollout_batch_size)

            obs_real = obs_fake = batch['observations']
            act_real = batch['actions']
            act_fake = self._policy.actions_np(obs_fake)
            _, real_traj = self._rollout_model(
                None,
                self._rwd_rollout_batch_size,
                self._rwd_rollout_length,
                obs=obs_real,
                act=act_real,
                terminate_early=False,
                deterministic=self._deterministic,
            )
            _, fake_traj = self._rollout_model(
                None,
                self._rwd_rollout_batch_size,
                self._rwd_rollout_length,
                obs=obs_real,
                act=act_fake,
                terminate_early=False,
                deterministic=self._deterministic,
            )
            real_obs = np.stack(real_traj["obs"]).transpose((1, 0, 2))
            real_act = np.stack(real_traj["act"]).transpose((1, 0, 2))
            real_done = np.stack(real_traj["done"]).transpose((1, 0, 2))      

            fake_obs = np.stack(fake_traj["obs"]).transpose((1, 0, 2))
            fake_act = np.stack(fake_traj["act"]).transpose((1, 0, 2))
            fake_done = np.stack(fake_traj["done"]).transpose((1, 0, 2))

            feed_dict = {
                self._real_observations_traj_ph: real_obs,
                self._real_actions_traj_ph: real_act,
                self._real_terminals_traj_ph: real_done,
                self._fake_observations_traj_ph: fake_obs,
                self._fake_actions_traj_ph: fake_act,
                self._fake_terminals_traj_ph: fake_done,
            }
            
            rwd_loss, decay_loss, _ = self._session.run(
                (self._rwd_loss, self._decay_loss, self._rwd_train_op), 
                feed_dict
            )
            rwd_loss_epoch.append(rwd_loss)
            decay_loss_epoch.append(decay_loss)

        stats = {
            "rwd_loss": np.mean(rwd_loss_epoch), 
            "decay_loss": np.mean(decay_loss_epoch), 
        }
        return stats
    
    def _train_reward_marginal(self):
        rwd_loss_epoch = []
        gp_loss_epoch = []
        steps = 0
        while steps < self._rwd_update_steps:
            batch = self._expert_pool.random_batch(self._rwd_rollout_batch_size)

            obs_real = obs_fake = batch['observations']
            act_real = batch['actions']
            act_fake = self._policy.actions_np(obs_fake)
            for t in range(self._rwd_rollout_length):
                if t > 0:
                    act_real = self._policy.actions_np(obs_real)
                    act_fake = self._policy.actions_np(obs_fake)

                feed_dict = {
                    self._observations_ph: obs_real, # dummy
                    self._actions_ph: act_real, # dummy
                    self._real_observations_ph: obs_real,
                    self._real_actions_ph: act_real,
                    self._fake_observations_ph: obs_fake,
                    self._fake_actions_ph: act_fake,
                }
                
                next_obs_real, next_obs_fake, rwd_loss, gp, _ = self._session.run(
                    (
                        self._next_obs_real, 
                        self._next_obs_fake, 
                        self._rwd_loss,
                        self._gp,
                        self._rwd_train_op,
                    ),
                    feed_dict
                )

                rwd_loss_epoch.append(rwd_loss)
                gp_loss_epoch.append(gp)

                obs_real = next_obs_real
                obs_fake = next_obs_fake

                steps += 1
                if steps == self._rwd_update_steps:
                    break
        
        stats = {
            "rwd_loss": np.mean(rwd_loss_epoch), 
            "gp_loss": np.mean(gp_loss_epoch), 
        }
        return stats
    
    def _train_adversary(self):
        """ train adversarial model using on-policy updates.
        """
        if (self._epoch < self._start_adv_train_epoch) or not self._train_adversarial:
            return
        
        adv_loss_epoch = []
        obs_loss_epoch = []
        steps = 0
        while steps < self._adv_update_steps:
            batch = self._expert_pool.random_batch(self._rwd_rollout_batch_size)

            obs_real = obs_fake = batch['observations']
            act_real = batch['actions']
            act_fake = self._policy.actions_np(obs_fake)
            for t in range(self._adv_rollout_length):
                if t > 0:
                    act_real = self._policy.actions_np(obs_real)
                    act_fake = self._policy.actions_np(obs_fake)
                
                inputs, targets = self._model.get_labeled_batch()

                feed_dict = {
                    self._observations_ph: obs_real, # dummy
                    self._actions_ph: act_real, # dummy
                    self._real_observations_ph: obs_real,
                    self._real_actions_ph: act_real,
                    self._fake_observations_ph: obs_fake,
                    self._fake_actions_ph: act_fake,
                    self._model.sy_train_in: inputs,
                    self._model.sy_train_targ: targets
                }

                next_obs_real, next_obs_fake, adv_loss, supervised_loss, _ = self._session.run(
                    (
                        self._next_obs_real, 
                        self._next_obs_fake, 
                        self._adv_objective, 
                        self._supervised_loss, 
                        self._adversarial_train_op
                    ),
                    feed_dict
                )

                adv_loss_epoch.append(adv_loss)
                obs_loss_epoch.append(supervised_loss)

                obs_real = next_obs_real
                obs_fake = next_obs_fake

                steps += 1
                if steps == self._adv_update_steps:
                    break
        
        stats = {
            "adv_loss": np.mean(adv_loss_epoch), 
            "obs_loss": np.mean(obs_loss_epoch)
        }
        return stats

    def train(self, *args, **kwargs):
        return self._train(*args, **kwargs)

    def _log_policy(self):
        save_path = os.path.join(self._log_dir, 'models')
        filesystem.mkdir(save_path)
        weights = self._policy.get_weights()
        data = {'policy_weights': weights}
        full_path = os.path.join(save_path, 'policy_{}.pkl'.format(self._total_timestep))
        print('Saving policy to: {}'.format(full_path))
        pickle.dump(data, open(full_path, 'wb'))

    def _log_model(self):
        print('MODEL: {}'.format(self._model_type))
        if self._model_type == 'identity':
            print('[ RAMBO ] Identity model, skipping save')
        elif self._model.model_loaded:
            print('[ RAMBO ] Loaded model, skipping save')
        else:
            self._save_path = os.path.join(self._log_dir, 'models')
            filesystem.mkdir(self._save_path)
            print('[ RAMBO ] Saving model to: {}'.format(self._save_path))
            self._model.save(self._save_path, "BNN_pretrain")
    
    def _save_checkpoint(self):
        save_path = os.path.join(self._log_dir, 'models')
        filesystem.mkdir(save_path)
        policy_weights = self._policy.get_weights()
        q_weights = [Q.get_weights() for Q in self._Qs]
        data = {'policy_weights': policy_weights, 'q_weights': q_weights}
        full_path = os.path.join(save_path, 'checkpoint.pkl')
        print('Saving checkpoint to: {}'.format(full_path))
        pickle.dump(data, open(full_path, 'wb'))
        self._model.save(save_path, "BNN")

    def _load_checkpoint(self):
        save_path = os.path.join(self._checkpoint_load_dir, 'models')
        full_path = os.path.join(save_path, 'checkpoint.pkl')
        print('loading checkpoint from: {}'.format(full_path))
        data = pickle.load(open(full_path, 'rb'))
        self._policy.set_weights(data["policy_weights"])
        for Q, weight in zip(self._Qs, data["q_weights"]):
            Q.set_weights(weight)
        self._model.load_params(os.path.join(save_path, "BNN"))

    def _set_rollout_length(self):
        min_epoch, max_epoch, min_length, max_length = self._rollout_schedule
        if self._epoch <= min_epoch:
            y = min_length
        else:
            dx = (self._epoch - min_epoch) / (max_epoch - min_epoch)
            dx = min(dx, 1)
            y = dx * (max_length - min_length) + min_length

        self._rollout_length = int(y)
        print('[ Model Length ] Epoch: {} (min: {}, max: {}) | Length: {} (min: {} , max: {})'.format(
            self._epoch, min_epoch, max_epoch, self._rollout_length, min_length, max_length
        ))

    def _reallocate_model_pool(self):
        obs_space = self._pool._observation_space
        act_space = self._pool._action_space

        rollouts_per_epoch = self._rollout_batch_size * self._epoch_length / self._model_rollout_freq
        model_steps_per_epoch = int(self._rollout_length * rollouts_per_epoch)
        new_pool_size = self._model_retain_epochs * model_steps_per_epoch

        if not hasattr(self, '_model_pool'):
            print('[ RAMBO ] Initializing new model pool with size {:.2e}'.format(
                new_pool_size
            ))
            self._model_pool = SimpleReplayPool(obs_space, act_space, new_pool_size)

        elif self._model_pool._max_size != new_pool_size:
            print('[ RAMBO ] Updating model pool | {:.2e} --> {:.2e}'.format(
                self._model_pool._max_size, new_pool_size
            ))
            samples = self._model_pool.return_all_samples()
            new_pool = SimpleReplayPool(obs_space, act_space, new_pool_size)
            new_pool.add_samples(samples)
            assert self._model_pool.size == new_pool.size
            self._model_pool = new_pool

    def _train_model(self, **kwargs):
        if self._model_type == 'identity':
            print('[ RAMBO ] Identity model, skipping model')
            model_metrics = {}
        else:
            env_samples = self._pool.return_all_samples()
            train_inputs, train_outputs = format_samples_for_training(env_samples)
            model_metrics = self._model.train(train_inputs, train_outputs, **kwargs)
        return model_metrics

    def _pretrain_bc(self, batch_size=256, n_epochs=50, max_logging=2000, holdout_ratio=0.1):
        """ Pretrain the policy using behaviour cloning on the dataset
        """
        progress = Progress(n_epochs)

        env_samples = self._pool.return_all_samples()
        obs = env_samples["observations"]
        act = env_samples["actions"]

        # Split into training and holdout sets
        num_holdout = min(int(obs.shape[0] * holdout_ratio), max_logging)
        permutation = np.random.permutation(obs.shape[0])
        obs, holdout_obs = obs[permutation[num_holdout:]], obs[permutation[:num_holdout]]
        act, holdout_act = act[permutation[num_holdout:]], act[permutation[:num_holdout]]
        idxs = np.random.randint(obs.shape[0], size=[obs.shape[0]])

        mse_loss = self._session.run(
            self._mse_loss,
            feed_dict = {
                self._observations_ph: holdout_obs,
                self._actions_ph: holdout_act
            }
        )

        for i in range(n_epochs):
            for batch_num in range(int(obs.shape[0] // batch_size)):
                batch_idxs = idxs[batch_num * batch_size:(batch_num + 1) * batch_size]
                acts = act[batch_idxs]
                obss = obs[batch_idxs]
                if np.max(acts) >= 1.0 or np.min(acts) <= -1.0:
                    continue

                feed_dict = {
                    self._observations_ph: obss,
                    self._actions_ph: acts
                }
                self._session.run(self._bc_train_op, feed_dict)

            mse_loss = self._session.run(
                self._mse_loss,
                feed_dict = {
                    self._observations_ph: holdout_obs,
                    self._actions_ph: holdout_act
                }
            )

            progress.update()
            progress.set_description([['BC loss', mse_loss]])

    def _rollout_model(self, pool, rollout_batch_size, rollout_length, obs=None, act=None, terminate_early=True, **kwargs):
        print('[ Model Rollout ] Starting | Epoch: {} | Rollout length: {} | Batch size: {} | Type: {}'.format(
            self._epoch, rollout_length, rollout_batch_size, self._model_type
        ))
        if obs is None:
            batch = self.sampler.random_batch(rollout_batch_size)
            obs = batch['observations']

        rollout_traj = {"obs": [], "act": [], "done": []}
        steps_added = []
        for i in range(rollout_length):
            if not self._rollout_random:
                if act is None or i > 0:
                    act = self._policy.actions_np(obs)
            else:
                act_ = self._policy.actions_np(obs)
                act = np.random.uniform(low=-1, high=1, size=act_.shape)

            if self._model_type == 'identity':
                next_obs = obs
                rew = np.zeros((len(obs), 1))
                term = (np.ones((len(obs), 1)) * self._identity_terminal).astype(np.bool)
                info = {}
            else:
                next_obs, rew, term, info = self.fake_env.step(obs, act, **kwargs)
            steps_added.append(len(obs))

            samples = {'observations': obs, 'actions': act, 'next_observations': next_obs, 'rewards': rew, 'terminals': term}
            if pool is not None:
                pool.add_samples(samples)
            
            if terminate_early:
                nonterm_mask = ~term.squeeze(-1)
                if nonterm_mask.sum() == 0:
                    print('[ Model Rollout ] Breaking early: {} | {} / {}'.format(i, nonterm_mask.sum(), nonterm_mask.shape))
                    break

                obs = next_obs[nonterm_mask]
            else:
                rollout_traj["obs"].append(obs.copy())
                rollout_traj["act"].append(act.copy())
                rollout_traj["done"].append(term.copy())
                obs = next_obs

        mean_rollout_length = sum(steps_added) / rollout_batch_size
        rollout_stats = {'mean_rollout_length': mean_rollout_length,
                        'max_reward': np.max(rew),
                        'min_reward': np.min(rew),
                        'avg_reward': np.mean(rew),
                        'std_reward': np.std(rew)}
        if pool is not None:
            print('[ Model Rollout ] Added: {:.1e} | Model pool: {:.1e} (max {:.1e}) | Length: {} | Train rep: {}'.format(
                sum(steps_added), pool.size, pool._max_size, mean_rollout_length, self._n_train_repeat
            ))
        return rollout_stats, rollout_traj

    def _visualize_model(self, env, timestep):
        ## save env state
        state = env.unwrapped.state_vector()
        qpos_dim = len(env.unwrapped.sim.data.qpos)
        qpos = state[:qpos_dim]
        qvel = state[qpos_dim:]

        print('[ Visualization ] Starting | Epoch {} | Log dir: {}\n'.format(self._epoch, self._log_dir))
        visualize_policy(env, self.fake_env, self._policy, self._writer, timestep)
        print('[ Visualization ] Done')
        ## set env state
        env.unwrapped.set_state(qpos, qvel)

    def _training_batch(self, batch_size=None):
        batch_size = batch_size or self.sampler._batch_size
        env_batch_size = int(batch_size*self._real_ratio)
        model_batch_size = batch_size - env_batch_size

        ## can sample from the env pool even if env_batch_size == 0
        env_batch = self._pool.random_batch(env_batch_size)

        obs = env_batch["observations"]
        next_obs = env_batch["next_observations"]
        deltas = next_obs - obs

        if model_batch_size > 0:
            model_batch = self._model_pool.random_batch(model_batch_size)

            # keys = env_batch.keys()
            keys = set(env_batch.keys()) & set(model_batch.keys())
            batch = {k: np.concatenate((env_batch[k], model_batch[k]), axis=0) for k in keys}
        else:
            ## if real_ratio == 1.0, no model pool was ever allocated,
            ## so skip the model pool sampling
            batch = env_batch
        return batch

    def _real_data_batch(self, batch_size=None):
        batch_size = batch_size or self.sampler._batch_size
        env_batch = self._pool.random_batch(batch_size)
        return env_batch

    def _compare_policy_to_data(self):
        """
        Compute the mean squared error between the actions taken in the dataset
        and the actions taken under the current policy.

        Returns:
            mae_per_action: the mean absolute error for each action dimension
            of the policy actions versus the dataset actions.
        """
        expert_batch = self._expert_pool.random_batch(self.sampler._batch_size)
        observations = expert_batch["observations"]
        dataset_actions = expert_batch["actions"]
        with self._policy.set_deterministic(True):
            policy_actions = self._policy.actions_np(observations)
        mae_per_action = (np.abs(dataset_actions - policy_actions)).mean(axis=0)
        return mae_per_action.tolist()

    def _init_global_step(self):
        self.global_step = training_util.get_or_create_global_step()
        self._training_ops.update({
            'increment_global_step': training_util._increment_global_step(1)
        })

    def _init_placeholders(self):
        """Create input placeholders for the SAC algorithm.

        Creates `tf.placeholder`s for:
            - observation
            - next observation
            - action
            - reward
            - terminals
        """
        self._iteration_ph = tf.placeholder(
            tf.int64, shape=None, name='iteration')

        self._observations_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._observation_shape),
            name='observation',
        )

        self._next_observations_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._observation_shape),
            name='next_observation',
        )

        self._actions_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._action_shape),
            name='actions',
        )

        self._rewards_ph = tf.placeholder(
            tf.float32,
            shape=(None, 1),
            name='rewards',
        )

        self._terminals_ph = tf.placeholder(
            tf.float32,
            shape=(None, 1),
            name='terminals',
        )
        
        # reward edits
        self._real_observations_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._observation_shape),
            name='real_observations',
        )

        self._real_actions_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._action_shape),
            name='real_actions',
        )

        self._fake_observations_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._observation_shape),
            name='fake_observations',
        )

        self._fake_actions_ph = tf.placeholder(
            tf.float32,
            shape=(None, *self._action_shape),
            name='fake_actions',
        )
        
        if self._rwd_update_method == "traj":
            self._real_observations_traj_ph = tf.placeholder(
                tf.float32,
                shape=(None, None, *self._observation_shape),
                name='real_observations_traj',
            )

            self._real_actions_traj_ph = tf.placeholder(
                tf.float32,
                shape=(None, None, *self._action_shape),
                name='real_actions_traj',
            )

            self._real_terminals_traj_ph = tf.placeholder(
                tf.float32,
                shape=(None, None, 1),
                name='real_terminals_traj',
            )

            self._fake_observations_traj_ph = tf.placeholder(
                tf.float32,
                shape=(None, None, *self._observation_shape),
                name='fake_observations_traj',
            )

            self._fake_actions_traj_ph = tf.placeholder(
                tf.float32,
                shape=(None, None, *self._action_shape),
                name='fake_actions_traj',
            )

            self._fake_terminals_traj_ph = tf.placeholder(
                tf.float32,
                shape=(None, None, 1),
                name='fake_terminals_traj',
            )

        if self._store_extra_policy_info:
            self._log_pis_ph = tf.placeholder(
                tf.float32,
                shape=(None, 1),
                name='log_pis',
            )
            self._raw_actions_ph = tf.placeholder(
                tf.float32,
                shape=(None, *self._action_shape),
                name='raw_actions',
            )

    def _get_Q_target(self):
        reward = tf.stop_gradient(self._reward_scale * compute_reward(
            self._observations_ph, 
            self._actions_ph,
            self._terminals_ph,
            self._rwd_clip_max,
            self._reward,
            self._rwd_done_flag,
            clip=True
        ))
        next_actions = self._policy.actions([self._next_observations_ph])
        next_log_pis = self._policy.log_pis(
            [self._next_observations_ph], next_actions)

        next_Qs_values = tuple(
            Q([self._next_observations_ph, next_actions])
            for Q in self._Q_targets)

        min_next_Q = tf.reduce_min(next_Qs_values, axis=0)
        next_value = min_next_Q - self._alpha * next_log_pis

        Q_target = td_target(
            reward=reward,
            discount=self._discount,
            next_value=next_value,
            terminated=self._terminals_ph,
            use_done_flag=self._rwd_done_flag
        ) # reward edit

        return Q_target

    def _init_bc_update(self):
        """ Initialise update to initially perform behaviour cloning on the
        dataset prior to running rambo.
        """
        log_pis = self._policy.log_pis([self._observations_ph], self._actions_ph)
        bc_loss = self._bc_loss = -tf.reduce_mean(log_pis)

        actions = self._policy.actions([self._observations_ph])
        mse = self._mse_loss = tf.reduce_mean(tf.square(actions - self._actions_ph))

        self._bc_optimizer = tf.train.AdamOptimizer(
            learning_rate=self._bc_lr,
            name="bc_optimizer")
        bc_train_op = tf.contrib.layers.optimize_loss(
            bc_loss,
            self.global_step,
            learning_rate=self._bc_lr,
            optimizer=self._policy_optimizer,
            variables=self._policy.trainable_variables,
            increment_global_step=False,
            summaries=(
                "loss", "gradients", "gradient_norm", "global_gradient_norm"
            ) if self._tf_summaries else ())

        self._bc_train_op = bc_train_op

    def _init_critic_update(self):
        """Create minimization operation for critic Q-function.

        Creates a `tf.optimizer.minimize` operation for updating
        critic Q-function with gradient descent, and appends it to
        `self._training_ops` attribute.
        """
        Q_target = tf.stop_gradient(self._get_Q_target())

        assert Q_target.shape.as_list() == [None, 1]

        Q_values = self._Q_values = tuple(
            Q([self._observations_ph, self._actions_ph])
            for Q in self._Qs)

        Q_losses = self._Q_losses = tuple(
            tf.losses.mean_squared_error(
                labels=Q_target, predictions=Q_value, weights=0.5)
            for Q_value in Q_values)

        self._Q_optimizers = tuple(
            tf.train.AdamOptimizer(
                learning_rate=self._Q_lr,
                name='{}_{}_optimizer'.format(Q._name, i)
            ) for i, Q in enumerate(self._Qs))
        Q_training_ops = tuple(
            tf.contrib.layers.optimize_loss(
                Q_loss,
                self.global_step,
                learning_rate=self._Q_lr,
                optimizer=Q_optimizer,
                variables=Q.trainable_variables,
                increment_global_step=False,
                summaries=((
                    "loss", "gradients", "gradient_norm", "global_gradient_norm"
                ) if self._tf_summaries else ()))
            for i, (Q, Q_loss, Q_optimizer)
            in enumerate(zip(self._Qs, Q_losses, self._Q_optimizers)))

        self._training_ops.update({'Q': tf.group(Q_training_ops)})

    def _init_actor_update(self):
        """Create minimization operations for policy and entropy.

        Creates a `tf.optimizer.minimize` operations for updating
        policy and entropy with gradient descent, and adds them to
        `self._training_ops` attribute.
        """

        actions = self._policy.actions([self._observations_ph])
        log_pis = self._policy.log_pis([self._observations_ph], actions)

        assert log_pis.shape.as_list() == [None, 1]

        log_alpha = self._log_alpha = tf.get_variable(
            'log_alpha',
            dtype=tf.float32,
            initializer=0.0)

        if isinstance(self._target_entropy, Number) and self._auto_alpha:
            alpha_loss = -tf.reduce_mean(
                log_alpha * tf.stop_gradient(log_pis + self._target_entropy))

            self._alpha_optimizer = tf.train.AdamOptimizer(
                self._policy_lr, name='alpha_optimizer')
            self._alpha_train_op = self._alpha_optimizer.minimize(
                loss=alpha_loss, var_list=[log_alpha])

            self._training_ops.update({
                'temperature_alpha': self._alpha_train_op
            })

        if self._auto_alpha:
            alpha = tf.clip_by_value(tf.exp(log_alpha), self._min_alpha, 1.)
            self._alpha = alpha
        else:
            alpha = tf.convert_to_tensor(
                self._alpha, dtype=None, dtype_hint=None, name=None
            )

        if self._action_prior == 'normal':
            policy_prior = tf.contrib.distributions.MultivariateNormalDiag(
                loc=tf.zeros(self._action_shape),
                scale_diag=tf.ones(self._action_shape))
            policy_prior_log_probs = policy_prior.log_prob(actions)
        elif self._action_prior == 'uniform':
            policy_prior_log_probs = 0.0

        Q_log_targets = tuple(
            Q([self._observations_ph, actions])
            for Q in self._Qs)
        min_Q_log_target = tf.reduce_min(Q_log_targets, axis=0)

        if self._reparameterize:
            policy_kl_losses = (
                alpha * log_pis
                - min_Q_log_target
                - policy_prior_log_probs)
        else:
            raise NotImplementedError

        assert policy_kl_losses.shape.as_list() == [None, 1]

        policy_loss = tf.reduce_mean(policy_kl_losses)

        self._policy_optimizer = tf.train.AdamOptimizer(
            learning_rate=self._policy_lr,
            name="policy_optimizer")
        policy_train_op = tf.contrib.layers.optimize_loss(
            policy_loss,
            self.global_step,
            learning_rate=self._policy_lr,
            optimizer=self._policy_optimizer,
            variables=self._policy.trainable_variables,
            increment_global_step=False,
            summaries=(
                "loss", "gradients", "gradient_norm", "global_gradient_norm"
            ) if self._tf_summaries else ())

        self._training_ops.update({'policy_train_op': policy_train_op})
    
    def _init_reward_update_traj(self):
        # compute reward loss
        real_rwd = self._real_rwd = self._reward_scale * compute_reward(
            self._real_observations_traj_ph, 
            self._real_actions_traj_ph,
            self._real_terminals_traj_ph,
            self._rwd_clip_max,
            self._reward,
            self._rwd_done_flag,
            clip=True
        )
        fake_rwd = self._fake_rwd = self._reward_scale * compute_reward(
            self._fake_observations_traj_ph, 
            self._fake_actions_traj_ph,
            self._fake_terminals_traj_ph,
            self._rwd_clip_max,
            self._reward,
            self._rwd_done_flag,
            clip=True
        )
        
        gamma = self._discount ** np.arange(self._rwd_rollout_length).reshape(1, -1, 1)
        reward_loss = self._rwd_loss = -(
            tf.reduce_mean(tf.reduce_sum(gamma * real_rwd, axis=1), axis=0) \
            - tf.reduce_mean(tf.reduce_sum(gamma * fake_rwd, axis=1), axis=0)
        )
        
        # compute weight decay
        rwd_vars = self._reward.trainable_variables
        decay_loss = self._decay_loss = tf.add_n([tf.nn.l2_loss(v) for v in rwd_vars])

        total_loss = reward_loss + self._l2_penalty * decay_loss

        self._rwd_optimizer = tf.train.AdamOptimizer(learning_rate=self._rwd_lr)
        self._rwd_train_op = self._rwd_optimizer.minimize(
            total_loss,
            var_list=self._reward.trainable_variables
        )
        self._session.run(tf.variables_initializer(self._rwd_optimizer.variables()))

    def _init_reward_update_marginal(self):
        real_terminals = self._terminals = self.fake_env.config.termination_fn_tf(
            utl.unnormalize(self._real_observations_ph, self._obs_mean, self._obs_std), 
            self._real_actions_ph,
            utl.unnormalize(self._real_observations_ph, self._obs_mean, self._obs_std)
        )
        done_real = tf.expand_dims(real_terminals, axis=-1)

        fake_terminals = self._terminals = self.fake_env.config.termination_fn_tf(
            utl.unnormalize(self._fake_observations_ph, self._obs_mean, self._obs_std), 
            self._fake_actions_ph,
            utl.unnormalize(self._fake_observations_ph, self._obs_mean, self._obs_std)
        )
        done_fake = tf.expand_dims(fake_terminals, axis=-1)
        
        # compute reward loss
        real_rwd = self._real_rwd = self._reward_scale * compute_reward(
            self._real_observations_ph, 
            self._real_actions_ph,
            done_real,
            self._rwd_clip_max,
            self._reward,
            self._rwd_done_flag,
            clip=True
        )
        fake_rwd = self._fake_rwd = self._reward_scale * compute_reward(
            self._fake_observations_ph, 
            self._fake_actions_ph,
            done_fake,
            self._rwd_clip_max,
            self._reward,
            self._rwd_done_flag,
            clip=True
        )
        
        rwd_loss = self._rwd_loss = -(tf.reduce_mean(real_rwd, axis=0) - tf.reduce_mean(fake_rwd, axis=0))

        # compute gradient penalty
        epsilon = tf.random_uniform([self._rwd_rollout_batch_size, 1], 0.0, 1.0)
        interp_observations = epsilon * self._real_observations_ph + (1 - epsilon) * self._fake_observations_ph
        interp_actions = epsilon * self._real_actions_ph + (1 - epsilon) * self._fake_actions_ph
        interp_terminals = epsilon * done_real + (1 - epsilon) * done_fake
        if self._rwd_done_flag:
            grad = tf.gradients(
                self._reward_scale * self._reward([interp_observations, interp_actions, interp_terminals]), 
                [interp_observations, interp_actions]
            )[0]
        else:
            grad = tf.gradients(
                self._reward_scale * self._reward([interp_observations, interp_actions]), 
                [interp_observations, interp_actions]
            )[0]

        grad_penalty = self._gp = tf.reduce_mean(tf.square(tf.norm(grad, ord=2) - 1))

        rwd_total_loss = rwd_loss + self._grad_penalty * grad_penalty

        self._rwd_optimizer = tf.train.AdamOptimizer(learning_rate=self._rwd_lr)
        self._rwd_train_op = self._rwd_optimizer.minimize(
            rwd_total_loss,
            var_list=self._reward.trainable_variables
        )
        self._session.run(tf.variables_initializer(self._rwd_optimizer.variables()))


    def _init_adversarial_model_update(self):
        """ Initialise update to adversarially modify the model.
        """
        def get_log_prob(states, means, stds):
            distribution = tfp.distributions.MultivariateNormalDiag(
                loc=means,
                scale_diag=stds
            )
            state_log_prob = distribution.log_prob(states)[:]
            return state_log_prob
        
        def sample_next_obs(obs, act):
            inputs = tf.concat([obs, act], -1)
            ensemble_model_means, ensemble_model_vars = self._model._compile_outputs(inputs)
            batch_size = self._rwd_rollout_batch_size

            # because model predicts deltas for observations add original obs
            ensemble_model_means = ensemble_model_means[:,:,1:]+self._observations_ph
            ensemble_model_stds = tf.math.sqrt(ensemble_model_vars[:, :, 1:])
            shape = tf.TensorShape([ensemble_model_means.shape[0], batch_size, ensemble_model_means.shape[2]])
            ensemble_samples = tf.stop_gradient(ensemble_model_means + tf.random.normal(shape) * ensemble_model_stds)

            # use one model from ensemble
            model_inds = self._model.random_inds(batch_size).astype(int)
            model_inds = [[model_inds[i], i] for i in range(len(model_inds))]
            samples = tf.gather_nd(ensemble_samples, model_inds)
            self._model_stds = ensemble_model_stds
            next_obs = samples
            
            # use terminals like mopo
            terminals = self._terminals = self.fake_env.config.termination_fn_tf(
                utl.unnormalize(obs, self._obs_mean, self._obs_std),
                act,
                utl.unnormalize(next_obs, self._obs_mean, self._obs_std)
            )
            done = tf.expand_dims(terminals, axis=-1)

            # compute mixture log prob
            log_prob = get_log_prob(
                samples,
                ensemble_model_means,
                ensemble_model_stds
            )

            # extract only the data from elites
            elite_inds = self._model.get_elite_inds()
            log_prob = tf.gather(log_prob, elite_inds, axis=0)

            # correct for fact that transition is sampled uniformly from elites
            prob = tf.math.exp(tf.cast(log_prob, tf.float64))
            prob = prob * (1/len(elite_inds))
            prob = tf.reduce_sum(prob, axis=0)
            log_prob = tf.cast(tf.math.log(prob), tf.float32)
            
            return next_obs, done, log_prob

        def compute_advantage(obs, act, next_obs, done):
            with self._policy.set_deterministic(True):
                next_actions = tf.stop_gradient(self._policy.actions([next_obs]))

            next_Qs_values = tuple(
                tf.stop_gradient(Q([next_obs, next_actions]))
                for Q in self._Qs)

            min_next_Q = tf.squeeze(tf.reduce_min(next_Qs_values, axis=0))

            # whether to include the entropy bonus at the next state in advantage calc
            if self._include_entropy_in_adv:
                next_log_pis = tf.stop_gradient(self._policy.log_pis([next_obs], next_actions))
                next_value = min_next_Q - self._alpha * next_log_pis
            else:
                next_value = min_next_Q
            
            rewards = tf.stop_gradient(self._reward_scale * compute_reward(
                obs, 
                act,
                done,
                self._rwd_clip_max,
                self._reward,
                self._rwd_done_flag,
                clip=True
            )) # reward edit
            value = tf.stop_gradient(td_target(
                reward=rewards,
                discount=self._discount,
                next_value=next_value,
                terminated=done,
                use_done_flag=self._rwd_done_flag
            )) # reward edit

            pred_Qs_values = tuple(tf.stop_gradient(Q([obs, act])) for Q in self._Qs)
            pred_value = tf.squeeze(tf.reduce_min(pred_Qs_values, axis=0))

            # normalise advantages using batch mean and std
            advantages = value - pred_value
            advantages = tf.stop_gradient((advantages - tf.reduce_mean(advantages)) / tf.math.reduce_std(advantages))
            return advantages
        
        next_obs_real, done_real, log_prob_real = sample_next_obs(self._real_observations_ph, self._real_actions_ph)
        next_obs_fake, done_fake, log_prob_fake = sample_next_obs(self._fake_observations_ph, self._fake_actions_ph)
        self._next_obs_real = next_obs_real
        self._next_obs_fake = next_obs_fake

        advantages_real = compute_advantage(
            self._real_observations_ph, self._real_actions_ph, next_obs_real, done_real
        )
        advantages_fake = compute_advantage(
            self._fake_observations_ph, self._fake_actions_ph, next_obs_fake, done_fake
        )
        
        adv_real = tf.reduce_mean(advantages_real * log_prob_real)
        adv_fake = tf.reduce_mean(advantages_fake * log_prob_fake)

        adv_objective = self._adv_objective = -(adv_real - adv_fake)
        supervised_loss = self._supervised_loss = self._model.train_loss

        adv_total_loss = adv_objective * self._adversary_loss_weighting + supervised_loss
        
        self._adv_optimizer = tf.train.AdamOptimizer(learning_rate=self._adv_lr)
        self._adversarial_train_op = self._adv_optimizer.minimize(
            adv_total_loss,
            var_list=self._model.optvars
        )
        self._session.run(tf.variables_initializer(self._adv_optimizer.variables()))
    
    def _init_training(self):
        self._update_target(tau=1.0)

    def _update_target(self, tau=None):
        tau = tau or self._tau

        for Q, Q_target in zip(self._Qs, self._Q_targets):
            source_params = Q.get_weights()
            target_params = Q_target.get_weights()
            Q_target.set_weights([
                tau * source + (1.0 - tau) * target
                for source, target in zip(source_params, target_params)
            ])

    def _do_agent_training_repeats(self, timestep):
        """Repeat training _n_train_repeat times every _train_every_n_steps"""
        if timestep % self._train_every_n_steps > 0: return
        trained_enough = (
            self._train_steps_this_epoch
            > self._max_train_repeat_per_timestep * self._timestep)
        if trained_enough: return

        for i in range(self._n_train_repeat):
            self._train_agent(
                iteration=timestep,
                batch=self._training_batch())

        self._num_train_steps += self._n_train_repeat
        self._train_steps_this_epoch += self._n_train_repeat

    def _train_agent(self, iteration, batch):
        """Runs the operations for updating training and target ops."""

        feed_dict = self._get_feed_dict(iteration, batch)
        self._session.run(self._training_ops, feed_dict)

        if iteration % self._target_update_interval == 0:
            # Run target ops here.
            self._update_target()

    def _get_feed_dict(self, iteration, batch, adv_update=False):
        """Construct TensorFlow feed_dict from sample batch."""

        feed_dict = {
            self._observations_ph: batch['observations'],
            self._actions_ph: batch['actions'],
            self._next_observations_ph: batch['next_observations'],
            self._rewards_ph: batch['rewards'],
            self._terminals_ph: batch['terminals'],

            # dummies
            self._real_observations_ph: batch['observations'],
            self._real_actions_ph: batch['actions'],
            self._fake_observations_ph: batch['observations'],
            self._fake_actions_ph: batch['actions'],
        }

        if adv_update:
            inputs, targets = self._model.get_labeled_batch()
            feed_dict[self._model.sy_train_in] = inputs
            feed_dict[self._model.sy_train_targ] = targets

        if self._store_extra_policy_info:
            feed_dict[self._log_pis_ph] = batch['log_pis']
            feed_dict[self._raw_actions_ph] = batch['raw_actions']

        if iteration is not None:
            feed_dict[self._iteration_ph] = iteration

        return feed_dict

    def get_diagnostics(self,
                        iteration,
                        batch,
                        training_paths):
        """Return diagnostic information as ordered dictionary.

        Records mean and standard deviation of Q-function and state
        value function, and TD-loss (mean squared Bellman error)
        for the sample batch.

        Also calls the `draw` method of the plotter, if plotter defined.
        """
        feed_dict = self._get_feed_dict(iteration, batch, adv_update=True)

        if self._auto_alpha:
            (Q_values, Q_losses, alpha, global_step, model_stds) = self._session.run(
                (self._Q_values,
                self._Q_losses,
                self._alpha,
                self.global_step,
                self._model_stds),
                feed_dict)
            self._Q_avgs.append(np.mean(Q_values))

            diagnostics = OrderedDict({
                'Q-avg': np.mean(Q_values),
                'Q-std': np.std(Q_values),
                'Q_loss': np.mean(Q_losses),
                'alpha': alpha,
                'model_std_dev': np.mean(model_stds)
            })
        else:
            (Q_values, Q_losses, global_step, model_stds) = self._session.run(
                (self._Q_values,
                self._Q_losses,
                self.global_step,
                self._model_stds),
                feed_dict)
            self._Q_avgs.append(np.mean(Q_values))

            diagnostics = OrderedDict({
                'Q-avg': np.mean(Q_values),
                'Q-std': np.std(Q_values),
                'Q_loss': np.mean(Q_losses),
                'alpha': self._alpha,
                'model_std_dev': np.mean(model_stds)
            })

        # TODO : Remove
        if np.abs(np.mean(Q_values)) > 1e10:
            sys.exit(0)

        policy_diagnostics = self._policy.get_diagnostics(
            batch['observations'])
        diagnostics.update({
            f'policy/{key}': value
            for key, value in policy_diagnostics.items()
        })

        policy_mae = self._compare_policy_to_data()
        diagnostics.update({
            'policy/dataset_mae_action_avg': np.mean(policy_mae)
        })

        if self._plotter:
            self._plotter.draw()

        return diagnostics

    @property
    def tf_saveables(self):
        saveables = {
            '_policy_optimizer': self._policy_optimizer,
            **{
                f'Q_optimizer_{i}': optimizer
                for i, optimizer in enumerate(self._Q_optimizers)
            },
            '_log_alpha': self._log_alpha,
        }

        if hasattr(self, '_alpha_optimizer'):
            saveables['_alpha_optimizer'] = self._alpha_optimizer

        return saveables