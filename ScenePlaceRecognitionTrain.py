# Train the NetVLAD network

from __future__ import print_function
import argparse
from math import ceil
import random, shutil, json
from os.path import join, exists, isfile
from os import makedirs, remove

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SubsetRandomSampler
from torch.utils.data.dataset import Subset
from datetime import datetime
import torchvision.models as models
import h5py
import faiss

from tensorboardX import SummaryWriter
import numpy as np
import netvlad

import warnings
import arguments


# --optim=ADAM --resume=runs/Dec29_14-09-36_alexnet_netvlad/ --mode=train --arch=alexnet --pooling=netvlad --num_clusters=64 --start-epoch=30 --nEpochs=45
# --mode=test --split=val --resume=runs/Dec29_14-09-36_alexnet_netvlad/ --ckpt=best

parser = argparse.ArgumentParser(description='ScenePlaceRecognitionTrain')
parser.add_argument('--mode', type=str, default='train', help='Mode', choices=['train', 'cluster'])
parser.add_argument('--batchSize', type=int, default=2,
                    help='Number of triplets (query, pos, negs). Each triplet consists of 12 images.')
parser.add_argument('--cacheBatchSize', type=int, default=4, help='Batch size for caching and testing')
parser.add_argument('--cacheRefreshRate', type=int, default=1000,
                    help='How often to refresh cache, in number of queries. 0 for off')
parser.add_argument('--nEpochs', type=int, default=40, help='number of epochs to train for')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--nGPU', type=int, default=1, help='number of GPU to use.')
parser.add_argument('--optim', type=str, default='SGD', help='optimizer to use', choices=['SGD', 'ADAM'])
parser.add_argument('--lr', type=float, default=0.0001, help='Learning Rate.')
parser.add_argument('--lrStep', type=float, default=5, help='Decay LR ever N steps.')
parser.add_argument('--lrGamma', type=float, default=0.5, help='Multiply LR by Gamma for decaying.')
parser.add_argument('--weightDecay', type=float, default=0.001, help='Weight decay for SGD.')
parser.add_argument('--momentum', type=float, default=0.9, help='Momentum for SGD.')
parser.add_argument('--nocuda', action='store_true', help='Dont use cuda')
parser.add_argument('--threads', type=int, default=8, help='Number of threads for each data loader to use')
parser.add_argument('--seed', type=int, default=123, help='Random seed to use.')
parser.add_argument('--dataPath', type=str, default='data/', help='Path for centroid data.')
parser.add_argument('--runsPath', type=str, default='runs/', help='Path to save runs to.')
parser.add_argument('--savePath', type=str, default='checkpoints/',
                    help='Path to save checkpoints to in logdir. Default=checkpoints/')
parser.add_argument('--cachePath', type=str, default='/tmp/', help='Path to save cache to.')
parser.add_argument('--resume', type=str, default='/home/ricky/ScenePlaceRecognition',
                    help='Path to load checkpoint from, for resuming training or testing.')
parser.add_argument('--ckpt', type=str, default='latest',
                    help='Resume from latest or best checkpoint.', choices=['latest', 'best'])
parser.add_argument('--evalEvery', type=int, default=1,
                    help='Do a validation set run, and save, every N epochs.')
parser.add_argument('--patience', type=int, default=10, help='Patience for early stopping. 0 is off.')
parser.add_argument('--dataset', type=str, default='highway',
                    help='DataSet to use', choices=['pittsburgh', 'tokyo247', 'highway'])
parser.add_argument('--arch', type=str, default='resnet18',
                    help='basenetwork to use', choices=['vgg16', 'alexnet', 'resnet18'])
parser.add_argument('--pooling', type=str, default='netvlad', help='type of pooling to use',
                    choices=['netvlad', 'max', 'avg'])
parser.add_argument('--num_clusters', type=int, default=64, help='Number of NetVlad clusters. Default=64')
parser.add_argument('--margin', type=float, default=0.1, help='Margin for triplet loss. Default=0.1')
parser.add_argument('--split', type=str, default='val', help='Data split to use for testing. Default is val',
                    choices=['test', 'test250k', 'train', 'val'])
parser.add_argument('--fromscratch', action='store_true', help='Train from scratch rather than using pretrained models')
parser.add_argument('--panoramicCrop', type=int, default=8, help='Number of panoramic crops')


