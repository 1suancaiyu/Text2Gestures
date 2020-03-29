import math
import matplotlib.pyplot as plt
import os
import time
import torch.optim as optim
import torch.nn as nn
import torchtext as tt
from net.T2GNet import T2GNet as T2GNet

from torchlight.torchlight.io import IO
from torchtext.data.utils import get_tokenizer
from utils.common import generate_rvo_trajectories
# from utils.mocap_dataset import MocapDataset
from utils.mocap_dataset import MocapDataset
from utils.Quaternions import Quaternions
from utils.visualizations import display_animations
from utils import losses
from utils.Quaternions_torch import *
from utils.spline import Spline_AS, Spline

torch.manual_seed(1234)

rec_loss = losses.quat_angle_loss


def find_all_substr(a_str, sub):
    start = 0
    while True:
        start = a_str.find(sub, start)
        if start == -1:
            return
        yield start
        start += len(sub)  # use start += 1 to find overlapping matches


def get_best_epoch_and_loss(path_to_model_files):
    all_models = os.listdir(path_to_model_files)
    if len(all_models) < 2:
        return '', None, np.inf
    loss_list = -1. * np.ones(len(all_models))
    acc_list = -1. * np.ones(len(all_models))
    for i, model in enumerate(all_models):
        loss_acc_val = str.split(model, '_')
        if len(loss_acc_val) > 1:
            loss_list[i] = float(loss_acc_val[3])
            acc_list[i] = float(loss_acc_val[5])
    if len(loss_list) < 3:
        best_model = all_models[np.argwhere(loss_list == min([n for n in loss_list if n > 0]))[0, 0]]
    else:
        loss_idx = np.argpartition(loss_list, 2)
        best_model = all_models[loss_idx[1]]
    all_underscores = list(find_all_substr(best_model, '_'))
    # return model name, best loss
    return best_model, int(best_model[all_underscores[0] + 1:all_underscores[1]]),\
           float(best_model[all_underscores[2] + 1:all_underscores[3]])


