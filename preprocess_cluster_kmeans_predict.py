import argparse
import os
import warnings
import sys
import time
import math
import numpy as np
import random
import seaborn as sns
from tqdm import tqdm

import torch
import torch.nn as nn
import torch.nn.functional as f
from torch import autocast

from faiss import Kmeans
from sklearn.decomposition import PCA

from data import DataGenerator
from tools import set_seed


warnings.filterwarnings('ignore')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Self Training benchmark')
    parser.add_argument('--data', metavar='DIR', default='/home/igogou/data/LUNA16',
                        help='Path to dataset')
    parser.add_argument('--centroids', default=None, type=float, help='File containing pre-trained k-means centroids')
    parser.add_argument('--ratio', default=1, type=float, help='Ratio of data used for pretraining/finetuning.')
    parser.add_argument('--model', default='cluster', choices=['cluster'], help='Choose the model')
    parser.add_argument('--b', default=16, type=int, help='Batch size')
    parser.add_argument('--n', default='luna', choices=['luna', 'lidc', 'brats', 'lits'], type=str, help='Dataset to use')
    parser.add_argument('--workers', default=4, type=int, help='Num of workers')
    parser.add_argument('--gpus', default='0,1,2,3', type=str, help='GPU indices to use')
    parser.add_argument('--seed', default=1, type=int)
    parser.add_argument('--k', default=10, type=int, help='Number of clusters for clustering pretask')
    parser.add_argument('--cpu', action='store_true', default=False, help='To run on CPU or not')
    args = parser.parse_args()
    if not os.path.exists(args.output):
        os.makedirs(args.output)
    print(args)
    print()

    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpus

    # Set seed
    set_seed(args.seed)
    print(f'Seed is {args.seed}\n')

    # Force arguments
    args.model = 'cluster'
    args.ratio = 1

    # Generate colors for cluster masks
    palette = sns.color_palette(palette='bright', n_colors=args.k)
    colors = torch.Tensor([list(color) for color in palette]).cpu()  # cpu because we apply it on a detached tensor later

    # Get dataloader
    generator = DataGenerator(args)
    loader_name = 'cluster_' + args.n + '_pretask'
    train_loader = getattr(generator, loader_name)(load_gt=False)['train']
    train_loader.shuffle = False
    
    # Get models
    featup = nn.DataParallel(torch.hub.load("mhamilton723/FeatUp", 'dino16', use_norm=False))
    if not args.cpu:
        featup = featup.cuda()
    else:
        featup = featup.cpu()
    kmeans = Kmeans(d=384, k=args.k, seed=args.seed, verbose=False, gpu=True if not args.cpu else False)

    featup.eval()



    # Predict with K-Means --------------------------------------------------------------------------------------------------------------
    print('Predict clusters with K-Means...')

    with tqdm(total=len(train_loader)) as tqdm_progress:
        for idx, (input1, input2, _, _, _, _, _, index) in enumerate(train_loader):

            input1 = input1.float()
            input2 = input2.float()

            if not args.cpu:
                input1 = input1.cuda()
                input2 = input2.cuda()

            # Convert 3D input to 2D
            B, C, H, W, D = input1.shape
            x1 = input1.permute(0,4,1,2,3).reshape(B*D,C,H,W)  # B x C x H x W x D -> B*D x C x H x W
            x2 = input2.permute(0,4,1,2,3).reshape(B*D,C,H,W)

            # device_type = 'cpu' if args.cpu else 'cuda'
            # with autocast(device_type=device_type):  # Run in mixed-precision

            with torch.no_grad():
                
                # Get upsampled features from teacher DINO ViT16 encoder and flatten spatial dimensions to get feature vectors for each pixel
                # B*D x 1 x H x W -(RGB)->  B*D x 3 x H x W -(Featup)-> B*D x C' x H x W -(Vectorize)-> B*D*H*W x C' 
                feat_vec1 = torch.zeros((B*D,384,H,W))
                feat_vec2 = torch.zeros((B*D,384,H,W))
                for b_idx in range(B*D):
                    feat_vec1[b_idx] = featup.module(x1[b_idx].unsqueeze(0).repeat(1,3,1,1))
                    feat_vec2[b_idx] = featup.module(x2[b_idx].unsqueeze(0).repeat(1,3,1,1))
                feat_vec1 = feat_vec1.permute(0,2,3,1).flatten(0,2)
                feat_vec2 = feat_vec2.permute(0,2,3,1).flatten(0,2)

                # Prepare data
                K = args.k
                N, E = feat_vec1.shape  # Number of points, Feature vector size

                gt_vec1 = np.zeros((N, 1))
                gt_vec2 = np.zeros((N, 1))
                _, gt_vec1 = kmeans.index.search(feat_vec1.cpu().numpy(), K)
                _, gt_vec2 = kmeans.index.search(feat_vec2.cpu().numpy(), K)

                gt_vec1 = torch.from_numpy(gt_vec1).to(torch.int64)
                gt_vec2 = torch.from_numpy(gt_vec2).to(torch.int64)
                if not args.cpu:
                    gt_vec1 = gt_vec1.cuda()
                    gt_vec2 = gt_vec2.cuda()

                # Restore spatial dimensions
                gt1 = gt_vec1.reshape(B, D, H, W, K).permute(0,4,2,3,1)  # B*D*H*W x K -> B x D x H x W x K -> B x K x H x W x D
                gt2 = gt_vec2.reshape(B, D, H, W, K).permute(0,4,2,3,1)

                # # Save ground truth files
                # for batch_idx, real_idx in enumerate(index):
                #     img_path = train_loader.dataset.imgs[index]
                #     name, ext = os.path.splitext(img_path)
                #     gt_path = name + f"_gt_k{args.k}" + ext
                #     np.save(gt_path, torch.cat(gt1[batch_idx].unsqueeze(0),gt2[batch_idx].unsqueeze(0)).cpu().numpy())
                
                tqdm_progress.update(1)

                # Generate cluster colors using kmeans
                # clusters = kmeans.centroids
                # color_kmeans = Kmeans(d=384, k=3, seed=args.seed, verbose=False, gpu=True if not args.cpu else False)
                # color_kmeans.train(clusters)
                # _, colors = kmeans.index.search(clusters, 3, )


                # Select 2D images
                img_idx = 0
                m_idx = 0
                s_idx = D//2
                in1 = input1[img_idx,m_idx,:,:,s_idx].unsqueeze(0)
                in2 = input2[img_idx,m_idx,:,:,s_idx].unsqueeze(0)
                gt1 = gt1[img_idx,:,:,:,s_idx].argmax(dim=0).unsqueeze(0)
                gt2 = gt2[img_idx,:,:,:,s_idx].argmax(dim=0).unsqueeze(0)

                # Min-max norm input images
                in1 = (in1 - in1.min())/(in1.max() - in1.min())
                in2 = (in2 - in2.min())/(in2.max() - in2.min())

                # Send to cpu
                in1 = in1.cpu().detach()
                in2 = in2.cpu().detach()
                gt1 = gt1.cpu().detach()
                gt2 = gt2.cpu().detach()

                # Give color to each cluster in cluster masks
                gt1 = gt1.repeat((3,1,1)).permute(1,2,0).float()
                gt2 = gt2.repeat((3,1,1)).permute(1,2,0).float()
                for c in range(colors.shape[0]):
                    gt1[gt1[:,:,0] == c] = colors[c]
                    gt2[gt2[:,:,0] == c] = colors[c]
                gt1 = gt1.permute(2,0,1)
                gt2 = gt2.permute(2,0,1)

                # Pad images for better visualization
                in1 = f.pad(in1.unsqueeze(0),(2,1,2,2),value=1)
                in2 = f.pad(in2.unsqueeze(0),(1,2,2,2),value=1)                
                gt1 = f.pad(gt1.unsqueeze(0),(2,1,2,2),value=1)
                gt2 = f.pad(gt2.unsqueeze(0),(1,2,2,2),value=1)

                # Combine crops
                in_img = torch.cat((in1,in2),dim=3).squeeze(0).cpu().detach().numpy()
                gt_img = torch.cat((gt1,gt2),dim=3).squeeze(0).cpu().detach().numpy()