def train(epoch):
    epoch_loss = 0
    startIter = 1  # keep track of batch iter across subsets for logging

    # 分割训练数据，每块大小为使用cacheRefreshRate
    if opt.cacheRefreshRate > 0:
        subsetN = ceil(len(train_set) / opt.cacheRefreshRate)
        # TODO randomise the arange before splitting?
        subsetIdx = np.array_split(np.arange(len(train_set)), subsetN)
    else:
        subsetN = 1
        subsetIdx = [np.arange(len(train_set))]

    nBatches = (len(train_set) + opt.batchSize - 1) // opt.batchSize

    for subIter in range(subsetN):
        # 对于所有训练数据，建立缓存
        print('====> Building Cache')
        model.eval()
        train_set.cache = join(opt.cachePath, train_set.whichSet + '_feat_cache.hdf5')
        with h5py.File(train_set.cache, mode='w') as h5:
            pool_size = encoder_dim
            if opt.pooling.lower() == 'netvlad': pool_size *= opt.num_clusters
            h5feat = h5.create_dataset("features",
                                       [len(whole_train_set), pool_size],
                                       dtype=np.float32)#float32
            with torch.no_grad():
                for iteration, (input, indices) in enumerate(whole_training_data_loader, 1):
                    if opt.panoramicCrop > 1:
                        input_batches = torch.empty(opt.panoramicCrop*opt.cacheBatchSize, 3, 224, 224)
                        for idx_batch in range(opt.cacheBatchSize):
                            for idx in range(opt.panoramicCrop):
                                input_batches[idx+idx_batch*opt.cacheBatchSize, :, :, 0:224] \
                                    = input[idx_batch, :, :, idx * 224:(idx + 1) * 224]
                        input_batches = input_batches.to(device)
                    else:
                        input_batches = input.to(device)
                    image_encoding = model.encoder(input_batches)
                    vlad_encoding = model.pool(image_encoding)

                    # 将各个batch相加
                    if opt.panoramicCrop > 1:
                        vlad_encoding_batches = torch.empty(int(vlad_encoding.shape[0]/opt.panoramicCrop),
                                                            vlad_encoding.shape[1])
                        if vlad_encoding.shape[0] > opt.panoramicCrop:
                            for ii in range(1, vlad_encoding.shape[0], opt.panoramicCrop):
                                tmpsum = torch.sum(vlad_encoding[ii:ii + opt.panoramicCrop, :], dim=0)
                                vlad_encoding_batches[ii//opt.panoramicCrop, :] = tmpsum

                    else:
                        vlad_encoding_batches = vlad_encoding
                    h5feat[indices.detach().numpy(), :] = vlad_encoding_batches.detach().cpu().numpy()
                    del input, image_encoding, vlad_encoding, vlad_encoding_batches

        sub_train_set = Subset(dataset=train_set, indices=subsetIdx[subIter])
        training_data_loader = DataLoader(dataset=sub_train_set, num_workers=opt.threads,
                                          batch_size=opt.batchSize, shuffle=True,
                                          collate_fn=dataset.collate_fn, pin_memory=cuda)
        print('Allocated:', torch.cuda.memory_allocated())#Returns the current GPU memory usage by tensors in bytes for a given device.
        print('Cached:', torch.cuda.memory_cached())#Returns the current GPU memory managed by the caching allocator in bytes for a given device.

        # 训练数据
        model.train()
        for iteration, (query, positives, negatives,
                        negCounts, indices) in enumerate(training_data_loader, startIter):
            # some reshaping to put query, pos, negs in a single (N, 3, H, W) tensor
            # where N = batchSize * (nQuery + nPos + nNeg)
            if query is None: continue  # in case we get an empty batch

            B, C, H, W = query.shape
            nNeg = torch.sum(negCounts)
            input = torch.cat([query, positives, negatives])

            input = input.to(device)
            image_encoding = model.encoder(input)
            vlad_encoding = model.pool(image_encoding)

            vladQ, vladP, vladN = torch.split(vlad_encoding, [B, B, nNeg])

            optimizer.zero_grad()

            # calculate loss for each Query, Positive, Negative triplet
            # due to potential difference in number of negatives have to
            # do it per query, per negative
            loss = 0
            for i, negCount in enumerate(negCounts):
                for n in range(negCount):
                    negIx = (torch.sum(negCounts[:i]) + n).item()
                    loss += criterion(vladQ[i:i + 1], vladP[i:i + 1], vladN[negIx:negIx + 1])

            loss /= nNeg.float().to(device)  # normalise by actual number of negatives
            loss.backward()
            optimizer.step()
            del input, image_encoding, vlad_encoding, vladQ, vladP, vladN
            del query, positives, negatives

            batch_loss = loss.item()
            epoch_loss += batch_loss

            if iteration % 50 == 0 or nBatches <= 10:
                print("==> Epoch[{}]({}/{}): Loss: {:.4f}".format(epoch, iteration,
                                                                  nBatches, batch_loss), flush=True)
                writer.add_scalar('Train/Loss', batch_loss,
                                  ((epoch - 1) * nBatches) + iteration)
                writer.add_scalar('Train/nNeg', nNeg,
                                  ((epoch - 1) * nBatches) + iteration)
                print('Allocated:', torch.cuda.memory_allocated())
                print('Cached:', torch.cuda.memory_cached())

        startIter += len(training_data_loader)
        del training_data_loader, loss
        optimizer.zero_grad()
        torch.cuda.empty_cache()
        remove(train_set.cache)  # delete HDF5 cache

    avg_loss = epoch_loss / nBatches

    print("===> Epoch {} Complete: Avg. Loss: {:.4f}".format(epoch, avg_loss),
          flush=True)
    writer.add_scalar('Train/AvgLoss', avg_loss, epoch)

def testDataset(eval_set, epoch=0, write_tboard=False):
    # TODO what if features dont fit in memory?
    test_data_loader = DataLoader(dataset=eval_set,
                                  num_workers=opt.threads, batch_size=opt.cacheBatchSize, shuffle=False,
                                  pin_memory=cuda)

    model.eval()
    with torch.no_grad():#不会反向传播，提高inference速度
        print('====> Extracting Features')
        pool_size = encoder_dim
        if opt.pooling.lower() == 'netvlad': pool_size *= opt.num_clusters
        dbFeat = np.empty((len(eval_set), pool_size))

        for iteration, (input, indices) in enumerate(test_data_loader, 1):
            input = input.to(device)

            input1 = input[ :, :, :, 0:224]
            input2 = input[ :, :, :, 224:448]
            input3 = input[ :, :, :, 448:672]
            input4 = input[ :, :, :, 672:896]

            # forward pass
            image_encoding1 = model.encoder(input1)
            image_encoding2 = model.encoder(input2)
            image_encoding3 = model.encoder(input3)
            image_encoding4 = model.encoder(input4)

            vlad_encoding1 = model.pool(image_encoding1)
            vlad_encoding2 = model.pool(image_encoding2)
            vlad_encoding3 = model.pool(image_encoding3)
            vlad_encoding4 = model.pool(image_encoding4)

            # if  iteration<eval_set.numDb:
            #     vlad_encoding=torch.cat((vlad_encoding1,vlad_encoding2),1)
            # else:
            #     vlad_encoding = torch.cat((vlad_encoding2, vlad_encoding1),1)
            vlad_encoding = vlad_encoding1+vlad_encoding2+vlad_encoding3+vlad_encoding4

            dbFeat[indices.detach().numpy(), :] = vlad_encoding.detach().cpu().numpy()
            if iteration % 50 == 0 or len(test_data_loader) <= 10:
                print("==> Batch ({}/{})".format(iteration,
                                                 len(test_data_loader)), flush=True)

            del input, image_encoding1, image_encoding2, image_encoding3, image_encoding4,\
                vlad_encoding, vlad_encoding1, vlad_encoding2, vlad_encoding3, vlad_encoding4
    del test_data_loader

    # extracted for both db and query, now split in own sets
    #qFeat = dbFeat[eval_set.dbStruct.numDb:].astype('float32')#float32
    #dbFeat = dbFeat[:eval_set.dbStruct.numDb].astype('float32')#float32
    qFeat = dbFeat[eval_set.numDb:].astype('float32')#float32
    dbFeat = dbFeat[:eval_set.numDb].astype('float32')#float32

    print('====> Building faiss index')
    faiss_index = faiss.IndexFlatL2(pool_size)
    faiss_index.add(dbFeat)

    print('====> Calculating recall @ N')
    #n_values = [1, 5, 10, 20]
    distances, predictions = faiss_index.search(qFeat, 1)

    fp = 0
    tp = 0
    for i in range(0,len(predictions)):
        if abs(predictions[i] - i)<=5:
            tp=tp+1
        else:
            fp=fp+1

    precision = tp/(tp+fp)
    recall = 1
    f1=2*precision/(precision+recall)

    print(['F1=', f1 ])



    return distances, predictions, f1

    #_, predictions = faiss_index.search(qFeat, max(n_values))

    # # for each query get those within threshold distance
    # gt = eval_set.getPositives()
    #
    # correct_at_n = np.zeros(len(n_values))
    # # TODO can we do this on the matrix in one go?
    # for qIx, pred in enumerate(predictions):
    #     for i, n in enumerate(n_values):
    #         # if in top N then also in top NN, where NN > N
    #         if np.any(np.in1d(pred[:n], gt[qIx])):
    #             correct_at_n[i:] += 1
    #             break
    # recall_at_n = correct_at_n / eval_set.dbStruct.numQ
    #
    # recalls = {}  # make dict for output
    # for i, n in enumerate(n_values):
    #     recalls[n] = recall_at_n[i]
    #     print("====> Recall@{}: {:.4f}".format(n, recall_at_n[i]))

    #return recalls

def test(eval_set, epoch=0, write_tboard=False):
    # TODO what if features dont fit in memory?
    test_data_loader = DataLoader(dataset=eval_set,
                                  num_workers=opt.threads, batch_size=opt.cacheBatchSize, shuffle=False,
                                  pin_memory=cuda)

    model.eval()
    with torch.no_grad():
        print('====> Extracting Features')
        pool_size = encoder_dim
        if opt.pooling.lower() == 'netvlad': pool_size *= opt.num_clusters
        dbFeat = np.empty((len(eval_set), pool_size))

        for iteration, (input, indices) in enumerate(test_data_loader, 1):
            input = input.to(device)
            image_encoding = model.encoder(input)
            vlad_encoding = model.pool(image_encoding)

            dbFeat[indices.detach().numpy(), :] = vlad_encoding.detach().cpu().numpy()
            if iteration % 50 == 0 or len(test_data_loader) <= 10:
                print("==> Batch ({}/{})".format(iteration,
                                                 len(test_data_loader)), flush=True)

            del input, image_encoding, vlad_encoding
    del test_data_loader

    # extracted for both db and query, now split in own sets
    qFeat = dbFeat[eval_set.dbStruct.numDb:].astype('float32')#float32
    dbFeat = dbFeat[:eval_set.dbStruct.numDb].astype('float32')#float32

    print('====> Building faiss index')
    faiss_index = faiss.IndexFlatL2(pool_size)
    faiss_index.add(dbFeat)

    print('====> Calculating recall @ N')
    n_values = [1, 5, 10, 20]

    _, predictions = faiss_index.search(qFeat, max(n_values))

    # for each query get those within threshold distance
    gt = eval_set.getPositives()

    correct_at_n = np.zeros(len(n_values))
    # TODO can we do this on the matrix in one go?
    for qIx, pred in enumerate(predictions):
        for i, n in enumerate(n_values):
            # if in top N then also in top NN, where NN > N
            if np.any(np.in1d(pred[:n], gt[qIx])):
                correct_at_n[i:] += 1
                break
    recall_at_n = correct_at_n / eval_set.dbStruct.numQ

    recalls = {}  # make dict for output
    for i, n in enumerate(n_values):
        recalls[n] = recall_at_n[i]
        print("====> Recall@{}: {:.4f}".format(n, recall_at_n[i]))
        if write_tboard: writer.add_scalar('Val/Recall@' + str(n), recall_at_n[i], epoch)

    return recalls


def get_clusters(cluster_set):
    nDescriptors = 50000
    nPerImage = 100
    nIm = ceil(nDescriptors / nPerImage)

    sampler = SubsetRandomSampler(np.random.choice(len(cluster_set), nIm, replace=False))
    data_loader = DataLoader(dataset=cluster_set,
                             num_workers=opt.threads, batch_size=opt.cacheBatchSize, shuffle=False,
                             pin_memory=cuda,
                             sampler=sampler)

    if not exists(join(opt.dataPath, 'centroids')):
        makedirs(join(opt.dataPath, 'centroids'))

    initcache = join(opt.dataPath, 'centroids',
                     opt.arch + '_' + cluster_set.dataset + '_' + str(opt.num_clusters) + '_desc_cen.hdf5')
    with h5py.File(initcache, mode='w') as h5:
        with torch.no_grad():
            model.eval()
            print('====> Extracting Descriptors')
            dbFeat = h5.create_dataset("descriptors",
                                       [nDescriptors, encoder_dim],
                                       dtype=np.float32)#float32

            for iteration, (input, indices) in enumerate(data_loader, 1):
                input = input.to(device)
                image_descriptors = model.encoder(input).view(input.size(0), encoder_dim, -1).permute(0, 2, 1)

                batchix = (iteration - 1) * opt.cacheBatchSize * nPerImage
                for ix in range(image_descriptors.size(0)):
                    # sample different location for each image in batch
                    sample = np.random.choice(image_descriptors.size(1), nPerImage, replace=False)
                    startix = batchix + ix * nPerImage
                    dbFeat[startix:startix + nPerImage, :] = image_descriptors[ix, sample, :].detach().cpu().numpy()

                if iteration % 50 == 0 or len(data_loader) <= 10:
                    print("==> Batch ({}/{})".format(iteration,
                                                     ceil(nIm / opt.cacheBatchSize)), flush=True)
                del input, image_descriptors

        print('====> Clustering..')
        niter = 100
        kmeans = faiss.Kmeans(encoder_dim, opt.num_clusters, niter, verbose=False)
        kmeans.train(dbFeat[...])

        print('====> Storing centroids', kmeans.centroids.shape)
        h5.create_dataset('centroids', data=kmeans.centroids)
        print('====> Done!')


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    model_out_path = join(opt.savePath, filename)
    torch.save(state, model_out_path)
    if is_best:
        shutil.copyfile(model_out_path, join(opt.savePath, 'model_best.pth.tar'))


class Flatten(nn.Module):
    def forward(self, input):
        return input.view(input.size(0), -1)


class L2Norm(nn.Module):
    def __init__(self, dim=1):
        super().__init__()
        self.dim = dim

    def forward(self, input):
        return F.normalize(input, p=2, dim=self.dim)


if __name__ == "__main__":
    # ignore warnings -- UserWarning: Loky-backed parallel loops cannot be called in a multiprocessing, setting n_jobs=1
    warnings.filterwarnings("ignore")

    ## read arguments from command or json file
    opt = parser.parse_args()
    restore_var = ['lr', 'lrStep', 'lrGamma', 'weightDecay', 'momentum',
                   'runsPath', 'savePath', 'arch', 'num_clusters', 'pooling', 'optim',
                   'margin', 'seed', 'patience']
    if opt.resume:
        opt = arguments.readArguments(opt, parser, restore_var)
    print(opt)

    ## desinate the device to train
    cuda = not opt.nocuda
    if cuda and not torch.cuda.is_available():
        raise Exception("No GPU found, please run with --nocuda")
    device = torch.device("cuda" if cuda else "cpu")

    random.seed(opt.seed)
    np.random.seed(opt.seed)
    torch.manual_seed(opt.seed)
    if cuda:
        torch.cuda.manual_seed(opt.seed)

    ## designate the dataset to train
    if opt.dataset.lower() == 'pittsburgh':
        from DataSet import pittsburgh as dataset
    elif opt.dataset.lower() == 'tokyo247':
        from DataSet import tokyo247 as dataset
    elif opt.dataset.lower() == 'highway':
        from DataSet import HighwayTrain as dataset
    else:
        raise Exception('Unknown dataset')

    ## read image files of the desinated dataset
    print('===> Loading dataset(s)')
    if opt.mode.lower() == 'train':
        whole_train_set = dataset.get_whole_training_set()
        whole_training_data_loader = DataLoader(dataset=whole_train_set,
                                                num_workers=opt.threads, batch_size=opt.cacheBatchSize, shuffle=False,
                                                pin_memory=cuda)
        train_set = dataset.get_training_query_set(opt.margin)

        print('====> Training query set:', len(train_set))
        whole_test_set = dataset.get_whole_val_set()
        print('===> Evaluating on val set, query count:', whole_test_set.dbStruct.numQ)
    elif opt.mode.lower() == 'cluster':
        whole_train_set = dataset.get_whole_training_set(onlyDB=True)
    else:
        raise Exception('Unknown mode')

## 构造网络模型
    print('===> Building model')
    pretrained = not opt.fromscratch
    if opt.arch.lower() == 'alexnet':
        encoder_dim = 256
        encoder = models.alexnet(pretrained=pretrained)
        # capture only features and remove last relu and maxpool
        layers = list(encoder.features.children())[:-2]

        if pretrained:
            # if using pretrained only train conv5
            for l in layers[:-1]:
                for p in l.parameters():
                    p.requires_grad = False

    elif opt.arch.lower() == 'vgg16':
        encoder_dim = 512
        encoder = models.vgg16(pretrained=pretrained)
        # capture only feature part and remove last relu and maxpool
        layers = list(encoder.features.children())[:-2]

        if pretrained:
            # if using pretrained then only train conv5_1, conv5_2, and conv5_3
            for l in layers[:-5]:
                for p in l.parameters():
                    p.requires_grad = False

    elif opt.arch.lower() == 'resnet18':
        encoder_dim = 512
        # loading resnet18 of trained on places365 as basenet
        from Place365 import wideresnet
        model_file = 'Place365/wideresnet18_places365.pth.tar'
        modelResNet = wideresnet.resnet18(num_classes=365)
        # load object saved with torch.save() from a file, with funtion specifiying how to remap storage locations in the parameter list
        checkpoint = torch.load(model_file, map_location=lambda storage, loc: storage) #gpu->cpu, why?!
        state_dict = {str.replace(k,'module.',''): v for k,v in checkpoint['state_dict'].items()} # 去掉module.字样
        modelResNet.load_state_dict(state_dict)

        layers = list(modelResNet.children())[:-2]        # children()只包括了第一代儿子模块，get rid of the last two layers: avepool & fc
        # 让最后1\2个block参与netVLAD训练
        for l in layers[:-2]:
            for p in l.parameters():
                p.requires_grad = False

    if opt.mode.lower() == 'cluster':  # and opt.vladv2 == False #TODO add v1 v2 switching as flag
        layers.append(L2Norm())

    encoder = nn.Sequential(*layers)
    model = nn.Module()
    model.add_module('encoder', encoder)

### 初始化model中的pooling模块
    if opt.mode.lower() != 'cluster':
        if opt.pooling.lower() == 'netvlad':
            net_vlad = netvlad.NetVLAD(num_clusters=opt.num_clusters, dim=encoder_dim, vladv2=False)
            if not opt.resume:
                if opt.mode.lower() == 'train':
                    initcache = join(opt.dataPath, 'centroids', opt.arch + '_' + train_set.dataset + '_' + str(
                        opt.num_clusters) + '_desc_cen.hdf5')
                else:
                    initcache = join(opt.dataPath, 'centroids', opt.arch + '_' + whole_test_set.dataset + '_' + str(
                        opt.num_clusters) + '_desc_cen.hdf5')

                if not exists(initcache):
                    raise FileNotFoundError('Could not find clusters, please run with --mode=cluster before proceeding')

                with h5py.File(initcache, mode='r') as h5:
                    clsts = h5.get("centroids")[...]
                    traindescs = h5.get("descriptors")[...]
                    net_vlad.init_params(clsts, traindescs)
                    del clsts, traindescs

            model.add_module('pool', net_vlad)
        elif opt.pooling.lower() == 'max':
            global_pool = nn.AdaptiveMaxPool2d((1, 1))
            model.add_module('pool', nn.Sequential(*[global_pool, Flatten(), L2Norm()]))
        elif opt.pooling.lower() == 'avg':
            global_pool = nn.AdaptiveAvgPool2d((1, 1))
            model.add_module('pool', nn.Sequential(*[global_pool, Flatten(), L2Norm()]))
        else:
            raise ValueError('Unknown pooling type: ' + opt.pooling)

    isParallel = False
    if opt.nGPU > 1 and torch.cuda.device_count() > 1:
        model.encoder = nn.DataParallel(model.encoder)
        if opt.mode.lower() != 'cluster':
            model.pool = nn.DataParallel(model.pool)
        isParallel = True

    if not opt.resume:
        model = model.to(device)

## 定义优化器和损失函数
    if opt.mode.lower() == 'train':
        if opt.optim.upper() == 'ADAM':
            optimizer = optim.Adam(filter(lambda p: p.requires_grad,
                                          model.parameters()), lr=opt.lr)  # , betas=(0,0.9))
        elif opt.optim.upper() == 'SGD':
            optimizer = optim.SGD(filter(lambda p: p.requires_grad,
                                         model.parameters()), lr=opt.lr,
                                  momentum=opt.momentum,
                                  weight_decay=opt.weightDecay)

            scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=opt.lrStep, gamma=opt.lrGamma)
        else:
            raise ValueError('Unknown optimizer: ' + opt.optim)

        # original paper/code doesn't sqrt() the distances, we do, so sqrt() the margin, I think :D
        criterion = nn.TripletMarginLoss(margin=opt.margin ** 0.5,
                                         p=2, size_average=False).to(device)  # reduction='sum'

