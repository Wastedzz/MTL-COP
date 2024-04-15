import os.path
from logging import getLogger
from Env.COPEnv import COPEnv as Env
from Models.models import COPModel as Model
from torch.optim import Adam as Optimizer
from torch.optim.lr_scheduler import MultiStepLR as Scheduler
from torch.nn.parallel import DistributedDataParallel as DDP
from utils import *
from SMPyBandits.SMPyBandits.Policies.Exp3R import Exp3R
from SMPyBandits.SMPyBandits.Policies.Exp3 import Exp3
from SMPyBandits.SMPyBandits.Policies.Thompson import Thompson
from SMPyBandits.SMPyBandits.Policies.DiscountedThompson import DiscountedThompson

import pickle
import torch.distributed as dist
from copy import deepcopy
import time
import itertools


def get_all_permutations(n):
    return list(itertools.permutations(range(n)))


def get_inner_model(model):
    return model.module if isinstance(model, DDP) else model


def get_rew_from_eval_res(evaluated_res,choice,alpha):
    eval_res = np.concatenate(evaluated_res)
    reward = (2*alpha-1)*eval_res[choice] + (1-alpha)*np.sum(eval_res)
    return reward/len(evaluated_res)


class Trainer:
    def __init__(self,
                 env_params,
                 model_params,
                 optimizer_params,
                 trainer_params,
                 rank,
                 opts):

        self.opts = opts

        # save arguments
        self.env_params = env_params['seen']
        self.unseen_params = env_params['unseen']

        self.evaluation_size = opts.evaluation_size

        self.model_params = model_params
        self.optimizer_params = optimizer_params
        self.trainer_params = trainer_params
        self.rank = rank
        self.bandit_alg = opts.bandit_alg

        self.total_count = 0

        # result folder, logger
        self.logger = getLogger(name='trainer')
        if self.rank != 0:
            self.logger.disabled = True
        self.result_folder = get_result_folder()
        self.result_log = LogData()

        if self.rank==0 and not os.path.exists(os.path.join(self.result_folder, "args.json")):
            with open(os.path.join(self.result_folder, "args.json"), 'w') as f:
                json.dump(vars(opts), f, indent=True)

        # cuda
        USE_CUDA = self.trainer_params['use_cuda']
        if USE_CUDA:
            cuda_device_num = rank
            torch.cuda.set_device(cuda_device_num)
            device = torch.device('cuda', cuda_device_num)
            torch.set_default_tensor_type('torch.cuda.FloatTensor')
        else:
            device = torch.device('cpu')
            torch.set_default_tensor_type('torch.FloatTensor')

        # Main Components
        self.problem = list(self.env_params.keys())
        self.unseen_problem = list(self.unseen_params.keys())

        self.model = Model(self.problem,**self.model_params)

        self.env_list = Env(**self.env_params).env_list
        self.unseen_env_list = Env(**self.unseen_params).env_list

        # historical best params
        self.hist_best_model_params_seen = [[[self.total_count,deepcopy(self.model.state_dict())] for env in cop_env] for cop_env in
                                            self.env_list]

        self.hist_best_model_params_unseen = [[[self.total_count,deepcopy(self.model.state_dict())] for env in cop_env] for cop_env in
                                              self.unseen_env_list]

        self.optimizer = Optimizer(self.model.parameters(), **self.optimizer_params['optimizer'])
        self.scheduler = Scheduler(self.optimizer, **self.optimizer_params['scheduler'])

        self.start_epoch = 1

        # bandit setting
        nbArms = sum([len(cop_env) for cop_env in self.env_list])

        self.select_freq = nbArms if opts.select_freq is None else opts.select_freq

        if self.trainer_params['train_episodes'] % self.trainer_params['train_batch_size'] == 0:
            num_batch = self.trainer_params['train_episodes'] // self.trainer_params['train_batch_size']
        else:
            num_batch = self.trainer_params['train_episodes'] // self.trainer_params['train_batch_size'] + 1
        horizon = (num_batch * self.trainer_params['epochs'])//self.select_freq if (num_batch * self.trainer_params['epochs'])\
                                                                                   %self.select_freq ==0 else (num_batch * self.trainer_params['epochs'])//self.select_freq+1
        if self.bandit_alg == 'exp3':
            self.bandit = Exp3(nbArms)
        elif self.bandit_alg == 'exp3r':
            exp3r_param = self.trainer_params['exp3r_param']
            self.bandit = Exp3R(nbArms, horizon=horizon, **exp3r_param)
        elif self.bandit_alg == 'Thompson':
            self.bandit = Thompson(nbArms)
        elif self.bandit_alg == 'DiscountedThompson':
            self.bandit = DiscountedThompson(nbArms)

        self.bandit.startGame()
        self.gradient_info = [[[],[],0] for i in range(nbArms)]
        self.gradient_norm = [[] for i in range(nbArms)]
        self.loss_each_task = [[] for i in range(nbArms)]

        self.choices = []
        self.influ_mats = []
        self.influ_header_mats = []
        self.influ_dec_mats = []
        self.influ_mats_sim = []
        self.influ_mats_sim_share = []
        self.influ_mats_sim_header = []
        self.influ_mats_sim_dec = []

        self.rewards = []
        self.eval_res = []
        self.training_time = []
        self.training_time_light = []

        if self.rank == 0:
            self.overall_seen_data = []
            for i, cop_env in enumerate(self.env_list):
                self.overall_seen_data.append([])
                for j, env in enumerate(cop_env):
                    generate_data = env.generate_data(opts.evaluation_size * dist.get_world_size()).cpu()
                    self.overall_seen_data[-1].append(generate_data)

            self.overall_unseen_data = []
            for i,cop_env in enumerate(self.unseen_env_list):
                self.overall_unseen_data.append([])
                for j,env in enumerate(cop_env):
                    generate_data = env.generate_data(opts.evaluation_size*dist.get_world_size()).cpu()
                    self.overall_unseen_data[-1].append(generate_data)

        # Restore
        model_load = trainer_params['model_load']
        if model_load['enable']:
            self.logger.info('Saved Model Loaded !!')
            try:
                checkpoint_fullname = '{path}/checkpoint-{epoch}.pt'.format(**model_load)
                checkpoint = torch.load(checkpoint_fullname, map_location=device)
                load_epoch = model_load['epoch']
            except:
                epochs = []
                for file in os.listdir(model_load['path']):
                    if file.split('-')[0] == 'checkpoint':
                        try:
                            epochs.append(int(file.split('-')[1].split('.')[0]))
                        except:
                            pass
                load_epoch = max(epochs)
                checkpoint_fullname = '{path}/checkpoint-{epoch}.pt'.format(path=model_load['path'], epoch=load_epoch)
                checkpoint = torch.load(checkpoint_fullname, map_location=device)

            self.result_folder = model_load['path']
            if self.rank == 0:
                self.model.load_state_dict(checkpoint['model_state_dict'])
                self.result_log.set_raw_data(checkpoint['result_log'])

            self.start_epoch = 1 + checkpoint['epoch']
            self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            self.scheduler.last_epoch = checkpoint['epoch'] - 1
            # load resume info for bandit algorithm
            with open('{}/bandit_info-{}.pkl'.format(model_load['path'],load_epoch), 'rb') as file:
                self.bandit = pickle.load(file)

            self.choices = checkpoint['choices']
            self.choice = self.choices[-1]
            self.influ_mats = checkpoint['influ_mats']
            self.influ_header_mats = checkpoint['influ_header_mats']
            self.influ_dec_mats = checkpoint['influ_dec_mats']

            self.influ_mats_sim = checkpoint['influ_mats_sim']
            self.influ_mats_sim_share = checkpoint['influ_mats_sim_share']
            self.influ_mats_sim_header = checkpoint['influ_mats_sim_header']
            self.influ_mats_sim_dec = checkpoint['influ_mats_sim_dec']

            self.rewards = checkpoint['rewards']
            self.eval_res = checkpoint['eval_res']

            self.select_freq = checkpoint['select_freq']
            self.total_count = checkpoint['total_count']
            self.overall_seen_data = checkpoint['overall_seen_data']
            self.overall_unseen_data = checkpoint['overall_unseen_data']
            # historical best params
            self.hist_best_model_params_seen = checkpoint['hist_best_model_params_seen']
            self.hist_best_model_params_unseen = checkpoint['hist_best_model_params_unseen']

            self.gradient_norm = checkpoint['gradient_norm']
            self.loss_each_task = checkpoint['loss_each_task']


            # try:
            #     self.gradient_info_latest_seen = [[[t.cpu() for t in info] for info in infos] for infos in checkpoint['gradient_info_latest_seen']]
            # except:
            #     pass
            try:
                self.gradient_info_latest_seen = checkpoint['gradient_info_latest_seen']
                self.gradient_info = [[self.gradient_info_latest_seen[i][0],[],0] for i in range(nbArms)]
            except:
                self.gradient_info = checkpoint['gradient_info']

            try:
                self.training_time = checkpoint['training_time']
                self.training_time_light = checkpoint['training_time_light']
            except:
                self.training_time = []
                self.training_time_light = []

        try:
            self.num_restart = self.bandit.number_of_restart
        except:
            self.num_restart = 0

        # fix the validation data and send to different gpus
        if rank == 0:
            self.fix_seen_validation_data = []
            for i, cop_env in enumerate(self.env_list):
                self.fix_seen_validation_data.append([])
                for j, env in enumerate(cop_env):
                    generate_data = self.overall_seen_data[i][j].cuda()
                    generate_data_list = torch.chunk(generate_data, dist.get_world_size())
                    for _ in range(1, dist.get_world_size()):
                        dist.send(generate_data_list[_], dst=_, tag=i * 100 + j * 10 + _)

                    self.fix_seen_validation_data[-1].append(generate_data_list[0])

            self.fix_unseen_validation_data = []
            for i, cop_env in enumerate(self.unseen_env_list):
                self.fix_unseen_validation_data.append([])
                for j, env in enumerate(cop_env):
                    generate_data = self.overall_unseen_data[i][j].cuda()
                    generate_data_list = torch.chunk(generate_data, dist.get_world_size())
                    for _ in range(1, dist.get_world_size()):
                        dist.send(generate_data_list[_], dst=_, tag=1000 + i * 100 + j * 10 + _)
                    self.fix_unseen_validation_data[-1].append(generate_data_list[0])

        else:
            self.fix_seen_validation_data = []
            for i,cop_env in enumerate(self.env_list):
                self.fix_seen_validation_data.append([])
                for j,env in enumerate(cop_env):
                    generate_data = env.generate_data(opts.evaluation_size)
                    dist.recv(generate_data, src=0, tag=i*100+j*10+self.rank)
                    self.fix_seen_validation_data[-1].append(generate_data)

            self.fix_unseen_validation_data = []
            for i,cop_env in enumerate(self.unseen_env_list):
                self.fix_unseen_validation_data.append([])
                for j,env in enumerate(cop_env):
                    generate_data = env.generate_data(opts.evaluation_size)
                    dist.recv(generate_data, src=0, tag=1000+i*100+j*10+self.rank)
                    self.fix_unseen_validation_data[-1].append(generate_data)


        if len(self.env_list)==1:
            self.model = DDP(self.model, device_ids=[rank])
        else:
            self.model = DDP(self.model, device_ids=[rank], find_unused_parameters=True)

        # utility
        self.time_estimator = TimeEstimator()

    def run(self):
        self.time_estimator.reset(self.start_epoch)
        for epoch in range(self.start_epoch, self.trainer_params['epochs']+1):
            self.logger.info('=================================================================')

            # LR Decay
            self.scheduler.step()

            # Train
            total_score, train_loss = self._train_one_epoch(epoch)
            self.result_log.append('train_score', epoch, total_score)
            self.result_log.append('train_loss', epoch, train_loss)


            ############################
            # Logs & Checkpoint
            ############################
            elapsed_time_str, remain_time_str = self.time_estimator.get_est_string(epoch, self.trainer_params['epochs'])
            self.logger.info("Epoch {:3d}/{:3d}: Time Est.: Elapsed[{}], Remain[{}]".format(
                epoch, self.trainer_params['epochs'], elapsed_time_str, remain_time_str))

            all_done = (epoch == self.trainer_params['epochs'])
            model_save_interval = self.trainer_params['logging']['model_save_interval']
            img_save_interval = self.trainer_params['logging']['img_save_interval']

            if self.rank == 0 and (all_done or (epoch % model_save_interval) == 0):
                self.logger.info("Saving trained_model")
                checkpoint_dict = {
                    'epoch': epoch,
                    'model_state_dict': get_inner_model(self.model).state_dict(),
                    'optimizer_state_dict': self.optimizer.state_dict(),
                    'scheduler_state_dict': self.scheduler.state_dict(),
                    'result_log': self.result_log.get_raw_data(),
                    'choices': self.choices,
                    'influ_mats': self.influ_mats,
                    'influ_header_mats': self.influ_header_mats,
                    'influ_dec_mats': self.influ_dec_mats,
                    'influ_mats_sim': self.influ_mats_sim,
                    'influ_mats_sim_share': self.influ_mats_sim_share,
                    'influ_mats_sim_header': self.influ_mats_sim_header,
                    'influ_mats_sim_dec':self.influ_mats_sim_dec,
                    'rewards': self.rewards,
                    'select_freq': self.select_freq,
                    'total_count': self.total_count,
                    'eval_res': self.eval_res,
                    'overall_seen_data': self.overall_seen_data,
                    'overall_unseen_data': self.overall_unseen_data,
                    'hist_best_model_params_seen': self.hist_best_model_params_seen,
                    'hist_best_model_params_unseen': self.hist_best_model_params_unseen,
                    'gradient_info':self.gradient_info,
                    'gradient_norm': self.gradient_norm,
                    'loss_each_task': self.loss_each_task,
                    'training_time': self.training_time,
                    'training_time_light': self.training_time_light,

                }
                torch.save(checkpoint_dict, '{}/checkpoint-{}.pt'.format(self.result_folder, epoch))
                with open('{}/bandit_info-{}.pkl'.format(self.result_folder,epoch), 'wb') as file:
                    pickle.dump(self.bandit, file)

            # All-done announcement
            if all_done:
                self.logger.info(" *** Training Done *** ")
                self.logger.info("Now, printing log array...")
                util_print_log_array(self.logger, self.result_log)

    def _train_one_epoch(self, epoch):

        score_AM = AverageMeter()
        loss_AM = AverageMeter()

        train_num_episode = self.trainer_params['train_episodes']
        episode = 0
        s = time.time()
        while episode < train_num_episode:

            remaining = train_num_episode - episode
            batch_size = min(self.trainer_params['train_batch_size'], remaining)

            avg_loss, avg_score = self._train_one_batch(batch_size)
            score_AM.update(avg_score, batch_size)
            loss_AM.update(avg_loss, batch_size)
            episode += batch_size

        self.training_time.append(time.time()-s)
        self.valiad_and_save_model(self.evaluation_size)
        # Log Once, for each epoch
        self.logger.info('Epoch {:3d}: Train ({:3.0f}%)  Score: {}  Loss: {}'
                         .format(epoch, 100. * episode / train_num_episode,
                                 self.eval_res[-1].reshape(-1), loss_AM.avg))

        return score_AM.avg, loss_AM.avg

    def _train_one_batch(self, batch_size):
        s = time.time()
        # Prep
        ###############################################
        self.model.train()

        # POMO Rollout
        ###############################################
        world_size = dist.get_world_size()
        num_tasks = (sum([len(cop_env) for cop_env in self.env_list]))

        # bandit alg for choice
        # need to sync the choice for different ranks
        if self.rank == 0:
            if  self.total_count < self.opts.warm_start *\
                    (self.trainer_params['train_episodes']//self.trainer_params['train_batch_size']):  # we select each task once at the beginning of training
                choice = self.total_count % num_tasks
                self.bandit.pulls[choice] += 1

            elif self.bandit_alg == 'Thompson' or self.bandit_alg == 'DiscountedThompson':
                posterior_list = []
                for arm in range(num_tasks):
                    posterior_list.append(self.bandit.computeIndex(arm))
                choice = np.argmax(posterior_list)
                self.bandit.pulls[choice] += 1
            else:
                choice = self.bandit.choice()

            choice = torch.tensor(choice).to(torch.device('cuda', 0))
            for i in range(1, world_size):
                dist.send(choice, dst=i, tag=i)

        else:
            choice = torch.tensor(1).to(torch.device('cuda', self.rank))
            dist.recv(choice, src=0, tag=self.rank)

        choice = choice.data.cpu().numpy()
        self.choice = choice
        self.choices.append(self.choice)

        problem_idx, scale_id = self.select_env_cop(self.choice)
        env = self.env_list[problem_idx][scale_id]
        problem = self.problem[problem_idx]
        env.load_problems(batch_size)
        reset_s, _, _ = env.reset()
        state, reward, done = env.pre_step()
        self.model.module.pre_forward_oneCOP(reset_s, problem)
        loss_mean, score_mean = self.train_one_COP(env, problem, state, reward, done)
        self.optimizer.zero_grad()
        loss_mean.backward()
        self.optimizer.step()
        self.loss_each_task[choice].append(loss_mean.data.item())
        self.training_time_light.append(time.time()-s)

        if self.rank == 0:
            # recored the gradient information
            grad_share = []
            for name, params in self.model.module.encoder.named_parameters():
                grad_share.append(params.grad.data.view(-1))
            grad_share = torch.cat(grad_share)

            grad_ts_h = []
            for name, params in self.model.module.headers[problem_idx].named_parameters():
                grad_ts_h.append(params.grad.data.view(-1))
            grad_ts_h = torch.cat(grad_ts_h)
            grad_ts_d = []
            for name, params in self.model.module.decoders[problem_idx].named_parameters():
                grad_ts_d.append(params.grad.data.view(-1))
            grad_ts_d = torch.cat(grad_ts_d)

            self.gradient_info[choice][0] = [grad_share, grad_ts_h, grad_ts_d]
            if self.gradient_info[choice][2] == 0:
                self.gradient_info[choice][1] = [grad_share, grad_ts_h, grad_ts_d]
            else:
                temp_count = self.gradient_info[choice][2]
                self.gradient_info[choice][1] = [
                    1 / (temp_count + 1) * self.gradient_info[choice][0][i] + temp_count / (temp_count + 1) *
                    self.gradient_info[choice][1][i] for i in range(3)]
            self.gradient_info[choice][2] += 1

            self.gradient_norm[choice].append(
                [torch.norm(torch.cat([grad_share, grad_ts_h, grad_ts_d])).cpu().data.item()])

        if self.total_count >= self.opts.warm_start * \
                (self.trainer_params['train_episodes'] // self.trainer_params['train_batch_size']) - 1 \
                and self.total_count % self.select_freq == 0 \
                and self.total_count != 0:
            # update ts using gradient information
            if self.rank == 0:
                M, M_header, M_dec, M_similarity, M_similarity_share, M_similarity_head, M_similarity_dec = self.get_influ_mat()
                self.influ_mats.append(M)
                self.influ_header_mats.append(M_header)
                self.influ_dec_mats.append(M_dec)

                self.influ_mats_sim.append(M_similarity)
                self.influ_mats_sim_share.append(M_similarity)
                self.influ_mats_sim_header.append(M_similarity_head)
                self.influ_mats_sim_dec.append(M_similarity_dec)
                reward_for_each_task = 1 / (1 + np.exp(-M_similarity.sum(axis=0)))
                grad_info_num = np.array([_[2] for _ in self.gradient_info])
                reward_for_each_task[grad_info_num == 0] = 0

                for task_idx in range(num_tasks):
                    select_counts = self.gradient_info[task_idx][2]
                    if select_counts != 0:
                        self.bandit.getReward(task_idx, reward_for_each_task[task_idx])

                    if self.bandit_alg == 'Thompson' or self.bandit_alg == 'DiscountedThompson':
                        self.bandit.rewards[task_idx] += reward_for_each_task[task_idx]

                self.rewards.append(reward_for_each_task)

            self.gradient_info = [[self.gradient_info[i][0], [], 0] for i in range(num_tasks)]

        self.total_count += 1

        return loss_mean.data.item(), score_mean

    def train_one_COP(self, env, problem, state, reward, done):
        prob_list = torch.zeros(size=(env.batch_size, env.pomo_size, 0))

        # shape: (batch, pomo, 0~problem)
        while not done:
            selected, prob = self.model(state, problem)
            # shape: (batch, pomo)
            state, reward, done = env.step(selected)
            try:
                # selected = state.true_selected  # the selected nodes may change due to some hard constrain, e.g. in OP
                prob = prob[state.BATCH_IDX, state.POMO_IDX, selected].reshape(state.BATCH_IDX.size(0),
                                                             state.BATCH_IDX.size(1))
            except:
                pass
            prob_list = torch.cat((prob_list, prob[:, :, None]), dim=2)
        # Loss
        ###############################################
        advantage = reward - reward.float().mean(dim=1, keepdims=True)
        # shape: (batch, pomo)
        log_prob = prob_list.log().sum(dim=2)
        # size = (batch, pomo)
        loss = -advantage * log_prob  # Minus Sign: To Increase REWARD
        # shape: (batch, pomo)
        loss_mean = loss.mean()

        # Score
        ###############################################
        max_pomo_reward, _ = reward.max(dim=1)  # get best results from pomo
        score_mean = torch.abs(max_pomo_reward.float().mean())  # negative sign to make positive value
        return loss_mean, score_mean.item()

    def get_influ_mat(self):
        M = torch.zeros((len(self.gradient_info),len(self.gradient_info))).cuda()
        M_header = torch.zeros((len(self.gradient_info),len(self.gradient_info))).cuda()
        M_dec = torch.zeros((len(self.gradient_info),len(self.gradient_info))).cuda()

        M_similarity = torch.zeros((len(self.gradient_info),len(self.gradient_info))).cuda()
        M_similarity_share = torch.zeros((len(self.gradient_info),len(self.gradient_info))).cuda()
        M_similarity_head = torch.zeros((len(self.gradient_info),len(self.gradient_info))).cuda()
        M_similarity_dec = torch.zeros((len(self.gradient_info),len(self.gradient_info))).cuda()

        cum_scales_per_cop = np.cumsum([len(cop_env) for cop_env in self.env_list])
        for i in range(len(self.gradient_info)):
            cop_i = np.where((i < cum_scales_per_cop) == True)[0][0]
            grad_i = self.gradient_info[i][1] if len(self.gradient_info[i][1])!=0 else self.gradient_info[i][0]
            for j in range(len(self.gradient_info)):
                grad_j = self.gradient_info[j][1]
                count_j = self.gradient_info[j][2]
                if len(grad_j)!=0 and count_j!=0:
                    cop_j = np.where((j < cum_scales_per_cop) == True)[0][0]
                    if cop_i == cop_j: # i,j are same kind of COP
                        grad_i_cat = torch.cat(grad_i)
                        grad_j_cat = torch.cat(grad_j)*count_j

                        inner_prod = grad_i_cat*grad_j_cat

                        M[i,j] = inner_prod.sum()
                        M_header[i,j] = inner_prod[len(grad_i[0]):len(grad_i[0])+len(grad_i[1])].sum()
                        M_dec[i,j] = inner_prod[len(grad_i[0])+len(grad_i[1]):].sum()

                        M_similarity[i, j] = M[i, j] / (
                                    torch.linalg.vector_norm(grad_i_cat) * torch.linalg.vector_norm(grad_j_cat))
                        M_similarity_share[i, j] = inner_prod[:len(grad_i[0])].sum() / (
                                    torch.linalg.vector_norm(grad_i_cat[:len(grad_i[0])])
                                    * torch.linalg.vector_norm(grad_j_cat[:len(grad_j[0])]))
                        M_similarity_head[i, j] = M_header[i, j] / (torch.linalg.vector_norm(grad_i_cat[len(grad_i[0]):len(grad_i[0]) + len(
                                                                         grad_i[1])])
                                                                    * torch.linalg.vector_norm(grad_j_cat[len(grad_j[0]):len(grad_j[0]) + len(
                                                                         grad_j[1])]))
                        M_similarity_dec[i,j] = M_dec[i,j]/((torch.linalg.vector_norm(grad_i_cat[len(grad_i[0]) + len(
                                                                         grad_i[1]):])
                                                                    * torch.linalg.vector_norm(grad_j_cat[len(grad_j[0]) + len(
                                                                         grad_j[1]):])))
                    else: #i,j are different kinds of COP
                        grad_i_share = grad_i[0]
                        grad_j_share_cat = grad_j[0]*count_j
                        M[i,j] = (grad_i_share* grad_j_share_cat).sum(-1)
                        M_similarity_share[i,j] = M[i,j]/(torch.linalg.vector_norm(grad_i_share) * torch.linalg.vector_norm(grad_j_share_cat))
                        M_similarity[i,j] = M_similarity_share[i,j]
        return M.cpu().numpy(), M_header.cpu().numpy(), M_dec.cpu().numpy(), M_similarity.cpu().numpy(), M_similarity_share.cpu().numpy(), M_similarity_head.cpu().numpy(), M_similarity_dec.cpu().numpy()

    def valiadate(self,batch_size):
        self.model.eval()

        def val_env_list(env_list, problems, batch_size, seen=False, fix_data=False):
            for i,cop_env in enumerate(env_list):
                for j,env in enumerate(cop_env):
                    if fix_data:
                        if seen:
                            validation_data = self.fix_seen_validation_data[i][j]
                        else:
                            validation_data = self.fix_unseen_validation_data[i][j]
                        env.load_problems(batch_size,prepare_dataset=validation_data)
                    else:
                        env.load_problems(batch_size)


            def val_one_model(model, params=None):
                if params is not None:
                    model.module.load_state_dict(params)
                reset_state = []
                states, rewards, dones = [], [], []
                for cop_env in env_list:
                    temp_reset_state = []
                    temp_state = []
                    temp_reward = []
                    temp_dones = []
                    for env in cop_env:
                        reset_s, _, _ = env.reset()
                        state, reward, done = env.pre_step()
                        temp_reset_state.append(reset_s)
                        temp_state.append(state)
                        temp_reward.append(reward)
                        temp_dones.append(done)

                    reset_state.append(temp_reset_state)
                    states.append(temp_state)
                    rewards.append(temp_reward)
                    dones.append(temp_dones)

                score_list = []
                # states, rewards, dones = zip(*[env.pre_step() for env in self.env_list])
                for j in range(len(env_list)):
                    cop_env = env_list[j]
                    problem = problems[j]
                    # temp_score = []
                    for i in range(len(cop_env)):
                        env = cop_env[i]

                        with torch.no_grad():
                            model.module.pre_forward_oneCOP(reset_state[j][i], problem)
                            state, reward, done = states[j][i], rewards[j][i], dones[j][i]
                            # shape: (batch, pomo, 0~problem)
                            while not done:
                                selected, _ = model(state, problem)
                                # shape: (batch, pomo)
                                state, reward, done = env.step(selected)

                            # Score
                            ###############################################
                            max_pomo_reward, _ = reward.max(dim=1)  # get best results from pomo
                            score = torch.abs(max_pomo_reward)  # negative sign to make positive value
                            score_list.append(score.mean().view(-1))
                    # score_list.append(temp_score)
                try:
                    return torch.cat(score_list)
                except:
                    return torch.tensor([])

            res_current_model = val_one_model(self.model)
            return res_current_model

        res_on_seen = val_env_list(self.env_list, self.problem, batch_size, seen=True, fix_data=True,)
        res_on_unseen = val_env_list(self.unseen_env_list, self.unseen_problem, batch_size, seen=False, fix_data=True)
        return res_on_seen, res_on_unseen

    def valiad_and_save_model(self,batch_size):
        if len(self.eval_res) != 0:
            eval_res_hist = np.concatenate(self.eval_res, axis=0)
        else:
            eval_res_hist = None
        cur_eval_res, unseen_eval_res = self.valiadate(batch_size)
        total_res_mean = torch.cat([cur_eval_res, unseen_eval_res], dim=0)
        dist.all_reduce(total_res_mean, op=dist.ReduceOp.SUM)
        total_res_mean /= dist.get_world_size()
        total_res_mean = total_res_mean.cpu().numpy()

        # update the historical best param on seen tasks
        if eval_res_hist is not None:
            temp_count = 0
            for i, cop_env in enumerate(self.env_list):
                problem = self.problem[i]
                for j, env in enumerate(cop_env):
                    if problem == 'KP' or problem == 'OP':
                        if total_res_mean[temp_count] > np.max(eval_res_hist[:, temp_count]):
                            self.hist_best_model_params_seen[i][j][0] = self.total_count
                            self.hist_best_model_params_seen[i][j][1] = deepcopy(self.model.module.state_dict())
                    else:
                        if total_res_mean[temp_count] < np.min(eval_res_hist[:, temp_count]):
                            self.hist_best_model_params_seen[i][j][0] = self.total_count
                            self.hist_best_model_params_seen[i][j][1] = deepcopy(self.model.module.state_dict())

                    temp_count += 1

            # update the historical best param on unseen tasks
            for i, cop_env in enumerate(self.unseen_env_list):
                problem = self.unseen_problem[i]
                for j, env in enumerate(cop_env):
                    if problem == 'KP' or problem == 'OP':
                        if total_res_mean[temp_count] > np.max(eval_res_hist[:, temp_count]):
                            self.hist_best_model_params_unseen[i][j][0] = self.total_count
                            self.hist_best_model_params_unseen[i][j][1] = deepcopy(self.model.module.state_dict())
                    else:
                        if total_res_mean[temp_count] < np.min(eval_res_hist[:, temp_count]):
                            self.hist_best_model_params_unseen[i][j][0] = self.total_count
                            self.hist_best_model_params_unseen[i][j][1] = deepcopy(self.model.module.state_dict())

                    temp_count += 1

        self.eval_res.append(total_res_mean.reshape(1, -1))


    def select_env_cop(self, choice):
        choice_id = choice + 1
        num_scales = np.array([len(cop_env) for cop_env in self.env_list])
        cum_sum = np.cumsum(num_scales)
        cop_id = np.where((cum_sum < choice_id) == False)[0][0]
        if cop_id == 0:
            scale_id = choice
        else:
            scale_id = choice - cum_sum[cop_id - 1]
        return cop_id, scale_id


