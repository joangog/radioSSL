from glob import glob
import numpy as np
import re
import math
import os
import re
import time
import torch
from torch.utils.tensorboard import SummaryWriter
import random
from PIL import ImageFilter
import torch.nn.functional as F
from torchvision import ops
import segmentation_models_pytorch as smp
from models import PCRLv23d, PCRLv2, SegmentationModel, UNet3D


def set_seed(seed):
    os.environ['PYTHONHASHSEED'] = str(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    np.random.seed(seed)
    random.seed(seed)


def seed_worker(worker_id):  # This is for the workers of the dataloader that need different seeds
    worker_seed = torch.initial_seed() % 2**32
    np.random.seed(worker_seed)
    random.seed(worker_seed)


def create_logger(args):
    curr_time = str(time.time()).replace(".", "")
    
    if args.phase in ['finetune', 'pretask']:  # If finetune or pretask, use the specified output path
        folder_name = None
        
        if args.phase == 'finetune':
            if 'cluster' in args.model:
                cluster_k = re.search(r'_k[0-9]+_', args.weight).group(0)[1:]
            else:
                cluster_k = ''
            sc = 'sc_' if args.skip_conn else ''
            run_name = f'{args.model}_{args.d}d_{cluster_k}{sc}pretrain_{args.pretrained}_finetune_{args.finetune}_b{args.b}_e{args.epochs}_lr{"{:f}".format(args.lr).split(".")[-1]}_r{int(args.ratio * 100)}_t{curr_time}'
            
            pretrain_type = None
            # Pretrain types that use weights
            if args.weight:  
                weight_dir = args.weight.lower()
                pretrain_type = args.model
                if 'luna' in weight_dir:
                    pretrain_type += '_luna_pretrain'
                elif 'brats' in weight_dir:
                    pretrain_type += '_brats_pretrain'
                elif 'lits' in weight_dir:
                    pretrain_type += '_lits_pretrain'
                elif 'chest' in weight_dir:
                    pretrain_type += '_chest_pretrain'
            # Pretrain types that don't use weights
            elif not args.weight:
                if args.model == 'imagenet':
                    pretrain_type = f'imagenet_pretrain'
                elif args.model == 'scratch' or args.pretrained == 'none':
                    pretrain_type = f'scratch_{args.d}d'
            folder_name =  args.n + '_finetune' + '_' + pretrain_type
        
        elif args.phase == 'pretask':
            sc = "sc_" if args.skip_conn else ""
            if args.model == 'pcrlv2':
                run_name = f'{args.model}_{args.d}d_{sc}pretask_b{args.b}_e{args.epochs}_lr{"{:f}".format(args.lr).split(".")[-1]}_t{curr_time}'
            elif 'cluster' in args.model:
                run_name = f'{args.model}_{args.d}d_k{args.k}_{args.cluster_loss}_{sc}pretask_b{args.b}_e{args.epochs}_lr{"{:f}".format(args.lr).split(".")[-1]}_t{curr_time}'
            folder_name = args.model + '_' + args.n + '_pretrain'
        
        if not os.path.exists(os.path.join(args.output,folder_name)):
            os.makedirs(os.path.join(args.output,folder_name))
        run_dir = os.path.join(args.output, folder_name, run_name)
    
    elif args.phase == 'test':  # If test, then just use the path from the loaded weights
        run_dir = args.weight.replace('.pt','') # remove .pt

    writer = None   
    if args.tensorboard or args.vis:  # Create tensorboard writer
        assert args.tensorboard  # args.vis can only be used with args.tensorboard
        print(f'Tensorboard logging at: {run_dir}\n')
        writer = SummaryWriter(run_dir)

    return writer, run_dir


def get_model(args, in_channels, n_class):
    if args.model == 'scratch':
        if args.phase == 'finetune':
            assert args.pretrained == 'none'
            assert args.weight == None
        if args.d == 3:
            model = SegmentationModel(in_channels=in_channels, n_class=n_class, norm='gn', skip_conn=args.skip_conn)
        elif args.d == 2:
            model = PCRLv2(in_channels=in_channels, n_class=n_class, segmentation=True)
    elif args.model == 'pcrlv2':
        if args.phase == 'finetune':
            assert args.pretrained != 'none'
            assert args.weight
        if args.d == 3:
            model = SegmentationModel(in_channels=in_channels, n_class=n_class, norm='gn', skip_conn=args.skip_conn)
        elif args.d == 2:
            model = PCRLv2(in_channels=in_channels, n_class=n_class)
    elif 'cluster' in args.model:
        if args.phase == 'finetune':
            assert args.pretrained != 'none'
            assert args.weight
        if args.d == 3:
            model = SegmentationModel(in_channels=in_channels, n_class=n_class, norm='gn', skip_conn=args.skip_conn)
        elif args.d == 2:
            model = PCRLv2(in_channels=in_channels, n_class=n_class)
    elif args.model == 'genesis':
        assert args.d == 3
        if args.phase == 'finetune':
            assert args.pretrained != 'none'
            assert args.weight
        # TODO: Implement 2d too
        model = UNet3D(in_chann=in_channels, n_class=n_class)
    elif args.model == 'imagenet':
        assert args.d == 2
        if args.phase == 'finetune':
            assert args.pretrained == 'encoder'
            assert args.weight == None
        model = PCRLv2(in_channels=in_channels, n_class=n_class, encoder_weights='imagenet', segmentation=True)
    return model


def prepare_model(args, in_channels, n_class):

    # Get model
    model = get_model(args, in_channels, n_class)
          
    # Prepare for finetuning
    if args.phase == 'finetune':

        pretrain_dict = {}

        # Load pretrained weights (if it applies)
        if args.weight and args.pretrained != 'none':  # If there is a weight file and we want a pretrained model

            model_dict = model.state_dict()
            weight_path = args.weight
            if args.cpu:
                state_dict = torch.load(weight_path, map_location=torch.device('cpu'))['state_dict']
            else:
                state_dict = torch.load(weight_path)['state_dict']
            
            # If model is genesis, unparallelize weights
            if args.model == 'genesis':
                tmp_state_dict = {}
                for key in state_dict.keys():
                    tmp_state_dict[key.replace("module.", "")] = state_dict[key]
                state_dict = tmp_state_dict

            if args.pretrained == 'encoder' or args.pretrained == 'all':
                # Load pretrained encoder
                if args.d == 3:
                    first_conv_weight = state_dict['down_tr64.ops.0.conv1.weight']
                    first_conv_weight = first_conv_weight.repeat((1, in_channels, 1, 1, 1))
                    state_dict['down_tr64.ops.0.conv1.weight'] = first_conv_weight
                    pretrain_dict.update({k: v for k, v in state_dict.items() if
                                k in model_dict and 'down_tr' in k})
                elif args.d == 2:
                    first_conv_weight = state_dict['encoder.conv1.weight']
                    first_conv_weight = first_conv_weight.repeat((1, in_channels, 1, 1))
                    state_dict['encoder.conv1.weight'] = first_conv_weight
                    pretrain_dict.update({k: v for k, v in state_dict.items() if
                                k in model_dict and 'encoder' in k})
                
            if args.pretrained == 'all':
                # Load pretrained decoder
                if args.d == 3:
                    last_conv_weight = state_dict['out_tr.final_conv.weight']
                    last_conv_weight = last_conv_weight.repeat((n_class, 1, 1, 1, 1))
                    state_dict['out_tr.final_conv.weight'] = last_conv_weight
                    last_conv_bias = state_dict['out_tr.final_conv.bias']
                    last_conv_bias = last_conv_bias.repeat((n_class))
                    state_dict['out_tr.final_conv.bias'] = last_conv_bias
                    # If skip connections are added, then do not load up_tr*.ops.0.*
                    if args.skip_conn:  
                        pretrain_dict.update({k: v for k, v in state_dict.items() if
                                        k in model_dict and ('up_tr' in k or 'out_tr' in k) and 'ops.0' not in k})  # Train skip conn first conv (ops.0) from scratch
                    else:
                        pretrain_dict.update({k: v for k, v in state_dict.items() if
                                        k in model_dict and ('up_tr' in k or 'out_tr' in k)})
                elif args.d == 2:
                    pass
                    # TODO: implement
                    # last_conv_weight = state_dict['decoder.final_conv.weight']
                    # last_conv_weight = last_conv_weight.repeat((n_class, 1, 1, 1, 1))
                    # state_dict['out_tr.final_conv.weight'] = last_conv_weight
                    # last_conv_bias = state_dict['out_tr.final_conv.bias']
                    # last_conv_bias = last_conv_bias.repeat((n_class))
                    # state_dict['out_tr.final_conv.bias'] = last_conv_bias
                    # # If skip connections are added, then do not load up_tr*.ops.0.*
                    # if args.skip_conn:  
                    #     pretrain_dict.update({k: v for k, v in state_dict.items() if
                    #                     k in model_dict and ('up_tr' in k or 'out_tr' in k) and 'ops.0' not in k})  # Train skip conn first conv (ops.0) from scratch
                    # else:
                    #     pretrain_dict.update({k: v for k, v in state_dict.items() if
                    #                     k in model_dict and ('up_tr' in k or 'out_tr' in k)})

            model_dict.update(pretrain_dict)
            model.load_state_dict(model_dict)

        # Set finetune weights
        for name, param in model.named_parameters():
            param.requires_grad = True  # Make all parameters trainable

        if args.finetune == 'last':  # Freeze everything but last layer
            for name, param in model.named_parameters():
                if all(layer not in name for layer in ['out_tr', 'segmentation_head']):
                    param.requires_grad = False

        elif args.finetune == 'decoder':  # Freeze encoder
            for name, param in model.named_parameters():
                if all(layer not in name for layer in ['up_tr', 'out_tr', 'decoder', 'segmentation_head']):  # up_tr and out_tr for cluster/pcrlv2/genesis/scratch, decoder and seg_head for imagenet
                    param.requires_grad = False

        # Print parameters
        model_dict = model.state_dict()
        print(f'Pretrained parameters from weight file (including buffers): {len(pretrain_dict.keys())}/{len(model_dict.keys())}')
        if pretrain_dict:
            print(pretrain_dict.keys())
        else:
            print(None)
        print()
        finetune_dict = {k: v for k, v in model.named_parameters() if
                                v.requires_grad == True}                         
        print(f'Finetuning parameters: {len(finetune_dict.keys())}/{len(list(model.named_parameters()))}')
        if len(finetune_dict)!=0:
            print(finetune_dict.keys())
        else:
            print(None)
        print()
        frozen_dict = {k: v for k, v in model.named_parameters() if
                                v.requires_grad == False}
        print('Frozen parameters:')
        if len(frozen_dict)!=0:
            print(frozen_dict.keys())
        else:
            print(None)
        print()

    # Prepare for testing
    elif args.phase == 'test':
        weight_path = args.weight    
        if args.cpu:
            state_dict = torch.load(weight_path, map_location=torch.device('cpu'))['state_dict']
        else:
            state_dict = torch.load(weight_path)['state_dict']
        model.load_state_dict(state_dict)

    return model


def get_chest_list(txt_path, data_dir):
    image_names = []
    labels = []
    with open(txt_path, "r") as f:
        for line in f:
            items = line.split()
            image_name = items[0]
            label = items[1:]
            label = [int(i) for i in label]
            image_name = os.path.join(data_dir, image_name)
            image_names.append(image_name)
            labels.append(label)
    return image_names, labels


def get_luna_pretrain_list(ratio):
    x_train = []
    with open('train_val_txt/luna_train.txt', 'r') as f:
        for line in f:
            x_train.append(line.strip('\n'))
    return x_train[:int(len(x_train) * ratio)]


def get_luna_finetune_list(ratio, path, train_fold):
    x_train = []
    with open('train_val_txt/luna_train.txt', 'r') as f:
        for line in f:
            x_train.append(line.strip('\n'))
    return x_train[:int(len(x_train) * ratio)]


def get_luna_list(config, train_fold, valid_fold, test_fold, suffix, file_list):
    x_train = []
    x_valid = []
    x_test = []
    for i in train_fold:
        for file in os.listdir(os.path.join(config.data, 'subset' + str(i))):
            if suffix in file and 'gt' not in file:
                if file_list is not None and file.split('_')[0] in file_list:
                    x_train.append(os.path.join(config.data, 'subset' + str(i), file))
                elif file_list is None:
                    x_train.append(os.path.join(config.data, 'subset' + str(i), file))
    for i in valid_fold:
        for file in os.listdir(os.path.join(config.data, 'subset' + str(i))):
            if suffix in file and 'gt' not in file:
                x_valid.append(os.path.join(config.data, 'subset' + str(i), file))
    for i in test_fold:
        for file in os.listdir(os.path.join(config.data, 'subset' + str(i))):
            if suffix in file and 'gt' not in file:
                x_test.append(os.path.join(config.data, 'subset' + str(i), file))
    return x_train, x_valid, x_test

def get_lidc_list(ratio):
    val_patients_list = []
    train_patients_list = []
    test_patients_list = []
    with open('./train_val_txt/lidc_train.txt', 'r') as f:
        for line in f:
            line = line.strip('\n')
            train_patients_list.append(line)
    with open('./train_val_txt/lidc_valid.txt', 'r') as f:
        for line in f:
            line = line.strip('\n')
            val_patients_list.append(line)
    with open('./train_val_txt/lidc_test.txt', 'r') as f:
        for line in f:
            line = line.strip('\n')
            test_patients_list.append(line)
    train_patients_list = train_patients_list[: int(len(train_patients_list) * ratio)]
    print(
        f"Train Patients: {len(train_patients_list)}, Valid Patients: {len(val_patients_list)},"
        f"Test Patients {len(test_patients_list)}\n")
    return train_patients_list, val_patients_list, test_patients_list


def get_brats_list(data, ratio):
    val_patients_list = []
    train_patients_list = []
    test_patients_list = []
    with open('./train_val_txt/brats_train.txt', 'r') as f:
        for line in f:
            line = line.strip('\n')
            train_patients_list.append(os.path.join(data, line))
    with open('./train_val_txt/brats_valid.txt', 'r') as f:
        for line in f:
            line = line.strip('\n')
            val_patients_list.append(os.path.join(data, line))
    with open('./train_val_txt/brats_test.txt', 'r') as f:
        for line in f:
            line = line.strip('\n')
            test_patients_list.append(os.path.join(data, line))
    train_patients_list = train_patients_list[: int(len(train_patients_list) * ratio)]
    print(
        f"Train Patients: {len(train_patients_list)}, Valid Patients: {len(val_patients_list)},"
        f"Test Patients {len(test_patients_list)}\n")
    return train_patients_list, val_patients_list, test_patients_list


def get_brats_pretrain_list(data, ratio, suffix):
    val_patients_list = []
    train_patients_list = []
    test_patients_list = []
    with open('./train_val_txt/brats_train.txt', 'r') as f:
        for line in f:
            line = line.strip('\n')
            train_patient_path = os.path.join(data, line)
            for file in os.listdir(train_patient_path):
                if suffix in file and 'gt' not in file:  # Do not include ground truth files for clustering
                    train_patients_list.append(os.path.join(train_patient_path, file))
    with open('./train_val_txt/brats_valid.txt', 'r') as f:
        for line in f:
            line = line.strip('\n')
            val_patient_path = os.path.join(data, line)
            for file in os.listdir(val_patient_path):
                if suffix in file and 'gt' not in file:
                    val_patients_list.append(os.path.join(val_patient_path, file))
    with open('./train_val_txt/brats_test.txt', 'r') as f:
        for line in f:
            line = line.strip('\n')
            test_patient_path = os.path.join(data, line)
            for file in os.listdir(test_patient_path):
                if suffix in file and 'gt' not in file:
                    test_patients_list.append(os.path.join(test_patient_path, file))
    train_patients_list = train_patients_list[: int(len(train_patients_list) * ratio)]
    print(
        f"train patients: {len(train_patients_list)}, valid patients: {len(val_patients_list)},"
        f"test patients {len(test_patients_list)}")
    return train_patients_list, val_patients_list, test_patients_list


def get_luna_finetune_nodule(config, train_fold, valid_txt, test_txt, suffix, file_list):
    x_train = []
    x_valid = []
    x_test = []
    for i in train_fold:
        for file in os.listdir(os.path.join(config.data, 'subset' + str(i))):
            if suffix in file and 'gt' not in file:
                if file_list is not None and file.split('_')[0] in file_list:
                    x_train.append(os.path.join(config.data, 'subset' + str(i), file))
                elif file_list is None:
                    x_train.append(os.path.join(config.data, 'subset' + str(i), file))
    with open(valid_txt, 'r') as f:
        for line in f:
            x_valid.append(line.strip('\n'))
    with open(test_txt, 'r') as f:
        for line in f:
            x_test.append(line.strip('\n'))
    return x_train, x_valid, x_test


def divide_luna_true_positive(data_list):
    true_list = []
    false_list = []
    for i in data_list:
        name = os.path.split(i)[-1]
        label = name.split('_')[1]
        if label == '1':
            true_list.append(i)
        else:
            false_list.append(i)
    return true_list, false_list


class Cutout(object):
    """Randomly mask out one or more patches from an image.
    Args:
        n_holes (int): Number of patches to cut out of each image.
        length (int): The length (in pixels) of each square patch.
    """

    def __init__(self, n_holes, length):
        self.n_holes = n_holes
        self.length = length

    def __call__(self, img):
        """
        Args:
            img (Tensor): Tensor image of size (C, H, W).
        Returns:
            Tensor: Image with n_holes of dimension length x length cut out of it.
        """
        h = img.size(1)
        w = img.size(2)

        mask = np.ones((h, w), np.float32)

        for n in range(self.n_holes):
            y = np.random.randint(h)
            x = np.random.randint(w)

            y1 = np.clip(y - self.length // 2, 0, h)
            y2 = np.clip(y + self.length // 2, 0, h)
            x1 = np.clip(x - self.length // 2, 0, w)
            x2 = np.clip(x + self.length // 2, 0, w)

            mask[y1: y2, x1: x2] = 0.

        mask = torch.from_numpy(mask)
        mask = mask.expand_as(img)
        img = img * mask

        return img


def adjust_learning_rate(epoch, args, optimizer):
    # iterations = opt.lr_decay_epochs.split(',')
    # opt.lr_decay_epochs_list = list([])
    # for it in iterations:
    #     opt.lr_decay_epochs_list.append(int(it))
    # steps = np.sum(epoch > np.asarray(opt.lr_decay_epochs_list))
    # if steps > 0:
    #     new_lr = opt.lr * (opt.lr_decay_rate ** steps)
    #     for param_group in optimizer.param_groups:
    #         param_group['lr'] = new_lr
    lr = args.lr
    lr *= 0.5 * (1. + math.cos(math.pi * epoch / args.epochs))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


class GaussianBlur(object):
    """Gaussian blur augmentation in SimCLR https://arxiv.org/abs/2002.05709"""

    def __init__(self, sigma=[.1, 2.]):
        self.sigma = sigma

    def __call__(self, x):
        sigma = random.uniform(self.sigma[0], self.sigma[1])
        x = x.filter(ImageFilter.GaussianBlur(radius=sigma))
        return x


def bceDiceLoss(input, target, train=True):
    bce = F.binary_cross_entropy_with_logits(input, target)
    smooth = 1e-5
    num = target.size(0)
    input = input.reshape(num, -1)
    target = target.reshape(num, -1)
    intersection = (input * target)
    dice = (2. * intersection.sum(1) + smooth) / (input.sum(1) + target.sum(1) + smooth)
    dice = 1 - dice.sum() / num  # Notice that the lower the dice loss the better (we minimize the loss)
    if train:
        return dice + 0.2 * bce
    return dice


def dice_coeff(input, target):
    smooth = 1e-5
    num = target.size(0)
    input = input.reshape(num, -1)
    target = target.reshape(num, -1)
    intersection = (input * target)
    dice = (2. * intersection.sum(1) + smooth) / (input.sum(1) + target.sum(1) + smooth)
    dice = dice.sum() / num  # Notice that the higher the dice score the better
    dice = dice.item()
    return dice


# Segmentation Losses

def get_loss(dataset):
    loss_fun_name = dataset + '_dice_loss'
    loss_fun = globals()[loss_fun_name]
    return loss_fun

def lidc_dice_loss(input, target, train=True):
    loss = bceDiceLoss(input, target, train)
    print(f'loss: {loss}')
    return loss

def brats_dice_loss(input, target, train=True):
    wt_loss = bceDiceLoss(input[:, 0], target[:, 0], train)
    tc_loss = bceDiceLoss(input[:, 1], target[:, 1], train)
    et_loss = bceDiceLoss(input[:, 2], target[:, 2], train)
    print(f'wt loss: {wt_loss}, tc_loss : {tc_loss}, et_loss: {et_loss}')
    return wt_loss + tc_loss + et_loss

def lits_dice_loss(input, target, train=True):
    loss = bceDiceLoss(input, target, train)
    print(f'loss: {loss}')
    return loss


# Clustering Losses

def ce_loss(gt, out):
    loss = - torch.mean(gt * torch.log(out))
    return loss

def swav_loss(gt1, gt2, out1, out2):
    loss1 = ce_loss(gt1,out2)
    loss2 = ce_loss(gt2,out1)
    loss = 0.5 * (loss1 + loss2)
    return loss


def sinkhorn(args, Q: torch.Tensor, nmb_iters: int) -> torch.Tensor:
    with torch.no_grad():
        sum_Q = torch.sum(Q)
        Q /= sum_Q

        K, B = Q.shape

        if not args.cpu:
            u = torch.zeros(K).cuda()
            r = torch.ones(K).cuda() / K
            c = torch.ones(B).cuda() / B
        else:
            u = torch.zeros(K)
            r = torch.ones(K) / K
            c = torch.ones(B) / B

        for _ in range(nmb_iters):
            u = torch.sum(Q, dim=1)

            Q *= (r / u).unsqueeze(1)
            Q *= (c / torch.sum(Q, dim=0)).unsqueeze(0)

        return (Q / torch.sum(Q, dim=0, keepdim=True)).t().float()


def roi_align_intersect(pred1, pred2, gt1, gt2, box1, box2):
    # Cluster assignments to align for crop 1 and crop 2: pred1, pred2, gt1, gt2
    # Coordinates of the crop bounding box : box1, box2

    # Input Crop dimensions (Original, before standardizing to one common size for batchification)
    H1 = box1[:,1] - box1[:,0]
    W1 = box1[:,3] - box1[:,2]
    D1 = box1[:,5] - box1[:,4]
    H2 = box2[:,1] - box2[:,0]
    W2 = box2[:,3] - box2[:,2]
    D2 = box2[:,5] - box2[:,4]

    # Output Dimensions
    B, K, H, W, D = pred1.shape

    # Convert to float
    pred1 = pred1.float()
    pred2 = pred2.float()
    gt1 = gt1.float()
    gt2 = gt2.float()

    # Calculate interesection box of the two crop bounding boxes
    x1 = torch.maximum(box1[:,0], box2[:,0])
    x2 = torch.minimum(box1[:,1], box2[:,1])
    y1 = torch.maximum(box1[:,2], box2[:,2])
    y2 = torch.minimum(box1[:,3], box2[:,3])
    z1 = torch.maximum(box1[:,4], box2[:,4])
    z2 = torch.minimum(box1[:,5], box2[:,5])

    # Coordinates of intersecting box inside bbox 1 (percentage coordinates)
    x1_1 = (x1-box1[:,0]) / H1
    x1_2 = (x2-box1[:,0]) / H1
    y1_1 = (y1-box1[:,2]) / W1
    y1_2 = (y2-box1[:,2]) / W1
    z1_1 = (z1-box1[:,4]) / D1
    z1_2 = (z2-box1[:,4]) / D1

    # Coordinates of intersecting box inside bbox 2 (percentage coordinates)
    x2_1 = (x1-box2[:,0]) / H2
    x2_2 = (x2-box2[:,0]) / H2
    y2_1 = (y1-box2[:,2]) / W2
    y2_2 = (y2-box2[:,2]) / W2
    z2_1 = (z1-box2[:,4]) / D2
    z2_2 = (z2-box2[:,4]) / D2

    # Convert percentage coordinates to coordinates in output
    x1_1 = (x1_1 * H).int()
    x1_2 = (x1_2 * H).int()
    y1_1 = (y1_1 * W).int()
    y1_2 = (y1_2 * W).int()
    z1_1 = (z1_1 * D).int()
    z1_2 = (z1_2 * D).int()
    x2_1 = (x2_1 * H).int()
    x2_2 = (x2_2 * H).int()
    y2_1 = (y2_1 * W).int()
    y2_2 = (y2_2 * W).int()
    z2_1 = (z2_1 * D).int()
    z2_2 = (z2_2 * D).int()

    # Align
    device = pred1.device
    roi_pred1 = torch.zeros(size=pred1.shape).to(device)
    roi_pred2 = torch.zeros(size=pred2.shape).to(device)
    roi_gt1 = torch.zeros(size=gt1.shape).to(device)
    roi_gt2 = torch.zeros(size=gt2.shape).to(device)
    for b_idx in range(B):
        roi_pred1[b_idx] = F.interpolate(pred1[b_idx, :, x1_1[b_idx]:x1_2[b_idx], y1_1[b_idx]:y1_2[b_idx], z1_1[b_idx]:z1_2[b_idx]].unsqueeze(0), size=(H,W,D))
        roi_pred2[b_idx] = F.interpolate(pred2[b_idx, :, x2_1[b_idx]:x2_2[b_idx], y2_1[b_idx]:y2_2[b_idx], z2_1[b_idx]:z2_2[b_idx]].unsqueeze(0), size=(H,W,D))
        roi_gt1[b_idx] = F.interpolate(gt1[b_idx, :, x1_1[b_idx]:x1_2[b_idx], y1_1[b_idx]:y1_2[b_idx], z1_1[b_idx]:z1_2[b_idx]].unsqueeze(0), size=(H,W,D))
        roi_gt2[b_idx] = F.interpolate(gt2[b_idx, :, x2_1[b_idx]:x2_2[b_idx], y2_1[b_idx]:y2_2[b_idx], z2_1[b_idx]:z2_2[b_idx]].unsqueeze(0), size=(H,W,D))

    return roi_pred1, roi_pred2, roi_gt1, roi_gt2