class Processor(object):
    """
        Processor for gait generation
    """

    def __init__(self, args, data_path, data_loader, Z, T, A, V, C, D, joint_names,
                 joint_parents, lower_body_start=15, fill=6, min_train_epochs=20,
                 generate_while_train=False, save_path=None, device='cuda:0'):

        def get_quats_sos_and_eos():
            quats_sos_and_eos_file = os.path.join(data_path, 'quats_sos_and_eos.npz')
            keys = list(self.data_loader['train'].keys())
            num_samples = len(self.data_loader['train'])
            try:
                mean_quats_sos = np.load(quats_sos_and_eos_file, allow_pickle=True)['quats_sos']
                mean_quats_eos = np.load(quats_sos_and_eos_file, allow_pickle=True)['quats_eos']
            except FileNotFoundError:
                mean_quats_sos = np.zeros((self.V, self.D))
                mean_quats_eos = np.zeros((self.V, self.D))
                for j in range(self.V):
                    quats_sos = np.zeros((self.D, num_samples))
                    quats_eos = np.zeros((self.D, num_samples))
                    for s in range(num_samples):
                        quats_sos[:, s] = self.data_loader['train'][keys[s]]['rotations'][0, j]
                        quats_eos[:, s] = self.data_loader['train'][keys[s]]['rotations'][-1, j]
                    _, sos_eig_vectors = np.linalg.eig(np.dot(quats_sos, quats_sos.T))
                    mean_quats_sos[j] = sos_eig_vectors[:, 0]
                    _, eos_eig_vectors = np.linalg.eig(np.dot(quats_eos, quats_eos.T))
                    mean_quats_eos[j] = eos_eig_vectors[:, 0]
                np.savez_compressed(quats_sos_and_eos_file, quats_sos=mean_quats_sos, quats_eos=mean_quats_eos)
            mean_quats_sos = torch.from_numpy(mean_quats_sos).unsqueeze(0)
            mean_quats_eos = torch.from_numpy(mean_quats_eos).unsqueeze(0)
            for s in range(num_samples):
                pos_sos = \
                    MocapDataset.forward_kinematics(mean_quats_sos.unsqueeze(0),
                                                    torch.from_numpy(self.data_loader['train'][keys[s]]
                                                    ['positions'][0:1, 0]).double().unsqueeze(0),
                                                    self.joint_parents,
                                                    torch.from_numpy(self.data_loader['train'][keys[s]]['joints_dict']
                                                    ['joints_offsets_all']).unsqueeze(0)).squeeze(0).numpy()
                affs_sos = MocapDataset.get_mpi_affective_features(pos_sos)
                pos_eos = \
                    MocapDataset.forward_kinematics(mean_quats_eos.unsqueeze(0),
                                                    torch.from_numpy(self.data_loader['train'][keys[s]]
                                                    ['positions'][-1:, 0]).double().unsqueeze(0),
                                                    self.joint_parents,
                                                    torch.from_numpy(self.data_loader['train'][keys[s]]['joints_dict']
                                                    ['joints_offsets_all']).unsqueeze(0)).squeeze(0).numpy()
                affs_eos = MocapDataset.get_mpi_affective_features(pos_eos)
                self.data_loader['train'][keys[s]]['positions'] = \
                    np.concatenate((pos_sos, self.data_loader['train'][keys[s]]['positions'], pos_eos), axis=0)
                self.data_loader['train'][keys[s]]['affective_features'] = \
                    np.concatenate((affs_sos, self.data_loader['train'][keys[s]]['affective_features'], affs_eos),
                                   axis=0)
            return mean_quats_sos, mean_quats_eos

        self.args = args
        self.dataset = args.dataset
        self.channel_map = {
            'Xrotation': 'x',
            'Yrotation': 'y',
            'Zrotation': 'z'
        }
        self.device = device
        self.data_loader = data_loader
        self.result = dict()
        self.iter_info = dict()
        self.epoch_info = dict()
        self.meta_info = dict(epoch=0, iter=0)
        self.io = IO(
            self.args.work_dir,
            save_log=self.args.save_log,
            print_log=self.args.print_log)

        # model
        self.T = T + 2
        self.A = A
        self.V = V
        self.C = C
        self.D = D
        self.O = 1
        self.joint_names = joint_names
        self.joint_parents = joint_parents
        self.lower_body_start = lower_body_start
        self.quats_sos, self.quats_eos = get_quats_sos_and_eos()
        # self.quats_sos = torch.from_numpy(Quaternions.id(self.V).qs).unsqueeze(0)
        # self.quats_eos = torch.from_numpy(Quaternions.from_euler(
        #     np.tile([np.pi / 2., 0, 0], (self.V, 1))).qs).unsqueeze(0)
        self.o_rs_loss_func = nn.L1Loss()
        self.affs_loss_func = nn.L1Loss()
        self.spline_loss_func = nn.L1Loss()
        self.best_loss = np.inf
        self.loss_updated = False
        self.mean_ap_updated = False
        self.step_epochs = [math.ceil(float(self.args.num_epoch * x)) for x in self.args.step]
        self.best_loss_epoch = None
        self.best_acc_epoch = None
        self.min_train_epochs = min_train_epochs
        self.zfill = fill
        try:
            self.text_processor = torch.load('text_processor.pt')
        except FileNotFoundError:
            self.text_processor = tt.data.Field(tokenize=get_tokenizer("basic_english"),
                                                init_token='<sos>',
                                                eos_token='<eos>',
                                                lower=True)
            train_text, eval_text, test_text = tt.datasets.WikiText2.splits(self.text_processor)
            self.text_processor.build_vocab(train_text, eval_text, test_text)
        self.text_sos = self.text_processor.vocab.stoi['<sos>']
        self.text_eos = self.text_processor.vocab.stoi['<eos>']
        num_tokens = len(self.text_processor.vocab.stoi)  # the size of vocabulary
        self.Z = Z + 2  # embedding dimension
        num_hidden_units = 200  # the dimension of the feedforward network model in nn.TransformerEncoder
        num_layers = 2  # the number of nn.TransformerEncoderLayer in nn.TransformerEncoder
        num_heads = 2  # the number of heads in the multiheadattention models
        dropout = 0.2  # the dropout value
        self.model = T2GNet(num_tokens, self.Z, self.V * self.D, self.D, self.V - 1,
                            num_heads, num_hidden_units, num_layers, dropout).to(device)

        # generate
        self.generate_while_train = generate_while_train
        self.save_path = save_path

        # optimizer
        if self.args.optimizer == 'SGD':
            self.optimizer = optim.SGD(
                self.model.parameters(),
                lr=self.args.base_lr,
                momentum=0.9,
                nesterov=self.args.nesterov,
                weight_decay=self.args.weight_decay)
        elif self.args.optimizer == 'Adam':
            self.optimizer = optim.Adam(
                self.model.parameters(),
                lr=self.args.base_lr)
                # weight_decay=self.args.weight_decay)
        else:
            raise ValueError()
        self.lr = self.args.base_lr
        self.tf = self.args.base_tr

    def process_data(self, data, poses, quat, trans, affs):
        data = data.float().to(self.device)
        poses = poses.float().to(self.device)
        quat = quat.float().to(self.device)
        trans = trans.float().to(self.device)
        affs = affs.float().to(self.device)
        return data, poses, quat, trans, affs

    def load_best_model(self, ):
        model_name, self.best_loss_epoch, self.best_loss =\
            get_best_epoch_and_loss(self.args.work_dir)
        best_model_found = False
        try:
            loaded_vars = torch.load(os.path.join(self.args.work_dir, model_name))
            self.model.load_state_dict(loaded_vars['model_dict'])
            best_model_found = True
        except (FileNotFoundError, IsADirectoryError):
            print('No saved model found.')
        return best_model_found

    def adjust_lr(self):
        self.lr = self.lr * self.args.lr_decay
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = self.lr

    def adjust_tf(self):
        if self.meta_info['epoch'] > 20:
            self.tf = self.tf * self.args.tf_decay

    def show_epoch_info(self):

        print_epochs = [self.best_loss_epoch if self.best_loss_epoch is not None else 0]
        best_metrics = [self.best_loss]
        i = 0
        for k, v in self.epoch_info.items():
            self.io.print_log('\t{}: {}. Best so far: {} (epoch: {:d}).'.
                              format(k, v, best_metrics[i], print_epochs[i]))
            i += 1
        if self.args.pavi_log:
            self.io.log('train', self.meta_info['iter'], self.epoch_info)

    def show_iter_info(self):

        if self.meta_info['iter'] % self.args.log_interval == 0:
            info = '\tIter {} Done.'.format(self.meta_info['iter'])
            for k, v in self.iter_info.items():
                if isinstance(v, float):
                    info = info + ' | {}: {:.4f}'.format(k, v)
                else:
                    info = info + ' | {}: {}'.format(k, v)

            self.io.print_log(info)

            if self.args.pavi_log:
                self.io.log('train', self.meta_info['iter'], self.iter_info)

    def yield_batch(self, batch_size, dataset):
        batch_joint_offsets = torch.zeros((batch_size, self.V - 1, self.C)).cuda()
        batch_pos = torch.zeros((batch_size, self.T, self.V, self.C)).cuda()
        batch_affs = torch.zeros((batch_size, self.T, self.A)).cuda()
        batch_quat = torch.zeros((batch_size, self.T, self.V * self.D)).cuda()
        batch_quat_valid_idx = torch.zeros((batch_size, self.T)).cuda()
        batch_text = torch.zeros((batch_size, self.Z)).cuda().long()
        batch_text_valid_idx = torch.zeros((batch_size, self.Z)).cuda()

        pseudo_passes = (len(dataset) + batch_size - 1) // batch_size

        probs = []
        for k in dataset.keys():
            probs.append(dataset[k]['positions'].shape[0])
        probs = np.array(probs) / np.sum(probs)

        for p in range(pseudo_passes):
            rand_keys = np.random.choice(len(dataset), size=batch_size, replace=True, p=probs)
            for i, k in enumerate(rand_keys):
                joint_offsets = torch.from_numpy(dataset[str(k).zfill(self.zfill)]
                                                 ['joints_dict']['joints_offsets_all'][1:])
                pos = torch.from_numpy(dataset[str(k).zfill(self.zfill)]['positions'])
                affs = torch.from_numpy(dataset[str(k).zfill(self.zfill)]['affective_features'])
                quat = torch.cat((self.quats_sos,
                                  torch.from_numpy(dataset[str(k).zfill(self.zfill)]['rotations']),
                                  self.quats_eos), dim=0)
                quat_length = quat.shape[0]
                quat_valid_idx = torch.zeros(self.T)
                quat_valid_idx[:quat_length] = 1
                text = torch.cat((self.text_processor.numericalize(dataset[str(k).zfill(self.zfill)]['Text'])[0],
                                  torch.from_numpy(np.array([self.text_eos]))))
                if text[0] != self.text_sos:
                    text = torch.cat((torch.from_numpy(np.array([self.text_sos])), text))
                text_length = text.shape[0]
                text_valid_idx = torch.zeros(self.Z)
                text_valid_idx[:text_length] = 1

                batch_joint_offsets[i] = joint_offsets
                batch_pos[i, :pos.shape[0]] = pos
                batch_affs[i, :affs.shape[0]] = affs
                batch_quat[i, :quat_length] = quat.view(quat_length, -1)
                batch_quat_valid_idx[i] = quat_valid_idx
                batch_text[i, :text_length] = text
                batch_text_valid_idx[i] = text_valid_idx
            yield batch_joint_offsets, batch_pos, batch_affs, batch_quat, \
                batch_quat_valid_idx, batch_text, batch_text_valid_idx

    def return_batch(self, batch_size, dataset, randomized=True):
        if len(batch_size) > 1:
            rand_keys = np.copy(batch_size)
            batch_size = len(batch_size)
        else:
            batch_size = batch_size[0]
            probs = []
            for k in dataset.keys():
                probs.append(dataset[k]['positions'].shape[0])
            probs = np.array(probs) / np.sum(probs)
            if randomized:
                rand_keys = np.random.choice(len(dataset), size=batch_size, replace=False, p=probs)
            else:
                rand_keys = np.arange(batch_size)

        batch_joint_offsets = torch.zeros((batch_size, self.V - 1, self.C)).cuda()
        batch_pos = torch.zeros((batch_size, self.T, self.V, self.C)).cuda()
        batch_affs = torch.zeros((batch_size, self.T, self.A)).cuda()
        batch_quat = torch.zeros((batch_size, self.T, self.V * self.D)).cuda()
        batch_quat_valid_idx = torch.zeros((batch_size, self.T)).cuda()
        batch_text = torch.zeros((batch_size, self.Z)).cuda().long()
        batch_text_valid_idx = torch.zeros((batch_size, self.Z)).cuda()

        for i, k in enumerate(rand_keys):
            joint_offsets = torch.from_numpy(dataset[str(k).zfill(self.zfill)]
                                             ['joints_dict']['joints_offsets_all'][1:])
            pos = torch.from_numpy(dataset[str(k).zfill(self.zfill)]['positions'])
            affs = torch.from_numpy(dataset[str(k).zfill(self.zfill)]['affective_features'])
            quat = torch.cat((self.quats_sos,
                              torch.from_numpy(dataset[str(k).zfill(self.zfill)]['rotations']),
                              self.quats_eos), dim=0)
            quat_length = quat.shape[0]
            quat_valid_idx = torch.zeros(self.T)
            quat_valid_idx[:quat_length] = 1
            text = torch.cat((self.text_processor.numericalize(dataset[str(k).zfill(self.zfill)]['Text'])[0],
                              torch.from_numpy(np.array([self.text_eos]))))
            if text[0] != self.text_sos:
                text = torch.cat((torch.from_numpy(np.array([self.text_sos])), text))
            text_length = text.shape[0]
            text_valid_idx = torch.zeros(self.Z)
            text_valid_idx[:text_length] = 1

            batch_joint_offsets[i] = joint_offsets
            batch_pos[i, :pos.shape[0]] = pos
            batch_pos[i, :affs.shape[0]] = affs
            batch_quat[i, :quat_length] = quat.view(quat_length, -1)
            batch_quat_valid_idx[i] = quat_valid_idx
            batch_text[i, :text_length] = text
            batch_text_valid_idx[i] = text_valid_idx

        return batch_joint_offsets, batch_pos, batch_affs, batch_quat,\
            batch_quat_valid_idx, batch_text, batch_text_valid_idx

    def per_train(self):

        self.model.train()
        train_loader = self.data_loader['train']
        batch_loss = 0.
        N = 0.

        for joint_offsets, pos, affs, quat, quat_valid_idx,\
            text, text_valid_idx in self.yield_batch(self.args.batch_size, train_loader):

            self.optimizer.zero_grad()
            with torch.autograd.detect_anomaly():
                joint_lengths = torch.norm(joint_offsets, dim=-1)
                scales, _ = torch.max(joint_lengths, dim=-1)
                quat_pred, quat_pred_pre_norm = self.model(text, quat[:, :-1], joint_lengths / scales[..., None])

                quat_pred_pre_norm = quat_pred_pre_norm.view(quat_pred_pre_norm.shape[0],
                                                             quat_pred_pre_norm.shape[1], -1, self.D)
                quat_norm_loss = self.args.quat_norm_reg *\
                                 torch.mean((torch.sum(quat_pred_pre_norm ** 2, dim=-1) - 1) ** 2)

                quat_loss, quat_derv_loss = losses.quat_angle_loss(quat_pred, quat[:, 1:],
                                                                   quat_valid_idx[:, 1:],
                                                                   self.V, self.D,
                                                                   self.lower_body_start,
                                                                   self.args.upper_body_weight)
                quat_loss *= self.args.quat_reg

                root_pos = torch.zeros(quat_pred.shape[0], quat_pred.shape[1], self.C).cuda()
                pos_pred = MocapDataset.forward_kinematics(quat_pred.contiguous().view(
                    quat_pred.shape[0], quat_pred.shape[1], -1, self.D), root_pos, self.joint_parents,
                    torch.cat((root_pos[:, 0:1], joint_offsets), dim=1).unsqueeze(1))
                affs_pred = MocapDataset.get_mpi_affective_features(pos_pred)

                row_sums = quat_valid_idx.sum(1, keepdim=True) * self.D * self.V
                row_sums[row_sums == 0.] = 1.

                shifted_pos = pos - pos[:, :, 0:1]
                shifted_pos_pred = pos_pred - pos_pred[:, :, 0:1]
                recons_loss = torch.abs(shifted_pos_pred - shifted_pos[:, 1:]).sum(-1)
                recons_loss = self.args.upper_body_weight * (recons_loss[:, :, :self.lower_body_start].sum(-1)) +\
                              recons_loss[:, :, self.lower_body_start:].sum(-1)
                recons_loss = self.args.recons_reg *\
                              torch.mean((recons_loss * quat_valid_idx[:, 1:]).sum(-1) / row_sums)

                recons_derv_loss = torch.abs(shifted_pos_pred[:, 1:] - shifted_pos_pred[:, :-1] -
                                             shifted_pos[:, 2:] + shifted_pos[:, 1:-1]).sum(-1)
                recons_derv_loss = self.args.upper_body_weight *\
                    (recons_derv_loss[:, :, :self.lower_body_start].sum(-1)) +\
                                   recons_derv_loss[:, :, self.lower_body_start:].sum(-1)
                recons_derv_loss = 2. * self.args.recons_reg *\
                                   torch.mean((recons_derv_loss * quat_valid_idx[:, 2:]).sum(-1) / row_sums)

                affs_loss = torch.abs(affs[:, 1:] - affs_pred).sum(-1)
                affs_loss = self.args.affs_reg * torch.mean((affs_loss * quat_valid_idx[:, 1:]).sum(-1) / row_sums)

                train_loss = quat_norm_loss + quat_loss + recons_loss + recons_derv_loss + affs_loss
                train_loss.backward()
                # nn.utils.clip_grad_norm_(self.model.parameters(), self.args.gradient_clip)
                self.optimizer.step()

            # animation = {
            #     'joint_names': self.joint_names,
            #     'joint_offsets': joint_offsets,
            #     'joint_parents': self.joint_parents,
            #     'positions': pos,
            #     'rotations': quat
            # }
            # MocapDataset.save_as_bvh(animation,
            #                          dataset_name=self.dataset,
            #                          subset_name='gt')
            # animation_pred = {
            #     'joint_names': self.joint_names,
            #     'joint_offsets': joint_offsets,
            #     'joint_parents': self.joint_parents,
            #     'positions': pos_pred,
            #     'rotations': quat_pred
            # }
            # MocapDataset.save_as_bvh(animation_pred,
            #                          dataset_name=self.dataset,
            #                          subset_name='test')

            # Compute statistics
            batch_loss += train_loss.item()
            N += quat.shape[0]

            # statistics
            self.iter_info['loss'] = train_loss.data.item()
            self.iter_info['lr'] = '{:.6f}'.format(self.lr)
            self.iter_info['tf'] = '{:.6f}'.format(self.tf)
            self.show_iter_info()
            self.meta_info['iter'] += 1

        batch_loss = batch_loss / N
        self.epoch_info['mean_loss'] = batch_loss
        self.show_epoch_info()
        self.io.print_timer()
        self.adjust_lr()
        self.adjust_tf()

        # pos_pred_np = np.swapaxes(np.reshape(pos_pred.detach().cpu().numpy(),
        #                                      (pos_pred.shape[0], self.T - 1, -1)), 2, 1)
        # display_animations(pos_pred_np, self.joint_parents,
        #                    save=True, dataset_name=self.dataset, subset_name='test', overwrite=True)

    def per_test(self):

        self.model.eval()
        test_loader = self.data_loader['test']
        eval_loss = 0.
        N = 0.

        for joint_offsets, pos, affs, quat, quat_valid_idx,\
            text, text_valid_idx in self.yield_batch(self.args.batch_size, test_loader):
            with torch.no_grad():
                joint_lengths = torch.norm(joint_offsets, dim=-1)
                scales, _ = torch.max(joint_lengths, dim=-1)
                quat_pred = torch.zeros_like(quat)
                quat_pred_pre_norm = torch.zeros_like(quat)
                quat_pred[:, 0] = torch.cat(quat_pred.shape[0] * [self.quats_sos]).view(quat_pred[:, 0].shape)
                text_latent = self.model(text, only_encoder=True)
                for s in range(1, self.T):
                    quat_pred_last, quat_pred_pre_norm_last = self.model(text_latent,
                                                                         quat_pred[:, :s],
                                                                         joint_lengths / scales[..., None],
                                                                         only_decoder=True)
                    quat_pred[:, s:s + 1] = quat_pred_last[:, -1:]
                    quat_pred_pre_norm[:, s:s + 1] = quat_pred_pre_norm_last[:,  -1:]

                quat_pred_pre_norm = quat_pred_pre_norm.view(quat_pred_pre_norm.shape[0],
                                                             quat_pred_pre_norm.shape[1], -1, self.D)
                quat_norm_loss = self.args.quat_norm_reg *\
                                 torch.mean((torch.sum(quat_pred_pre_norm ** 2, dim=-1) - 1) ** 2)

                quat_loss, quat_derv_loss = losses.quat_angle_loss(quat_pred[:, 1:], quat[:, 1:],
                                                                   quat_valid_idx[:, 1:],
                                                                   self.V, self.D,
                                                                   self.lower_body_start,
                                                                   self.args.upper_body_weight)
                quat_loss *= self.args.quat_reg

                root_pos = torch.zeros(quat_pred.shape[0], quat_pred.shape[1], self.C).cuda()
                pos_pred = MocapDataset.forward_kinematics(quat_pred.contiguous().view(
                    quat_pred.shape[0], quat_pred.shape[1], -1, self.D), root_pos, self.joint_parents,
                    torch.cat((root_pos[:, 0:1], joint_offsets), dim=1).unsqueeze(1))
                affs_pred = MocapDataset.get_mpi_affective_features(pos_pred)

                row_sums = quat_valid_idx.sum(1, keepdim=True) * self.D * self.V
                row_sums[row_sums == 0.] = 1.

                shifted_pos = pos - pos[:, :, 0:1]
                shifted_pos_pred = pos_pred - pos_pred[:, :, 0:1]
                recons_loss = torch.abs(shifted_pos_pred[:, 1:] - shifted_pos[:, 1:]).sum(-1)
                recons_loss = self.args.upper_body_weight * (recons_loss[:, :, :self.lower_body_start].sum(-1)) + \
                              recons_loss[:, :, self.lower_body_start:].sum(-1)
                recons_loss = self.args.recons_reg * torch.mean(
                    (recons_loss * quat_valid_idx[:, 1:]).sum(-1) / row_sums)

                recons_derv_loss = torch.abs(shifted_pos_pred[:, 2:] - shifted_pos_pred[:, 1:-1] -
                                             shifted_pos[:, 2:] + shifted_pos[:, 1:-1]).sum(-1)
                recons_derv_loss = self.args.upper_body_weight * \
                                   (recons_derv_loss[:, :, :self.lower_body_start].sum(-1)) + \
                                   recons_derv_loss[:, :, self.lower_body_start:].sum(-1)
                recons_derv_loss = 2. * self.args.recons_reg * \
                                   torch.mean((recons_derv_loss * quat_valid_idx[:, 2:]).sum(-1) / row_sums)

                affs_loss = torch.abs(affs[:, 1:] - affs_pred[:, 1:]).sum(-1)
                affs_loss = self.args.affs_reg * torch.mean((affs_loss * quat_valid_idx[:, 1:]).sum(-1) / row_sums)

                eval_loss += quat_norm_loss + quat_loss + recons_loss + recons_derv_loss + affs_loss
                N += quat.shape[0]

        eval_loss /= N
        self.epoch_info['mean_loss'] = eval_loss
        if self.epoch_info['mean_loss'] < self.best_loss and self.meta_info['epoch'] > self.min_train_epochs:
            self.best_loss = self.epoch_info['mean_loss']
            self.best_loss_epoch = self.meta_info['epoch']
            self.loss_updated = True
        else:
            self.loss_updated = False
        self.show_epoch_info()

    def train(self):

        if self.args.load_last_best:
            best_model_found = self.load_best_model()
            self.args.start_epoch = self.best_loss_epoch if best_model_found else 0
        for epoch in range(self.args.start_epoch, self.args.num_epoch):
            self.meta_info['epoch'] = epoch

            # training
            self.io.print_log('Training epoch: {}'.format(epoch))
            self.per_train()
            self.io.print_log('Done.')

            # evaluation
            if (epoch % self.args.eval_interval == 0) or (
                    epoch + 1 == self.args.num_epoch):
                self.io.print_log('Eval epoch: {}'.format(epoch))
                self.per_test()
                self.io.print_log('Done.')

            # save model and weights
            if self.loss_updated:
                torch.save({'model_dict': self.model.state_dict()},
                           os.path.join(self.args.work_dir, 'epoch_{}_loss_{:.4f}_model.pth.tar'.
                                        format(epoch, self.best_loss)))

                if self.generate_while_train:
                    self.generate_motion(load_saved_model=False, samples_to_generate=1)

    def copy_prefix(self, var, prefix_length=None):
        if prefix_length is None:
            prefix_length = self.prefix_length
        return [var[s, :prefix_length].unsqueeze(0) for s in range(var.shape[0])]

    def generate_linear_trajectory(self, traj, alpha=0.001):
        traj_markers = (traj[:, self.prefix_length - 2] +
                        (traj[:, self.prefix_length - 1] - traj[:, self.prefix_length - 2]) / alpha).unsqueeze(1)
        return traj_markers

    def generate_circular_trajectory(self, traj, alpha=5., num_segments=10):
        last_segment = alpha * traj[:, self.prefix_length - 1:self.prefix_length] -\
                       traj[:, self.prefix_length - 2:self.prefix_length - 1]
        last_marker = traj[:, self.prefix_length - 1:self.prefix_length]
        traj_markers = last_marker.clone()
        angle_per_segment = 2. * np.pi / num_segments
        for _ in range(num_segments):
            next_segment = qrot(expmap_to_quaternion(
                torch.tensor([0, -angle_per_segment, 0]).cuda().float().repeat(
                    last_segment.shape[0], last_segment.shape[1], 1)), torch.cat((
                last_segment[..., 0:1],
                torch.zeros_like(last_segment[..., 0:1]),
                last_segment[..., 1:]), dim=-1))[..., [0, 2]]
            next_marker = next_segment + last_marker
            traj_markers = torch.cat((traj_markers, next_marker), dim=1)
            last_segment = next_segment.clone()
            last_marker = next_marker.clone()
        traj_markers = traj_markers[:, 1:]
        return traj_markers

    def compute_next_traj_point(self, traj, traj_marker, rs_pred):
        tangent = traj_marker - traj
        tangent /= (torch.norm(tangent, dim=-1) + 1e-9)
        return tangent * rs_pred + traj

    def compute_next_traj_point_sans_markers(self, pos_last, quat_next, z_pred, rs_pred):
        pos_next = torch.zeros_like(pos_last)
        offsets = torch.from_numpy(self.mocap.joint_offsets).cuda().float(). \
            unsqueeze(0).unsqueeze(0).repeat(pos_last.shape[0], pos_last.shape[1], 1, 1)
        quat_copy = quat_next.contiguous().view(quat_next.shape[0], quat_next.shape[1], -1, self.D).clone()
        for joint in range(1, self.V):
            pos_next[:, :, joint] = qrot(quat_copy[:, :, joint - 1], offsets[:, :, joint]) \
                                    + pos_next[:, :, self.mocap.joint_parents[joint]]
        root = pos_next[:, :, 0]
        l_shoulder = pos_next[:, :, 13]
        r_shoulder = pos_next[:, :, 17]
        facing = torch.cross(l_shoulder - root, r_shoulder - root, dim=-1)[..., [0, 2]]
        facing /= (torch.norm(facing, dim=-1)[..., None] + 1e-9)
        return rs_pred * facing + pos_last[:, :, 0, [0, 2]]

    def get_diff_from_traj(self, pos_pred, traj_pred, s):
        root = pos_pred[s][:, :, 0]
        l_shoulder = pos_pred[s][:, :, 13]
        r_shoulder = pos_pred[s][:, :, 17]
        facing = torch.cross(l_shoulder - root, r_shoulder - root, dim=-1)[..., [0, 2]]
        facing /= (torch.norm(facing, dim=-1)[..., None] + 1e-9)
        tangents = traj_pred[s][:, 1:] - traj_pred[s][:, :-1]
        tangent_norms = torch.norm(tangents, dim=-1)
        tangents /= (tangent_norms[..., None] + 1e-9)
        tangents = torch.cat((torch.zeros_like(tangents[:, 0:1]), tangents), dim=1)
        tangent_norms = torch.cat((torch.zeros_like(tangent_norms[:, 0:1]), tangent_norms), dim=1)
        angle_diff = torch.acos(torch.einsum('ijk,ijk->ij', facing, tangents).clamp(min=-1., max=1.))
        angle_diff[tangent_norms < 1e-6] = 0.
        return angle_diff

    def rotate_gaits(self, pos_pred, quat_pred, quat_diff, head_tilt, l_shoulder_slouch, r_shoulder_slouch, s):
        quat_copy = quat_pred[s].contiguous().view(
            quat_pred[s].shape[0], quat_pred[s].shape[1], -1, self.D).clone()
        pos_copy = pos_pred[s].clone()
        for j in range(1, self.V):
            quat_copy[:, :, j - 1] = torch.from_numpy((Quaternions(quat_diff) *
                                                       Quaternions(quat_copy[:, :, j - 1].cpu().numpy())).qs
                                                      ).cuda().float()
            if j == 12:
                quat_copy[:, :, j - 1] = torch.from_numpy((Quaternions(head_tilt) *
                                                           Quaternions(quat_copy[:, :, j - 1].cpu().numpy())).qs
                                                          ).cuda().float()
            if j == 13:
                quat_copy[:, :, j - 1] = torch.from_numpy((Quaternions(l_shoulder_slouch) *
                                                           Quaternions(quat_copy[:, :, j - 1].cpu().numpy())).qs
                                                          ).cuda().float()
            if j == 17:
                quat_copy[:, :, j - 1] = torch.from_numpy((Quaternions(r_shoulder_slouch) *
                                                           Quaternions(quat_copy[:, :, j - 1].cpu().numpy())).qs
                                                          ).cuda().float()
            pos_copy[:, :, j] = qrot(quat_copy[:, :, j - 1],
                                     torch.from_numpy(
                                         self.mocap.joint_offsets[j]
                                     ).cuda().float().unsqueeze(0).unsqueeze(0)
                                     .repeat(pos_copy.shape[0], pos_copy.shape[1], 1)) + \
                                pos_copy[:, :, self.mocap.joint_parents[j]]
        return pos_copy, quat_copy

    def generate_motion(self, load_saved_model=True, samples_to_generate=10, max_steps=300, randomized=True):

        if load_saved_model:
            self.load_best_model()
        self.model.eval()
        test_loader = self.data_loader['test']

        joint_offsets, pos, quat, quat_valid_idx,\
        text, text_valid_idx = self.return_batch([samples_to_generate], test_loader, randomized=randomized)
        with torch.no_grad():
            joint_lengths = torch.norm(joint_offsets, dim=-1)

            scales, _ = torch.max(joint_lengths, dim=-1)
            quat_pred = torch.zeros_like(quat)
            quat_pred_pre_norm = torch.zeros_like(quat)
            quat_pred[:, 0] = torch.cat(quat_pred.shape[0] * [self.quats_sos]).view(quat_pred[:, 0].shape)
            text_latent = self.model(text, only_encoder=True)
            for s in range(1, self.T):
                quat_pred_last, quat_pred_pre_norm_last = self.model(text_latent,
                                                                     quat_pred[:, :s],
                                                                     joint_lengths / scales[..., None],
                                                                     only_decoder=True)
                quat_pred[:, s:s + 1] = quat_pred_last[:, -1:]
                quat_pred_pre_norm[:, s:s + 1] = quat_pred_pre_norm_last[:, -1:]
        root_pos = torch.zeros(quat_pred.shape[0], quat_pred.shape[1], self.C).cuda()
        pos_pred = MocapDataset.forward_kinematics(quat_pred.contiguous().view(
            quat_pred.shape[0], quat_pred.shape[1], -1, self.D), root_pos, self.joint_parents,
            torch.cat((root_pos[:, 0:1], joint_offsets), dim=1).unsqueeze(1)).cpu().numpy()
        pos_pred = np.swapaxes(np.reshape(pos_pred, (pos_pred.shape[0], pos_pred.shape[1], -1)), 1, 2)

        # pos_np = np.swapaxes(
        #     np.reshape((pos - pos[:, :, 0:1]).cpu().numpy(),
        #                (pos.shape[0], pos.shape[1], -1)), 1, 2)

        # self.mocap.save_as_bvh(pos_pred[s][:, :, 0].detach().cpu().numpy(),
        #                        o_z_rs_pred[s][..., 0:1].detach().cpu().numpy(),
        #                        np.reshape(quat_pred[s].detach().cpu().numpy(),
        #                                   (quat_pred[s].shape[0], quat_pred[s].shape[1], -1, self.D)),
        #                        dataset_name=self.dataset,
        #                        # subset_name='epoch_' + str(self.best_loss_epoch),
        #                        # save_file_names=[str(s).zfill(6)])
        #                        subset_name=os.path.join('epoch_' + str(self.best_loss_epoch), label_dir),
        #                        save_file_names=[save_file_name])

        display_animations(pos_pred, self.joint_parents, save=True,
                           dataset_name=self.dataset,
                           subset_name='epoch_' + str(self.best_loss_epoch),
                           overwrite=True)