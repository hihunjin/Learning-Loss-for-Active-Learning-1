'''Active Learning Procedure in PyTorch.

Reference:
[Yoo et al. 2019] Learning Loss for Active Learning (https://arxiv.org/abs/1905.03677)
'''

# Python
import os
import random

# Torch
import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader
import torch.optim.lr_scheduler as lr_scheduler
from torch.utils.data.sampler import SubsetRandomSampler
from torch.autograd import Variable

# Torchvison
import torchvision.transforms as T
import torchvision.models as models
from torchvision.datasets import CIFAR100, CIFAR10,MNIST,FashionMNIST,SVHN

# Utils
import numpy
import visdom
from tqdm import tqdm
import argparse
from utils import L2dist

# Custom
import models.resnet as resnet
import models.lossnet as lossnet
from config import *
from data.sampler import SubsetSequentialSampler

parser = argparse.ArgumentParser()
parser.add_argument('--lrl', action='store_true', default = False)  # AL pool
parser.add_argument('--query', type=int, default = 1000)
parser.add_argument('--epoch', type=int, default = 200)
parser.add_argument('--cycles', type=int, default = 10)
parser.add_argument('--subset', type=int, default = 10000)
parser.add_argument('--rule', type=str, default = "LL")
parser.add_argument('--trials', type=int, default = TRIALS)
parser.add_argument('--softmax', action='store_true', default = False)
parser.add_argument('--onehot', action='store_true', default = False)
parser.add_argument('--lamb2', type=float, default = 1.)
parser.add_argument('--data', type=str, default = "CIFAR10")
parser.add_argument('--lpl', action='store_true', default = False)
parser.add_argument('--lamb1', type=float, default = 1.)
parser.add_argument('--seed', action='store_true', default = False)
parser.add_argument('--lrlbatch', type=int, default = 128)
parser.add_argument('--savedata', action='store_true', default = False)
parser.add_argument('--printdata', action='store_true', default = False)
parser.add_argument('--nolog', action='store_true', default = False)

parser.add_argument('--triplet', action='store_true', default = False)
parser.add_argument('--tripletlog', action='store_true', default = False)
parser.add_argument('--tripletratio', action='store_true', default = False)
parser.add_argument('--numtriplet', type=int, default = 200)

args = parser.parse_args()
if args.triplet:
    args.nolog = True
if args.rule in ["lrlonly", "lrlonlywsoftmax"]:
    args.lrl = True
if args.rule in ["lrlonlywsoftmax", "lplwsoftmax"]:
    args.softmax = True
if args.rule in ["lpl", "lplwsoftmax"]:
    args.lrl = True
if args.softmax or args.onehot:
    args.lrl = True
if args.lrl:
    args.embed2embed = True
    args.is_norm = True
    args.no_square = False
    pdist = L2dist(2)
if args.rule == "Entropy":
    SUBSET = 39000
if args.seed == True:
    args.trials = 1
ADDENDUM = args.query
EPOCH = args.epoch
CYCLES = args.cycles
SUBSET = args.subset
TRIALS = args.trials
WEIGHT = args.lamb1
softmax = nn.Softmax(dim=1)
print(args)
##
# Data
if args.data == "CIFAR10":
    Nor = T.Normalize([0.4914, 0.4822, 0.4465], [0.2023, 0.1994, 0.2010])
elif args.data == "CIFAR100":
    Nor = T.Normalize((0.5071, 0.4867, 0.4408), (0.2675, 0.2565, 0.2761))
elif args.data == "SVHN":
    Nor = T.Normalize((0.4377, 0.4438, 0.4728), (0.1980, 0.2010, 0.1970))
elif args.data == "MNIST":
    Nor = T.Normalize((0.1307,), (0.3081,))
elif args.data == "FashionMNIST":
    Nor = T.Normalize((0.1307,), (0.3081,))
train_transform = T.Compose([
    T.RandomHorizontalFlip(),
    T.RandomCrop(size=32, padding=4),
    T.ToTensor(),
    Nor
])

