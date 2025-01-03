"""
Training code for C2L

"""
from __future__ import print_function

import io
import sys
import time
import math
import random
import PIL
import numpy as np
import seaborn as sns
from matplotlib import pyplot as plt


import torch
import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.nn.functional as f
import torchvision.transforms.functional as t

from models import PCRLv23d, Cluster3d, ClusterPatch3d, TraceWrapper
from tools import adjust_learning_rate, AverageMeter, sinkhorn, ce_loss, swav_loss, roi_align_intersect


def Normalize(x):
    norm_x = x.pow(2).sum(1, keepdim=True).pow(1. / 2.)
    x = x.div(norm_x)
    return x


def moment_update(model, model_ema, m):
    """ model_ema = m * model_ema + (1 - m) model """
    for p1, p2 in zip(model.parameters(), model_ema.parameters()):
        p2.data.mul_(m).add_(1 - m, p1.detach().data)


def cos_loss(cosine, output1, output2):
    index = random.randint(0, len(output1) - 1)  # Because we select a feature map from a random scale
    sample1 = output1[index]
    sample2 = output2[index]
    loss = -(cosine(sample1[1], sample2[0].detach()).mean() + cosine(sample2[1],
                                                                     sample1[0].detach()).mean()) * 0.5
    return loss, index


def train_3d(args, data_loader, run_dir, writer=None):

    if 'cluster' in args.model:
        # Generate colors for cluster masks
        palette = sns.color_palette(palette='bright', n_colors=args.k)
        colors = torch.Tensor([list(color) for color in palette])
        if args.cpu:
            colors = colors.cpu()
        else:
            colors = colors.cuda()

    torch.backends.cudnn.deterministic = True

    train_loader = data_loader['train']
    val_loader = data_loader['eval']

    # Create model and optimizer
    if args.model == 'pcrlv2':
        model = PCRLv23d(skip_conn=args.skip_conn)
    elif args.model == 'cluster':
        model = Cluster3d(n_clusters=args.k, seed=args.seed, skip_conn=args.skip_conn)
    elif args.model == 'cluster_patch':
        model = ClusterPatch3d(n_clusters=args.k, seed=args.seed, skip_conn=args.skip_conn)
    if not args.cpu:
        model = model.cuda()

    optimizer = torch.optim.SGD(model.parameters(),
                                lr=args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)
    model = nn.DataParallel(model)

    if args.model == 'pcrlv2':
        criterion = nn.MSELoss()
        cosine = nn.CosineSimilarity()
        if not args.cpu:
            criterion = criterion.cuda()
            cosine = cosine.cuda()

    grid_pred = []  # Grid for visualizing predictions at each epoch

    for epoch in range(0, args.epochs + 1):

        # TRAINING

        adjust_learning_rate(epoch, args, optimizer)
        print("==> Training...")

        time1 = time.time()

        if args.model == 'pcrlv2':
            _, _, total_loss, writer = train_pcrlv2_inner(args, epoch, train_loader, model, optimizer, criterion, cosine, writer)
        elif args.model == 'cluster':
            _, _, total_loss, writer = train_cluster_inner(args, epoch, train_loader, model, optimizer, writer, colors)
        elif args.model == 'cluster_patch':
            _, _, total_loss, writer = train_cluster_patch_inner(args, epoch, train_loader, model, optimizer, writer, colors)

        time2 = time.time()
        print('epoch {}, total time {:.2f}'.format(epoch, time2 - time1))

        if args.tensorboard:
            writer.add_scalar('loss/train', total_loss, epoch)  # Write train loss on tensorboard


        # VALIDATION (only for clustering task, just for visualization purposes)

        N = 8  # Grid row/col size
        n_epochs = min(N,args.epochs) # The number of epochs to sample for the grid (N or all epochs if total less than N)
        step_epochs =  args.epochs // n_epochs # Every how many epochs to sample
        
        if args.vis and (epoch % step_epochs == 0) and (epoch / step_epochs) <= n_epochs and 'cluster' in args.model:  

            print("==> Validating...")
            
            # Validate
            if args.model == 'cluster':
                grid_pred.extend(val_cluster_inner(args, epoch, val_loader, model, colors, N))  # Array of a row for each scale to add to the grid of each scale
            elif args.model == 'cluster_patch':
                grid_pred.extend(val_cluster_patch_inner(args, epoch, val_loader, model, colors, N))  # Array of a row for each scale to add to the grid of each scale
            
            n_cols = min(N,args.b)
            n_rows = len(grid_pred) // n_cols
            
            # Plot grid of predictions for sampled epochs up to now
            fig, axes = plt.subplots(n_rows, n_cols, figsize=(15, 15*(n_rows/n_cols)))
            for i, ax in enumerate(axes.flat):
                ax.imshow(grid_pred[i]) 
                ax.axis('off')  # Turn off axis labels
                if i % n_cols:
                    ax.set_ylabel(f'Epoch {epoch}', rotation=0, size='large')
            plt.tight_layout()  # Adjust spacing between subplots
            # Save grid to buffer and then log on tensorboard
            buf = io.BytesIO()
            plt.savefig(buf, format='png', bbox_inches='tight', dpi=100)
            buf.seek(0)
            grid = PIL.Image.open(buf)
            grid = t.pil_to_tensor(grid)
            writer.add_image(f'img/val/grid', img_tensor=grid, global_step=epoch)

        # Save model
        if epoch % 100 == 0 or epoch == 240:
            print('==> Saving...')
            state = {'opt': args, 'state_dict': model.module.state_dict(),
                     'optimizer': optimizer.state_dict(), 'epoch': epoch}
            save_file = run_dir + '.pt'
            torch.save(state, save_file)

            # Help release GPU memory
            del state

        if not args.cpu:
            torch.cuda.empty_cache()

        