## 读入预先训练结果
    if opt.resume:
        if opt.ckpt.lower() == 'latest':
            resume_ckpt = join(opt.resume, 'checkpoints', 'checkpoint.pth.tar')
        elif opt.ckpt.lower() == 'best':
            resume_ckpt = join(opt.resume, 'checkpoints', 'model_best.pth.tar')

        if isfile(resume_ckpt):
            print("=> loading checkpoint '{}'".format(resume_ckpt))
            checkpoint = torch.load(resume_ckpt, map_location=lambda storage, loc: storage)
            opt.start_epoch = checkpoint['epoch']
            best_metric = checkpoint['best_score']
            model.load_state_dict(checkpoint['state_dict'])
            model = model.to(device)
            if opt.mode == 'train':
                optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(resume_ckpt, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(resume_ckpt))

## 执行test/cluster/train操作
    if opt.mode.lower() == 'test':
        print('===> Running evaluation step')
        epoch = 1
        #recalls = test(whole_test_set, epoch, write_tboard=False)

        distances, predictions = testDataset(whole_test_set, epoch, write_tboard=False)
        np.savetxt('distances.txt', distances)
        np.savetxt('predictions.txt', predictions)
    elif opt.mode.lower() == 'cluster':
        print('===> Calculating descriptors and clusters')
        get_clusters(whole_train_set)
    elif opt.mode.lower() == 'train':
        print('===> Training model')
        writer = SummaryWriter(
            log_dir=join(opt.runsPath, datetime.now().strftime('%b%d_%H-%M-%S') + '_' + opt.arch + '_' + opt.pooling))

        # write checkpoints in logdir
        logdir = writer.file_writer.get_logdir()
        # opt.savePath = join(logdir, opt.savePath)
        if not opt.resume:
            makedirs(opt.savePath)

        with open(join(opt.savePath, 'flags.json'), 'w') as f:
            f.write(json.dumps(
                {k: v for k, v in vars(opt).items()}
            ))
        print('===> Saving state to:', logdir)

        not_improved = 0
        best_score = 0
        for epoch in range(opt.start_epoch + 1, opt.nEpochs + 1):
            if opt.optim.upper() == 'SGD':
                scheduler.step(epoch)
            train(epoch)
            if (epoch % opt.evalEvery) == 0:
            #     recalls = test(whole_test_set, epoch, write_tboard=True)
            #     is_best = recalls[5] > best_score
                is_best = False
            #     if is_best:
            #         not_improved = 0
            #         best_score = recalls[5]
            #     else:
            #         not_improved += 1
            #
                save_checkpoint({
                    'epoch': epoch,
                    'state_dict': model.state_dict(),
                    ##'recalls': recalls,
                    'best_score': best_score,
                    'optimizer': optimizer.state_dict(),
                    'parallel': isParallel,
                }, is_best)
            #
            #     if opt.patience > 0 and not_improved > (opt.patience / opt.evalEvery):
            #         print('Performance did not improve for', opt.patience, 'epochs. Stopping.')
            #         break

        # print("=> Best Recall@5: {:.4f}".format(best_score), flush=True)
        writer.close()