test_transform = T.Compose([
    T.ToTensor(),
    Nor
])
if args.data == "CIFAR10":
    cifar10_train = CIFAR10('./cifar10', train=True, download=True, transform=train_transform)
    cifar10_unlabeled   = CIFAR10('./cifar10', train=True, download=True, transform=test_transform)
    cifar10_test  = CIFAR10('./cifar10', train=False, download=True, transform=test_transform)
elif args.data =="CIFAR100":
    cifar10_train = CIFAR100('./cifar100', train=True, download=True, transform=train_transform)
    cifar10_unlabeled   = CIFAR100('./cifar100', train=True, download=True, transform=test_transform)
    cifar10_test  = CIFAR100('./cifar100', train=False, download=True, transform=test_transform)
elif args.data == "SVHN":
    cifar10_train = SVHN('./SVHN', split='train', download=True, transform=train_transform)
    cifar10_unlabeled   = SVHN('./SVHN', split='train', download=True, transform=test_transform)
    cifar10_test  = SVHN('./SVHN', split='test', download=True, transform=test_transform)
elif args.data =="MNIST":
    cifar10_train = MNIST('./MNIST', train=True, download=True, transform=train_transform)
    cifar10_unlabeled   = MNIST('./MNIST', train=True, download=True, transform=test_transform)
    cifar10_test  = MNIST('./MNIST', train=False, download=True, transform=test_transform)
elif args.data == "FashionMNIST":
    cifar10_train = FashionMNIST('./FashionMNIST', train=True, download=True, transform=train_transform)
    cifar10_unlabeled   = FashionMNIST('./FashionMNIST', train=True, download=True, transform=test_transform)
    cifar10_test  = FashionMNIST('./FashionMNIST', train=False, download=True, transform=test_transform)

transform = T.Compose(
    [T.ToTensor(),
     ])
visual = CIFAR10('./cifar10', train=True, download=True, transform=transform)