def train_pcrlv2_inner(args, epoch, train_loader, model, optimizer, criterion, cosine, writer):
    """
    one epoch training for instance discrimination
    """
    
    model.train()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    loss_meter = AverageMeter()
    mg_loss_meter = AverageMeter()
    prob_meter = AverageMeter()
    total_loss_meter = AverageMeter()

    end = time.time()
    for idx, (input1, input2, gt1, gt2, _, _, local_views, _) in enumerate(train_loader):

        B, C, H, W, D = input1.shape

        data_time.update(time.time() - end)

        bsz = input1.size(0)
        x1 = input1.float()  # Crop 1
        x2 = input2.float()  # Crop 2

        if not args.cpu:
            x1 = x1.cuda()
            x2 = x2.cuda()
            gt1 = gt1.cuda()
            gt2 = gt2.cuda()

        # Get predictions
        mask1, decoder_outputs1, middle_masks1 = model(x1)
        _, decoder_outputs2, _ = model(x2)

        loss2, index2 = cos_loss(cosine, decoder_outputs1, decoder_outputs2)
        local_loss = 0.0

        local_input = torch.cat(local_views, dim=0)  # 6 * bsz, 3, d, 96, 96
        _, local_views_outputs, _ = model(local_input, local=True)  # 4 * 2 * [6 * bsz, 3, d, 96, 96]
        local_views_outputs = [torch.stack(t) for t in local_views_outputs]
        
        for i in range(len(local_views)):
            local_views_outputs_tmp = [t[:, bsz * i: bsz * (i + 1)] for t in local_views_outputs]
            loss_local_1, _ = cos_loss(cosine, decoder_outputs1, local_views_outputs_tmp)
            loss_local_2, _ = cos_loss(cosine, decoder_outputs2, local_views_outputs_tmp)
            local_loss += loss_local_1
            local_loss += loss_local_2
        local_loss = local_loss / (2 * len(local_views))
        loss1 = criterion(mask1, gt1)
        beta = 0.5 * (1. + math.cos(math.pi * epoch / 240))
        loss4 = beta * criterion(middle_masks1[index2], gt1)  
        
        # Total Loss
        loss = loss1 + loss2 + loss4 + local_loss

        # Backward
        if loss > 1000 and epoch > 10:
            print('skip the step')
            continue
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # Meters
        mg_loss_meter.update(loss1.item(), bsz)
        loss_meter.update(loss2.item(), bsz)
        prob_meter.update(local_loss, bsz)
        total_loss_meter.update(loss, bsz)
        if not args.cpu:
            torch.cuda.synchronize()
        batch_time.update(time.time() - end)
        end = time.time()

        # if args.tensorboard:
        #     if epoch == 0:  # Only on the first iteration, write model graph on tensorboard
        #         model_wrapper = TraceWrapper(model)
        #         writer.add_graph(model_wrapper, x1)

        # Print info
        if (idx + 1) % 10 == 0:
            print('Train: [{0}][{1}/{2}]\t'
                  'BT {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'DT {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'cos_loss {c2l_loss.val:.3f} ({c2l_loss.avg:.3f})\t'
                  'mg loss {mg_loss.val:.3f} ({mg_loss.avg:.3f})\t'
                  'local loss {prob.val:.3f} ({prob.avg:.3f})'.format(
                epoch, idx + 1, len(train_loader), batch_time=batch_time,
                data_time=data_time, c2l_loss=loss_meter, mg_loss=mg_loss_meter, prob=prob_meter))
            sys.stdout.flush()

    return mg_loss_meter.avg, prob_meter.avg, total_loss_meter.avg, writer


def train_pcrlv2_3d(args, data_loader, run_dir, writer=None):
    train_3d(args, data_loader, run_dir, writer=writer)


def train_cluster_inner(args, epoch, train_loader, model, optimizer, writer, colors):

    model.train()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    loss_meter = AverageMeter()
    mg_loss_meter = AverageMeter()
    prob_meter = AverageMeter()
    total_loss_meter = AverageMeter()

    end = time.time()
    for idx, (input1, input2, gt1, gt2, crop1_coords, crop2_coords, _, _) in enumerate(train_loader):

        B, C, H, W, D = input1.shape

        data_time.update(time.time() - end)

        x1 = input1.float()  # Crop 1
        x2 = input2.float()  # Crop 2

        if not args.cpu:
            x1 = x1.cuda()
            x2 = x2.cuda()
            gt1 = gt1.cuda()
            gt2 = gt2.cuda()
            crop1_coords = crop1_coords.cuda()
            crop2_coords = crop2_coords.cuda()

        pred1 = model.module(x1)  # Get cluster predictions
        pred1 = pred1.softmax(2)  # Convert to probabilities
        gt1 = f.one_hot(gt1.long(), num_classes=args.k).permute(0,4,1,2,3)  # B x H x W x D -> B x H x W x D x K -> B x K x H x W x D

        # Do everything again for the other crop if using swav loss
        if args.cluster_loss == 'swav':
            pred2 = model.module(x2)
            pred2 = pred2.softmax(2)
            gt2 = f.one_hot(gt2.long(), num_classes=args.k).permute(0,4,1,2,3)
            # ROI-align crop intersection with cluster assignment intersection
            roi_pred1, roi_pred2, roi_gt1, roi_gt2 = roi_align_intersect(pred1, pred2, gt1, gt2, crop1_coords, crop2_coords)

        # Clustering Loss
        if args.cluster_loss == 'ce':
            cluster_loss = ce_loss(gt1, pred1)
        elif args.cluster_loss == 'swav':
            cluster_loss = swav_loss(roi_gt1, roi_gt2, roi_pred1, roi_pred2)

        # Plot predictions on tensorboard
        with torch.no_grad():
            b_idx = 0
            if args.vis and idx==b_idx: #and epoch % 10 == 0:

                # Select 2D images
                img_idx = 0
                m_idx = 0
                s_idx = D//2
                in1 = x1[img_idx,m_idx,:,:,s_idx].unsqueeze(0)
                pred1 = pred1[img_idx,:,:,:,s_idx].argmax(dim=0).unsqueeze(0)  # Take only hard cluster assignment (argmax)
                gt1 = gt1[img_idx,:,:,:,s_idx].argmax(dim=0).unsqueeze(0)
                # Min-max norm input images
                in1 = (in1 - in1.min())/(in1.max() - in1.min())
                # Give color to each cluster in cluster masks
                pred1 = pred1.repeat((3,1,1)).permute(1,2,0).float()  # Convert to RGB and move channel dim to the end
                gt1 = gt1.repeat((3,1,1)).permute(1,2,0).float()
                for c in range(colors.shape[0]):
                    pred1[pred1[:,:,0] == c] = colors[c]
                    gt1[gt1[:,:,0] == c] = colors[c]
                pred1 = pred1.permute(2,0,1)
                gt1 = gt1.permute(2,0,1)
                if args.cluster_loss == 'ce':
                    in_img = in1
                    pred_img = pred1
                    gt_img = gt1

                # Do everything again for the other crop if using swav loss
                if args.cluster_loss == 'swav':
                    in2 = x2[img_idx,m_idx,:,:,s_idx].unsqueeze(0)
                    pred2 = pred2[img_idx,:,:,:,s_idx].argmax(dim=0).unsqueeze(0)
                    gt2 = gt2[img_idx,:,:,:,s_idx].argmax(dim=0).unsqueeze(0)
                    in2 = (in2 - in2.min())/(in2.max() - in2.min())
                    pred2 = pred2.repeat((3,1,1)).permute(1,2,0).float()
                    gt2 = gt2.repeat((3,1,1)).permute(1,2,0).float()
                    for c in range(colors.shape[0]):
                        pred2[pred2[:,:,0] == c] = colors[c]
                        gt2[gt2[:,:,0] == c] = colors[c]
                    pred2 = pred2.permute(2,0,1)
                    gt2 = gt2.permute(2,0,1)
                    # Pad images for better visualization                
                    in1 = f.pad(in1.unsqueeze(0),(2,1,2,2),value=1)
                    in2 = f.pad(in2.unsqueeze(0),(1,2,2,2),value=1)
                    pred1 = f.pad(pred1.unsqueeze(0),(2,1,2,2),value=1)
                    pred2 = f.pad(pred2.unsqueeze(0),(1,2,2,2),value=1)
                    gt1 = f.pad(gt1.unsqueeze(0),(2,1,2,2),value=1)
                    gt2 = f.pad(gt2.unsqueeze(0),(1,2,2,2),value=1)
                    # Combine crops
                    in_img = torch.cat((in1,in2),dim=3).squeeze(0)
                    pred_img = torch.cat((pred1,pred2),dim=3).squeeze(0)
                    gt_img = torch.cat((gt1,gt2),dim=3).squeeze(0)

                # Save in tensorboard
                in_img_name = 'img/train/raw' 
                pred_img_name = f'img/train/pred'
                gt_img_name = f'img/train/gt'

                writer.add_image(in_img_name, img_tensor=in_img.cpu().detach().numpy(), global_step=epoch, dataformats='CHW')
                writer.add_image(pred_img_name, img_tensor=pred_img.cpu().detach().numpy(), global_step=epoch, dataformats='CHW')   
                writer.add_image(gt_img_name, img_tensor=gt_img.cpu().detach().numpy(), global_step=epoch, dataformats='CHW')

        # TODO: add the other losses later
        loss1 = cluster_loss
        loss2 = torch.tensor(0)
        loss4 = 0
        local_loss = 0

        # Total Loss
        # TODO: add the other losses later
        loss = loss1

        # Backward
        if loss > 1000 and epoch > 10:
            print('skip the step')
            continue
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # Meters
        mg_loss_meter.update(loss1.item(), B)
        loss_meter.update(loss2.item(), B)
        prob_meter.update(local_loss, B)
        total_loss_meter.update(loss, B)
        if not args.cpu:
            torch.cuda.synchronize()
        batch_time.update(time.time() - end)
        end = time.time()

        # if args.tensorboard:
        #     if epoch == 0:  # Only on the first iteration, write model graph on tensorboard
        #         model_wrapper = TraceWrapper(model)
        #         writer.add_graph(model_wrapper, x1)

        # print info
        if (idx + 1) % 10 == 0:
            print('Train: [{0}][{1}/{2}]\t'
                  'BT {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'DT {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'cos_loss {c2l_loss.val:.3f} ({c2l_loss.avg:.3f})\t'
                  'mg loss {mg_loss.val:.3f} ({mg_loss.avg:.3f})\t'
                  'local loss {prob.val:.3f} ({prob.avg:.3f})'.format(
                epoch, idx + 1, len(train_loader), batch_time=batch_time,
                data_time=data_time, c2l_loss=loss_meter, mg_loss=mg_loss_meter, prob=prob_meter))
            sys.stdout.flush()

    return mg_loss_meter.avg, prob_meter.avg, total_loss_meter.avg, writer


def val_cluster_inner(args, epoch, val_loader, model, colors, N):

    with torch.no_grad():

        model.eval()

        for idx, (image, _, _, _, _, _, _, _) in enumerate(val_loader):
            
            if idx != 0:
                continue  # Validate only batch 0

            B, _, H, W, D = image.shape
            K = args.k

            # Keep only modality 0
            image = image[:,0:1,:,:,:]

            x = image.float()

            if not args.cpu:
                x = x.cuda()

            # Get embeddings and predictions
            pred = model(x)

            grid_pred = []  # Contains the grid predictions of a specific scale

            # Convert to probabilities
            pred = pred.softmax(2)

            # Gather predictions to visualize on grid
            n_images = min(N,args.b)  # The number of images to sample from for the grid (N or all images if total less than N)
            if epoch == 0:  # If epoch 0, add the input images as the first row of the grid
                for img_idx in range(n_images):
                    x_i = x[img_idx,0,:,:,D//2]                   
                    x_i = (x_i - x_i.min())/(x_i.max() - x_i.min())  # Min-max norm input images
                    x_i = x_i.repeat((3,1,1)).permute(1,2,0)  # Convert to RGB and move channel dim to the end
                    x_i = x_i.cpu().detach()
                    grid_pred.append(x_i)
            else:
                for img_idx in range(n_images): # If next epochs, add the predictions for each image at the current epoch as the next row
                    pred_i = pred[img_idx,:,:,:,pred.shape[-1]//2].argmax(dim=0).unsqueeze(0)  # Take only hard cluster assignment (argmax)
                    pred_i = pred_i.repeat((3,1,1)).permute(1,2,0).float()  # Convert to RGB and move channel dim to the end
                    for c in range(colors.shape[0]):  # Give color to each cluster in cluster masks
                        pred_i[pred_i[:,:,0] == c] = colors[c]
                    pred_i = pred_i.cpu().detach()
                    grid_pred.append(pred_i)
                
    return grid_pred


def train_cluster_3d(args, data_loader, run_dir, writer=None):
    train_3d(args, data_loader, run_dir, writer=writer)


def train_cluster_patch_inner(args, epoch, train_loader, model, optimizer, writer, colors):

    model.train()

    batch_time = AverageMeter()
    data_time = AverageMeter()
    loss_meter = AverageMeter()
    mg_loss_meter = AverageMeter()
    prob_meter = AverageMeter()
    total_loss_meter = AverageMeter()

    end = time.time()
    for idx, (input1, input2, gt1, gt2, crop1_coords, crop2_coords, _, _) in enumerate(train_loader):

        B, C, H, W, D = input1.shape

        data_time.update(time.time() - end)

        x1 = input1.float()  # Crop 1
        x2 = input2.float()  # Crop 2

        if not args.cpu:
            x1 = x1.cuda()
            x2 = x2.cuda()
            gt1 = gt1.cuda()
            gt2 = gt2.cuda()
            crop1_coords = crop1_coords.cuda()
            crop2_coords = crop2_coords.cuda()

        # Get embeddings and predictions
        emb1, pred1 = model(x1)
        emb2, pred2 = model(x2)

        # Normalize D dimension (BxNxD)
        emb1 = nn.functional.normalize(emb1, dim=2, p=2)  
        emb2 = nn.functional.normalize(emb2, dim=2, p=2) 

        # Get ground truths (teacher predictions)
        with torch.no_grad():
            # Get prototypes and normalize
            proto = model.module.prototypes.weight.data.clone()
            proto = nn.functional.normalize(proto, dim=1, p=2)  # Normalize D dimension (KxD)

            # Embedding to prototype similarity matrix
            cos_sim1 = torch.matmul(emb1, proto.t())  # BxNxK
            cos_sim2 = torch.matmul(emb2, proto.t())

            N = cos_sim1.shape[1]  # Number of patches
            P = model.module.patch_size  # Patch size
            HP, WP, DP = (H//P, W//P, D//P)  # Grid of patches dims
            K = model.module.proto_num  # Clusters

            # Flatten batch and patch num dimensions of similarity matrices (BxNxK -> B*NxK), and transpose matrices (KxB*N) for sinkhorn algorithm
            flat_cos_sim1 = cos_sim1.reshape(B*N,K).T
            flat_cos_sim2 = cos_sim2.reshape(B*N,K).T

            # Standardize for numerical stability (maybe?)
            eps = 0.05
            flat_cos_sim1 = torch.exp(flat_cos_sim1 / eps)
            flat_cos_sim2 = torch.exp(flat_cos_sim2 / eps)

            # Teacher cluster assignments
            gt1 = sinkhorn(args, Q=flat_cos_sim1, nmb_iters=3).T.reshape((B,N,K))  # Also restore patch num dimension
            gt2 = sinkhorn(args, Q=flat_cos_sim2, nmb_iters=3).T.reshape((B,N,K))

            # Apply temperature
            temp = 1
            gt1 = gt1 / temp
            gt2 = gt2 / temp

        # Convert to probabilities
        pred1 = pred1.softmax(2)
        pred2 = pred2.softmax(2)

        # Convert prediction and ground truth to (soft) cluster masks (restore spatial position of pooled image)
        pred1 = pred1.permute(0,2,1).reshape((B,K,HP,WP,DP))
        pred2 = pred2.permute(0,2,1).reshape((B,K,HP,WP,DP))
        gt1 = gt1.permute(0,2,1).reshape((B,K,HP,WP,DP))
        gt2 = gt2.permute(0,2,1).reshape((B,K,HP,WP,DP))

        # ROI-align crop intersection with cluster assignment intersection
        roi_pred1, roi_pred2, roi_gt1, roi_gt2 = roi_align_intersect(pred1, pred2, gt1, gt2, crop1_coords, crop2_coords)

        # SwAV Loss for current scale
        cluster_loss = swav_loss(roi_gt1, roi_gt2, roi_pred1, roi_pred2)

        # Plot predictions on tensorboard
        with torch.no_grad():
            b_idx = 0
            if args.vis and idx==b_idx: #and epoch % 10 == 0:

                # Select 2D images
                img_idx = 0
                m_idx = 0
                in1 = x1[img_idx,m_idx,:,:,input1.size(-1)//2].unsqueeze(0)
                in2 = x2[img_idx,m_idx,:,:,input2.size(-1)//2].unsqueeze(0)
                pred1 = pred1[img_idx,:,:,:,pred1.size(-1)//2].argmax(dim=0).unsqueeze(0)  # Take only hard cluster assignment (argmax)
                pred2 = pred2[img_idx,:,:,:,pred2.size(-1)//2].argmax(dim=0).unsqueeze(0)
                gt1 = gt1[img_idx,:,:,:,gt1.size(-1)//2].argmax(dim=0).unsqueeze(0)
                gt2 = gt2[img_idx,:,:,:,gt2.size(-1)//2].argmax(dim=0).unsqueeze(0)

                # Min-max norm input images
                in1 = (in1 - in1.min())/(in1.max() - in1.min())
                in2 = (in2 - in2.min())/(in2.max() - in2.min())

                # Interpolate cluster masks to original input shape
                pred1 = f.interpolate(pred1.float().unsqueeze(0), size=(H,W)).squeeze(0)
                pred2 = f.interpolate(pred2.float().unsqueeze(0), size=(H,W)).squeeze(0)
                gt1 = f.interpolate(gt1.float().unsqueeze(0), size=(H,W)).squeeze(0)
                gt2 = f.interpolate(gt2.float().unsqueeze(0), size=(H,W)).squeeze(0)

                # Give color to each cluster in cluster masks
                pred1 = pred1.repeat((3,1,1)).permute(1,2,0)  # Convert to RGB and move channel dim to the end
                pred2 = pred2.repeat((3,1,1)).permute(1,2,0)
                gt1 = gt1.repeat((3,1,1)).permute(1,2,0)
                gt2 = gt2.repeat((3,1,1)).permute(1,2,0)
                for c in range(colors.shape[0]):
                    pred1[pred1[:,:,0] == c] = colors[c]
                    pred2[pred2[:,:,0] == c] = colors[c]
                    gt1[gt1[:,:,0] == c] = colors[c]
                    gt2[gt2[:,:,0] == c] = colors[c]
                pred1 = pred1.permute(2,0,1)
                pred2 = pred2.permute(2,0,1)
                gt1 = gt1.permute(2,0,1)
                gt2 = gt2.permute(2,0,1)

                # Pad images for better visualization                
                in1 = f.pad(in1.unsqueeze(0),(2,1,2,2),value=1)
                in2 = f.pad(in2.unsqueeze(0),(1,2,2,2),value=1)
                pred1 = f.pad(pred1.unsqueeze(0),(2,1,2,2),value=1)
                pred2 = f.pad(pred2.unsqueeze(0),(1,2,2,2),value=1)
                gt1 = f.pad(gt1.unsqueeze(0),(2,1,2,2),value=1)
                gt2 = f.pad(gt2.unsqueeze(0),(1,2,2,2),value=1)

                # Combine crops and save in tensorboard
                in_img = torch.cat((in1,in2),dim=3).squeeze(0)
                pred_img = torch.cat((pred1,pred2),dim=3).squeeze(0)
                gt_img = torch.cat((gt1,gt2),dim=3).squeeze(0).cpu()

                in_img_name = 'img/train/raw' 
                pred_img_name = f'img/train/pred'
                gt_img_name = f'img/train/gt'

                writer.add_image(in_img_name, img_tensor=in_img.cpu().detach().numpy(), global_step=epoch, dataformats='CHW')
                writer.add_image(pred_img_name, img_tensor=pred_img.cpu().detach().numpy(), global_step=epoch, dataformats='CHW')   
                writer.add_image(gt_img_name, img_tensor=gt_img.cpu().detach().numpy(), global_step=epoch, dataformats='CHW')

        # TODO: add the other losses later
        loss1 = cluster_loss
        loss2 = torch.tensor(0)
        loss4 = 0
        local_loss = 0

        # Total Loss
        # TODO: add the other losses later
        loss = loss1

        # Backward
        if loss > 1000 and epoch > 10:
            print('skip the step')
            continue
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # Meters
        mg_loss_meter.update(loss1.item(), B)
        loss_meter.update(loss2.item(), B)
        prob_meter.update(local_loss, B)
        total_loss_meter.update(loss, B)
        if not args.cpu:
            torch.cuda.synchronize()
        batch_time.update(time.time() - end)
        end = time.time()

        # if args.tensorboard:
        #     if epoch == 0:  # Only on the first iteration, write model graph on tensorboard
        #         model_wrapper = TraceWrapper(model)
        #         writer.add_graph(model_wrapper, x1)

        # print info
        if (idx + 1) % 10 == 0:
            print('Train: [{0}][{1}/{2}]\t'
                  'BT {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'DT {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'cos_loss {c2l_loss.val:.3f} ({c2l_loss.avg:.3f})\t'
                  'mg loss {mg_loss.val:.3f} ({mg_loss.avg:.3f})\t'
                  'local loss {prob.val:.3f} ({prob.avg:.3f})'.format(
                epoch, idx + 1, len(train_loader), batch_time=batch_time,
                data_time=data_time, c2l_loss=loss_meter, mg_loss=mg_loss_meter, prob=prob_meter))
            sys.stdout.flush()

    return mg_loss_meter.avg, prob_meter.avg, total_loss_meter.avg, writer


def val_cluster_patch_inner(args, epoch, val_loader, model, colors, N):

    with torch.no_grad():

        model.eval()

        for idx, (image, _, _, _, _, _, _, _) in enumerate(val_loader):
            
            if idx != 0:
                continue  # Validate only batch 0

            B, _, H, W, D = image.shape
            K = args.k

            # Keep only modality 0
            image = image[:,0:1,:,:,:]

            x = image.float()

            if not args.cpu:
                x = x.cuda()

            # Get embeddings and predictions
            _, pred = model(x)

            grid_pred = []  # Contains the grid predictions of a specific scale

            P = model.module.patch_size

            # Convert to probabilities
            pred = pred.softmax(2)

            # Convert prediction to (soft) cluster masks (restore spatial position of pooled image)
            HP = H//P  # Num patches at X
            WP = W//P  # Num patches at Y
            DP = D//P  # Num patches at Z
            pred = pred.permute(0,2,1).reshape((B,K,HP,WP,DP))

            # Gather predictions to visualize on grid
            n_images = min(N,args.b)  # The number of images to sample from for the grid (N or all images if total less than N)
            if epoch == 0:  # If epoch 0, add the input images as the first row of the grid
                for img_idx in range(n_images):
                    x_i = x[img_idx,0,:,:,D//2]                   
                    x_i = (x_i - x_i.min())/(x_i.max() - x_i.min())  # Min-max norm input images
                    x_i = x_i.repeat((3,1,1)).permute(1,2,0)  # Convert to RGB and move channel dim to the end
                    x_i = x_i.cpu().detach()
                    grid_pred.append(x_i)
            else:
                for img_idx in range(n_images): # If next epochs, add the predictions for each image at the current epoch as the next row
                    pred_i = pred[img_idx,:,:,:,DP//2].argmax(dim=0).unsqueeze(0)  # Take only hard cluster assignment (argmax)
                    pred_i = f.interpolate(pred_i.float().unsqueeze(0), size=(H,W)).squeeze(0)
                    pred_i = pred_i.repeat((3,1,1)).permute(1,2,0).float()  # Convert to RGB and move channel dim to the end
                    for c in range(colors.shape[0]):  # Give color to each cluster in cluster masks
                        pred_i[pred_i[:,:,0] == c] = colors[c]
                    pred_i = pred_i.cpu().detach()
                    grid_pred.append(pred_i)
                
    return grid_pred

def train_cluster_patch_3d(args, data_loader, run_dir, writer=None):
    train_3d(args, data_loader, run_dir, writer=writer)