##
# Loss Prediction Loss
def LossPredLoss(input, target, margin=1.0, reduction='mean'):
    assert len(input) % 2 == 0, 'the batch size is not even.'
    assert input.shape == input.flip(0).shape
    
    input = (input - input.flip(0))[:len(input)//2] # [l_1 - l_2B, l_2 - l_2B-1, ... , l_B - l_B+1], where batch_size = 2B
    target = (target - target.flip(0))[:len(target)//2]
    target = target.detach()

    one = 2 * torch.sign(torch.clamp(target, min=0)) - 1 # 1 operation which is defined by the authors
    
    if reduction == 'mean':
        loss = torch.sum(torch.clamp(margin - one * input, min=0))
        loss = loss / input.size(0) # Note that the size of input is already halved
    elif reduction == 'none':
        loss = torch.clamp(margin - one * input, min=0)
    else:
        NotImplementedError()
    
    return loss

def TripletLoss(input, label, margin=1.0):
    
    m = input.size()[0]-1
    a = input[0],label[0]
    p = input[1:]

#     losses = Variable(torch.Tensor([0]), requires_grad=True)
    losses = 0.
    diff = torch.abs(a[0]-p)
    out = torch.pow(diff,2).sum(1)
    out = torch.pow(out,1./2)
    if args.tripletlog:
        out = torch.log(out)
    
    n = 0
    for i in range(1,m):
        for j in range(i+1,m):
            if label[i] == a[1] and label[j] !=a[1]:
                distance_positive = out[i]
                distance_negative = out[j]
                if args.tripletratio:
                    losses += F.relu(1 - distance_negative / (distance_positive + 0.01))
                else:
                    losses += F.relu(distance_positive - distance_negative + margin)
                n+=1
            elif label[i] != a[1] and label[j] ==a[1]:
                distance_positive = out[j]
                distance_negative = out[i]
                if args.tripletratio:
                    losses += F.relu(1 - distance_negative / (distance_positive + 0.01))
                else:
                    losses += F.relu(distance_positive - distance_negative + margin)
                n+=1
            if n>args.numtriplet:
                break
        else:
            continue
        break

    if losses > 0:
        return losses/n
    else:
        return 0

def LogRatioLoss(input, value):
    m = input.size()[0]-1   # #paired
    a = input[0]            # anchor
    p = input[1:]           # paired

    if not args.embed2embed:
        eps = 1e-4# / value[0]
        diff = torch.abs(value[0] - value[1:])
        out = torch.pow(diff, 2)
        gt_dist = torch.pow(out + eps, 1. / 2)

        # auxiliary variables
        idxs = torch.arange(1, m+1).cuda()
        indc = idxs.repeat(m,1).t() < idxs.repeat(m,1)

        epsilon = 1e-6
        dist = pdist.forward(a,p)

        log_dist = torch.log(dist + epsilon)
        log_gt_dist = torch.log(gt_dist + epsilon)
        diff_log_dist = log_dist.repeat(m,1).t()-log_dist.repeat(m, 1)
        diff_log_gt_dist = log_gt_dist.repeat(m,1).t()-log_gt_dist.repeat(m, 1)
    else:
        if value.size()[1]<=10:
            value = softmax(value)
        if input.size()[1]<=10:
            input = softmax(input)
        gt_dist = value

        eps = 1e-4# / value[0]
        diff = torch.abs(value[0] - value[1:])
        out = torch.pow(diff, 2)
        gt_dist = torch.pow(out + eps, 1. / 2).sum(dim=1)

        # auxiliary variables
        idxs = torch.arange(1, m+1).cuda()
        indc = idxs.repeat(m,1).t() < idxs.repeat(m,1)

        epsilon = 1e-6
        dist = pdist.forward(a,p)
        if args.nolog:
            log_dist = dist
            log_gt_dist = gt_dist
        else:
            log_dist = torch.log(dist + epsilon)
            log_gt_dist = torch.log(gt_dist + epsilon)
        diff_log_dist = torch.abs(log_dist.repeat(m,1).t()-log_dist.repeat(m, 1))
        diff_log_gt_dist = torch.abs(log_gt_dist.repeat(m,1).t()-log_gt_dist.repeat(m, 1))

    # uniform weight coefficients 
    wgt = indc.clone().float()
    wgt = wgt.div(wgt.sum())

    if args.no_square:
        log_ratio_loss = (diff_log_dist-diff_log_gt_dist).abs()
    else:
        log_ratio_loss = (diff_log_dist-diff_log_gt_dist).pow(2)

    loss = log_ratio_loss
    loss = loss.mul(wgt).sum()
    return loss

##
# Train Utils
iters = 0

#
def train_epoch(models, criterion, optimizers, dataloaders, epoch, epoch_loss, vis=None, plot_data=None):
    models['backbone'].train()
    models['module'].train()
    global iters

    for data in tqdm(dataloaders['train'], leave=False, total=len(dataloaders['train'])):
        inputs = data[0].cuda()
        labels = data[1].cuda()
        iters += 1

        optimizers['backbone'].zero_grad()
        optimizers['module'].zero_grad()

        if args.lrl:
            models['backbone'].is_norm = args.is_norm
            models['module'].is_norm = args.is_norm
        scores, features2, features = models['backbone'](inputs)
        target_loss = criterion(scores, labels)

        if epoch > epoch_loss:
            # After 120 epochs, stop the gradient from the loss prediction module propagated to the target model.
            features[0] = features[0].detach()
            features[1] = features[1].detach()
            features[2] = features[2].detach()
            features[3] = features[3].detach()
        pred_loss, embed = models['module'](features)
        pred_loss = pred_loss.view(pred_loss.size(0))

        m_backbone_loss = torch.sum(target_loss) / target_loss.size(0)
        m_module_loss   = LossPredLoss(pred_loss, target_loss, margin=MARGIN)
        loss            = m_backbone_loss + WEIGHT * m_module_loss
        
        if args.lrl:
            if args.softmax or args.rule == "lplwsoftmax":
                loss2 = LogRatioLoss(scores,embed)
            elif args.onehot:
                labels=labels.unsqueeze(1)
                y_onehot = torch.FloatTensor(len(labels),10).cuda()
                y_onehot.zero_()
                y_onehot.scatter_(1,labels,1)
                loss2 = LogRatioLoss(y_onehot, embed)
            else:
                loss2 = LogRatioLoss(embed, features2)
            loss  = m_backbone_loss + WEIGHT * m_module_loss + args.lamb2 * loss2
        if args.triplet:
            loss += TripletLoss(features2,labels)
        loss.backward()
        optimizers['backbone'].step()
        optimizers['module'].step()

        # Visualize
        if (iters % 100 == 0) and (vis != None) and (plot_data != None):
            plot_data['X'].append(iters)
            plot_data['Y'].append([
                m_backbone_loss.item(),
                m_module_loss.item(),
                loss.item()
            ])
            vis.line(
                X=np.stack([np.array(plot_data['X'])] * len(plot_data['legend']), 1),
                Y=np.array(plot_data['Y']),
                opts={
                    'title': 'Loss over Time',
                    'legend': plot_data['legend'],
                    'xlabel': 'Iterations',
                    'ylabel': 'Loss',
                    'width': 1200,
                    'height': 390,
                },
                win=1
            )

#
def test(models, dataloaders, mode='val'):
    assert mode == 'val' or mode == 'test'
    models['backbone'].eval()
    models['module'].eval()

    total = 0
    correct = 0
    with torch.no_grad():
        for (inputs, labels) in dataloaders[mode]:
            inputs = inputs.cuda()
            labels = labels.cuda()

            scores, _,_ = models['backbone'](inputs)
            _, preds = torch.max(scores.data, 1)
            total += labels.size(0)
            correct += (preds == labels).sum().item()
    
    return 100 * correct / total

#
def train(models, criterion, optimizers, schedulers, dataloaders, num_epochs, epoch_loss, vis, plot_data):
    print('>> Train a Model.')
    best_acc = 0.
    checkpoint_dir = os.path.join('./cifar10', 'train', 'weights')
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)
    
    for epoch in range(num_epochs):
        schedulers['backbone'].step()
        schedulers['module'].step()

        train_epoch(models, criterion, optimizers, dataloaders, epoch, epoch_loss, vis, plot_data)

        # Save a checkpoint
        if False and epoch % 5 == 4:
            acc = test(models, dataloaders, 'test')
            if best_acc < acc:
                best_acc = acc
                torch.save({
                    'epoch': epoch + 1,
                    'state_dict_backbone': models['backbone'].state_dict(),
                    'state_dict_module': models['module'].state_dict()
                },
                '%s/active_resnet18_cifar10.pth' % (checkpoint_dir))
            print('Val Acc: {:.3f} \t Best Acc: {:.3f}'.format(acc, best_acc))
    print('>> Finished.')

#
def get_uncertainty(models, unlabeled_loader,labeled_loader=None):
    models['backbone'].eval()
    models['module'].eval()
    uncertainty = torch.tensor([]).cuda()
    
    if labeled_loader != None:
        with torch.no_grad():
            for (inputs,labeles) in labeled_loader:
                inputs = inputs.cuda()
                labeles = labeles.cuda()
                
                labeled_scores, labeled_feat,features = models['backbone'](inputs)
                pred_loss, labeled_embed = models['module'](features)
                break
                
    with torch.no_grad():
        for (inputs, labels) in unlabeled_loader:
            inputs = inputs.cuda()
            # labels = labels.cuda()

            scores, features2,features = models['backbone'](inputs)
            pred_loss, embed = models['module'](features) # pred_loss = criterion(scores, labels) # ground truth loss
            pred_loss = pred_loss.view(pred_loss.size(0))
            
            ### LL plus LRL = lpl
            if args.rule == "lpl":
                loss2 = LogRatioLoss(embed, features2)
                pred_loss = WEIGHT * pred_loss + args.lamb2 * loss2
            elif args.rule == "lplwsoftmax":
                loss2 = LogRatioLoss(scores,embed)
                pred_loss = WEIGHT * pred_loss + args.lamb2 * loss2
            ### pick by lrl only
            if args.rule in ["lrlonly", "lrlonlywsoftmax"]:
                for i in range(len(features2)):
                    labeled_embed[0] = embed[i]
                    if args.rule == "lrlonlywsoftmax":
                        labeled_scores[0] = scores[i]
                        loss2 = LogRatioLoss(labeled_embed, labeled_scores)
                    else:
                        labeled_feat[0] = features2[i]
                        loss2 = LogRatioLoss(labeled_embed, labeled_feat)
                    uncertainty = torch.cat((uncertainty, torch.tensor([loss2]).cuda()), 0)
            else:
                uncertainty = torch.cat((uncertainty, pred_loss), 0)
    
    return uncertainty.cpu()

def predict_prob(models, unlabeled_loader):
    models['backbone'].eval()
    models['module'].eval()
    probs = torch.tensor([]).cuda()

    with torch.no_grad():
        for (inputs, labels) in unlabeled_loader:
            inputs = inputs.cuda()
            out = models['backbone'](inputs)[0]
            prob = F.softmax(out, dim=1)
            probs = torch.cat((probs,prob),0)
    
    return probs.cpu()

##
# Main
if __name__ == '__main__':
    vis = visdom.Visdom(server='http://localhost', port=9000)
    plot_data = {'X': [], 'Y': [], 'legend': ['Backbone Loss', 'Module Loss', 'Total Loss']}
    collect_acc=[]
    for trial in range(TRIALS):
        # Initialize a labeled dataset by randomly sampling K=ADDENDUM=1,000 data points from the entire dataset.
        random.seed(0)
        if args.seed:
            torch.manual_seed(0)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False


        indices = list(range(NUM_TRAIN))
        collect_acc.append([])
        random.shuffle(indices)
        labeled_set = indices[:ADDENDUM]
        unlabeled_set = indices[ADDENDUM:]
        
        train_loader = DataLoader(cifar10_train, batch_size=BATCH, 
                                  sampler=SubsetRandomSampler(labeled_set), 
                                  pin_memory=True)
        test_loader  = DataLoader(cifar10_test, batch_size=BATCH)
        dataloaders  = {'train': train_loader, 'test': test_loader}
        
        # Model
        if args.data == "CIFAR100":
            resnet18    = resnet.ResNet18(num_classes=100).cuda()
        elif args.data in ["CIFAR10","SVHN"]:
            resnet18    = resnet.ResNet18(num_classes=10).cuda()
        else:
            resnet18    = resnet.ResNet18(num_classes=10,in_channel=1).cuda()
        loss_module = lossnet.LossNet().cuda()
        models      = {'backbone': resnet18, 'module': loss_module}
        torch.backends.cudnn.benchmark = True
        
        # Active learning cycles
        for cycle in range(CYCLES):
            # Loss, criterion and scheduler (re)initialization
            criterion      = nn.CrossEntropyLoss(reduction='none')
            optim_backbone = optim.SGD(models['backbone'].parameters(), lr=LR, 
                                    momentum=MOMENTUM, weight_decay=WDECAY)
            optim_module   = optim.SGD(models['module'].parameters(), lr=LR, 
                                    momentum=MOMENTUM, weight_decay=WDECAY)
            sched_backbone = lr_scheduler.MultiStepLR(optim_backbone, milestones=MILESTONES)
            sched_module   = lr_scheduler.MultiStepLR(optim_module, milestones=MILESTONES)

            optimizers = {'backbone': optim_backbone, 'module': optim_module}
            schedulers = {'backbone': sched_backbone, 'module': sched_module}

            # Training and test
            train(models, criterion, optimizers, schedulers, dataloaders, EPOCH, EPOCHL, vis, plot_data)
            acc = test(models, dataloaders, mode='test')
            collect_acc[-1]+=acc,
            print('Trial {}/{} || Cycle {}/{} || Label set size {}: Test acc {}'.format(trial+1, TRIALS, cycle+1, CYCLES, len(labeled_set), acc))

            ##
            #  Update the labeled dataset via loss prediction-based uncertainty measurement

            # Randomly sample 10000 unlabeled data points
            random.shuffle(unlabeled_set)
            subset = unlabeled_set[:SUBSET]
            labeledsubset = unlabeled_set[SUBSET:]

            # Create unlabeled dataloader for the unlabeled subset
            unlabeled_loader = DataLoader(cifar10_unlabeled, batch_size=BATCH, 
                                          sampler=SubsetSequentialSampler(subset), # more convenient if we maintain the order of subset
                                          pin_memory=True)
            labeled_loader = DataLoader(cifar10_unlabeled, batch_size=args.lrlbatch, 
                                          sampler=SubsetSequentialSampler(labeledsubset),
                                          pin_memory=True)


            if args.rule == "Entropy":
                probs = predict_prob(models, unlabeled_loader)
                log_probs = torch.log(probs)
                U = (probs*log_probs).sum(1)
                init = labeled_set[:]
                added_set = list(torch.tensor(subset)[U.sort()[1][:ADDENDUM]].numpy())
                labeled_set += list(torch.tensor(subset)[U.sort()[1][:ADDENDUM]].numpy())
                unlabeled_set = list(torch.tensor(subset)[U.sort()[1][ADDENDUM:]].numpy()) + unlabeled_set[SUBSET:]
            else:
                # Measure uncertainty of each data points in the subset
                if args.rule in ["lrlonly", "lrlonlywsoftmax"]:
                    uncertainty = get_uncertainty(models, unlabeled_loader, labeled_loader)
                else:
                    uncertainty = get_uncertainty(models, unlabeled_loader)

                # Index in ascending order
                arg = np.argsort(uncertainty)

                # Update the labeled dataset and the unlabeled dataset, respectively
                init = labeled_set[:]
                
                added_set = list(torch.tensor(subset)[arg][-ADDENDUM:].numpy())
                labeled_set += list(torch.tensor(subset)[arg][-ADDENDUM:].numpy())
                unlabeled_set = list(torch.tensor(subset)[arg][:-ADDENDUM].numpy()) + unlabeled_set[SUBSET:]
            
            # Create a new dataloader for the updated labeled dataset
            dataloaders['train'] = DataLoader(cifar10_train, batch_size=BATCH, 
                                              sampler=SubsetRandomSampler(labeled_set), 
                                              pin_memory=True)
            
            classes = ('plane', 'car', 'bird', 'cat', 'deer', 'dog', 'frog', 'horse', 'ship', 'truck')
            if args.savedata:
                import sys
                import matplotlib.pyplot as plt
                import torchvision
                plt.ion()
                a=args.query
                f, axarr = plt.subplots(a//10+1,10)
                k=0
                for i in added_set:
                    inputs, label = visual[i]
                    axarr[k].imshow(torchvision.utils.make_grid(inputs).numpy().transpose((1,2,0)))
                    axarr[k].set_title(classes[label])
                    k+=1
                plt.ioff()
                plt.savefig('temp.jpg')
                sys.exit()
            elif args.printdata:
                import sys
                print('init')
                num={ i:0 for i in classes}
                for i in init:
                    num[classes[visual[i][1]]]+=1
                for i in range(10):
                    print(classes[i] + ' :\t' + str(num[classes[i]]))
                print('added')
                num={ i:0 for i in classes}
                for i in added_set:
                    num[classes[visual[i][1]]]+=1
                for i in range(10):
                    print(classes[i] + ' :\t' + str(num[classes[i]]))
                sys.exit()
        
        # Save a checkpoint
        torch.save({
                    'trial': trial + 1,
                    'state_dict_backbone': models['backbone'].state_dict(),
                    'state_dict_module': models['module'].state_dict()
                },
                './cifar10/train/weights/active_resnet18_cifar10_trial{}.pth'.format(trial))
    import time
    timestr = "./results/" + time.strftime("%Y%m%d-%H%M%S")
    with open(timestr + 'output.txt','w') as f:
        f.write(str(collect_acc))
        f.write('\n'+str(args))
